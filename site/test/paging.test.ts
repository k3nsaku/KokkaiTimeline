/**
 * 期間をまたぐページ送り（`mergePages`）の回帰テスト。
 *
 * ここが壊れると**画面の「新しい順」が黙って崩れる**。実際に一度壊れていて、
 * 6年ぶんを選ぶと1ページ目が120件返り、「いちばん新しい期間の21件目」を飛ばして
 * 次の期間へ進んでいた（docs/PITFALLS.md）。
 *
 * 配信DBは半期で割ってある（`2026H1` = 1〜6月）。期間DBの中身を模した corpus を
 * 用意して、UIと同じようにページを送りながら「画面に並んだ順」が
 * 全体の日付降順と1件ずつ一致するかを見る。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { mergePages, type SpeechRow } from "../src/lib/query.ts";

/** 期間IDは**辞書順が時系列順**（2021H1 < 2021H2 < 2022H1）。並べ替えはこれに乗る。 */
const newestFirst = (a: string, b: string) => b.localeCompare(a);

/** すべての期間ID。テストで使う並びの正解でもある。 */
const ALL = ["2021H1", "2021H2", "2022H1", "2022H2", "2023H1", "2023H2",
             "2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"];

/** rowid は日付の昇順に振ってある（build_db.py の load()）ので、
 *  期間が新しいほど、同じ期間なら rowid が大きいほど新しい。 */
function row(period: string, rowid: number): SpeechRow {
  const year = Number(period.slice(0, 4));
  const month = period.endsWith("H1") ? "01" : "07";
  return {
    speech_id: `${period}_${rowid}`, date: `${year}-${month}-01`, speaker: "誰か",
    speaker_group: null, speaker_position: null, politician_id: 1,
    issue_id: period, speech_order: rowid, meeting: "内閣委員会", house: "衆議院",
    snippet: "", marked: false, rowid, year,
  };
}

/**
 * 期間DBの代わり。期間ごとに rowid の降順で持っておき、
 * `WHERE rowid < cursor ORDER BY rowid DESC LIMIT n` と同じ形で返す。
 */
class Corpus {
  // Node の型ストリップは parameter property を消せないので普通のフィールドにする
  hits: Map<string, number[]>;

  constructor(hits: Map<string, number[]>) {
    this.hits = hits;
  }

  get periods(): string[] {
    return [...this.hits.keys()].sort(newestFirst);
  }

  /** 全体の「新しい順」＝ 期間の降順 → rowid の降順。これが正解の並び。 */
  get expected(): string[] {
    return this.periods.flatMap((p) =>
      [...(this.hits.get(p) ?? [])].sort((a, b) => b - a).map((r) => `${p}_${r}`));
  }

  fetch(period: string, cursor: number | undefined, limit: number): SpeechRow[] {
    return [...(this.hits.get(period) ?? [])]
      .sort((a, b) => b - a)
      .filter((r) => cursor == null || r < cursor)
      .slice(0, limit)
      .map((r) => row(period, r));
  }
}

