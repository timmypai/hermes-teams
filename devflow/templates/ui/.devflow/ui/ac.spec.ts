import { expect, type APIResponse } from '@playwright/test';
import { scenario } from './ac';

// EPIC 驗收（## 驗收）的每個情境 Sm 寫成一次 scenario()，id = <EPIC>/AC-n.Sm（EPIC = kanban task id）。
// given / when / then 的 title 要與 EPIC 的 Given / When / Then 文字完全相同；Given 一定以 check 斷言前置狀態結束，
// 這樣在改動前的 base 上，情境會在 When / Then 失敗（RED），而不是因為環境沒準備好而失敗。
// ui：像使用者一樣操作（role / label / text 定位），斷言畫面上看得到的結果；Then 之後自動截圖當證據。
// api：只用 request（不開瀏覽器），斷言狀態碼與回應內容。
// smoke: true 只給唯讀情境（不 seed、不寫入）：部署後會對正式服務跑。寫入資料的情境絕不標 smoke。
// 下面兩個是範例（t_example 換成真的 EPIC id），改寫成這個 repo 的驗收。

scenario('t_example/AC-1.S1', '首頁可開啟並顯示標題', {
  kind: 'ui',
  smoke: true, // 唯讀：只開頁面、看標題
  given: {
    title: '服務已啟動',
    check: async ({ request }) => {
      expect((await request.get('/')).ok()).toBe(true);
    },
  },
  when: {
    title: '開啟首頁',
    act: async ({ page }) => {
      await page.goto('/');
    },
  },
  then: {
    title: '看得到頁面的主標題',
    check: async ({ page }) => {
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    },
  },
});

// phase 之間傳資料：用情境自己的變數（When 存回應，Then 斷言）。
let health: APIResponse | undefined;
scenario('t_example/AC-2.S1', '健康檢查回報正常', {
  kind: 'api',
  given: {
    title: '服務已啟動',
    check: async ({ request }) => {
      expect((await request.get('/')).status()).toBeLessThan(500);
    },
  },
  when: {
    title: '呼叫 GET /health',
    act: async ({ request }) => {
      health = await request.get('/health');
    },
  },
  then: {
    title: '回應 200',
    check: async () => {
      expect(health?.status()).toBe(200);
    },
  },
});
