/**
 * `data/dist` を Range 対応で配る。R2 の代わり。
 *
 * 本番の期間DBは R2 に置く（半期ごと12ファイル・最大377MB・合計2.42GB）。`site/public/` に
 * 入れると `astro build` が dist へ丸ごとコピーしてしまうので、開発サーバの
 * ミドルウェアとして直接配る。プロセスを増やさずに済む。
 *
 *   /db/kokkai-2025H1.db → ../data/dist/kokkai-2025H1.db（Range 対応）
 *   /db/manifest.json    → 期間DBの目録（build_db.py が出力）
 *
 * あわせて `/speech/<speech_id>` を `/speech` に書き換える。発言は 650,785 件あって
 * 事前生成できないため、本番では `public/_redirects` の 200 rewrite が同じ役割をする。
 *
 * 使い方は2通り:
 *   - `npm run dev`     → Astro に相乗りする（同一オリジンの /db）
 *   - `npm run dbserve` → 単体で起動する。`astro preview` でビルド成果物を
 *                         **本番と同じ別オリジン構成**で試すときに使う
 */

import { createReadStream, promises as fs } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA_DIR = path.resolve(HERE, "..", "..", "data", "dist");

const MIME = {
  ".db": "application/octet-stream",
  ".json": "application/json; charset=utf-8",
};

/** `bytes=100-199` を [100, 199] にする。sql.js-httpvfs は単一レンジしか使わない。 */
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

async function serve(req, res, filePath) {
  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("not found");
    return;
  }

  const headers = {
    "content-type": MIME[path.extname(filePath)] || "application/octet-stream",
    "accept-ranges": "bytes",
    // 本番（R2 の別オリジン）と同じ条件で動かすため開発でも付けておく。
    // ★ accept-ranges を公開一覧から落とさないこと。別オリジンだと JS から
    //   読めなくなり、sql.js-httpvfs が「バイト単位で取れない」と判断して
    //   **DB全体を1チャンクとして読みに行く**（`file is not a database` で落ちる）。
    //   同一オリジンでは露見しないので、R2 側の設定と揃えておく
    "access-control-allow-origin": "*",
    "access-control-expose-headers": "content-range, content-length, accept-ranges, etag",
    "cache-control": "no-store",
  };

  if (req.method === "HEAD") {
    res.writeHead(200, { ...headers, "content-length": stat.size });
    res.end();
    return;
  }

  const range = parseRange(req.headers.range, stat.size);
  if (!range) {
    res.writeHead(200, { ...headers, "content-length": stat.size });
    createReadStream(filePath).pipe(res);
    return;
  }

  const [start, end] = range;
  record(path.basename(filePath), end - start + 1);
  res.writeHead(206, {
    ...headers,
    "content-range": `bytes ${start}-${end}/${stat.size}`,
    "content-length": end - start + 1,
  });
  // 壊れたブラウザキャッシュの再現。`POISON=1` で、`retry=` の付かないURLには
  // 中身の代わりにゼロを返す（＝`file is not a database` になる）。
  // db.ts の**やり直しが URL を変えてキャッシュを外せているか**を確かめるためだけの
  // 仕掛け。この経路は壊れたときしか動かないので、無いと検証できない。
  //   POISON=1 node scripts/dev-data-server.js
  if (process.env.POISON && !req.url.includes("retry=")) {
    res.end(Buffer.alloc(end - start + 1));
    return;
  }
  createReadStream(filePath, { start, end }).pipe(res);
}

/**
 * Range リクエストの計測。**この設計の費用はリクエスト数で決まる**ので
 * （待ち時間 ≒ リクエスト数 × RTT・`docs/DECISIONS.md`）、分割の単位や索引を変えたら
 * ここで数える。**ワーカ内の fetch はブラウザの計測APIにも DevTools にも出てこない**ので、
 * 配る側で数えるしかない。
 *
 *   検索したあと GET /db/__trace → {total, files: {ファイル: {n, bytes}}}
 *   **読むと0に戻る**ので、測りたい操作の直前に1回叩いてから測る。
 */
const trace = new Map();

function record(name, bytes) {
  const seen = trace.get(name) ?? { n: 0, bytes: 0 };
  trace.set(name, { n: seen.n + 1, bytes: seen.bytes + bytes });
}

/** `/db/<name>` を data/dist から返す。範囲外のパスは弾く。true なら処理済み。 */
function handleDb(req, res) {
  const url = new URL(req.url, "http://localhost");
  if (!url.pathname.startsWith("/db/")) return false;

  if (url.pathname === "/db/__trace") {
    const rows = Object.fromEntries([...trace].sort());
    const total = [...trace.values()].reduce(
      (a, v) => ({ n: a.n + v.n, bytes: a.bytes + v.bytes }), { n: 0, bytes: 0 });
    trace.clear();
    res.writeHead(200, { "content-type": MIME[".json"] });
    res.end(JSON.stringify({ total, files: rows }, null, 1));
    return true;
  }

  const resolved = path.join(DATA_DIR, path.normalize(url.pathname.slice("/db/".length)));
  if (!resolved.startsWith(DATA_DIR)) {
    res.writeHead(403).end("forbidden");
    return true;
  }
  serve(req, res, resolved).catch(() => res.writeHead(500).end("error"));
  return true;
}

/** @returns {import("astro").AstroIntegration} */
export function devDataServer() {
  return {
    name: "kokkai:dev-data-server",
    hooks: {
      "astro:server:setup": ({ server, logger }) => {
        logger.info(`DB: ${DATA_DIR} を /db/ で配信`);
        server.middlewares.use((req, res, next) => {
          if (handleDb(req, res)) return;

          // 本番の `_redirects` と同じ書き換え。/speech/<id> を1枚の /speech で受ける
          const url = new URL(req.url, "http://localhost");
          if (url.pathname.startsWith("/speech/")) req.url = "/speech" + url.search;
          next();
        });
      },
    },
  };
}

/** 単体起動。`npm run dbserve` から。 */
export function startStandalone(port = Number(process.env.DB_PORT || 8788)) {
  const server = http.createServer((req, res) => {
    if (handleDb(req, res)) return;
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
  });
  server.listen(port, "127.0.0.1", () => {
    console.log(`DB: ${DATA_DIR}`);
    console.log(`    http://127.0.0.1:${port}/db/manifest.json`);
    console.log(`ビルドして試すには:`);
    console.log(`    PUBLIC_DB_BASE=http://127.0.0.1:${port}/db npm run build && npm run preview`);
  });
  return server;
}

// `node scripts/dev-data-server.js` で直接叩かれたときだけ立てる
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  startStandalone();
}
