import { defineConfig, devices } from '@playwright/test';
import { defineBddProject } from 'playwright-bdd';

// Two copies of the app with the scripted agent: one as the live site runs today (no
// accounts), and one with the development sign-in turned on for the account tests.
const PORT = 8010;
const ACCOUNTS_PORT = 8011;

const server = (port: number, authMode: string) => ({
  command: 'uv run --no-project --python 3.14 --with-requirements requirements.txt python e2e/fake_agent_server.py',
  cwd: '..',
  // Every test run comes from the same address, so the daily run limit is raised here.
  env: { PORT: String(port), AUTH_MODE: authMode, FREE_RUNS_PER_DAY: '1000' },
  url: `http://127.0.0.1:${port}/app`,
  reuseExistingServer: !process.env.CI,
  timeout: 120_000,
});

export default defineConfig({
  // Tests share the two test servers (and their run limits and saved files), so they run one at a time.
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
  projects: [
    { name: 'specs', testDir: './tests', use: { ...devices['Desktop Chrome'] } },
    // Cucumber scenarios in features/, run by playwright-bdd with the steps in steps/.
    {
      ...defineBddProject({ name: 'cucumber', features: 'features/*.feature', steps: 'steps/*.ts' }),
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'accounts',
      testDir: './account-tests',
      use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${ACCOUNTS_PORT}` },
    },
  ],
  webServer: [server(PORT, 'off'), server(ACCOUNTS_PORT, 'dev')],
});
