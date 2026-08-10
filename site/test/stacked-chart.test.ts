/**
 * 積み上げ棒グラフ（`stackedMonthlyChart`）の回帰テスト。
 *
 * 議員ページの発言数の推移がこれ。**誤りが目に見えにくい**種類の描画なので、
 * 積み上げの順序・高さの基準・色の対応をここで固める。
 *
 * ★ 色は呼ぶ側が系列と一緒に渡す。**絞り込みで系列が減っても生き残りが
 *   塗り替わらないこと**を、ここで担保する。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { stackedMonthlyChart, type StackedSeries } from "../src/lib/chart.ts";

const MONTHS = ["2021-01", "2021-02", "2022-01"];

function s(label: string, values: number[], color: string): StackedSeries {
  return { label, values, color };
}

/** `<rect …>` を上から順に拾う（y 座標つき）。 */
function rects(svg: string) {
  return [...svg.matchAll(/<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)" fill="([^"]+)"/g)]
    .map((m) => ({
      x: Number(m[1]), y: Number(m[2]), w: Number(m[3]), h: Number(m[4]), fill: m[5],
    }));
}

describe("stackedMonthlyChart", () => {
  it("月も系列も無ければ何も出さない", () => {
    assert.equal(stackedMonthlyChart([], [s("a", [], "red")], "x"), "");
    assert.equal(stackedMonthlyChart(MONTHS, [], "x"), "");
  });

  it("0の月は矩形を作らない（高さ0の矩形を並べない）", () => {
    const svg = stackedMonthlyChart(MONTHS, [s("委員会A", [5, 0, 0], "var(--cat-1)")], "x");
    assert.equal(rects(svg).length, 1);
  });

  it("同じ月の系列は積み上がる（下から順、重ならない）", () => {
    const svg = stackedMonthlyChart(["2021-01"], [
      s("A", [10], "var(--cat-1)"),
      s("B", [10], "var(--cat-2)"),
    ], "x");
    const [first, second] = rects(svg);
    // 1つ目が下、2つ目がその上。y は小さいほど上
    assert.ok(second.y < first.y, "2つ目が上に積まれていない");
    assert.ok(second.y + second.h <= first.y + 0.01, "積み上げが重なっている");
    assert.equal(first.x, second.x, "同じ月なのに横位置が違う");
  });

  it("高さの基準は「積み上げの合計」の最大値", () => {
    // 2021-01 は合計20、2022-01 は合計5。20 が満杯になる
    const svg = stackedMonthlyChart(MONTHS, [
      s("A", [10, 0, 5], "var(--cat-1)"),
      s("B", [10, 0, 0], "var(--cat-2)"),
    ], "x");
    const all = rects(svg);
    const jan21 = all.filter((r) => Math.abs(r.x - all[0].x) < 0.01);
    const totalH = jan21.reduce((sum, r) => sum + r.h + 2, 0);   // 隙間を戻す
    const last = all[all.length - 1];
    // 合計5の月は、合計20の月のおよそ1/4の高さ
    assert.ok(Math.abs((last.h + 2) / totalH - 0.25) < 0.05,
      `比率が合わない: ${(last.h + 2) / totalH}`);
  });

  it("渡された色をそのまま使う（順番で振り直さない）", () => {
    const svg = stackedMonthlyChart(["2021-01"], [
      s("A", [1], "var(--cat-3)"),
      s("B", [1], "var(--cat-7)"),
    ], "x");
    assert.deepEqual(rects(svg).map((r) => r.fill), ["var(--cat-3)", "var(--cat-7)"]);
  });

  it("★系列が減っても生き残りの色は変わらない（絞り込みで塗り替えない）", () => {
    const before = stackedMonthlyChart(["2021-01"], [
      s("A", [1], "var(--cat-1)"),
      s("B", [1], "var(--cat-2)"),
      s("C", [1], "var(--cat-3)"),
    ], "x");
    // B が0件になって落ちた状態
    const after = stackedMonthlyChart(["2021-01"], [
      s("A", [1], "var(--cat-1)"),
      s("C", [1], "var(--cat-3)"),
    ], "x");
    assert.deepEqual(rects(after).map((r) => r.fill), ["var(--cat-1)", "var(--cat-3)"]);
    assert.ok(before.includes("var(--cat-3)") && after.includes("var(--cat-3)"));
  });

  it("凡例を必ず出す（色だけで区別させない）", () => {
    const svg = stackedMonthlyChart(["2021-01"], [
      s("予算委員会", [1], "var(--cat-1)"),
      s("その他", [1], "var(--cat-other)"),
    ], "x");
    assert.match(svg, /chart-legend/);
    assert.ok(svg.includes("予算委員会") && svg.includes("その他"));
  });

  it("値が足りない月は0として扱う（落ちない）", () => {
    const svg = stackedMonthlyChart(MONTHS, [s("A", [3], "var(--cat-1)")], "x");
    assert.equal(rects(svg).length, 1);
  });

  it("委員会名と見出しを HTML エスケープする", () => {
    const svg = stackedMonthlyChart(["2021-01"],
      [s("<script>", [1], "var(--cat-1)")], `"><b>`);
    assert.equal(svg.includes("<script>"), false);
    assert.equal(svg.includes("<b>"), false);
  });

  it("積み上げの区切りに隙間を空ける（境目が溶けない）", () => {
    const svg = stackedMonthlyChart(["2021-01"], [
      s("A", [100], "var(--cat-1)"),
      s("B", [100], "var(--cat-2)"),
    ], "x");
    const [first, second] = rects(svg);
    assert.ok(first.y - (second.y + second.h) >= 1.5, "隙間が空いていない");
  });
});
