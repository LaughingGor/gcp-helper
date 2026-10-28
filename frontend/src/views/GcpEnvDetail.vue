<template>
  <div class="page detail-page">
    <div class="detail-header">
      <div>
        <el-button link type="primary" class="back-link" @click="returnToList">← 返回环境列表</el-button>
        <div class="title-row">
          <h1>{{ env?.envName || '环境详情' }}</h1>
          <el-tag v-if="env" :type="envStatusType(env.status)" effect="dark">{{ env.status }}</el-tag>
        </div>
        <p class="muted">查看环境授权、Project 和自动化资源的完整状态。</p>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-skeleton v-if="loading && !env" :rows="8" animated />
    <el-alert v-else-if="loadError" type="error" :title="loadError" show-icon />

    <template v-else-if="env">
      <div class="summary-grid">
        <el-card shadow="never" class="summary-card account-card">
          <span class="eyebrow">已授权账号</span>
          <strong>{{ env.authorizedAccount || '-' }}</strong>
          <span class="muted">目标账号：{{ env.googleAccountHint }}</span>
        </el-card>
        <el-card shadow="never" class="summary-card project-card">
          <span class="eyebrow">当前激活的 Project</span>
          <div class="copy-row">
            <strong class="mono">{{ env.defaultProjectId || '-' }}</strong>
            <el-button v-if="env.defaultProjectId" link type="primary" @click="copyValue(env.defaultProjectId, 'Project 已复制')">复制</el-button>
          </div>
          <span class="muted">{{ task?.status === 'RUNNING' ? '资源正在配置中' : projectSummary }}</span>
        </el-card>
        <el-card shadow="never" class="summary-card task-card">
          <span class="eyebrow">最新预设任务</span>
          <div class="copy-row">
            <el-tag :type="taskStatusType(task?.status)">{{ task?.status || '未执行' }}</el-tag>
            <span v-if="task?.currentStep && task.status !== 'SUCCESS'" class="muted">{{ taskStepText(task.currentStep) }}</span>
          </div>
          <span class="muted">{{ task?.finishedAt ? `完成于 ${formatBeijingTime(task.finishedAt)}` : '等待任务完成' }}</span>
        </el-card>
      </div>

      <div class="detail-grid">
        <el-card shadow="never">
          <template #header><span class="section-title">环境信息</span></template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="环境 ID"><span class="mono">{{ env.envId }}</span></el-descriptions-item>
            <el-descriptions-item label="环境名称">{{ env.envName }}</el-descriptions-item>
            <el-descriptions-item label="创建用户">{{ env.createdBy || '-' }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ formatBeijingTime(env.createdAt) }}</el-descriptions-item>
            <el-descriptions-item label="代理状态">
              <el-tag :type="env.proxyEnabled ? 'success' : 'info'">{{ env.proxyEnabled ? '已启用' : '未启用' }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="gcloud 配置">
              <div class="copy-row">
                <span class="mono">{{ env.configName }}</span>
                <el-button link type="primary" @click="copyValue(env.configName, 'gcloud 配置名已复制')">复制</el-button>
              </div>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never">
          <template #header><span class="section-title">云资源</span></template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="Project"><span class="mono">{{ task?.projectId || env.defaultProjectId || '-' }}</span></el-descriptions-item>
            <el-descriptions-item label="VM">{{ task?.vmName || '-' }}</el-descriptions-item>
            <el-descriptions-item label="任务计划 Zone">{{ task?.requestedVmZone || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Zone">{{ task?.vmZone || env.defaultZone || '-' }}</el-descriptions-item>
            <el-descriptions-item label="外网 IP"><span class="mono">{{ task?.vmExternalIp || '-' }}</span></el-descriptions-item>
            <el-descriptions-item label="防火墙规则">{{ task?.firewallRuleName || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Service Account">{{ task?.serviceAccountEmail || '-' }}</el-descriptions-item>
            <el-descriptions-item label="JSON 凭据归档">{{ task?.credentialStored ? '已加密归档，可在我的渠道中查看推送记录' : '尚未归档；推送时可尝试补录' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </div>

      <el-card v-if="auth.hasPermission('task:run')" shadow="never" class="billing-card">
        <template #header>
          <div class="card-header-row">
            <span class="section-title">Billing Account</span>
            <el-tag :type="env.billingAccountId ? 'success' : 'warning'">
              {{ env.billingAccountId ? '已选择' : '待选择' }}
            </el-tag>
          </div>
        </template>
        <el-alert
          type="warning"
          title="系统无法通过 API 确认 300 USD 赠金余额，请在 Google Cloud Billing Console 确认后选择对应账户。"
          :closable="false"
          show-icon
          class="billing-alert"
        />
        <el-form-item label="本次任务 VM Zone（可选）"><el-input v-model="requestedZone" placeholder="留空按环境 → 所属用户偏好 → 系统默认；提交后固定" style="max-width: 560px" /></el-form-item>
        <div class="billing-controls">
          <el-select
            v-model="billingAccountId"
            :loading="billingLoading"
            :disabled="Boolean(billingDisabledReason) || billingSaving"
            filterable
            placeholder="选择当前授权账号可访问的 Open Billing Account"
            class="billing-select"
          >
            <el-option
              v-for="account in billingAccounts"
              :key="account.billingAccountId"
              :label="`${account.displayName} (${account.billingAccountId})`"
              :value="account.billingAccountId"
            />
          </el-select>
          <el-tooltip :disabled="!billingDisabledReason" :content="billingDisabledReason">
            <el-button type="primary" :loading="billingSaving" :disabled="Boolean(billingDisabledReason)" @click="saveBillingAccount">
              {{ env.defaultProjectId ? '保存并重新绑定 Project' : '保存 Billing Account' }}
            </el-button>
          </el-tooltip>
          <el-button :loading="billingLoading" :disabled="billingSaving" @click="loadBillingAccounts">刷新列表</el-button>
        </div>
        <p v-if="env.billingAccountId" class="muted selected-billing">
          当前选择：{{ env.billingAccountDisplayName || env.billingAccountId }}（{{ env.billingAccountId }}）
        </p>
        <div v-if="canStartBootstrap" class="billing-start-row">
          <span class="muted">已选择 Billing Account，可以开始创建 Project 和资源。</span>
          <el-button type="success" plain :loading="bootstrapStarting" @click="startSelectedBillingBootstrap">启动预设任务</el-button>
        </div>
      </el-card>

      <el-card v-if="auth.hasPermission('task:run') && auth.hasPermission('channel:push')" shadow="never" class="task-detail-card">
        <template #header><span class="section-title">新建项目并替换渠道</span></template>
        <el-select v-if="canSelectTargets" v-model="targetId" :loading="targetsLoading" placeholder="选择目标后台" style="width: 280px; margin-right: 12px">
          <el-option v-for="target in targets" :key="target.id" :value="target.id" :label="target.name" />
        </el-select>
        <span v-else style="margin-right: 12px">{{ targetLabel }}</span>
        <ProjectReplacement :env="env" :target-id="targetId" @changed="load(true)" />
      </el-card>

      <el-card shadow="never" class="task-detail-card">
        <template #header>
          <div class="card-header-row">
            <span class="section-title">任务与连接信息</span>
            <el-button
              v-if="task?.keyFileExists && auth.hasPermission('secret:read')"
              type="primary"
              plain
              @click="downloadKey"
            >下载 Service Account JSON</el-button>
          </div>
        </template>

        <el-alert
          v-if="task?.errorMessage"
          type="error"
          :title="task.errorCode || '任务执行失败'"
          :description="task.errorMessage"
          show-icon
          class="task-error"
        />

        <el-descriptions :column="2" border class="task-descriptions">
          <el-descriptions-item label="任务 ID"><span class="mono">{{ task?.taskId || '-' }}</span></el-descriptions-item>
          <el-descriptions-item label="当前步骤">{{ taskStepText(task?.currentStep) }}</el-descriptions-item>
          <el-descriptions-item label="开始时间">{{ task?.startedAt ? formatBeijingTime(task.startedAt) : '-' }}</el-descriptions-item>
          <el-descriptions-item label="结束时间">{{ task?.finishedAt ? formatBeijingTime(task.finishedAt) : '-' }}</el-descriptions-item>
          <el-descriptions-item label="SOCKS5" :span="2">
            <div class="copy-row">
              <span class="mono break-all">{{ task?.socks5Url || '-' }}</span>
              <el-button v-if="task?.socks5Url" link type="primary" @click="copyValue(task.socks5Url, 'SOCKS5 链接已复制')">复制</el-button>
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="渠道状态">
            <el-tag v-if="env.channelPushStatus" :type="channelStatusType(env.channelPushStatus)">{{ env.channelPushStatus }}</el-tag>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="渠道结果">{{ env.channelPushMessage || '-' }}</el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card v-if="auth.hasPermission('gcloud:execute')" shadow="never" class="command-console-card">
        <template #header>
          <div class="card-header-row">
            <span class="section-title">gcloud 命令控制台</span>
            <el-button link type="primary" :loading="commandHistoryLoading" @click="loadCommandHistory">查看执行历史</el-button>
          </div>
        </template>
        <el-alert
          type="warning"
          title="只输入 gcloud 后的参数。命令将在当前环境的独立 gcloud 配置中执行。"
          description="未授权环境或正在运行预设任务的环境不可执行；可能修改云资源的命令需要二次确认。"
          :closable="false"
          show-icon
          class="command-alert"
        />
        <div class="command-presets">
          <el-button size="small" @click="commandInput = 'auth list'">auth list</el-button>
          <el-button size="small" @click="commandInput = 'projects list --format=table(projectId,name)'">projects list</el-button>
          <el-button size="small" @click="commandInput = 'compute instances list'">VM list</el-button>
        </div>
        <el-input
          v-model="commandInput"
          type="textarea"
          :rows="3"
          maxlength="4096"
          show-word-limit
          placeholder="例如：projects list --format=&quot;table(projectId,name)&quot;"
          :disabled="commandLoading"
          class="command-input"
        />
        <div class="command-actions">
          <el-select v-model="commandTimeout" :disabled="commandLoading" class="timeout-select">
            <el-option :value="30" label="30 秒超时" />
            <el-option :value="60" label="60 秒超时" />
            <el-option :value="120" label="120 秒超时" />
            <el-option :value="300" label="300 秒超时" />
          </el-select>
          <el-button :disabled="commandLoading" @click="clearCommandOutput">清空输出</el-button>
          <el-tooltip :disabled="!commandDisabledReason" :content="commandDisabledReason">
            <el-button type="primary" :loading="commandLoading" :disabled="Boolean(commandDisabledReason)" @click="runCommand">执行命令</el-button>
          </el-tooltip>
        </div>

        <template v-if="commandResult">
          <div class="command-result-header">
            <span class="mono">{{ commandResult.command }}</span>
            <el-tag :type="commandResult.success ? 'success' : 'danger'" effect="dark">
              {{ commandResult.success ? '成功' : '失败' }} · exit {{ commandResult.exitCode }} · {{ commandResult.durationMs }} ms
            </el-tag>
          </div>
          <el-alert v-if="commandResult.outputTruncated" type="warning" title="输出超过上限，界面仅显示截断后的内容" :closable="false" show-icon class="command-alert" />
          <div class="terminal-label">stdout</div>
          <pre class="command-output">{{ commandResult.stdout || '（无输出）' }}</pre>
          <template v-if="commandResult.stderr">
            <div class="terminal-label stderr-label">stderr</div>
            <pre class="command-output command-error-output">{{ commandResult.stderr }}</pre>
          </template>
        </template>

        <template v-if="commandHistory.length">
          <div class="history-title">最近执行记录</div>
          <el-table :data="commandHistory" size="small" max-height="250">
            <el-table-column prop="command" label="命令" min-width="280" show-overflow-tooltip />
            <el-table-column label="结果" width="100">
              <template #default="{ row }"><el-tag size="small" :type="row.status === 'SUCCESS' ? 'success' : 'danger'">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="exitCode" label="退出码" width="90" />
            <el-table-column label="执行时间" min-width="170"><template #default="{ row }">{{ formatBeijingTime(row.startedAt) }}</template></el-table-column>
          </el-table>
        </template>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { copyTextToClipboard } from '../utils/clipboard'
import { formatBeijingTime } from '../utils/beijingDate'
import { useTargetSelection } from '../composables/useTargetSelection'
import ProjectReplacement from '../components/ProjectReplacement.vue'
import {
  downloadServiceAccountKey,
  executeGcloudCommand,
  getGcpEnv,
  getLatestBootstrapTask,
  listBillingAccounts,
  listGcloudCommandHistory,
  startBootstrapTask,
  updateBillingAccount,
  type BillingAccountResponse,
  type GcloudCommandHistoryItem,
  type GcloudCommandResponse,
  type GcpEnvironmentResponse,
  type PresetTaskResponse
} from '../api/gcpEnv'

const props = defineProps<{ envId: string }>()
const router = useRouter()
const auth = useAuthStore()
const { targets, targetId, targetsLoading, canSelectTargets, targetLabel } = useTargetSelection(auth.hasPermission('channel:push'))
const requestedZone = ref('')
const env = ref<GcpEnvironmentResponse>()
const task = ref<PresetTaskResponse>()
const loading = ref(false)
const loadError = ref('')
const commandInput = ref('')
const commandTimeout = ref(60)
const commandLoading = ref(false)
const commandHistoryLoading = ref(false)
const commandResult = ref<GcloudCommandResponse>()
const commandHistory = ref<GcloudCommandHistoryItem[]>([])
const billingAccounts = ref<BillingAccountResponse[]>([])
const billingAccountId = ref('')
const billingLoading = ref(false)
const billingSaving = ref(false)
const bootstrapStarting = ref(false)
let pollTimer: number | undefined

const commandDisabledReason = computed(() => {
  if (!env.value || env.value.status !== 'AUTHORIZED') return '只有已授权的环境可以执行命令'
  if (task.value?.status === 'PENDING' || task.value?.status === 'RUNNING') return '预设任务运行中，暂不能执行命令'
  return ''
})

const billingDisabledReason = computed(() => {
  if (!env.value || env.value.status !== 'AUTHORIZED') return '只有已授权的环境可以选择 Billing Account'
  if (task.value?.status === 'PENDING' || task.value?.status === 'RUNNING') return '预设任务运行中，不能更换 Billing Account'
  return ''
})

const canStartBootstrap = computed(() => {
  if (!env.value?.billingAccountId || billingDisabledReason.value) return false
  return !task.value || task.value.status === 'FAILED'
})

const projectSummary = computed(() => {
  if (!env.value?.defaultProjectId) return '尚未选择 Project'
  return env.value.defaultProjectId.startsWith('gcph-') ? 'GCP Helper 管理的 Project' : '外部 Project'
})

async function loadCommandHistory() {
  commandHistoryLoading.value = true
  try {
    commandHistory.value = await listGcloudCommandHistory(props.envId)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '加载命令历史失败')
  } finally {
    commandHistoryLoading.value = false
  }
}

async function loadBillingAccounts() {
  if (!auth.hasPermission('task:run') || !env.value || env.value.status !== 'AUTHORIZED') return
  billingLoading.value = true
  try {
    billingAccounts.value = await listBillingAccounts(props.envId)
    billingAccountId.value = env.value.billingAccountId || ''
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '加载 Billing Account 列表失败')
  } finally {
    billingLoading.value = false
  }
}

async function saveBillingAccount() {
  if (!billingAccountId.value) {
    ElMessage.warning('请选择 Billing Account')
    return
  }
  const selected = billingAccounts.value.find((account) => account.billingAccountId === billingAccountId.value)
  const projectText = env.value?.defaultProjectId
    ? `将把 Project ${env.value.defaultProjectId} 重新链接到 ${selected?.displayName || billingAccountId.value}。`
    : `将保存 ${selected?.displayName || billingAccountId.value}，供后续创建 Project 时使用。`
  try {
    await ElMessageBox.confirm(`${projectText}\n请确认该账户是你希望使用的 Billing Account。`, '确认 Billing Account', {
      type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '取消'
    })
  } catch {
    return
  }
  billingSaving.value = true
  try {
    const result = await updateBillingAccount(props.envId, billingAccountId.value)
    if (env.value) {
      env.value.billingAccountId = result.billingAccountId
      env.value.billingAccountDisplayName = result.displayName
    }
    ElMessage.success(result.linkedProjectId ? `Billing Account 已重新链接到 ${result.linkedProjectId}` : 'Billing Account 已保存')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '保存 Billing Account 失败')
  } finally {
    billingSaving.value = false
  }
}

async function startSelectedBillingBootstrap() {
  bootstrapStarting.value = true
  try {
    task.value = await startBootstrapTask(props.envId, requestedZone.value.trim() || undefined)
    ElMessage.success('预设任务已启动')
    schedulePoll()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '启动预设任务失败')
  } finally {
    bootstrapStarting.value = false
  }
}

async function executeCommand(confirmHighRisk: boolean) {
  commandResult.value = await executeGcloudCommand(props.envId, {
    command: commandInput.value.trim(),
    timeoutSeconds: commandTimeout.value,
    confirmHighRisk
  })
  await loadCommandHistory()
}

async function runCommand() {
  if (!commandInput.value.trim()) {
    ElMessage.warning('请输入 gcloud 后的参数')
    return
  }
  commandLoading.value = true
  try {
    await executeCommand(false)
    ElMessage.success(commandResult.value?.success ? '命令执行成功' : '命令执行完成，退出码非 0')
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    if (error?.response?.status === 409 && detail?.code === 'COMMAND_CONFIRMATION_REQUIRED') {
      try {
        await ElMessageBox.confirm(detail.message || '命令可能修改云资源，是否继续？', '确认高风险命令', {
          type: 'warning', confirmButtonText: '确认执行', cancelButtonText: '取消'
        })
        await executeCommand(true)
        ElMessage.success(commandResult.value?.success ? '命令执行成功' : '命令执行完成，退出码非 0')
      } catch (confirmError: any) {
        if (confirmError !== 'cancel' && confirmError !== 'close') {
          ElMessage.error(confirmError?.response?.data?.detail || '命令执行失败')
        }
      }
    } else {
      ElMessage.error(detail?.message || detail || '命令执行失败')
    }
  } finally {
    commandLoading.value = false
  }
}

function clearCommandOutput() {
  commandResult.value = undefined
}

function returnToList() {
  const previousPath = window.history.state?.back
  if (typeof previousPath === 'string' && /^\/gcp-envs(?:\?|$)/.test(previousPath)) {
    router.back()
    return
  }
  router.push('/gcp-envs')
}

function envStatusType(status: string) {
  if (status === 'AUTHORIZED') return 'success'
  if (status === 'FAILED' || status === 'EXPIRED' || status === 'CANCELLED') return 'danger'
  if (status === 'WAITING_USER_CODE' || status === 'AUTHORIZING') return 'warning'
  return 'info'
}

function taskStatusType(status?: PresetTaskResponse['status']) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'RUNNING' || status === 'PENDING') return 'warning'
  return 'info'
}

function channelStatusType(status: GcpEnvironmentResponse['channelPushStatus']) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED') return 'danger'
  return 'warning'
}

