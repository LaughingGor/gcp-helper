import { http } from './http'

export type UploadKind = 'vertex_json' | 'gemini_key'
export interface UploadInput { kind: UploadKind; credential: string; remark: string }
export interface UploadedChannel {
  id: string; kind: UploadKind; name: string; remark: string; identity: string
  status: string; message: string; channelId: string | null; remoteStatus: string | null
  currency: string; usedAmount: string | null; syncedAt: string | null
  syncError: string | null; createdAt: string
  sourceUploadId: string | null; targetId: string | null; targetName: string | null
  credentialStored: boolean; deployments?: UploadedChannel[]; userId?: string; username?: string
  origin: 'UPLOAD' | 'GCP'; originEnvId?: string; originTaskId?: string
}
export interface UsageSummary {
  channelCount: number; activeCount: number; pendingCount: number; incompleteCount: number
  totals: Record<string, string>
}
export interface UserUsage extends UsageSummary { userId: string; username: string }

export async function previewUpload(input: UploadInput) {
  return (await http.post<{ kind: UploadKind; identity: string }>('/channel-uploads/preview', input)).data
}
export async function createUpload(input: UploadInput, requestId: string) {
  return (await http.post<UploadedChannel>('/channel-uploads', { ...input, requestId })).data
}
export async function retryUpload(id: string) {
  return (await http.post<UploadedChannel>(`/channel-uploads/${id}/retry`)).data
}
export async function listUploads(page: number, pageSize: number, userId?: string) {
  const path = userId ? `/admin/channel-usage/${userId}` : '/my-channels'
  return (await http.get<{ items: UploadedChannel[]; total: number }>(path, { params: { page, pageSize } })).data
}
export async function getUploadUsage() {
  return (await http.get<UsageSummary>('/my-channel-usage')).data
}
export async function refreshUploadUsage() {
  return (await http.post<{ message: string }>('/my-channel-usage/refresh')).data
}
export async function getUsersUsage() {
  return (await http.get<UserUsage[]>('/admin/channel-usage')).data
}

export interface NewApiTarget {
  id: string; name: string; baseUrl: string; tokenConfigured: boolean; enabled: boolean
  models: string; group: string; statsGroup: string; vertexRegion: string; vertexTemplate: string; geminiTemplate: string
  quotaPerUnit: string; currency: string
}
export interface TargetInput { baseUrl: string; token?: string; statsGroup?: string }
export type AvailableTarget = Pick<NewApiTarget, 'id' | 'name' | 'models' | 'group' | 'currency' | 'quotaPerUnit' | 'vertexRegion'>
export async function getAvailableTargets() { return (await http.get<AvailableTarget[]>('/new-api-targets')).data }
export async function getTargets() { return (await http.get<NewApiTarget[]>('/admin/new-api-targets')).data }
export async function deleteTarget(id: string) { await http.delete(`/admin/new-api-targets/${id}`) }
export async function saveTarget(input: TargetInput, id?: string) {
  return (await (id ? http.put<NewApiTarget>(`/admin/new-api-targets/${id}`, input)
    : http.post<NewApiTarget>('/admin/new-api-targets', input))).data
}
export async function getVault(page: number) {
  return (await http.get<{ items: UploadedChannel[]; total: number }>('/admin/channel-uploads', { params: { page, pageSize: 20 } })).data
}
export async function revealCredential(id: string) {
  return (await http.post<{ kind: UploadKind; credential: string }>(`/admin/channel-uploads/${id}/credential`)).data
}
export async function downloadCredential(id: string) {
  return (await http.post<Blob>(`/admin/channel-uploads/${id}/download`, undefined, { responseType: 'blob' })).data
}
export async function pushCredential(id: string, targetIds: string[]) {
  return (await http.post<{ items: UploadedChannel[] }>(`/admin/channel-uploads/${id}/push`, { targetIds })).data
}
export interface LegacyPush { id: string; channelId: string; channelName: string; createdAt: string }
export async function getLegacyPushes(id: string) { return (await http.get<LegacyPush[]>(`/admin/channel-uploads/${id}/legacy-pushes`)).data }
export async function adoptLegacyPush(id: string, recordId: string, targetId: string) {
  return (await http.post<UploadedChannel>(`/admin/channel-uploads/${id}/adopt-legacy`, { recordId, targetId })).data
}
export async function importGcpCredentials(cursor = '') {
  return (await http.post<{ imported: number; failed: number; failures: { taskId: string; message: string }[]; nextCursor: string | null }>(
    '/admin/gcp-credentials/import', undefined, { params: { cursor } })).data
}
