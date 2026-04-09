/**
 * Note upload flow.
 * Supports two entry types:
 *   typed  — text area entry (also used for voice transcriptions)
 *   map    — image picker → crop → OpenCV correction on server
 *
 * After upload, polls the note detail endpoint until ingestion is complete
 * (no awaiting_* pending flags remain).
 */

import { useRef, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import ReactCrop, { centerCrop, makeAspectCrop } from 'react-image-crop'
import 'react-image-crop/dist/ReactCrop.css'
import axios from 'axios'
import useAuthStore from '../store/auth'

// Voice recording states
const REC_IDLE        = 'idle'
const REC_RECORDING   = 'recording'
const REC_TRANSCRIBING = 'transcribing'
const REC_DONE        = 'done'

// Map crop states
const CROP_IDLE       = 'idle'      // no image selected
const CROP_CROPPING   = 'cropping'  // image loaded, crop UI showing
const CROP_CONFIRMED  = 'confirmed' // crop locked in, ready to submit

const ACCEPTED_IMAGE_TYPES = 'image/jpeg,image/png,image/webp,image/heic,image/heif'

export default function NoteUpload() {
  const { accessToken } = useAuthStore()
  const navigate = useNavigate()
  const fileInputRef = useRef(null)

  const [sourceType, setSourceType] = useState('typed')
  const [content, setContent] = useState('')
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [uploadedNoteId, setUploadedNoteId] = useState(null)
  const [processingStatus, setProcessingStatus] = useState(null)

  // Voice state
  const [recState, setRecState] = useState(REC_IDLE)
  const [transcription, setTranscription] = useState('')
  const mediaRecorderRef = useRef(null)
  const audioChunksRef = useRef([])

  // Map crop state
  const [cropState, setCropState] = useState(CROP_IDLE)
  const [imgSrc, setImgSrc] = useState('')
  const [crop, setCrop] = useState()
  const [completedCrop, setCompletedCrop] = useState()
  const [croppedBlob, setCroppedBlob] = useState(null)
  const imgRef = useRef(null)

  const headers = { Authorization: `Bearer ${accessToken}` }

  // ---------------------------------------------------------------------------
  // Voice recording
  // ---------------------------------------------------------------------------

  const startRecording = async () => {
    setError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : ''
      const mr = new MediaRecorder(stream, mimeType ? { mimeType } : {})
      audioChunksRef.current = []
      mr.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunksRef.current.push(e.data)
      }
      mr.onstop = async () => {
        stream.getTracks().forEach(t => t.stop())
        const blob = new Blob(audioChunksRef.current, { type: mr.mimeType || 'audio/webm' })
        await sendAudioForTranscription(blob)
      }
      mediaRecorderRef.current = mr
      mr.start()
      setRecState(REC_RECORDING)
    } catch {
      setError('Microphone access denied. Please allow microphone access and try again.')
    }
  }

  const stopRecording = () => {
    mediaRecorderRef.current?.stop()
    setRecState(REC_TRANSCRIBING)
  }

  const sendAudioForTranscription = async (blob) => {
    try {
      const form = new FormData()
      form.append('file', blob, 'recording.webm')
      const { data } = await axios.post('/api/notes/transcribe', form, {
        headers: { ...headers, 'Content-Type': 'multipart/form-data' },
      })
      setTranscription(data.text)
      setRecState(REC_DONE)
    } catch {
      setError('Transcription failed. Please try again.')
      setRecState(REC_IDLE)
    }
  }

  const resetRecording = () => {
    setTranscription('')
    setRecState(REC_IDLE)
    setError(null)
  }

  // ---------------------------------------------------------------------------
  // Map crop
  // ---------------------------------------------------------------------------

  const onImageFileChange = (e) => {
    const f = e.target.files?.[0]
    if (!f) return
    setCroppedBlob(null)
    setCompletedCrop(undefined)
    setCrop(undefined)
    setCropState(CROP_CROPPING)
    const reader = new FileReader()
    reader.addEventListener('load', () => setImgSrc(reader.result?.toString() || ''))
    reader.readAsDataURL(f)
    // reset input so same file can be re-selected
    e.target.value = ''
  }

  const onImageLoad = useCallback((e) => {
    const { naturalWidth: width, naturalHeight: height } = e.currentTarget
    // default crop: centered, full width
    const initial = centerCrop(
      makeAspectCrop({ unit: '%', width: 90 }, width / height, width, height),
      width,
      height,
    )
    setCrop(initial)
  }, [])

  const confirmCrop = useCallback(() => {
    if (!completedCrop || !imgRef.current) return
    const img = imgRef.current
    const scaleX = img.naturalWidth / img.width
    const scaleY = img.naturalHeight / img.height

    const canvas = document.createElement('canvas')
    canvas.width = Math.round(completedCrop.width * scaleX)
    canvas.height = Math.round(completedCrop.height * scaleY)
    const ctx = canvas.getContext('2d')
    ctx.drawImage(
      img,
      completedCrop.x * scaleX,
      completedCrop.y * scaleY,
      completedCrop.width * scaleX,
      completedCrop.height * scaleY,
      0, 0,
      canvas.width,
      canvas.height,
    )
    canvas.toBlob(blob => {
      setCroppedBlob(blob)
      setCropState(CROP_CONFIRMED)
    }, 'image/jpeg', 0.95)
  }, [completedCrop])

  const resetCrop = () => {
    setImgSrc('')
    setCrop(undefined)
    setCompletedCrop(undefined)
    setCroppedBlob(null)
    setCropState(CROP_IDLE)
  }

  // ---------------------------------------------------------------------------
  // Submit
  // ---------------------------------------------------------------------------

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setUploading(true)

    // Voice tab submits transcription as a typed note
    const effectiveSourceType = sourceType === 'voice' ? 'typed' : sourceType
    const effectiveContent    = sourceType === 'voice' ? transcription : content

    try {
      const form = new FormData()
      form.append('source_type', effectiveSourceType)

      if (effectiveSourceType === 'typed') {
        form.append('content', effectiveContent)
      } else {
        // map — send the cropped blob
        if (!croppedBlob) {
          setError('Please select and crop a map image')
          setUploading(false)
          return
        }
        form.append('file', croppedBlob, 'map.jpg')
      }

      const { data } = await axios.post('/api/notes/upload', form, {
        headers: { ...headers, 'Content-Type': 'multipart/form-data' },
      })

      setUploadedNoteId(data.note_id)
      setProcessingStatus('processing')
      pollStatus(data.note_id)
    } catch (err) {
      const msg = err.response?.data?.detail || 'Upload failed. Please try again.'
      setError(msg)
    } finally {
      setUploading(false)
    }
  }

  const pollStatus = async (noteId) => {
    const maxAttempts = 60
    let attempts = 0

    const check = async () => {
      try {
        const { data } = await axios.get(`/api/notes/${noteId}`, { headers })
        const pending = data.pending_flags || []

        if (pending.length === 0) {
          setProcessingStatus('done')
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

  // ---------------------------------------------------------------------------
  // Tab switch helper
  // ---------------------------------------------------------------------------

  const switchTab = (key) => {
    setSourceType(key)
    resetRecording()
    resetCrop()
    setError(null)
  }

  // ---------------------------------------------------------------------------
  // Post-upload status screen
  // ---------------------------------------------------------------------------

  if (uploadedNoteId) {
    return (
      <div className="max-w-lg mx-auto px-4 py-12 text-center">
        {processingStatus === 'processing' || Array.isArray(processingStatus) ? (
          <>
            <div className="text-4xl mb-4">⏳</div>
            <h2 className="text-lg font-semibold text-gray-800 mb-2">Processing your note…</h2>
            <p className="text-gray-500 text-sm">Applying corrections and saving.</p>
          </>
        ) : processingStatus === 'done' ? (
          <>
            <div className="text-4xl mb-4">✓</div>
            <h2 className="text-lg font-semibold text-gray-800">Note added!</h2>
            <p className="text-gray-500 text-sm mt-1">Redirecting to notes…</p>
          </>
        ) : (
          <>
            <div className="text-4xl mb-4">⚠</div>
            <h2 className="text-lg font-semibold text-gray-800">Something went wrong during processing.</h2>
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

  // ---------------------------------------------------------------------------
  // Main form
  // ---------------------------------------------------------------------------

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Add Note</h1>

      {/* Source type selector */}
      <div className="flex gap-2 mb-6">
        {[
          { key: 'typed', label: 'Type it' },
          { key: 'voice', label: 'Voice' },
          { key: 'map',   label: 'Map only' },
        ].map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => switchTab(key)}
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

        {/* Typed tab */}
        {sourceType === 'typed' && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Note</label>
            <textarea
              value={content}
              onChange={e => setContent(e.target.value)}
              rows={8}
              placeholder="Describe your fishing trip — location, conditions, what worked…"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              required
            />
          </div>
        )}

        {/* Voice tab */}
        {sourceType === 'voice' && (
          <div>
            {recState === REC_IDLE && (
              <button
                type="button"
                onClick={startRecording}
                className="w-full border-2 border-dashed border-gray-300 rounded-lg px-4 py-10 text-center text-gray-500 hover:border-blue-400 hover:text-blue-500 transition-colors"
              >
                <span className="block text-3xl mb-2">🎙</span>
                <span className="text-sm">Tap to start recording</span>
              </button>
            )}
            {recState === REC_RECORDING && (
              <div className="text-center py-6">
                <div className="inline-block w-4 h-4 bg-red-500 rounded-full animate-pulse mb-3" />
                <p className="text-sm text-gray-600 mb-4">Recording…</p>
                <button
                  type="button"
                  onClick={stopRecording}
                  className="px-6 py-2 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700"
                >
                  Stop
                </button>
              </div>
            )}
            {recState === REC_TRANSCRIBING && (
              <div className="text-center py-8 text-gray-500 text-sm">Transcribing…</div>
            )}
            {recState === REC_DONE && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Transcription — review and edit before submitting
                </label>
                <textarea
                  value={transcription}
                  onChange={e => setTranscription(e.target.value)}
                  rows={8}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  type="button"
                  onClick={resetRecording}
                  className="mt-2 text-xs text-gray-400 hover:text-gray-600 underline"
                >
                  Re-record
                </button>
              </div>
            )}
          </div>
        )}

        {/* Map tab */}
        {sourceType === 'map' && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Map image</label>
            <p className="text-xs text-gray-400 mb-3">
              Select a photo, crop to the page, then upload. OpenCV will correct contrast and shadows automatically.
            </p>

            {/* File picker — always hidden, triggered by button */}
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_IMAGE_TYPES}
              onChange={onImageFileChange}
              className="hidden"
            />

            {cropState === CROP_IDLE && (
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="w-full border-2 border-dashed border-gray-300 rounded-lg px-4 py-8 text-center text-gray-500 hover:border-blue-400 hover:text-blue-500 transition-colors"
              >
                <span className="block text-2xl mb-1">📷</span>
                <span className="text-sm">Tap to choose photo</span>
              </button>
            )}

            {cropState === CROP_CROPPING && imgSrc && (
              <div>
                <p className="text-xs text-gray-500 mb-2">Drag to adjust the crop box around the map page.</p>
                <div className="rounded-lg overflow-hidden border border-gray-200">
                  <ReactCrop
                    crop={crop}
                    onChange={(_, pct) => setCrop(pct)}
                    onComplete={(c) => setCompletedCrop(c)}
                    minWidth={50}
                    minHeight={50}
                  >
                    <img
                      ref={imgRef}
                      src={imgSrc}
                      onLoad={onImageLoad}
                      className="max-w-full"
                      alt="Map to crop"
                    />
                  </ReactCrop>
                </div>
                <div className="flex gap-2 mt-3">
                  <button
                    type="button"
                    onClick={resetCrop}
                    className="flex-1 py-2 px-3 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50"
                  >
                    Choose different photo
                  </button>
                  <button
                    type="button"
                    onClick={confirmCrop}
                    disabled={!completedCrop?.width}
                    className="flex-1 py-2 px-3 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
                  >
                    Confirm crop
                  </button>
                </div>
              </div>
            )}

            {cropState === CROP_CONFIRMED && (
              <div className="flex items-center justify-between px-3 py-3 bg-green-50 border border-green-200 rounded-lg">
                <span className="text-sm text-green-800 font-medium">Crop confirmed</span>
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="text-xs text-gray-500 hover:text-gray-700 underline"
                >
                  Change photo
                </button>
              </div>
            )}
          </div>
        )}

        {error && <p className="text-sm text-red-600">{error}</p>}

        {/* Submit row — hidden while recording or transcribing, or while cropping */}
        {!(sourceType === 'voice' && recState !== REC_DONE) &&
         !(sourceType === 'map' && cropState !== CROP_CONFIRMED) && (
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
              disabled={uploading || (sourceType === 'voice' && !transcription.trim())}
              className="flex-1 py-2 px-4 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
            >
              {uploading ? 'Uploading…' : 'Upload'}
            </button>
          </div>
        )}
      </form>
    </div>
  )
}
