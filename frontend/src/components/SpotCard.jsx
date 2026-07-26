/**
 * SpotCard — Phase 2 turn builder output.
 *
 * Renders structured spot data assembled by the backend turn builder.
 * Receives a single card object from the `spot_cards` SSE event.
 *
 * Props:
 *   card           — spot card object from turn builder
 *   conversationId — current conversation ID (for exclude-spot API call)
 *   onExcluded     — callback(spotId) called after successful exclusion
 */

import { useState } from 'react'
import api from '../api/client'

const TYPE_LABELS = {
  river: 'River',
  lake: 'Lake',
  creek: 'Creek',
  coastal: 'Coastal',
  pond: 'Pond',
}

function formatDriveTime(card) {
  if (!card.is_haversine && card.drive_minutes) {
    return { text: `${card.drive_minutes} min drive`, estimated: false }
  }
  if (card.is_haversine && card.straight_line_miles) {
    return { text: `~${Math.round(card.straight_line_miles)} mi`, estimated: true }
  }
  return null
}

function CfsDisplay({ cfs, trend }) {
  const arrow = trend === 'rising' ? ' ↑' : trend === 'dropping' ? ' ↓' : ''
  return (
    <span>
      {Math.round(cfs).toLocaleString()} CFS
      {arrow && <span className="text-gray-400">{arrow}</span>}
    </span>
  )
}

export default function SpotCard({ card, conversationId, onExcluded, onLocked, lockedFishingSpotId }) {
  const [skipping, setSkipping] = useState(false)
  const [skipped, setSkipped] = useState(false)
  const [locking, setLocking] = useState(false)
  const [lockedLocal, setLockedLocal] = useState(false)

  // This card is the trip's committed spot if either we just locked it, or the
  // trip already carries this card's FishingSpot UUID (persists across reloads).
  const isLocked =
    lockedLocal ||
    (!!lockedFishingSpotId && !!card.fishing_spot_id && lockedFishingSpotId === card.fishing_spot_id)

  async function handleSkip() {
    if (skipping || skipped || !conversationId) return
    setSkipping(true)
    try {
      await api.post('/chat/exclude-spot', {
        conversation_id: conversationId,
        spot_id: card.spot_id,
      })
      setSkipped(true)
      onExcluded?.(card.spot_id)
    } catch {
      // Silent — card stays visible, user can retry
    } finally {
      setSkipping(false)
    }
  }

  async function handleLock() {
    if (locking || isLocked || !conversationId) return
    setLocking(true)
    try {
      const { data } = await api.post('/chat/commit-spot', {
        conversation_id: conversationId,
        spot_id: card.spot_id,
      })
      setLockedLocal(true)
      onLocked?.(data.fishing_spot_id)
    } catch {
      // Silent — card stays interactive, user can retry
    } finally {
      setLocking(false)
    }
  }

  const typeLabel = TYPE_LABELS[card.spot_type] || card.spot_type
  const drive = formatDriveTime(card)
  const cond = card.conditions || {}
  const regs = card.fishing_regs || {}

  const showName = card.name !== card.water_body_name
  const gearNote = regs.gear || null
  const openDates = regs.open_dates || null
  const yearRoundClosed = regs.year_round_closed === true

  if (skipped) return null

  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3 mb-2">

      {/* Header */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-gray-900 leading-tight truncate">{card.name}</p>
          {showName && (
            <p className="text-xs text-gray-400 truncate">{card.water_body_name}</p>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
          {typeLabel && (
            <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">
              {typeLabel}
            </span>
          )}
          {conversationId && (
            <button
              onClick={handleSkip}
              disabled={skipping}
              className="text-xs text-gray-400 hover:text-gray-600 disabled:opacity-40 px-1"
              title="Skip this spot"
            >
              {skipping ? '…' : '✕'}
            </button>
          )}
        </div>
      </div>

      {/* Drive time */}
      {drive && (
        <p className="text-xs text-gray-600 mb-2">
          {drive.text}
          {drive.estimated && (
            <span className="text-amber-600"> (estimated)</span>
          )}
        </p>
      )}

      {/* Conditions row */}
      {(cond.cfs != null || cond.water_temp_f != null || cond.weather_summary) && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-600 mb-2">
          {cond.cfs != null && (
            <CfsDisplay cfs={cond.cfs} trend={cond.cfs_trend} />
          )}
          {cond.water_temp_f != null && (
            <span>{cond.water_temp_f.toFixed(1)}°F water</span>
          )}
          {cond.weather_summary && (
            <span>
              {cond.weather_summary}
              {cond.air_temp_f != null && `, ${Math.round(cond.air_temp_f)}°F`}
            </span>
          )}
          {cond.aqi != null && cond.aqi > 50 && (
            <span className="text-amber-600">AQI {cond.aqi}</span>
          )}
        </div>
      )}

      {/* Tags row: regulations, stocking, notes */}
      {(gearNote || openDates || yearRoundClosed || card.last_stocked_date || card.note_count > 0) && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500 mb-1">
          {yearRoundClosed && (
            <span className="text-red-600 font-medium">Closed year-round</span>
          )}
          {gearNote && !yearRoundClosed && (
            <span>{gearNote}</span>
          )}
          {openDates && !yearRoundClosed && (
            <span>Open: {openDates}</span>
          )}
          {card.last_stocked_date && (
            <span>Stocked {card.last_stocked_date}</span>
          )}
          {card.note_count > 0 && (
            <span>{card.note_count} group {card.note_count === 1 ? 'note' : 'notes'}</span>
          )}
        </div>
      )}

      {/* Warnings */}
      {card.warnings?.length > 0 && (
        <div className="mt-2 space-y-1">
          {card.warnings.map((w, i) => (
            <p key={i} className="text-xs text-amber-700 bg-amber-50 rounded px-2 py-1">{w}</p>
          ))}
        </div>
      )}

      {/* Lock this spot — commit flow */}
      {conversationId && (
        <div className="mt-2 pt-2 border-t border-gray-100">
          {isLocked ? (
            <span className="inline-flex items-center gap-1 text-xs font-medium text-green-700">
              <span aria-hidden="true">✓</span> Locked in for this trip
            </span>
          ) : (
            <button
              onClick={handleLock}
              disabled={locking}
              className="text-xs font-medium text-blue-600 hover:text-blue-800 disabled:opacity-40"
              title="Lock this spot in as the trip's chosen spot"
            >
              {locking ? 'Locking…' : '📍 Lock this spot'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