function taskStepText(step?: string) {
  if (!step) return '-'
  if (step === 'WAIT_KEY_POLICY_PROPAGATION') return '等待 Key 权限生效（约 3 分钟）'
  if (step === 'GRANT_AGENT_PLATFORM_USER_ROLE') return '授予 Agent Platform User 权限'
  if (step === 'WAIT_AGENT_PLATFORM_IAM_PROPAGATION') return '等待 Agent Platform IAM 权限生效'
  return step
}

function schedulePoll() {
  if (pollTimer) window.clearTimeout(pollTimer)
  if (task.value?.status !== 'PENDING' && task.value?.status !== 'RUNNING') return
  pollTimer = window.setTimeout(() => void load(true), 2500)
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const [environment, latestTask] = await Promise.all([
      getGcpEnv(props.envId),
      getLatestBootstrapTask(props.envId)
    ])
    env.value = environment
    task.value = latestTask || undefined
    loadError.value = ''
    schedulePoll()
    if (!silent && auth.hasPermission('task:run')) void loadBillingAccounts()
  } catch (error: any) {
    loadError.value = error?.response?.data?.detail || '加载环境详情失败'
    if (silent) ElMessage.error(loadError.value)
  } finally {
    if (!silent) loading.value = false
  }
}

async function copyValue(value: string, message: string) {
  try {
    await copyTextToClipboard(value)
    ElMessage.success(message)
  } catch {
    ElMessage.error('复制失败')
  }
}

