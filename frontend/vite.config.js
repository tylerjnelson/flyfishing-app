import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Flyfish',
        short_name: 'Flyfish',
        description: 'Fly fishing trip planner and conditions tracker',
        theme_color: '#863bff',
        background_color: '#0f0720',
        display: 'standalone',
        orientation: 'portrait',
        scope: '/',
        start_url: '/',
        icons: [
          {
            src: '/pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
          },
          {
            src: '/pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
          },
          {
            src: '/pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable',
          },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        runtimeCaching: [
          // OSM tile cache — CacheFirst, 7-day expiry
          {
            urlPattern: /^https:\/\/[abc]\.tile\.openstreetmap\.org\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'osm-tiles',
              expiration: {
                maxEntries: 500,
                maxAgeSeconds: 60 * 60 * 24 * 7,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          // Spot detail + conditions — NetworkFirst, 48h fallback
          {
            urlPattern: /^\/api\/spots\/.*/i,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-spots',
              networkTimeoutSeconds: 5,
              expiration: {
                maxEntries: 100,
                maxAgeSeconds: 60 * 60 * 48,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          // Conditions data — NetworkFirst, 48h fallback
          {
            urlPattern: /^\/api\/conditions\/.*/i,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-conditions',
              networkTimeoutSeconds: 5,
              expiration: {
                maxEntries: 100,
                maxAgeSeconds: 60 * 60 * 48,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          // Saved spots list — NetworkFirst, 48h fallback
          {
            urlPattern: /^\/api\/users\/me\/saved-spots$/i,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-saved-spots',
              networkTimeoutSeconds: 5,
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 48,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
        ],
      },
    }),
  ],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: '../dist',
  },
})
