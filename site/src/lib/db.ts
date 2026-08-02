/**
 * ブラウザから年ごとのDBを引く層。
 *
 * ここに書いてある SQL は `docs/PHASE1_PROTOTYPE.md` の実測に縛られている。
 * **書き換える前に必ず読むこと。** 特に3つ:
 *
 *   1. 「新しい順」は必ず `ORDER BY rowid DESC`。`ORDER BY date DESC` にすると
 *      一時B-TREEができてヒット全件を読みに行く（検索で204MB転送）
 *   2. 年またぎは**年ごとに別ワーカ**で並列に引く。1ワーカ内の通信は同期XHRなので
 *      ATTACH+UNION にすると待ち時間が年数に比例する
 *   3. `db.query(sql, params)` の第2引数は**配列で1個**渡す。型定義は (sql, ...params)
 *      だが実体は sql.js の exec(sql, params) で、展開すると黙って束縛されない
 */

import { createDbWorker, type WorkerHttpvfs } from "sql.js-httpvfs";

/** page_size と一致させる。4096比でリクエスト18%減（PHASE1_PROTOTYPE §3）。 */
const CHUNK = 8192;

/** 本番は R2 のカスタムドメイン。開発は Astro のミドルウェアが /db で配る。 */
export const DB_BASE = (import.meta.env.PUBLIC_DB_BASE ?? "/db").replace(/\/$/, "");

export interface Manifest {
  years: number[];
  databases: { year: number; file: string; size: number }[];
}

export interface SpeechRow {
  speech_id: string;
  date: string;
  speaker: string;
  speaker_group: string | null;
  speaker_position: string | null;
  politician_id: number | null;
  issue_id: string;
  speech_order: number;
  meeting: string;
  house: string;
  /** 検索語の周辺。FTS 経由なら snippet()、争点語経由なら substr() で作る */
  snippet: string;
  /** snippet が SQLite の snippet() 由来か（＝マーカーが入っているか） */
  marked: boolean;
  rowid: number;
  year: number;
}

export interface SearchOptions {
  /** FTS に投げる語。争点語の場合は topicId を使うのでこちらは空 */
  query?: string;
  topicId?: number;
  politicianId?: number;
  meetingName?: string;
  years?: number[];
  limit?: number;
  /** 年 → その年で「ここより前」を続きとして読む rowid。keyset ページング用 */
  before?: Record<number, number>;
  /**
   * `resolveQuery()` の結果。**1回だけ解いて、ページ送りでは使い回す。**
   * 省略すると FTS 扱いになる（＝2文字語は引けない）ので、
   * 任意語の検索では必ず渡すこと。
   */
  plan?: QueryPlan;
}

export interface SearchPage {
  rows: SpeechRow[];
  /** 次ページ用のカーソル。空なら打ち止め */
  before: Record<number, number>;
  done: boolean;
}

// --- ワーカの管理 ---------------------------------------------------------
//
// 年ごとに1つ。作るのは約50msなので、必要になった年だけ遅延で立てる。

const workers = new Map<number, Promise<WorkerHttpvfs>>();
let manifestPromise: Promise<Manifest> | null = null;

export function getManifest(): Promise<Manifest> {
  manifestPromise ??= fetch(`${DB_BASE}/manifest.json`).then((r) => {
    if (!r.ok) throw new Error(`目録を読めない: ${r.status}`);
    return r.json() as Promise<Manifest>;
  });
  return manifestPromise;
}

