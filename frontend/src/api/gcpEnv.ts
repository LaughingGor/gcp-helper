import { http } from './http'

export interface ProxyConfigIn {
  enabled: boolean
  type?: 'http' | 'http_no_tunnel' | 'socks4' | 'socks5'
  host?: string
  port?: number
  username?: string
  password?: string
}

export interface StartLoginRequest {
  googleAccount: string
  proxy: ProxyConfigIn
  vmZone?: string
}

export interface LoginStartResponse {
  envId: string
  loginSessionId: string
  configName: string
  status: string
  authUrl?: string
  expiresAt: string
}

export type LoginSessionStatus =
  | 'PENDING'
  | 'PROCESS_STARTED'
  | 'AUTH_URL_READY'
  | 'WAITING_CODE'
  | 'CODE_SUBMITTED'
  | 'SUCCESS'
  | 'FAILED'
  | 'TIMEOUT'
  | 'CANCELLED'

export interface LoginSessionResponse {
  loginSessionId: string
  envId: string
  envName?: string
  googleAccountHint?: string
  createdBy?: string
  status: LoginSessionStatus
  authUrl?: string
  errorCode?: string
  errorMessage?: string
  expiresAt: string
  startedAt: string
  finishedAt?: string
}

export interface SubmitCodeResponse {
  status: string
  envId: string
  authorizedAccount?: string
  configName: string
  presetTaskId?: string
  errorCode?: string
  message?: string
}

export interface GcpEnvironmentResponse {
  envId: string
  envName: string
  googleAccountHint: string
  authorizedAccount?: string
  createdBy?: string
  status: string
  configName: string
  proxyEnabled: boolean
  defaultProjectId?: string
  defaultRegion?: string
  defaultZone?: string
  billingAccountId?: string
  billingAccountDisplayName?: string
  createdAt: string
  channelPushStatus?: 'PENDING' | 'SUCCESS' | 'FAILED'
  channelPushAction?: 'CREATED' | 'REACTIVATED' | 'ALREADY_EXISTS'
  channelPushedAt?: string
  channelPushMessage?: string
}

export interface BillingAccountResponse {
  billingAccountId: string
  displayName: string
  open: boolean
}

export interface UpdateBillingAccountResponse {
  billingAccountId: string
  displayName: string
  linkedProjectId?: string
}

export interface DeleteEnvResponse {
  envId: string
  deleted: boolean
}

export interface ChannelPushResponse {
  success: boolean
  message: string
  envId: string
  taskId: string
  channelName: string
  templateName: string
}

export interface PresetTaskResponse {
  taskId: string
  envId: string
  envName?: string
  googleAccountHint?: string
  createdBy?: string
  taskType: string
  status: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'FAILED'
  currentStep?: string
  projectId?: string
  serviceAccountEmail?: string
  keyFileName?: string
  keyFileExists: boolean
  credentialStored: boolean
  vmName?: string
  vmZone?: string
  requestedVmZone?: string
  vmExternalIp?: string
  firewallRuleName?: string
  socks5Port?: string
  socks5Url?: string
  errorCode?: string
  errorMessage?: string
  createdAt: string
  startedAt?: string
  finishedAt?: string
}

export interface ListPresetTasksParams {
  status?: PresetTaskResponse['status']
  envId?: string
  limit?: number
  offset?: number
  createdDate?: string
  startDate?: string
  endDate?: string
}

export async function startLogin(payload: StartLoginRequest) {
  const { data } = await http.post<LoginStartResponse>('/gcp-envs/login/start', payload)
  return data
}

export async function getLoginSession(loginSessionId: string) {
  const { data } = await http.get<LoginSessionResponse>(`/gcp-envs/login-sessions/${loginSessionId}`)
  return data
}

export async function listLoginSessions(params: { activeOnly?: boolean; limit?: number } = {}) {
  const { data } = await http.get<LoginSessionResponse[]>('/gcp-envs/login-sessions', { params })
  return data
}

export async function submitLoginCode(loginSessionId: string, code: string) {
  const { data } = await http.post<SubmitCodeResponse>(`/gcp-envs/login-sessions/${loginSessionId}/code`, { code })
  return data
}

export async function cancelLogin(loginSessionId: string) {
  const { data } = await http.post<LoginSessionResponse>(`/gcp-envs/login-sessions/${loginSessionId}/cancel`)
  return data
}

export async function retryLoginSession(loginSessionId: string) {
  const { data } = await http.post<LoginSessionResponse>(`/gcp-envs/login-sessions/${loginSessionId}/retry`)
  return data
}

