import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  getCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  type CurrentUser
} from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser>()
  const initialized = ref(false)
  const isAuthenticated = computed(() => Boolean(user.value))
  const homePath = computed(() => user.value?.role === 'CHANNEL_USER' ? '/my-channels' : '/gcp-envs')

  async function loadCurrentUser() {
    try {
      user.value = await getCurrentUser()
    } catch {
      user.value = undefined
    } finally {
      initialized.value = true
    }
  }

  async function login(username: string, password: string) {
    user.value = await loginRequest(username, password)
    initialized.value = true
  }

  async function logout() {
    try {
      await logoutRequest()
    } finally {
      user.value = undefined
      initialized.value = true
    }
  }

  function hasPermission(permission: string) {
    return Boolean(user.value?.permissions.includes(permission))
  }

  return { user, initialized, isAuthenticated, homePath, loadCurrentUser, login, logout, hasPermission }
})
