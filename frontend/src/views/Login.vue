<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <div class="login-heading">
        <h1>GCP Helper</h1>
        <p class="muted">登录后管理渠道或 GCP 环境</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @keyup.enter="submit">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" autofocus />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password autocomplete="current-password" />
        </el-form-item>
        <el-button type="primary" class="login-button" :loading="submitting" @click="submit">登录</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ username: '', password: '' })
const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

async function submit() {
  await formRef.value?.validate()
  submitting.value = true
  try {
    await auth.login(form.username, form.password)
    form.password = ''
    const next = typeof route.query.next === 'string'
      && route.query.next.startsWith('/')
      && !route.query.next.startsWith('//')
      ? route.query.next
      : auth.homePath
    await router.replace(next)
  } catch (error: any) {
    form.password = ''
    ElMessage.error(error?.response?.data?.detail || '登录失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    radial-gradient(circle at 50% 10%, rgba(37, 99, 235, 0.2), transparent 30rem),
    linear-gradient(160deg, #070b14, #0b1220 55%, #07101b);
}
.login-card { width: min(420px, 100%); border-color: #293a58; }
.login-heading { margin-bottom: 24px; text-align: center; }
.login-heading h1 { margin: 0 0 8px; }
.login-button { width: 100%; margin-top: 8px; }
</style>
