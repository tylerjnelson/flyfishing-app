import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { CircleMarker, MapContainer, Popup, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import api from '../api/client'

// Washington state center and default zoom
const WA_CENTER = [47.5, -120.5]
const WA_ZOOM = 7

function scoreColor(score) {
  if (score === null || score === undefined) return '#9ca3af'
  if (score >= 8) return '#16a34a'
  if (score >= 5) return '#ca8a04'
  if (score >= 2) return '#ea580c'
  return '#9ca3af'
}

function TypeBadge({ type }) {
  const colors = {
    river: 'bg-blue-100 text-blue-800',
    creek: 'bg-cyan-100 text-cyan-800',
    lake: 'bg-indigo-100 text-indigo-800',
    coastal: 'bg-teal-100 text-teal-800',
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${colors[type] ?? 'bg-gray-100 text-gray-700'}`}>
      {type}
    </span>
  )
}

function ConfidenceBadge({ confidence }) {
  const colors = {
    confirmed: 'bg-blue-600 text-white',
    probable: 'bg-teal-500 text-white',
    unvalidated: 'bg-gray-300 text-gray-700',
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${colors[confidence] ?? 'bg-gray-200 text-gray-600'}`}>
      {confidence}
    </span>
  )
}

export default function Spots() {
  const navigate = useNavigate()
  const [spots, setSpots] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [flyOnly, setFlyOnly] = useState(false)
  const [highlightId, setHighlightId] = useState(null)
  const searchTimer = useRef(null)

  const fetchSpots = useCallback(async (q, type, fly) => {
    setLoading(true)
    setError('')
    try {
      let data
      if (q && q.length >= 2) {
        const res = await api.get('/spots/search', { params: { q, limit: 50 } })
        data = res.data.spots
      } else {
        const params = { limit: 200 }
        if (type) params.type = type
        if (fly) params.fly_only = true
        const res = await api.get('/spots', { params })
        data = res.data.spots
      }
      setSpots(data)
    } catch {
      setError('Failed to load spots.')
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial load
  useEffect(() => {
    fetchSpots('', typeFilter, flyOnly)
  }, [typeFilter, flyOnly]) // eslint-disable-line react-hooks/exhaustive-deps

  // Debounced search
  useEffect(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current)
    if (!search) {
      fetchSpots('', typeFilter, flyOnly)
      return
    }
    searchTimer.current = setTimeout(() => fetchSpots(search, typeFilter, flyOnly), 300)
    return () => clearTimeout(searchTimer.current)
  }, [search]) // eslint-disable-line react-hooks/exhaustive-deps

  const mappable = spots.filter(s => s.latitude && s.longitude)

  return (
    <div className="flex flex-col h-screen">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 bg-white">
        <div className="max-w-6xl mx-auto flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold text-gray-900 mr-2">Spots</h1>

          <input
            type="search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search by name…"
            className="flex-1 min-w-[160px] max-w-xs px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />

          <select
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
            className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
          >
            <option value="">All types</option>
            <option value="river">River</option>
            <option value="creek">Creek</option>
            <option value="lake">Lake</option>
            <option value="coastal">Coastal</option>
          </select>

          <label className="flex items-center gap-1.5 text-sm text-gray-700 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={flyOnly}
              onChange={e => setFlyOnly(e.target.checked)}
              className="accent-blue-600"
            />
            Fly legal only
          </label>

          <span className="ml-auto text-xs text-gray-400">
            {loading ? 'Loading…' : `${spots.length} spot${spots.length !== 1 ? 's' : ''}`}
          </span>
        </div>
      </div>

      {error && (
        <div className="px-4 py-2 bg-red-50 text-red-700 text-sm">{error}</div>
      )}

      {/* Body: map + list */}
      <div className="flex flex-col lg:flex-row flex-1 overflow-hidden">
        {/* Map */}
        <div className="h-64 lg:h-full lg:flex-1 relative">
          <MapContainer
            center={WA_CENTER}
            zoom={WA_ZOOM}
            style={{ height: '100%', width: '100%' }}
          >
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
              url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            />
            {mappable.map(spot => (
              <CircleMarker
                key={spot.id}
                center={[spot.latitude, spot.longitude]}
                radius={highlightId === spot.id ? 10 : 7}
                pathOptions={{
                  color: scoreColor(spot.score),
                  fillColor: scoreColor(spot.score),
                  fillOpacity: 0.85,
                  weight: highlightId === spot.id ? 2 : 1,
                }}
                eventHandlers={{
                  click: () => navigate(`/spots/${spot.id}`),
                  mouseover: () => setHighlightId(spot.id),
                  mouseout: () => setHighlightId(null),
                }}
              >
                <Popup>
                  <div className="text-sm">
                    <div className="font-medium">{spot.name}</div>
                    <div className="text-gray-500">{spot.type} · {spot.county}</div>
                    <button
                      onClick={() => navigate(`/spots/${spot.id}`)}
                      className="text-blue-600 underline mt-1 block"
                    >
                      View detail
                    </button>
                  </div>
                </Popup>
              </CircleMarker>
            ))}
          </MapContainer>
        </div>

        {/* List */}
        <div className="lg:w-96 overflow-y-auto divide-y divide-gray-100 bg-white border-l border-gray-200">
          {loading && (
            <div className="flex items-center justify-center py-12 text-gray-400 text-sm">
              Loading…
            </div>
          )}
          {!loading && spots.length === 0 && (
            <div className="flex items-center justify-center py-12 text-gray-400 text-sm">
              No spots found
            </div>
          )}
          {spots.map(spot => (
            <button
              key={spot.id}
              onClick={() => navigate(`/spots/${spot.id}`)}
              onMouseEnter={() => setHighlightId(spot.id)}
              onMouseLeave={() => setHighlightId(null)}
              className={`w-full text-left px-4 py-3 hover:bg-gray-50 transition-colors ${
                highlightId === spot.id ? 'bg-blue-50' : ''
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="font-medium text-gray-900 truncate">{spot.name}</div>
                  <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                    <TypeBadge type={spot.type} />
                    <ConfidenceBadge confidence={spot.seed_confidence} />
                    {spot.fly_fishing_legal === false && (
                      <span className="px-2 py-0.5 bg-red-100 text-red-700 rounded text-xs font-medium">
                        Bait only
                      </span>
                    )}
                  </div>
                  {spot.county && (
                    <div className="text-xs text-gray-400 mt-0.5">{spot.county} County</div>
                  )}
                </div>
                <div className="flex flex-col items-end shrink-0">
                  <span
                    className="text-sm font-semibold"
                    style={{ color: scoreColor(spot.score) }}
                  >
                    {spot.score != null ? spot.score.toFixed(1) : '—'}
                  </span>
                  <span className="text-xs text-gray-400">score</span>
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
