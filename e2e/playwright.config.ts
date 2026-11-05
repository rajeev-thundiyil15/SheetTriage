import { defineConfig, devices } from '@playwright/test';

const PORT = 8010;

export default defineConfig({
  testDir: './tests',
  // The app runs one cleaning job at a time, so tests can't run side by side.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'on',
    screenshot: 'on',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'uv run --no-project --python 3.14 --with-requirements requirements.txt python e2e/fake_agent_server.py',
    cwd: '..',
    url: `http://127.0.0.1:${PORT}/app`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
