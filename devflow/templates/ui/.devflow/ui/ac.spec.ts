import { test, expect } from '@playwright/test';

// 一條 EPIC 驗收（AC-n）一個 test：名稱以 AC 編號開頭，像使用者一樣操作（role/label/text 定位），
// 斷言畫面上看得到的結果，最後把畫面存成 /evidence/AC-n.png 當證據。改寫這個範例。
test('AC-1 首頁可開啟並顯示標題', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await page.screenshot({ path: '/evidence/AC-1.png', fullPage: true });
});
