<template>
  <div class="page create-page">
    <div class="header-row">
      <div>
        <h1>创建环境与授权</h1>
        <p class="muted">创建后会在后台独立启动 gcloud；无需等待当前环境授权完成即可继续创建。</p>
      </div>
      <el-button @click="$router.push('/gcp-envs')">返回环境列表</el-button>
    </div>

    <div class="card create-card">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="130px">
        <div class="form-grid">
          <el-form-item label="Google 账号" prop="googleAccount">
            <el-input v-model="form.googleAccount" placeholder="user@gmail.com" />
          </el-form-item>
          <el-form-item label="VM Zone（可选）"><el-input v-model="form.vmZone" placeholder="留空使用默认偏好；新任务创建时固定" /></el-form-item>
          <el-form-item label="启用代理">
            <el-switch v-model="form.proxy.enabled" />
          </el-form-item>
          <el-form-item v-if="form.proxy.enabled" label="代理 URL" prop="proxy.url">
            <el-input
              v-model="form.proxy.url"
              placeholder="socks5://127.0.0.1:7890:username:password"
              autocomplete="off"
            />
          </el-form-item>
        </div>

        <el-form-item>
          <div class="actions">
            <el-button type="primary" :loading="creating" @click="submit">创建并加入授权队列</el-button>
            <span class="muted">创建成功后可立即填写下一组环境。</span>
          </div>
        </el-form-item>
      </el-form>
    </div>

    <div class="session-heading">
      <div>
        <h2>授权会话</h2>
        <p class="muted">每个会话拥有独立的授权链接、授权码输入框和状态。</p>
      </div>
      <div class="session-tools">
        <el-tag v-if="activeCount" type="warning">进行中 {{ activeCount }}</el-tag>
        <el-button :loading="sessionsLoading" @click="loadSessions(false)">刷新</el-button>
      </div>
    </div>

    <div v-if="sessions.length" class="pagination-row">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        :total="sessions.length"
        layout="total, sizes, prev, pager, next"
      />
    </div>
    <div v-loading="sessionsLoading && !sessions.length" class="session-list">
      <el-empty v-if="!sessions.length && !sessionsLoading" description="暂无授权会话" />
      <el-card v-for="session in paginatedSessions" :key="session.loginSessionId" shadow="never" class="session-card">
        <template #header>
          <div class="session-card-header">
            <div>
              <strong>{{ session.envName || session.envId }}</strong>
              <span class="account">{{ session.googleAccountHint || '-' }}</span>
            </div>
            <el-tag :type="sessionTagType(session.status)">{{ sessionStatusText(session.status) }}</el-tag>
          </div>
        </template>

        <div class="session-content">
          <div class="session-meta">
            <span class="muted">会话 {{ shortId(session.loginSessionId) }}</span>
            <span v-if="session.createdBy" class="muted">创建用户 {{ session.createdBy }}</span>
            <span class="muted">过期时间 {{ formatTime(session.expiresAt) }}</span>
          </div>

          <div v-if="session.authUrl && canOpenAuth(session.status)" class="auth-link-row">
            <div class="code-box auth-url">{{ session.authUrl }}</div>
            <div class="auth-actions">
              <el-button type="primary" @click="openAuthUrl(session.authUrl)">打开授权链接</el-button>
              <el-button @click="copyAuthUrl(session.authUrl)">复制链接</el-button>
              <el-button @click="copyAuthDetails(session)">一键复制全部</el-button>
            </div>
          </div>
          <el-alert
            v-else-if="isPreparing(session.status)"
            type="info"
            title="正在初始化 gcloud 并生成授权链接…"
            :closable="false"
            show-icon
          />

          <div v-if="session.status === 'WAITING_CODE'" class="code-submit-row">
            <el-input
              v-model="codes[session.loginSessionId]"
              type="password"
              show-password
              autocomplete="off"
              placeholder="粘贴 verification code"
              @keyup.enter="submitCode(session)"
            />
            <el-button
              type="success"
              :loading="Boolean(submittingSessionIds[session.loginSessionId])"
              :disabled="!codes[session.loginSessionId]?.trim()"
              @click="submitCode(session)"
            >提交授权码</el-button>
          </div>

          <el-alert
            v-if="session.status === 'CODE_SUBMITTED'"
            type="info"
            title="授权码已提交，正在验证账号并启动预设任务…"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="session.errorMessage"
            type="error"
            :title="session.errorMessage"
            :closable="false"
            show-icon
          />

          <div v-if="canCancel(session.status) || canRetry(session)" class="session-actions">
            <el-button
              type="danger"
              link
              :loading="Boolean(cancellingSessionIds[session.loginSessionId])"
              @click="cancelSession(session)"
            >取消登录</el-button>
            <el-button
              v-if="canRetry(session)"
              type="primary"
              :loading="Boolean(retryingSessionIds[session.loginSessionId])"
              @click="retrySession(session)"
            >再次授权</el-button>
          </div>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { useClientPagination } from '../composables/useClientPagination'
