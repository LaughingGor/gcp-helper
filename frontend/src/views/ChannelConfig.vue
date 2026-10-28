<template>
  <div class="page config-page">
    <div class="header-row"><div><h1>默认偏好</h1><p class="muted">偏好只用于预填新任务和后台选择，不会改变已创建任务、渠道或历史消耗。</p></div></div>
    <el-card shadow="never" v-loading="loading">
      <el-form label-position="top" class="config-form">
        <el-form-item label="默认 VM Zone"><el-input v-model="zone" placeholder="us-west2-a" /></el-form-item>
        <el-form-item label="new-api 后台">
          <el-select v-if="auth.user?.role === 'ADMIN'" v-model="selectedTarget" clearable placeholder="不指定，由每次推送选择" style="width: 100%">
            <el-option v-for="target in targets" :key="target.id" :value="target.id" :label="target.name" />
          </el-select>
          <span v-else>{{ targets[0]?.name || '尚未绑定可用后台，请联系管理员' }}</span>
        </el-form-item>
        <el-alert title="后台地址、管理 Token、模型和 Vertex 区域由管理员在“new-api 渠道管理”统一维护。VM Zone 与 Vertex 区域相互独立。" type="info" :closable="false" />
        <el-form-item style="margin-top: 20px"><el-button type="primary" :loading="saving" @click="save">保存默认偏好</el-button></el-form-item>
      </el-form>
    </el-card>
  </div>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getAvailableTargets, type AvailableTarget } from '../api/channelUploads'
import { getUserSettings, updateUserSettings } from '../api/userSettings'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore()
const loading = ref(false), saving = ref(false), zone = ref(''), selectedTarget = ref(''), targets = ref<AvailableTarget[]>([])
async function load() {
  loading.value = true
  try { const [settings, items] = await Promise.all([getUserSettings(), getAvailableTargets()]); zone.value = settings.computeVmZone; targets.value = items; selectedTarget.value = items.some(t => t.id === settings.defaultTargetId) ? settings.defaultTargetId! : '' }
  catch { ElMessage.error('加载默认偏好失败') }
  finally { loading.value = false }
}
async function save() {
  saving.value = true
  try { await updateUserSettings(zone.value.trim(), auth.user?.role === 'ADMIN' ? selectedTarget.value || null : undefined); ElMessage.success('默认偏好已保存；已提交任务保持原配置') }
  catch (error: any) { ElMessage.error(typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : '请检查 Zone 格式和后台选择') }
  finally { saving.value = false }
}
onMounted(load)
</script>
<style scoped>
.config-page { max-width: 900px; }
.config-form { max-width: 680px; }
</style>
