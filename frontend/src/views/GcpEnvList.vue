<template>
  <div class="page">
    <div class="header-row">
      <div>
        <h1>GCP 环境</h1>
        <p class="muted">每个环境对应独立的 gcloud configuration 和配置目录。</p>
      </div>
      <div class="header-actions">
        <el-input
          v-model="targetAccountQuery"
          clearable
          placeholder="搜索目标账号"
          class="target-account-search"
          @clear="handleTargetAccountSearch"
          @keyup.enter="handleTargetAccountSearch"
        >
          <template #append><el-button @click="handleTargetAccountSearch">搜索</el-button></template>
        </el-input>
        <el-date-picker
          v-model="selectedDateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          range-separator="至"
          clearable
          class="date-range-picker"
          @change="handleDateChange"
        />
        <el-button @click="selectToday">今天</el-button>
        <el-button @click="selectAllDates">全部</el-button>
        <el-button :loading="loading" @click="load">刷新</el-button>
        <el-button v-if="auth.hasPermission('env:create')" type="primary" @click="$router.push('/gcp-envs/create')">创建环境</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <div v-if="auth.hasPermission('channel:push')" style="margin-bottom: 16px">
        <span style="margin-right: 12px">推送目标后台</span>
        <el-select v-if="canSelectTargets" v-model="targetId" :loading="targetsLoading" :disabled="batchRunning || !!pushingEnvId" placeholder="请选择本次推送后台" style="width: 280px">
          <el-option v-for="target in targets" :key="target.id" :value="target.id" :label="target.name" />
        </el-select>
        <span v-else>{{ targetLabel }}</span>
        <el-button link @click="loadTargets">刷新后台</el-button>
        <span v-if="!targets.length" class="muted">暂无可用后台，请管理员配置或确认启用旧连接。</span>
      </div>
      <div class="list-control-row">
        <div class="batch-actions">
          <span class="selected-count">已选 {{ selectedEnvs.length }} 项</span>
          <el-button
            v-if="auth.hasPermission('channel:push')"
            type="primary"
            plain
            :disabled="!batchPushableEnvs.length || batchRunning"
            :loading="batchAction === 'push'"
            @click="batchPushChannels"
          >批量推送（{{ batchPushableEnvs.length }}）</el-button>
          <el-button
            v-if="auth.hasPermission('secret:read')"
            type="success"
            plain
            :disabled="!batchDownloadableEnvs.length || batchRunning"
            :loading="batchAction === 'download'"
            @click="batchDownloadKeys"
          >批量下载（{{ batchDownloadableEnvs.length }}）</el-button>
          <el-button
            v-if="auth.hasPermission('env:delete')"
            type="danger"
            plain
            :disabled="!selectedEnvs.length || batchRunning"
            :loading="batchAction === 'cleanup'"
            @click="batchCleanupEnvs"
          >批量清理</el-button>
        </div>
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="envs.length"
          layout="total, sizes, prev, pager, next"
          @current-change="clearTableSelection"
          @size-change="clearTableSelection"
        />
      </div>
      <el-table
        ref="tableRef"
        v-loading="loading || batchRunning"
        :data="paginatedEnvs"
        row-key="envId"
        :empty-text="selectedDateRange ? '所选日期范围暂无环境' : '暂无环境'"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="48" />
        <el-table-column prop="envName" label="环境名" min-width="160">
          <template #default="{ row }">
            <el-button link type="primary" class="env-name-link" @click="openDetail(row)">
              {{ row.envName }}
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="渠道状态" min-width="190">
          <template #default="{ row }">
            <div v-if="row.channelPushStatus" class="channel-status-cell">
              <el-tooltip :content="row.channelPushMessage || channelPushLabel(row.channelPushStatus)" placement="top">
                <el-tag :type="channelPushTagType(row.channelPushStatus)">
                  {{ channelPushLabel(row.channelPushStatus) }}
                </el-tag>
              </el-tooltip>
              <span v-if="row.channelPushAction" class="muted task-step">
                {{ channelPushActionLabel(row.channelPushAction) }}
              </span>
              <span v-if="row.channelPushedAt" class="muted task-step">
                {{ formatBeijingTime(row.channelPushedAt) }}
              </span>
            </div>
            <el-tag v-else type="info">未推送</el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="auth.user?.role === 'ADMIN'" prop="createdBy" label="创建用户" min-width="130">
          <template #default="{ row }">{{ row.createdBy || '-' }}</template>
        </el-table-column>
        <el-table-column prop="googleAccountHint" label="目标账号" min-width="200" />
        <el-table-column prop="authorizedAccount" label="已授权账号" min-width="200">
          <template #default="{ row }">{{ row.authorizedAccount || '-' }}</template>
        </el-table-column>
        <el-table-column prop="defaultProjectId" label="当前激活的 Project 名" min-width="230">
          <template #default="{ row }">{{ row.defaultProjectId || '-' }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="150">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="configName" label="gcloud 配置" min-width="220" />
        <el-table-column prop="proxyEnabled" label="代理" width="90">
          <template #default="{ row }">
            <el-tag :type="row.proxyEnabled ? 'success' : 'info'">{{ row.proxyEnabled ? '启用' : '关闭' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="180">
          <template #default="{ row }">{{ formatBeijingTime(row.createdAt) }}</template>
        </el-table-column>
        <el-table-column label="预设任务" min-width="210">
          <template #default="{ row }">
            <div class="task-cell">
              <template v-if="latestTask(row.envId)">
                <el-tag :type="taskTagType(latestTask(row.envId)?.status)">
                  {{ latestTask(row.envId)?.status }}
                </el-tag>
                <span v-if="shouldShowTaskStep(latestTask(row.envId))" class="muted task-step">
                  {{ taskStepText(latestTask(row.envId)?.currentStep) }}
                </span>
              </template>
              <template v-else-if="needsBillingSelection(row)">
                <el-tag type="warning">待选择账单</el-tag>
                <el-button link type="primary" @click="openDetail(row)">去选择</el-button>
              </template>
              <span v-else class="muted">未执行</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="VM" min-width="220">
          <template #default="{ row }">
            <div v-if="latestTask(row.envId)?.vmName" class="vm-cell">
              <span>{{ latestTask(row.envId)?.vmName }}</span>
              <span class="muted task-step">{{ latestTask(row.envId)?.vmZone }}</span>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="SOCKS5" min-width="340">
          <template #default="{ row }">
            <div v-if="latestTask(row.envId)?.socks5Url" class="socks-cell">
              <span class="socks-url">{{ latestTask(row.envId)?.socks5Url }}</span>
              <el-button link type="primary" @click="copySocks5Url(row)">复制</el-button>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right" align="center">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" @click="openDetail(row)">详情</el-button>
              <el-button
                v-if="auth.hasPermission('channel:push')"
                type="primary"
                link
                :disabled="!canPushChannel(row) || deletingEnvId === row.envId || batchRunning"
                :loading="pushingEnvId === row.envId"
                @click="pushChannel(row)"
              >推送渠道</el-button>
              <ProjectReplacement v-if="auth.hasPermission('task:run') && auth.hasPermission('channel:push')"
                :env="row" :target-id="targetId" :disabled="batchRunning || isEnvActionRunning(row)" @changed="load" />
              <el-dropdown
                v-if="auth.hasPermission('secret:read') || auth.hasPermission('env:delete')"
                trigger="click"
                popper-class="env-action-menu"
                :disabled="isEnvActionRunning(row) || batchRunning"
                @command="handleMoreAction(row, $event)"
              >
                <el-button link :loading="deletingEnvId === row.envId">
                  更多<span class="dropdown-arrow">⌄</span>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item
                      v-if="auth.hasPermission('secret:read')"
                      command="download"
                      :disabled="latestTask(row.envId)?.status !== 'SUCCESS' || !latestTask(row.envId)?.keyFileExists"
                    >下载 JSON</el-dropdown-item>
                    <el-dropdown-item
                      v-if="auth.hasPermission('env:delete')"
                      command="cleanup"
                      divided
                      class="danger-menu-item"
                    >清理环境</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { beijingToday, formatBeijingTime } from '../utils/beijingDate'
import { useTargetSelection } from '../composables/useTargetSelection'
import { useClientPagination } from '../composables/useClientPagination'
import { copyTextToClipboard } from '../utils/clipboard'
import ProjectReplacement from '../components/ProjectReplacement.vue'
import {
  deleteGcpEnv,
  downloadServiceAccountKey,
  getLatestBootstrapTask,
  getGcpEnv,
  getPresetTask,
  listGcpEnvs,
  pushNewApiChannel,
  type GcpEnvironmentResponse,
  type PresetTaskResponse
} from '../api/gcpEnv'

defineOptions({ name: 'GcpEnvList' })

const envs = ref<GcpEnvironmentResponse[]>([])
const { currentPage, pageSize, paginatedItems: paginatedEnvs, resetPage } = useClientPagination(envs)
const tableRef = ref()
const selectedEnvs = ref<GcpEnvironmentResponse[]>([])
const batchAction = ref<'push' | 'download' | 'cleanup'>()
const batchRunning = computed(() => Boolean(batchAction.value))
const batchPushableEnvs = computed(() => selectedEnvs.value.filter(canPushChannel))
const batchDownloadableEnvs = computed(() => selectedEnvs.value.filter((env) => {
  const task = latestTask(env.envId)
  return task?.status === 'SUCCESS' && task.keyFileExists
}))
const auth = useAuthStore()
const { targets, targetId, targetsLoading, loadTargets, canSelectTargets, targetLabel } = useTargetSelection(auth.hasPermission('channel:push'))
const route = useRoute()
const router = useRouter()
type DateRange = [string, string]
const datePattern = /^\d{4}-\d{2}-\d{2}$/
const queryStart = typeof route.query.startDate === 'string' && datePattern.test(route.query.startDate)
  ? route.query.startDate
  : undefined
const queryEnd = typeof route.query.endDate === 'string' && datePattern.test(route.query.endDate)
  ? route.query.endDate
  : undefined
const legacyDate = typeof route.query.date === 'string' && datePattern.test(route.query.date)
  ? route.query.date
  : undefined
const routeTargetAccount = typeof route.query.googleAccount === 'string' ? route.query.googleAccount : ''
const today = beijingToday()
const selectedDateRange = ref<DateRange | null>(
  route.query.scope === 'all' ? null : queryStart && queryEnd ? [queryStart, queryEnd] : [legacyDate || today, legacyDate || today]
)
const targetAccountQuery = ref(routeTargetAccount)
const loading = ref(false)
const deletingEnvId = ref<string>()
const pushingEnvId = ref<string>()
const tasksByEnvId = ref<Record<string, PresetTaskResponse | undefined>>({})
const pollingTaskIds = new Set<string>()

function statusType(status: string) {
  if (status === 'AUTHORIZED') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'WAITING_USER_CODE' || status === 'AUTHORIZING') return 'warning'
  return 'info'
}

function latestTask(envId: string) {
  return tasksByEnvId.value[envId]
}

function needsBillingSelection(env: GcpEnvironmentResponse) {
  return env.status === 'AUTHORIZED' && !env.billingAccountId && !latestTask(env.envId)
}

function openDetail(env: GcpEnvironmentResponse) {
  router.push(`/gcp-envs/${env.envId}`)
}

function taskTagType(status?: string) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'RUNNING' || status === 'PENDING') return 'warning'
  return 'info'
}

