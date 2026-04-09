/**
 * Trip list — primary navigation (§10.3).
 *
 * Groups trips into three sections for the sidebar-style layout:
 *   UPCOMING     — PLANNED + IMMINENT + IN_WINDOW (IMMINENT highlighted)
 *   NEEDS DEBRIEF — POST_TRIP (dot indicator)
 *   PAST TRIPS   — DEBRIEFED
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api from '../api/client'

const STATE_LABELS = {
  PLANNED: 'Planned',
  IMMINENT: 'Soon',
  IN_WINDOW: 'On Trip',
  POST_TRIP: 'Needs Debrief',
  DEBRIEFED: 'Archived',
}

function TripRow({ trip }) {
  const date = trip.departure_time
    ? new Date(trip.departure_time).toLocaleDateString('en-US', {
        weekday: 'short', month: 'short', day: 'numeric',
      })
    : trip.trip_date || '—'

  const spotLabel = trip.spot_name || trip.session_intake?.water_type?.join(', ') || 'New trip'

  return (
    <Link
      to={`/trips/${trip.id}`}
      className={`flex items-center justify-between px-4 py-3 rounded-lg transition-colors
        ${trip.highlight
          ? 'bg-blue-50 border border-blue-200 hover:bg-blue-100'
          : 'bg-white border border-gray-100 hover:bg-gray-50'}`}
    >
      <div className="flex items-center gap-2 min-w-0">
        {trip.needs_debrief_dot && (
          <span className="w-2 h-2 rounded-full bg-amber-500 shrink-0" />
        )}
        <div className="min-w-0">
          <p className={`text-sm font-medium truncate ${trip.highlight ? 'text-blue-900' : 'text-gray-900'}`}>
            {spotLabel}
          </p>
          <p className="text-xs text-gray-500">{date}</p>
        </div>
      </div>
      <span className={`text-xs shrink-0 ml-2 ${trip.highlight ? 'text-blue-600' : 'text-gray-400'}`}>
        {STATE_LABELS[trip.state] || trip.state}
      </span>
    </Link>
  )
}

function Section({ title, trips }) {
  if (!trips || trips.length === 0) return null
  return (
    <div className="mb-6">
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-2 px-1">
        {title}
      </p>
      <div className="space-y-2">
        {trips.map(t => <TripRow key={t.id} trip={t} />)}
      </div>
    </div>
  )
}

export default function Trips() {
  const [grouped, setGrouped] = useState({ upcoming: [], needs_debrief: [], past: [] })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.get('/trips')
      .then(({ data }) => setGrouped(data))
      .catch(() => setError('Failed to load trips'))
      .finally(() => setLoading(false))
  }, [])

  const total = grouped.upcoming.length + grouped.needs_debrief.length + grouped.past.length

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Trips</h1>
      </div>

      {/* Plan a new trip — always at top */}
      <Link
        to="/trips/new"
        className="flex items-center justify-center gap-2 w-full px-4 py-3 mb-6
          bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors"
      >
        + Plan a new trip
      </Link>

      {loading && (
        <div className="text-center py-12 text-gray-400">Loading…</div>
      )}
      {error && (
        <div className="text-center py-12 text-red-500">{error}</div>
      )}

      {!loading && !error && total === 0 && (
        <div className="text-center py-12 text-gray-400">
          No trips yet. Plan your first one above.
        </div>
      )}

      {!loading && !error && (
        <>
          <Section title="Upcoming" trips={grouped.upcoming} />
          <Section title="Needs Debrief" trips={grouped.needs_debrief} />
          <Section title="Past Trips" trips={grouped.past} />
        </>
      )}

      {/* Bottom nav */}
      <div className="fixed bottom-0 left-0 right-0 border-t border-gray-200 bg-white flex">
        <Link to="/trips" className="flex-1 py-3 text-center text-sm font-medium text-blue-600">
          Trips
        </Link>
        <Link to="/spots" className="flex-1 py-3 text-center text-sm font-medium text-gray-500 hover:text-gray-700">
          Spots
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
