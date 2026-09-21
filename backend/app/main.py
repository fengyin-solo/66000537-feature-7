import asyncio, math, random, time, json, threading, sqlite3
from collections import deque
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import numpy as np

app = FastAPI(title="Digital Twin Factory Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEVICE_TYPES = ["CNC", "RobotArm", "Conveyor", "AGV", "InjectionMolding", "QCStation"]
DEVICE_TYPE_LABELS = {
    "CNC": "CNC机床", "RobotArm": "机械臂", "Conveyor": "传送带",
    "AGV": "AGV小车", "InjectionMolding": "注塑机", "QCStation": "质检站",
}
STATUSES = ["RUNNING", "IDLE", "FAULT", "OFFLINE"]
ACTIVE_CLIENTS: list[WebSocket] = []
SIMULATOR_RUNNING = True
LOCK = threading.RLock()
EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


def broadcast(msg: str):
    """线程安全地把消息推给所有 WS 客户端（模拟器线程 -> 主事件循环）。"""
    if not ACTIVE_CLIENTS or EVENT_LOOP is None:
        return
    dead = []
    for ws in ACTIVE_CLIENTS:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(msg), EVENT_LOOP)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ACTIVE_CLIENTS:
            ACTIVE_CLIENTS.remove(ws)

HISTORY_LIMIT = 1800          # 每台设备保留的采样点数（约30分钟，1点/秒）
TREND_MIN_SAMPLES = 8         # 温度趋势判定所需最少样本
TREND_DELTA = 3.0             # 温度趋势上升阈值(°C)

# ---------------------------------------------------------------------------
# 持久化（SQLite）：按设备类型的判定规则 + 生效方式选择，重启后沿用
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "factory.db"

# 每类设备的默认判定上限 / 周期 / 连续超限次数
DEFAULT_RULES = {
    "CNC":              {"temp_limit": 55.0, "vib_limit": 2.2, "pres_limit": 1.6, "period": 10, "consecutive": 1},
    "RobotArm":         {"temp_limit": 50.0, "vib_limit": 2.0, "pres_limit": 1.5, "period": 10, "consecutive": 1},
    "Conveyor":         {"temp_limit": 48.0, "vib_limit": 1.6, "pres_limit": 1.5, "period": 10, "consecutive": 1},
    "AGV":              {"temp_limit": 46.0, "vib_limit": 1.8, "pres_limit": 1.4, "period": 10, "consecutive": 1},
    "InjectionMolding": {"temp_limit": 60.0, "vib_limit": 2.5, "pres_limit": 1.8, "period": 10, "consecutive": 1},
    "QCStation":        {"temp_limit": 45.0, "vib_limit": 1.5, "pres_limit": 1.3, "period": 10, "consecutive": 1},
}

# 字段取值边界（为空 / 非数字 / 越界 / 填反 时拦截）
BOUNDS = {
    "temp_limit":   {"label": "高温判定上限(°C)", "min": 25.0,  "max": 100.0},
    "vib_limit":    {"label": "振动超标上限(mm/s)", "min": 0.1,  "max": 10.0},
    "pres_limit":   {"label": "压力异常上限(MPa)", "min": 0.1,  "max": 10.0},
    "period":       {"label": "判定周期(个采样点)", "min": 1,     "max": 300},
    "consecutive":  {"label": "连续超限次数(次)",   "min": 1,     "max": 300},
}


def db_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anomaly_rules (
                device_type TEXT PRIMARY KEY,
                temp_limit REAL NOT NULL,
                vib_limit REAL NOT NULL,
                pres_limit REAL NOT NULL,
                period INTEGER NOT NULL,
                consecutive INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        for dtype in DEVICE_TYPES:
            row = conn.execute(
                "SELECT device_type FROM anomaly_rules WHERE device_type=?", (dtype,)
            ).fetchone()
            if not row:
                d = DEFAULT_RULES[dtype]
                conn.execute(
                    "INSERT INTO anomaly_rules(device_type,temp_limit,vib_limit,pres_limit,"
                    "period,consecutive,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (dtype, d["temp_limit"], d["vib_limit"], d["pres_limit"],
                     d["period"], d["consecutive"], time.time()),
                )
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)",
            ("apply_mode", "future"),
        )


