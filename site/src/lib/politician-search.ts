/**
 * 議員名の検索。**DBを引かない。**
 *
 * ★ 議員を探すのに発言DBを使わない理由:
 *   姓は2文字が多く（「岸田」「林」「高市」）、2文字語は `word_hit` の語彙に
 *   入っていなければ引けないうえ、入っていても**その語を含む発言**が返るだけで
 *   「その議員」は返らない。議員マスタは1,111人ぶんが手元にあるので、
 *   照合はページの中で完結させる（`/politicians` の絞り込みと同じ考え）。
 *
 * ★ 素の `includes` にしない。`/politicians` の絞り込みはそれで、
 *   **カタカナで打つと当たらず、「岸田 文雄」と空白を入れても当たらない**
 *   （よみは全員ひらがな・氏名に空白は無いというデータの形に依存している）。
 *
 * `db.ts` に依存しないこと — 純粋関数にしてあるので `site/test/` から直接呼べる。
 */

export interface PoliticianLite {
  id: number;
  name: string;
  /** よみ（ひらがな）。全員ぶんある */
  kana: string;
  kaiha: string;
  house: string;
  n: number;
}

/**
 * 一致の強さ。小さいほど上に出す。
 *
 * ★ **enum にしない。** テストは `.ts` を Node がそのまま読む（型を消すだけの変換）ので、
 *   enum は実行時に消えて落ちる（site/README.md）。
 */
export const RANK = {
  nameExact: 0,
  namePrefix: 1,
  namePart: 2,
  kanaPrefix: 3,
  kanaPart: 4,
  kaiha: 5,
} as const;

export type Rank = (typeof RANK)[keyof typeof RANK];

const KATAKANA_START = 0x30a1;   // ァ
const KATAKANA_END = 0x30f6;     // ヶ

/**
 * 照合用に畳む。**問い合わせ側と対象側の両方に同じものを掛ける。**
 *
 * - 空白を落とす（「岸田 文雄」「岸田　文雄」で引けるように）
 * - カタカナ → ひらがな（よみはひらがなで持っているが、打つ側は両方ありうる）
 *
 * 長音符（ー）と中黒（・）には触らない。カタカナ表記の氏名があっても、
 * **両側を同じ形に畳むので釣り合いは崩れない。**
 */
export function fold(input: string): string {
  let out = "";
  for (const ch of input.replace(/[\s　]+/g, "")) {
    const code = ch.codePointAt(0)!;
    out += (code >= KATAKANA_START && code <= KATAKANA_END)
      ? String.fromCodePoint(code - 0x60)
      : ch;
  }
  return out;
}

function rankOf(query: string, p: PoliticianLite): Rank | null {
  const name = fold(p.name);
  if (name === query) return RANK.nameExact;
  if (name.startsWith(query)) return RANK.namePrefix;
  if (name.includes(query)) return RANK.namePart;

  const kana = fold(p.kana);
  if (kana.startsWith(query)) return RANK.kanaPrefix;
  if (kana.includes(query)) return RANK.kanaPart;

  if (p.kaiha && fold(p.kaiha).includes(query)) return RANK.kaiha;
  return null;
}

export interface MatchOptions {
  limit?: number;
  /** 会派でも当てるか。既定は当てる（`/politicians` の絞り込みと揃える） */
  includeKaiha?: boolean;
}

/**
 * 問い合わせに合う議員を、一致の強い順に返す。
 *
 * **氏名の一致をよみより先に出す。** 「はやし」で引いたときに
 * 氏名が「林」の人より先に「早矢仕」が出ると、探している人に辿り着けない。
 * 同じ強さなら発言数の多い順（`/politicians` の既定の並びと揃える）。
 */
export function matchPoliticians(
  input: string, list: PoliticianLite[], options: MatchOptions = {},
): PoliticianLite[] {
  const { limit = 60, includeKaiha = true } = options;
  const query = fold(input.trim());
  // 1文字でも引けてよい（「林」「泉」は1文字の姓）。空だけ弾く
  if (!query) return [];

  const hits: { p: PoliticianLite; rank: Rank }[] = [];
  for (const p of list) {
    const rank = rankOf(query, p);
    if (rank === null) continue;
    if (rank === RANK.kaiha && !includeKaiha) continue;
    hits.push({ p, rank });
  }

  hits.sort((a, b) =>
    a.rank - b.rank || b.p.n - a.p.n || a.p.name.localeCompare(b.p.name, "ja"));
  return hits.slice(0, limit).map((h) => h.p);
}
