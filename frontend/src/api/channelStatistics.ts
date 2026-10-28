import { http } from './http'

export type ChannelClassification =
  | 'DISABLED'
  | 'WITHIN_LIMIT'
  | 'EXHAUSTED'
  | 'OVER_LIMIT'
  | 'DATA_INVALID'
  | 'UNKNOWN_STATUS'

export interface DisabledChannelStatistics {
  channelCount: number
  validCount: number
  invalidCount: number
  manualDisabledCount: number
  autoDisabledCount: number
  totalConsumedAmount: string
  averageConsumedAmount: string | null
}

export interface EnabledChannelStatistics {
  channelCount: number
  totalConsumedAmount: string
  withinLimitCount: number
  exhaustedCount: number
  overLimitCount: number
  invalidCount: number
  totalRemainingAmount: string
  averageRemainingAmount: string | null
  totalOverAmount: string
}

export interface ChannelStatisticItem {
  channelId: string
  name: string
  channelType?: number
  groups: string[]
  status?: number
  statusLabel: string
  usedQuota: string | null
  usedAmount: string | null
  maxAmount: string
  remainingAmount: string | null
  overAmount: string | null
  classification: ChannelClassification
  invalidReason: string | null
}

export interface ChannelStatisticsResponse {
  group: string
  currency: string
  quotaPerUnit: string
  maxAmountPerChannel: string
  generatedAt: string
  partial: boolean
  matchedChannelCount: number
  unknownStatusCount: number
  totalConsumedAmount: string
  disabled: DisabledChannelStatistics
  enabled: EnabledChannelStatistics
  channels: ChannelStatisticItem[]
  warnings: string[]
}

export async function getChannelStatistics(refresh = false, targetId?: string) {
  const { data } = await http.get<ChannelStatisticsResponse>('/channel-statistics', {
    params: { refresh: refresh || undefined, targetId }
  })
  return data
}
