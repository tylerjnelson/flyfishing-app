import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import axios from 'axios'
import useAuthStore from '../store/auth'

export default function Login() {
  const accessToken = useAuthStore(s => s.accessToken)
  const [email, setEmail] = useState('')
  const [submitted, setSubmitted] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  if (accessToken) return <Navigate to="/trips" replace />

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await axios.post('/api/auth/magic-link', { email }, { withCredentials: true })
      setSubmitted(true)
    } catch {
      setError('Something went wrong. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  if (submitted) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50">
        <div className="max-w-sm w-full mx-4 text-center">
          <h1 className="text-2xl font-semibold text-gray-900 mb-2">Check your email</h1>
          <p className="text-gray-600">
            We sent a sign-in link to <strong>{email}</strong>.
            The link expires in 15 minutes.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50">
      <div className="max-w-sm w-full mx-4">
        <div className="text-center mb-8">
          <h1 className="text-3xl font-semibold text-gray-900">Fly Fish WA</h1>
          <p className="text-gray-500 mt-2">Sign in with your email</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            type="email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            placeholder="your@email.com"
            required
            autoFocus
            className="w-full px-4 py-3 border border-gray-300 rounded-lg text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
          {error && <p className="text-red-600 text-sm">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Sending…' : 'Send sign-in link'}
          </button>
        </form>
      </div>
    </div>
  )
}
