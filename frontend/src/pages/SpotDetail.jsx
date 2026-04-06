import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { CircleMarker, MapContainer, TileLayer } from 'react-leaflet'
import 'leaflet/dist/leaflet.css'
import api from '../api/client'

function scoreColor(score) {
  if (score === null || score === undefined) return '#9ca3af'
  if (score >= 8) return '#16a34a'
  if (score >= 5) return '#ca8a04'
  if (score >= 2) return '#ea580c'
  return '#9ca3af'
}

function InfoRow({ label, value }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <div className="flex justify-between py-2 border-b border-gray-100 text-sm">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-900 font-medium text-right max-w-xs">{value}</span>
    </div>
  )
}

function RegsSection({ regs }) {
  if (!regs) return null
  return (
    <div className="mt-6">
      <h2 className="text-base font-semibold text-gray-900 mb-2">Fishing Regulations</h2>
      <div className="bg-gray-50 rounded-lg p-4 space-y-0.5">
        <InfoRow label="Open dates" value={regs.open_dates} />
        <InfoRow label="Gear" value={regs.gear} />
        <InfoRow label="Size limits" value={regs.size_limits} />
        <InfoRow label="Bag limits" value={regs.bag_limits} />
        <InfoRow label="Special rules" value={regs.special_rules} />
        {regs.year_round_closed && (
          <div className="pt-2 text-sm text-red-700 font-medium">Year-round closed</div>
        )}
      </div>
    </div>
  )
}

