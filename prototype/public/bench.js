/**
 * docs/DECISIONS.md の未検証リスクを潰すための計測。
 *
 * 測るのは4つ:
 *   - 1回の検索で飛ぶ HTTP Range リクエスト数（R2 Class B 無料枠 月1,000万の消費量）
 *   - 転送バイト数
 *   - 初回検索までの待ち時間（wasm + DBヘッダ + インデックス）
 *   - ヒット数が多い語での最悪ケース
 *
 * リクエスト数は sql.js-httpvfs 自身の getStats() とサーバ側の実測を突き合わせる。
 * 片方だけだと取りこぼしに気づけない。
 */

const $ = (id) => document.getElementById(id);
const logEl = $("log");

function log(...parts) {
  logEl.textContent += parts.join(" ") + "\n";
  logEl.scrollTop = logEl.scrollHeight;
}

// --- 実際のUIで投げることになる問い合わせ ---------------------------------

const SELECT_COLS = `
  s.speech_id, s.date, s.speaker, s.speaker_group,
  snippet(speech_fts, 0, '[', ']', '…', 20) AS snip`;

// ★ 新しい順は rowid DESC で書く。rowid は日付昇順に投入してある（build_db.py）。
const SEARCH = `
  SELECT ${SELECT_COLS}
  FROM speech_fts f JOIN speech s ON s.rowid = f.rowid
  WHERE speech_fts MATCH ?
  ORDER BY f.rowid DESC LIMIT 20`;

// 比較用の「悪い書き方」。ORDER BY date は一時B-TREEを作りヒット全件を読む
const SEARCH_BY_DATE = `
  SELECT ${SELECT_COLS}
  FROM speech_fts f JOIN speech s ON s.rowid = f.rowid
  WHERE speech_fts MATCH ?
  ORDER BY s.date DESC LIMIT 20`;

const SEARCH_PAGE2 = `
  SELECT ${SELECT_COLS}
  FROM speech_fts f JOIN speech s ON s.rowid = f.rowid
  WHERE speech_fts MATCH ?
  ORDER BY f.rowid DESC LIMIT 20 OFFSET 80`;

// 議員ページ。politician_id で引き、所属はその時点のものを表示する
const MEMBER = `
  SELECT s.speech_id, s.date, a.kaiha, a.party, substr(s.body, 1, 120) AS head
  FROM speech s
  LEFT JOIN affiliation a ON a.politician_id = s.politician_id
                         AND s.date BETWEEN a.start_date AND a.end_date
  WHERE s.politician_id = ?
  ORDER BY s.rowid DESC LIMIT 50`;

const MEMBER_BY_DATE = `
  SELECT s.speech_id, s.date, substr(s.body, 1, 120) AS head
  FROM speech s WHERE s.politician_id = ?
  ORDER BY s.date DESC LIMIT 50`;

const MEMBER_LIST = `
  SELECT id, name, name_kana, house, n_speeches FROM politician
  ORDER BY n_speeches DESC LIMIT 50`;

const CONTEXT = `
  SELECT speech_order, speaker, speaker_kind, substr(body, 1, 200) AS head
  FROM speech WHERE issue_id = ? AND speech_order BETWEEN ? AND ?
  ORDER BY speech_order`;

// 発言数の多い議員。scripts/build_politicians.py の採番なので、
// data/politician_ids.json を作り直さない限り変わらない
const MEMBER_ID = 9;

