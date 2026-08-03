/**
 * ブラウザから年ごとのDBを引く層。
 *
 * **SQL とページ送りの計算は `query.ts` にある。** ここはワーカの管理と、
 * 年ごとの問い合わせを並列に投げる部分だけを持つ。分けてあるのは、
 * SQL 側をブラウザ無しでテストできるようにするため（`site/test/`）。
 *
 * ここに残る決まりごとは2つ:
 *
 *   1. 年またぎは**年ごとに別ワーカ**で並列に引く。1ワーカ内の通信は同期XHRなので
 *      ATTACH+UNION にすると待ち時間が年数に比例する
 *   2. `db.query(sql, params)` の第2引数は**配列で1個**渡す。型定義は (sql, ...params)
 *      だが実体は sql.js の exec(sql, params) で、展開すると黙って束縛されない
 */

import { createDbWorker, type WorkerHttpvfs } from "sql.js-httpvfs";
import {
  countQuery, mergePages, searchQuery, splitTerms, timelineQuery, toMatchExpr,
  wordPlan, wordProbeKeys, yearOfSpeechId,
  type QueryPlan, type SearchOptions, type SearchPage, type SpeechRow,
} from "./query";

export {
  canonicalQuery, splitTerms, toFullWidth, toMatchExpr, toWordKey, yearOfSpeechId,
  type QueryPlan, type SearchOptions, type SearchPage, type SpeechRow,
} from "./query";

/** page_size と一致させる。4096比でリクエスト18%減（docs/DECISIONS.md）。 */
const CHUNK = 8192;

/** 本番は R2 のカスタムドメイン。開発は Astro のミドルウェアが /db で配る。 */
export const DB_BASE = (import.meta.env.PUBLIC_DB_BASE ?? "/db").replace(/\/$/, "");

export interface Manifest {
  years: number[];
  databases: {
    year: number; file: string; size: number;
    /** そのファイルの中身の指紋。URL に付けて配信の世代を分ける */
    version?: string;
    /** 2文字語の語彙の指紋。年をまたいで一致していなければならない */
    vocabulary?: string | null;
  }[];
}

// --- ワーカの管理 ---------------------------------------------------------
//
// 年ごとに1つ。作るのは約50msなので、必要になった年だけ遅延で立てる。

const workers = new Map<number, Promise<WorkerHttpvfs>>();
let manifestPromise: Promise<Manifest> | null = null;
/** やり直しのときにURLを変えるための通し番号（ブラウザキャッシュを外す用）。 */
let retryCount = 0;

export function getManifest(): Promise<Manifest> {
  manifestPromise ??= fetch(`${DB_BASE}/manifest.json`).then((r) => {
    if (!r.ok) throw new Error(`目録を読めない: ${r.status}`);
    return r.json() as Promise<Manifest>;
  });
  return manifestPromise;
}

async function workerFor(year: number, bustCache = false): Promise<WorkerHttpvfs> {
  let w = workers.get(year);
  if (!w) {
    const manifest = await getManifest();
    const entry = manifest.databases.find((d) => d.year === year);
    // 世代を URL に入れる。日次更新でDBが差し替わっても、
    // 新しく開いたページは別のURLとして取り直す（CDNのパージも要らない）
    const url = `${DB_BASE}/kokkai-${year}.db`
      + (entry?.version ? `?v=${entry.version}` : "?v=0")
      // やり直しのときだけ、URLを変えてブラウザキャッシュを外す。
      // 壊れたものがブラウザキャッシュに入ると、過去年は差し替わらないので
      // `?v=` も変わらず、同じURLを引き直しても同じ壊れたものが返る（実際に踏んだ）。
      // 配信側は `immutable` をやめた（ブラウザは1時間）ので放っておいても
      // いつかは直るが、**利用者を1時間待たせない**ためにここで外す
      + (bustCache ? `&retry=${++retryCount}` : "");
    w = createDbWorker(
      [{ from: "inline", config: { serverMode: "full", requestChunkSize: CHUNK, url } }],
      // ワーカ内で解決されるので絶対パスで渡す（相対だと /vendor/vendor/ を見に行く）
      new URL("/vendor/sqlite.worker.js", location.href).href,
      new URL("/vendor/sql-wasm.wasm", location.href).href,
    );
    workers.set(year, w);
  }
  return w;
}

