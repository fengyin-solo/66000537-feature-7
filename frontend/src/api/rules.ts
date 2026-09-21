import axios from 'axios'
import type { RuleConfig, RulesMeta, RuleError } from '@/types'

const http = axios.create({ baseURL: '/api' })

export async function fetchRules(): Promise<RulesMeta> {
  const { data } = await http.get<RulesMeta>('/rules')
  return data
}

export interface SaveResult {
  ok: boolean
  config?: RuleConfig
  recomputed?: boolean
  message?: string
  errors?: RuleError[]
}

export async function saveRules(config: RuleConfig): Promise<SaveResult> {
  try {
    const { data } = await http.put<SaveResult>('/rules', config)
    return data
  } catch (e: any) {
    if (e?.response?.status === 422) {
      return { ok: false, errors: e.response.data?.detail?.errors ?? [] }
    }
    throw e
  }
}
