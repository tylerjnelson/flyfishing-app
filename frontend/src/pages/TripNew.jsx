/**
 * Session intake card — §8.2.
 *
 * Collects departure/return time, water type, target species, trip goal,
 * and trail difficulty. Merges with user profile preferences server-side.
 * On submit → POST /api/trips → navigate to /trips/:tripId.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'

const STEPS = [
  {
    id: 'times',
    question: 'When are you heading out?',
    type: 'datetime_range',
  },
  {
    id: 'water_type',
    question: 'What type of water are you looking for?',
    type: 'multi_select',
    options: [
      { value: 'river', label: 'River' },
      { value: 'creek', label: 'Creek' },
      { value: 'lake', label: 'Lake' },
      { value: 'saltwater', label: 'Saltwater' },
    ],
  },
  {
    id: 'target_species',
    question: 'What species are you targeting?',
    type: 'multi_select',
    options: [
      { value: 'steelhead', label: 'Steelhead' },
      { value: 'trout', label: 'Trout' },
      { value: 'salmon', label: 'Salmon' },
      { value: 'cutthroat', label: 'Cutthroat' },
      { value: 'bass', label: 'Bass' },
      { value: 'any', label: 'Any' },
    ],
  },
  {
    id: 'trip_goal',
    question: "What's your main goal for this trip?",
    type: 'single_select',
    options: [
      { value: 'maximise_catch', label: 'Maximise catch' },
      { value: 'explore', label: 'Explore new water' },
      { value: 'relax', label: 'Relax / scenic' },
      { value: 'teach', label: 'Teach beginners' },
    ],
  },
  {
    id: 'trail_difficulty',
    question: 'What trail difficulty can you handle?',
    type: 'single_select',
    options: [
      { value: 'easy', label: 'Easy' },
      { value: 'moderate', label: 'Moderate' },
      { value: 'strenuous', label: 'Strenuous' },
      { value: 'any', label: 'Any' },
    ],
  },
]

export default function TripNew() {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [answers, setAnswers] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const q = STEPS[step]
  const isLast = step === STEPS.length - 1

  function setAnswer(id, value) {
    setAnswers(prev => ({ ...prev, [id]: value }))
  }

  function toggleMulti(id, value) {
    const current = answers[id] || []
    const next = current.includes(value)
      ? current.filter(v => v !== value)
      : [...current, value]
    setAnswer(id, next)
  }

  function canAdvance() {
    const q = STEPS[step]
    if (q.type === 'datetime_range') {
      return !!(answers.departure_time && answers.return_time)
    }
    if (q.type === 'multi_select') {
      return (answers[q.id] || []).length > 0
    }
    return !!answers[q.id]
  }

  async function submit() {
    setSubmitting(true)
    setError(null)
    try {
      const body = {
        ...answers,
        departure_time: answers.departure_time
          ? new Date(answers.departure_time).toISOString()
          : null,
        return_time: answers.return_time
          ? new Date(answers.return_time).toISOString()
          : null,
      }
      const { data } = await api.post('/trips', body)
      navigate(`/trips/${data.trip_id}`, { replace: true })
    } catch {
      setError('Failed to create trip. Please try again.')
      setSubmitting(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50 px-4">
      <div className="max-w-md w-full">
        {/* Progress bar */}
        <div className="flex gap-1 mb-6">
          {STEPS.map((_, i) => (
            <div
              key={i}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i <= step ? 'bg-blue-600' : 'bg-gray-200'
              }`}
            />
          ))}
        </div>

        <h2 className="text-xl font-semibold text-gray-900 mb-6">{q.question}</h2>

        {/* Datetime range */}
        {q.type === 'datetime_range' && (
          <div className="space-y-3 mb-8">
            <div>
              <label className="block text-sm text-gray-600 mb-1">Departure</label>
              <input
                type="datetime-local"
                value={answers.departure_time || ''}
                onChange={e => setAnswer('departure_time', e.target.value)}
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">Return</label>
              <input
                type="datetime-local"
                value={answers.return_time || ''}
                onChange={e => setAnswer('return_time', e.target.value)}
                min={answers.departure_time || ''}
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>
        )}

        {/* Single select */}
        {q.type === 'single_select' && (
          <div className="space-y-3 mb-8">
            {q.options.map(opt => (
              <button
                key={opt.value}
                onClick={() => setAnswer(q.id, opt.value)}
                className={`w-full text-left px-4 py-3 rounded-lg border transition-colors ${
                  answers[q.id] === opt.value
                    ? 'border-blue-600 bg-blue-50 text-blue-900'
                    : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        )}

        {/* Multi select */}
        {q.type === 'multi_select' && (
          <div className="space-y-3 mb-8">
            {q.options.map(opt => {
              const selected = (answers[q.id] || []).includes(opt.value)
              return (
                <button
                  key={opt.value}
                  onClick={() => toggleMulti(q.id, opt.value)}
                  className={`w-full text-left px-4 py-3 rounded-lg border transition-colors ${
                    selected
                      ? 'border-blue-600 bg-blue-50 text-blue-900'
                      : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300'
                  }`}
                >
                  <span className="flex items-center gap-3">
                    <span className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ${
                      selected ? 'bg-blue-600 border-blue-600' : 'border-gray-300'
                    }`}>
                      {selected && <span className="text-white text-xs">✓</span>}
                    </span>
                    {opt.label}
                  </span>
                </button>
              )
            })}
          </div>
        )}

        {error && (
          <p className="text-sm text-red-500 mb-4">{error}</p>
        )}

        <div className="flex gap-3">
          {step > 0 && (
            <button
              onClick={() => setStep(s => s - 1)}
              className="flex-1 px-4 py-3 border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50 transition-colors"
            >
              Back
            </button>
          )}
          {isLast ? (
            <button
              onClick={submit}
              disabled={submitting || !canAdvance()}
              className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {submitting ? 'Planning…' : 'Get recommendations'}
            </button>
          ) : (
            <button
              onClick={() => setStep(s => s + 1)}
              disabled={!canAdvance()}
              className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              Next
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