def load_rules() -> dict:
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM anomaly_rules").fetchall()
    out = {}
    for r in rows:
        out[r["device_type"]] = {
            "device_type": r["device_type"],
            "temp_limit": r["temp_limit"], "vib_limit": r["vib_limit"],
            "pres_limit": r["pres_limit"], "period": r["period"],
            "consecutive": r["consecutive"], "updated_at": r["updated_at"],
        }
    return out


def load_apply_mode() -> str:
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key='apply_mode'").fetchone()
    return row["value"] if row else "future"


def save_rule(dtype: str, r: dict):
    with db_conn() as conn:
        conn.execute(
            "UPDATE anomaly_rules SET temp_limit=?,vib_limit=?,pres_limit=?,"
            "period=?,consecutive=?,updated_at=? WHERE device_type=?",
            (r["temp_limit"], r["vib_limit"], r["pres_limit"], r["period"],
             r["consecutive"], time.time(), dtype),
        )


def save_apply_mode(mode: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value) VALUES('apply_mode',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (mode,),
        )


# ---------------------------------------------------------------------------
# 规则校验
# ---------------------------------------------------------------------------
def _to_number(raw, field):
    """空值 / 非数字拦截；整数位字段必须为正整数。"""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None, f"{BOUNDS[field]['label']}不能为空"
    if isinstance(raw, bool):
        return None, f"{BOUNDS[field]['label']}必须为数字"
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"{BOUNDS[field]['label']}必须为数字"
    if not math.isfinite(v):
        return None, f"{BOUNDS[field]['label']}必须为有限数字"
    if field in ("period", "consecutive"):
        if v != int(v):
            return None, f"{BOUNDS[field]['label']}必须为整数"
        v = int(v)
    return v, None


def validate_rule(raw: dict):
    """返回 (clean_dict, {field: 错误说明})。上限填反/越界/为空均判不合格。"""
    errors = {}
    clean = {}
    for field in ("temp_limit", "vib_limit", "pres_limit", "period", "consecutive"):
        v, err = _to_number(raw.get(field), field)
        if err:
            errors[field] = err
            continue
        b = BOUNDS[field]
        if v < b["min"] or v > b["max"]:
            unit = "个采样点" if field == "period" else ("次" if field == "consecutive" else "")
            rng = f"{b['min']:g}~{b['max']:g}" + unit
            errors[field] = f"{b['label']}越界，允许范围 {rng}"
            continue
        clean[field] = v

    # 周期与连续次数互为约束（防止二者填反）
    if "period" in clean and "consecutive" in clean:
        if clean["consecutive"] > clean["period"]:
            errors["consecutive"] = (
                f"连续超限次数({clean['consecutive']})不能大于判定周期"
                f"({clean['period']})，两项疑似填反"
            )
    return clean, errors


# ---------------------------------------------------------------------------
# 设备与模拟状态
# ---------------------------------------------------------------------------
class DeviceState:
    def __init__(self, did: int, dtype: str, x: float, y: float, z: float):
        self.id = did
        self.type = dtype
        self.status = "RUNNING"
        self.position = [x, y, z]
        self.temperature = random.uniform(35, 45)
        self.vibration = random.uniform(0.1, 1.5)
        self.pressure = random.uniform(0.8, 1.2)
        self.production_count = 0
        self.fault_count = 0
        self.uptime = 0.0
        self.cycle_time = random.uniform(2, 8)
        self.quality_rate = random.uniform(0.95, 0.995)
        # 与判定规则无关的随机硬件故障标志
        self.fault_other = False
        # 规则生效起点：仅后续生效时，早于该时刻的样本不参与新规则判定
        self.rule_start_ts = 0.0

    def to_dict(self):
        return {
            "id": self.id, "type": self.type, "status": self.status,
            "position": self.position, "temperature": round(self.temperature, 2),
            "vibration": round(self.vibration, 3), "pressure": round(self.pressure, 2),
            "production_count": self.production_count, "fault_count": self.fault_count,
            "uptime": round(self.uptime, 2), "quality_rate": round(self.quality_rate, 3)
        }


