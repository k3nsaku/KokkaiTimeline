/**
 * 表の並べ替え（`compareKeys` / `firstDir`）の回帰テスト。
 *
 * ここが崩れると `/politicians` の**既定の並び（氏名のよみの五十音順）**が
 * 静かに変わる。ビルド時の並びと、見出しを押したときの並びは同じ関数から出るので、
 * 五十音順の性質はここで固める。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { compareKeys, firstDir, toggleDir, type SortType } from "../src/lib/table-sort.ts";

const sorted = (values: string[], type: SortType = "text", dir: "asc" | "desc" = "asc") =>
  [...values].sort((a, b) => compareKeys(a, b, type, dir));

describe("compareKeys（text＝五十音順）", () => {
  it("かなを五十音順に並べる", () => {
    assert.deepEqual(
      sorted(["こいずみ", "あべ", "きしだ", "すが"]),
      ["あべ", "きしだ", "こいずみ", "すが"],
    );
  });

  it("清音・濁音・半濁音が同じ行にまとまる（コードポイント順では離れる）", () => {
    // は → ば → ぱ。素の `<` でも同順になるが、`か` と `が` は離れるので照合が要る
    assert.deepEqual(sorted(["ぱぱ", "はは", "ばば"]), ["はは", "ばば", "ぱぱ"]);
    assert.deepEqual(sorted(["がいむ", "かいご", "きしだ"]), ["かいご", "がいむ", "きしだ"]);
  });

  it("カタカナ表記の氏名も同じ読みの位置に入る", () => {
    // 「アントニオ猪木」のようにカタカナのよみが来ても、あ行の位置に並ぶこと
    assert.deepEqual(
      sorted(["こいずみ", "アントニオいのき", "きしだ"]),
      ["アントニオいのき", "きしだ", "こいずみ"],
    );
  });

  it("よみが無く氏名（漢字）で代用した行は、かなの後ろに寄る", () => {
    // 五十音の中に漢字が割り込まないことだけを見る（並べる値そのものは代用品）
    assert.deepEqual(sorted(["山田太郎", "あべ", "わたなべ"]), ["あべ", "わたなべ", "山田太郎"]);
  });

  it("降順は昇順の逆", () => {
    const values = ["こいずみ", "あべ", "きしだ", "すが"];
    assert.deepEqual(sorted(values, "text", "desc"), [...sorted(values)].reverse());
  });
});

describe("compareKeys（空の扱い）", () => {
  it("空は方向によらず末尾（会派は空がありうる）", () => {
    assert.deepEqual(sorted(["立憲民主党", "", "自由民主党"]), ["自由民主党", "立憲民主党", ""]);
    assert.deepEqual(
      sorted(["立憲民主党", "", "自由民主党"], "text", "desc"),
      ["立憲民主党", "自由民主党", ""],
    );
  });

  it("数として読めない値も末尾（降順で先頭に来ない）", () => {
    assert.deepEqual(sorted(["12", "", "5"], "num", "desc"), ["12", "5", ""]);
  });

  it("空どうしは同着（安定ソートで元の並びが残る）", () => {
    assert.equal(compareKeys("", "", "text", "asc"), 0);
    assert.equal(compareKeys("", "", "num", "desc"), 0);
  });
});

describe("compareKeys（num・date）", () => {
  it("数は数として比べる（文字列比較では 1000 < 900 になる）", () => {
    assert.deepEqual(sorted(["1000", "900", "80"], "num", "asc"), ["80", "900", "1000"]);
    assert.deepEqual(sorted(["1000", "900", "80"], "num", "desc"), ["1000", "900", "80"]);
  });

  it("日付の降順が新しい順", () => {
    assert.deepEqual(
      sorted(["2021-03-02", "2026-08-01", "2021-12-31"], "date", "desc"),
      ["2026-08-01", "2021-12-31", "2021-03-02"],
    );
  });
});

describe("firstDir / toggleDir（押したときの向き）", () => {
  it("文字は昇順から、数と日付は降順から", () => {
    assert.equal(firstDir("text"), "asc");
    assert.equal(firstDir("num"), "desc");
    assert.equal(firstDir("date"), "desc");
  });

  it("もう一度押すと逆になる", () => {
    assert.equal(toggleDir("asc"), "desc");
    assert.equal(toggleDir(toggleDir("asc")), "asc");
  });
});
