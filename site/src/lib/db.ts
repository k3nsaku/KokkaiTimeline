/**
 * ブラウザから期間ごとのDBを引く層。
 *
 * **SQL とページ送りの計算は `query.ts` にある。** ここはワーカの管理と、
 * 期間ごとの問い合わせを並列に投げる部分だけを持つ。分けてあるのは、
 * SQL 側をブラウザ無しでテストできるようにするため（`site/test/`）。
 *
 * ここに残る決まりごとは2つ:
 *
 *   1. 期間またぎは**期間ごとに別ワーカ**で並列に引く。1ワーカ内の通信は同期XHRなので
 *      ATTACH+UNION にすると待ち時間が期間数に比例する
 *   2. `db.query(sql, params)` の第2引数は**配列で1個**渡す。型定義は (sql, ...params)
 *      だが実体は sql.js の exec(sql, params) で、展開すると黙って束縛されない
 */

import { createDbWorker, type WorkerHttpvfs } from "sql.js-httpvfs";
import {
  countQuery, fillBounds, mergePages, monthBoundsQuery, monthlyQuery, monthsInPeriod,
  searchQuery, splitTerms, timelineQuery, toMatchExpr,
  wordPlan, wordProbeKeys, periodOfIssueId, periodOfSpeechId,
  type QueryPlan, type SearchOptions, type SearchPage, type SpeechRow,
} from "./query";

export {
  canonicalQuery, splitTerms, toFullWidth, toMatchExpr, toWordKey,
  MAX_TERMS, MAX_TERM_LENGTH, MAX_INPUT_LENGTH, MAX_URL_LENGTH, MAX_MEETING_LENGTH,
  periodOf, periodOfSpeechId, periodsInYearRange, unsearchableTerms,
  yearOfPeriod, yearsOfPeriods, yearOfSpeechId,
  type PeriodRule, type QueryPlan, type SearchOptions, type SearchPage, type SpeechRow,
} from "./query";

/** page_size と一致させる。4096比でリクエスト18%減（docs/DECISIONS.md）。 */
const CHUNK = 8192;

/** 本番は R2 のカスタムドメイン。開発は Astro のミドルウェアが /db で配る。 */
export const DB_BASE = (import.meta.env.PUBLIC_DB_BASE ?? "/db").replace(/\/$/, "");

export interface Manifest {
  /** 分割の単位。`scripts/build_db.py` の `--period` と同じ語 */
  period: "half" | "year";
  /** 期間IDを古い順に。辞書順が時系列順（`2021H1` < `2021H2` < `2022H1`） */
  periods: string[];
  databases: {
    id: string; file: string; size: number;
    /** そのファイルの中身の指紋。URL に付けて配信の世代を分ける */
    version?: string;
    /** 実データの収録範囲。年での絞り込みを期間に直すのに使う */
    from?: string; to?: string;
  }[];
}

// --- ワーカの管理 ---------------------------------------------------------
//
// 期間ごとに1つ。作るのは約50msなので、必要になった期間だけ遅延で立てる。

const workers = new Map<string, Promise<WorkerHttpvfs>>();
let manifestPromise: Promise<Manifest> | null = null;
/** やり直しのときにURLを変えるための通し番号（ブラウザキャッシュを外す用）。 */
let retryCount = 0;

async function fetchManifest(suffix = ""): Promise<Manifest> {
  const res = await fetch(`${DB_BASE}/manifest.json${suffix}`);
  if (!res.ok) throw new Error(`目録を読めません: ${res.status}`);
  return (await res.json()) as Manifest;
}

/**
 * 配信DBの目録。**セッションに1回だけ引いて使い回す。**
 *
 * ★ **失敗したらURLを変えて1度だけ引き直す。** エッジに `Access-Control-Allow-Origin`
 * の無い応答が載ることがあるため（R2 は `Origin` の付いた要求にだけ CORS ヘッダを
 * 返し、応答に `Vary: Origin` を付けない。`Origin` 無しの要求が先にキャッシュを
 * 埋めると、それが全ブラウザに配られる）。**目録が読めないとサイトは丸ごと止まる**
 * ので、ここだけは自力で抜け出せるようにしておく。2026-08-21 に実際に踏んだ
 * （日次の「配る → 検算する」の検算側が発生源。`scripts/verify_published.py`）。
 *
 * 失敗を握ったままにしない（`manifestPromise` を戻す）。握ると、次の操作まで
 * 同じ失敗を配り続けることになる。
 */
