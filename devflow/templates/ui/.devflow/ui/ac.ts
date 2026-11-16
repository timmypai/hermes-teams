import { test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import * as fs from 'node:fs';
import * as path from 'node:path';

// devflow 驗收情境 helper：EPIC 的每個情境（<EPIC>/AC-n.Sm）寫成一次 scenario() 呼叫，三個 phase：
//   Given（可選 act，必須以 check 斷言前置狀態結束）→ When（act）→ Then（check）。
// 每個 phase 是一個 Playwright step（標題 "Given <文字>" 等，文字要與 EPIC 完全相同），並把結果
// 以一行 JSON 附加到 $DEVFLOW_EVENTS（容器內 /evidence/scenario-events.jsonl），由 .devflow/acceptance.py 組成報告。
// phase 失敗：記下錯誤、後面的 phase 記 not-run，再把錯誤丟回去讓 Playwright 判這個 test 失敗。
// kind 'ui' 拿到 { page, request }，Then 之後（不論成敗）存整頁截圖 /evidence/shots/<id 的 / 換成 __>.png；
// kind 'api' 只拿到 { request }（不開瀏覽器）。id 格式錯或缺 phase = 定義時就丟錯（收集失敗，整輪紅燈）。
// smoke: true 只給唯讀情境（不 seed、不寫入）：部署後會對正式服務跑。

const ID_RE = /^(t_[0-9A-Za-z]+)\/(AC-\d+)\.(S\d+)$/;
const EVENTS = process.env.DEVFLOW_EVENTS || '/evidence/scenario-events.jsonl';
const SHOTS = path.join(path.dirname(EVENTS), 'shots');
const MAX_ERROR = 2000;

export type Kind = 'ui' | 'api';
export type UiFixtures = { page: Page; request: APIRequestContext };
export type ApiFixtures = { request: APIRequestContext };
export type Fixtures<K extends Kind> = K extends 'ui' ? UiFixtures : ApiFixtures;
export type Step<K extends Kind> = (f: Fixtures<K>) => Promise<void>;

export interface Scenario<K extends Kind> {
  kind: K;
  smoke?: boolean;
  given: { title: string; act?: Step<K>; check: Step<K> };
  when: { title: string; act: Step<K> };
  then: { title: string; check: Step<K> };
}

type Phase = 'given' | 'when' | 'then';
type Status = 'passed' | 'failed' | 'not-run';

function record(id: string, smoke: boolean, phase: Phase, title: string, status: Status, error = ''): void {
  const line = { id, runner: 'playwright', smoke, phase, title, status, error: error.slice(0, MAX_ERROR) };
  fs.mkdirSync(path.dirname(EVENTS), { recursive: true });
  fs.appendFileSync(EVENTS, JSON.stringify(line) + '\n');
}

function message(e: unknown): string {
  const text = e instanceof Error ? e.message || String(e) : String(e);
  return text.replace(/\u001b\[[0-9;]*[A-Za-z]/g, '');
}

async function screenshot(id: string, page: Page, testInfo: TestInfo): Promise<void> {
  const file = path.join(SHOTS, `${id.replace(/\//g, '__')}.png`);
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    const body = await page.screenshot({ path: file, fullPage: true });
    await testInfo.attach('screenshot', { body, contentType: 'image/png' });
  } catch {
    // 截圖失敗（頁面已關閉等）不能蓋掉情境本身的結果。
  }
}

function invalid(id: string, why: string): Error {
  return new Error(`devflow scenario ${id}: ${why}`);
}

export function scenario<K extends Kind>(id: string, description: string, spec: Scenario<K>): void {
  if (!ID_RE.test(id)) throw invalid(id, 'id must look like t_ab12/AC-1.S1 (<EPIC>/AC-n.Sm)');
  if (!description || !description.trim()) throw invalid(id, 'description is required');
  if (spec.kind !== 'ui' && spec.kind !== 'api') throw invalid(id, "kind must be 'ui' or 'api'");
  for (const p of ['given', 'when', 'then'] as const) {
    const t = spec[p]?.title;
    if (typeof t !== 'string' || !t.trim()) throw invalid(id, `${p}.title is required (the EPIC ${p} text)`);
  }
  if (typeof spec.given.check !== 'function') throw invalid(id, 'given.check is required: Given ends by asserting the precondition');
  if (spec.given.act !== undefined && typeof spec.given.act !== 'function') throw invalid(id, 'given.act must be a function');
  if (typeof spec.when.act !== 'function') throw invalid(id, 'when.act is required');
  if (typeof spec.then.check !== 'function') throw invalid(id, 'then.check is required');

  const smoke = spec.smoke === true;
  const title = `${id} ${description}${smoke ? ' @smoke' : ''}`;
  const phases: [Phase, string, Step<K>][] = [
    ['given', spec.given.title, async (f) => {
      if (spec.given.act) await spec.given.act(f);
      await spec.given.check(f);
    }],
    ['when', spec.when.title, (f) => spec.when.act(f)],
    ['then', spec.then.title, (f) => spec.then.check(f)],
  ];
  const label: Record<Phase, string> = { given: 'Given', when: 'When', then: 'Then' };

  const run = async (f: Fixtures<K>, testInfo: TestInfo, page?: Page): Promise<void> => {
    let failed = false;
    let failure: unknown;
    try {
      for (const [phase, text, fn] of phases) {
        if (failed) {
          record(id, smoke, phase, text, 'not-run');
          continue;
        }
        try {
          await test.step(`${label[phase]} ${text}`, () => fn(f));
          record(id, smoke, phase, text, 'passed');
        } catch (e) {
          record(id, smoke, phase, text, 'failed', message(e));
          failed = true;
          failure = e;
        }
      }
    } finally {
      if (page) await screenshot(id, page, testInfo);
    }
    if (failed) throw failure;
  };

  // Playwright 依 test 函式的參數解構決定要建哪些 fixture：api 情境不解構 page，就不會開瀏覽器。
  if (spec.kind === 'ui') {
    test(title, async ({ page, request }, testInfo) => run({ page, request } as unknown as Fixtures<K>, testInfo, page));
  } else {
    test(title, async ({ request }, testInfo) => run({ request } as unknown as Fixtures<K>, testInfo));
  }
}