function workerFor(year: number): Promise<WorkerHttpvfs> {
  let w = workers.get(year);
  if (!w) {
    w = createDbWorker(
      [{
        from: "inline",
        config: { serverMode: "full", requestChunkSize: CHUNK, url: `${DB_BASE}/kokkai-${year}.db` },
      }],
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
  const { db } = await workerFor(year);
  // ★ 第2引数は配列で1個。展開して渡すと束縛されない
  return (await db.query(sql, params as never)) as T[];
}

/** 年ごとに同じ問い合わせを並列で投げる。ワーカが別なので本当に並列に動く。 */
async function eachYear<T>(years: number[], run: (year: number) => Promise<T>): Promise<T[]> {
  return Promise.all(years.map(run));
}

// --- SQL ------------------------------------------------------------------

/** 検索結果と議員ページで共通に要る列。meeting を JOIN するのは会議名を出すため。
 *  meeting は1年で約1,100行（150KB程度）しかなく、最初の数件でワーカのページ
 *  キャッシュに乗るので、追加のリクエストはほぼ初回だけで済む。 */
const RESULT_COLS = `
  s.speech_id, s.date, s.speaker, s.speaker_group, s.speaker_position,
  s.politician_id, s.issue_id, s.speech_order, s.rowid AS rowid,
  m.name AS meeting, m.house AS house`;

/**
 * 全文検索。**`ORDER BY f.rowid DESC`**（＝日付の降順）。
 * ページ送りは OFFSET ではなく rowid の keyset で進める。
 * OFFSET 80 は 134リクエスト・4.1秒かかった（PHASE1_PROTOTYPE §4）のに対し、
 * keyset なら何ページ目でも1ページ目と同じコストで済む。
 */
function ftsSql(opts: { politician: boolean; meeting: boolean; before: boolean }) {
  return `
    SELECT ${RESULT_COLS},
           snippet(speech_fts, 0, char(1), char(2), '…', 24) AS snippet
    FROM speech_fts f
    JOIN speech s ON s.rowid = f.rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE speech_fts MATCH ?
      ${opts.before ? "AND f.rowid < ?" : ""}
      ${opts.politician ? "AND s.politician_id = ?" : ""}
      ${opts.meeting ? "AND m.name = ?" : ""}
    ORDER BY f.rowid DESC LIMIT ?`;
}

/**
 * 争点語での検索。FTS を通さず `topic_hit` を引く。
 * 2文字語（憲法・年金）は FTS では**原理的に引けない**し、引ける語でも 3.3倍速い。
 *
 * snippet() が使えないので `instr()` で語の位置を求めて周辺を切り出す。
 * 別表記でヒットした発言では instr が 0 を返すが、その場合は先頭から出る。
 */
function topicSql(opts: { politician: boolean; meeting: boolean; before: boolean }) {
  return `
    SELECT ${RESULT_COLS},
           substr(s.body, max(1, instr(s.body, t.term) - 40), 160) AS snippet
    FROM topic_hit h
    JOIN topic t ON t.id = h.topic_id
    JOIN speech s ON s.rowid = h.speech_rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE h.topic_id = ?
      ${opts.before ? "AND h.speech_rowid < ?" : ""}
      ${opts.politician ? "AND s.politician_id = ?" : ""}
      ${opts.meeting ? "AND m.name = ?" : ""}
    ORDER BY h.speech_rowid DESC LIMIT ?`;
}

/**
 * 2文字語の検索。FTS5 の trigram は3文字未満のトークンを作れないので、
 * 「増税」「憲法」「年金」「原発」は**原理的に FTS では引けない**。
 * `word` / `word_hit`（機械抽出の語彙、`scripts/build_words.py`）を引く。
 *
 * 複数語のときは**いちばん珍しい2文字語を起点**にして、残りは `instr()` で絞る。
 * 走査する行数が起点の語の件数で頭打ちになるので、起点の選び方が効く。
 */
function wordSql(opts: {
  politician: boolean; meeting: boolean; before: boolean; filters: number;
}) {
  return `
    SELECT ${RESULT_COLS},
           substr(s.body, max(1, instr(s.body, ?) - 40), 160) AS snippet
    FROM word w
    JOIN word_hit h ON h.word_id = w.id
    JOIN speech s ON s.rowid = h.speech_rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE w.term = ?
      ${opts.before ? "AND h.speech_rowid < ?" : ""}
      ${"AND instr(s.body, ?) > 0 ".repeat(opts.filters)}
      ${opts.politician ? "AND s.politician_id = ?" : ""}
      ${opts.meeting ? "AND m.name = ?" : ""}
    ORDER BY h.speech_rowid DESC LIMIT ?`;
}

// --- 検索 -----------------------------------------------------------------

/** trigram にそのまま渡すと記号が演算子として解釈されるので、フレーズとして囲む。 */
export function toMatchExpr(input: string): string {
  const words = splitTerms(input);
  // 二重引用符はフレーズの区切りなので、FTS5 の作法どおり2つ重ねて逃がす
  return words.map((w) => `"${w.replace(/"/g, '""')}"`).join(" AND ");
}

export function splitTerms(input: string): string[] {
  return input.trim().split(/[\s　]+/).filter(Boolean);
}

/**
 * 検索語をどの索引で引くか決める。
 *
 *   fts  : 全部3文字以上。FTS5 trigram（1.5〜6秒）
 *   word : 2文字以下の語がある。`word_hit` を引く
 *   none : 2文字以下の語が語彙に無い。**引けない**ので、代わりの案を出す
 *
 * 語彙は全期間から作ってあり年によらないので、**判定は1年ぶんだけ引けばよい**。
 */
export type QueryPlan =
  | { mode: "fts"; match: string }
  | { mode: "word"; driver: string; filters: string[] }
  | { mode: "none"; unsupported: string[] };

export async function resolveQuery(input: string, years: number[]): Promise<QueryPlan> {
  const terms = splitTerms(input);
  const short = terms.filter((t) => t.length < 3);
  if (!terms.length) return { mode: "none", unsupported: [] };
  if (!short.length) return { mode: "fts", match: toMatchExpr(input) };

  const probeYear = [...years].sort((a, b) => b - a)[0] ?? (await getManifest()).years.at(-1)!;
  const rows = await query<{ term: string; n_speeches: number }>(
    probeYear,
    `SELECT term, n_speeches FROM word WHERE term IN (${short.map(() => "?").join(",")})`,
    short);

  const unsupported = short.filter((t) => !rows.some((r) => r.term === t));
  if (unsupported.length) return { mode: "none", unsupported };

  // いちばん珍しい2文字語を起点にする。走査する行数がこれで決まる
  const driver = [...rows].sort((a, b) => a.n_speeches - b.n_speeches)[0].term;
  return { mode: "word", driver, filters: terms.filter((t) => t !== driver) };
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

  const shape = {
    politician: opts.politicianId != null,
    meeting: !!opts.meetingName,
    before: false,
  };

  const perYear = await eachYear(years, async (year) => {
    // その年を読み切っている（前ページで LIMIT に満たなかった）なら、もう投げない
    if (year in before && before[year] === 0) return [] as SpeechRow[];
    const cursor = before[year];
    const params: unknown[] = [];
    let sql: string;

    if (opts.topicId != null) {
      sql = topicSql({ ...shape, before: cursor != null });
      params.push(opts.topicId);
      if (cursor != null) params.push(cursor);
    } else if (plan?.mode === "word") {
      sql = wordSql({ ...shape, before: cursor != null, filters: plan.filters.length });
      params.push(plan.driver, plan.driver);       // 1つ目は snippet を切り出す位置用
      if (cursor != null) params.push(cursor);
      params.push(...plan.filters);
    } else {
      sql = ftsSql({ ...shape, before: cursor != null });
      params.push(plan?.mode === "fts" ? plan.match : toMatchExpr(opts.query ?? ""));
      if (cursor != null) params.push(cursor);
    }

    if (shape.politician) params.push(opts.politicianId);
    if (shape.meeting) params.push(opts.meetingName);
    params.push(limit);

    const rows = await query<SpeechRow>(year, sql, params);
    // snippet() のマーカーが入るのは FTS 経由のときだけ
    const marked = opts.topicId == null && plan?.mode !== "word";
    return rows.map((r) => ({ ...r, year, marked }));
  });

  // 年DBは日付で綺麗に分かれている（build_db.py が年で分ける）ので、
  // 新しい年から順に並べるだけで全体が日付の降順になる。マージソートは要らない。
  return { rows: perYear.flat(), ...nextCursor(years, perYear, limit, before) };
}

/**
 * 次ページのカーソル。年ごとに「ここより前の rowid」を持つ。
 * LIMIT に満たなかった年は読み切りなので 0 を入れ、次から問い合わせ自体をしない。
 * OFFSET を使わないのは、5ページ目が 134リクエスト・4.1秒になるため
 * （`docs/PHASE1_PROTOTYPE.md` §4）。keyset なら何ページ目でも1ページ目と同じ。
 */
function nextCursor(
  years: number[], perYear: SpeechRow[][], limit: number, before: Record<number, number>,
): { before: Record<number, number>; done: boolean } {
  const next: Record<number, number> = {};
  years.forEach((year, i) => {
    const got = perYear[i];
    next[year] = before[year] === 0 || got.length < limit
      ? 0
      : got[got.length - 1].rowid;
  });
  return { before: next, done: years.every((y) => next[y] === 0) };
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

/** 件数の SQL とパラメータ。議員での絞り込みが無ければ索引だけで数えられる。 */
function countQuery(opts: SearchOptions, plan: QueryPlan | undefined): [string, unknown[]] {
  const byPolitician = opts.politicianId != null;

  if (opts.topicId != null) {
    return byPolitician
      ? [`SELECT COUNT(*) AS n FROM topic_hit h JOIN speech s ON s.rowid = h.speech_rowid
          WHERE h.topic_id = ? AND s.politician_id = ?`, [opts.topicId, opts.politicianId]]
      : ["SELECT COUNT(*) AS n FROM topic_hit WHERE topic_id = ?", [opts.topicId]];
  }

  if (plan?.mode === "word") {
    // 絞り込みが何も無ければ word.n_speeches に答えが入っている（1行読むだけ）
    if (!plan.filters.length && !byPolitician) {
      return ["SELECT n_speeches AS n FROM word WHERE term = ?", [plan.driver]];
    }
    return [`SELECT COUNT(*) AS n FROM word w
             JOIN word_hit h ON h.word_id = w.id
             JOIN speech s ON s.rowid = h.speech_rowid
             WHERE w.term = ?
               ${"AND instr(s.body, ?) > 0 ".repeat(plan.filters.length)}
               ${byPolitician ? "AND s.politician_id = ?" : ""}`,
            [plan.driver, ...plan.filters, ...(byPolitician ? [opts.politicianId] : [])]];
  }

  const match = plan?.mode === "fts" ? plan.match : toMatchExpr(opts.query ?? "");
  return byPolitician
    ? [`SELECT COUNT(*) AS n FROM speech_fts f JOIN speech s ON s.rowid = f.rowid
        WHERE speech_fts MATCH ? AND s.politician_id = ?`, [match, opts.politicianId]]
    : ["SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?", [match]];
}

// --- 議員ページ -----------------------------------------------------------

/**
 * 議員の発言タイムライン。
 * **`speech(politician_id)` だけの索引で `ORDER BY rowid DESC` を降順スキャンさせる。**
 * 索引に date を足すと一時B-TREEに落ちて 27 → 509 リクエストになる。
 */
export async function politicianSpeeches(
  politicianId: number, years: number[], limit = 20, before: Record<number, number> = {},
): Promise<SearchPage> {
  const sorted = [...years].sort((a, b) => b - a);
  const perYear = await eachYear(sorted, async (year) => {
    if (before[year] === 0) return [] as SpeechRow[];
    const cursor = before[year];
    const rows = await query<SpeechRow>(year, `
      SELECT ${RESULT_COLS}, substr(s.body, 1, 160) AS snippet
      FROM speech s
      JOIN meeting m ON m.issue_id = s.issue_id
      WHERE s.politician_id = ? ${cursor != null ? "AND s.rowid < ?" : ""}
      ORDER BY s.rowid DESC LIMIT ?`,
      cursor != null ? [politicianId, cursor, limit] : [politicianId, limit]);
    return rows.map((r) => ({ ...r, year, marked: false }));
  });

  return { rows: perYear.flat(), ...nextCursor(sorted, perYear, limit, before) };
}

// --- 発言ページ -----------------------------------------------------------

/** speech_id は `<issue_id>_<連番>` で、issue_id の末尾8桁が日付。年DBの選択に使う。 */
export function yearOfSpeechId(speechId: string): number | null {
  const m = /^(.+?)_(\d+)$/.exec(speechId);
  const year = Number(m?.[1].slice(-8, -4));
  return Number.isInteger(year) && year > 1900 ? year : null;
}

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

/** 会議名の一覧。meeting は1年で1,100行ほどなので、そのまま数えて出せる。 */
export async function meetingNames(years: number[]): Promise<string[]> {
  const perYear = await eachYear(years, (year) =>
    query<{ name: string }>(year, "SELECT DISTINCT name FROM meeting ORDER BY name"));
  return [...new Set(perYear.flat().map((r) => r.name))].sort((a, b) => a.localeCompare(b, "ja"));
}