export async function listGcpEnvs(params: { createdDate?: string; startDate?: string; endDate?: string; googleAccount?: string } = {}) {
  const { data } = await http.get<GcpEnvironmentResponse[]>('/gcp-envs', { params })
  return data
}

export async function getGcpEnv(envId: string) {
  const { data } = await http.get<GcpEnvironmentResponse>(`/gcp-envs/${envId}`)
  return data
}

export async function listBillingAccounts(envId: string) {
  const { data } = await http.get<BillingAccountResponse[]>(`/gcp-envs/${envId}/billing-accounts`)
  return data
}

export async function updateBillingAccount(envId: string, billingAccountId: string) {
  const { data } = await http.put<UpdateBillingAccountResponse>(`/gcp-envs/${envId}/billing-account`, { billingAccountId })
  return data
}

export async function deleteGcpEnv(envId: string) {
  const { data } = await http.delete<DeleteEnvResponse>(`/gcp-envs/${envId}`)
  return data
}

export async function pushNewApiChannel(envId: string, targetId: string) {
  const payload = { targetId }
  const { data } = await http.post<ChannelPushResponse>(`/gcp-envs/${envId}/channel-push`, payload)
  return data
}

export async function startBootstrapTask(envId: string, vmZone?: string) {
  const { data } = await http.post<PresetTaskResponse>(`/gcp-envs/${envId}/preset-tasks/bootstrap`, { vmZone })
  return data
}

export async function getLatestBootstrapTask(envId: string) {
  const { data } = await http.get<PresetTaskResponse | null>(`/gcp-envs/${envId}/preset-tasks/latest`)
  return data
}

export async function getPresetTask(envId: string, taskId: string) {
  const { data } = await http.get<PresetTaskResponse>(`/gcp-envs/${envId}/preset-tasks/${taskId}`)
  return data
}

export async function listPresetTasks(params: ListPresetTasksParams = {}) {
  const { data } = await http.get<PresetTaskResponse[]>('/preset-tasks', { params })
  return data
}

export async function retryPresetTask(taskId: string) {
  const { data } = await http.post<PresetTaskResponse>(`/preset-tasks/${taskId}/retry`)
  return data
}

export async function downloadServiceAccountKey(envId: string, taskId: string, filename?: string) {
  const response = await http.get(`/gcp-envs/${envId}/preset-tasks/${taskId}/service-account-key`, {
    responseType: 'blob'
  })
  const url = URL.createObjectURL(response.data)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'gcp-helper-service-account-key.json'
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

export interface ExecuteGcloudCommandRequest {
  command: string
  timeoutSeconds: number
  confirmHighRisk: boolean
}

export interface GcloudCommandResponse {
  command: string
  exitCode: number
  success: boolean
  stdout: string
  stderr: string
  startedAt: string
  finishedAt: string
  durationMs: number
  outputTruncated: boolean
}

export interface GcloudCommandHistoryItem {
  id: string
  command: string
  exitCode?: number
  status: string
  startedAt: string
  finishedAt?: string
}

export async function executeGcloudCommand(envId: string, payload: ExecuteGcloudCommandRequest) {
  const { data } = await http.post<GcloudCommandResponse>(`/gcp-envs/${envId}/commands`, payload)
  return data
}

export async function listGcloudCommandHistory(envId: string, limit = 20) {
  const { data } = await http.get<GcloudCommandHistoryItem[]>(`/gcp-envs/${envId}/commands`, { params: { limit } })
  return data
}

export interface ReplacementPreview {
  deploymentId: string
  targetId: string
  targetName: string
  channelId: string
  channelName: string
  projectId?: string
  billingAccountId?: string
  vmZone?: string
}

export interface ProjectReplacement {
  id: string
  envId: string
  taskId: string
  targetId: string
  deploymentId: string
  channelId: string
  oldProjectId?: string
  newProjectId?: string
  vmZone?: string
  status: string
  message: string
  currentStep?: string
  createdAt: string
  updatedAt: string
}

export async function previewProjectReplacement(envId: string, targetId: string) {
  return (await http.get<ReplacementPreview>(`/gcp-envs/${envId}/project-replacements/preview`, { params: { targetId } })).data
}

export async function listProjectReplacements(envId: string) {
  return (await http.get<ProjectReplacement[]>(`/gcp-envs/${envId}/project-replacements`)).data
}

export async function createProjectReplacement(envId: string, payload: {
  targetId: string; deploymentId: string; requestId: string; vmZone?: string
}) {
  return (await http.post<ProjectReplacement>(`/gcp-envs/${envId}/project-replacements`, payload)).data
}

export async function retryProjectReplacement(envId: string, id: string) {
  return (await http.post<ProjectReplacement>(`/gcp-envs/${envId}/project-replacements/${id}/retry`)).data
}
