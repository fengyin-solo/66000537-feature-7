export interface Device {
  id: number; type: string; status: string; position: number[]
  temperature: number; vibration: number; pressure: number
  production_count: number; fault_count: number
  uptime: number; quality_rate: number
}

export interface DeviceTypeInfo {
  value: string
  label: string
}

export type RuleField = 'temp_limit' | 'vib_limit' | 'pres_limit' | 'period' | 'consecutive'

export interface AnomalyRuleConfig {
  device_type: string
  temp_limit: number
  vib_limit: number
  pres_limit: number
  period: number
  consecutive: number
  updated_at?: number
}

export interface RuleFieldBound {
  label: string
  min: number
  max: number
}

export type RuleBounds = Record<RuleField, RuleFieldBound>

export interface RulesResponse {
  rules: Record<string, AnomalyRuleConfig>
  apply_mode: 'future' | 'recompute'
  bounds: RuleBounds
}

export type RuleForm = Record<RuleField, string>
export type RuleErrors = Partial<Record<RuleField, string>>

export interface Anomaly {
  timestamp: number; triggers: { device_id: number; rule: string; value: number; threshold: string }[]
  device_type: string
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