import { copyTextToClipboard } from '../utils/clipboard'
import {
  cancelLogin,
  listLoginSessions,
  retryLoginSession,
  startLogin,
  submitLoginCode,
  type LoginSessionResponse,
  type LoginSessionStatus,
  type ProxyConfigIn,
  type StartLoginRequest
} from '../api/gcpEnv'

const formRef = ref<FormInstance>()
const router = useRouter()
const creating = ref(false)
const sessionsLoading = ref(false)
const sessions = ref<LoginSessionResponse[]>([])
const { currentPage, pageSize, paginatedItems: paginatedSessions, resetPage } = useClientPagination(sessions)
const codes = reactive<Record<string, string>>({})
const submittingSessionIds = reactive<Record<string, boolean>>({})
const cancellingSessionIds = reactive<Record<string, boolean>>({})
const retryingSessionIds = reactive<Record<string, boolean>>({})
let pollTimer: number | undefined

interface CreateEnvForm {
  vmZone: string
  googleAccount: string
  proxy: { enabled: boolean; url: string }
}

const activeStatuses = new Set<LoginSessionStatus>([
  'PENDING', 'PROCESS_STARTED', 'AUTH_URL_READY', 'WAITING_CODE', 'CODE_SUBMITTED'
])
const activeCount = computed(() => sessions.value.filter((session) => activeStatuses.has(session.status)).length)

