/**
 * Zustand auth store.
 *
 * accessToken lives in memory only — never written to localStorage or
 * sessionStorage.  On page refresh the token is lost and recovered via
 * the silent refresh call in App.jsx using the httpOnly cookie.
 */

import { create } from 'zustand'

const useAuthStore = create(set => ({
  user: null,
  accessToken: null,

  setAuth: (user, accessToken) => set({ user, accessToken }),
  clearAuth: () => set({ user: null, accessToken: null }),
}))

export default useAuthStore
