/**
 * Notes corpus browser.
 * Shows all notes for the current user, grouped: handwritten notes display
 * their extracted map child entries inline.
 */

import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import axios from 'axios'
import useAuthStore from '../store/auth'

const SOURCE_LABELS = {
  handwritten: 'Handwritten',
  typed: 'Typed',
  map: 'Map',
  debrief: 'Debrief',
}

const OUTCOME_COLORS = {
  positive: 'text-green-600',
  neutral: 'text-gray-500',
  negative: 'text-red-500',
}

function PendingBadge({ flags }) {
  if (!flags || flags.length === 0) return null
  return (
    <span className="text-xs bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded ml-2">
      {flags.includes('awaiting_date_confirmation') && 'Date needed '}
      {flags.includes('awaiting_spot_confirmation') && 'Spot needed'}
    </span>
  )
}

function NoteCard({ note, childMap }) {
  const outcomeColor = OUTCOME_COLORS[note.outcome] || 'text-gray-500'
  return (
    <div className="border border-gray-200 rounded-lg p-4 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
              {SOURCE_LABELS[note.source_type] || note.source_type}
            </span>
            {note.note_date && (
              <span className="text-sm text-gray-600">{note.note_date}</span>
            )}
            {note.outcome && (
              <span className={`text-xs font-medium capitalize ${outcomeColor}`}>
                {note.outcome}
              </span>
            )}
            <PendingBadge flags={note.pending_flags} />
          </div>

          {note.species && note.species.length > 0 && (
            <p className="mt-1 text-sm text-gray-700">
              <span className="font-medium">Species:</span>{' '}
              {note.species.join(', ')}
            </p>
          )}

          {note.flies && note.flies.length > 0 && (
            <p className="text-sm text-gray-600">
              <span className="font-medium">Flies:</span>{' '}
              {note.flies.join(', ')}
            </p>
          )}
        </div>

        <div className="flex flex-col items-end gap-1 shrink-0">
          {note.has_image && (
            <NoteImage noteId={note.id} />
          )}
        </div>
      </div>

      {/* Extracted map child note */}
      {childMap && (
        <div className="mt-3 pt-3 border-t border-gray-100">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-blue-600 uppercase tracking-wide">
              Extracted Map
            </span>
            {childMap.pending_flags?.includes('low_quality_scan') && (
              <span className="text-xs text-amber-600">
                Low quality — consider re-uploading
              </span>
            )}
          </div>
          <MapThumb noteId={childMap.id} />
        </div>
      )}

      <div className="mt-2 flex justify-end">
        <Link
          to={`/notes/${note.id}`}
          className="text-xs text-blue-600 hover:underline"
        >
          View →
        </Link>
      </div>
    </div>
  )
}

function NoteImage({ noteId }) {
  return (
    <img
      src={`/api/notes/${noteId}/image`}
      alt="Note"
      className="w-16 h-16 object-cover rounded border border-gray-200"
      onError={e => { e.target.style.display = 'none' }}
    />
  )
}

function MapThumb({ noteId }) {
  return (
    <img
      src={`/api/notes/${noteId}/image`}
      alt="Extracted map"
      className="mt-1 w-full max-h-48 object-contain rounded border border-gray-100"
      onError={e => { e.target.style.display = 'none' }}
    />
  )
}

export default function Notes() {
  const { accessToken } = useAuthStore()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [notes, setNotes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [sourceFilter, setSourceFilter] = useState(searchParams.get('source') || '')

  const headers = { Authorization: `Bearer ${accessToken}` }

  useEffect(() => {
    setLoading(true)
    const params = { limit: 100 }
    if (sourceFilter) params.source_type = sourceFilter

    axios
      .get('/api/notes', { headers, params })
      .then(({ data }) => {
        setNotes(data.notes)
        setError(null)
      })
      .catch(() => setError('Failed to load notes'))
      .finally(() => setLoading(false))
  }, [sourceFilter])

  // Build map from parent_note_id → child map note
  const childMapByParent = {}
  for (const note of notes) {
    if (note.source_type === 'map' && note.parent_note_id) {
      childMapByParent[note.parent_note_id] = note
    }
  }

  // Top-level notes (not child map records)
  const topLevel = notes.filter(
    n => !(n.source_type === 'map' && n.parent_note_id)
  )

  const handleSourceFilter = (val) => {
    setSourceFilter(val)
    if (val) setSearchParams({ source: val })
    else setSearchParams({})
  }

  return (
    <div className="max-w-3xl mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Notes</h1>
        <Link
          to="/notes/upload"
          className="bg-blue-600 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          + Add Note
        </Link>
      </div>

      {/* Filters */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {['', 'typed', 'handwritten', 'map'].map(type => (
          <button
            key={type}
            onClick={() => handleSourceFilter(type)}
            className={`px-3 py-1 rounded-full text-sm border transition-colors ${
              sourceFilter === type
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
            }`}
          >
            {type === '' ? 'All' : SOURCE_LABELS[type] || type}
          </button>
        ))}
      </div>

      {loading && (
        <div className="text-center py-12 text-gray-400">Loading notes…</div>
      )}
      {error && (
        <div className="text-center py-12 text-red-500">{error}</div>
      )}

      {!loading && !error && topLevel.length === 0 && (
        <div className="text-center py-12 text-gray-400">
          No notes yet.{' '}
          <Link to="/notes/upload" className="text-blue-600 hover:underline">
            Upload your first note.
          </Link>
        </div>
      )}

      <div className="space-y-4">
        {topLevel.map(note => (
          <NoteCard
            key={note.id}
            note={note}
            childMap={childMapByParent[note.id] || null}
          />
        ))}
      </div>
    </div>
  )
}
