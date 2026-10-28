import { http } from './http'

export interface ChannelConfigResponse {
  baseUrl: string
  tokenConfigured: boolean
  updatedAt: string
}

export interface UpdateChannelConfigRequest {
  baseUrl: string
  token?: string
}

export async function getChannelConfig() {
  const { data } = await http.get<ChannelConfigResponse | null>('/channel-config')
  return data
}

export async function updateChannelConfig(payload: UpdateChannelConfigRequest) {
  const { data } = await http.put<ChannelConfigResponse>('/channel-config', payload)
  return data
}