export function getManifest(): Promise<Manifest> {
  manifestPromise ??= fetchManifest()
    .catch((first) => {
      console.warn("目録を引き直す（キャッシュを外す）", first);
      return fetchManifest(`?retry=${++retryCount}`);
    })
    .catch((err) => {
      manifestPromise = null;
      throw err;
    });
  return manifestPromise;
}

async function workerFor(period: string, bustCache = false): Promise<WorkerHttpvfs> {
  let w = workers.get(period);
  if (!w) {
    const manifest = await getManifest();
    const entry = manifest.databases.find((d) => d.id === period);
    // ファイル名は**目録の記載を使う**（規則で組み立てない）。
    // 分割の単位が変わっても、目録さえ正しければ引き先を間違えない
    const url = `${DB_BASE}/${entry?.file ?? `kokkai-${period}.db`}`
      + (entry?.version ? `?v=${entry.version}` : "?v=0")
      // やり直しのときだけ、URLを変えてブラウザキャッシュを外す。
      // 壊れたものがブラウザキャッシュに入ると、閉じた期間は差し替わらないので
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
    workers.set(period, w);
  }
  return w;
}

async function query<T = Record<string, unknown>>(
  period: string, sql: string, params: unknown[] = [],
): Promise<T[]> {
  try {
    const { db } = await workerFor(period);
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
    //        再読み込みでは読み直されず、閉じた期間は `?v=` も変わらない
    //
    // **やり直しは1回だけ。** 本当に誤っている問い合わせを無限に投げ直さない。
    //
    // 古いワーカは捨てるだけで、止められない（sql.js-httpvfs が Worker を
    // 外に出さないため）。差し替えは1日1回なので、取りこぼしても1つ残るだけ。
    console.warn(`${period} のDBを読み直す`, first);
    void reportBadHeader(period, first);
    manifestPromise = null;
    workers.delete(period);
    const { db } = await workerFor(period, true);
    return (await db.query(sql, params as never)) as T[];
  }
}

/**
 * `file is not a database` を踏んだとき、**実際に返ってきた先頭16バイト**を出す。
 *
 * これが無いと、報告を受けても「ゼロ埋めか・HTMLか・別の世代のDBか」が分からず、
 * 毎回サーバとブラウザのどちらが悪いのかを一から確かめ直すことになる
 * （2026-08-02 と 2026-08-19 に2回やった。docs/ROADMAP.md「未解決の不具合」）。
 *
 * **メインスレッドの `fetch` を使うのが肝。** ワーカ内は同期XHRなので
 * `cache` を指定できないが、ここでは `reload` と既定の両方を引けるので、
 * **ブラウザキャッシュが壊れているのかネットワークが壊れているのかを分けられる。**
 *
 * 表示には出さない（利用者向けの文言は `describeError()`）。失敗しても黙る。
 */
async function reportBadHeader(period: string, err: unknown): Promise<void> {
  if (!/not a database/.test(String(err))) return;
  try {
    const entry = (await getManifest()).databases.find((d) => d.id === period);
    if (!entry) return;
    const url = `${DB_BASE}/${entry.file}` + (entry.version ? `?v=${entry.version}` : "");
    const head = async (cache: RequestCache) => {
      const res = await fetch(url, { headers: { Range: "bytes=0-15" }, cache });
      const bytes = [...new Uint8Array(await res.arrayBuffer())];
      return {
        status: res.status,
        contentRange: res.headers.get("content-range"),
        contentType: res.headers.get("content-type"),
        head16: bytes.map((b) => b.toString(16).padStart(2, "0")).join(" "),
        isSqlite: String.fromCharCode(...bytes.slice(0, 16)) === "SQLite format 3\0",
      };
    };
    console.warn(`${period} の先頭16バイト`, {
      url, size: entry.size,
      cached: await head("default"),
      network: await head("reload"),
    });
  } catch {
    // 診断が失敗しても本筋（やり直し）は続ける
  }
}