function channelPushTagType(status: GcpEnvironmentResponse['channelPushStatus']) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED') return 'danger'
  return 'warning'
}

function channelPushLabel(status: GcpEnvironmentResponse['channelPushStatus']) {
  if (status === 'SUCCESS') return '已推送'
  if (status === 'FAILED') return '推送失败'
  return '推送中'
}

function channelPushActionLabel(action: GcpEnvironmentResponse['channelPushAction']) {
  if (action === 'CREATED') return '新建渠道'
  if (action === 'REACTIVATED') return '重新启用'
  if (action === 'ALREADY_EXISTS') return '渠道已存在'
  return ''
}

function isTaskActive(task?: PresetTaskResponse) {
  return task?.status === 'PENDING' || task?.status === 'RUNNING'
}

function shouldShowTaskStep(task?: PresetTaskResponse) {
  return Boolean(task?.currentStep && task.status !== 'SUCCESS')
}

function taskStepText(step?: string) {
  if (step === 'WAIT_KEY_POLICY_PROPAGATION') return '等待 Key 权限生效（约 3 分钟）'
  return step || '-'
}

function canPushChannel(env: GcpEnvironmentResponse) {
  const task = latestTask(env.envId)
  return env.channelPushStatus !== 'PENDING' && Boolean(task?.status === 'SUCCESS' && (task.keyFileExists || task.credentialStored) && task.socks5Url && task.socks5Url !== '-')
}

