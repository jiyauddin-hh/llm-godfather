import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Both ports are overridable so the app can move off a busy port:
//   PORT=5180 BACKEND_PORT=9100 npm run dev
const FRONTEND_PORT = Number(process.env.PORT || 5173)
const BACKEND_PORT = Number(process.env.BACKEND_PORT || 8000)

export default defineConfig({
  plugins: [react()],
  server: {
    port: FRONTEND_PORT,
    // Fail loudly instead of silently sliding to the next port.
    strictPort: true,
    proxy: {
      // REST and WebSocket both go to the FastAPI backend.
      '/api': {
        target: `http://127.0.0.1:${BACKEND_PORT}`,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    port: FRONTEND_PORT,
    strictPort: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${BACKEND_PORT}`,
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
