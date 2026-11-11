export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

const BASE = `${import.meta.env.BASE_URL}api`

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(init.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    let msg = res.statusText
    try { msg = (await res.json()).detail || msg } catch { /* ignore */ }
    throw new ApiError(res.status, msg)
  }
  return res.json() as Promise<T>
}

export interface Me { kind: string; name: string; role?: string }
export const auth = {
  me: () => api<Me>('/auth/me'),
  login: (username: string, password: string) =>
    api<{ username: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => api<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
}
