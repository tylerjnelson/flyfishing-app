/**
 * Saved spots — offline-capable list of spots the user has saved.
 *
 * Data is served from the Workbox 'api-saved-spots' cache when offline,
 * so this page remains readable at the trailhead with no cell signal.
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api from '../api/client'

function scoreColor(score) {
  if (score === null || score === undefined) return '#9ca3af'
  if (score >= 8) return '#16a34a'
  if (score >= 5) return '#ca8a04'
  if (score >= 2) return '#ea580c'
  return '#9ca3af'
}

export default function SavedSpots() {
  const navigate = useNavigate()
  const [spots, setSpots] = useState([])
  const [loading, setLoading] = useState(true)
  const [offline, setOffline] = useState(false)

  useEffect(() => {
    setOffline(!navigator.onLine)
    api
      .get('/users/me/saved-spots')
      .then(({ data }) => setSpots(data.saved_spots || []))
      .catch(() => {
        // Request may have been served from SW cache even offline
        setOffline(!navigator.onLine)
      })
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="max-w-lg mx-auto px-4 py-8 pb-24">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Saved Spots</h1>
        {offline && (
          <span className="text-xs px-2 py-1 bg-amber-100 text-amber-700 rounded-full font-medium">
            Offline
          </span>
        )}
      </div>

      {loading && (
        <div className="text-center py-12 text-gray-400">Loading…</div>
      )}

      {!loading && spots.length === 0 && (
        <div className="text-center py-12 text-gray-400">
          <p className="text-lg mb-2">No saved spots yet.</p>
          <p className="text-sm">
            Open a spot and tap{' '}
            <span className="font-medium text-gray-600">☆ Save</span> to add it here.
            Saved spots are available offline at the trailhead.
          </p>
          <Link
            to="/spots"
            className="mt-4 inline-block px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
          >
            Browse spots
          </Link>
        </div>
      )}

      <div className="space-y-2">
        {spots.map(spot => (
          <button
            key={spot.spot_id}
            onClick={() => navigate(`/spots/${spot.spot_id}`)}
            className="w-full text-left bg-white border border-gray-100 rounded-lg px-4 py-3 hover:bg-gray-50 transition-colors"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="font-medium text-gray-900 truncate">{spot.name}</div>
                <div className="flex items-center gap-2 mt-1 text-xs text-gray-500">
                  <span className="capitalize">{spot.type}</span>
                  {spot.county && <span>· {spot.county} County</span>}
                  {spot.fly_fishing_legal === false && (
                    <span className="text-red-600 font-medium">Bait only</span>
                  )}
                </div>
                <div className="text-xs text-gray-400 mt-0.5">
                  Saved {new Date(spot.saved_at).toLocaleDateString()}
                </div>
              </div>
              <div className="shrink-0 text-right">
                <div
                  className="text-lg font-bold"
                  style={{ color: scoreColor(spot.score) }}
                >
                  {spot.score != null ? spot.score.toFixed(1) : '—'}
                </div>
                {spot.has_realtime_conditions && (
                  <div className="text-xs text-purple-500 mt-0.5">Live</div>
                )}
              </div>
            </div>
          </button>
        ))}
      </div>

      {/* Bottom nav */}
      <div
        className="fixed bottom-0 left-0 right-0 border-t border-gray-200 bg-white flex"
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      >
        <Link to="/trips" className="flex-1 py-3 text-center text-sm font-medium text-gray-500 hover:text-gray-700">
          Trips
        </Link>
        <Link to="/spots" className="flex-1 py-3 text-center text-sm font-medium text-gray-500 hover:text-gray-700">
          Spots
        </Link>
        <Link to="/spots/saved" className="flex-1 py-3 text-center text-sm font-medium text-purple-600">
          Saved
        </Link>
        <Link to="/notes" className="flex-1 py-3 text-center text-sm font-medium text-gray-500 hover:text-gray-700">
          Notes
        </Link>
        <Link to="/settings" className="flex-1 py-3 text-center text-sm font-medium text-gray-500 hover:text-gray-700">
          Settings
        </Link>
      </div>
    </div>
  )
}