async function query<T = Record<string, unknown>>(
  year: number, sql: string, params: unknown[] = [],
): Promise<T[]> {
  try {
    const { db } = await workerFor(year);
    // ★ 第2引数は配列で1個。展開して渡すと束縛されない
    return (await db.query(sql, params as never)) as T[];
  } catch (first) {
    // 読めなくなる理由は2つあって、どちらもここで拾う。
    //
    //   1. 日次更新でDBが差し替わった。開きっぱなしのページは「古いページと
    //      新しいページが混ざったDB」を読んで壊れる（`no such table: speech_fts`）。
    //      → 目録を取り直せば新しい世代を頭から読み直せる
    //   2. ブラウザキャッシュに壊れたものが入った（`file is not a database`）。
    //      → **URLを変えないと抜け出せない。** `immutable` で1年握らせているので
    //        再読み込みでは読み直されず、過去年は `?v=` も変わらない
    //
    // **やり直しは1回だけ。** 本当に誤っている問い合わせを無限に投げ直さない。
    //
    // 古いワーカは捨てるだけで、止められない（sql.js-httpvfs が Worker を
    // 外に出さないため）。差し替えは1日1回なので、取りこぼしても1つ残るだけ。
    console.warn(`${year}年のDBを読み直す`, first);
    manifestPromise = null;
    workers.delete(year);
    const { db } = await workerFor(year, true);
    return (await db.query(sql, params as never)) as T[];
  }
}

/** 年ごとに同じ問い合わせを並列で投げる。ワーカが別なので本当に並列に動く。 */
async function eachYear<T>(years: number[], run: (year: number) => Promise<T>): Promise<T[]> {
  return Promise.all(years.map(run));
}

// --- 検索 -----------------------------------------------------------------

/**
 * 検索語をどの索引で引くか決める（`QueryPlan`）。
 * 語彙は全期間から作ってあり年によらないので、**判定は1年ぶんだけ引けばよい**。
 */
export async function resolveQuery(input: string, years: number[]): Promise<QueryPlan> {
  const terms = splitTerms(input);
  if (!terms.length) return { mode: "none", unsupported: [] };

  const probe = wordProbeKeys(terms);
  if (!probe.length) return { mode: "fts", match: toMatchExpr(input) };

  const probeYear = [...years].sort((a, b) => b - a)[0] ?? (await getManifest()).years.at(-1)!;
  const rows = await query<{ term: string; n_speeches: number }>(
    probeYear,
    `SELECT term, n_speeches FROM word WHERE term IN (${probe.map(() => "?").join(",")})`,
    probe);

  // 引き当てた後の判断は query.ts（純粋関数）に置いてある。テストはそちらから
  return wordPlan(terms, new Map(rows.map((r) => [r.term, r.n_speeches])));
}

export async function search(opts: SearchOptions): Promise<SearchPage> {
  const manifest = await getManifest();
  const years = (opts.years?.length ? opts.years : manifest.years)
    .filter((y) => manifest.years.includes(y))
    .sort((a, b) => b - a);
  const limit = opts.limit ?? 20;
  const before = opts.before ?? {};
  const plan = opts.plan;

  if (plan?.mode === "none") {
    return { rows: [], before: Object.fromEntries(years.map((y) => [y, 0])), done: true };
  }

  const perYear = await eachYear(years, async (year) => {
    // その年を読み切っている（前ページで LIMIT に満たなかった）なら、もう投げない
    if (year in before && before[year] === 0) return [] as SpeechRow[];
    const [sql, params] = searchQuery({ ...opts, limit }, plan, before[year]);
    const rows = await query<SpeechRow>(year, sql, params);
    // snippet() のマーカーが入るのは FTS 経由のときだけ
    const marked = opts.topicId == null && plan?.mode !== "word";
    return rows.map((r) => ({ ...r, year, marked }));
  });

  return mergePages(years, perYear, limit, before);
}

