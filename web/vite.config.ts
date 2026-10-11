import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// In dev, the Vite server proxies /api to the BFF (npm run start, default port 8787) so cookies and same-origin rules behave
// exactly as in production. Override with BFF_DEV_TARGET if the BFF listens elsewhere.
const bff = process.env.BFF_DEV_TARGET ?? 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false, target: 'es2022' },
  server: {
    port: 5173,
    proxy: { '/api': bff, '/healthz': bff },
  },
  test: {
    include: ['src/**/*.test.ts', 'test/**/*.test.ts'],
    environment: 'node',
    restoreMocks: true,
  },
});
