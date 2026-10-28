<template>
  <div class="page">
    <div class="header-row"><h1>new-api 渠道管理</h1><el-button :loading="loading" @click="load">刷新</el-button></div>
        <div class="target-heading"><p class="muted">配置后台地址、Access Token 和统计分组，其余参数沿用默认模板及系统配置。地址创建后不可修改。</p><el-button type="primary" @click="editTarget()">新增后台</el-button></div>
        <el-card shadow="never"><el-table :data="targets" empty-text="先新增一个 new-api 后台，再推送凭据">
          <el-table-column prop="baseUrl" label="地址" min-width="240"><template #default="{ row }"><el-button link type="primary" :aria-label="`编辑后台 ${row.baseUrl}`" @click="editTarget(row)">{{ row.baseUrl }}</el-button></template></el-table-column>
          <el-table-column label="状态"><template #default="{ row }">{{ row.enabled ? '启用' : '停用' }}</template></el-table-column>
          <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="danger" :loading="deletingTargetId === row.id" :disabled="!!deletingTargetId || savingTarget" @click="removeTarget(row)">删除</el-button></template></el-table-column>
        </el-table></el-card>
    <el-dialog v-model="targetDialog" :title="editingId ? '编辑后台' : '新增后台'" width="min(660px, 95vw)" :close-on-click-modal="false" :show-close="!savingTarget" :close-on-press-escape="!savingTarget" @closed="targetForm.token = ''">
      <el-form label-position="top" :disabled="savingTarget">
        <el-form-item label="后台地址"><el-input v-model="targetForm.baseUrl" :disabled="!!editingId" placeholder="https://new-api.example.com" /></el-form-item>
        <el-form-item label="管理 Access Token"><el-input v-model="targetForm.token" type="password" show-password autocomplete="new-password" :placeholder="editingId ? '留空保持原 Token' : '填写此后台的管理 Token'" /></el-form-item>
        <el-form-item label="统计分组"><el-input v-model="targetForm.statsGroup" maxlength="128" placeholder="留空沿用当前统计分组或默认模板分组" /><span class="muted">渠道统计按此分组筛选，不改变渠道推送分组。</span></el-form-item>
        <p class="muted">保存前会验证 Access Token 的渠道查看权限；验证失败时保留原配置。</p>
      </el-form>
      <template #footer><el-button :disabled="savingTarget" @click="targetDialog = false">取消</el-button><el-button type="primary" :loading="savingTarget" @click="persistTarget">{{ savingTarget ? '验证并保存中' : '保存后台' }}</el-button></template>
    </el-dialog>
  </div>
</template>
<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deleteTarget, getTargets, saveTarget, type NewApiTarget, type TargetInput } from '../api/channelUploads'
const targets = ref<NewApiTarget[]>([]), loading = ref(false)
const targetDialog = ref(false), editingId = ref(''), savingTarget = ref(false), deletingTargetId = ref('')
const targetForm = reactive<TargetInput>({ baseUrl: '', token: '', statsGroup: '' })
function errorMessage(error: any) { const detail = error?.response?.data?.detail; ElMessage.error(typeof detail === 'string' ? detail : '操作失败，请检查输入和后台配置') }
async function load() {
  loading.value = true
  try { targets.value = await getTargets() } catch (error) { errorMessage(error) }
  finally { loading.value = false }
}
function editTarget(target?: NewApiTarget) {
  editingId.value = target?.id || ''
  Object.assign(targetForm, { baseUrl: target?.baseUrl || '', token: '', statsGroup: target?.statsGroup || '' })
  targetDialog.value = true
}
async function removeTarget(target: NewApiTarget) {
  try {
    await ElMessageBox.confirm(`确认删除后台 ${target.baseUrl} 的本地配置？已有关联推送或历史配置的后台无法删除。删除后会清除用户的默认后台选择和绑定，不影响远端渠道。`, '删除后台', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
    })
  } catch { return }
  deletingTargetId.value = target.id
  try {
    await deleteTarget(target.id)
    if (editingId.value === target.id) targetDialog.value = false
    ElMessage.success('后台配置已删除')
    await load()
  } catch (error) { errorMessage(error) }
  finally { deletingTargetId.value = '' }
}
async function persistTarget() {
  savingTarget.value = true
  try { const input = { ...targetForm }; if (!input.token) delete input.token; input.statsGroup = input.statsGroup?.trim() || undefined; await saveTarget(input, editingId.value || undefined); targetForm.token = ''; targetDialog.value = false; ElMessage.success('后台配置已保存'); await load() }
  catch (error) { errorMessage(error) }
  finally { savingTarget.value = false }
}
onMounted(load)
onUnmounted(() => { targetForm.token = '' })
</script>
<style scoped>
.target-heading, .header-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
</style>
