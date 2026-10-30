/**
 * 本文パネルの「パネル内を検索」の照合。
 *
 * 位置は**元の文字列の位置**としてそのまま `Range` に使う。畳んだ結果の長さが
 * 変わると、光る場所が黙ってずれる。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { findMatches, findTerms, foldForFind } from "../src/lib/find.ts";

describe("foldForFind", () => {
  it("全角英数と大文字を畳み、長さを変えない", () => {
    const text = "ＳＤＧｓと　ＬＧＢＴ法案（２０２３年）";
    const folded = foldForFind(text);
    assert.equal(folded, "sdgsと lgbt法案(2023年)");
    assert.equal(folded.length, text.length);
  });
});

describe("findTerms", () => {
  it("半角・全角の空白で区切り、重複を落とす", () => {
    assert.deepEqual(findTerms(" 防衛　予算 防衛 "), ["防衛", "予算"]);
    assert.deepEqual(findTerms("   "), []);
  });
});

describe("findMatches", () => {
  it("半角で打っても全角の本文に当たる", () => {
    assert.deepEqual(findMatches("ＬＧＢＴ理解増進法", findTerms("lgbt")), [[0, 4]]);
  });

  it("前から重ならないように拾う", () => {
    assert.deepEqual(findMatches("ああああ", ["ああ"]), [[0, 2], [2, 4]]);
  });

  it("複数の語は位置順に並べ、同じ位置なら長いほうを採る", () => {
    const text = "防衛予算と防衛費";
    assert.deepEqual(findMatches(text, ["防衛", "防衛費", "予算"]),
      [[0, 2], [2, 4], [5, 8]]);
  });

  it("語が無ければ何も返さない", () => {
    assert.deepEqual(findMatches("本文", []), []);
    assert.deepEqual(findMatches("本文", ["無い"]), []);
  });
});
