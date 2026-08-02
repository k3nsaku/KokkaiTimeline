/**
 * 年をまたぐページ送り（`mergePages`）の回帰テスト。
 *
 * ここが壊れると**画面の「新しい順」が黙って崩れる**。実際に一度壊れていて、
 * 6年ぶんを選ぶと1ページ目が120件返り、「2026年の21件目」を飛ばして
 * 2025年へ進んでいた（docs/ROADMAP.md §4-8 の指摘2）。
 *
 * 年DBの中身を模した corpus を用意して、UIと同じようにページを送りながら
 * 「画面に並んだ順」が全体の日付降順と1件ずつ一致するかを見る。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { mergePages, type SpeechRow } from "../src/lib/query.ts";

/** rowid は日付の昇順に振ってある（build_db.py の load()）ので、
 *  年が新しいほど、同じ年なら rowid が大きいほど新しい。 */
function row(year: number, rowid: number): SpeechRow {
  return {
    speech_id: `${year}_${rowid}`, date: `${year}-01-01`, speaker: "誰か",
    speaker_group: null, speaker_position: null, politician_id: 1,
    issue_id: `${year}`, speech_order: rowid, meeting: "内閣委員会", house: "衆議院",
    snippet: "", marked: false, rowid, year,
  };
}

/**
 * 年DBの代わり。年ごとに rowid の降順で持っておき、
 * `WHERE rowid < cursor ORDER BY rowid DESC LIMIT n` と同じ形で返す。
 */
class Corpus {
  // Node の型ストリップは parameter property を消せないので普通のフィールドにする
  hits: Map<number, number[]>;

  constructor(hits: Map<number, number[]>) {
    this.hits = hits;
  }

  get years(): number[] {
    return [...this.hits.keys()].sort((a, b) => b - a);
  }

  /** 全体の「新しい順」＝ 年の降順 → rowid の降順。これが正解の並び。 */
  get expected(): string[] {
    return this.years.flatMap((y) =>
      [...(this.hits.get(y) ?? [])].sort((a, b) => b - a).map((r) => `${y}_${r}`));
  }

  fetch(year: number, cursor: number | undefined, limit: number): SpeechRow[] {
    return [...(this.hits.get(year) ?? [])]
      .sort((a, b) => b - a)
      .filter((r) => cursor == null || r < cursor)
      .slice(0, limit)
      .map((r) => row(year, r));
  }
}

/** UIと同じようにページを送る。`search()` の呼び出し側の再現。 */
function walk(corpus: Corpus, limit: number, maxPages = 200) {
  const years = corpus.years;
  const pages: SpeechRow[][] = [];
  let before: Record<number, number> = {};
  let done = false;

  for (let i = 0; i < maxPages && !done; i++) {
    const perYear = years.map((y) =>
      // 読み切った年には問い合わせない（search() の先頭にある早期リターン）
      y in before && before[y] === 0 ? [] : corpus.fetch(y, before[y], limit));
    const page = mergePages(years, perYear, limit, before);
    if (!page.rows.length && !page.done) {
      assert.fail("進んでいないのに done にならない（無限ループ）");
    }
    pages.push(page.rows);
    before = page.before;
    done = page.done;
  }

  assert.ok(done, "打ち止めにならなかった");
  return pages;
}

