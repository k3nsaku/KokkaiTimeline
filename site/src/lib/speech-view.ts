/**
 * 発言の全文表示で、前後の発言を**本文の上と下に**並べるための組み立て。
 *
 * `/speech`（1枚のページ）と本文パネル（`speech-panel.ts`）の両方が使う。
 * **同じものを2か所に書かない** — 片方だけ直すと表示が食い違う。
 *
 * ★ 会議録は「読む」ものなので、前の発言は上・後の発言は下に置く。
 *   下にまとめて並べると、前の発言を読むのに一度下まで行って戻ることになる。
 *
 * ★ 前の発言を**上に**差し込むぶん、本文が下へ押し出される。**差し込んだあとに
 *   本文の先頭へ位置を合わせ直すこと**（呼ぶ側の責任）。合わせないと、
 *   開いた瞬間に見えているのが「前の発言」になる。
 *
 * `db.ts` に依存しないこと — 純粋関数にしてあるので `site/test/` から直接呼べる。
 */

// ★ 拡張子を付ける。テストは `.ts` を Node がそのまま読む（型を消すだけの変換）ので、
//   拡張子なしの相対 import は ERR_MODULE_NOT_FOUND になる。
//   **`site/test/` から辿るモジュールでは省略しないこと**（site/README.md）
import { escapeHtml } from "./format.ts";

export interface ContextRow {
  speech_id: string;
  speech_order: number;
  speaker: string;
  speaker_kind: string;
  politician_id: number | null;
  head: string;
}

export interface SplitContext {
  before: ContextRow[];
  after: ContextRow[];
}

/**
 * 前後に振り分ける。**対象の発言そのものは、どちらにも入れない**
 * （本文を出すのだから、抜粋で二度出す意味がない）。
 *
 * 振り分けは `speech_order` で見る。`speech_id` の一致で切ると、
 * 同じ会議に同じIDが無いことに寄りかかることになる。
 */
export function splitContext(rows: ContextRow[], current: number): SplitContext {
  const sorted = [...rows].sort((a, b) => a.speech_order - b.speech_order);
  return {
    before: sorted.filter((r) => r.speech_order < current),
    after: sorted.filter((r) => r.speech_order > current),
  };
}

export interface ContextListOptions {
  /** 見出し。前は「この前の発言」、後は「この後の発言」 */
  label: string;
  /** 本文の上に置くほうは、見出しを控えめにする */
  compact?: boolean;
}

/**
 * 抜粋の並び。**本文と同じ見た目にしない** —— どちらが全文かが分からなくなる。
 */
export function renderContextList(
  rows: ContextRow[], options: ContextListOptions,
): string {
  if (!rows.length) return "";
  const items = rows.map((r) => `
    <li class="speech">
      <div class="speech-meta">
        <span class="speech-speaker">${
          r.politician_id
            ? `<a href="/politician/${r.politician_id}">${escapeHtml(r.speaker)}</a>`
            : escapeHtml(r.speaker)}</span>
        <span>${escapeHtml(r.speaker_kind)}</span>
      </div>
      <p class="speech-body"><a
        href="/speech/${encodeURIComponent(r.speech_id)}"
        data-speech-id="${escapeHtml(r.speech_id)}">${escapeHtml(r.head)}…</a></p>
    </li>`).join("");

  return `
    <section class="speech-context${options.compact ? " compact" : ""}">
      <h2 class="context-label">${escapeHtml(options.label)}</h2>
      <ul class="speech-list">${items}</ul>
    </section>`;
}
