/**
 * 会期での絞り込み。設計は site/README.md「/frequent の会期絞り込み」。
 *
 * **DBを引かない。** `data/dist/frequent.json` に会期ごとの件数が入っているので、
 * 並べ替えはページの中で完結する。
 *
 * ★ **ブラウザから `word_hit` / FTS で任意期間を集計しない。**
 *   年330万行の範囲集計は全走査になり、`ORDER BY date DESC` と同じ穴に落ちる
 *   （docs/PITFALLS.md）。期間を選ばせる機能はここで閉じる。
 *
 * ★ **会期を月の合算で作らない。** 会期は月境界で始まらない（第204回は
 *   2021-01-18 開会で、同じ月に第203回が同居する）。件数は
 *   `scripts/build_frequent.py` が会期ごとに直接数えたものを使う。
 *
 * `db.ts` に依存しないこと — 純粋関数にしてあるので `site/test/` から直接呼べる。
 */

export interface SessionMeta {
  session: number;
  from: string;
  until: string;
  /** その会期の議員発言数。**割るのはこれ** */
  n_speeches: number;
}

export interface SessionWord {
  term: string;
  /** 全期間の発言数。lift の基準になる */
  n: number;
  topicId: number | null;
  /** `sessions` と同じ並びの、会期ごとの発言数 */
  hits: number[];
}

export interface RankedWord {
  term: string;
  hits: number;
  /** その会期での出現率 ÷ 全期間の出現率。1.0 なら「いつもどおり」 */
  lift: number;
  topicId: number | null;
}

export interface RankOptions {
  /**
   * この件数に満たない語は出さない。**下限を置かないと率が跳ねるだけの語が並ぶ**
   * （3件が9件になっただけで3倍になる）。
   */
  minHits?: number;
  limit?: number;
}

/**
 * ある会期で「いつもより多く出た語」を並べる。
 *
 * **件数順にしない。** 件数で並べると `日本` `国民` `経済` のような
 * どの会期でも多い語が上に来て、その会期の特徴が出ない
 * （`build_frequent.py` の冒頭にある、頻度順が使えないのと同じ理由）。
 */
export function rankBySession(
  words: SessionWord[],
  sessions: SessionMeta[],
  index: number,
  totalSpeeches: number,
  options: RankOptions = {},
): RankedWord[] {
  const { minHits = 30, limit = 120 } = options;
  const meta = sessions[index];
  // 会期が無い・その会期に発言が無い・全期間の分母が無い、のどれでも計算にならない。
  // 0件の一覧を返すほうが、NaN や Infinity で並べ替えるより始末がよい
  if (!meta || meta.n_speeches <= 0 || totalSpeeches <= 0) return [];

  const ranked: RankedWord[] = [];
  for (const word of words) {
    const hits = word.hits[index] ?? 0;
    if (hits < minHits || word.n <= 0) continue;
    const inSession = hits / meta.n_speeches;
    const overall = word.n / totalSpeeches;
    ranked.push({ term: word.term, hits, lift: inSession / overall, topicId: word.topicId });
  }

  // lift が同じなら件数の多いほうを先に。並びを実行ごとに変えないため
  // （同点で順序が揺れると、同じページが再ビルドのたびに違って見える）
  ranked.sort((a, b) => b.lift - a.lift || b.hits - a.hits || a.term.localeCompare(b.term));
  return ranked.slice(0, limit);
}

/** 「2024年1月26日〜8月23日」のような表示。同じ年なら年を繰り返さない。 */
export function formatSpan(from: string, until: string): string {
  const part = (iso: string, withYear: boolean) => {
    const [y, m, d] = iso.split("-").map(Number);
    return `${withYear ? `${y}年` : ""}${m}月${d}日`;
  };
  return `${part(from, true)}〜${part(until, from.slice(0, 4) !== until.slice(0, 4))}`;
}