/** 期間ごとに同じ問い合わせを並列で投げる。ワーカが別なので本当に並列に動く。 */
async function eachPeriod<T>(
  periods: string[], run: (period: string) => Promise<T>,
): Promise<T[]> {
  return Promise.all(periods.map(run));
}

/** 目録にある期間だけに絞り、**新しい順**に並べる（辞書順が時系列順）。 */
async function targetPeriods(wanted: string[] | undefined): Promise<string[]> {
  const manifest = await getManifest();
  const all = manifest.periods;
  return (wanted?.length ? wanted.filter((p) => all.includes(p)) : [...all])
    .sort((a, b) => b.localeCompare(a));
}

// --- 検索 -----------------------------------------------------------------

/**
 * 検索語をどの索引で引くか決める（`QueryPlan`）。
 *
 * **2文字語の索引は期間ごとに中身が違う**（本文に出てきた2文字窓をその期間ぶん
 * 入れるだけなので）。だから件数は**対象の全期間から引いて合算する**。
 * 1期間だけ見て決めると、その期間にたまたま出てこない語で他の期間まで0件になる。
 *
 * 引く量は期間あたり1行（`word.term` の UNIQUE 索引を突くだけ）で、
 * どのみち検索で全期間のワーカを立てるので、増えるのは往復1回ぶんだけ。
 */
export async function resolveQuery(input: string, periods: string[]): Promise<QueryPlan> {
  const terms = splitTerms(input);
  const probe = wordProbeKeys(terms);
  if (terms.length && !probe.length) return { mode: "fts", match: toMatchExpr(input) };

  const targets = await targetPeriods(periods);
  const placeholders = probe.map(() => "?").join(",");
  const perPeriod = probe.length
    ? await eachPeriod(targets, (period) => query<{ term: string; n_speeches: number }>(
        period, `SELECT term, n_speeches FROM word WHERE term IN (${placeholders})`, probe))
    : [];

  const counts = new Map<string, number>();
  for (const row of perPeriod.flat()) {
    counts.set(row.term, (counts.get(row.term) ?? 0) + row.n_speeches);
  }
  // 引き当てた後の判断は query.ts（純粋関数）に置いてある。テストはそちらから
  return wordPlan(terms, counts);
}

export async function search(opts: SearchOptions): Promise<SearchPage> {
  const periods = await targetPeriods(opts.periods);
  const limit = opts.limit ?? 20;
  const before = opts.before ?? {};
  const plan = opts.plan;

  const perPeriod = await eachPeriod(periods, async (period) => {
    // その期間を読み切っている（前ページで LIMIT に満たなかった）なら、もう投げない
    if (period in before && before[period] === 0) return [] as SpeechRow[];
    const [sql, params] = searchQuery({ ...opts, limit }, plan, before[period]);
    const rows = await query<SpeechRow>(period, sql, params);
    // snippet() のマーカーが入るのは FTS 経由のときだけ
    const marked = opts.topicId == null && plan?.mode !== "word";
    // 年は**日付から**採る（見出しに出すだけ。引き先の決定は期間IDでやっている）
    return rows.map((r) => ({ ...r, year: Number(r.date.slice(0, 4)), marked }));
  });

  return mergePages(periods, perPeriod, limit, before);
}

/** 件数。結果の表示とは別に走らせて、出たところで差し込む（FTS の COUNT は重い）。 */
export async function countHits(opts: SearchOptions): Promise<number> {
  const periods = await targetPeriods(opts.periods);
  const counts = await eachPeriod(periods, async (period) => {
    const [row] = await query<{ n: number }>(period, ...countQuery(opts, opts.plan));
    return row?.n ?? 0;
  });

  return counts.reduce((a, b) => a + b, 0);
}

// --- 月別の件数（検索結果のグラフ）-----------------------------------------

export interface MonthlyPoint {
  /** `YYYY-MM` */
  month: string;
  n: number;
}

/** 期間 × 世代 → 月境界。**世代（目録の `version`）をキーに含める。**
 *  日次でDBが差し替わると rowid の付き直しで境界が動くので、
 *  期間だけをキーにすると古い境界で月を割ってしまう。 */
