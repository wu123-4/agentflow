import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 项目未装 @types/node，此处最小声明（仅构建配置用）
declare const process: { env: Record<string, string | undefined> }

// Vite 开发服务器：/api 代理到后端（默认 8000，可用 VITE_PROXY_TARGET 覆盖）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
