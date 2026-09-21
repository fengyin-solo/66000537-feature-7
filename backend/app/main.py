import asyncio, math, random, time, json, threading, os
from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import numpy as np

app = FastAPI(title="Digital Twin Factory Monitor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEVICE_TYPES = ["CNC", "RobotArm", "Conveyor", "AGV", "InjectionMolding", "QCStation"]
STATUSES = ["RUNNING", "IDLE", "FAULT", "OFFLINE"]
ACTIVE_CLIENTS: list[WebSocket] = []
SIMULATOR_RUNNING = True

# ---------------------------------------------------------------------------
# 异常判定规则配置（按设备类型分别调整，持久化到磁盘，重启后沿用）
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "anomaly_rules.json")

# 各传感器物理量纲的允许区间（与模拟器裁剪范围一致）：
#   min 以下视为“上限填反”，max 以上视为“越界”，缺省视为“为空”
METRICS = {
    "temperature": {"label": "高温判定上限", "unit": "°C", "min": 25.0, "max": 65.0, "default": 48.0},
    "vibration":   {"label": "振动超标上限", "unit": "mm/s", "min": 0.0,  "max": 3.0,  "default": 2.0},
    "pressure":    {"label": "压力异常上限", "unit": "MPa", "min": 0.5,  "max": 2.0,  "default": 1.5},
}
PERIOD_MIN, PERIOD_MAX = 1, 60          # 判定周期（秒）允许区间
CONSECUTIVE_MIN, CONSECUTIVE_MAX = 1, 60  # 连续超限次数允许区间
MODES = {"future": "只对后续数据生效", "recompute": "同时重算已有数据"}
MAX_LOG = 2000                          # 异常记录保留条数
HISTORY_SECONDS = 600                   # 重算时回溯的原始采样时长（秒）


def default_device_rules():
    return {
        "temperature": METRICS["temperature"]["default"],
        "vibration": METRICS["vibration"]["default"],
        "pressure": METRICS["pressure"]["default"],
        "period": 10,
        "consecutive": 3,
    }


def default_config():
    return {
        "mode": "future",
        "devices": {t: default_device_rules() for t in DEVICE_TYPES},
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg = _deep_merge(default_config(), saved)
        # mode 非法时回退默认，避免脏数据影响生效方式
        if cfg.get("mode") not in MODES:
            cfg["mode"] = "future"
        return cfg
    except Exception:
        cfg = default_config()
        save_config(cfg)
        return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _is_number(v):
    # bool 是 int 的子类，需要显式排除
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_rules(payload):
    """返回 (errors, normalized_config)。errors 为空时配置允许保存。"""
    errors = []
    norm = default_config()

    mode = payload.get("mode") if isinstance(payload, dict) else None
    if mode in MODES:
        norm["mode"] = mode
    else:
        errors.append({"field": "mode", "message": "生效方式必须为“只对后续数据生效”或“同时重算已有数据”"})

    devices_in = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices_in, dict):
        for t in DEVICE_TYPES:
            errors.append({"field": f"devices.{t}", "message": "缺少该设备类型的规则配置"})
        return errors, norm
    norm["devices"] = {}

    for dtype in DEVICE_TYPES:
        d_in = devices_in.get(dtype)
        d_out = default_device_rules()
        if not isinstance(d_in, dict):
            errors.append({"field": f"devices.{dtype}", "message": "缺少该设备类型的规则配置"})
            norm["devices"][dtype] = d_out
            continue

        # 三个判定上限：为空 / 非数值 / 填反（低于物理下限）/ 越界（高于物理上限）
        for key, meta in METRICS.items():
            raw = d_in.get(key)
            p = f"devices.{dtype}.{key}"
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                errors.append({"field": p, "message": f"{dtype} 的{meta['label']}不能为空"})
            elif not _is_number(raw):
                errors.append({"field": p, "message": f"{dtype} 的{meta['label']}必须是数字"})
            elif float(raw) <= meta["min"]:
                errors.append({"field": p,
                               "message": f"{dtype} 的{meta['label']}填反或过低，需大于 {meta['min']}{meta['unit']}"})
            elif float(raw) > meta["max"]:
                errors.append({"field": p,
                               "message": f"{dtype} 的{meta['label']}越界，不能超过 {meta['max']}{meta['unit']}"})
            else:
                d_out[key] = round(float(raw), 3)

        # 判定周期、连续超限次数：空 / 整数 / 区间
        for key, label, lo, hi in (
            ("period", "判定周期", PERIOD_MIN, PERIOD_MAX),
            ("consecutive", "连续超限次数", CONSECUTIVE_MIN, CONSECUTIVE_MAX),
        ):
            raw = d_in.get(key)
            p = f"devices.{dtype}.{key}"
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                errors.append({"field": p, "message": f"{dtype} 的{label}不能为空"})
            elif not _is_number(raw) or float(raw) != int(float(raw)):
                errors.append({"field": p, "message": f"{dtype} 的{label}必须是整数"})
            elif not (lo <= int(raw) <= hi):
                errors.append({"field": p, "message": f"{dtype} 的{label}需在 {lo}~{hi} 之间"})
            else:
                d_out[key] = int(raw)

        norm["devices"][dtype] = d_out

    # 连续超限次数不能超过判定周期内的采样数（填反）；仅在各项本身都合法时检查
    for dtype in DEVICE_TYPES:
        prefix = f"devices.{dtype}."
        d_out = norm["devices"][dtype]
        if (not any(e["field"].startswith(prefix) for e in errors)
                and d_out["consecutive"] > d_out["period"]):
            errors.append({"field": f"devices.{dtype}.consecutive",
                           "message": f"{dtype} 的连续超限次数({d_out['consecutive']})不能大于判定周期({d_out['period']})，两项可能填反"})

    return errors, norm


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
        self.rule_fault = False       # 当前 FAULT 是否由判定规则触发

    def to_dict(self):
        return {
            "id": self.id, "type": self.type, "status": self.status,
            "position": self.position, "temperature": round(self.temperature, 2),
            "vibration": round(self.vibration, 3), "pressure": round(self.pressure, 2),
            "production_count": self.production_count, "fault_count": self.fault_count,
            "uptime": round(self.uptime, 2), "quality_rate": round(self.quality_rate, 3)
        }


