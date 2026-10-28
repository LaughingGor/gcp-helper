import { createRouter, createWebHistory } from 'vue-router'
import GcpEnvList from '../views/GcpEnvList.vue'
import GcpEnvCreate from '../views/GcpEnvCreate.vue'
import GcpEnvDetail from '../views/GcpEnvDetail.vue'
import GcpEnvAuth from '../views/GcpEnvAuth.vue'
import PresetTaskList from '../views/PresetTaskList.vue'
import Login from '../views/Login.vue'
import UserManagement from '../views/UserManagement.vue'
import ChannelConfig from '../views/ChannelConfig.vue'
import ChannelStatistics from '../views/ChannelStatistics.vue'
import ChannelModelManagement from '../views/ChannelModelManagement.vue'
import { useAuthStore } from '../stores/auth'
import { pinia } from '../stores/pinia'

const router = createRouter({
  history: createWebHistory(),
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition || { top: 0 }
  },
  routes: [
    { path: '/', redirect: '/gcp-envs' },
    { path: '/login', component: Login, meta: { public: true } },
    { path: '/gcp-envs', component: GcpEnvList, meta: { permission: 'env:read' } },
    { path: '/gcp-envs/create', component: GcpEnvCreate, meta: { permission: 'env:create' } },
    { path: '/gcp-envs/:envId', component: GcpEnvDetail, props: true, meta: { permission: 'env:read' } },
    { path: '/gcp-tasks', component: PresetTaskList, meta: { permission: 'task:read' } },
    { path: '/channel-statistics', component: ChannelStatistics, meta: { permission: 'channel:stats' } },
    { path: '/my-channels', component: () => import('../views/MyChannels.vue'), meta: { permission: 'channel:usage', roles: ['ADMIN', 'CHANNEL_USER'] } },
    { path: '/channel-vault', component: () => import('../views/ChannelVault.vue'), meta: { permission: 'user:manage' } },
    { path: '/channel-models', component: ChannelModelManagement, meta: { permission: 'channel:model:write' } },
    { path: '/new-api-targets', component: () => import('../views/NewApiTargetManagement.vue'), meta: { permission: 'user:manage' } },
    { path: '/users', component: UserManagement, meta: { permission: 'user:manage' } },
    { path: '/channel-config', component: ChannelConfig, meta: { permission: 'settings:write' } },
    { path: '/gcp-envs/:envId/auth/:loginSessionId', component: GcpEnvAuth, props: true, meta: { permission: 'env:authorize' } }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore(pinia)
  if (!auth.initialized) await auth.loadCurrentUser()
  if (to.meta.public) return auth.isAuthenticated && to.path === '/login' ? auth.homePath : true
  if (!auth.isAuthenticated) return { path: '/login', query: { next: to.fullPath } }
  const roles = to.meta.roles
  if (Array.isArray(roles) && !roles.includes(auth.user?.role)) return auth.homePath
  const permission = typeof to.meta.permission === 'string' ? to.meta.permission : undefined
  if (permission && !auth.hasPermission(permission)) return auth.homePath
  return true
})

export default router
