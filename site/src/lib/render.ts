/** 発言リストの描画。検索結果・議員ページ・争点語ページ・頻出語ページで共通に使う。
 *
 * 抜粋は `<a href="/speech/<id>">` のまま出す。**リンクであることを壊さない。**
 * 広い画面では `speech-panel.ts` がクリックを横取りして本文パネルを開くが、
 * 狭い画面・JS無効・Ctrl+クリック・新しいタブでは**そのまま遷移する**
 * （`data-speech-id` は横取り側が拾うための目印で、無くても遷移は成立する）。
 */

import type { SpeechRow } from "./db";
import { escapeHtml, formatDate, highlight, highlightTerm } from "./format";

export interface RenderOptions {
  /** マーカーの無い抜粋（争点語・2文字語・議員ページ）で強調したい語 */
  terms?: string[];
  /** 年ごとの見出しを入れるか。議員ページや年またぎ検索で効く */
  groupByYear?: boolean;
  /** 発言者名を出すか。議員ページでは自明なので省く */
  showSpeaker?: boolean;
}

function speechCard(row: SpeechRow, opts: RenderOptions): string {
  const body = row.marked
    ? highlight(row.snippet ?? "")
    : highlightTerm(row.snippet ?? "", opts.terms ?? []);

  const speaker = opts.showSpeaker === false ? "" : row.politician_id
    ? `<a class="speech-speaker" href="/politician/${row.politician_id}">${escapeHtml(row.speaker)}</a>`
    : `<span class="speech-speaker">${escapeHtml(row.speaker)}</span>`;

  const position = row.speaker_position
    ? `<span>${escapeHtml(row.speaker_position)}</span>` : "";
  const group = row.speaker_group ? `<span>${escapeHtml(row.speaker_group)}</span>` : "";

  return `
    <li class="speech">
      <div class="speech-meta">
        ${speaker}${position}${group}
        <span>${escapeHtml(formatDate(row.date))}</span>
        <span>${escapeHtml(row.house)}・${escapeHtml(row.meeting)}</span>
      </div>
      <p class="speech-body"><a
        href="/speech/${encodeURIComponent(row.speech_id)}"
        data-speech-id="${escapeHtml(row.speech_id)}">${body}</a></p>
    </li>`;
}

/** 行の配列を HTML にする。年で切り替わるところに見出しを挟む。 */
export function renderSpeeches(rows: SpeechRow[], opts: RenderOptions = {}): string {
  if (!rows.length) return "";
  if (!opts.groupByYear) {
    return `<ul class="speech-list">${rows.map((r) => speechCard(r, opts)).join("")}</ul>`;
  }

  const chunks: string[] = [];
  let current: number | null = null;
  let open = false;
  for (const row of rows) {
    if (row.year !== current) {
      if (open) chunks.push("</ul>");
      chunks.push(`<h2 class="year-heading">${row.year}年</h2><ul class="speech-list">`);
      current = row.year;
      open = true;
    }
    chunks.push(speechCard(row, opts));
  }
  if (open) chunks.push("</ul>");
  return chunks.join("");
}
