<template>
  <span class="replacement-entry">
    <el-button link type="warning" :disabled="disabled || env.status !== 'AUTHORIZED'" @click="open">新建项目并替换渠道</el-button>
    <span v-if="active" class="muted">{{ label(active.status) }}</span>
  </span>
  <el-dialog v-model="visible" title="新建项目并替换渠道" width="min(760px, 95vw)" :close-on-click-modal="false">
    <el-alert type="warning" :closable="false" show-icon title="将创建新 Project、SOCKS5 VM 和 JSON，替换当前后台的原渠道。"
      description="保留渠道 ID 和累计消耗。旧项目和 VM 会保留，可能继续计费；切换期间可能短暂中断，测试失败时尝试恢复原配置。" />
    <div v-loading="loading" class="replacement-content">
      <el-alert v-if="error" type="error" :title="error" :closable="false" />
      <el-descriptions v-if="preview" :column="1" border>
        <el-descriptions-item label="环境">{{ env.envName }}</el-descriptions-item>
        <el-descriptions-item label="目标后台">{{ preview.targetName }}</el-descriptions-item>
        <el-descriptions-item label="原渠道">{{ preview.channelName }}（ID {{ preview.channelId }}）</el-descriptions-item>
        <el-descriptions-item label="原 Project">{{ preview.projectId || '-' }}</el-descriptions-item>
        <el-descriptions-item label="Billing Account">{{ preview.billingAccountId || '尚未选择，请先在环境详情中选择' }}</el-descriptions-item>
      </el-descriptions>
      <el-form v-if="preview" label-width="100px" class="zone-form">
        <el-form-item label="VM 区域"><el-input v-model="zone" :disabled="busy || submitting" placeholder="沿用原任务区域；留空使用环境默认" /></el-form-item>
      </el-form>
      <el-table v-if="operations.length" :data="operations" size="small">
        <el-table-column label="替换记录" min-width="190"><template #default="{ row }">
          <div>{{ formatBeijingTime(row.createdAt) }}</div><div>{{ row.newProjectId || '等待创建' }}</div>
          <small>渠道 ID {{ row.channelId }}</small>
        </template></el-table-column>
        <el-table-column label="进度" min-width="240"><template #default="{ row }">
          <el-tag :type="row.status === 'SUCCESS' ? 'success' : retryable(row) ? 'danger' : 'warning'">{{ label(row.status) }}</el-tag>
          <div>{{ row.message }}</div><small v-if="row.status === 'PREPARING'">{{ row.currentStep }}</small>
        </template></el-table-column>
        <el-table-column label="操作" width="105"><template #default="{ row }">
          <el-button v-if="retryable(row)" link type="primary" :loading="retrying === row.id" :disabled="submitting || !!retrying || (busy && row.status !== 'UNKNOWN')" @click="retry(row)">
            {{ row.status === 'UNKNOWN' ? '核对 / 恢复' : '重试替换' }}
          </el-button>
        </template></el-table-column>
      </el-table>
    </div>
    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!preview?.billingAccountId || loading || busy || !!retrying" @click="submit">创建新项目并替换</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { createProjectReplacement, listProjectReplacements, previewProjectReplacement, retryProjectReplacement,
  type GcpEnvironmentResponse, type ProjectReplacement, type ReplacementPreview } from '../api/gcpEnv'
import { formatBeijingTime } from '../utils/beijingDate'

const props = defineProps<{ env: GcpEnvironmentResponse; targetId: string; disabled?: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const visible = ref(false), loading = ref(false), submitting = ref(false), retrying = ref(''), error = ref(''), zone = ref('')
const preview = ref<ReplacementPreview>(), operations = ref<ProjectReplacement[]>([])
const requestId = ref('')
const active = computed(() => operations.value.find(op => ['PENDING', 'PREPARING', 'READY', 'UPDATING', 'TESTING', 'ROLLING_BACK', 'UNKNOWN'].includes(op.status)))
const busy = computed(() => Boolean(active.value))
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
function label(status: string) {
  return ({ PENDING: '等待执行', PREPARING: '准备新项目', READY: '准备替换', UPDATING: '更新渠道', TESTING: '测试渠道',
    ROLLING_BACK: '恢复原配置', UNKNOWN: '结果待核对', FAILED: '准备或校验失败', ROLLED_BACK: '已恢复原配置', SUCCESS: '替换成功' } as Record<string, string>)[status] || status
}
function retryable(op: ProjectReplacement) { return ['FAILED', 'ROLLED_BACK', 'UNKNOWN'].includes(op.status) }
async function refresh() {
  try {
    const previous = operations.value[0]
    operations.value = await listProjectReplacements(props.env.envId)
    const latest = operations.value[0]
    if (previous && latest?.id === previous.id && latest.status !== previous.status && ['SUCCESS', 'FAILED', 'ROLLED_BACK', 'UNKNOWN'].includes(latest.status)) emit('changed')
  } catch (e: any) {
    if (visible.value) error.value = e?.response?.data?.detail || '加载替换进度失败'
  } finally {
    clearTimeout(timer)
    if (!disposed && active.value && active.value.status !== 'UNKNOWN') timer = setTimeout(() => void refresh(), 3000)
  }
}
async function open() {
  visible.value = true
  preview.value = undefined
  error.value = ''
  loading.value = true
  requestId.value = ''
  await refresh()
  try {
    if (!props.targetId) throw new Error('请先选择可用的目标后台')
    preview.value = await previewProjectReplacement(props.env.envId, props.targetId)
    zone.value = preview.value.vmZone || ''
  } catch (e: any) { error.value = e?.response?.data?.detail || e.message || '无法读取待替换渠道' }
  finally { loading.value = false }
}
async function submit() {
  if (!preview.value || busy.value) return
  // Keep this ID after a request timeout so another click cannot start a second rotation.
  if (!requestId.value) {
    const bytes = crypto.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6]! & 15) | 64
    bytes[8] = (bytes[8]! & 63) | 128
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
    requestId.value = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  submitting.value = true
  error.value = ''
  try {
    await createProjectReplacement(props.env.envId, { targetId: preview.value.targetId,
      deploymentId: preview.value.deploymentId, requestId: requestId.value, vmZone: zone.value.trim() || undefined })
    requestId.value = ''
    ElMessage.success('已提交新建项目和渠道替换任务')
    await refresh()
    emit('changed')
  } catch (e: any) {
    error.value = e?.response?.data?.detail || '提交结果未确认，请刷新进度后重试'
    await refresh()
  } finally { submitting.value = false }
}
async function retry(op: ProjectReplacement) {
  retrying.value = op.id
  error.value = ''
  try { await retryProjectReplacement(props.env.envId, op.id); await refresh(); emit('changed') }
  catch (e: any) { error.value = e?.response?.data?.detail || '重试提交失败'; await refresh() }
  finally { retrying.value = '' }
}
watch(() => props.targetId, () => { if (visible.value && !submitting.value) void open() })
onMounted(() => { void refresh() })
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>

<style scoped>
.replacement-entry { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.replacement-content { margin-top: 16px; min-height: 80px; }
.zone-form { margin-top: 16px; }
</style>
