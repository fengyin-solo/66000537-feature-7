<template>
  <el-dialog v-model="visible" title="⚙️ 产线异常判定规则设置" width="760px" top="6vh" @closed="onClosed">
    <div v-loading="loading">
      <el-alert type="info" :closable="false" class="tip">
        按设备类型分别调整高温 / 振动超标 / 压力异常的判定上限，以及判定周期与连续超限次数。
        保存后设备状态变化与异常记录按同一份口径计算。
      </el-alert>

      <el-alert v-if="errorList.length" type="error" :closable="false" class="tip" title="以下项不合格，无法保存：">
        <div v-for="(e, i) in errorList" :key="i" class="err-line">· {{ e.message }}</div>
      </el-alert>

      <el-tabs v-model="activeType" v-if="meta">
        <el-tab-pane v-for="t in meta.device_types" :key="t" :name="t">
          <template #label>
            <span class="tab-label">
              <i v-if="typeErrors(t).length" class="dot"></i>{{ deviceLabel(t) }}
            </span>
          </template>

          <el-form label-width="150px" class="rule-form">
            <el-form-item v-for="key in metricKeys" :key="key"
                          :label="meta.metrics[key].label"
                          :error="fieldError(`devices.${t}.${key}`)">
              <el-input-number v-model="form.devices[t][key]" :precision="key === 'temperature' ? 1 : 2"
                               :step="key === 'temperature' ? 0.5 : 0.1" :min="-9999" :max="9999"
                               controls-position="right" size="small" />
              <span class="unit">{{ meta.metrics[key].unit }}</span>
              <span class="hint">允许区间 ({{ meta.metrics[key].min }}, {{ meta.metrics[key].max }}]，
                低于下限视为填反，高于上限视为越界</span>
            </el-form-item>

            <el-form-item label="判定周期" :error="fieldError(`devices.${t}.period`)">
              <el-input-number v-model="form.devices[t].period" :step="1" :min="-9999" :max="9999"
                               controls-position="right" size="small" />
              <span class="unit">秒</span>
              <span class="hint">每 {{ meta.period_range[0] }}~{{ meta.period_range[1] }} 秒为一个判定周期</span>
            </el-form-item>

            <el-form-item label="连续超限次数" :error="fieldError(`devices.${t}.consecutive`)">
              <el-input-number v-model="form.devices[t].consecutive" :step="1" :min="-9999" :max="9999"
                               controls-position="right" size="small" />
              <span class="unit">次</span>
              <span class="hint">周期内连续超限达到该次数即告警，不能大于判定周期</span>
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <el-divider content-position="left">生效方式（重启后沿用上次选择）</el-divider>
      <el-radio-group v-model="form.mode" class="mode-group">
        <el-radio value="future">只对后续数据生效（已有异常记录与设备状态保持不变）</el-radio>
        <el-radio value="recompute">同时重算已有数据（设备状态变化与异常记录按新口径重算）</el-radio>
      </el-radio-group>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'
import { fetchRules, saveRules } from '@/api/rules'
import type { RulesMeta, RuleConfig, RuleMode, RuleError } from '@/types'
import { DEVICE_TYPE_LABELS } from '@/types'

const visible = ref(false)
const loading = ref(false)
const saving = ref(false)
const meta = ref<RulesMeta | null>(null)
const form = ref<RuleConfig>({ mode: 'future', devices: {} })
const activeType = ref('')
const errors = ref<RuleError[]>([])

const metricKeys = ['temperature', 'vibration', 'pressure'] as const

const errorList = computed(() => errors.value)

function deviceLabel(t: string) {
  return DEVICE_TYPE_LABELS[t] ?? t
}

function fieldError(field: string): string {
  return errors.value.find(e => e.field === field)?.message ?? ''
}

function typeErrors(t: string): RuleError[] {
  return errors.value.filter(e => e.field.startsWith(`devices.${t}.`))
}

function isFiniteNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

