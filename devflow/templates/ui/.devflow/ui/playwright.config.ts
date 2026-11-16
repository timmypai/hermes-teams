import { defineConfig } from '@playwright/test';

// 由 .devflow/playwright.sh 在官方映像內執行；BASE_URL 指向 compose network 內的服務。
export default defineConfig({
  testDir: '.',
  outputDir: '/evidence/playwright',
  reporter: [['list'], ['json', { outputFile: '/evidence/playwright-report.json' }]],
  use: { baseURL: process.env.BASE_URL, screenshot: 'only-on-failure', trace: 'retain-on-failure' },
});