function isEnvActionRunning(env: GcpEnvironmentResponse) {
  return pushingEnvId.value === env.envId || deletingEnvId.value === env.envId
}

async function handleMoreAction(env: GcpEnvironmentResponse, command: string) {
  if (command === 'download') await downloadKey(env)
  if (command === 'cleanup') await cleanupEnv(env)
}

async function load() {
  loading.value = true
  try {
    envs.value = await listGcpEnvs({
      startDate: selectedDateRange.value?.[0],
      endDate: selectedDateRange.value?.[1],
      googleAccount: targetAccountQuery.value.trim() || undefined
    })
    await loadLatestTasks()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '加载环境失败')
  } finally {
    loading.value = false
  }
}

async function handleDateChange() {
  clearTableSelection()
  resetPage()
  await router.replace({
    query: {
      ...route.query,
      date: undefined,
      startDate: selectedDateRange.value?.[0],
      endDate: selectedDateRange.value?.[1],
      googleAccount: targetAccountQuery.value.trim() || undefined,
      scope: selectedDateRange.value ? undefined : 'all'
    }
  })
  await load()
}

async function handleTargetAccountSearch() {
  clearTableSelection()
  resetPage()
  await router.replace({
    query: {
      ...route.query,
      googleAccount: targetAccountQuery.value.trim() || undefined
    }
  })
  await load()
}

