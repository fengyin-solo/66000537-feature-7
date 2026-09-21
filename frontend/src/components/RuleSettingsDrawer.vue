<template>
  <el-drawer
    :model-value="modelValue"
    title="⚙️ 产线异常判定规则"
    direction="rtl"
    size="460px"
    @update:model-value="(v: boolean) => emit('update:modelValue', v)"
  >
    <div v-loading="loading" class="rules-panel">
      <el-alert type="info" :closable="false" show-icon class="tip">
        <template #title>
          按设备类型调整高温、振动、压力的判定上限，以及判定周期与连续超限次数；
          设备状态与异常记录按同一口径计算。
        </template>
      </el-alert>

      <el-form label-position="top" :model="form" class="rule-form" @submit.prevent>
        <el-form-item label="设备类型">
          <el-select v-model="currentType" class="full" @change="onTypeChange">
            <el-option
              v-for="t in deviceTypes"
              :key="t.value"
              :label="`${t.label}（${t.value}）`"
              :value="t.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item
          v-for="f in FIELDS"
          :key="f.key"
          :label="bounds[f.key].label"
          :error="errors[f.key] || undefined"
        >
          <el-input
            v-model="form[f.key]"
            :placeholder="`范围 ${fmtBound(f.key)}`"
            clearable
            :class="{ invalid: !!errors[f.key] }"
            @input="clearError(f.key)"
          >
            <template #append>{{ unit(f.key) }}</template>
          </el-input>
        </el-form-item>

        <el-form-item label="生效方式（重启后沿用上次选择）">
          <el-radio-group v-model="applyMode" :disabled="modeSaving" @change="onModeChange">
            <el-radio value="future">仅对后续数据生效</el-radio>
            <el-radio value="recompute">同时重算已有数据</el-radio>
          </el-radio-group>
        </el-form-item>

        <div v-if="globalError" class="global-error">
          <el-alert :title="globalError" type="error" :closable="false" show-icon />
        </div>

        <div class="actions">
          <el-button @click="resetForm">恢复为已保存值</el-button>
          <el-button type="primary" :loading="saving" @click="onSave">
            保存规则
          </el-button>
        </div>

        <p v-if="lastSavedAt" class="saved-at">
          当前规则更新于 {{ new Date(lastSavedAt * 1000).toLocaleString() }}
        </p>
      </el-form>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type {
  AnomalyRuleConfig, RuleForm, RuleField, RuleErrors, RuleBounds, DeviceTypeInfo,
} from '@/types'
import {
  fetchRules, fetchDeviceTypes, saveRule, setApplyMode, extractRuleErrors,
} from '@/api/rules'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const FIELDS: { key: RuleField }[] = [
  { key: 'temp_limit' }, { key: 'vib_limit' }, { key: 'pres_limit' },
  { key: 'period' }, { key: 'consecutive' },
]

const loading = ref(false)
const saving = ref(false)
const modeSaving = ref(false)
const globalError = ref('')
const deviceTypes = ref<DeviceTypeInfo[]>([])
const rulesMap = ref<Record<string, AnomalyRuleConfig>>({})
const bounds = ref<RuleBounds>({} as RuleBounds)
const currentType = ref('')
const applyMode = ref<'future' | 'recompute'>('future')

const form = reactive<RuleForm>(emptyForm())
const errors = reactive<RuleErrors>({})

const currentRule = computed(() =>
  currentType.value ? rulesMap.value[currentType.value] : undefined)
const lastSavedAt = computed(() => currentRule.value?.updated_at)

function emptyForm(): RuleForm {
  return { temp_limit: '', vib_limit: '', pres_limit: '', period: '', consecutive: '' }
}

function unit(key: RuleField): string {
  return ({ temp_limit: '°C', vib_limit: 'mm/s', pres_limit: 'MPa',
            period: '个采样点', consecutive: '次' } as const)[key]
}

function fmtBound(key: RuleField): string {
  const b = bounds.value[key]
  if (!b) return ''
  return `${b.min}~${b.max} ${unit(key)}`
}

function fillForm(r?: AnomalyRuleConfig) {
  for (const f of FIELDS) {
    form[f.key] = r ? String(r[f.key]) : ''
    errors[f.key] = undefined
  }
  globalError.value = ''
}

function clearError(key: RuleField) {
  errors[key] = undefined
  globalError.value = ''
}

function onTypeChange() {
  fillForm(currentRule.value)
}

function resetForm() {
  fillForm(currentRule.value)
}

async function load() {
  loading.value = true
  try {
    const [rulesResp, types] = await Promise.all([fetchRules(), fetchDeviceTypes()])
    rulesMap.value = rulesResp.rules
    bounds.value = rulesResp.bounds
    applyMode.value = rulesResp.apply_mode
    deviceTypes.value = types
    if (!currentType.value && types.length) currentType.value = types[0].value
    fillForm(rulesMap.value[currentType.value])
  } catch {
    globalError.value = '规则加载失败，请确认后端服务已启动'
  } finally {
    loading.value = false
  }
}

async function onSave() {
  saving.value = true
  globalError.value = ''
  for (const f of FIELDS) errors[f.key] = undefined
  try {
    const result = await saveRule(currentType.value, { ...form })
    rulesMap.value[currentType.value] = result.rule
    fillForm(result.rule)
    ElMessage.success(
      result.recomputed
        ? '规则已保存，设备状态与历史异常记录已按新口径重算'
        : '规则已保存，将对后续数据生效',
    )
  } catch (err) {
    const parsed = extractRuleErrors(err)
    Object.assign(errors, parsed.errors)
    globalError.value = parsed.message
    if (Object.keys(parsed.errors).length) {
      ElMessage.error('存在不合格项，未保存：' + Object.values(parsed.errors).join('；'))
    } else {
      ElMessage.error(parsed.message)
    }
  } finally {
    saving.value = false
  }
}

async function onModeChange(mode: string | number | boolean | undefined) {
  const m = mode as 'future' | 'recompute'
  modeSaving.value = true
  try {
    const result = await setApplyMode(m)
    applyMode.value = result.apply_mode
    ElMessage.success(
      result.recomputed
        ? '已选择“同时重算已有数据”，当前数据已重算，重启后沿用该选择'
        : '已选择“仅对后续数据生效”，重启后沿用该选择',
    )
  } catch {
    ElMessage.error('生效方式保存失败')
  } finally {
    modeSaving.value = false
  }
}

watch(() => props.modelValue, (open) => {
  if (open) load()
})

onMounted(load)
</script>

<style scoped>
.rules-panel{padding:0 4px}
.tip{margin-bottom:14px}
.rule-form{margin-top:4px}
.full{width:100%}
.invalid :deep(.el-input__wrapper){box-shadow:0 0 0 1px var(--el-color-danger) inset}
.actions{display:flex;justify-content:flex-end;gap:10px;margin-top:8px}
.global-error{margin-bottom:10px}
.saved-at{margin-top:10px;font-size:12px;color:#94a3b8;text-align:right}
</style>
