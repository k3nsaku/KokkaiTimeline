/**
 * 運営者の表示と連絡先（docs/SCOPE.md「法務の最小セット」）。
 *
 * **ここが未記入のまま公開しない。** 免責事項・プライバシーポリシー・訂正依頼の
 * 各ページはこの1か所を見ていて、埋まっていなければ「未記入」の警告を出す。
 * 埋めるのは運営者本人にしかできないので、値だけをここに集めてある。
 *
 * ビルド時にしか読まない（`.astro` のフロントマターから import する）。
 * `db.ts` に依存しないこと — ブラウザ専用のコードを引き込むと SSR が落ちる。
 */

export interface Operator {
  /**
   * 表示名。**ハンドルネームでよい。**
   * 特定商取引法の表示義務が生じる取引（販売・広告収入を伴う勧誘）をしていないので、
   * 本名・住所・電話番号は要らない（docs/SCOPE.md）。
   */
  name: string | null;
  /**
   * 連絡先メールアドレス。訂正依頼・削除依頼・問い合わせの窓口。
   * **公開する以上、ここは必ず生きているものにする。** 受け取れない窓口は
   * 「窓口が無い」のと同じで、名誉毀損の抗弁としても弱くなる。
   */
  email: string | null;
  /**
   * 訂正依頼フォームのURL（Google フォーム等）。
   * **無くてもよい。** その場合はメールだけで受ける。
   * 入れるときは、そのフォームの提供者にもデータが渡ることをプライバシーポリシーに書くこと
   * （`privacy.astro` が自動で1行足す）。
   */
  formUrl: string | null;
  /**
   * アクセス解析。**ここが唯一の入り口。** 計測タグ（`Base.astro`）と
   * プライバシーポリシーの記述（`privacy.astro`）が**同じ値から出る**ので、
   * 「計測しているのにポリシーに書いていない」状態を作れない。
   *
   * ★ **ダッシュボード側の自動挿入を使わないのはこのため。** Cloudflare Pages の
   *   Web Analytics を画面から有効にすると、リポジトリを1行も変えずに計測が始まり、
   *   公開中のプライバシーポリシーが黙って嘘になる。**必ずここを埋めて配ること。**
   *
   * `beaconToken` は Cloudflare Web Analytics のサイトトークン。
   * **秘密ではない**（全ページのHTMLに出る公開値）。空のままにすると
   * `missing` に出て、`/privacy` などに⚠が出る。
   */
  analytics:
    | false
    | { name: string; url: string; cookies: boolean; beaconToken: string };
}

/**
 * **アクセス解析のタグを出さないページ。**
 *
 * ★ 検索語をフラグメント（`#q=…`）に移したので、**HTTP 要求には載らない**。
 *   だが `location.hash` は**そのページで動くスクリプトからは読める**。
 *   計測タグは Cloudflare が配る外部JSで、手動埋め込みでは**版の固定も SRI も
 *   できない**（Cloudflare 自身がそう書いている）。いま配信中のビーコンが
 *   パスしか送らないとしても、**「読める場所に外部コードを置かない」**ほうが、
 *   よそのスクリプトの中身に依存しなくて済む。
 *
 * ★ **`operator.ts` に置いてあるのが肝。** ページ側に直接書くと、
 *   「計測していないのにポリシーには載っている」が黙って生まれる。
 *   `Base.astro`（タグを出すか）と `privacy.astro`（何と書くか）が
 *   **同じ配列を読む**ので、片方だけ変わることがない。
 *
 * ここを縮めるときは、そのページのURLに何が載るかを先に確かめること。
 */
export const ANALYTICS_EXCLUDED_PATHS = ["/search"];

export const OPERATOR: Operator = {
  name: "国会タイムライン 運営",
  // Cloudflare Email Routing で Gmail へ転送している（2026-08-06）。
  // 返信は Gmail 側のエイリアスからこのアドレスで送れる。
  // **常時稼働プロセスは増えていない** — 転送は Cloudflare 側の設定だけで動く
  email: "info@kokkai-timeline.com",
  formUrl: null,
  // ★ダッシュボードの RUM は「JS スニペットのインストールで有効にする」にしてある
  //   （2026-08-16）。**「有効にする」に戻さないこと** — エッジで勝手に挿されて、
  //   ここを false にしても計測が止まらなくなる（経緯は docs/PITFALLS.md）。
  //   やめるときは、ここを false にするのと同時にダッシュボードも「無効にする」へ。
  analytics: {
    name: "Cloudflare Web Analytics",
    url: "https://www.cloudflare.com/web-analytics/",
    cookies: false,
    // 秘密ではない（全ページのHTMLに出る公開値）
    beaconToken: "f121ac20f1a845b0805f68b563bb6af3",
  },
};

/** 公開に必要な最低限のうち、まだ埋まっていないもの。空なら公開してよい。 */
export const missing: string[] = [
  OPERATOR.name ? null : "運営者の表示名",
  OPERATOR.email ? null : "連絡先メール",
  // ★ 解析を入れる宣言だけしてトークンが空、は**計測されないのにポリシーには
  //   「使っています」と出る**状態。片方だけ埋まらないように、ここで拾う
  OPERATOR.analytics && !OPERATOR.analytics.beaconToken
    ? "アクセス解析の計測トークン"
    : null,
].filter((v): v is string => v !== null);

/** 公開に必要な最低限（表示名と連絡先）が埋まっているか。 */
export const operatorReady = missing.length === 0;

/** メールアドレスをそのまま置くと収集されるので、表示は分割する。 */
export function mailParts(email: string): { user: string; domain: string } {
  const at = email.lastIndexOf("@");
  return { user: email.slice(0, at), domain: email.slice(at + 1) };
}
