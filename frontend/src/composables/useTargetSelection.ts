import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getAvailableTargets, type AvailableTarget } from '../api/channelUploads'
import { getUserSettings } from '../api/userSettings'
import { useAuthStore } from '../stores/auth'

export function useTargetSelection(enabled = true) {
  const auth = useAuthStore()
  const canSelectTargets = computed(() => auth.user?.role === 'ADMIN')
  const targets = ref<AvailableTarget[]>([]), targetId = ref(''), targetsLoading = ref(false)
  const targetLabel = computed(() => targets.value.find(t => t.id === targetId.value)?.name || '尚未绑定可用后台，请联系管理员')
  async function loadTargets() {
    if (!enabled) return
    targetsLoading.value = true
    try {
      const [items, settings] = await Promise.all([getAvailableTargets(), canSelectTargets.value ? getUserSettings() : Promise.resolve(null)])
      targets.value = items
      if (!canSelectTargets.value) { targetId.value = items[0]?.id || ''; return }
      if (!items.some(t => t.id === targetId.value)) {
        targetId.value = items.some(t => t.id === settings?.defaultTargetId) ? settings!.defaultTargetId!
          : items.length === 1 ? items[0].id : ''
      }
    } catch { ElMessage.error('加载目标后台失败，请检查权限或联系管理员') }
    finally { targetsLoading.value = false }
  }
  onMounted(() => { void loadTargets() })
  return { targets, targetId, targetsLoading, loadTargets, canSelectTargets, targetLabel }
}
