import { http } from './http'

export interface UserSettingsResponse {
  computeVmZone: string
  usingDefaultZone: boolean
  defaultTargetId: string | null
}

export async function getUserSettings() {
  const { data } = await http.get<UserSettingsResponse>('/user-settings')
  return data
}

export async function updateUserSettings(computeVmZone: string, defaultTargetId?: string | null) {
  const { data } = await http.put<UserSettingsResponse>('/user-settings', { computeVmZone, defaultTargetId })
  return data
}
