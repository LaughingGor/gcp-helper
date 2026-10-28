<template>
  <div class="page task-page">
    <div class="header-row">
      <div>
        <h1>后台任务</h1>
        <p class="muted">自动预设任务的执行记录与资源结果。</p>
      </div>
      <div class="toolbar">
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
        <el-select v-model="statusFilter" clearable placeholder="全部状态" class="status-select" @change="handleStatusChange">
          <el-option label="PENDING" value="PENDING" />
          <el-option label="RUNNING" value="RUNNING" />
          <el-option label="SUCCESS" value="SUCCESS" />
          <el-option label="FAILED" value="FAILED" />
        </el-select>
        <el-button :loading="loading" @click="loadTasks">刷新</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <div class="pagination-row">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="tasks.length"
          layout="total, sizes, prev, pager, next"
        />
      </div>
      <el-table v-loading="loading" :data="paginatedTasks" :empty-text="selectedDateRange ? '所选日期范围暂无任务记录' : '暂无任务记录'">
        <el-table-column label="创建时间" min-width="170">
          <template #default="{ row }">{{ formatTime(row.createdAt) }}</template>
        </el-table-column>
        <el-table-column label="环境" min-width="220">
          <template #default="{ row }">
            <div class="stack-cell">
              <span>{{ row.envName || '-' }}</span>
              <span class="muted small-text">{{ row.googleAccountHint || row.envId }}</span>
              <span v-if="auth.user?.role === 'ADMIN'" class="owner-text">创建用户：{{ row.createdBy || '-' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="taskTagType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="步骤" min-width="180">
          <template #default="{ row }">
            <span>{{ taskStepText(row.currentStep) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Project" min-width="210">
          <template #default="{ row }">
            <span class="mono-text">{{ row.projectId || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="VM" min-width="230">
          <template #default="{ row }">
            <div v-if="row.vmName" class="stack-cell">
              <span>{{ row.vmName }}</span>
              <span class="muted small-text">{{ row.vmZone || '-' }} / {{ row.vmExternalIp || '-' }}</span>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="SOCKS5" min-width="360">
          <template #default="{ row }">
            <div v-if="row.socks5Url" class="inline-cell">
              <span class="mono-text ellipsis">{{ row.socks5Url }}</span>
              <el-button link type="primary" @click="copyText(row.socks5Url, 'SOCKS5 链接已复制')">复制</el-button>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="JSON Key" min-width="180">
          <template #default="{ row }">
            <div class="inline-cell">
              <el-tag :type="row.keyFileExists ? 'success' : 'info'">
                {{ row.keyFileExists ? '已生成' : '未生成' }}
              </el-tag>
              <el-button
                v-if="auth.hasPermission('secret:read')"
                link
                type="success"
                :disabled="row.status !== 'SUCCESS' || !row.keyFileExists"
                @click="downloadKey(row)"
              >
                下载
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="260">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.errorMessage"
              :content="row.errorMessage"
              placement="top"
              popper-class="task-error-tooltip"
            >
              <span class="error-text ellipsis">{{ row.errorMessage }}</span>
            </el-tooltip>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="完成时间" min-width="170">
          <template #default="{ row }">{{ formatTime(row.finishedAt) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'FAILED' && auth.hasPermission('task:run')"
              link
              type="primary"
              :loading="isRetrying(row.taskId)"
              @click="retryTask(row)"
            >
              重试
            </el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { beijingToday, formatBeijingTime } from '../utils/beijingDate'
import { useClientPagination } from '../composables/useClientPagination'
import { copyTextToClipboard } from '../utils/clipboard'
import {
  downloadServiceAccountKey,
  listPresetTasks,
  retryPresetTask,
  type PresetTaskResponse
} from '../api/gcpEnv'

const tasks = ref<PresetTaskResponse[]>([])
const { currentPage, pageSize, paginatedItems: paginatedTasks, resetPage } = useClientPagination(tasks)
const auth = useAuthStore()
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
const today = beijingToday()
const selectedDateRange = ref<DateRange | null>(
  route.query.scope === 'all' ? null : queryStart && queryEnd ? [queryStart, queryEnd] : [legacyDate || today, legacyDate || today]
)
const loading = ref(false)
const statusFilter = ref<PresetTaskResponse['status'] | ''>('')
const retryingTaskIds = ref<Record<string, boolean>>({})
let pollTimer: number | undefined

const hasActiveTask = computed(() => tasks.value.some((task) => task.status === 'PENDING' || task.status === 'RUNNING'))

function taskTagType(status: PresetTaskResponse['status']) {
  if (status === 'SUCCESS') return 'success'
  if (status === 'FAILED') return 'danger'
  if (status === 'RUNNING' || status === 'PENDING') return 'warning'
  return 'info'
}

function taskStepText(step?: string) {
  if (step === 'WAIT_KEY_POLICY_PROPAGATION') return '等待 Key 权限生效（约 3 分钟）'
  return step || '-'
}

function formatTime(value?: string) {
  return formatBeijingTime(value)
}

async function loadTasks() {
  loading.value = true
  try {
    tasks.value = await listPresetTasks({
      status: statusFilter.value || undefined,
      startDate: selectedDateRange.value?.[0],
      endDate: selectedDateRange.value?.[1],
      limit: 500
    })
    resetPolling()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '加载后台任务失败')
  } finally {
    loading.value = false
  }
}

async function pollTasks() {
  try {
    tasks.value = await listPresetTasks({
      status: statusFilter.value || undefined,
      startDate: selectedDateRange.value?.[0],
      endDate: selectedDateRange.value?.[1],
      limit: 500
    })
    resetPolling()
  } catch {
    resetPolling()
  }
}

async function handleDateChange() {
  resetPage()
  await router.replace({
    query: {
      ...route.query,
      date: undefined,
      startDate: selectedDateRange.value?.[0],
      endDate: selectedDateRange.value?.[1],
      scope: selectedDateRange.value ? undefined : 'all'
    }
  })
  await loadTasks()
}

async function handleStatusChange() {
  resetPage()
  await loadTasks()
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

function resetPolling() {
  if (pollTimer) {
    window.clearTimeout(pollTimer)
  }
  const delay = hasActiveTask.value ? 2500 : 6000
  pollTimer = window.setTimeout(() => {
    void pollTasks()
  }, delay)
}

async function copyText(text: string, message: string) {
  try {
    await copyTextToClipboard(text)
    ElMessage.success(message)
  } catch {
    ElMessage.error('复制失败')
  }
}

async function downloadKey(task: PresetTaskResponse) {
  try {
    await downloadServiceAccountKey(task.envId, task.taskId, task.keyFileName)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '下载 JSON 失败')
  }
}

function isRetrying(taskId: string) {
  return Boolean(retryingTaskIds.value[taskId])
}

async function retryTask(task: PresetTaskResponse) {
  retryingTaskIds.value = { ...retryingTaskIds.value, [task.taskId]: true }
  try {
    await retryPresetTask(task.taskId)
    statusFilter.value = ''
    resetPage()
    await loadTasks()
    ElMessage.success('重试任务已提交')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '重试任务失败')
  } finally {
    const nextRetryingTaskIds = { ...retryingTaskIds.value }
    delete nextRetryingTaskIds[task.taskId]
    retryingTaskIds.value = nextRetryingTaskIds
  }
}

onMounted(loadTasks)

onBeforeUnmount(() => {
  if (pollTimer) {
    window.clearTimeout(pollTimer)
  }
})
</script>

<style scoped>
.task-page { max-width: 1440px; }
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
h1 { margin: 0 0 8px; }
.toolbar { display: flex; align-items: center; gap: 10px; }
.status-select { width: 150px; }
.date-range-picker { width: 260px; }
.pagination-row { display: flex; justify-content: flex-end; overflow-x: auto; padding-bottom: 12px; }
.stack-cell { display: grid; gap: 2px; min-width: 0; }
.inline-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.small-text { font-size: 12px; }
.owner-text { color: var(--app-primary); font-size: 12px; }
.mono-text { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.error-text { display: block; max-width: 100%; color: #fb7185; line-height: 20px; }
:global(.task-error-tooltip) { max-width: 560px; word-break: break-word; white-space: normal; }
</style>
