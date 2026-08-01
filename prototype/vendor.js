/**
 * sql.js-httpvfs の配布物を public/vendor/ にコピーする。
 *
 * ワーカと wasm は同一オリジンから読ませる必要があるため、node_modules を直接
 * 参照させずにコピーする。バンドラを入れないのは検証を最小構成に保つため。
 */

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FROM = path.join(HERE, "node_modules", "sql.js-httpvfs", "dist");
const TO = path.join(HERE, "public", "vendor");
const FILES = ["index.js", "sqlite.worker.js", "sql-wasm.wasm"];

await fs.mkdir(TO, { recursive: true });
for (const name of FILES) {
  await fs.copyFile(path.join(FROM, name), path.join(TO, name));
  const { size } = await fs.stat(path.join(TO, name));
  console.log(`${name}  ${(size / 1024).toFixed(1)} KB`);
}
