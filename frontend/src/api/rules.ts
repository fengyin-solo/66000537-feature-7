import axios from 'axios'
import type {
  RulesResponse, AnomalyRuleConfig, RuleForm, RuleErrors, DeviceTypeInfo,
} from '@/types'

const http = axios.create({ baseURL: '/api' })

export async function fetchRules(): Promise<RulesResponse> {
  const { data } = await http.get<RulesResponse>('/rules')
  return data
}

export async function fetchDeviceTypes(): Promise<DeviceTypeInfo[]> {
  const { data } = await http.get<{ types: DeviceTypeInfo[] }>('/device-types')
  return data.types
}

export interface SaveResult {
  ok: boolean
  rule: AnomalyRuleConfig
  apply_mode: 'future' | 'recompute'
  recomputed: boolean
}

export async function saveRule(deviceType: string, form: RuleForm): Promise<SaveResult> {
  const { data } = await http.put<SaveResult>(`/rules/${deviceType}`, {
    temp_limit: form.temp_limit === '' ? null : Number(form.temp_limit),
    vib_limit: form.vib_limit === '' ? null : Number(form.vib_limit),
    pres_limit: form.pres_limit === '' ? null : Number(form.pres_limit),
    period: form.period === '' ? null : Number(form.period),
    consecutive: form.consecutive === '' ? null : Number(form.consecutive),
  })
  return data
}

export async function setApplyMode(mode: 'future' | 'recompute') {
  const { data } = await http.put<{ ok: boolean; apply_mode: 'future' | 'recompute'; recomputed: boolean }>(
    '/settings/apply-mode', { mode },
  )
  return data
}

/** 从 axios 错误中提取后端逐项校验信息；无法解析时返回通用错误。 */
export function extractRuleErrors(err: unknown): { message: string; errors: RuleErrors } {
  if (axios.isAxiosError(err) && err.response?.status === 422) {
    const detail = err.response.data?.detail
    if (detail && typeof detail === 'object' && detail.errors) {
      return { message: detail.message || '存在不合格项，规则未保存', errors: detail.errors }
    }
  }
  return { message: '保存失败，请检查服务连接', errors: {} }
}
