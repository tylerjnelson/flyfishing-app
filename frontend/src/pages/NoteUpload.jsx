/**
 * Note upload flow.
 * Supports three entry types:
 *   typed     — text area entry
 *   handwritten — file picker (notebook page image)
 *   map       — file picker (standalone map image, applies OpenCV correction)
 *
 * After upload, polls the note detail endpoint until ingestion is complete
 * (no awaiting_* pending flags remain), then presents any confirmation prompts.
 */

import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'
import useAuthStore from '../store/auth'

const ACCEPTED_IMAGE_TYPES = 'image/jpeg,image/png,image/webp,image/heic,image/heif'

export default function NoteUpload() {
  const { accessToken } = useAuthStore()
  const navigate = useNavigate()
  const fileInputRef = useRef(null)

  const [sourceType, setSourceType] = useState('typed')
  const [content, setContent] = useState('')
  const [file, setFile] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [uploadedNoteId, setUploadedNoteId] = useState(null)
  const [processingStatus, setProcessingStatus] = useState(null)

  const headers = { Authorization: `Bearer ${accessToken}` }

  const handleFileChange = (e) => {
    const f = e.target.files?.[0]
    if (f) setFile(f)
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setUploading(true)

    try {
      const form = new FormData()
      form.append('source_type', sourceType)
      if (sourceType === 'typed') {
        form.append('content', content)
      } else {
        if (!file) {
          setError('Please select an image file')
          setUploading(false)
          return
        }
        form.append('file', file)
      }

      const { data } = await axios.post('/api/notes/upload', form, {
        headers: { ...headers, 'Content-Type': 'multipart/form-data' },
      })

      setUploadedNoteId(data.note_id)
      setProcessingStatus('processing')
      pollStatus(data.note_id)
    } catch (err) {
      const msg =
        err.response?.data?.detail || 'Upload failed. Please try again.'
      setError(msg)
    } finally {
      setUploading(false)
    }
  }

  const pollStatus = async (noteId) => {
    // Poll every 2 seconds for up to 2 minutes waiting for ingestion to complete.
    const maxAttempts = 60
    let attempts = 0

    const check = async () => {
      try {
        const { data } = await axios.get(`/api/notes/${noteId}`, { headers })
        const pending = data.pending_flags || []

        if (pending.length === 0) {
          setProcessingStatus('done')
          // Navigate to notes list after a brief pause
          setTimeout(() => navigate('/notes'), 1200)
        } else {
          setProcessingStatus(pending)
          if (attempts < maxAttempts) {
            attempts++
            setTimeout(check, 2000)
          } else {
            setProcessingStatus('timeout')
          }
        }
      } catch {
        setProcessingStatus('error')
      }
    }

    setTimeout(check, 2000)
  }

  if (uploadedNoteId) {
    return (
      <div className="max-w-lg mx-auto px-4 py-12 text-center">
        {processingStatus === 'processing' || Array.isArray(processingStatus) ? (
          <>
            <div className="text-4xl mb-4">⏳</div>
            <h2 className="text-lg font-semibold text-gray-800 mb-2">
              Processing your note…
            </h2>
            <p className="text-gray-500 text-sm">
              {Array.isArray(processingStatus)
                ? 'Waiting for confirmation steps to complete.'
                : 'Extracting text, resolving location, and generating embeddings.'}
            </p>
            {Array.isArray(processingStatus) && processingStatus.length > 0 && (
              <div className="mt-4 space-y-2">
                {processingStatus.includes('awaiting_date_confirmation') && (
                  <DateConfirmationBanner noteId={uploadedNoteId} headers={headers} />
                )}
              </div>
            )}
          </>
        ) : processingStatus === 'done' ? (
          <>
            <div className="text-4xl mb-4">✓</div>
            <h2 className="text-lg font-semibold text-gray-800">
              Note added!
            </h2>
            <p className="text-gray-500 text-sm mt-1">Redirecting to notes…</p>
          </>
        ) : (
          <>
            <div className="text-4xl mb-4">⚠</div>
            <h2 className="text-lg font-semibold text-gray-800">
              Something went wrong during processing.
            </h2>
            <button
              onClick={() => navigate('/notes')}
              className="mt-4 px-4 py-2 bg-blue-600 text-white rounded"
            >
              Go to Notes
            </button>
          </>
        )}
      </div>
    )
  }

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Add Note</h1>

      {/* Source type selector */}
      <div className="flex gap-2 mb-6">
        {[
          { key: 'typed', label: 'Type it' },
          { key: 'handwritten', label: 'Notebook page' },
          { key: 'map', label: 'Map only' },
        ].map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => setSourceType(key)}
            className={`flex-1 py-2 px-3 rounded-lg border text-sm font-medium transition-colors ${
              sourceType === key
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white text-gray-600 border-gray-300 hover:border-blue-400'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {sourceType === 'typed' ? (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Note
            </label>
            <textarea
              value={content}
              onChange={e => setContent(e.target.value)}
              rows={8}
              placeholder="Describe your fishing trip — location, conditions, what worked…"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
          </div>
        ) : (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {sourceType === 'map' ? 'Map image' : 'Notebook page photo'}
            </label>
            <p className="text-xs text-gray-400 mb-2">
              {sourceType === 'map'
                ? 'Upload a standalone hand-drawn map. OpenCV will correct perspective and contrast automatically.'
                : 'Upload a photo of a handwritten notebook page. Text will be extracted via OCR. If a map sketch is detected, it will be extracted as a separate entry.'}
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_IMAGE_TYPES}
              onChange={handleFileChange}
              className="hidden"
              required
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="w-full border-2 border-dashed border-gray-300 rounded-lg px-4 py-8 text-center text-gray-500 hover:border-blue-400 hover:text-blue-500 transition-colors"
            >
              {file ? (
                <span className="text-gray-700 font-medium">{file.name}</span>
              ) : (
                <>
                  <span className="block text-2xl mb-1">📷</span>
                  <span className="text-sm">Tap to choose photo or file</span>
                </>
              )}
            </button>
            {file && (
              <img
                src={URL.createObjectURL(file)}
                alt="Preview"
                className="mt-2 w-full max-h-64 object-contain rounded border border-gray-200"
              />
            )}
          </div>
        )}

        {error && (
          <p className="text-sm text-red-600">{error}</p>
        )}

        <div className="flex gap-3 pt-2">
          <button
            type="button"
            onClick={() => navigate('/notes')}
            className="flex-1 py-2 px-4 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={uploading}
            className="flex-1 py-2 px-4 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
          >
            {uploading ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </form>
    </div>
  )
}

/**
 * Inline date confirmation banner shown during processing.
 */
function DateConfirmationBanner({ noteId, headers }) {
  const [noteData, setNoteData] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [dateInput, setDateInput] = useState('')

  useEffect(() => {
    axios.get(`/api/notes/${noteId}`, { headers }).then(({ data }) => {
      setNoteData(data)
      if (data.note_date) setDateInput(data.note_date)
    })
  }, [noteId])

  const handleConfirm = async () => {
    if (!dateInput) return
    await axios.patch(
      `/api/notes/${noteId}/confirm-date`,
      null,
      { headers, params: { confirmed_date: dateInput } }
    )
    setConfirmed(true)
  }

  if (!noteData || confirmed) return null

  return (
    <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-left">
      <p className="text-sm font-medium text-amber-800 mb-2">
        Confirm trip date
      </p>
      <p className="text-xs text-amber-600 mb-3">
        We extracted this date from your note. Please confirm or correct it.
      </p>
      <div className="flex gap-2 items-center">
        <input
          type="date"
          value={dateInput}
          onChange={e => setDateInput(e.target.value)}
          className="border border-amber-300 rounded px-2 py-1 text-sm flex-1"
        />
        <button
          onClick={handleConfirm}
          className="px-3 py-1 bg-amber-600 text-white rounded text-sm"
        >
          Confirm
        </button>
      </div>
    </div>
  )
}