function handleSelectionChange(rows: GcpEnvironmentResponse[]) {
  selectedEnvs.value = rows
}

function clearTableSelection() {
  tableRef.value?.clearSelection()
  selectedEnvs.value = []
}

async function confirmBatch(message: string, title: string) {
  try {
    await ElMessageBox.confirm(message, title, {
      confirmButtonText: '确认执行',
      cancelButtonText: '取消',
      type: 'warning'
    })
    return true
  } catch {
    return false
  }
}

function showBatchResult(actionName: string, successCount: number, errors: string[]) {
  if (!errors.length) {
    ElMessage.success(`${actionName}完成：成功 ${successCount} 项`)
    return
  }
  ElMessage.warning(`${actionName}完成：成功 ${successCount} 项，失败 ${errors.length} 项；${errors[0]}`)
}

async function batchPushChannels() {
  if (!targetId.value) { ElMessage.warning(canSelectTargets.value ? '请先选择目标后台' : '尚未绑定可用后台，请联系管理员'); return }
  const selectedTarget = targetId.value
  const selectedTargetName = targets.value.find(t => t.id === selectedTarget)?.name || selectedTarget
  const environments = [...batchPushableEnvs.value]
  if (!environments.length || !await confirmBatch(`确认将选中的 ${environments.length} 个环境推送到后台「${selectedTargetName}」？`, '批量推送渠道')) return
  batchAction.value = 'push'
  let successCount = 0
  const errors: string[] = []
  try {
    for (const env of environments) {
      try {
        await pushNewApiChannel(env.envId, selectedTarget)
        successCount += 1
      } catch (error: any) {
        errors.push(`${env.envName}: ${error?.response?.data?.detail || '推送失败'}`)
      }
    }
    showBatchResult('批量提交推送', successCount, errors)
    clearTableSelection()
    await load()
  } finally {
    batchAction.value = undefined
  }
}

