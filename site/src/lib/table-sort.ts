/**
 * 表の並べ替え。**DOM に依存しない**（`query.ts` と同じ方針で、`site/test/` から直接呼ぶ）。
 *
 * ★ **ビルド時の既定順とブラウザ側の並べ替えを同じ関数から出す。**
 *   別々に書くと「氏名を2回押したら最初と違う並びになる」が静かに起きる
 *   （`/politicians` の既定は `compareKeys(よみ, "text", "asc")` そのもの）。
 *
 * ★ 五十音順を自前で作らない。`Intl.Collator("ja")` を通す。
 *   コードポイント順だと `が` が `か` の隣に来ず、カタカナ表記の氏名も別の塊になる。
 */

/** 列の値の種類。日付は ISO（`2026-08-17`）であることに寄りかかっている */
export type SortType = "text" | "num" | "date";
export type SortDir = "asc" | "desc";

const collator = new Intl.Collator("ja");

/** 並べる値が無い、と見なすもの。数の列は数に読めないものも含む */
function isBlank(value: string, type: SortType): boolean {
  return value === "" || (type === "num" && Number.isNaN(Number(value)));
}

/**
 * 1列ぶんの比較。
 *
 * **空は方向によらず末尾に置く。** 降順で先頭に空欄が並ぶと、何も分からない行だけが
 * 最初に見えることになる（会派は空がありうる）。
 */
export function compareKeys(a: string, b: string, type: SortType, dir: SortDir): number {
  const blankA = isBlank(a, type);
  const blankB = isBlank(b, type);
  if (blankA || blankB) return blankA && blankB ? 0 : blankA ? 1 : -1;

  const sign = dir === "asc" ? 1 : -1;
  if (type === "num") return sign * (Number(a) - Number(b));
  // 日付は桁が揃っているので素の文字列比較でよい（照合に掛ける意味が無い）
  if (type === "date") return sign * (a < b ? -1 : a > b ? 1 : 0);
  return sign * collator.compare(a, b);
}

/**
 * 見出しを**最初に**押したときの向き。**数と日付は降順から。**
 * 「発言数」を押して1件の人が、「最後の発言」を押して5年前が並ぶのでは押した意味が無い。
 * もう一度押せば昇順になる。
 */
export function firstDir(type: SortType): SortDir {
  return type === "text" ? "asc" : "desc";
}

/** 同じ見出しをもう一度押したときの向き。 */
export function toggleDir(dir: SortDir): SortDir {
  return dir === "asc" ? "desc" : "asc";
}
