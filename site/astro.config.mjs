// @ts-check
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
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
  // 正規URL・OGP・サイトマップの絶対URLに使う。www は付けない（apex に寄せる）
  site: "https://kokkai-timeline.com",
  trailingSlash: "ignore",
  integrations: [
    devDataServer(),
    // 事前生成した議員1,111ページ・争点語79ページを見つけてもらうため。
    // 検索結果と発言ページは中身がクライアント側にしか無いので載せない
    // （noindex を付けてあるページと一致させる）
    sitemap({
      filter: (page) => !/\/(search|speech)(\.html)?$/.test(page),
    }),
  ],
  build: {
    // 発言ページは 650,785 件あって事前生成できない。
    // ディレクトリ形式にすると `/speech/xxx/index.html` が要るため、ファイル形式にする
    format: "file",
    // ★小さい <script> をHTMLに埋め込ませない。**CSP のため。**
    //   既定だと Mail.astro のような短いコンポーネントスクリプトが
    //   `<script type="module">`（src 無し）として直に書き出され、
    //   `script-src 'self'` に弾かれて**エラーも出さずに機能だけ消える**
    //   （実際にメールのコピーボタンが消えた）。外部ファイルなら 'self' で通る。
    //   これを戻すなら public/_headers の CSP も一緒に見直すこと
    inlineStylesheets: "never",
  },
  vite: {
    // 同上。0 にすると小さいアセットも data: URI にせず外部ファイルにする
    build: { assetsInlineLimit: 0 },
  },
});

// 補足: sql.js-httpvfs の dist は webpack の UMD なので、Vite の依存最適化から
// 除外してはいけない（素の ESM として読むと `exports` が無くて落ちる）。
// ワーカと wasm のほうはバンドルを通さず public/vendor に置く（scripts/vendor.js）。
