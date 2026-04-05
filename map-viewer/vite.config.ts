import { defineConfig } from 'vite';

export default defineConfig({
  root: '.',
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/catalog': 'http://localhost:3000',
      // Proxy tile requests to Martin during development
      '^/[^/]+/\\d+/\\d+/\\d+': {
        target: 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
});