// db.query の第2引数は「配列で1個」。型定義は (sql, ...params) だが
// 実際は sql.js の exec(sql, params) にそのまま渡るので、展開して渡すと束縛されない。
const SCENARIOS = [
  { id: "open", label: "DBを開くだけ（ヘッダ読み）",
    run: (db) => db.query("SELECT 1 AS ok") },
  { id: "search-166", label: "検索 再稼働（166件）新しい順20",
    run: (db) => db.query(SEARCH, ['"再稼働"']) },
  { id: "search-795", label: "検索 原子力（795件）新しい順20",
    run: (db) => db.query(SEARCH, ['"原子力"']) },
  { id: "search-2864", label: "検索 安全保障（2,864件）新しい順20 ★最悪ケース",
    run: (db) => db.query(SEARCH, ['"安全保障"']) },
  { id: "search-2864-page5", label: "検索 安全保障 5ページ目（OFFSET 80）",
    run: (db) => db.query(SEARCH_PAGE2, ['"安全保障"']) },
  { id: "search-and", label: "検索 原子力 AND 再稼働",
    run: (db) => db.query(SEARCH, ['"原子力" AND "再稼働"']) },
  { id: "search-count", label: "件数だけ 安全保障（COUNT）",
    run: (db) => db.query("SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?",
                          ['"安全保障"']) },
  { id: "search-long", label: "検索 デジタル田園都市国家構想（14件）",
    run: (db) => db.query(SEARCH, ['"デジタル田園都市国家構想"']) },
  { id: "member", label: "議員ページ 新しい順50件（所属をJOIN）",
    run: (db) => db.query(MEMBER, [MEMBER_ID]) },
  { id: "member-list", label: "議員一覧 発言数順50人",
    run: (db) => db.query(MEMBER_LIST) },
  { id: "context", label: "前後の発言（予算委員会 2025-03-28 の5件）",
    run: (db) => db.query(CONTEXT, ["121715261X01420250328", 100, 104]) },
  // --- 比較用: 素直に ORDER BY date と書いた場合。採用してはいけない書き方 ---
  { id: "x-search-by-date", label: "✗ 検索 安全保障 ORDER BY date DESC",
    run: (db) => db.query(SEARCH_BY_DATE, ['"安全保障"']) },
  { id: "x-member-by-date", label: "✗ 議員ページ ORDER BY date DESC",
    run: (db) => db.query(MEMBER_BY_DATE, [MEMBER_ID]) },
];

// --- 計測 -----------------------------------------------------------------

async function serverStats(reset) {
  const res = await fetch(`/stats${reset ? "?reset=1" : ""}`, { cache: "no-store" });
  return res.json();
}

/** DBへの1リクエストごとの遅延をサーバに設定する。localhost の往復は0なので、
 *  実測RTT（Cloudflareエッジまで中央値19.6ms）を入れて本番の効き方を再現する。 */
async function applyDelay() {
  const ms = Number($("rtt").value) || 0;
  await fetch(`/delay?ms=${ms}`, { cache: "no-store" });
  return ms;
}

async function newWorker(dbUrl, chunk) {
  return window.createDbWorker(
    [{ from: "inline", config: { serverMode: "full", requestChunkSize: chunk, url: dbUrl } }],
    // wasm の URL はワーカ内で解決されるので相対パスにしない（./ だと /vendor/vendor/ を見に行く）
    "/vendor/sqlite.worker.js",
    "/vendor/sql-wasm.wasm",
  );
}

/** 1シナリオ: 毎回ワーカを作り直してコールドで測り、続けてウォームで測る。 */
async function measure(scenario, dbUrl, chunk) {
  await serverStats(true);

  const tWorker = performance.now();
  const w = await newWorker(dbUrl, chunk);
  const workerMs = performance.now() - tWorker;

  const tCold = performance.now();
  const rows = await scenario.run(w.db);
  const coldMs = performance.now() - tCold;
  const cold = await serverStats(true);
  const selfStats = await w.worker.getStats();

  const tWarm = performance.now();
  await scenario.run(w.db);
  const warmMs = performance.now() - tWarm;
  const warm = await serverStats(true);

  return {
    id: scenario.id, label: scenario.label, chunk, db: dbUrl, delayMs: Number($("rtt").value) || 0,
    rows: Array.isArray(rows) ? rows.length : 0,
    workerMs: Math.round(workerMs),
    coldMs: Math.round(coldMs),
    coldRequests: cold.requests,
    coldBytes: cold.bytes,
    selfRequests: selfStats ? selfStats.totalRequests : null,
    selfBytes: selfStats ? selfStats.totalFetchedBytes : null,
    warmMs: Math.round(warmMs),
    warmRequests: warm.requests,
    warmBytes: warm.bytes,
    sample: Array.isArray(rows) && rows.length ? JSON.stringify(rows[0]).slice(0, 160) : "",
  };
}

