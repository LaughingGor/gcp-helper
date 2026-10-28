<template>
  <router-view v-if="$route.meta.public" />
  <el-container v-else class="layout">
    <el-header class="topbar">
      <div class="brand">GCP Helper</div>
      <el-menu mode="horizontal" router :default-active="$route.path" class="menu">
        <el-menu-item v-if="auth.hasPermission('env:read')" index="/gcp-envs">环境列表</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('env:create')" index="/gcp-envs/create">创建与授权</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('task:read')" index="/gcp-tasks">后台任务</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('channel:stats')" index="/channel-statistics">渠道统计</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('channel:usage') && auth.user?.role !== 'OPERATOR'" index="/my-channels">我的渠道</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('user:manage')" index="/channel-vault">凭据管理</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('channel:model:write')" index="/channel-models">渠道模型管理</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('settings:write')" index="/channel-config">默认偏好</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('user:manage')" index="/new-api-targets">new-api 渠道管理</el-menu-item>
        <el-menu-item v-if="auth.hasPermission('user:manage')" index="/users">用户管理</el-menu-item>
      </el-menu>
      <div class="current-user">
        <span>{{ auth.user?.username }}</span>
        <el-tag size="small" type="info">{{ auth.user?.role }}</el-tag>
        <el-button link type="primary" @click="handleLogout">退出</el-button>
      </div>
    </el-header>
    <el-main>
      <router-view v-slot="{ Component }">
        <keep-alive include="GcpEnvList" :max="1">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from './stores/auth'

const router = useRouter()
const auth = useAuthStore()

async function handleLogout() {
  try {
    await auth.logout()
  } catch {
    ElMessage.error('退出登录失败')
  } finally {
    await router.replace('/login')
  }
}
</script>

<style scoped>
.layout { min-height: 100vh; background: transparent; }
.topbar {
  position: sticky;
  z-index: 20;
  top: 0;
  display: flex;
  align-items: center;
  gap: 24px;
  height: 64px;
  border-bottom: 1px solid rgba(58, 76, 110, 0.55);
  background: rgba(8, 13, 23, 0.86);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
  backdrop-filter: blur(16px);
}
.brand {
  flex: 0 0 auto;
  font-size: 19px;
  font-weight: 750;
  letter-spacing: -0.035em;
  background: linear-gradient(120deg, #93c5fd, #60a5fa 48%, #5eead4);
  background-clip: text;
  color: transparent;
}
.menu { flex: 1; min-width: 0; border-bottom: none; background: transparent; }
.current-user { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
:deep(.el-main) { padding: 28px 30px 44px; }
:deep(.el-menu--horizontal) { --el-menu-horizontal-height: 63px; }
:deep(.el-menu-item) { font-weight: 520; }
@media (max-width: 900px) {
  .topbar { gap: 12px; padding: 0 14px; overflow-x: auto; }
  .brand { font-size: 17px; }
  :deep(.el-main) { padding: 20px 14px 36px; }
}
</style>
