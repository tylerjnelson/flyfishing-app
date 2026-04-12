import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import useAuthStore from '../store/auth'
import LocationInput from '../components/LocationInput'

export default function Settings() {
  const navigate = useNavigate()
  const { user, setAuth, clearAuth, accessToken } = useAuthStore()
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  // home_location is {label, lat, lon} or null
  const [homeLocation, setHomeLocation] = useState(
    user?.preferences?.home_location?.lat != null ? user.preferences.home_location : null
  )
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.get('/users/me').then(({ data }) => {
      setDisplayName(data.display_name || '')
      const loc = data.preferences?.home_location
      setHomeLocation(loc?.lat != null ? loc : null)
      setAuth({ ...data }, accessToken)
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  async function handleSave(e) {
    e.preventDefault()
    setSaving(true)
    setSaved(false)
    try {
      const { data } = await api.patch('/users/me', {
        display_name: displayName,
        preferences: { ...user.preferences, home_location: homeLocation },
      })
      setAuth({ ...data }, accessToken)
      setSaved(true)
    } finally {
      setSaving(false)
    }
  }

  async function handleLogout() {
    await api.post('/auth/logout').catch(() => {})
    clearAuth()
    navigate('/login', { replace: true })
  }

  const prefs = user?.preferences || {}

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <h1 className="text-2xl font-semibold text-gray-900 mb-8">My Profile</h1>

      <form onSubmit={handleSave} className="space-y-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Display name
          </label>
          <input
            type="text"
            value={displayName}
            onChange={e => setDisplayName(e.target.value)}
            className="w-full px-4 py-2 border border-gray-300 rounded-lg bg-white text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Home / departure location
          </label>
          <LocationInput value={homeLocation} onChange={setHomeLocation} />
        </div>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={saving}
            className="px-6 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          {saved && <span className="text-green-600 text-sm">Saved</span>}
        </div>
      </form>

      <div className="mt-12 pt-8 border-t border-gray-200">
        <p className="text-sm text-gray-500 mb-1">Signed in as</p>
        <p className="text-sm text-gray-900 mb-4">{user?.email}</p>
        <button
          onClick={handleLogout}
          className="text-sm text-red-600 hover:underline"
        >
          Sign out
        </button>
      </div>
    </div>
  )
}
