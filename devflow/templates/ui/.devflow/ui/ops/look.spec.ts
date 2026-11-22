import { test, expect } from '@playwright/test';
import { writeFileSync } from 'node:fs';

// 操作腳本範例（唯讀）：開一頁、截圖、回報狀態碼與看得到的文字，讓 Hermes 自己看正式服務的畫面。
//   devflow operate <slug> look --arg path=/login
// 寫新操作照這個形狀：參數從 DEVFLOW_OP_ARGS（JSON）讀，秘密從環境變數讀（repo secret DEVFLOW_OPS_ENV），
// 結果寫成 DEVFLOW_OP_RESULT（JSON），截圖存 /evidence/*.png。不要把秘密寫進結果、截圖或 console。
test('look', async ({ page }) => {
  const { path = '/' } = JSON.parse(process.env.DEVFLOW_OP_ARGS || '{}');
  expect(typeof path === 'string' && path.startsWith('/') && !path.startsWith('//'), 'path 要是服務內的路徑').toBe(true);
  const res = await page.goto(path);
  await page.screenshot({ path: '/evidence/look.png', fullPage: true });
  writeFileSync(process.env.DEVFLOW_OP_RESULT!, JSON.stringify({
    status: res?.status() ?? null,
    path: new URL(page.url()).pathname,
    title: await page.title(),
    text: (await page.locator('body').innerText()).slice(0, 4000),
  }, null, 2));
});