devices = {i: DeviceState(i, random.choice(DEVICE_TYPES),
                          random.uniform(-5, 5), 0.5, random.uniform(-5, 5))
           for i in range(1, 13)}

production_log = []
anomaly_log = []

# 每台设备的历史样本（用于按新口径重算）：
# {ts, temperature, vibration, pressure, fault_other}
history: dict[int, deque] = {did: deque(maxlen=HISTORY_LIMIT) for did in devices}
# 每台设备当前处于激活态的规则名（实时判定用，上升沿才记异常）
active_rules: dict[int, set] = {did: set() for did in devices}

RULE_DEFS = [
    ("高温告警", "temperature", "temp_limit"),
    ("振动超标", "vibration", "vib_limit"),
    ("压力异常", "pressure", "pres_limit"),
]
TREND_RULE = "温度趋势上升"


def evaluate_window(window: deque, rule_cfg: dict) -> set:
    """对一台设备最近一个判定周期的样本做统一口径判定，返回当前激活的规则名集合。

    - 三个上限类规则：窗口内最近 consecutive 个样本全部超过上限才激活
      （窗口样本数不足 consecutive 时不激活，与模拟流程上线初期一致）
    - 温度趋势上升：窗口内前/后半段均值差 > 3°C 时激活
    """
    active = set()
    period = rule_cfg["period"]
    consec = rule_cfg["consecutive"]
    samples = list(window)[-period:]
    if len(samples) >= consec:
        tail = samples[-consec:]
        for name, field, cfg_key in RULE_DEFS:
            limit = rule_cfg[cfg_key]
            if all(s[field] > limit for s in tail):
                active.add(name)

    if len(samples) >= TREND_MIN_SAMPLES:
        vals = [s["temperature"] for s in samples]
        half = len(vals) // 2
        if np.mean(vals[half:]) - np.mean(vals[:half]) > TREND_DELTA:
            active.add(TREND_RULE)
    return active


def effective_window(dev: "DeviceState", cfg: dict) -> deque:
    """实时判定窗口：只取规则生效起点之后的样本，保证“仅后续数据生效”。"""
    if dev.rule_start_ts <= 0:
        return history[dev.id]
    return deque((s for s in history[dev.id] if s["ts"] >= dev.rule_start_ts),
                 maxlen=cfg["period"])


def _trigger_payload(dev: DeviceState, name: str, window: deque, rule_cfg: dict):
    last = window[-1]
    if name == "高温告警":
        return {"device_id": dev.id, "rule": name, "value": round(last["temperature"], 3),
                "threshold": rule_cfg["temp_limit"]}
    if name == "振动超标":
        return {"device_id": dev.id, "rule": name, "value": round(last["vibration"], 3),
                "threshold": rule_cfg["vib_limit"]}
    if name == "压力异常":
        return {"device_id": dev.id, "rule": name, "value": round(last["pressure"], 3),
                "threshold": rule_cfg["pres_limit"]}
    vals = [s["temperature"] for s in list(window)[-rule_cfg["period"]:]]
    half = len(vals) // 2
    return {"device_id": dev.id, "rule": name,
            "value": round(float(np.mean(vals[half:])), 2), "threshold": ">3°C/周期"}


class RulesEngine:
    def __init__(self):
        self.rules_by_type = load_rules()

    def update(self, rules_by_type: dict):
        self.rules_by_type = rules_by_type

    def config_for(self, dev: DeviceState) -> dict:
        return self.rules_by_type[dev.type]


init_db()
rules_engine = RulesEngine()