// --- 年またぎ検索 ---------------------------------------------------------
//
// 2通り測る。どちらもリクエスト数は年数に比例するが、待ち時間の出方が違う。
//   attach   : 1ワーカに ATTACH して UNION ALL。リクエストが直列になる
//   parallel : 年ごとに別ワーカを立てて並列に引き、JS側でマージする
// ワーカ内の通信は同期XHRなので、並列化するにはワーカを分けるしかない。

const CROSS_SQL = `
  SELECT s.date, s.speaker, s.speaker_group,
         snippet(speech_fts, 0, '[', ']', '…', 12) AS snip
  FROM speech_fts JOIN speech s ON s.rowid = speech_fts.rowid
  WHERE speech_fts MATCH ?
  ORDER BY speech_fts.rowid DESC LIMIT 20`;

/** ATTACH 版。snippet() と MATCH はスキーマ修飾を受け付けないので FROM だけ修飾する。 */
function attachSql(schemas) {
  return schemas.map((q) => `
    SELECT * FROM (
      SELECT s.date, s.speaker, s.speaker_group,
             snippet(speech_fts, 0, '[', ']', '…', 12) AS snip
      FROM ${q}speech_fts JOIN ${q}speech s ON s.rowid = speech_fts.rowid
      WHERE speech_fts MATCH ? ORDER BY speech_fts.rowid DESC LIMIT 20)`)
    .join(" UNION ALL ") + " ORDER BY date DESC LIMIT 20";
}

async function measureCrossYear(mode, dbUrls, chunk, word) {
  const started = { workers: performance.now() };
  let run;

  if (mode === "attach") {
    const configs = dbUrls.map((url, i) => ({
      from: "inline", virtualFilename: `y${i}.db`,
      config: { serverMode: "full", requestChunkSize: chunk, url },
    }));
    const w = await window.createDbWorker(configs, "/vendor/sqlite.worker.js",
                                          "/vendor/sql-wasm.wasm");
    const schemas = [""];
    for (let i = 1; i < dbUrls.length; i++) {
      await w.db.query(`ATTACH DATABASE 'y${i}.db' AS y${i}`);
      schemas.push(`y${i}.`);
    }
    const sql = attachSql(schemas);
    run = () => w.db.query(sql, schemas.map(() => `"${word}"`));
  } else {
    const ws = await Promise.all(dbUrls.map((url) => newWorker(url, chunk)));
    run = async () => {
      const parts = await Promise.all(ws.map((w) => w.db.query(CROSS_SQL, [`"${word}"`])));
      return parts.flat().sort((a, b) => (a.date < b.date ? 1 : -1)).slice(0, 20);
    };
  }

  const workerMs = performance.now() - started.workers;
  await serverStats(true);
  const t = performance.now();
  const rows = await run();
  const coldMs = performance.now() - t;
  const cold = await serverStats(true);

  return {
    id: `cross-${mode}-${dbUrls.length}年`, label: `年またぎ ${word}（${dbUrls.length}年 / ${mode}）`,
    chunk, db: dbUrls.join(" + "), delayMs: Number($("rtt").value) || 0, rows: rows.length,
    workerMs: Math.round(workerMs), coldMs: Math.round(coldMs),
    coldRequests: cold.requests, coldBytes: cold.bytes,
    selfRequests: null, selfBytes: null,
    warmMs: 0, warmRequests: 0, warmBytes: 0,
    sample: rows.length ? JSON.stringify(rows[0]).slice(0, 160) : "",
  };
}

// --- 表示 -----------------------------------------------------------------

const results = [];

