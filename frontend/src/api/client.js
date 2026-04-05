/**
 * Axios instance with auth interceptors.
 *
 * - Attaches Authorization: Bearer <token> on every request
 * - On 401: attempts one silent token refresh via the httpOnly cookie,
 *   retries the original request, then redirects to /login on failure
 * - withCredentials: true — sends httpOnly refresh_token cookie
 */

import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
})

// Lazy import to avoid circular dependency with store
const getStore = () => import('../store/auth').then(m => m.default)

api.interceptors.request.use(async config => {
  const store = await getStore()
  const token = store.getState().accessToken
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// Single in-flight refresh promise — prevents multiple simultaneous refreshes
let refreshPromise = null

api.interceptors.response.use(
  res => res,
  async error => {
    const original = error.config

    if (error.response?.status !== 401 || original._retry) {
      return Promise.reject(error)
    }
    original._retry = true

    try {
      if (!refreshPromise) {
        refreshPromise = axios
          .post('/api/auth/refresh', {}, { withCredentials: true })
          .finally(() => { refreshPromise = null })
      }
      const { data } = await refreshPromise
      const store = await getStore()
      store.getState().setAuth(data.user, data.access_token)
      original.headers.Authorization = `Bearer ${data.access_token}`
      return api.request(original)
    } catch {
      const store = await getStore()
      store.getState().clearAuth()
      window.location.href = '/login'
      return Promise.reject(error)
    }
  }
)

export default api
