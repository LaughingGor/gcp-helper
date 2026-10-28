import { http } from './http'

export type ChannelModelStatusFilter = 'enabled' | 'disabled' | 'all'
export type ChannelModelUpdateStatus = 'SUCCESS' | 'SKIPPED' | 'FAILED'

export interface AddChannelModelRequest {
  targetId?: string
  group: string
  status: ChannelModelStatusFilter
  model: string
}

export interface ChannelModelCandidate {
  channelId: string
  name: string
  status: number | null
  currentModels: string
  updatedModels: string
}

export interface ChannelModelPreviewResponse {
  group: string
  status: ChannelModelStatusFilter
  model: string
  totalChannels: number
  alreadyPresentCount: number
  candidateCount: number
  candidates: ChannelModelCandidate[]
}

export interface ChannelModelUpdateResult {
  channelId: string
  name: string
  status: ChannelModelUpdateStatus
  message: string
}

export interface ChannelModelApplyResponse {
  group: string
  status: ChannelModelStatusFilter
  model: string
  requestedCount: number
  successCount: number
  skippedCount: number
  failedCount: number
  results: ChannelModelUpdateResult[]
}

export async function previewChannelModel(payload: AddChannelModelRequest) {
  const { data } = await http.post<ChannelModelPreviewResponse>('/channel-models/preview', payload)
  return data
}

export async function addChannelModel(payload: AddChannelModelRequest) {
  const { data } = await http.post<ChannelModelApplyResponse>('/channel-models/add', payload)
  return data
}