async function batchDownloadKeys() {
  const targets = [...batchDownloadableEnvs.value]
  if (!targets.length || !await confirmBatch(`确认下载选中的 ${targets.length} 个 JSON Key？`, '批量下载')) return
  batchAction.value = 'download'
  let successCount = 0
  const errors: string[] = []
  try {
    for (const env of targets) {
      const task = latestTask(env.envId)
      if (!task) continue
      try {
        await downloadServiceAccountKey(env.envId, task.taskId, task.keyFileName)
        successCount += 1
      } catch (error: any) {
        errors.push(`${env.envName}: ${error?.response?.data?.detail || '下载失败'}`)
      }
    }
    showBatchResult('批量下载', successCount, errors)
    clearTableSelection()
  } finally {
    batchAction.value = undefined
  }
}

async function batchCleanupEnvs() {
  const targets = [...selectedEnvs.value]
  if (!targets.length || !await confirmBatch(
    `确认清理选中的 ${targets.length} 个环境？本地环境及原始密钥文件将删除；已归档凭据、远程渠道和消耗记录保留，不会删除云资源。`,
    '批量清理环境'
  )) return
  batchAction.value = 'cleanup'
  let successCount = 0
  const errors: string[] = []
  try {
    for (const env of targets) {
      try {
        await deleteGcpEnv(env.envId)
        successCount += 1
      } catch (error: any) {
        errors.push(`${env.envName}: ${error?.response?.data?.detail || '清理失败'}`)
      }
    }
    showBatchResult('批量清理', successCount, errors)
    clearTableSelection()
    await load()
  } finally {
    batchAction.value = undefined
  }
}

async function selectToday() {
  const value = beijingToday()
  selectedDateRange.value = [value, value]
  await handleDateChange()
}

async function selectAllDates() {
  selectedDateRange.value = null
  await handleDateChange()
}

async function loadLatestTasks() {
  const entries = await Promise.all(
    envs.value.map(async (env) => [env.envId, await getLatestBootstrapTask(env.envId)] as const)
  )
  tasksByEnvId.value = Object.fromEntries(entries.map(([envId, task]) => [envId, task || undefined]))
  for (const [envId, task] of entries) {
    if (task && isTaskActive(task)) {
      void pollTask(envId, task.taskId)
    }
  }
}

async function pollTask(envId: string, taskId: string) {
  if (pollingTaskIds.has(taskId)) {
    return
  }
  pollingTaskIds.add(taskId)
  try {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 2000))
      const task = await getPresetTask(envId, taskId)
      tasksByEnvId.value = { ...tasksByEnvId.value, [envId]: task }
      if (task.status === 'SUCCESS') {
        ElMessage.success('预设任务执行成功，可以下载 JSON')
        return
      }
      if (task.status === 'FAILED') {
        ElMessage.error(task.errorMessage || '预设任务执行失败')
        return
      }
    }
  } finally {
    pollingTaskIds.delete(taskId)
  }
}

async function downloadKey(env: GcpEnvironmentResponse) {
  const task = latestTask(env.envId)
  if (!task || task.status !== 'SUCCESS') {
    return
  }
  try {
    await downloadServiceAccountKey(env.envId, task.taskId, task.keyFileName)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '下载 JSON 失败')
  }
}

