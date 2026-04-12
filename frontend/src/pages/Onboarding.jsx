import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import useAuthStore from '../store/auth'
import LocationInput from '../components/LocationInput'

const QUESTIONS = [
  {
    id: 'home_location',
    question: 'Where do you typically depart from?',
    type: 'location',
    placeholder: 'City, State or full address',
  },
  {
    id: 'vehicle_capability',
    question: 'What is your vehicle capability?',
    type: 'single_select',
    options: [
      { value: 'paved_only', label: 'Paved / 2WD only' },
      { value: 'dirt_ok', label: 'Dirt road OK' },
      { value: 'four_wd', label: '4WD / High clearance' },
    ],
  },
]

export default function Onboarding() {
  const navigate = useNavigate()
  const setAuth = useAuthStore(s => s.setAuth)
  const user = useAuthStore(s => s.user)
  const [step, setStep] = useState(0)
  const [answers, setAnswers] = useState({})
  const [saving, setSaving] = useState(false)

  const q = QUESTIONS[step]
  const isLast = step === QUESTIONS.length - 1

  function setAnswer(value) {
    setAnswers(prev => ({ ...prev, [q.id]: value }))
  }

  function canAdvance() {
    if (q.type === 'location') return answers[q.id]?.lat != null
    return !!answers[q.id]
  }

  async function finish() {
    setSaving(true)
    try {
      const { data } = await api.patch('/users/me', {
        preferences: {
          ...answers,
          experience_level: 'advanced',
          catch_intent: 'catch_and_release',
        },
      })
      setAuth({ ...user, preferences: data.preferences }, useAuthStore.getState().accessToken)
      navigate('/trips', { replace: true })
    } catch {
      setSaving(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50">
      <div className="max-w-md w-full mx-4">
        <div className="mb-8">
          <div className="flex gap-1 mb-6">
            {QUESTIONS.map((_, i) => (
              <div
                key={i}
                className={`h-1 flex-1 rounded-full ${i <= step ? 'bg-blue-600' : 'bg-gray-200'}`}
              />
            ))}
          </div>
          <h2 className="text-xl font-semibold text-gray-900">{q.question}</h2>
        </div>

        <div className="space-y-3 mb-8">
          {q.type === 'location' && (
            <LocationInput
              value={answers[q.id] || null}
              onChange={val => setAnswer(val)}
            />
          )}

          {q.type === 'single_select' && q.options.map(opt => (
            <button
              key={opt.value}
              onClick={() => setAnswer(opt.value)}
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
              onClick={finish}
              disabled={saving || !canAdvance()}
              className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {saving ? 'Saving…' : 'Finish setup'}
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