devices = {i: DeviceState(i, random.choice(DEVICE_TYPES),
                          random.uniform(-5, 5), 0.5, random.uniform(-5, 5)) for i in range(1, 13)}

production_log = []


class RuleEngine:
    """统一口径的异常判定引擎：实时 ingest 与重算 rebuild 走同一份判定逻辑。"""

    FIELD_RULE_NAMES = {"temperature": "高温告警", "vibration": "振动超标", "pressure": "压力异常"}

    def __init__(self, cfg: dict):
        self.lock = threading.RLock()
        self.cfg = cfg
        self.log: list = []
        # 每台设备保存原始采样（含时间戳），用于“同时重算已有数据”
        self.history: dict[int, deque] = {d.id: deque(maxlen=HISTORY_SECONDS) for d in devices.values()}
        self._reset_states()

    def _reset_states(self):
        # 判定过程状态（实时与重算共用同一份）
        self.cur_bucket: dict[int, int] = {did: -1 for did in devices}
        self.streaks: dict[int, dict[str, int]] = {did: {k: 0 for k in METRICS} for did in devices}
        self.fired: dict[int, dict[str, float]] = {did: {} for did in devices}  # field -> 触发点峰值
        self.trend_win: dict[int, deque] = {did: deque(maxlen=10) for did in devices}
        self.trend_fired: dict[int, set] = {did: set() for did in devices}
        # 已经出过记录的周期，避免重算后实时流程对“当前桶”重复发记录
        self.emitted: set = set()

    def _entry(self, ts, dev, triggers):
        return {"timestamp": ts, "triggers": triggers, "device_type": dev.type}

    def _eval_trend(self, dev, bucket, bucket_end, entries_out):
        """温度趋势上升：沿用 8 点滑窗、后 4 点均值 - 前 4 点均值 > 3°C 的口径。"""
        win = self.trend_win[dev.id]
        if len(win) >= 8 and bucket not in self.trend_fired[dev.id]:
            vals = list(win)
            if np.mean(vals[-4:]) - np.mean(vals[:4]) > 3:
                self.trend_fired[dev.id].add(bucket)
                entries_out.append(self._entry(
                    bucket_end - 0.001, dev,
                    [{"device_id": dev.id, "rule": "温度趋势上升",
                      "value": round(float(np.mean(vals[-4:])), 2), "threshold": ">3°C/周期"}]))

    def _close_bucket(self, dev, bucket, entries_out):
        """评估一个已封口的判定周期并产出异常记录，返回该周期是否触发规则。"""
        period = self.cfg["devices"][dev.type]["period"]
        bucket_end = (bucket + 1) * period
        had_fire = bool(self.fired[dev.id])
        if (dev.id, bucket) not in self.emitted:
            triggers = [
                {"device_id": dev.id, "rule": self.FIELD_RULE_NAMES[key],
                 "value": round(peak, 3), "threshold": self.cfg["devices"][dev.type][key]}
                for key, peak in self.fired[dev.id].items()
            ]
            if triggers:
                entries_out.append(self._entry(bucket_end - 0.001, dev, triggers))
            self._eval_trend(dev, bucket, bucket_end, entries_out)
            self.emitted.add((dev.id, bucket))
        return had_fire

    def _start_bucket(self, dev, bucket):
        self.streaks[dev.id] = {k: 0 for k in METRICS}
        self.fired[dev.id] = {}
        # 清理已封口周期的去重标记，防止集合无限增长
        self.emitted = {(d, b) for (d, b) in self.emitted if d != dev.id or b >= bucket - 2}

    def ingest(self, dev: DeviceState, now: float = None):
        """处理一个实时采样点（每秒一次）。"""
        with self.lock:
            now = now if now is not None else time.time()
            rules = self.cfg["devices"][dev.type]
            period = rules["period"]
            bucket = int(now // period)
            self.history[dev.id].append({"temperature": dev.temperature, "vibration": dev.vibration,
                                         "pressure": dev.pressure, "ts": now})

            if self.cur_bucket[dev.id] != bucket:
                # 上一周期封口：先出记录，再按结果恢复规则故障
                if self.cur_bucket[dev.id] >= 0:
                    entries = []
                    had_fire = self._close_bucket(dev, self.cur_bucket[dev.id], entries)
                    if entries:
                        self.log.extend(entries)
                        if len(self.log) > MAX_LOG:
                            del self.log[:len(self.log) - MAX_LOG]
                    if not had_fire and dev.rule_fault:
                        dev.status = "RUNNING"
                        dev.rule_fault = False
                self.cur_bucket[dev.id] = bucket
                self._start_bucket(dev, bucket)

            # 连续超限判定
            for key in METRICS:
                val = getattr(dev, key)
                if val > rules[key]:
                    self.streaks[dev.id][key] += 1
                    if self.streaks[dev.id][key] >= rules["consecutive"]:
                        self.fired[dev.id][key] = max(self.fired[dev.id].get(key, float("-inf")), val)
                else:
                    self.streaks[dev.id][key] = 0

            self.trend_win[dev.id].append(dev.temperature)

            # 周期内一旦规则触发，设备立即进入故障态（与重算同一口径）；
            # 即使此前是随机故障，也改由规则口径接管恢复时机
            if self.fired[dev.id]:
                dev.status = "FAULT"
                dev.rule_fault = True

    def rebuild(self):
        """按当前配置重算已有采样：设备状态与异常记录全部用同一口径重新生成。"""
        with self.lock:
            self._reset_states()
            self.log = []
            entries = []
            for dev in devices.values():
                rules = self.cfg["devices"][dev.type]
                period = rules["period"]
                samples = list(self.history[dev.id])
                if not samples:
                    dev.rule_fault = False
                    if dev.status == "FAULT":
                        dev.status = "RUNNING"
                    continue
                # 逐点回放，复用与实时完全一致的计数/触发逻辑
                last_bucket = int(samples[-1]["ts"] // period)
                for s in samples:
                    b = int(s["ts"] // period)
                    if self.cur_bucket[dev.id] != b:
                        if self.cur_bucket[dev.id] >= 0:
                            self._close_bucket(dev, self.cur_bucket[dev.id], entries)
                        self.cur_bucket[dev.id] = b
                        self._start_bucket(dev, b)
                    for key in METRICS:
                        val = s[key]
                        if val > rules[key]:
                            self.streaks[dev.id][key] += 1
                            if self.streaks[dev.id][key] >= rules["consecutive"]:
                                self.fired[dev.id][key] = max(self.fired[dev.id].get(key, float("-inf")), val)
                        else:
                            self.streaks[dev.id][key] = 0
                    self.trend_win[dev.id].append(s["temperature"])
                # 封口最后一个周期，并用最近周期的结果确定设备当前状态
                self._close_bucket(dev, last_bucket, entries)
                if self.fired[dev.id]:
                    dev.status = "FAULT"
                    dev.rule_fault = True
                else:
                    dev.rule_fault = False
                    if dev.status == "FAULT":
                        dev.status = "RUNNING"

            entries.sort(key=lambda e: e["timestamp"])
            self.log = entries[-MAX_LOG:]

    def clear_future_state(self):
        """只对后续数据生效：清空判定过程状态，后续采样按新规则重新累计。"""
        with self.lock:
            self._reset_states()


rules_engine = RuleEngine(load_config())


def apply_config(new_cfg: dict):
    """保存新配置并按所选方式生效，返回是否触发了重算。"""
    with rules_engine.lock:
        rules_engine.cfg = new_cfg
        recompute = new_cfg["mode"] == "recompute"
        save_config(new_cfg)
        if recompute:
            rules_engine.rebuild()
        else:
            rules_engine.clear_future_state()
        return recompute


def simulate():
    while SIMULATOR_RUNNING:
        for dev in devices.values():
            drift = 0.1 * math.sin(time.time() * 0.5 + dev.id)
            noise = random.gauss(0, 0.3)
            dev.temperature = max(25, min(65, dev.temperature + drift + noise))

            v_drift = 0.02 * math.sin(time.time() * 0.3 + dev.id * 0.7)
            dev.vibration = max(0, min(3, dev.vibration + v_drift + random.gauss(0, 0.05)))

            dev.pressure = max(0.5, min(2, dev.pressure + random.gauss(0, 0.02)))

            # 与规则无关的偶发设备故障；规则故障由规则引擎确定性地置位/恢复
            if dev.status != "FAULT" and random.random() < 0.015:
                dev.status = "FAULT"
                dev.fault_count += 1
                dev.rule_fault = False

            rules_engine.ingest(dev)

            if dev.status == "FAULT" and not dev.rule_fault and random.random() < 0.03:
                dev.status = "RUNNING"

            if dev.status == "RUNNING":
                if random.random() < 0.4:
                    dev.production_count += 1
                dev.uptime += 1

        production_log.append({"timestamp": time.time(), "count": sum(d.production_count for d in devices.values())})

        try:
            payload = {
                "devices": [d.to_dict() for d in devices.values()],
                "production": sum(d.production_count for d in devices.values()),
                "anomalies": rules_engine.log[-5:] if rules_engine.log else [],
                "oee": calculate_oee()
            }
            msg = json.dumps(payload)
        except Exception:
            continue

        dead = []
        for ws in ACTIVE_CLIENTS:
            try:
                loop = getattr(ws, "app_loop", None)
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), loop)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in ACTIVE_CLIENTS:
                ACTIVE_CLIENTS.remove(ws)

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


@app.on_event("startup")
async def startup():
    # 上次选择的生效方式已随配置加载（recompute 在已有采样为空时等价于后续生效，
    # 模拟器启动后即按新配置与所选模式运行），保持与模拟流程一致。
    t = threading.Thread(target=simulate, daemon=True)
    t.start()


@app.get("/api/devices")
def get_devices():
    return {"devices": [d.to_dict() for d in devices.values()], "anomalies": rules_engine.log[-10:]}


@app.get("/api/oee")
def get_oee():
    return {"oee": calculate_oee()}


@app.get("/api/production")
def get_production():
    return {"log": production_log[-60:]}


@app.get("/api/rules")
def get_rules():
    with rules_engine.lock:
        return {
            "config": rules_engine.cfg,
            "device_types": DEVICE_TYPES,
            "modes": MODES,
            "metrics": {k: {kk: vv for kk, vv in meta.items()} for k, meta in METRICS.items()},
            "period_range": [PERIOD_MIN, PERIOD_MAX],
            "consecutive_range": [CONSECUTIVE_MIN, CONSECUTIVE_MAX],
        }


@app.put("/api/rules")
async def put_rules(payload: dict):
    errors, norm = validate_rules(payload if isinstance(payload, dict) else {})
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    recomputed = apply_config(norm)
    return {"ok": True, "config": norm, "recomputed": recomputed,
            "message": ("规则已保存，设备状态与异常记录已按同一口径重算" if recomputed
                        else "规则已保存，仅对后续数据生效")}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket.app_loop = asyncio.get_event_loop()
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