const COLUMNS = [
  ["シナリオ", (r) => r.label],
  ["chunk", (r) => r.chunk],
  ["遅延ms", (r) => r.delayMs],
  ["行", (r) => r.rows],
  ["ワーカ起動ms", (r) => r.workerMs],
  ["初回ms", (r) => r.coldMs],
  ["初回req", (r) => r.coldRequests],
  ["初回KB", (r) => (r.coldBytes / 1024).toFixed(0)],
  ["再検索ms", (r) => r.warmMs],
  ["再検索req", (r) => r.warmRequests],
];

function render() {
  const head = COLUMNS.map(([name]) => `<th>${name}</th>`).join("");
  const body = results.map((r) => {
    // R2 Class B は月1,000万。1検索1,000リクエストを超えると設計として厳しい
    const cls = r.coldRequests > 1000 ? "bad" : r.coldRequests > 300 ? "warn" : "";
    return `<tr class="${cls}">` +
      COLUMNS.map(([, get]) => `<td>${get(r)}</td>`).join("") + "</tr>";
  }).join("");
  $("out").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  $("json").textContent = JSON.stringify(results, null, 1);
}

// --- 実行 -----------------------------------------------------------------

async function runSuite(chunk, { skipBad = false } = {}) {
  const dbUrl = $("db").value;
  const delay = await applyDelay();
  log(`--- ${dbUrl} / requestChunkSize=${chunk} / 遅延 ${delay}ms ---`);
  // 比較用の悪い書き方（x-）はチャンクサイズ比較では回さない。200MB×4回は時間の無駄
  for (const scenario of SCENARIOS.filter((s) => !(skipBad && s.id.startsWith("x-")))) {
    try {
      const r = await measure(scenario, dbUrl, chunk);
      results.push(r);
      log(`${scenario.id}: ${r.coldRequests}req ${(r.coldBytes / 1024).toFixed(0)}KB ` +
          `${r.coldMs}ms (再検索 ${r.warmMs}ms/${r.warmRequests}req) rows=${r.rows}`);
      render();
    } catch (err) {
      log(`${scenario.id}: 失敗 ${err}`);
      console.error(err);
    }
  }
  log(`--- 完了 chunk=${chunk} ---`);
}

async function findDatabases() {
  const found = await (await fetch("/dbs", { cache: "no-store" })).json();
  $("db").innerHTML = found.map(
    (f) => `<option value="${f.url}">${f.url} (${(f.size / 1024 ** 2).toFixed(0)} MB)</option>`,
  ).join("");
  log(found.length ? `DB ${found.length}件を検出` : "DBが見つからない。build_db.py を先に実行すること");
}

/** 選択中のDBと同じディレクトリにある年DBを全部使って、年またぎを測る。 */
async function runCrossYear() {
  const chunk = Number($("chunk").value);
  const dir = $("db").value.slice(0, $("db").value.lastIndexOf("/"));
  const all = await (await fetch("/dbs", { cache: "no-store" })).json();
  const urls = all.map((f) => f.url).filter((u) => u.startsWith(dir + "/")).sort().reverse();
  const delay = await applyDelay();
  log(`--- 年またぎ ${urls.length}年 / chunk=${chunk} / 遅延 ${delay}ms ---`);
  for (const mode of ["parallel", "attach"]) {
    try {
      const r = await measureCrossYear(mode, urls, chunk, "再稼働");
      results.push(r);
      log(`${r.id}: ${r.coldRequests}req ${(r.coldBytes / 1024).toFixed(0)}KB ${r.coldMs}ms`);
      render();
    } catch (err) {
      log(`cross-${mode}: 失敗 ${err}`);
      console.error(err);
    }
  }
}

$("run").onclick = () => runSuite(Number($("chunk").value)).catch((e) => log("失敗", e));
$("runAll").onclick = async () => {
  for (const chunk of [1024, 4096, 8192, 16384, 32768]) await runSuite(chunk, { skipBad: true });
};
$("runCross").onclick = () => runCrossYear().catch((e) => log("失敗", e));

findDatabases();
window.__benchResults = results;
window.__runSuite = runSuite; // コンソールから条件を変えて回すため
