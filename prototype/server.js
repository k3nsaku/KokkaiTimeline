/**
 * docs/DECISIONS.md の計測用サーバ。
 *
 * R2 の代わりに年ごとDBを HTTP Range で配る。目的は本番配信ではなく
 * **1回の検索で何回リクエストが飛ぶかを数えること**なので、
 * サーバ側でリクエストを記録する（ワーカ内の fetch は外から差し替えられないため）。
 *
 *   /                → public/ の静的ファイル
 *   /db/<name>.db    → ../data/dist/<name>.db を Range 付きで返す
 *   /stats           → 計測値（?reset=1 でゼロに戻す）
 *
 * Node の標準モジュールだけで動く。依存を増やさないのはこのリポジトリの方針。
 */

import { createReadStream, promises as fs } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(HERE, "public");
const DB_DIR = path.join(HERE, "..", "data", "dist");
const PORT = Number(process.env.PORT || 8787);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".wasm": "application/wasm",
  ".db": "application/octet-stream",
};

/** 計測値。GET /stats?reset=1 でゼロに戻す。 */
let stats = freshStats();

/**
 * DBへの1リクエストごとに入れる遅延(ms)。localhost では往復がほぼ0で、
 * 「リクエスト数×RTT」という本番の効き方が見えないため、実測RTTを注入して測る。
 * 既定は0。`GET /delay?ms=20` で変える。
 */
let delayMs = 0;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function freshStats() {
  return { requests: 0, bytes: 0, ranges: [], byFile: {} };
}

function record(name, start, length) {
  stats.requests += 1;
  stats.bytes += length;
  // ページ位置の散らばりを見たいので先頭2000件だけ残す
  if (stats.ranges.length < 2000) stats.ranges.push([start, length]);
  const f = (stats.byFile[name] ||= { requests: 0, bytes: 0 });
  f.requests += 1;
  f.bytes += length;
}

/** `bytes=100-199` を [100, 199] にする。単一レンジのみ対応（httpvfs はそれしか使わない）。 */
function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header || "");
  if (!m) return null;
  const [, rawStart, rawEnd] = m;
  if (rawStart === "" && rawEnd === "") return null;
  if (rawStart === "") {
    const length = Math.min(Number(rawEnd), size);
    return [size - length, size - 1];
  }
  const start = Number(rawStart);
  const end = rawEnd === "" ? size - 1 : Math.min(Number(rawEnd), size - 1);
  return start > end || start >= size ? null : [start, end];
}

async function serveFile(req, res, filePath, { countIt = false } = {}) {
  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("not found");
    return;
  }

  const type = MIME[path.extname(filePath)] || "application/octet-stream";
  const base = {
    "content-type": type,
    "accept-ranges": "bytes",
    // 検証のたびに条件を揃えたいのでブラウザキャッシュは切る
    "cache-control": "no-store",
  };

  if (req.method === "HEAD") {
    res.writeHead(200, { ...base, "content-length": stat.size });
    res.end();
    return;
  }

  const range = parseRange(req.headers.range, stat.size);
  if (!range) {
    if (countIt) record(path.basename(filePath), 0, stat.size);
    res.writeHead(200, { ...base, "content-length": stat.size });
    createReadStream(filePath).pipe(res);
    return;
  }

  const [start, end] = range;
  const length = end - start + 1;
  if (countIt) {
    record(path.basename(filePath), start, length);
    if (delayMs) await sleep(delayMs);
  }
  res.writeHead(206, {
    ...base,
    "content-range": `bytes ${start}-${end}/${stat.size}`,
    "content-length": length,
  });
  createReadStream(filePath, { start, end }).pipe(res);
}

/** data/dist 配下の .db を1階層まで拾う（page_size 比較で exp-* に分けて置くため）。 */
async function listDatabases() {
  const found = [];
  for (const entry of await fs.readdir(DB_DIR, { withFileTypes: true })) {
    if (entry.isFile() && entry.name.endsWith(".db")) {
      found.push(entry.name);
    } else if (entry.isDirectory()) {
      for (const name of await fs.readdir(path.join(DB_DIR, entry.name))) {
        if (name.endsWith(".db")) found.push(`${entry.name}/${name}`);
      }
    }
  }
  return Promise.all(found.map(async (rel) => ({
    url: `/db/${rel}`,
    size: (await fs.stat(path.join(DB_DIR, rel))).size,
  })));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (url.pathname === "/stats") {
    const body = JSON.stringify(stats);
    if (url.searchParams.has("reset")) stats = freshStats();
    res.writeHead(200, { "content-type": MIME[".json"], "cache-control": "no-store" });
    res.end(body);
    return;
  }

  if (url.pathname === "/delay") {
    if (url.searchParams.has("ms")) delayMs = Number(url.searchParams.get("ms")) || 0;
    res.writeHead(200, { "content-type": MIME[".json"], "cache-control": "no-store" });
    res.end(JSON.stringify({ delayMs }));
    return;
  }

  if (url.pathname === "/dbs") {
    res.writeHead(200, { "content-type": MIME[".json"], "cache-control": "no-store" });
    res.end(JSON.stringify(await listDatabases()));
    return;
  }

  if (url.pathname.startsWith("/db/")) {
    const rel = path.normalize(url.pathname.slice("/db/".length));
    const resolved = path.join(DB_DIR, rel);
    if (!resolved.startsWith(DB_DIR)) {
      res.writeHead(403).end("forbidden");
      return;
    }
    await serveFile(req, res, resolved, { countIt: true });
    return;
  }

  const rel = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  const resolved = path.join(PUBLIC_DIR, path.normalize(rel));
  if (!resolved.startsWith(PUBLIC_DIR)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  await serveFile(req, res, resolved);
});

server.listen(PORT, "127.0.0.1", async () => {
  let names = [];
  try {
    names = (await fs.readdir(DB_DIR)).filter((n) => n.endsWith(".db"));
  } catch {
    /* まだ作っていない */
  }
  console.log(`http://127.0.0.1:${PORT}/`);
  console.log(`DB: ${DB_DIR} → ${names.join(", ") || "(なし)"}`);
  if (!names.length) {
    console.log("先に `python scripts/build_db.py --split-by-year --year 2025` を実行すること");
  }
});
