/**
 * Trip conversation thread — §10.2.
 *
 * Full planning/IN_WINDOW conversation with streaming SSE from /api/chat.
 * Handles:
 *   - Token-by-token streaming via fetch + ReadableStream
 *   - filter_confirmation_required → Yes/No confirmation card
 *   - drive_time_unavailable → persistent banner
 *   - IN_WINDOW mode → note-logging nudge on assistant responses
 *   - Map images surfaced for top recommended spots
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import api from '../api/client'
import useAuthStore from '../store/auth'

const STATE_LABELS = {
  PLANNED: 'Planning',
  IMMINENT: 'Heads up — trip is soon',
  IN_WINDOW: 'On Trip',
  POST_TRIP: 'Trip complete',
  DEBRIEFED: 'Archived',
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DriveTimeBanner() {
  return (
    <div className="mx-4 mt-3 px-3 py-2 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
      Drive time data unavailable — verify drive times before heading out.
      Distances shown are straight-line estimates.
    </div>
  )
}

function FilterConfirmCard({ event, onConfirm, onReject, loading }) {
  const label = event.key === 'departure_location'
    ? `Update starting location to "${event.value}"?`
    : event.key === 'max_drive_minutes'
      ? `Narrow results to ${event.value} min drive?`
      : `Update ${event.key} to "${event.value}"?`

  return (
    <div className="mx-4 my-2 px-4 py-3 bg-blue-50 border border-blue-200 rounded-lg">
      <p className="text-sm text-blue-900 font-medium mb-3">{label}</p>
      <div className="flex gap-2">
        <button
          onClick={onConfirm}
          disabled={loading}
          className="flex-1 px-3 py-2 bg-blue-600 text-white text-sm rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? 'Updating…' : 'Yes'}
        </button>
        <button
          onClick={onReject}
          disabled={loading}
          className="flex-1 px-3 py-2 border border-gray-300 text-gray-700 text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          No
        </button>
      </div>
    </div>
  )
}

function InWindowNudge() {
  return (
    <div className="mx-4 my-1 px-3 py-2 bg-green-50 border border-green-200 rounded-lg text-xs text-green-800">
      Tip: save detailed notes (flies, fish caught, conditions) for your debrief after the trip.
    </div>
  )
}

function MessageBubble({ msg, isStreaming }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-3 px-4`}>
      <div
        className={`max-w-[85%] px-4 py-3 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap break-words ${
          isUser
            ? 'bg-blue-600 text-white rounded-br-sm'
            : 'bg-white border border-gray-200 text-gray-800 rounded-bl-sm shadow-sm'
        }`}
      >
        {msg.content}
        {isStreaming && (
          <span className="inline-block w-2 h-4 ml-0.5 bg-gray-400 animate-pulse rounded-sm align-text-bottom" />
        )}
      </div>
    </div>
  )
}

function MapThumbs({ spotIds }) {
  const [maps, setMaps] = useState([])

  useEffect(() => {
    if (!spotIds || spotIds.length === 0) return
    // Fetch map notes for the top recommended spots
    const params = new URLSearchParams({ source_type: 'map', limit: 10 })
    api.get(`/notes?${params}`).then(({ data }) => {
      const relevant = (data.notes || []).filter(
        n => spotIds.includes(n.spot_id)
      )
      setMaps(relevant)
    }).catch(() => {})
  }, [spotIds?.join(',')])

  if (maps.length === 0) return null

  return (
    <div className="px-4 mb-3">
      <p className="text-xs text-gray-400 mb-1">Hand-drawn maps</p>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {maps.map(m => (
          <img
            key={m.id}
            src={`/api/notes/${m.id}/map`}
            alt="Field map"
            className="h-24 w-auto rounded border border-gray-200 shrink-0"
            onError={e => { e.target.style.display = 'none' }}
          />
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function TripThread() {
  const { tripId } = useParams()
  const navigate = useNavigate()
  const { accessToken } = useAuthStore()

  const [trip, setTrip] = useState(null)
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [topSpotIds, setTopSpotIds] = useState([])
  const [driveTimeUnavailable, setDriveTimeUnavailable] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')

  const [pendingFilter, setPendingFilter] = useState(null)   // {key, value}
  const [confirmLoading, setConfirmLoading] = useState(false)

  const [showInWindowNudge, setShowInWindowNudge] = useState(false)

  const bottomRef = useRef(null)
  const inputRef = useRef(null)

  // ------------------------------------------------------------------
  // Load trip
  // ------------------------------------------------------------------
  useEffect(() => {
    api.get(`/trips/${tripId}`)
      .then(({ data }) => {
        setTrip(data.trip)
        setConversationId(data.conversation_id)
        setMessages(data.messages || [])
        setDriveTimeUnavailable(data.drive_time_unavailable || false)

        const candidates = data.session_candidates || []
        setTopSpotIds(candidates.slice(0, 5).map(c => c.spot_id).filter(Boolean))
      })
      .catch(() => setError('Failed to load trip'))
      .finally(() => setLoading(false))
  }, [tripId])

  // Scroll to bottom on new messages / streaming
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  // ------------------------------------------------------------------
  // Send message + stream response
  // ------------------------------------------------------------------
  const send = useCallback(async () => {
    const text = input.trim()
    if (!text || streaming || !conversationId) return

    setInput('')
    setStreaming(true)
    setStreamingContent('')
    setPendingFilter(null)
    setShowInWindowNudge(false)

    // Optimistically add user message
    const userMsg = { id: Date.now(), role: 'user', content: text }
    setMessages(prev => [...prev, userMsg])

    try {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ conversation_id: conversationId, message: text }),
      })

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let accumulated = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() // keep incomplete last chunk

        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data: ')) continue
            let event
            try {
              event = JSON.parse(line.slice(6))
            } catch {
              continue
            }

            if (event.type === 'token') {
              accumulated += event.content
              setStreamingContent(accumulated)
            } else if (event.type === 'drive_time_unavailable') {
              setDriveTimeUnavailable(true)
            } else if (event.type === 'filter_confirmation_required') {
              setPendingFilter({ key: event.key, value: event.value })
            } else if (event.type === 'done') {
              // Commit streamed message to history
              if (accumulated) {
                setMessages(prev => [
                  ...prev,
                  { id: Date.now() + 1, role: 'assistant', content: accumulated },
                ])
                if (trip?.state === 'IN_WINDOW') {
                  setShowInWindowNudge(true)
                }
              }
              setStreamingContent('')
            }
          }
        }
      }
    } catch (err) {
      setMessages(prev => [
        ...prev,
        { id: Date.now() + 1, role: 'assistant', content: 'Something went wrong. Please try again.' },
      ])
    } finally {
      setStreaming(false)
      setStreamingContent('')
      inputRef.current?.focus()
    }
  }, [input, streaming, conversationId, accessToken, trip?.state])

  // ------------------------------------------------------------------
  // Filter confirmation
  // ------------------------------------------------------------------
  async function handleConfirmFilter(confirm) {
    setConfirmLoading(true)
    try {
      const { data } = await api.post('/chat/confirm-filter', {
        conversation_id: conversationId,
        confirm,
      })
      if (confirm && data.session_candidates) {
        setTopSpotIds(
          (data.session_candidates || []).slice(0, 5).map(c => c.spot_id).filter(Boolean)
        )
        if (data.drive_time_unavailable) setDriveTimeUnavailable(true)
      }
    } catch {
      // Silent failure — pending_filter clears regardless
    } finally {
      setPendingFilter(null)
      setConfirmLoading(false)
    }
  }

  // ------------------------------------------------------------------
  // Keyboard
  // ------------------------------------------------------------------
  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-gray-400">
        Loading…
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen text-red-500">
        {error}
      </div>
    )
  }

  const stateLabel = trip ? STATE_LABELS[trip.state] || trip.state : ''
  const isInWindow = trip?.state === 'IN_WINDOW'

  return (
    <div className="flex flex-col h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-3 shrink-0">
        <Link to="/trips" className="text-gray-500 hover:text-gray-700 text-lg leading-none">
          ←
        </Link>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-gray-900 truncate">
            {trip?.spot_name || 'Trip Planning'}
          </p>
          <p className={`text-xs ${isInWindow ? 'text-green-600 font-medium' : 'text-gray-500'}`}>
            {stateLabel}
          </p>
        </div>
        <Link
          to="/notes/upload"
          className="text-xs px-3 py-1.5 border border-gray-300 rounded-full text-gray-600 hover:bg-gray-50"
        >
          + Note
        </Link>
      </div>

      {/* Drive-time unavailable banner */}
      {driveTimeUnavailable && <DriveTimeBanner />}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto py-4">
        {messages.length === 0 && !streaming && (
          <div className="text-center py-16 text-gray-400 text-sm px-8">
            {isInWindow
              ? "You're on the water. Ask for a quick spot lookup or log what you're seeing."
              : "Ask for spot recommendations, or tell me what you're looking for."}
          </div>
        )}

        {messages.map(msg => (
          <MessageBubble key={msg.id} msg={msg} isStreaming={false} />
        ))}

        {/* Streaming in-progress */}
        {streaming && streamingContent && (
          <MessageBubble
            msg={{ role: 'assistant', content: streamingContent }}
            isStreaming
          />
        )}
        {streaming && !streamingContent && (
          <div className="flex justify-start mb-3 px-4">
            <div className="bg-white border border-gray-200 rounded-2xl rounded-bl-sm shadow-sm px-4 py-3">
              <div className="flex gap-1">
                <span className="w-2 h-2 rounded-full bg-gray-300 animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-2 h-2 rounded-full bg-gray-300 animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-2 h-2 rounded-full bg-gray-300 animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}

        {/* Filter confirmation card */}
        {pendingFilter && (
          <FilterConfirmCard
            event={pendingFilter}
            onConfirm={() => handleConfirmFilter(true)}
            onReject={() => handleConfirmFilter(false)}
            loading={confirmLoading}
          />
        )}

        {/* IN_WINDOW debrief nudge */}
        {showInWindowNudge && <InWindowNudge />}

        {/* Map thumbnails for top recommended spots */}
        {topSpotIds.length > 0 && messages.length > 0 && (
          <MapThumbs spotIds={topSpotIds} />
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="bg-white border-t border-gray-200 px-4 py-3 shrink-0">
        <div className="flex gap-2 items-end">
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              isInWindow
                ? "Quick lookup or log what you're seeing…"
                : "Ask about spots, conditions, or fishing…"
            }
            rows={1}
            disabled={streaming}
            className="flex-1 px-4 py-2.5 border border-gray-300 rounded-2xl text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50"
            style={{ maxHeight: '120px' }}
            onInput={e => {
              e.target.style.height = 'auto'
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
            }}
          />
          <button
            onClick={send}
            disabled={!input.trim() || streaming}
            className="w-10 h-10 bg-blue-600 text-white rounded-full flex items-center justify-center hover:bg-blue-700 disabled:opacity-40 transition-colors shrink-0"
          >
            <svg className="w-4 h-4 rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  )
}
