<template>
  <div class="page statistics-page">
    <div class="header-row">
      <div>
        <h1>new-api 渠道统计</h1>
        <p class="muted">{{ statistics ? `统计 ${statistics.group} 渠道的消费与可用额度。` : '按所选后台配置的统计分组查看消费与可用额度。' }}</p>
      </div>
      <el-select v-if="canSelectTargets" v-model="targetId" :loading="targetsLoading" :disabled="loading" placeholder="选择统计后台" style="width: 260px"><el-option v-for="target in targets" :key="target.id" :value="target.id" :label="target.name" /></el-select>
      <span v-else>{{ targetLabel }}</span>
      <el-button type="primary" :disabled="!targetId" :loading="loading" @click="load(true)">刷新实时数据</el-button>
    </div>

    <el-alert
      v-if="errorMessage"
      type="error"
      :title="errorMessage"
      show-icon
      class="page-alert"
    />
    <el-alert
      v-if="statistics?.partial"
      type="warning"
      title="渠道数据未完整读取，当前统计结果仅供参考"
      :description="statistics.warnings.join('；')"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <template v-if="statistics">
      <div class="scope-bar">
        <div><span class="scope-label">统计分组</span><strong>{{ statistics.group }}</strong></div>
        <div><span class="scope-label">单渠道额度</span><strong>{{ money(statistics.maxAmountPerChannel) }}</strong></div>
        <div><span class="scope-label">Quota 换算</span><strong>1 {{ statistics.currency }} = {{ number(statistics.quotaPerUnit) }} quota</strong></div>
        <div><span class="scope-label">更新时间</span><strong>{{ formatBeijingTime(statistics.generatedAt) }}</strong></div>
      </div>

      <el-card shadow="never" class="total-consumption-card">
        <div>
          <span class="metric-label">全部渠道已使用总量</span>
          <p class="muted">启用渠道与禁用渠道的有效消费金额合计</p>
        </div>
        <strong>{{ money(statistics.totalConsumedAmount) }}</strong>
      </el-card>

      <div class="statistics-groups">
        <section class="statistics-group enabled-group">
          <div class="statistics-group-header">
            <div>
              <h2>启用渠道统计</h2>
              <p class="muted">额度使用、剩余量及超量情况</p>
            </div>
            <el-tag type="success">{{ statistics.enabled.channelCount }} 个渠道</el-tag>
          </div>
          <div class="metric-grid">
            <el-card shadow="never" class="metric-card enabled-card">
          <span class="metric-label">启用渠道</span>
          <strong>{{ statistics.enabled.channelCount }}</strong>
          <span class="metric-note">正常 {{ statistics.enabled.withinLimitCount }} / 用尽或超量 {{ usedUpOrOverCount }}</span>
            </el-card>
            <el-card shadow="never" class="metric-card enabled-card">
          <span class="metric-label">启用渠道已使用总量</span>
          <strong>{{ money(statistics.enabled.totalConsumedAmount) }}</strong>
          <span class="metric-note">有效数据 {{ enabledValidCount }} 条，包含超量渠道</span>
            </el-card>
            <el-card shadow="never" class="metric-card enabled-card">
          <span class="metric-label">正常渠道总余量</span>
          <strong>{{ money(statistics.enabled.totalRemainingAmount) }}</strong>
          <span class="metric-note">平均 {{ optionalMoney(statistics.enabled.averageRemainingAmount) }}</span>
            </el-card>
            <el-card shadow="never" class="metric-card danger-card">
          <span class="metric-label">超量渠道</span>
          <strong>{{ statistics.enabled.overLimitCount }}</strong>
          <span class="metric-note">合计超量 {{ money(statistics.enabled.totalOverAmount) }}</span>
            </el-card>
          </div>
        </section>

        <section class="statistics-group disabled-group">
          <div class="statistics-group-header">
            <div>
              <h2>禁用渠道统计</h2>
              <p class="muted">历史消费与禁用方式分布</p>
            </div>
            <el-tag type="info">{{ statistics.disabled.channelCount }} 个渠道</el-tag>
          </div>
          <div class="metric-grid">
            <el-card shadow="never" class="metric-card disabled-card">
          <span class="metric-label">禁用渠道</span>
          <strong>{{ statistics.disabled.channelCount }}</strong>
          <span class="metric-note">手动 {{ statistics.disabled.manualDisabledCount }} / 自动 {{ statistics.disabled.autoDisabledCount }}</span>
            </el-card>
            <el-card shadow="never" class="metric-card disabled-card">
          <span class="metric-label">禁用总消费</span>
          <strong>{{ money(statistics.disabled.totalConsumedAmount) }}</strong>
          <span class="metric-note">有效数据 {{ statistics.disabled.validCount }} 条</span>
            </el-card>
            <el-card shadow="never" class="metric-card disabled-card">
          <span class="metric-label">禁用平均消费</span>
          <strong>{{ optionalMoney(statistics.disabled.averageConsumedAmount) }}</strong>
          <span class="metric-note">异常 {{ statistics.disabled.invalidCount }} 条不参与计算</span>
            </el-card>
          </div>
        </section>
      </div>

      <el-card shadow="never" class="table-card">
        <el-tabs v-model="activeTab">
          <el-tab-pane :label="`启用渠道 (${enabledChannels.length})`" name="enabled">
            <el-table :data="paginatedEnabledChannels" empty-text="暂无启用渠道" :row-class-name="enabledRowClass">
              <el-table-column prop="name" label="渠道名称" min-width="220" fixed="left" />
              <el-table-column prop="channelId" label="ID" width="90" />
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag :type="classificationType(row.classification)">{{ classificationLabel(row.classification) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="已消费（升序）" min-width="160">
                <template #default="{ row }">{{ row.usedAmount ? money(row.usedAmount) : '-' }}</template>
              </el-table-column>
              <el-table-column label="额度使用" min-width="220">
                <template #default="{ row }">
                  <el-progress
                    v-if="row.usedAmount"
                    :percentage="usagePercentage(row)"
                    :status="row.classification === 'OVER_LIMIT' ? 'exception' : row.classification === 'EXHAUSTED' ? 'warning' : undefined"
                    :stroke-width="8"
                  />
                  <span v-else class="muted">无法计算</span>
                </template>
              </el-table-column>
              <el-table-column label="剩余金额" min-width="140">
                <template #default="{ row }">
                  <strong v-if="row.remainingAmount != null" class="remaining">{{ money(row.remainingAmount) }}</strong>
                  <span v-else-if="row.classification === 'OVER_LIMIT'" class="over">不计入余量</span>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="超量金额" min-width="130">
                <template #default="{ row }">
                  <strong v-if="row.overAmount" class="over">+{{ money(row.overAmount) }}</strong>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="说明" min-width="180">
                <template #default="{ row }">{{ row.invalidReason || classificationDescription(row.classification) }}</template>
              </el-table-column>
            </el-table>
            <el-pagination
              v-if="enabledChannels.length > pageSize"
              v-model:current-page="enabledPage"
              :page-size="pageSize"
              :total="enabledChannels.length"
              layout="total, prev, pager, next"
              class="table-pagination"
            />
          </el-tab-pane>

          <el-tab-pane :label="`禁用渠道 (${disabledChannels.length})`" name="disabled">
            <el-table :data="paginatedDisabledChannels" empty-text="暂无禁用渠道">
              <el-table-column prop="name" label="渠道名称" min-width="220" fixed="left" />
              <el-table-column prop="channelId" label="ID" width="90" />
              <el-table-column label="禁用类型" width="120">
                <template #default="{ row }">
                  <el-tag :type="row.status === 3 ? 'danger' : 'info'">{{ row.statusLabel }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="消费金额" min-width="150">
                <template #default="{ row }">
                  <strong v-if="row.usedAmount">{{ money(row.usedAmount) }}</strong>
                  <el-tooltip v-else :content="row.invalidReason || '数据无效'">
                    <el-tag type="info">数据异常</el-tag>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="原始 used_quota" min-width="180">
                <template #default="{ row }"><span class="mono">{{ row.usedQuota ? number(row.usedQuota) : '-' }}</span></template>
              </el-table-column>
              <el-table-column label="分组" min-width="250">
                <template #default="{ row }">{{ row.groups.join(', ') }}</template>
              </el-table-column>
            </el-table>
            <el-pagination
              v-if="disabledChannels.length > pageSize"
              v-model:current-page="disabledPage"
              :page-size="pageSize"
              :total="disabledChannels.length"
              layout="total, prev, pager, next"
              class="table-pagination"
            />
          </el-tab-pane>
        </el-tabs>
      </el-card>

      <el-alert
        v-if="statistics.unknownStatusCount || totalInvalidCount"
        type="info"
        :title="`另有 ${statistics.unknownStatusCount} 个未知状态渠道、${totalInvalidCount} 个金额异常渠道未纳入核心计算`"
        :closable="false"
        show-icon
        class="footer-alert"
      />
    </template>

    <el-skeleton v-else-if="loading" :rows="10" animated />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useTargetSelection } from '../composables/useTargetSelection'
import { ElMessage } from 'element-plus'
import { formatBeijingTime } from '../utils/beijingDate'
import {
  getChannelStatistics,
  type ChannelClassification,
  type ChannelStatisticItem,
  type ChannelStatisticsResponse
} from '../api/channelStatistics'

const { targets, targetId, targetsLoading, canSelectTargets, targetLabel } = useTargetSelection()
const statistics = ref<ChannelStatisticsResponse>()
const loading = ref(false)
const errorMessage = ref('')
const activeTab = ref('enabled')
const pageSize = 20
const disabledPage = ref(1)
const enabledPage = ref(1)

const disabledChannels = computed(() => statistics.value?.channels.filter((item) => item.status === 2 || item.status === 3) || [])
const enabledChannels = computed(() => {
  const channels = statistics.value?.channels.filter((item) => item.status === 1) || []
  return [...channels].sort((left, right) => {
    const leftRank = usedAmountSortValue(left)
    const rightRank = usedAmountSortValue(right)
    if (leftRank !== rightRank) return leftRank - rightRank
    return left.name.localeCompare(right.name)
  })
})
const paginatedDisabledChannels = computed(() => disabledChannels.value.slice((disabledPage.value - 1) * pageSize, disabledPage.value * pageSize))
const paginatedEnabledChannels = computed(() => enabledChannels.value.slice((enabledPage.value - 1) * pageSize, enabledPage.value * pageSize))
const totalInvalidCount = computed(() => (statistics.value?.disabled.invalidCount || 0) + (statistics.value?.enabled.invalidCount || 0))
const usedUpOrOverCount = computed(() => (statistics.value?.enabled.exhaustedCount || 0) + (statistics.value?.enabled.overLimitCount || 0))
const enabledValidCount = computed(() => (statistics.value?.enabled.channelCount || 0) - (statistics.value?.enabled.invalidCount || 0))

function usedAmountSortValue(channel: ChannelStatisticItem) {
  if (channel.usedAmount != null) return Number(channel.usedAmount)
  return Number.POSITIVE_INFINITY
}

watch(() => disabledChannels.value.length, (total) => {
  disabledPage.value = Math.min(disabledPage.value, Math.max(1, Math.ceil(total / pageSize)))
})
watch(() => enabledChannels.value.length, (total) => {
  enabledPage.value = Math.min(enabledPage.value, Math.max(1, Math.ceil(total / pageSize)))
})

async function load(refresh = false) {
  if (!targetId.value) return
  const selectedTarget = targetId.value
  loading.value = true
  try {
    statistics.value = await getChannelStatistics(refresh, selectedTarget)
    errorMessage.value = ''
    if (refresh) ElMessage.success('渠道统计已刷新')
  } catch (error: any) {
    errorMessage.value = error?.response?.data?.detail || '加载渠道统计失败'
  } finally {
    loading.value = false
  }
}

function money(value: string) {
  const amount = Number(value)
  try {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: statistics.value?.currency || 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }).format(amount)
  } catch {
    return `${statistics.value?.currency || ''} ${amount.toFixed(2)}`.trim()
  }
}

