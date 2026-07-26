import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import axios from 'axios'
import useAuthStore from './store/auth'
import ProtectedRoute from './components/ProtectedRoute'
import Login from './pages/Login'
import AuthVerify from './pages/AuthVerify'
import Onboarding from './pages/Onboarding'
import Settings from './pages/Settings'
import Spots from './pages/Spots'
import SpotDetail from './pages/SpotDetail'
import SavedSpots from './pages/SavedSpots'
import Notes from './pages/Notes'
import NoteUpload from './pages/NoteUpload'
import Trips from './pages/Trips'
import TripNew from './pages/TripNew'
import TripThread from './pages/TripThread'

export default function App() {
  const { setAuth, accessToken } = useAuthStore()
  const [ready, setReady] = useState(false)

  // On mount: attempt silent refresh from httpOnly cookie.
  // Retries up to 3 times on transient errors (network blip, server restart).
  // Only a definitive 401 means the session is gone.
  useEffect(() => {
    async function tryRefresh() {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const { data } = await axios.post('/api/auth/refresh', {}, { withCredentials: true })
          return data
        } catch (err) {
          const status = err.response?.status
          // Definitive "no session" — stop.
          if (status === 401) return null
          // Rate-limited (429) or upstream-limited (503): a retry just burns
          // more of the same budget and can starve a concurrent verify request.
          // Treat as non-transient and give up rather than attack our own limit.
          if (status === 429 || status === 503) return null
          // True transient error (network blip, server restart) — back off + retry.
          await new Promise(r => setTimeout(r, 1000 * (attempt + 1)))
        }
      }
      return null
    }
    tryRefresh()
      .then(data => { if (data) setAuth(data.user, data.access_token) })
      .finally(() => setReady(true))
  }, [setAuth])

  if (!ready) return null

  return (
    <BrowserRouter>
      <Routes>
        {/* Public */}
        <Route path="/login" element={<Login />} />
        <Route path="/auth/verify" element={<AuthVerify />} />

        {/* Root redirect */}
        <Route
          path="/"
          element={<Navigate to={accessToken ? '/trips' : '/login'} replace />}
        />

        {/* Protected */}
        <Route path="/onboarding" element={
          <ProtectedRoute><Onboarding /></ProtectedRoute>
        } />
        <Route path="/trips" element={
          <ProtectedRoute><Trips /></ProtectedRoute>
        } />
        <Route path="/trips/new" element={
          <ProtectedRoute><TripNew /></ProtectedRoute>
        } />
        <Route path="/trips/:tripId" element={
          <ProtectedRoute><TripThread /></ProtectedRoute>
        } />
        <Route path="/spots" element={
          <ProtectedRoute><Spots /></ProtectedRoute>
        } />
        <Route path="/spots/saved" element={
          <ProtectedRoute><SavedSpots /></ProtectedRoute>
        } />
        <Route path="/spots/:spotId" element={
          <ProtectedRoute><SpotDetail /></ProtectedRoute>
        } />
        <Route path="/notes" element={
          <ProtectedRoute><Notes /></ProtectedRoute>
        } />
        <Route path="/notes/upload" element={
          <ProtectedRoute><NoteUpload /></ProtectedRoute>
        } />
        <Route path="/settings" element={
          <ProtectedRoute><Settings /></ProtectedRoute>
        } />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
