<template>
  <div class="page">
    <div class="header-row"><div><h1>凭据管理</h1><p class="muted">GCP 自动生成和直接上传的 JSON / Key 共用后台与推送记录。</p></div><div><el-button :loading="importing" @click="importHistory">补录历史 GCP 凭据</el-button><el-button :loading="loading" @click="load()">刷新</el-button></div></div>
    <el-alert v-if="importFailures.length" type="warning" :closable="true" :title="`${importFailures.length} 个历史任务无法归档，请检查密钥文件和归属`" :description="importFailures.map(f => f.taskId).join('，')" class="notice" />
        <el-alert type="info" :closable="false" show-icon class="notice" title="凭据在数据库中加密保存，查看和下载会记录审计日志。历史上已清除的内容需要原用户重新上传。" />
        <el-card shadow="never" v-loading="loading">
          <el-table :data="uploads" empty-text="暂无上传凭据">
            <el-table-column type="expand"><template #default="{ row }">
              <el-table :data="row.deployments || []" empty-text="尚无其他后台的推送记录" class="inner-table">
                <el-table-column prop="targetName" label="后台" />
                <el-table-column prop="channelId" label="渠道 ID" />
                <el-table-column label="状态" min-width="220"><template #default="{ row: push }">{{ statusLabel(push.status) }}<div class="muted">{{ push.message }}</div></template></el-table-column>
                <el-table-column label="消耗"><template #default="{ row: push }">{{ push.usedAmount ?? '待同步' }} {{ push.currency }}</template></el-table-column>
                <el-table-column label="同步"><template #default="{ row: push }">{{ formatBeijingTime(push.syncedAt || undefined) }}<div class="warning">{{ push.syncError }}</div></template></el-table-column>
                <el-table-column label="操作"><template #default="{ row: push }"><el-button v-if="!['ACTIVE', 'CREATING', 'TESTING'].includes(push.status)" link type="primary" @click="retry(push.id)">重试 / 核对</el-button></template></el-table-column>
              </el-table>
            </template></el-table-column>
            <el-table-column prop="username" label="所属用户" width="150" />
            <el-table-column label="凭据" min-width="250"><template #default="{ row }"><div>{{ row.remark || row.identity }}</div><small class="muted">{{ row.origin === 'GCP' ? 'GCP 自动生成' : '直接上传' }} · {{ row.kind === 'vertex_json' ? 'Service Account JSON' : 'Gemini API Key' }}</small></template></el-table-column>
            <el-table-column label="保存状态" width="140"><template #default="{ row }"><el-tag :type="row.credentialStored ? 'success' : 'warning'">{{ row.credentialStored ? '已加密保存' : '历史内容已清除' }}</el-tag></template></el-table-column>
            <el-table-column label="原有渠道" min-width="190"><template #default="{ row }">{{ row.channelId ? `${row.targetName || '历史后台'} · ${row.channelId}` : '—' }}<small v-if="row.channelId" class="muted"> {{ statusLabel(row.status) }}</small></template></el-table-column>
            <el-table-column label="操作" min-width="250"><template #default="{ row }">
              <el-button link type="primary" :disabled="!row.credentialStored" @click="viewSecret(row)">查看</el-button>
              <el-button link type="primary" :disabled="!row.credentialStored" @click="download(row)">下载</el-button>
              <el-button link type="primary" :disabled="!row.credentialStored" @click="openPush(row)">选择后台推送</el-button>
              <el-button v-if="row.originEnvId" link type="primary" @click="openLegacy(row)">关联历史渠道</el-button>
              <el-button v-if="row.channelId && !['ACTIVE', 'CREATING', 'TESTING'].includes(row.status)" link type="primary" @click="retry(row.id)">核对</el-button>
            </template></el-table-column>
          </el-table>
          <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="total, prev, pager, next" @current-change="load()" />
        </el-card>


    <el-dialog v-model="legacyDialog" title="核对历史渠道归属" width="min(640px, 95vw)">
      <el-alert title="请确认历史渠道实际所在的后台。系统将核对渠道名称和 ID，不会创建新渠道。" type="info" :closable="false" class="notice" />
      <el-form label-position="top">
        <el-form-item label="历史推送（含结果未确认的记录）"><el-select v-model="legacyRecordId" placeholder="选择待关联历史记录" style="width: 100%"><el-option v-for="record in legacyRows" :key="record.id" :value="record.id" :label="`${record.channelName} · ID ${record.channelId || '待核对'}`" /></el-select></el-form-item>
        <el-form-item label="渠道实际所在后台"><el-select v-model="legacyTargetId" placeholder="选择已确认的后台" style="width: 100%"><el-option v-for="target in targets.filter(t => t.enabled)" :key="target.id" :value="target.id" :label="target.name" /></el-select></el-form-item>
      </el-form>
      <el-empty v-if="!legacyRows.length" description="没有待关联的历史推送" />
      <template #footer><el-button @click="legacyDialog = false">取消</el-button><el-button type="primary" :disabled="!legacyRecordId || !legacyTargetId" :loading="adopting" @click="adopt">核对并关联</el-button></template>
    </el-dialog>
    <el-dialog v-model="secretDialog" :title="secretTitle" width="min(760px, 95vw)" @closed="clearSecret">
      <div v-loading="secretLoading"><el-input :model-value="secret" type="textarea" :rows="16" readonly autocomplete="off" /></div>
      <template #footer><el-button @click="secretDialog = false">关闭</el-button></template>
    </el-dialog>
    <el-dialog v-model="pushDialog" title="选择目标后台" width="min(640px, 95vw)" :close-on-click-modal="false">
      <p>{{ selectedUpload?.username }} · {{ selectedUpload?.remark || selectedUpload?.identity }}</p>
      <el-alert type="info" :closable="false" class="notice" title="同一份凭据可推送到多个后台。同一后台重复操作会复用已有记录；创建结果不明确时只核对，不重复创建。" />
      <el-checkbox-group v-model="selectedTargets">
        <div v-for="target in targets" :key="target.id" class="target-option"><el-checkbox :value="target.id" :disabled="!target.enabled || pushing">{{ target.name }} {{ target.enabled ? '' : '（已停用）' }}</el-checkbox><small class="muted">{{ target.baseUrl }} · {{ target.group }} · {{ target.currency }}</small></div>
      </el-checkbox-group>
      <el-empty v-if="!targets.length" description="请先在“new-api 渠道管理”菜单新增后台" />
      <template #footer><el-button :disabled="pushing" @click="pushDialog = false">取消</el-button><el-button type="primary" :disabled="!selectedTargets.length" :loading="pushing" @click="push">推送到所选后台</el-button></template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { formatBeijingTime } from '../utils/beijingDate'
