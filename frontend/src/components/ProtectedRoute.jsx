import { Navigate } from 'react-router-dom'
import useAuthStore from '../store/auth'

/**
 * Wrap any route that requires authentication.
 * Redirects to /login if no access token is present in memory.
 */
export default function ProtectedRoute({ children }) {
  const accessToken = useAuthStore(s => s.accessToken)
  if (!accessToken) return <Navigate to="/login" replace />
  return children
}
