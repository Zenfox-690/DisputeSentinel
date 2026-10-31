import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('ag-grid-community') || id.includes('ag-grid-react')) {
            return 'ag-grid'
          }
          if (id.includes('lucide-react')) {
            return 'lucide'
          }
          if (id.includes('node_modules/react') || id.includes('node_modules/react-dom')) {
            return 'react-vendor'
          }
        }
      }
    }
  }
})
