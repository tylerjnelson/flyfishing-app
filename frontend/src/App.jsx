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

// Placeholder pages for routes built in later phases
const Placeholder = ({ name }) => (
  <div className="flex items-center justify-center min-h-screen text-gray-500">
    {name} — coming soon
  </div>
)

export default function App() {
  const { setAuth, accessToken } = useAuthStore()
  const [ready, setReady] = useState(false)

  // On mount: attempt silent refresh from httpOnly cookie.
  // If it succeeds the user stays logged in across page refreshes.
  useEffect(() => {
    axios
      .post('/api/auth/refresh', {}, { withCredentials: true })
      .then(({ data }) => setAuth(data.user, data.access_token))
      .catch(() => {})
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
          <ProtectedRoute><Placeholder name="Trips" /></ProtectedRoute>
        } />
        <Route path="/trips/new" element={
          <ProtectedRoute><Placeholder name="New Trip" /></ProtectedRoute>
        } />
        <Route path="/trips/:tripId" element={
          <ProtectedRoute><Placeholder name="Trip" /></ProtectedRoute>
        } />
        <Route path="/spots" element={
          <ProtectedRoute><Spots /></ProtectedRoute>
        } />
        <Route path="/spots/:spotId" element={
          <ProtectedRoute><SpotDetail /></ProtectedRoute>
        } />
        <Route path="/notes" element={
          <ProtectedRoute><Placeholder name="Notes" /></ProtectedRoute>
        } />
        <Route path="/notes/upload" element={
          <ProtectedRoute><Placeholder name="Upload Note" /></ProtectedRoute>
        } />
        <Route path="/settings" element={
          <ProtectedRoute><Settings /></ProtectedRoute>
        } />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
