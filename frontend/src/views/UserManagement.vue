<template>
  <div class="page user-page">
    <div class="header-row">
      <div>
        <h1>用户管理</h1>
        <p class="muted">管理系统账号、角色和登录状态。</p>
      </div>
      <el-button type="primary" @click="openCreateDialog">创建用户</el-button>
    </div>

    <el-card shadow="never">
      <div class="pagination-row">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50, 100]"
          :total="users.length"
          layout="total, sizes, prev, pager, next"
        />
      </div>
      <el-table v-loading="loading" :data="paginatedUsers" empty-text="暂无用户">
        <el-table-column prop="username" label="用户名" min-width="180" />
        <el-table-column prop="email" label="邮箱" min-width="220">
          <template #default="{ row }">{{ row.email || '-' }}</template>
        </el-table-column>
        <el-table-column prop="role" label="角色" width="140">
          <template #default="{ row }"><el-tag>{{ row.role === 'CHANNEL_USER' ? '渠道用户' : row.role }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="row.status === 'ACTIVE' ? 'success' : 'danger'">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" min-width="180">
          <template #default="{ row }">{{ formatTime(row.lastLoginAt) }}</template>
        </el-table-column>
        <el-table-column label="绑定后台" min-width="220"><template #default="{ row }">{{ targets.find(t => t.id === row.boundTargetId)?.name || (row.boundTargetId ? '后台已不可用' : '未绑定') }}</template></el-table-column>
        <el-table-column label="操作" width="360" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openBinding(row)">绑定后台</el-button>
            <el-select
              :model-value="row.role"
              size="small"
              class="role-select"
              :disabled="row.id === auth.user?.id"
              @change="changeRole(row, $event)"
            >
              <el-option label="ADMIN" value="ADMIN" />
              <el-option label="OPERATOR" value="OPERATOR" />
              <el-option label="渠道用户" value="CHANNEL_USER" />
            </el-select>
            <el-button link type="primary" @click="resetPassword(row)">重置密码</el-button>
            <el-button
              link
              :type="row.status === 'ACTIVE' ? 'danger' : 'success'"
              :disabled="row.id === auth.user?.id"
              @click="toggleStatus(row)"
            >{{ row.status === 'ACTIVE' ? '禁用' : '启用' }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="bindingDialog" title="绑定 new-api 后台" width="520px" :close-on-click-modal="false" :show-close="!bindingSaving" :close-on-press-escape="!bindingSaving">
      <p>{{ bindingUser?.username }}：普通用户的渠道推送和统计将使用此后台；管理员仍可自行选择。</p>
      <el-select v-model="bindingTargetId" clearable :disabled="bindingSaving" placeholder="未绑定，普通用户将无法推送或查看渠道统计" class="full-width"><el-option v-for="target in targets" :key="target.id" :value="target.id" :disabled="!target.enabled" :label="target.enabled ? target.name : `${target.name}（已停用）`" /></el-select>
      <template #footer><el-button :disabled="bindingSaving" @click="bindingDialog = false">取消</el-button><el-button type="primary" :loading="bindingSaving" @click="saveBinding">保存绑定</el-button></template>
    </el-dialog>
    <el-dialog v-model="dialogVisible" title="创建用户" width="480px">
      <el-form ref="createFormRef" :model="createForm" :rules="rules" label-width="90px">
        <el-form-item label="用户名" prop="username"><el-input v-model="createForm.username" /></el-form-item>
        <el-form-item label="邮箱" prop="email"><el-input v-model="createForm.email" /></el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="createForm.role" class="full-width">
            <el-option label="ADMIN" value="ADMIN" />
            <el-option label="OPERATOR" value="OPERATOR" />
            <el-option label="渠道用户" value="CHANNEL_USER" />
          </el-select>
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="createForm.password" type="password" show-password autocomplete="new-password" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import {
  bindUserTarget,
  createUser,
  listUsers,
  updateUser,
  type CurrentUser,
  type UserRole
} from '../api/auth'
import { useAuthStore } from '../stores/auth'
import { useClientPagination } from '../composables/useClientPagination'
import { getTargets, type NewApiTarget } from '../api/channelUploads'

const auth = useAuthStore()
const users = ref<CurrentUser[]>([])
const targets = ref<NewApiTarget[]>([])
const bindingDialog = ref(false), bindingSaving = ref(false), bindingUser = ref<CurrentUser>(), bindingTargetId = ref('')
async function openBinding(user: CurrentUser) {
  try { targets.value = await getTargets(); bindingUser.value = user; bindingTargetId.value = user.boundTargetId || ''; bindingDialog.value = true }
  catch { ElMessage.error('加载后台失败') }
}
async function saveBinding() {
  if (!bindingUser.value) return
  bindingSaving.value = true
  try { await bindUserTarget(bindingUser.value.id, bindingTargetId.value || null); bindingDialog.value = false; ElMessage.success('绑定已更新'); await load() }
  catch (error: any) { ElMessage.error(error?.response?.data?.detail || '绑定失败') }
  finally { bindingSaving.value = false }
}
const { currentPage, pageSize, paginatedItems: paginatedUsers } = useClientPagination(users)
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ username: '', email: '', password: '', role: 'OPERATOR' as UserRole })
const rules: FormRules = {
  username: [{ required: true, min: 3, message: '用户名至少 3 位', trigger: 'blur' }],
  password: [{ required: true, min: 10, message: '密码至少 10 位', trigger: 'blur' }],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}