const monthBoundsCache = new Map<string, Promise<number[]>>();

async function monthBounds(period: string, months: string[]): Promise<number[]> {
  const entry = (await getManifest()).databases.find((d) => d.id === period);
  const key = `${period}:${entry?.version ?? "0"}`;
  let bounds = monthBoundsCache.get(key);
  if (!bounds) {
    const [sql, params] = monthBoundsQuery(months);
    bounds = query<{ i: number; at: number | null }>(period, sql, params)
      .then((rows) => fillBounds(
        [...rows].sort((a, b) => a.i - b.i).map((r) => r.at), months.length))
      .catch((e) => { monthBoundsCache.delete(key); throw e; });
    monthBoundsCache.set(key, bounds);
  }
  return bounds;
}

/**
 * 月ごとの件数。**検索結果ページのグラフはこれで描く。**
 *
 * 件数（`countHits`）とは別に走らせる。絞り込みが無いときの件数は索引の1行で
 * 出るので（`countQuery` の近道）、そちらを月別で置き換えると速い場合まで
 * 遅くなる。**同じ索引を2回読むが、2回目はワーカのページキャッシュに乗る。**
 *
 * 月は期間DBごとに閉じている（`build_db.py` が期間で分ける）ので、
 * 期間をまたいでも足し合わせは要らない——古い順に並べるだけでよい。
 */
export async function monthlyHits(opts: SearchOptions): Promise<MonthlyPoint[]> {
  const periods = await targetPeriods(opts.periods);
  const manifest = await getManifest();

  const perPeriod = await eachPeriod(periods, async (period) => {
    const entry = manifest.databases.find((d) => d.id === period);
    // 収録範囲で月を詰める。**無い月まで並べると seek が空振りする**
    const months = monthsInPeriod(period, entry?.from || undefined, entry?.to || undefined);
    if (!months.length) return [] as MonthlyPoint[];

    const bounds = await monthBounds(period, months);
    const [sql, params] = monthlyQuery(opts, opts.plan, bounds);
    const rows = await query<{ b: number; n: number }>(period, sql, params);
    return rows.flatMap((r) => (months[r.b] ? [{ month: months[r.b], n: r.n }] : []));
  });

  return perPeriod.flat().sort((a, b) => a.month.localeCompare(b.month));
}

// --- 議員ページ -----------------------------------------------------------

/** 議員の発言タイムライン。 */
export async function politicianSpeeches(
  politicianId: number, periods: string[], limit = 20, before: Record<string, number> = {},
): Promise<SearchPage> {
  const sorted = await targetPeriods(periods);
  const perPeriod = await eachPeriod(sorted, async (period) => {
    if (before[period] === 0) return [] as SpeechRow[];
    const [sql, params] = timelineQuery(politicianId, before[period], limit);
    const rows = await query<SpeechRow>(period, sql, params);
    return rows.map((r) => ({ ...r, year: Number(r.date.slice(0, 4)), marked: false }));
  });

  return mergePages(sorted, perPeriod, limit, before);
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
  const period = periodOfSpeechId(speechId, (await getManifest()).period);
  if (period == null) return null;
  const [row] = await query<SpeechDetail>(period, `
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
  const period = periodOfIssueId(issueId, (await getManifest()).period);
  if (period == null) return [];
  return query<ContextRow>(period, `
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
 * 絞り込みを変えるたびに引き直すことになるので、期間ごとに覚えておく
 * （DBは差し替わっても、その期間に開かれた会議の顔ぶれは変わらない）。
 */
const meetingNameCache = new Map<string, Promise<string[]>>();

export async function meetingNames(periods: string[]): Promise<string[]> {
  const perPeriod = await eachPeriod(await targetPeriods(periods), (period) => {
    let p = meetingNameCache.get(period);
    if (!p) {
      p = query<{ name: string }>(period, "SELECT DISTINCT name FROM meeting ORDER BY name")
        .then((rows) => rows.map((r) => r.name))
        .catch((e) => { meetingNameCache.delete(period); throw e; });
      meetingNameCache.set(period, p);
    }
    return p;
  });
  return [...new Set(perPeriod.flat())].sort((a, b) => a.localeCompare(b, "ja"));
}
