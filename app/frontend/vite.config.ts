import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Served by nginx at https://<host>/next/ ; API at /next/api/ -> 127.0.0.1:18800/api/
export default defineConfig({
  base: '/next/',
  plugins: [vue()],
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 1200 },
  server: { proxy: { '/next/api': { target: 'http://127.0.0.1:18800', rewrite: (p) => p.replace(/^\/next/, '') } } },
})
