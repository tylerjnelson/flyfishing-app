import { registerSW } from 'virtual:pwa-register'

// Register service worker with auto-update.
// On first activation, warm the cache for the user's saved spots so
// their spot detail pages and conditions data are readable offline.
export function initPWA() {
  const updateSW = registerSW({
    onRegisteredSW(swUrl, registration) {
      // Warm saved-spot cache once the SW is active and controlling the page.
      // We wait for the SW to be ready so cache writes go through the SW.
      if (registration?.active) {
        warmSavedSpotCache()
      } else {
        navigator.serviceWorker.ready.then(() => warmSavedSpotCache())
      }
    },
    onOfflineReady() {
      console.log('[PWA] App is ready for offline use.')
    },
  })
  return updateSW
}

// Fetch saved-spot detail pages into the Workbox runtime cache so they are
// available at the trailhead with no cell signal.
async function warmSavedSpotCache() {
  try {
    const res = await fetch('/api/users/me/saved-spots', { credentials: 'include' })
    if (!res.ok) return  // not authenticated yet — nothing to warm
    const spots = await res.json()
    for (const { spot_id } of spots) {
      // Fetch spot detail — response goes into the 'api-spots' cache via SW
      fetch(`/api/spots/${spot_id}`, { credentials: 'include' }).catch(() => {})
    }
  } catch {
    // Offline at warm-up time — nothing to do, cache stays as-is
  }
}