/** UIと同じようにページを送る。`search()` の呼び出し側の再現。 */
function walk(corpus: Corpus, limit: number, maxPages = 400) {
  const periods = corpus.periods;
  const pages: SpeechRow[][] = [];
  let before: Record<string, number> = {};
  let done = false;

  for (let i = 0; i < maxPages && !done; i++) {
    const perPeriod = periods.map((p) =>
      // 読み切った期間には問い合わせない（search() の先頭にある早期リターン）
      p in before && before[p] === 0 ? [] : corpus.fetch(p, before[p], limit));
    const page = mergePages(periods, perPeriod, limit, before);
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

/** 各期間に n 件ずつヒットする corpus。 */
function evenly(periods: string[], n: number): Corpus {
  return new Corpus(new Map(
    periods.map((p) => [p, Array.from({ length: n }, (_, i) => i + 1)])));
}

describe("mergePages", () => {
  it("期間を何個選んでも1ページは limit 件（連結して 20→240 にならない）", () => {
    // 12期間それぞれに 50件ヒットする語。壊れていたときは1ページ目が全期間ぶん返った
    const pages = walk(evenly(ALL, 50), 20);
    for (const [i, page] of pages.entries()) {
      if (i < pages.length - 1) assert.equal(page.length, 20, `${i + 1}ページ目`);
    }
    assert.equal(pages[0].every((r) => r.speech_id.startsWith("2026H2")), true,
                 "1ページ目は全部いちばん新しい期間から出る");
  });

  it("画面に並んだ順が全体の日付降順と1件ずつ一致する（重複も飛ばしも無い）", () => {
    const corpus = evenly(ALL, 50);
    const shown = walk(corpus, 20).flat().map((r) => r.speech_id);
    assert.deepEqual(shown, corpus.expected);
  });

  it("同じ年の H2 → H1 の順に並ぶ（年の中でも新しい順）", () => {
    const corpus = new Corpus(new Map([
      ["2025H1", [2, 1]],
      ["2025H2", [4, 3]],
    ]));
    assert.deepEqual(walk(corpus, 20).flat().map((r) => r.speech_id),
                     ["2025H2_4", "2025H2_3", "2025H1_2", "2025H1_1"]);
  });

  it("期間の変わり目を1ページの中でまたげる", () => {
    // 2026H2 は19件しか無いので、1ページ目は 2026H2×19 + 2026H1×1 になるはず
    const corpus = new Corpus(new Map([
      ["2026H2", Array.from({ length: 19 }, (_, i) => i + 1)],
      ["2026H1", Array.from({ length: 40 }, (_, i) => i + 1)],
    ]));

    const pages = walk(corpus, 20);
    assert.equal(pages[0].length, 20);
    assert.deepEqual(pages[0].map((r) => r.speech_id.slice(0, 6)),
                     [...Array(19).fill("2026H2"), "2026H1"]);
    assert.deepEqual(pages.flat().map((r) => r.speech_id), corpus.expected);
  });

  it("1件も出さなかった期間のカーソルは進めない", () => {
    // 2026H2 だけで枠が埋まるので、2026H1 は引いたのに1件も出せない。
    // ここでカーソルを進めると、その20件が二度と出てこない
    const periods = ["2026H2", "2026H1"];
    const perPeriod = [
      Array.from({ length: 20 }, (_, i) => row("2026H2", 100 - i)),
      Array.from({ length: 20 }, (_, i) => row("2026H1", 50 - i)),
    ];
    const page = mergePages(periods, perPeriod, 20, {});

    assert.equal(page.rows.length, 20);
    assert.equal(page.before["2026H2"], 81, "出し切った期間は最後の rowid まで進む");
    assert.equal("2026H1" in page.before, false,
                 "出さなかった期間は前回のまま（頭から引き直す）");
    assert.equal(page.done, false);
  });

  it("ヒットが limit に満たない期間は読み切りとして 0 を立てる", () => {
    const page = mergePages(["2026H2", "2026H1"], [
      [row("2026H2", 10), row("2026H2", 9)],
      [row("2026H1", 5)],
    ], 20, {});

    assert.deepEqual(page.before, { "2026H2": 0, "2026H1": 0 });
    assert.equal(page.done, true);
    assert.equal(page.rows.length, 3);
  });

  it("前ページで読み切った期間（カーソル 0）は結果が空でも読み切りのまま", () => {
    const page = mergePages(["2026H2", "2026H1"], [[], [row("2026H1", 5)]], 20,
                            { "2026H2": 0 });

    assert.equal(page.before["2026H2"], 0);
    assert.deepEqual(page.rows.map((r) => r.speech_id), ["2026H1_5"]);
  });

  it("ヒットが1件も無ければ done", () => {
    const page = mergePages(["2026H2", "2026H1"], [[], []], 20, {});
    assert.deepEqual(page.before, { "2026H2": 0, "2026H1": 0 });
    assert.equal(page.done, true);
    assert.equal(page.rows.length, 0);
  });

  it("ちょうど limit 件で終わる期間があっても取りこぼさない", () => {
    // 2026H2 がちょうど20件。次ページで0件と分かって初めて読み切りになる
    const corpus = new Corpus(new Map([
      ["2026H2", Array.from({ length: 20 }, (_, i) => i + 1)],
      ["2026H1", Array.from({ length: 5 }, (_, i) => i + 1)],
    ]));

    assert.deepEqual(walk(corpus, 20).flat().map((r) => r.speech_id), corpus.expected);
  });

  it("期間ごとのヒット数が偏っていても全体の順序を保つ", () => {
    // 半期分割では H1（常会）に大きく偏る。実測で H1 309〜356MB / H2 6〜110MB
    const corpus = new Corpus(new Map([
      ["2026H2", [3, 1]],
      ["2026H1", []],
      ["2025H2", Array.from({ length: 77 }, (_, i) => i + 1)],
      ["2025H1", [9]],
      ["2024H2", Array.from({ length: 41 }, (_, i) => i + 1)],
      ["2024H1", []],
    ]));

    const shown = walk(corpus, 7).flat().map((r) => r.speech_id);
    assert.deepEqual(shown, corpus.expected);
    assert.equal(new Set(shown).size, shown.length, "重複が出た");
  });

  it("limit が 1 でも成立する", () => {
    const corpus = new Corpus(new Map([["2026H1", [2, 1]], ["2025H2", [5]]]));
    const pages = walk(corpus, 1);
    assert.deepEqual(pages.flat().map((r) => r.speech_id), corpus.expected);
  });
});
