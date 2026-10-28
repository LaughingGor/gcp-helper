<template>
  <div class="page">
    <div class="header-row">
      <div><h1>我的渠道</h1><p class="muted">统一查看 GCP 生成和直接上传的凭据；展开记录查看各后台的渠道与消耗。</p></div>
      <div>
        <el-button :loading="refreshing" @click="refreshUsage">同步我的消耗</el-button>
        <el-button v-if="auth.hasPermission('channel:upload')" type="primary" @click="openUpload">上传渠道</el-button>
      </div>
    </div>
    <el-alert type="info" :closable="false" show-icon class="notice"
      title="金额为 new-api 记账消耗，并非供应商账单。系统定期同步；缺失或异常数据保留历史值，不会按零处理。" />
    <el-row :gutter="16" class="summary">
      <el-col :xs="12" :sm="6"><el-card shadow="never"><div class="muted">凭据数量</div><strong>{{ summary?.channelCount ?? 0 }}</strong></el-card></el-col>
      <el-col :xs="12" :sm="6"><el-card shadow="never"><div class="muted">远程启用</div><strong>{{ summary?.activeCount ?? 0 }}</strong></el-card></el-col>
      <el-col :xs="12" :sm="6"><el-card shadow="never"><div class="muted">累计消耗</div><strong v-for="(amount, currency) in summary?.totals" :key="currency">{{ amount }} {{ currency }}</strong><strong v-if="!Object.keys(summary?.totals || {}).length">待同步</strong></el-card></el-col>
      <el-col :xs="12" :sm="6"><el-card shadow="never"><div class="muted">未同步 / 数据异常</div><strong>{{ summary?.incompleteCount ?? 0 }}</strong></el-card></el-col>
    </el-row>
    <el-card v-if="auth.hasPermission('user:manage')" shadow="never" class="notice">
      <h2>用户消耗汇总</h2>
      <el-table :data="users" size="small" empty-text="尚无上传记录">
        <el-table-column prop="username" label="用户" />
        <el-table-column prop="channelCount" label="渠道数" />
        <el-table-column label="累计消耗"><template #default="{ row }"><span v-for="(amount, currency) in row.totals" :key="currency">{{ amount }} {{ currency }} </span></template></el-table-column>
        <el-table-column prop="incompleteCount" label="未同步 / 异常" />
        <el-table-column label="操作"><template #default="{ row }"><el-button link type="primary" @click="viewUser(row)">查看明细</el-button></template></el-table-column>
      </el-table>
    </el-card>
    <el-card shadow="never" v-loading="loading">
      <div class="table-title"><h2>{{ selectedUser ? selectedUser.username + ' 的渠道' : '我的凭据与渠道' }}</h2><el-button v-if="selectedUser" @click="viewMine">返回我的渠道</el-button></div>
      <el-table :data="channels" empty-text="暂无渠道，点击“上传渠道”开始">
        <el-table-column type="expand">
          <template #default="{ row }">
            <el-table :data="row.deployments || []" empty-text="尚未推送到其他后台" style="margin: 12px; width: calc(100% - 24px)">
              <el-table-column prop="targetName" label="目标后台" />
              <el-table-column prop="channelId" label="渠道 ID" />
              <el-table-column label="状态"><template #default="{ row: push }">{{ statusLabel(push.status) }} · {{ remoteLabel(push.remoteStatus) }}</template></el-table-column>
              <el-table-column label="累计消耗"><template #default="{ row: push }">{{ push.usedAmount === null ? '待同步' : `${push.usedAmount} ${push.currency}` }}</template></el-table-column>
              <el-table-column label="同步信息" min-width="220"><template #default="{ row: push }">{{ formatBeijingTime(push.syncedAt || undefined) }}<div class="error">{{ push.syncError }}</div></template></el-table-column>
            </el-table>
          </template>
        </el-table-column>
        <el-table-column label="渠道" min-width="210"><template #default="{ row }"><div>{{ row.remark || row.identity }}</div><small class="muted">{{ row.origin === 'GCP' ? 'GCP 自动生成' : '直接上传' }} · {{ row.kind === 'vertex_json' ? 'Vertex JSON' : 'Gemini Key' }} · ID {{ row.channelId || '待创建' }}</small></template></el-table-column>
        <el-table-column label="创建状态" min-width="230"><template #default="{ row }"><el-tag :type="row.status === 'ACTIVE' ? 'success' : 'warning'">{{ statusLabel(row.status) }}</el-tag><div class="muted">{{ row.message }}</div></template></el-table-column>
        <el-table-column label="远程状态" width="125"><template #default="{ row }">{{ remoteLabel(row.remoteStatus) }}</template></el-table-column>
        <el-table-column label="累计消耗" width="150"><template #default="{ row }">{{ row.usedAmount === null ? (row.deployments?.length ? '展开查看各后台明细' : '尚无有效消耗') : `${row.usedAmount} ${row.currency}` }}</template></el-table-column>
        <el-table-column label="同步信息" min-width="220"><template #default="{ row }"><div>{{ formatBeijingTime(row.syncedAt || undefined) }}</div><small class="error">{{ row.syncError }}</small></template></el-table-column>
        <el-table-column label="凭据保存" width="130"><template #default="{ row }">{{ row.credentialStored ? '已加密持久保存' : '历史凭据已清除' }}</template></el-table-column>
      </el-table>
      <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" @current-change="load()" />
    </el-card>

    <el-dialog v-model="dialog" title="上传渠道" width="min(640px, 95vw)" :close-on-click-modal="false" @closed="clearSecret">
      <el-alert title="上传的 JSON / Key 将长期加密保存，管理员可以查看、下载并选择后台推送。上传不会自动创建远程渠道。" type="info" :closable="false" class="notice" />
      <el-form label-position="top">
        <el-form-item label="凭据类型"><el-radio-group v-model="form.kind" :disabled="submitting" @change="resetPreview"><el-radio-button value="vertex_json">Service Account JSON</el-radio-button><el-radio-button value="gemini_key">Gemini API Key</el-radio-button></el-radio-group></el-form-item>
        <el-form-item v-if="form.kind === 'vertex_json'" label="选择 JSON 文件（最大 64 KB）"><input ref="fileInput" type="file" accept=".json,application/json" :disabled="submitting" @change="readFile" /></el-form-item>
        <el-form-item :label="form.kind === 'vertex_json' ? 'JSON 内容' : 'API Key'">
          <el-input v-model="form.credential" :type="form.kind === 'vertex_json' ? 'textarea' : 'password'" :rows="7" :show-password="form.kind === 'gemini_key'" :disabled="submitting" autocomplete="off" @input="resetPreview" />
        </el-form-item>
        <el-form-item label="备注（可选）"><el-input v-model="form.remark" maxlength="255" :disabled="submitting" /></el-form-item>
        <el-alert v-if="previewIdentity" :title="'已校验：' + previewIdentity" type="success" :closable="false" />
      </el-form>
      <template #footer><el-button :disabled="submitting" @click="dialog = false">取消</el-button><el-button :loading="validating" :disabled="submitting || !form.credential" @click="validate">校验预览</el-button><el-button type="primary" :disabled="!previewIdentity" :loading="submitting" @click="submit">保存凭据</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { formatBeijingTime } from '../utils/beijingDate'
