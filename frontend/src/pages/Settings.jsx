import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import useAuthStore from '../store/auth'

const OPTION_LABELS = {
  paved_only: 'Paved / 2WD only',
  dirt_ok: 'Dirt road OK',
  four_wd: '4WD / High clearance',
  beginner: 'Beginner',
  intermediate: 'Intermediate',
  advanced: 'Advanced',
  catch_and_release: 'Strict catch-and-release',
  keep_if_legal: 'Keep if legal',
  full_setup: 'Full setup',
  pack_rod: 'Pack rod',
  float_tube: 'Float tube',
  spey: 'Spey / two-hander',
}

export default function Settings() {
  const navigate = useNavigate()
  const { user, setAuth, clearAuth, accessToken } = useAuthStore()
  const [displayName, setDisplayName] = useState(user?.display_name || '')
  const [homeLocation, setHomeLocation] = useState(user?.preferences?.home_location || '')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    api.get('/users/me').then(({ data }) => {
      setDisplayName(data.display_name || '')
      setHomeLocation(data.preferences?.home_location || '')
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
            className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Home / departure location
          </label>
          <input
            type="text"
            value={homeLocation}
            onChange={e => setHomeLocation(e.target.value)}
            placeholder="City, State or full address"
            className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {/* Read-only preference summary */}
        {Object.keys(prefs).filter(k => k !== 'home_location').length > 0 && (
          <div className="bg-gray-50 rounded-lg p-4 space-y-2">
            <p className="text-sm font-medium text-gray-700 mb-3">Preferences</p>
            {Object.entries(prefs)
              .filter(([k]) => k !== 'home_location')
              .map(([key, val]) => (
                <div key={key} className="flex justify-between text-sm">
                  <span className="text-gray-500 capitalize">{key.replace(/_/g, ' ')}</span>
                  <span className="text-gray-900">
                    {Array.isArray(val)
                      ? val.map(v => OPTION_LABELS[v] || v).join(', ')
                      : OPTION_LABELS[val] || val}
                  </span>
                </div>
              ))}
            <button
              type="button"
              onClick={() => navigate('/onboarding')}
              className="text-sm text-blue-600 underline mt-2"
            >
              Edit all preferences
            </button>
          </div>
        )}

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