async function load() {
  loading.value = true
  try { [users.value, targets.value] = await Promise.all([listUsers(), getTargets()]) }
  catch (error: any) { ElMessage.error(error?.response?.data?.detail || '加载用户失败') }
  finally { loading.value = false }
}

function openCreateDialog() {
  Object.assign(createForm, { username: '', email: '', password: '', role: 'OPERATOR' })
  dialogVisible.value = true
}

async function submitCreate() {
  await createFormRef.value?.validate()
  saving.value = true
  try {
    await createUser({
      username: createForm.username.trim(),
      email: createForm.email.trim() || undefined,
      password: createForm.password,
      role: createForm.role
    })
    createForm.password = ''
    dialogVisible.value = false
    ElMessage.success('用户创建成功')
    await load()
  } catch (error: any) {
    createForm.password = ''
    ElMessage.error(error?.response?.data?.detail || '创建用户失败')
  } finally { saving.value = false }
}

async function changeRole(user: CurrentUser, role: UserRole) {
  try {
    await updateUser(user.id, { role })
    ElMessage.success('角色已更新')
    await load()
  } catch (error: any) { ElMessage.error(error?.response?.data?.detail || '更新角色失败') }
}

async function toggleStatus(user: CurrentUser) {
  const status = user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    await updateUser(user.id, { status })
    ElMessage.success(status === 'ACTIVE' ? '用户已启用' : '用户已禁用')
    await load()
  } catch (error: any) { ElMessage.error(error?.response?.data?.detail || '更新用户状态失败') }
}

async function resetPassword(user: CurrentUser) {
  try {
    const { value } = await ElMessageBox.prompt(`为 ${user.username} 设置新密码`, '重置密码', {
      inputType: 'password',
      inputValidator: (password) => Boolean(password && password.length >= 10) || '密码至少 10 位',
      confirmButtonText: '重置',
      cancelButtonText: '取消'
    })
    await updateUser(user.id, { password: value })
    ElMessage.success('密码已重置，原登录会话已撤销')
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(error?.response?.data?.detail || '重置密码失败')
  }
}

function formatTime(value?: string) { return value ? new Date(value).toLocaleString() : '-' }
onMounted(load)
</script>

<style scoped>
.user-page { max-width: 1280px; }
.header-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
h1 { margin: 0 0 8px; }
.role-select { width: 120px; margin-right: 8px; }
.full-width { width: 100%; }
.pagination-row { display: flex; justify-content: flex-end; overflow-x: auto; padding-bottom: 12px; }
</style>