/** 件数。結果の表示とは別に走らせて、出たところで差し込む（FTS の COUNT は重い）。 */
export async function countHits(opts: SearchOptions): Promise<number> {
  const manifest = await getManifest();
  const years = (opts.years?.length ? opts.years : manifest.years)
    .filter((y) => manifest.years.includes(y));
  const plan = opts.plan;

  if (plan?.mode === "none") return 0;

  const counts = await eachYear(years, async (year) => {
    const [row] = await query<{ n: number }>(year, ...countQuery(opts, plan));
    return row?.n ?? 0;
  });

  return counts.reduce((a, b) => a + b, 0);
}

// --- 議員ページ -----------------------------------------------------------

/** 議員の発言タイムライン。 */
export async function politicianSpeeches(
  politicianId: number, years: number[], limit = 20, before: Record<number, number> = {},
): Promise<SearchPage> {
  const sorted = [...years].sort((a, b) => b - a);
  const perYear = await eachYear(sorted, async (year) => {
    if (before[year] === 0) return [] as SpeechRow[];
    const [sql, params] = timelineQuery(politicianId, before[year], limit);
    const rows = await query<SpeechRow>(year, sql, params);
    return rows.map((r) => ({ ...r, year, marked: false }));
  });

  return mergePages(sorted, perYear, limit, before);
}

// --- 発言ページ -----------------------------------------------------------

export interface SpeechDetail {
  speech_id: string;
  issue_id: string;
  speech_order: number;
  date: string;
  speaker: string;
  speaker_group: string | null;
  speaker_position: string | null;
  speaker_kind: string;
  politician_id: number | null;
  body: string;
  speech_url: string;
  meeting: string;
  house: string;
  session: number;
  issue: string | null;
  meeting_url: string | null;
  pdf_url: string | null;
}

export async function speechDetail(speechId: string): Promise<SpeechDetail | null> {
  const year = yearOfSpeechId(speechId);
  if (year == null) return null;
  const [row] = await query<SpeechDetail>(year, `
    SELECT s.speech_id, s.issue_id, s.speech_order, s.date, s.speaker, s.speaker_group,
           s.speaker_position, s.speaker_kind, s.politician_id, s.body, s.speech_url,
           m.name AS meeting, m.house, m.session, m.issue, m.meeting_url, m.pdf_url
    FROM speech s JOIN meeting m ON m.issue_id = s.issue_id
    WHERE s.speech_id = ?`, [speechId]);
  return row ?? null;
}

export interface ContextRow {
  speech_id: string;
  speech_order: number;
  speaker: string;
  speaker_kind: string;
  politician_id: number | null;
  head: string;
}

/** 前後の発言。`idx_speech_issue(issue_id, speech_order)` が効く。 */
export async function speechContext(
  issueId: string, order: number, span = 3,
): Promise<ContextRow[]> {
  const year = Number(issueId.slice(-8, -4));
  if (!Number.isInteger(year)) return [];
  return query<ContextRow>(year, `
    SELECT speech_id, speech_order, speaker, speaker_kind, politician_id,
           substr(body, 1, 140) AS head
    FROM speech
    WHERE issue_id = ? AND speech_order BETWEEN ? AND ?
    ORDER BY speech_order`, [issueId, Math.max(0, order - span), order + span]);
}

// --- 絞り込みの選択肢 -----------------------------------------------------

/**
 * 会議名の一覧。meeting は1年で1,100行ほどなので、そのまま数えて出せる。
 *
 * 期間を変えるたびに引き直すことになるので、年ごとに覚えておく
 * （年DBは差し替わっても、その年に開かれた会議の顔ぶれは変わらない）。
 */
const meetingNameCache = new Map<number, Promise<string[]>>();

export async function meetingNames(years: number[]): Promise<string[]> {
  const perYear = await eachYear(years, (year) => {
    let p = meetingNameCache.get(year);
    if (!p) {
      p = query<{ name: string }>(year, "SELECT DISTINCT name FROM meeting ORDER BY name")
        .then((rows) => rows.map((r) => r.name))
        .catch((e) => { meetingNameCache.delete(year); throw e; });
      meetingNameCache.set(year, p);
    }
    return p;
  });
  return [...new Set(perYear.flat())].sort((a, b) => a.localeCompare(b, "ja"));
}
