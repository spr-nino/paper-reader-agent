import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173, open: false, host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:8321' },
  },
})

