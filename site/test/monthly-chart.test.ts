/**
 * 月ごとの棒グラフ（`monthlyChart`）の回帰テスト。
 *
 * ★ このグラフは **`mode` で言っていることが変わる**。件数と率は同じデータでも
 *   結論が逆になることがあるので（`chart.ts` の実測メモ）、
 *   「どちらの数字で棒を立てているか」をここで固める。
 *
 * 検索結果ページ（既定＝件数）と、争点語・頻出語のページ（率）が同じ関数を使う。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { monthlyChart, toSeries, type SeriesPoint } from "../src/lib/chart.ts";

/** 実測の「逆になる」例（2025年上半期・`安全保障`）を縮めたもの。
 *  1月は発言数が少なく、件数では最小・率では最大になる。 */
const MONTHS = ["2025-01", "2025-04"];
const HITS = [68, 706];
const TOTALS = [908, 19739];

/** 棒（`class="bar…"` の矩形）を左から順に。 */
function bars(svg: string) {
  return [...svg.matchAll(/<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" class="([^"]*)"/g)]
    .map((m) => ({ x: +m[1], y: +m[2], w: +m[3], h: +m[4], cls: m[5] }));
}

describe("monthlyChart", () => {
  const points = toSeries(MONTHS, HITS, TOTALS);

  it("空の系列では何も出さない", () => {
    assert.equal(monthlyChart([], "x"), "");
  });

  it("既定は率（争点語・頻出語のページを変えない）", () => {
    const [jan, apr] = bars(monthlyChart(points, "x"));
    // 1月 74.9 / 4月 35.8。**率では1月のほうが高い**
    assert.ok(jan.h > apr.h, "率のグラフで1月が4月より低い");
  });

  it("件数モードでは件数で棒が立つ（率とは高さの大小が逆になる）", () => {
    const [jan, apr] = bars(monthlyChart(points, "x", "count"));
    assert.ok(apr.h > jan.h, "件数のグラフで4月が1月より低い");
  });

  it("件数モードでは薄い棒を作らない（割っていないので跳ねようがない）", () => {
    // 発言数500件未満の月は、率のグラフでは薄くする（率が振れやすいところ）
    const points = toSeries(["2025-01", "2025-04"], [20, 706], [300, 19739]);
    assert.ok(bars(monthlyChart(points, "x")).some((b) => b.cls.includes("faint")),
              "率のグラフで分母の小さい月が薄くなっていない");
    assert.ok(!bars(monthlyChart(points, "x", "count")).some((b) => b.cls.includes("faint")),
              "件数のグラフに faint が残っている");
  });

  it("どちらのモードでも件数と分母と率をツールチップに出す", () => {
    for (const mode of ["rate", "count"] as const) {
      const svg = monthlyChart(points, "x", mode);
      assert.match(svg, /2025-01: 68件 \/ 908発言（1,000発言あたり 74\.9件）/, mode);
    }
  });

  it("縦軸の目盛りは件数なら整数（0.5件は無い）", () => {
    const ticks = (svg: string) =>
      [...svg.matchAll(/class="tick right">([\d.]+)</g)].map((m) => m[1]);
    assert.ok(ticks(monthlyChart(points, "x", "count")).every((t) => !t.includes(".")),
              "件数の目盛りに小数が出ている");
  });

  it("件数の目盛りは整数の位置に打つ（線と数字がずれない）", () => {
    // 最大1件の月しか無い語。`max / 2` の位置に「1」を出すと上の「1」と2つ並ぶ
    const few = toSeries(["2025-01", "2025-04"], [1, 0], [908, 19739]);
    const ticks = [...monthlyChart(few, "x", "count")
      .matchAll(/y="([\d.]+)" class="tick right">([\d.]+)</g)]
      .map((m) => ({ y: +m[1], label: m[2] }));
    assert.deepEqual(ticks.map((t) => t.label), ["0", "1"], "目盛りの数字が重複している");
    // 同じ数字が別の高さに出ていないこと
    assert.equal(new Set(ticks.map((t) => t.y)).size, ticks.length);
  });

  it("縦軸の説明はモードごとに変える（件数は開催日数に引きずられると書く）", () => {
    assert.match(monthlyChart(points, "x", "count"), /国会が長く開かれた月ほど大きく出ます/);
    assert.match(monthlyChart(points, "x"), /1,000発言あたりの出現件数/);
  });
});

describe("toSeries", () => {
  it("分母0の月は落とす（国会が開いていない月を軸に並べない）", () => {
    const got = toSeries(["2025-01", "2025-08"], [5, 0], [908, 0]);
    assert.deepEqual(got.map((p: SeriesPoint) => p.month), ["2025-01"]);
  });

  it("keepHitsWithoutTotal なら、分母が無くても当たった月は残す", () => {
    // 期間DBのほうが分母（topics.json）より新しいとき。**当たっているのに消さない**
    const got = toSeries(["2026-08"], [3], [0], { keepHitsWithoutTotal: true });
    assert.deepEqual(got.map((p) => [p.month, p.hits, p.rate]), [["2026-08", 3, 0]]);
  });

  it("keepHitsWithoutTotal でも、当たっていない月は落とす", () => {
    assert.equal(toSeries(["2026-08"], [0], [0], { keepHitsWithoutTotal: true }).length, 0);
  });
});
