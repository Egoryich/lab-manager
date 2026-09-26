import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5174',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev --workspace @lab/web -- --port 5174',
    url: 'http://localhost:5174',
    reuseExistingServer: false,
    env: { LAB_API_PROXY: 'http://127.0.0.1:8001' },
    timeout: 60_000,
  },
});