const proxyTypeByProtocol: Record<string, NonNullable<ProxyConfigIn['type']>> = {
  http: 'http', http_no_tunnel: 'http_no_tunnel', socks4: 'socks4', socks5: 'socks5', sock5: 'socks5'
}
const colonProxyUrlPattern = /^([a-zA-Z][a-zA-Z0-9+._-]*):\/\/([^:/?#]+):(\d{1,5}):([^:]+):(.+)$/

const form = reactive<CreateEnvForm>({
  vmZone: '',
  googleAccount: '',
  proxy: { enabled: true, url: 'socks5://127.0.0.1:7890' }
})

const rules: FormRules = {
  googleAccount: [{ required: true, message: '请输入 Google 账号', trigger: 'blur' }],
  'proxy.url': [{
    validator: (_rule, value: string, callback) => {
      if (!form.proxy.enabled) return callback()
      try {
        parseProxyUrl(value)
        callback()
      } catch (error) {
        callback(error instanceof Error ? error : new Error('代理 URL 格式不正确'))
      }
    },
    trigger: 'blur'
  }]
}

function parseProxyUrl(value: string): ProxyConfigIn {
  const rawValue = value.trim()
  if (!rawValue) throw new Error('请输入代理 URL')
  const colonProxyUrl = parseColonProxyUrl(rawValue)
  if (colonProxyUrl) return colonProxyUrl

  let proxyUrl: URL
  try { proxyUrl = new URL(rawValue) } catch { throw new Error('代理 URL 格式不正确，例如 socks5://127.0.0.1:7890') }
  const type = getProxyType(proxyUrl.protocol.replace(':', '').toLowerCase())
  if (!proxyUrl.hostname) throw new Error('代理 URL 缺少地址')
  if (!proxyUrl.port) throw new Error('代理 URL 缺少端口')
  return {
    enabled: true,
    type,
    host: proxyUrl.hostname,
    port: parseProxyPort(proxyUrl.port),
    username: proxyUrl.username ? decodeProxyPart(proxyUrl.username) : undefined,
    password: proxyUrl.password ? decodeProxyPart(proxyUrl.password) : undefined
  }
}

function parseColonProxyUrl(value: string): ProxyConfigIn | null {
  const match = value.match(colonProxyUrlPattern)
  if (!match) return null
  const [, protocol, host, portValue, username, password] = match
  return {
    enabled: true,
    type: getProxyType(protocol.toLowerCase()),
    host,
    port: parseProxyPort(portValue),
    username: decodeProxyPart(username),
    password: decodeProxyPart(password)
  }
}

function getProxyType(protocol: string): NonNullable<ProxyConfigIn['type']> {
  const type = proxyTypeByProtocol[protocol]
  if (!type) throw new Error('代理协议仅支持 http、socks4、socks5')
  return type
}

function parseProxyPort(value: string): number {
  const port = Number(value)
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('代理端口必须在 1 到 65535 之间')
  return port
}

function decodeProxyPart(value: string): string {
  try { return decodeURIComponent(value) } catch { return value }
}

async function submit() {
  await formRef.value?.validate()
  creating.value = true
  try {
    const payload: StartLoginRequest = {
      googleAccount: form.googleAccount.trim(),
      vmZone: form.vmZone.trim() || undefined,
      proxy: form.proxy.enabled ? parseProxyUrl(form.proxy.url) : { enabled: false }
    }
    await startLogin(payload)
    resetPage()
    ElMessage.success('环境已加入授权队列，可以继续创建下一个环境')
    form.googleAccount = ''
    form.proxy.url = ''
    formRef.value?.clearValidate()
    await loadSessions(true)
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (error?.response?.status === 409 && detail?.code === 'GOOGLE_ACCOUNT_ALREADY_EXISTS') {
      void ElMessageBox.confirm(
        detail.message || '该 Google 账号已经存在对应环境，请使用已有环境。',
        '目标账号已存在',
        {
          type: 'warning',
          confirmButtonText: '查看已有环境',
          cancelButtonText: '留在当前页'
        }
      ).then(() => router.push(`/gcp-envs/${detail.envId}`)).catch(() => undefined)
    } else {
      ElMessage.error(typeof detail === 'string' ? detail : '创建环境失败')
    }
  } finally {
    creating.value = false
  }
}

async function loadSessions(silent = true) {
  if (sessionsLoading.value) return
  sessionsLoading.value = true
  try {
    sessions.value = await listLoginSessions({ limit: 500 })
  } catch (error: any) {
    if (!silent) ElMessage.error(error?.response?.data?.detail || '加载授权会话失败')
  } finally {
    sessionsLoading.value = false
    schedulePoll()
  }
}

function schedulePoll() {
  if (pollTimer) window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(() => void loadSessions(true), activeCount.value ? 2000 : 5000)
}

async function submitCode(session: LoginSessionResponse) {
  const code = codes[session.loginSessionId]?.trim()
  if (!code || submittingSessionIds[session.loginSessionId]) return
  submittingSessionIds[session.loginSessionId] = true
  try {
    await submitLoginCode(session.loginSessionId, code)
    codes[session.loginSessionId] = ''
    ElMessage.success(`授权码已提交：${session.envName || session.googleAccountHint}`)
    await loadSessions(true)
  } catch (error: any) {
    codes[session.loginSessionId] = ''
    ElMessage.error(error?.response?.data?.detail || '提交授权码失败')
  } finally {
    delete submittingSessionIds[session.loginSessionId]
  }
}

async function cancelSession(session: LoginSessionResponse) {
  cancellingSessionIds[session.loginSessionId] = true
  try {
    await cancelLogin(session.loginSessionId)
    ElMessage.success(`已取消：${session.envName || session.googleAccountHint}`)
    await loadSessions(true)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '取消失败')
  } finally {
    delete cancellingSessionIds[session.loginSessionId]
  }
}

async function retrySession(session: LoginSessionResponse) {
  if (!canRetry(session) || retryingSessionIds[session.loginSessionId]) return
  retryingSessionIds[session.loginSessionId] = true
  try {
    await retryLoginSession(session.loginSessionId)
    resetPage()
    ElMessage.success(`已重新发起授权：${session.envName || session.googleAccountHint}`)
    await loadSessions(true)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '再次授权失败')
  } finally {
    delete retryingSessionIds[session.loginSessionId]
  }
}

