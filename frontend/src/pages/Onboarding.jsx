import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import api from '../api/client'
import useAuthStore from '../store/auth'

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
  {
    id: 'experience_level',
    question: 'How would you describe your fly fishing experience?',
    type: 'single_select',
    options: [
      { value: 'beginner', label: 'Beginner' },
      { value: 'intermediate', label: 'Intermediate' },
      { value: 'advanced', label: 'Advanced' },
    ],
  },
  {
    id: 'catch_intent',
    question: 'What is your catch intent?',
    type: 'single_select',
    options: [
      { value: 'catch_and_release', label: 'Strict catch-and-release' },
      { value: 'keep_if_legal', label: 'Keep if legal' },
    ],
  },
  {
    id: 'gear_setup',
    question: 'What gear setups do you typically fish with?',
    type: 'multi_select',
    options: [
      { value: 'full_setup', label: 'Full setup' },
      { value: 'pack_rod', label: 'Pack rod' },
      { value: 'float_tube', label: 'Float tube' },
      { value: 'spey', label: 'Spey / two-hander' },
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

  function toggleMulti(value) {
    const current = answers[q.id] || []
    const next = current.includes(value)
      ? current.filter(v => v !== value)
      : [...current, value]
    setAnswer(next)
  }

  async function finish() {
    setSaving(true)
    try {
      const { data } = await api.patch('/users/me', { preferences: answers })
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
            <input
              type="text"
              value={answers[q.id] || ''}
              onChange={e => setAnswer(e.target.value)}
              placeholder={q.placeholder}
              autoFocus
              className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
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

          {q.type === 'multi_select' && q.options.map(opt => {
            const selected = (answers[q.id] || []).includes(opt.value)
            return (
              <button
                key={opt.value}
                onClick={() => toggleMulti(opt.value)}
                className={`w-full text-left px-4 py-3 rounded-lg border transition-colors ${
                  selected
                    ? 'border-blue-600 bg-blue-50 text-blue-900'
                    : 'border-gray-200 bg-white text-gray-700 hover:border-gray-300'
                }`}
              >
                {opt.label}
              </button>
            )
          })}
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
              disabled={saving}
              className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {saving ? 'Saving…' : 'Finish setup'}
            </button>
          ) : (
            <button
              onClick={() => setStep(s => s + 1)}
              className="flex-1 px-4 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors"
            >
              Next
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
