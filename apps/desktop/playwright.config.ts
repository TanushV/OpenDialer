import { defineConfig } from '@playwright/test';
export default defineConfig({ testDir: './tests', testMatch: '**/*.spec.ts', workers: 1,
  use: { baseURL: 'http://127.0.0.1:1420', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: '../../.venv/bin/python ../../scripts/e2e_server.py local', url: 'http://127.0.0.1:8765/health', reuseExistingServer: false },
    { command: '../../.venv/bin/python ../../scripts/e2e_server.py connected', url: 'http://127.0.0.1:8766/health', reuseExistingServer: false },
    { command: 'npm run dev', url: 'http://127.0.0.1:1420', env: { VITE_BACKEND_URL: 'http://127.0.0.1:8765', VITE_LOCAL_TOKEN: 'e2e-local-token' }, reuseExistingServer: false }
  ] });
