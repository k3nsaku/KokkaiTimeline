/**
 * 議員名の検索（`matchPoliticians`）の回帰テスト。
 *
 * ここが弱いと**探している議員に辿り着けない**。姓は2文字が多く、
 * 同じ姓の議員も同姓の別人もいるので、並び順まで含めて固める。
 *
 * データの形（よみは全員ひらがな・氏名に空白は無い）に寄りかかった実装なので、
 * 打つ側がカタカナや空白を混ぜてきたときに崩れないことを確かめる。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fold, matchPoliticians, type PoliticianLite } from "../src/lib/politician-search.ts";

function p(id: number, name: string, kana: string, kaiha = "", n = 100): PoliticianLite {
  return { id, name, kana, kaiha, house: "衆議院", n };
}

const LIST: PoliticianLite[] = [
  p(1, "岸田文雄", "きしだふみお", "自由民主党・無所属の会", 5000),
  p(2, "林芳正", "はやしよしまさ", "自由民主党・無所属の会", 3000),
  p(3, "早矢仕彩", "はやしあや", "立憲民主党", 200),
  p(4, "高市早苗", "たかいちさなえ", "自由民主党・無所属の会", 4000),
  p(5, "黄川田仁志", "きかわだひとし", "自由民主党・無所属の会", 800),
  p(6, "小林鷹之", "こばやしたかゆき", "自由民主党・無所属の会", 900),
  p(7, "泉健太", "いずみけんた", "立憲民主党", 2000),
];

const names = (rows: PoliticianLite[]) => rows.map((r) => r.name);

describe("matchPoliticians（議員名の検索）", () => {
  it("2文字の姓で引ける（発言DBを使わない一番の理由）", () => {
    assert.deepEqual(names(matchPoliticians("岸田", LIST)), ["岸田文雄"]);
  });

  it("1文字の姓でも引ける", () => {
    // 「林」は林芳正の前方一致、小林鷹之の部分一致。前方一致が先
    assert.deepEqual(names(matchPoliticians("林", LIST)), ["林芳正", "小林鷹之"]);
  });

  it("氏名の一致をよみの一致より先に出す", () => {
    // 「はやし」: 林芳正はよみの前方一致、早矢仕彩もよみの前方一致。
    // 発言数の多い林芳正が先
    assert.deepEqual(names(matchPoliticians("はやし", LIST)), ["林芳正", "早矢仕彩"]);
  });

  it("カタカナで打っても当たる（よみはひらがなで持っている）", () => {
    assert.deepEqual(names(matchPoliticians("キシダ", LIST)), ["岸田文雄"]);
  });

  it("氏名に空白を入れても当たる", () => {
    assert.deepEqual(names(matchPoliticians("岸田 文雄", LIST)), ["岸田文雄"]);
    assert.deepEqual(names(matchPoliticians("岸田　文雄", LIST)), ["岸田文雄"]);
  });

  it("完全一致を前方一致より先に出す", () => {
    const list = [...LIST, p(8, "林", "はやし", "無所属", 10)];
    assert.deepEqual(names(matchPoliticians("林", list))[0], "林");
  });

  it("同じ強さなら発言数の多い順", () => {
    // どちらも会派の一致
    const rows = matchPoliticians("立憲民主党", LIST);
    assert.deepEqual(names(rows), ["泉健太", "早矢仕彩"]);
  });

  it("会派でも引ける（/politicians の絞り込みと揃える）", () => {
    assert.equal(matchPoliticians("自由民主党", LIST).length, 5);
  });

  it("会派を対象から外せる", () => {
    assert.deepEqual(matchPoliticians("自由民主党", LIST, { includeKaiha: false }), []);
  });

  it("会派の一致は氏名・よみの一致より後ろ", () => {
    // 「立憲民主党」に一致する人より、氏名に「泉」を含む人が先に来ること
    const rows = matchPoliticians("泉", LIST);
    assert.deepEqual(names(rows), ["泉健太"]);
  });

  it("3文字以上の姓も当然引ける（黄川田）", () => {
    assert.deepEqual(names(matchPoliticians("黄川田", LIST)), ["黄川田仁志"]);
  });

  it("該当が無ければ空", () => {
    assert.deepEqual(matchPoliticians("ホルムズ", LIST), []);
  });

  it("空文字・空白だけなら空（全員返さない）", () => {
    assert.deepEqual(matchPoliticians("", LIST), []);
    assert.deepEqual(matchPoliticians("   ", LIST), []);
    assert.deepEqual(matchPoliticians("　", LIST), []);
  });

  it("limit で切る", () => {
    assert.equal(matchPoliticians("自由民主党", LIST, { limit: 2 }).length, 2);
  });
});

describe("fold（照合用に畳む）", () => {
  it("カタカナをひらがなにする", () => {
    assert.equal(fold("キシダフミオ"), "きしだふみお");
  });

  it("空白を落とす（半角・全角とも）", () => {
    assert.equal(fold("岸田 文雄"), "岸田文雄");
    assert.equal(fold("岸田　文雄"), "岸田文雄");
  });

  it("漢字には触らない", () => {
    assert.equal(fold("黄川田仁志"), "黄川田仁志");
  });

  it("長音符と中黒には触らない（カタカナ表記の氏名のため）", () => {
    assert.equal(fold("アントニオ・猪木"), "あんとにお・猪木");
    assert.equal(fold("ジョン・レノン"), "じょん・れのん");
  });

  it("両側に同じものを掛けるので釣り合いは崩れない", () => {
    const list = [p(9, "アントニオ猪木", "あんとにおいのき", "無所属", 50)];
    // カタカナ表記の氏名を、ひらがなでもカタカナでも引ける
    assert.equal(matchPoliticians("アントニオ", list).length, 1);
    assert.equal(matchPoliticians("あんとにお", list).length, 1);
  });
});