def recompute_all(rules_by_type: dict):
    """按同一份规则口径重放历史样本：重建异常记录与当前设备状态。

    实时模拟与重算共用 evaluate_window，保证“同一份口径”。
    返回新的 anomaly_log（按时间排序）。
    """
    new_anomalies = []
    for dev in devices.values():
        dev.rule_start_ts = 0.0  # 重算对全部已有数据生效
        cfg = rules_by_type[dev.type]
        replay = deque(maxlen=cfg["period"])
        prev_active = set()
        last_status = "RUNNING"
        for s in history[dev.id]:
            replay.append(s)
            cur_active = evaluate_window(replay, cfg)
            status = "FAULT" if (cur_active or s["fault_other"]) else "RUNNING"

            onset = cur_active - prev_active
            if onset:
                triggers = [_trigger_payload(dev, name, replay, cfg) for name in sorted(onset)]
                new_anomalies.append({"timestamp": s["ts"], "triggers": triggers,
                                      "device_type": dev.type})
            prev_active = cur_active
            last_status = status

        # 当前状态以重放末态为准
        dev.status = last_status
        active_rules[dev.id] = prev_active

    new_anomalies.sort(key=lambda a: a["timestamp"])
    return new_anomalies


def simulate():
    while SIMULATOR_RUNNING:
        with LOCK:
            for dev in devices.values():
                drift = 0.1 * math.sin(time.time() * 0.5 + dev.id)
                noise = random.gauss(0, 0.3)
                dev.temperature = max(25, min(65, dev.temperature + drift + noise))

                v_drift = 0.02 * math.sin(time.time() * 0.3 + dev.id * 0.7)
                dev.vibration = max(0, min(3, dev.vibration + v_drift + random.gauss(0, 0.05)))

                dev.pressure = max(0.5, min(2, dev.pressure + random.gauss(0, 0.02)))

                # 与规则无关的随机硬件故障（重算时可从样本复现）
                if not dev.fault_other and random.random() < 0.015:
                    dev.fault_other = True
                elif dev.fault_other and random.random() < 0.03:
                    dev.fault_other = False

                cfg = rules_engine.config_for(dev)
                window = history[dev.id]
                window.append({
                    "ts": time.time(),
                    "temperature": dev.temperature,
                    "vibration": dev.vibration,
                    "pressure": dev.pressure,
                    "fault_other": dev.fault_other,
                })

                # 实时判定只看规则生效起点之后的样本（仅后续生效时旧数据被排除）
                eff = effective_window(dev, cfg)
                cur_active = evaluate_window(eff, cfg)
                onset = cur_active - active_rules[dev.id]

                # 状态变化与异常记录使用同一份判定结果
                prev_status = dev.status
                dev.status = "FAULT" if (cur_active or dev.fault_other) else "RUNNING"
                if prev_status != "FAULT" and dev.status == "FAULT":
                    dev.fault_count += 1
                if onset:
                    triggers = [_trigger_payload(dev, name, eff, cfg) for name in sorted(onset)]
                    anomaly_log.append({"timestamp": window[-1]["ts"],
                                        "triggers": triggers, "device_type": dev.type})
                active_rules[dev.id] = cur_active

                if dev.status == "RUNNING":
                    if random.random() < 0.4:
                        dev.production_count += 1
                    dev.uptime += 1

            production_log.append({"timestamp": time.time(),
                                   "count": sum(d.production_count for d in devices.values())})

            try:
                payload = {
                    "devices": [d.to_dict() for d in devices.values()],
                    "production": sum(d.production_count for d in devices.values()),
                    "anomalies": anomaly_log[-5:] if anomaly_log else [],
                    "oee": calculate_oee()
                }
                broadcast(json.dumps(payload))
            except Exception:
                continue

        time.sleep(1)


def calculate_oee():
    oee_list = []
    for dev in devices.values():
        if dev.uptime == 0:
            continue
        availability = min(1.0, dev.uptime / max(1, dev.uptime + dev.fault_count))
        performance = min(1.0, dev.production_count / max(1, dev.uptime / 2))
        quality = dev.quality_rate
        oee = round(availability * performance * quality * 100, 1)
        oee_list.append({"id": dev.id, "type": dev.type, "oee": oee,
                         "availability": round(availability * 100, 1),
                         "performance": round(performance * 100, 1),
                         "quality": round(quality * 100, 1)})
    return oee_list