async function copySocks5Url(env: GcpEnvironmentResponse) {
  const socks5Url = latestTask(env.envId)?.socks5Url
  if (!socks5Url) {
    return
  }
  try {
    await copyTextToClipboard(socks5Url)
    ElMessage.success('SOCKS5 链接已复制')
  } catch {
    ElMessage.error('复制失败')
  }
}

async function pushChannel(env: GcpEnvironmentResponse) {
  if (!targetId.value) { ElMessage.warning(canSelectTargets.value ? '请先选择目标后台' : '尚未绑定可用后台，请联系管理员'); return }
  const selectedTarget = targetId.value
  if (!canPushChannel(env)) {
    return
  }
  pushingEnvId.value = env.envId
  try {
    const result = await pushNewApiChannel(env.envId, selectedTarget)
    ElMessage.success(result.message || `渠道已推送：${result.channelName}`)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '推送渠道失败')
  } finally {
    pushingEnvId.value = undefined
    await load()
  }
}

async function cleanupEnv(env: GcpEnvironmentResponse) {
  try {
    await ElMessageBox.confirm(
      `确认清理环境「${env.envName}」？这会删除本地环境及原始密钥文件；已归档凭据、远程渠道和消耗记录保留，不会删除云资源。`,
      '清理环境',
      {
        confirmButtonText: '清理',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger'
      }
    )
  } catch {
    return
  }

  deletingEnvId.value = env.envId
  try {
    await deleteGcpEnv(env.envId)
    ElMessage.success('环境已清理')
    const nextTasks = { ...tasksByEnvId.value }
    delete nextTasks[env.envId]
    tasksByEnvId.value = nextTasks
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '清理环境失败')
  } finally {
    deletingEnvId.value = undefined
  }
}

let pushPoll: ReturnType<typeof setTimeout> | undefined, activePage = true
function schedulePushPoll() {
  if (!activePage || pushPoll) return
  pushPoll = setTimeout(async () => {
    pushPoll = undefined
    if (!document.hidden && !loading.value && !batchRunning.value && activePage) {
      const pending = envs.value.filter(env => env.channelPushStatus === 'PENDING')
      const results = await Promise.allSettled(pending.map(env => getGcpEnv(env.envId)))
      for (const result of results) if (result.status === 'fulfilled') {
        const index = envs.value.findIndex(env => env.envId === result.value.envId)
        if (index >= 0) Object.assign(envs.value[index], result.value)
      }
    }
    schedulePushPoll()
  }, 5000)
}
function stopPushPoll() { activePage = false; clearTimeout(pushPoll); pushPoll = undefined }
onMounted(() => { void load(); schedulePushPoll() })
onActivated(() => { activePage = true; schedulePushPoll() })
onDeactivated(stopPushPoll)
onUnmounted(stopPushPoll)
</script>

<style scoped>
.header-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.header-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 10px; }
.target-account-search { width: 260px; }
.date-range-picker { width: 260px; }
.list-control-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; overflow-x: auto; padding-bottom: 12px; }
.batch-actions { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.selected-count { min-width: 64px; color: var(--app-text-muted); font-size: 13px; }
h1 { margin: 0 0 8px; }
.task-cell { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.channel-status-cell { display: grid; justify-items: start; gap: 3px; }
.task-step { font-size: 12px; }
.vm-cell { display: grid; gap: 2px; }
.socks-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.socks-url { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.env-name-link { max-width: 100%; padding: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
.env-name-link :deep(span) { overflow: hidden; text-overflow: ellipsis; }
.row-actions { display: flex; align-items: center; justify-content: center; gap: 12px; white-space: nowrap; }
.row-actions :deep(.el-button + .el-button) { margin-left: 0; }
.dropdown-arrow { margin-left: 4px; color: var(--app-text-muted); font-size: 14px; }
:global(.env-action-menu .danger-menu-item) { color: var(--el-color-danger); }
</style>