// 前端先按与后端一致的口径校验，便于就地提示；后端仍为最终裁决
function localValidate(): RuleError[] {
  const errs: RuleError[] = []
  if (!meta.value) return errs
  const m = meta.value

  if (form.value.mode !== 'future' && form.value.mode !== 'recompute') {
    errs.push({ field: 'mode', message: '请选择生效方式' })
  }

  for (const t of m.device_types) {
    const d = form.value.devices[t]
    if (!d) continue

    for (const key of metricKeys) {
      const mm = m.metrics[key]
      const v = d[key]
      if (!isFiniteNum(v)) {
        errs.push({ field: `devices.${t}.${key}`, message: `${deviceLabel(t)} 的${mm.label}不能为空且必须是数字` })
      } else if (v <= mm.min) {
        errs.push({ field: `devices.${t}.${key}`, message: `${deviceLabel(t)} 的${mm.label}填反或过低，需大于 ${mm.min}${mm.unit}` })
      } else if (v > mm.max) {
        errs.push({ field: `devices.${t}.${key}`, message: `${deviceLabel(t)} 的${mm.label}越界，不能超过 ${mm.max}${mm.unit}` })
      }
    }

    const intField = (key: 'period' | 'consecutive', label: string, range: [number, number]) => {
      const v = d[key]
      if (!isFiniteNum(v) || !Number.isInteger(v)) {
        errs.push({ field: `devices.${t}.${key}`, message: `${deviceLabel(t)} 的${label}不能为空且必须是整数` })
      } else if (v < range[0] || v > range[1]) {
        errs.push({ field: `devices.${t}.${key}`, message: `${deviceLabel(t)} 的${label}需在 ${range[0]}~${range[1]} 之间` })
      }
    }
    intField('period', '判定周期', m.period_range)
    intField('consecutive', '连续超限次数', m.consecutive_range)

    if (isFiniteNum(d.period) && isFiniteNum(d.consecutive)
        && Number.isInteger(d.period) && Number.isInteger(d.consecutive)
        && d.period >= m.period_range[0] && d.period <= m.period_range[1]
        && d.consecutive >= m.consecutive_range[0] && d.consecutive <= m.consecutive_range[1]
        && d.consecutive > d.period) {
      errs.push({ field: `devices.${t}.consecutive`,
        message: `${deviceLabel(t)} 的连续超限次数(${d.consecutive})不能大于判定周期(${d.period})，两项可能填反` })
    }
  }
  return errs
}

async function open() {
  visible.value = true
  loading.value = true
  errors.value = []
  try {
    const data = await fetchRules()
    meta.value = data
    form.value = JSON.parse(JSON.stringify(data.config)) as RuleConfig
    activeType.value = data.device_types[0] ?? ''
  } catch {
    ElMessage.error('规则配置加载失败，请确认后端已启动')
    visible.value = false
  } finally {
    loading.value = false
  }
}

async function onSave() {
  // 切到第一个有错误的设备类型页签，方便定位
  const local = localValidate()
  errors.value = local
  if (local.length) {
    const firstType = local[0].field.split('.')[1]
    if (firstType && firstType !== 'mode') activeType.value = firstType
    return
  }

  saving.value = true
  try {
    const res = await saveRules(form.value)
    if (!res.ok) {
      errors.value = res.errors ?? []
      const firstType = errors.value[0]?.field.split('.')[1]
      if (firstType && firstType !== 'mode') activeType.value = firstType
      return
    }
    ElMessage.success(res.message || '规则已保存')
    visible.value = false
    const mode = (form.value.mode as RuleMode)
    if (mode === 'recompute') {
      // 下一帧 WS 推送即携带重算后的状态与记录，无需额外刷新
      ElMessage.info('设备状态与异常记录已按同一口径重算')
    }
  } catch {
    ElMessage.error('保存失败，请稍后重试')
  } finally {
    saving.value = false
  }
}

function onClosed() {
  errors.value = []
}

defineExpose({ open })
</script>

<style scoped>
.tip { margin-bottom: 12px }
.err-line { font-size: 12px; line-height: 1.7 }
.rule-form { max-width: 620px }
.unit { margin-left: 8px; color: #94a3b8; font-size: 12px }
.hint { display: block; margin-left: 130px; color: #64748b; font-size: 11px; line-height: 1.4; margin-top: 2px }
.tab-label { display: inline-flex; align-items: center; gap: 5px }
.dot { width: 7px; height: 7px; border-radius: 50%; background: #f56c6c; display: inline-block }
.mode-group { display: flex; flex-direction: column; gap: 8px }
:deep(.el-radio) { margin-right: 0; height: auto; white-space: normal }
</style>
