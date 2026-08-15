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
   * アクセス解析を入れているか。**入れたら必ず true にする**
   * （プライバシーポリシーの記述が変わる）。site/README.md「運営者名と連絡先」。
   */
  analytics: false | { name: string; url: string; cookies: boolean };
}

export const OPERATOR: Operator = {
  name: "国会タイムライン 運営",
  // Cloudflare Email Routing で Gmail へ転送している（2026-08-06）。
  // 返信は Gmail 側のエイリアスからこのアドレスで送れる。
  // **常時稼働プロセスは増えていない** — 転送は Cloudflare 側の設定だけで動く
  email: "info@kokkai-timeline.com",
  formUrl: null,
  analytics: false,
};

/** 公開に必要な最低限のうち、まだ埋まっていないもの。空なら公開してよい。 */
export const missing: string[] = [
  OPERATOR.name ? null : "運営者の表示名",
  OPERATOR.email ? null : "連絡先メール",
].filter((v): v is string => v !== null);

/** 公開に必要な最低限（表示名と連絡先）が埋まっているか。 */
export const operatorReady = missing.length === 0;

/** メールアドレスをそのまま置くと収集されるので、表示は分割する。 */
export function mailParts(email: string): { user: string; domain: string } {
  const at = email.lastIndexOf("@");
  return { user: email.slice(0, at), domain: email.slice(at + 1) };
}
