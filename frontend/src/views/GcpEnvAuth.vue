<template>
  <div class="page">
    <h1>Google 授权</h1>
    <p class="muted">请在浏览器打开授权链接，登录后把验证码粘贴回来。授权码只提交给后端，不会保存。</p>

    <div class="card">
      <el-descriptions :column="1" border>
        <el-descriptions-item label="环境 ID">{{ envId }}</el-descriptions-item>
        <el-descriptions-item label="登录会话">{{ loginSessionId }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag>{{ session?.status || 'LOADING' }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="过期时间">{{ session?.expiresAt ? new Date(session.expiresAt).toLocaleString() : '-' }}</el-descriptions-item>
      </el-descriptions>

      <el-divider content-position="left">授权链接</el-divider>
      <div v-if="session?.authUrl" class="auth-area">
        <div class="code-box">{{ session.authUrl }}</div>
        <el-button type="primary" @click="openAuthUrl">打开 Google 授权链接</el-button>
      </div>
      <el-alert v-else type="warning" title="还没有拿到授权链接，请稍后刷新。" show-icon />

      <el-divider content-position="left">提交授权码</el-divider>
      <el-form label-width="100px">
        <el-form-item label="授权码">
          <el-input
            v-model="code"
            type="password"
            show-password
            autocomplete="off"
            placeholder="粘贴 Google 页面返回的 verification code"
          />
        </el-form-item>
        <el-form-item>
          <div class="actions">
            <el-button
              type="success"
              :loading="submitting"
              :disabled="!code || session?.status !== 'WAITING_CODE'"
              @click="submitCode"
            >提交授权码</el-button>
            <el-button :loading="cancelling" :disabled="!canCancelSession" @click="cancel">取消登录</el-button>
            <el-button @click="load">刷新状态</el-button>
          </div>
        </el-form-item>
      </el-form>

      <el-alert v-if="session?.errorMessage" type="error" :title="session.errorMessage" show-icon />
      <div v-if="canRetrySession" class="retry-area">
        <el-button type="primary" :loading="retrying" @click="retry">再次授权</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { cancelLogin, getLoginSession, retryLoginSession, submitLoginCode, type LoginSessionResponse } from '../api/gcpEnv'

const props = defineProps<{ envId: string; loginSessionId: string }>()
const router = useRouter()
const session = ref<LoginSessionResponse>()
const code = ref('')
const submitting = ref(false)
const cancelling = ref(false)
const retrying = ref(false)
let timer: number | undefined
let successHandled = false
const canCancelSession = computed(() => {
  const status = session.value?.status
  return status === 'PENDING' || status === 'PROCESS_STARTED' || status === 'AUTH_URL_READY' || status === 'WAITING_CODE'
})
const canRetrySession = computed(() => {
  const status = session.value?.status
  return status === 'FAILED' || status === 'TIMEOUT' || status === 'CANCELLED'
})

async function load() {
  try {
    session.value = await getLoginSession(props.loginSessionId)
    if (session.value.status === 'SUCCESS' && !successHandled) {
      successHandled = true
      ElMessage.success('授权成功，预设任务已自动开始')
      router.push('/gcp-envs')
    }
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '加载登录会话失败')
  }
}

function openAuthUrl() {
  if (session.value?.authUrl) window.open(session.value.authUrl, '_blank', 'noopener,noreferrer')
}

async function submitCode() {
  submitting.value = true
  try {
    await submitLoginCode(props.loginSessionId, code.value)
    code.value = ''
    ElMessage.success('授权码已提交，正在后台验证')
    await load()
  } catch (error: any) {
    code.value = ''
    ElMessage.error(error?.response?.data?.detail || '提交授权码失败')
    await load()
  } finally {
    submitting.value = false
  }
}

async function cancel() {
  cancelling.value = true
  try {
    await cancelLogin(props.loginSessionId)
    ElMessage.success('已取消登录')
    router.push('/gcp-envs')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '取消失败')
  } finally {
    cancelling.value = false
  }
}

async function retry() {
  if (retrying.value) return
  retrying.value = true
  try {
    const nextSession = await retryLoginSession(props.loginSessionId)
    session.value = nextSession
    ElMessage.success('已重新发起授权')
    router.replace(`/gcp-envs/${nextSession.envId}/auth/${nextSession.loginSessionId}`)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '再次授权失败')
  } finally {
    retrying.value = false
  }
}

onMounted(() => {
  load()
  timer = window.setInterval(load, 5000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<style scoped>
h1 { margin-bottom: 8px; }
.card { margin-top: 20px; }
.retry-area { display: flex; align-items: center; gap: 12px; margin-top: 16px; }
.auth-area { display: grid; gap: 12px; }
</style>
