// @ts-check
import { defineConfig } from "astro/config";
import { devDataServer } from "./scripts/dev-data-server.js";

/**
 * 完全静的サイト（`output: "static"`）。サーバは持たない — ROADMAP の絶対制約。
 *
 * 発言の中身は sql.js-httpvfs がブラウザから HTTP Range で年DBを引く。
 * DBの置き場所は `PUBLIC_DB_BASE` で切り替える:
 *
 *   開発  : 未設定 → `/db`。`scripts/dev-data-server.js` が data/dist を Range 付きで配る
 *   本番  : R2 のカスタムドメイン（例 https://db.example.org）
 *
 * 本番では別オリジンになるので R2 側に CORS（`Access-Control-Allow-Origin` と
 * `Access-Control-Expose-Headers: content-range`）が要る。ROADMAP §3.4 で設定する。
 */
export default defineConfig({
  output: "static",
  trailingSlash: "ignore",
  integrations: [devDataServer()],
  build: {
    // 発言ページは 650,785 件あって事前生成できない（§3.3 参照）。
    // ディレクトリ形式にすると `/speech/xxx/index.html` が要るため、ファイル形式にする
    format: "file",
  },
});

// 補足: sql.js-httpvfs の dist は webpack の UMD なので、Vite の依存最適化から
// 除外してはいけない（素の ESM として読むと `exports` が無くて落ちる）。
// ワーカと wasm のほうはバンドルを通さず public/vendor に置く（scripts/vendor.js）。