# ---------------------------------------------------------------------------
# API 模型
# ---------------------------------------------------------------------------
class RulePayload(BaseModel):
    temp_limit: Optional[float] = None
    vib_limit: Optional[float] = None
    pres_limit: Optional[float] = None
    period: Optional[int] = None
    consecutive: Optional[int] = None


class ApplyModePayload(BaseModel):
    mode: str = Field(..., description="future=仅后续数据，recompute=同时重算已有数据")


@app.on_event("startup")
async def startup():
    global EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()
    init_db()
    rules_engine.update(load_rules())
    t = threading.Thread(target=simulate, daemon=True)
    t.start()

@app.get("/api/device-types")
def get_device_types():
    return {"types": [{"value": t, "label": DEVICE_TYPE_LABELS[t]} for t in DEVICE_TYPES]}


@app.get("/api/rules")
def get_rules():
    with LOCK:
        return {"rules": rules_engine.rules_by_type,
                "apply_mode": load_apply_mode(),
                "bounds": BOUNDS}


@app.put("/api/rules/{device_type}")
def update_rule(device_type: str, payload: RulePayload):
    if device_type not in DEFAULT_RULES:
        raise HTTPException(404, detail=f"未知设备类型: {device_type}")

    with LOCK:
        # 五项必须整体提交；缺字段按空值处理（不允许保存）
        raw = {
            "temp_limit": payload.temp_limit,
            "vib_limit": payload.vib_limit,
            "pres_limit": payload.pres_limit,
            "period": payload.period,
            "consecutive": payload.consecutive,
        }
        clean, errors = validate_rule(raw)
        if errors:
            # 不合格：拒绝保存，并逐项指出问题
            raise HTTPException(422, detail={"message": "存在不合格项，规则未保存", "errors": errors})

        save_rule(device_type, clean)
        new_rules = dict(rules_engine.rules_by_type)
        new_rules[device_type] = {**new_rules[device_type], **clean}
        rules_engine.update(new_rules)

        mode = load_apply_mode()
        recomputed = False
        now = time.time()
        if mode == "recompute":
            anomaly_log[:] = recompute_all(new_rules)
            recomputed = True
        else:
            # 仅对后续数据生效：判定窗口从生效时刻重新累积，旧样本不参与新规则
            for dev in devices.values():
                if dev.type == device_type:
                    dev.rule_start_ts = now
                    active_rules[dev.id] = set()

        return {"ok": True, "rule": rules_engine.rules_by_type[device_type],
                "apply_mode": mode, "recomputed": recomputed}


@app.put("/api/settings/apply-mode")
def update_apply_mode(payload: ApplyModePayload):
    """记录“仅后续生效 / 同时重算”的选择，重启后沿用，与模拟流程一致。"""
    if payload.mode not in ("future", "recompute"):
        raise HTTPException(422, detail={"message": "生效方式只能为 future 或 recompute"})
    with LOCK:
        save_apply_mode(payload.mode)
        recomputed = False
        if payload.mode == "recompute":
            anomaly_log[:] = recompute_all(rules_engine.rules_by_type)
            recomputed = True
    return {"ok": True, "apply_mode": payload.mode, "recomputed": recomputed}


@app.get("/api/devices")
def get_devices():
    with LOCK:
        return {"devices": [d.to_dict() for d in devices.values()], "anomalies": anomaly_log[-10:]}


@app.get("/api/oee")
def get_oee():
    with LOCK:
        return {"oee": calculate_oee()}


@app.get("/api/production")
def get_production():
    with LOCK:
        return {"log": production_log[-60:]}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ACTIVE_CLIENTS.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in ACTIVE_CLIENTS:
            ACTIVE_CLIENTS.remove(websocket)


@app.on_event("shutdown")
async def shutdown():
    global SIMULATOR_RUNNING
    SIMULATOR_RUNNING = False
