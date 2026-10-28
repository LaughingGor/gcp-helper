<template>
  <div class="page channel-model-page">
    <div class="header-row">
      <div>
        <h1>渠道模型管理</h1>
        <p class="muted">仅管理员可批量向当前 new-api 实例的渠道添加模型。</p>
      </div>
    </div>

    <el-alert
      type="warning"
      title="执行前会重新读取渠道数据，仅修改 models 字段；已有该模型的渠道会自动跳过。"
      :closable="false"
      show-icon
      class="page-alert"
    />

    <el-card shadow="never" class="settings-card">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px" class="filter-form" @submit.prevent>
        <el-form-item label="目标后台"><el-select v-model="targetId" :loading="targetsLoading" :disabled="previewing || applying" placeholder="选择后台"><el-option v-for="target in targets" :key="target.id" :value="target.id" :label="target.name" /></el-select></el-form-item>
        <el-form-item label="渠道分组" prop="group">
          <el-input v-model="form.group" maxlength="128" placeholder="Gemini_Vertex_Pro" />
        </el-form-item>
        <el-form-item label="渠道状态" prop="status">
          <el-select v-model="form.status">
            <el-option label="启用渠道" value="enabled" />
            <el-option label="禁用渠道" value="disabled" />
            <el-option label="全部渠道" value="all" />
          </el-select>
        </el-form-item>
        <el-form-item label="添加模型" prop="model">
          <el-input v-model="form.model" maxlength="256" placeholder="gemini-3.1-pro-preview" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="previewing" @click="preview">预览变更</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="previewResult">
      <el-card shadow="never" class="settings-card">
        <div class="summary-row">
          <div>
            <h2>预览结果</h2>
            <p class="muted">共查询 {{ previewResult.totalChannels }} 个渠道，{{ previewResult.alreadyPresentCount }} 个已包含模型。</p>
          </div>
          <el-button
            type="danger"
            :disabled="previewResult.candidateCount === 0"
            :loading="applying"
            @click="apply"
          >
            确认向 {{ previewResult.candidateCount }} 个渠道添加模型
          </el-button>
        </div>
        <el-alert
          :type="previewResult.candidateCount ? 'info' : 'success'"
          :title="previewResult.candidateCount ? `以下渠道将添加 ${previewResult.model}` : '所有匹配渠道均已包含该模型'"
          :closable="false"
          show-icon
          class="preview-alert"
        />
        <el-table :data="previewResult.candidates" max-height="460" empty-text="没有需要更新的渠道">
          <el-table-column prop="channelId" label="渠道 ID" width="110" />
          <el-table-column prop="name" label="渠道名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="当前 models" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">{{ row.currentModels || '（空）' }}</template>
          </el-table-column>
          <el-table-column prop="updatedModels" label="更新后 models" min-width="300" show-overflow-tooltip />
        </el-table>
      </el-card>
    </template>

    <template v-if="applyResult">
      <el-card shadow="never" class="settings-card">
        <h2>执行结果</h2>
        <p class="muted">
          成功 {{ applyResult.successCount }}，跳过 {{ applyResult.skippedCount }}，失败 {{ applyResult.failedCount }}。
        </p>
        <el-table :data="applyResult.results" max-height="460">
          <el-table-column prop="channelId" label="渠道 ID" width="110" />
          <el-table-column prop="name" label="渠道名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag :type="resultTagType(row.status)">{{ resultLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="message" label="说明" min-width="300" show-overflow-tooltip />
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useTargetSelection } from '../composables/useTargetSelection'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import {
  addChannelModel,
  previewChannelModel,
  type ChannelModelApplyResponse,
  type ChannelModelPreviewResponse,
  type ChannelModelStatusFilter,
  type ChannelModelUpdateStatus
} from '../api/channelModels'

const { targets, targetId, targetsLoading } = useTargetSelection()
const formRef = ref<FormInstance>()
const previewing = ref(false)
const applying = ref(false)
const previewResult = ref<ChannelModelPreviewResponse>()
const applyResult = ref<ChannelModelApplyResponse>()
let previewSignature = ''
const form = reactive<{ group: string; status: ChannelModelStatusFilter; model: string }>({
  group: 'Gemini_Vertex_Pro',
  status: 'enabled',
  model: ''
})

const rules: FormRules = {
  group: [{ required: true, message: '请输入渠道分组', trigger: 'blur' }],
  model: [
    { required: true, message: '请输入模型名称', trigger: 'blur' },
    { pattern: /^\S+$/, message: '模型名称不能包含空白字符', trigger: 'blur' },
    { pattern: /^[^,]+$/, message: '一次只能添加一个模型', trigger: 'blur' }
  ]
}

watch([targetId, () => form.group, () => form.status, () => form.model], () => { previewResult.value = undefined; applyResult.value = undefined })
watch(targetId, () => { form.group = targets.value.find(t => t.id === targetId.value)?.group || form.group })

function payload() {
  return { targetId: targetId.value, group: form.group.trim(), status: form.status, model: form.model.trim() }
}

async function preview() {
  if (!targetId.value) { ElMessage.warning('请选择目标后台'); return }
  await formRef.value?.validate()
  previewing.value = true
  previewResult.value = undefined
  applyResult.value = undefined
  const requested = payload()
  try {
    const result = await previewChannelModel(requested)
    if (JSON.stringify(requested) !== JSON.stringify(payload())) { ElMessage.warning('配置已变化，请重新预览'); return }
    previewSignature = JSON.stringify(requested)
    previewResult.value = result
    ElMessage.success(`预览完成：${previewResult.value.candidateCount} 个渠道待更新`)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '读取渠道列表失败')
  } finally {
    previewing.value = false
  }
}

async function apply() {
  if (!previewResult.value) return
  const confirmedPayload = payload()
  if (previewSignature !== JSON.stringify(confirmedPayload)) { ElMessage.warning('请先预览当前配置'); return }
  try {
    await ElMessageBox.confirm(
      `将向 ${previewResult.value.candidateCount} 个渠道添加模型 ${previewResult.value.model}。执行前会重新读取远端数据。`,
      '确认批量更新',
      { confirmButtonText: '确认执行', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }

  if (JSON.stringify(confirmedPayload) !== JSON.stringify(payload())) { ElMessage.warning('配置已变化，请重新预览'); return }
  applying.value = true
  applyResult.value = undefined
  try {
    applyResult.value = await addChannelModel(confirmedPayload)
    const result = applyResult.value
    if (result.failedCount) {
      ElMessage.warning(`执行完成：成功 ${result.successCount}，失败 ${result.failedCount}`)
    } else {
      ElMessage.success(`执行完成：成功更新 ${result.successCount} 个渠道`)
    }
    previewResult.value = await previewChannelModel(payload())
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '批量添加模型失败')
  } finally {
    applying.value = false
  }
}

function resultTagType(status: ChannelModelUpdateStatus) {
  return status === 'SUCCESS' ? 'success' : status === 'SKIPPED' ? 'info' : 'danger'
}

function resultLabel(status: ChannelModelUpdateStatus) {
  return status === 'SUCCESS' ? '成功' : status === 'SKIPPED' ? '跳过' : '失败'
}
</script>

<style scoped>
.channel-model-page { max-width: 1280px; }
.header-row, .summary-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
.header-row { margin-bottom: 20px; }
h1, h2 { margin: 0 0 8px; }
.settings-card { margin-bottom: 18px; }
.filter-form { max-width: 720px; }
.preview-alert { margin-bottom: 18px; }
@media (max-width: 700px) {
  .header-row, .summary-row { align-items: flex-start; flex-direction: column; }
}
</style>
