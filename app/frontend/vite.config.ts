import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Served by nginx at https://<host>/next/ ; API at /next/api/ -> 127.0.0.1:18800/api/
export default defineConfig({
  base: '/next/',
  plugins: [vue()],
  build: {
    outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 700,
    rollupOptions: { output: { manualChunks: { echarts: ['echarts/core', 'echarts/charts', 'echarts/components', 'echarts/renderers'], 'naive-ui': ['naive-ui'], vue: ['vue', 'vue-router'] } } },
  },
  server: { proxy: { '/next/api': { target: 'http://127.0.0.1:18800', rewrite: (p) => p.replace(/^\/next/, '') } } },
})
