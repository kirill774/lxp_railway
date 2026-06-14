// @ts-check
const { defineConfig, devices } = require('@playwright/test');

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:8000';

module.exports = defineConfig({
  testDir: './tests/e2e',
  timeout: 60000,
  expect: { timeout: 5000 },
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  webServer: {
    command: 'python -m uvicorn main:app --host 127.0.0.1 --port 8000',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 60000,
    env: {
      BOT_TOKEN: process.env.BOT_TOKEN || 'test-token',
      WORKER_SECRET: process.env.WORKER_SECRET || 'test-worker-secret',
      ALLOWED_ORIGINS: process.env.ALLOWED_ORIGINS || 'http://127.0.0.1:8000',
    },
  },
  projects: [
    {
      name: 'chromium-mobile',
      use: { ...devices['Pixel 5'] },
    },
  ],
});
