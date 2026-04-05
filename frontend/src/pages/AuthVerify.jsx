import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import axios from 'axios'
import useAuthStore from '../store/auth'

export default function AuthVerify() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const setAuth = useAuthStore(s => s.setAuth)
  const [error, setError] = useState('')

  useEffect(() => {
    const token = searchParams.get('token')
    if (!token) {
      setError('Invalid sign-in link.')
      return
    }

    axios
      .get(`/api/auth/verify?token=${encodeURIComponent(token)}`, { withCredentials: true })
      .then(({ data }) => {
        setAuth(data.user, data.access_token)
        navigate(data.onboarding_required ? '/onboarding' : '/trips', { replace: true })
      })
      .catch(err => {
        const msg = err.response?.data?.detail
        if (msg === 'Token already used') {
          setError('This sign-in link has already been used. Request a new one.')
        } else if (msg === 'Token expired') {
          setError('This sign-in link has expired. Request a new one.')
        } else {
          setError('Invalid or expired sign-in link.')
        }
      })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="max-w-sm w-full mx-4 text-center">
          <p className="text-red-600 mb-4">{error}</p>
          <a href="/login" className="text-blue-600 underline">Back to sign in</a>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50">
      <p className="text-gray-500">Signing you in…</p>
    </div>
  )
}
