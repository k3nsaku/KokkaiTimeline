/**
 * 本文パネルの「パネル内を検索」の照合（`speech-panel.ts`）。
 *
 * DOM に依存しない純粋関数にしてあるので `site/test/` から直接呼べる。
 *
 * ★ **全角・半角と大文字小文字を区別しない。** 会議録の本文は英数字が全部全角だが、
 *   パネルの見出しや日付（`第213回国会`）はこちらで組んだ半角。どちらで打っても当てる。
 *
 * ★ **畳み方は1文字→1文字に限る。** 当たった位置をそのまま元の文字列の位置として
 *   `Range` に使うため、長さが変わる畳み方（NFKC など）は使えない。
 */

/** 全角英数記号・全角空白を半角へ、英字を小文字へ。**長さは変えない** */
export function foldForFind(text: string): string {
  let out = "";
  for (let i = 0; i < text.length; i++) {
    let c = text.charCodeAt(i);
    if (c >= 0xff01 && c <= 0xff5e) c -= 0xfee0;
    else if (c === 0x3000) c = 0x20;
    if (c >= 0x41 && c <= 0x5a) c += 0x20;
    out += String.fromCharCode(c);
  }
  return out;
}

/** 空白（半角・全角）で区切った語。サイトの検索欄と同じ区切り方 */
export function findTerms(input: string): string[] {
  const terms = foldForFind(input).trim().split(/\s+/).filter(Boolean);
  return [...new Set(terms)];
}

/**
 * `text` の中で語が当たる位置 `[開始, 終了)` を、前から重ならないように返す。
 * 同じ位置で複数の語が当たるときは長いほうを採る。
 */
export function findMatches(text: string, terms: string[]): Array<[number, number]> {
  const folded = foldForFind(text);
  const needles = terms.map(foldForFind).filter(Boolean)
    .sort((a, b) => b.length - a.length);
  if (!needles.length) return [];

  const out: Array<[number, number]> = [];
  let pos = 0;
  while (pos < folded.length) {
    let best = -1;
    let bestLen = 0;
    for (const n of needles) {
      const at = folded.indexOf(n, pos);
      if (at < 0) continue;
      if (best < 0 || at < best || (at === best && n.length > bestLen)) {
        best = at;
        bestLen = n.length;
      }
    }
    if (best < 0) break;
    out.push([best, best + bestLen]);
    pos = best + bestLen;
  }
  return out;
}
