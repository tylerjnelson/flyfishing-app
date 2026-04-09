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
import Notes from './pages/Notes'
import NoteUpload from './pages/NoteUpload'
import Trips from './pages/Trips'
import TripNew from './pages/TripNew'
import TripThread from './pages/TripThread'

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
