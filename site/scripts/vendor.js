/**
 * sql.js-httpvfs のワーカと wasm を `public/vendor/` へコピーする。
 *
 * この2つは Vite のバンドルを通してはいけない。ワーカは自分の中で wasm の URL を
 * 解決するので、**絶対パスで渡せる場所に素のまま置く**必要がある
 * （相対パスにすると `/vendor/vendor/...` を見に行って CompileError になる。
 *  `docs/PHASE1_PROTOTYPE.md` §7）。
 *
 * `npm run dev` / `npm run build` の前に自動で走る（package.json の predev / prebuild）。
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FROM = path.resolve(HERE, "..", "node_modules", "sql.js-httpvfs", "dist");
const TO = path.resolve(HERE, "..", "public", "vendor");

const FILES = ["sqlite.worker.js", "sql-wasm.wasm"];

await fs.mkdir(TO, { recursive: true });
for (const name of FILES) {
  await fs.copyFile(path.join(FROM, name), path.join(TO, name));
}
console.log(`vendor: ${FILES.join(", ")} → public/vendor/`);