function openAuthUrl(url: string) { window.open(url, '_blank', 'noopener,noreferrer') }
async function copyAuthUrl(url: string) {
  try {
    await copyTextToClipboard(url)
    ElMessage.success('授权链接已复制')
  } catch {
    ElMessage.error('复制授权链接失败')
  }
}
async function copyAuthDetails(session: LoginSessionResponse) {
  if (!session.authUrl) return
  const content = [
    `授权链接：${session.authUrl}`,
    `用户名：${session.googleAccountHint || '-'}`,
    `环境名：${session.envName || '-'}`
  ].join('\n')
  try {
    await copyTextToClipboard(content)
    ElMessage.success('授权信息已复制')
  } catch {
    ElMessage.error('复制授权信息失败')
  }
}
function isPreparing(status: LoginSessionStatus) {
  return status === 'PENDING' || status === 'PROCESS_STARTED' || status === 'AUTH_URL_READY'
}
function canCancel(status: LoginSessionStatus) {
  return status === 'PENDING' || status === 'PROCESS_STARTED' || status === 'AUTH_URL_READY' || status === 'WAITING_CODE'
}
function canOpenAuth(status: LoginSessionStatus) {
  return status === 'AUTH_URL_READY' || status === 'WAITING_CODE'
}
function canRetry(session: LoginSessionResponse) {
  if (session.status !== 'FAILED' && session.status !== 'TIMEOUT' && session.status !== 'CANCELLED') return false
  const latest = sessions.value.find((item) => item.envId === session.envId)
  if (latest?.loginSessionId !== session.loginSessionId) return false
  return !sessions.value.some((item) => item.envId === session.envId && activeStatuses.has(item.status))
}
function sessionTagType(status: LoginSessionStatus) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED' || status === 'TIMEOUT') return 'danger'
  if (status === 'CANCELLED') return 'info'
  return 'warning'
}
function sessionStatusText(status: LoginSessionStatus) {
  const labels: Record<LoginSessionStatus, string> = {
    PENDING: '等待初始化', PROCESS_STARTED: '生成授权链接', AUTH_URL_READY: '授权链接就绪',
    WAITING_CODE: '等待授权码', CODE_SUBMITTED: '正在验证', SUCCESS: '授权成功',
    FAILED: '授权失败', TIMEOUT: '已过期', CANCELLED: '已取消'
  }
  return labels[status]
}
function shortId(value: string) { return value.slice(0, 8) }
function formatTime(value: string) { return new Date(value).toLocaleString() }

onMounted(() => void loadSessions(false))
onBeforeUnmount(() => { if (pollTimer) window.clearTimeout(pollTimer) })
</script>

<style scoped>
.create-page { max-width: 1280px; }
.header-row, .session-heading, .session-card-header, .session-meta, .session-tools {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
}
h1, h2 { margin: 0 0 8px; }
.create-card { margin-top: 20px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; column-gap: 24px; }
.session-heading { margin: 28px 0 14px; }
.session-list { min-height: 120px; display: grid; gap: 14px; }
.session-card-header > div { display: flex; align-items: center; gap: 12px; min-width: 0; }
.account { color: #91a0b8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-content { display: grid; gap: 14px; }
.session-meta { justify-content: flex-start; font-size: 12px; }
.auth-link-row, .code-submit-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; }
.auth-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.auth-actions :deep(.el-button + .el-button) { margin-left: 0; }
.auth-url { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-actions { display: flex; justify-content: flex-end; }
.pagination-row { display: flex; justify-content: flex-end; overflow-x: auto; padding-bottom: 12px; }
@media (max-width: 800px) {
  .form-grid { grid-template-columns: 1fr; }
  .header-row, .session-heading { align-items: flex-start; }
  .auth-link-row, .code-submit-row { grid-template-columns: 1fr; }
}
</style>
