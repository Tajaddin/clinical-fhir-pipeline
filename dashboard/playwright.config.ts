import { defineConfig } from '@playwright/test';

// The API must already be serving the e2e fixture (make e2e-fixture; make serve-e2e). The dev server is
// started here and proxies /api to it.
export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 20_000 },
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env['DASHBOARD_BASE'] ?? 'http://localhost:4200',
    trace: 'retain-on-failure',
  },
  webServer: process.env['DASHBOARD_BASE']
    ? undefined
    : {
        command: 'npx ng serve --port 4200 --proxy-config proxy.conf.json',
        url: 'http://localhost:4200',
        reuseExistingServer: true,
        timeout: 180_000,
      },
});
