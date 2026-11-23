import { defineConfig } from '@playwright/test';

// 由 .devflow/playwright.sh（驗收情境）與 .devflow/operate.sh（ops/ 操作腳本，設 DEVFLOW_OP）在官方映像內執行；
// BASE_URL 指向 compose network 內的服務。驗收不跑 ops/：那些是對正式服務的操作，不是情境。
// retries 0：重試會把不穩定的情境藏成通過。forbidOnly：test.only 會讓其他情境默默消失。
// JSON 報告由 .devflow/acceptance.py 讀（skipped / 失敗 / phase 外的失敗都進驗收報告）。
// actionTimeout / navigationTimeout 小於 test timeout：找不到元素時由該 phase 的動作失敗（base 上 = RED），
// 而不是整個 test 逾時、失敗落在 phase 外（會被判 INVALID）。
export default defineConfig({
  testDir: '.',
  testIgnore: process.env.DEVFLOW_OP ? [] : ['ops/**'],
  outputDir: '/evidence/playwright',
  retries: 0,
  forbidOnly: true,
  timeout: 60_000,
  expect: { timeout: 5_000 },
  reporter: [['list'], ['json', { outputFile: '/evidence/playwright-report.json' }]],
  use: { baseURL: process.env.BASE_URL, actionTimeout: 10_000, navigationTimeout: 15_000,
         screenshot: 'only-on-failure', trace: 'retain-on-failure' },
});