export default function SpotDetail() {
  const { spotId } = useParams()
  const navigate = useNavigate()
  const [spot, setSpot] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    setLoading(true)
    api
      .get(`/spots/${spotId}`)
      .then(({ data }) => setSpot(data))
      .catch(() => setError('Spot not found.'))
      .finally(() => setLoading(false))
  }, [spotId])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-gray-400 text-sm">
        Loading…
      </div>
    )
  }

  if (error || !spot) {
    return (
      <div className="max-w-lg mx-auto px-4 py-8">
        <button onClick={() => navigate('/spots')} className="text-blue-600 text-sm mb-4 hover:underline">
          ← Back to spots
        </button>
        <p className="text-red-600">{error || 'Spot not found.'}</p>
      </div>
    )
  }

  const hasCoords = spot.latitude != null && spot.longitude != null
  const activeClosures = spot.emergency_closures ?? []

  return (
    <div className="max-w-2xl mx-auto px-4 py-6">
      {/* Back */}
      <button
        onClick={() => navigate('/spots')}
        className="text-blue-600 text-sm mb-4 hover:underline"
      >
        ← Back to spots
      </button>

      {/* Active closure banner */}
      {activeClosures.length > 0 && (
        <div className="mb-4 rounded-lg bg-red-50 border border-red-200 p-4">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-red-700 font-semibold text-sm">Active Closure</span>
          </div>
          {activeClosures.map((c, i) => (
            <div key={i} className="text-sm text-red-700 mb-1">
              <p>{c.rule_text}</p>
              {(c.effective || c.expires) && (
                <p className="text-red-500 text-xs mt-0.5">
                  {c.effective && `Effective: ${c.effective}`}
                  {c.effective && c.expires && ' · '}
                  {c.expires && `Expires: ${c.expires}`}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Header */}
      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900">{spot.name}</h1>
          <div className="flex items-center gap-2 mt-1.5 flex-wrap">
            <span className="text-sm text-gray-500 capitalize">{spot.type}</span>
            {spot.county && (
              <span className="text-sm text-gray-400">· {spot.county} County</span>
            )}
            <span
              className={`px-2 py-0.5 rounded text-xs font-medium ${
                spot.seed_confidence === 'confirmed'
                  ? 'bg-blue-600 text-white'
                  : spot.seed_confidence === 'probable'
                  ? 'bg-teal-500 text-white'
                  : 'bg-gray-200 text-gray-600'
              }`}
            >
              {spot.seed_confidence}
            </span>
          </div>
        </div>

        {/* Score */}
        <div className="shrink-0 text-center">
          <div
            className="text-3xl font-bold"
            style={{ color: scoreColor(spot.score) }}
          >
            {spot.score != null ? spot.score.toFixed(1) : '—'}
          </div>
          <div className="text-xs text-gray-400">
            {spot.score_updated
              ? `Scored ${new Date(spot.score_updated).toLocaleDateString()}`
              : 'Not scored yet'}
          </div>
        </div>
      </div>

      {/* Fishability row */}
      <div className="flex flex-wrap gap-2 mb-6">
        {spot.fly_fishing_legal === false ? (
          <span className="px-3 py-1 bg-red-100 text-red-700 rounded-full text-sm font-medium">
            Bait only — fly fishing not legal
          </span>
        ) : (
          <span className="px-3 py-1 bg-green-100 text-green-800 rounded-full text-sm font-medium">
            Fly fishing legal
          </span>
        )}
        {spot.min_cfs != null && spot.max_cfs != null && (
          <span className="px-3 py-1 bg-blue-50 text-blue-700 rounded-full text-sm">
            {spot.min_cfs}–{spot.max_cfs} CFS ideal
          </span>
        )}
        {spot.max_temp_f != null && (
          <span className="px-3 py-1 bg-orange-50 text-orange-700 rounded-full text-sm">
            Max {spot.max_temp_f}°F
          </span>
        )}
        {spot.has_realtime_conditions && (
          <span className="px-3 py-1 bg-purple-50 text-purple-700 rounded-full text-sm">
            Live conditions available
          </span>
        )}
      </div>

      {/* Details */}
      <div className="bg-gray-50 rounded-lg px-4 mb-6">
        <InfoRow label="Species" value={spot.species_primary?.join(', ')} />
        <InfoRow label="Elevation" value={spot.elevation_ft ? `${spot.elevation_ft} ft` : null} />
        <InfoRow label="Alpine lake" value={spot.is_alpine ? 'Yes' : null} />
        <InfoRow label="Public access" value={spot.is_public === false ? 'Private' : null} />
        <InfoRow label="Permit required" value={spot.permit_required ? 'Yes' : null} />
        {spot.permit_required && spot.permit_url && (
          <div className="py-2 border-b border-gray-100 text-sm">
            <span className="text-gray-500">Permit info </span>
            <a href={spot.permit_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">
              link
            </a>
          </div>
        )}
        <InfoRow
          label="Last stocked"
          value={spot.last_stocked_date
            ? `${spot.last_stocked_date}${spot.last_stocked_species?.length ? ` (${spot.last_stocked_species.join(', ')})` : ''}`
            : null}
        />
        {spot.wta_trail_url && (
          <div className="py-2 border-b border-gray-100 text-sm">
            <span className="text-gray-500">WTA reports </span>
            <a href={spot.wta_trail_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">
              view on WTA
            </a>
          </div>
        )}
        {spot.aliases?.length > 0 && (
          <InfoRow label="Also known as" value={spot.aliases.join(', ')} />
        )}
      </div>

      {/* Regulations */}
      <RegsSection regs={spot.fishing_regs} />

      {/* Map */}
      {hasCoords && (
        <div className="mt-6">
          <h2 className="text-base font-semibold text-gray-900 mb-2">Location</h2>
          <div className="rounded-lg overflow-hidden border border-gray-200" style={{ height: 240 }}>
            <MapContainer
              center={[spot.latitude, spot.longitude]}
              zoom={11}
              style={{ height: '100%', width: '100%' }}
              zoomControl={true}
              scrollWheelZoom={false}
            >
              <TileLayer
                attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
                url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
              />
              <CircleMarker
                center={[spot.latitude, spot.longitude]}
                radius={9}
                pathOptions={{
                  color: scoreColor(spot.score),
                  fillColor: scoreColor(spot.score),
                  fillOpacity: 0.9,
                  weight: 2,
                }}
              />
            </MapContainer>
          </div>
          <p className="text-xs text-gray-400 mt-1">
            {spot.latitude.toFixed(4)}°N, {Math.abs(spot.longitude).toFixed(4)}°W
          </p>
        </div>
      )}
    </div>
  )
}