import { createUpload, getUploadUsage, getUsersUsage, listUploads, previewUpload, refreshUploadUsage,
  type UploadedChannel, type UploadInput, type UsageSummary, type UserUsage } from '../api/channelUploads'

const auth = useAuthStore()
const loading = ref(false), refreshing = ref(false), dialog = ref(false), validating = ref(false), submitting = ref(false)
const channels = ref<UploadedChannel[]>([]), users = ref<UserUsage[]>([]), summary = ref<UsageSummary>()
const page = ref(1), total = ref(0), previewIdentity = ref('')
const selectedUser = ref<UserUsage>(), fileInput = ref<HTMLInputElement>()
const form = reactive<UploadInput>({ kind: 'vertex_json', credential: '', remark: '' })
let requestId = '', poll: ReturnType<typeof setTimeout> | undefined, disposed = false

function newRequestId() {
  // crypto.randomUUID is unavailable on plain HTTP deployments. getRandomValues still works.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
  const value = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`
}
function statusLabel(status: string) {
  return ({ STORED: '凭据已保存', PENDING: '等待执行', CREATING: '创建中', TESTING: '测试中', ACTIVE: '创建成功', TEST_FAILED: '测试失败', UNKNOWN: '结果待核对', REJECTED: '创建被拒绝', EXPIRED: '凭据已过期', BLOCKED: '连接或配置异常', LEGACY_REVIEW: '历史渠道待管理员核对' } as Record<string, string>)[status] || status
}
function remoteLabel(status: string | null) {
  return ({ '1': '启用', '2': '手动禁用', '3': '自动禁用', MISSING: '已删除 / 不可见' } as Record<string, string>)[status || ''] || '待同步'
}
function showError(error: any) {
  const detail = error?.response?.data?.detail
  ElMessage.error(typeof detail === 'string' ? detail : '操作失败，请检查配置后重试')
}
async function load(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const [list, usage, allUsers] = await Promise.all([
      listUploads(page.value, 20, selectedUser.value?.userId), getUploadUsage(),
      auth.hasPermission('user:manage') ? getUsersUsage() : Promise.resolve([])
    ])
    channels.value = list.items; total.value = list.total; summary.value = usage; users.value = allUsers
  } catch (error) { if (!quiet) showError(error) }
  finally { loading.value = false }
}
function schedulePoll() {
  poll = setTimeout(async () => { if (!document.hidden) await load(true); if (!disposed) schedulePoll() }, 5000)
}
function viewUser(user: UserUsage) { selectedUser.value = user; page.value = 1; void load() }
function viewMine() { selectedUser.value = undefined; page.value = 1; void load() }
function resetPreview() { previewIdentity.value = ''; requestId = '' }
function clearSecret() { form.credential = ''; previewIdentity.value = ''; form.remark = ''; requestId = ''; if (fileInput.value) fileInput.value.value = '' }
function openUpload() { clearSecret(); dialog.value = true }
async function readFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  resetPreview()
  if (!file) return
  if (file.size > 65536) { ElMessage.error('文件不能超过 64 KB'); (event.target as HTMLInputElement).value = ''; return }
  try { form.credential = await file.text() } catch { ElMessage.error('读取文件失败') }
}
async function validate() {
  validating.value = true
  const credential = form.credential, kind = form.kind
  try {
    const result = await previewUpload({ ...form })
    if (credential === form.credential && kind === form.kind) previewIdentity.value = result.identity
  } catch (error) { previewIdentity.value = ''; showError(error) }
  finally { validating.value = false }
}
async function submit() {
  submitting.value = true
  requestId ||= newRequestId()
  try {
    await createUpload({ ...form }, requestId)
    dialog.value = false; clearSecret(); viewMine()
    ElMessage.success('凭据已加密保存，等待管理员推送')
  } catch (error) { showError(error) }
  finally { submitting.value = false }
}
async function refreshUsage() {
  refreshing.value = true
  try { const result = await refreshUploadUsage(); ElMessage.success(result.message); await load() } catch (error) { showError(error) }
  finally { refreshing.value = false }
}
onMounted(() => { void load(); schedulePoll() })
onUnmounted(() => { disposed = true; clearTimeout(poll); clearSecret() })
</script>

<style scoped>
.notice { margin-bottom: 20px; }
.summary { margin-bottom: 20px; }
.summary .el-col { margin-bottom: 12px; }
.summary strong { display: block; margin-top: 10px; font-size: 23px; }
.table-title { display: flex; justify-content: space-between; align-items: center; }
.error { color: var(--el-color-warning); }
.el-pagination { margin-top: 20px; justify-content: flex-end; }
</style>