async function downloadKey() {
  if (!task.value) return
  try {
    await downloadServiceAccountKey(props.envId, task.value.taskId, task.value.keyFileName)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '下载 JSON 失败')
  }
}

onMounted(() => {
  void load()
  if (auth.hasPermission('gcloud:execute')) void loadCommandHistory()
})
onBeforeUnmount(() => {
  if (pollTimer) window.clearTimeout(pollTimer)
})
</script>

<style scoped>
.detail-page { padding-bottom: 32px; }
.detail-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 22px; }
.back-link { margin-bottom: 8px; padding: 0; }
.title-row { display: flex; align-items: center; gap: 12px; }
.title-row h1 { margin: 0; }
.detail-header p { margin: 6px 0 0; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 16px; }
.summary-card :deep(.el-card__body) { display: grid; align-content: center; gap: 8px; min-height: 130px; }
.summary-card strong { overflow: hidden; color: #f4f7fb; font-size: 17px; text-overflow: ellipsis; white-space: nowrap; }
.eyebrow { color: var(--app-text-muted); font-size: 12px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }
.account-card { border-top: 2px solid #60a5fa; }
.project-card { border-top: 2px solid #5eead4; }
.task-card { border-top: 2px solid #a78bfa; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.task-detail-card, .command-console-card, .billing-card { margin-top: 16px; }
.billing-alert { margin-bottom: 14px; }
.billing-controls, .billing-start-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.billing-select { flex: 1 1 420px; min-width: 240px; }
.selected-billing { margin: 12px 0 0; }
.billing-start-row { justify-content: space-between; margin-top: 16px; padding-top: 14px; border-top: 1px solid rgba(58, 76, 110, .55); }
.command-alert { margin-bottom: 14px; }
.command-presets, .command-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.command-presets { margin-bottom: 12px; }
.command-input { margin-bottom: 12px; }
.timeout-select { width: 140px; }
.command-result-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 20px; }
.terminal-label, .history-title { margin: 14px 0 6px; color: var(--app-text-muted); font-size: 12px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
.stderr-label { color: var(--el-color-danger); }
.command-output { max-height: 360px; margin: 0; overflow: auto; padding: 14px; border: 1px solid rgba(58, 76, 110, .7); border-radius: 8px; background: #050914; color: #dbeafe; font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
.command-error-output { color: #fecaca; }
.section-title { color: #f4f7fb; font-size: 15px; font-weight: 650; }
.card-header-row, .copy-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; }
.task-error { margin-bottom: 16px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12.5px; }
.break-all { min-width: 0; word-break: break-all; }
@media (max-width: 900px) {
  .summary-grid, .detail-grid { grid-template-columns: 1fr; }
  .detail-header { align-items: flex-start; }
  .task-descriptions :deep(.el-descriptions__body) { overflow-x: auto; }
}
</style>
