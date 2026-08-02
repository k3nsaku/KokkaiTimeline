/**
 * 表示まわりの小物。
 *
 * **`db.ts` を import しないこと。** このモジュールは .astro のフロントマター
 * （＝ビルド時の Node 側）からも読む。db.ts は sql.js-httpvfs に依存していて
 * ブラウザでしか動かないので、間に import が1本でも通ると SSR が落ちる。
 */

/**
 * snippet() のマーカー。**`<mark>` を直接 SQL に入れない。**
 * 発言本文は会議録そのままで `<` や `&` が入りうる。SQLite が返した文字列を
 * そのまま innerHTML に入れると壊れる（し、危ない）。本文に出てこない制御文字を
 * 使っておき、HTML エスケープしてから `<mark>` に置き換える。
 * SQL 側は `char(1)` / `char(2)` で同じものを指している（db.ts）。
 */
export const MARK_OPEN = String.fromCharCode(1);
export const MARK_CLOSE = String.fromCharCode(2);

const ESCAPES: Record<string, string> = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
};

export function escapeHtml(text: string): string {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/**
 * snippet() が返した文字列を安全に `<mark>` 付きの HTML にする。
 * **必ずエスケープしてからマーカーを置き換える。** 逆にすると本文中の `<` で壊れる。
 */
export function highlight(snippet: string): string {
  return escapeHtml(snippet)
    .split(MARK_OPEN).join("<mark>")
    .split(MARK_CLOSE).join("</mark>");
}

/** マーカーを持たない抜粋（争点語検索・議員ページ）に、あとから語を強調する。 */
export function highlightTerm(text: string, term: string): string {
  const escaped = escapeHtml(text);
  if (!term) return escaped;
  const needle = escapeHtml(term);
  return escaped.split(needle).join(`<mark>${needle}</mark>`);
}

const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];

/** `2025-03-28` → `2025年3月28日（金）` */
export function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  const weekday = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${y}年${m}月${d}日（${weekday}）`;
}

export function formatNumber(n: number): string {
  return n.toLocaleString("ja-JP");
}

/** 会議録の本文は改行が段落の代わりなので、段落に起こす。 */
export function paragraphs(body: string): string[] {
  return body.split(/\n+/).map((p) => p.trim()).filter(Boolean);
}

/** クエリ文字列を作る。空の値は落とす。 */
export function toQuery(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== "") q.set(key, String(value));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}