function optionalMoney(value?: string | null) {
  return value === undefined || value === null ? '-' : money(value)
}

function number(value: string) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 }).format(Number(value))
}

function usagePercentage(row: ChannelStatisticItem) {
  if (!row.usedAmount || !Number(row.maxAmount)) return 0
  return Math.min(100, Math.round(Number(row.usedAmount) / Number(row.maxAmount) * 100))
}

function classificationType(value: ChannelClassification) {
  if (value === 'WITHIN_LIMIT') return 'success'
  if (value === 'EXHAUSTED') return 'warning'
  if (value === 'OVER_LIMIT') return 'danger'
  return 'info'
}

function classificationLabel(value: ChannelClassification) {
  return {
    WITHIN_LIMIT: '正常',
    EXHAUSTED: '额度用尽',
    OVER_LIMIT: '超量',
    DATA_INVALID: '数据异常',
    DISABLED: '已禁用',
    UNKNOWN_STATUS: '未知'
  }[value]
}

function classificationDescription(value: ChannelClassification) {
  if (value === 'WITHIN_LIMIT') return '参与剩余金额统计'
  if (value === 'EXHAUSTED') return '剩余金额为 0'
  if (value === 'OVER_LIMIT') return '不参与剩余金额统计'
  return '-'
}

function enabledRowClass({ row }: { row: ChannelStatisticItem }) {
  if (row.classification === 'OVER_LIMIT') return 'over-limit-row'
  if (row.classification === 'DATA_INVALID') return 'invalid-row'
  return ''
}