import { listUsers } from '../api/auth'
import { downloadCredential, getTargets, getVault, pushCredential, revealCredential, retryUpload,
  getLegacyPushes, adoptLegacyPush, importGcpCredentials, type LegacyPush, type NewApiTarget, type UploadedChannel } from '../api/channelUploads'

const page = ref(1), total = ref(0), loading = ref(false)
const importing = ref(false), importFailures = ref<{ taskId: string; message: string }[]>([])
const uploads = ref<UploadedChannel[]>([]), targets = ref<NewApiTarget[]>([])
const secretDialog = ref(false), secret = ref(''), secretTitle = ref(''), secretLoading = ref(false)
const pushDialog = ref(false), selectedUpload = ref<UploadedChannel>(), selectedTargets = ref<string[]>([]), pushing = ref(false)
const legacyDialog = ref(false), legacyUpload = ref<UploadedChannel>(), legacyRows = ref<LegacyPush[]>([]), legacyRecordId = ref(''), legacyTargetId = ref(''), adopting = ref(false)
let secretRequest = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined
function statusLabel(status: string) { return ({ STORED: '已保存', PENDING: '等待推送', CREATING: '创建中', TESTING: '测试中', ACTIVE: '已启用', UNKNOWN: '结果待核对', TEST_FAILED: '测试失败', BLOCKED: '配置或连接异常', REJECTED: '后台拒绝创建', EXPIRED: '历史凭据缺失', LEGACY_REVIEW: '历史渠道待核对' } as Record<string, string>)[status] || status }
function errorMessage(error: any) { const detail = error?.response?.data?.detail; ElMessage.error(typeof detail === 'string' ? detail : '操作失败，请检查输入和后台配置') }
async function load(quiet = false) {
  if (!quiet) loading.value = true
  try { const [list, destinations] = await Promise.all([getVault(page.value), getTargets()]); uploads.value = list.items; total.value = list.total; targets.value = destinations }
  catch (error) { if (!quiet) errorMessage(error) }
  finally { loading.value = false }
}
function poll() { timer = setTimeout(async () => { if (!document.hidden) await load(true); if (!disposed) poll() }, 5000) }
function clearSecret() { secretRequest++; secret.value = ''; secretLoading.value = false }
async function importHistory() {
  importing.value = true; importFailures.value = []
  let cursor = '', count = 0
  try {
    do { const result = await importGcpCredentials(cursor); count += result.imported; importFailures.value.push(...result.failures); cursor = result.nextCursor || '' } while (cursor && !disposed)
    ElMessage.success(`历史凭据归档完成：处理 ${count} 个成功任务，失败 ${importFailures.value.length} 个`); await load()
  } catch (error) { errorMessage(error) }
  finally { importing.value = false }
}
async function viewSecret(row: UploadedChannel) {
  clearSecret(); const request = secretRequest
  secretTitle.value = `${row.username} · ${row.kind === 'vertex_json' ? 'JSON' : 'API Key'}`
  secretDialog.value = true; secretLoading.value = true
  try { const result = await revealCredential(row.id); if (request === secretRequest && secretDialog.value) secret.value = result.credential }
  catch (error) { errorMessage(error); secretDialog.value = false }
  finally { if (request === secretRequest) secretLoading.value = false }
}
async function download(row: UploadedChannel) {
  try {
    const blob = await downloadCredential(row.id), url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = `credential-${row.id}.${row.kind === 'vertex_json' ? 'json' : 'txt'}`
    document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) { errorMessage(error) }
}
async function openPush(row: UploadedChannel) {
  try {
    const owner = (await listUsers()).find(user => user.id === row.userId)
    selectedUpload.value = row
    selectedTargets.value = targets.value.some(t => t.id === owner?.boundTargetId && t.enabled) ? [owner!.boundTargetId!] : []
    pushDialog.value = true
  } catch (error) { errorMessage(error) }
}
async function push() {
  if (!selectedUpload.value) return
  pushing.value = true
  try { await pushCredential(selectedUpload.value.id, selectedTargets.value); pushDialog.value = false; ElMessage.success('已提交推送任务'); await load() }
  catch (error) { errorMessage(error) }
  finally { pushing.value = false }
}
async function retry(id: string) { try { await retryUpload(id); ElMessage.success('已提交重试 / 核对'); await load() } catch (error) { errorMessage(error) } }
async function openLegacy(row: UploadedChannel) {
  try { legacyRows.value = await getLegacyPushes(row.id); legacyUpload.value = row; legacyRecordId.value = ''; legacyTargetId.value = ''; legacyDialog.value = true }
  catch (error) { errorMessage(error) }
}
async function adopt() {
  if (!legacyUpload.value || !legacyRecordId.value || !legacyTargetId.value) return
  adopting.value = true
  try { await adoptLegacyPush(legacyUpload.value.id, legacyRecordId.value, legacyTargetId.value); legacyDialog.value = false; ElMessage.success('历史渠道已核对并关联，消耗将统一统计'); await load() }
  catch (error) { errorMessage(error) }
  finally { adopting.value = false }
}
onMounted(() => { void load(); poll() })
onUnmounted(() => { disposed = true; clearTimeout(timer); clearSecret() })
</script>

<style scoped>
.notice { margin-bottom: 20px; }
.target-heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 16px; }
.target-option { margin-bottom: 12px; }
.target-option small { display: block; margin-left: 24px; }
.inner-table { margin: 12px; width: calc(100% - 24px); }
.warning { color: var(--el-color-warning); }
.el-pagination { margin-top: 20px; justify-content: flex-end; }
</style>