describe("mergePages", () => {
  it("年を何個選んでも1ページは limit 件（連結して 20→120 にならない）", () => {
    // 6年それぞれに 50件ヒットする語。壊れていたときは1ページ目が 120件だった
    const corpus = new Corpus(new Map(
      [2021, 2022, 2023, 2024, 2025, 2026].map((y) =>
        [y, Array.from({ length: 50 }, (_, i) => i + 1)])));

    const pages = walk(corpus, 20);
    for (const [i, page] of pages.entries()) {
      if (i < pages.length - 1) assert.equal(page.length, 20, `${i + 1}ページ目`);
    }
    assert.equal(pages[0].map((r) => r.year).every((y) => y === 2026), true,
                 "1ページ目は全部いちばん新しい年から出る");
  });

  it("画面に並んだ順が全体の日付降順と1件ずつ一致する（重複も飛ばしも無い）", () => {
    const corpus = new Corpus(new Map(
      [2021, 2022, 2023, 2024, 2025, 2026].map((y) =>
        [y, Array.from({ length: 50 }, (_, i) => i + 1)])));

    const shown = walk(corpus, 20).flat().map((r) => r.speech_id);
    assert.deepEqual(shown, corpus.expected);
  });

  it("年の変わり目を1ページの中でまたげる", () => {
    // 2026年は19件しか無いので、1ページ目は 2026×19 + 2025×1 になるはず
    const corpus = new Corpus(new Map([
      [2026, Array.from({ length: 19 }, (_, i) => i + 1)],
      [2025, Array.from({ length: 40 }, (_, i) => i + 1)],
    ]));

    const pages = walk(corpus, 20);
    assert.equal(pages[0].length, 20);
    assert.deepEqual(pages[0].map((r) => r.year),
                     [...Array(19).fill(2026), 2025]);
    assert.deepEqual(pages.flat().map((r) => r.speech_id), corpus.expected);
  });

  it("1件も出さなかった年のカーソルは進めない", () => {
    // 2026年だけで枠が埋まるので、2025年は引いたのに1件も出せない。
    // ここでカーソルを進めると、その20件が二度と出てこない
    const years = [2026, 2025];
    const perYear = [
      Array.from({ length: 20 }, (_, i) => row(2026, 100 - i)),
      Array.from({ length: 20 }, (_, i) => row(2025, 50 - i)),
    ];
    const page = mergePages(years, perYear, 20, {});

    assert.equal(page.rows.length, 20);
    assert.equal(page.before[2026], 81, "出し切った年は最後の rowid まで進む");
    assert.equal(2025 in page.before, false, "出さなかった年は前回のまま（頭から引き直す）");
    assert.equal(page.done, false);
  });

  it("ヒットが limit に満たない年は読み切りとして 0 を立てる", () => {
    const page = mergePages([2026, 2025], [
      [row(2026, 10), row(2026, 9)],
      [row(2025, 5)],
    ], 20, {});

    assert.deepEqual(page.before, { 2026: 0, 2025: 0 });
    assert.equal(page.done, true);
    assert.equal(page.rows.length, 3);
  });

  it("前ページで読み切った年（カーソル 0）は結果が空でも読み切りのまま", () => {
    const page = mergePages([2026, 2025], [[], [row(2025, 5)]], 20, { 2026: 0 });

    assert.equal(page.before[2026], 0);
    assert.deepEqual(page.rows.map((r) => r.year), [2025]);
  });

  it("ヒットが1件も無ければ done", () => {
    const page = mergePages([2026, 2025], [[], []], 20, {});
    assert.deepEqual(page.before, { 2026: 0, 2025: 0 });
    assert.equal(page.done, true);
    assert.equal(page.rows.length, 0);
  });

  it("ちょうど limit 件で終わる年があっても取りこぼさない", () => {
    // 2026年がちょうど20件。次ページで0件と分かって初めて読み切りになる
    const corpus = new Corpus(new Map([
      [2026, Array.from({ length: 20 }, (_, i) => i + 1)],
      [2025, Array.from({ length: 5 }, (_, i) => i + 1)],
    ]));

    assert.deepEqual(walk(corpus, 20).flat().map((r) => r.speech_id), corpus.expected);
  });

  it("年ごとのヒット数が偏っていても全体の順序を保つ", () => {
    const corpus = new Corpus(new Map([
      [2026, [3, 1]],
      [2025, []],
      [2024, Array.from({ length: 77 }, (_, i) => i + 1)],
      [2023, [9]],
      [2022, Array.from({ length: 41 }, (_, i) => i + 1)],
      [2021, []],
    ]));

    const shown = walk(corpus, 7).flat().map((r) => r.speech_id);
    assert.deepEqual(shown, corpus.expected);
    assert.equal(new Set(shown).size, shown.length, "重複が出た");
  });

  it("limit が 1 でも成立する", () => {
    const corpus = new Corpus(new Map([[2026, [2, 1]], [2025, [5]]]));
    const pages = walk(corpus, 1);
    assert.deepEqual(pages.flat().map((r) => r.speech_id), corpus.expected);
  });
});