function changeTarget() { statistics.value = undefined; errorMessage.value = ''; void load() }
watch(targetId, () => changeTarget())
</script>

<style scoped>
.statistics-page { max-width: 1380px; }
.header-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 20px; }
.header-row h1, .header-row p { margin: 0; }
.header-row p { margin-top: 6px; }
.page-alert { margin-bottom: 16px; }
.scope-bar { display: flex; flex-wrap: wrap; gap: 12px 30px; margin-bottom: 16px; padding: 13px 16px; border: 1px solid var(--app-border-soft); border-radius: 10px; background: rgba(13, 20, 34, .72); }
.scope-bar div { display: flex; gap: 8px; }
.scope-label, .metric-label { color: var(--app-text-muted); }
.total-consumption-card { margin-bottom: 16px; border: 1px solid rgba(96, 165, 250, .45); background: linear-gradient(115deg, rgba(37, 99, 235, .18), rgba(13, 20, 34, .96) 56%); }
.total-consumption-card :deep(.el-card__body) { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 104px; }
.total-consumption-card p { margin: 4px 0 0; }
.total-consumption-card strong { color: #bfdbfe; font-size: 32px; letter-spacing: -.03em; }
.statistics-groups { display: grid; gap: 16px; margin-bottom: 16px; }
.statistics-group { padding: 16px; border: 1px solid var(--app-border-soft); border-radius: 14px; background: rgba(10, 18, 32, .54); }
.enabled-group { border-left: 3px solid #34d399; }
.disabled-group { border-left: 3px solid #94a3b8; }
.statistics-group-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.statistics-group-header h2, .statistics-group-header p { margin: 0; }
.statistics-group-header h2 { font-size: 17px; }
.statistics-group-header p { margin-top: 3px; font-size: 12px; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; }
.metric-card :deep(.el-card__body) { display: grid; gap: 5px; min-height: 120px; align-content: center; }
.metric-card strong { color: #f4f7fb; font-size: 25px; letter-spacing: -.02em; }
.metric-note { color: var(--app-text-muted); font-size: 12px; }
.disabled-card { border-top: 2px solid #94a3b8; }
.enabled-card { border-top: 2px solid #34d399; }
.danger-card { border-top: 2px solid #fb7185; }
.table-card { overflow: hidden; }
.table-pagination { justify-content: flex-end; margin-top: 16px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.remaining { color: var(--app-success); }
.over { color: var(--app-danger); }
.footer-alert { margin-top: 16px; }
:deep(.over-limit-row td.el-table__cell) { background: rgba(127, 29, 29, .13); }
:deep(.invalid-row td.el-table__cell) { opacity: .72; }
@media (max-width: 900px) {
  .metric-grid { grid-template-columns: 1fr 1fr; }
  .header-row { align-items: flex-start; }
}
@media (max-width: 600px) {
  .metric-grid { grid-template-columns: 1fr; }
  .header-row { flex-direction: column; }
  .total-consumption-card :deep(.el-card__body) { align-items: flex-start; flex-direction: column; gap: 10px; }
}
</style>
