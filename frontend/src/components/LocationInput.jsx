import { useEffect, useRef, useState } from 'react'
import api from '../api/client'

/**
 * Geocode-verified location input.
 *
 * Props:
 *   value   — null | {label, lat, lon}
 *   onChange — called with {label, lat, lon} on success, or null when text is cleared/changed
 */
export default function LocationInput({ value, onChange }) {
  const [text, setText] = useState(value?.label || '')
  const [geocoding, setGeocoding] = useState(false)
  const [geocodeError, setGeocodeError] = useState(null)
  const timerRef = useRef(null)

  function handleChange(e) {
    const raw = e.target.value
    setText(raw)
    onChange(null)
    setGeocodeError(null)
    if (timerRef.current) clearTimeout(timerRef.current)
    if (raw.trim().length < 3) return
    timerRef.current = setTimeout(() => runGeocode(raw.trim()), 600)
  }

  async function runGeocode(q) {
    setGeocoding(true)
    setGeocodeError(null)
    try {
      const { data } = await api.get('/users/geocode', { params: { q } })
      if (data.result) {
        onChange(data.result)
        setText(data.result.label)
      } else {
        setGeocodeError('Location not found. Try a more specific address.')
      }
    } catch {
      setGeocodeError('Could not verify location. Check your connection.')
    } finally {
      setGeocoding(false)
    }
  }

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const confirmed = value && value.lat != null

  return (
    <div className="space-y-2">
      <div className="relative">
        <input
          type="text"
          value={text}
          onChange={handleChange}
          placeholder="City, State or full address"
          autoFocus
          className={`w-full px-4 py-3 pr-10 border rounded-lg bg-white text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-500 ${
            confirmed ? 'border-green-500' : geocodeError ? 'border-red-400' : 'border-gray-300'
          }`}
        />
        <div className="absolute right-3 top-1/2 -translate-y-1/2">
          {geocoding && <span className="text-gray-400 text-sm">...</span>}
          {!geocoding && confirmed && <span className="text-green-500 text-lg">✓</span>}
          {!geocoding && geocodeError && <span className="text-red-400 text-lg">✗</span>}
        </div>
      </div>
      {confirmed && (
        <p className="text-sm text-green-700 px-1">{value.label}</p>
      )}
      {geocodeError && (
        <p className="text-sm text-red-500 px-1">{geocodeError}</p>
      )}
    </div>
  )
}
