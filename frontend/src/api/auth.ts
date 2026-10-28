import { http } from './http'

export type UserRole = 'ADMIN' | 'OPERATOR' | 'CHANNEL_USER'
export type UserStatus = 'ACTIVE' | 'DISABLED'

export interface CurrentUser {
  id: string
  username: string
  email?: string
  role: UserRole
  boundTargetId?: string | null
  status: UserStatus
  permissions: string[]
  createdAt: string
  lastLoginAt?: string
}

export async function login(username: string, password: string) {
  const { data } = await http.post<{ user: CurrentUser }>('/auth/login', { username, password })
  return data.user
}

export async function getCurrentUser() {
  const { data } = await http.get<CurrentUser>('/auth/me')
  return data
}

export async function logout() {
  await http.post('/auth/logout')
}

export async function listUsers() {
  const { data } = await http.get<CurrentUser[]>('/admin/users')
  return data
}

export async function bindUserTarget(userId: string, boundTargetId: string | null) {
  return (await http.put<CurrentUser>(`/admin/users/${userId}/target`, { boundTargetId })).data
}

export async function createUser(payload: {
  username: string
  password: string
  email?: string
  role: UserRole
}) {
  const { data } = await http.post<CurrentUser>('/admin/users', payload)
  return data
}

export async function updateUser(
  userId: string,
  payload: { role?: UserRole; status?: UserStatus; password?: string }
) {
  const { data } = await http.patch<CurrentUser>(`/admin/users/${userId}`, payload)
  return data
}
