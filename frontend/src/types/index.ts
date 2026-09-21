export interface Device {
  id: number; type: string; status: string; position: number[]
  temperature: number; vibration: number; pressure: number
  production_count: number; fault_count: number
  uptime: number; quality_rate: number
}

export interface Anomaly {
  timestamp: number; triggers: { device_id: number; rule: string; value: number; threshold: number | string }[]
  device_type: string
}

// ---- 异常判定规则（按设备类型分别配置） ----
export interface DeviceRule {
  temperature: number
  vibration: number
  pressure: number
  period: number        // 判定周期（秒）
  consecutive: number   // 连续超限次数
}

export type RuleMode = 'future' | 'recompute'

export interface RuleConfig {
  mode: RuleMode
  devices: Record<string, DeviceRule>
}

export interface MetricMeta {
  label: string; unit: string; min: number; max: number; default: number
}

export interface RulesMeta {
  config: RuleConfig
  device_types: string[]
  modes: Record<RuleMode, string>
  metrics: Record<'temperature' | 'vibration' | 'pressure', MetricMeta>
  period_range: [number, number]
  consecutive_range: [number, number]
}

export interface RuleError {
  field: string
  message: string
}

export const DEVICE_TYPE_LABELS: Record<string, string> = {
  CNC: 'CNC 数控机床',
  RobotArm: '机械臂',
  Conveyor: '传送带',
  AGV: 'AGV 小车',
  InjectionMolding: '注塑机',
  QCStation: '质检站',
}

export interface OEEItem {
  id: number; type: string; oee: number
  availability: number; performance: number; quality: number
}

export interface FactoryData {
  devices: Device[]
  production: number
  anomalies: Anomaly[]
  oee: OEEItem[]
}

export const DEVICE_COLORS: Record<string, string> = {
  CNC: '#e74c3c', RobotArm: '#3498db', Conveyor: '#f39c12',
  AGV: '#2ecc71', InjectionMolding: '#9b59b6', QCStation: '#1abc9c'
}

export const STATUS_COLORS: Record<string, string> = {
  RUNNING: '#2ecc71', IDLE: '#f1c40f', FAULT: '#e74c3c', OFFLINE: '#95a5a6'
}