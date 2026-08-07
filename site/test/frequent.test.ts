/**
 * 会期での絞り込み（`rankBySession`）の回帰テスト。
 *
 * ここが壊れると**「その会期で増えた語」に一般語が並ぶ**。件数順にしただけの
 * 一覧は `日本` `国民` `経済` で埋まり、会期を選ぶ意味が消える
 * （頻度順が使えないことは `scripts/build_frequent.py` で実測済み）。
 *
 * 誤りが目に見えないたぐいなので、率の計算と並び順をここで固める。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  formatSpan, rankBySession, type SessionMeta, type SessionWord,
} from "../src/lib/frequent.ts";

// 会期2つ。分母が倍違う（発言数の多い会期ほど件数が増えるので、率にしないと比べられない）
const SESSIONS: SessionMeta[] = [
  { session: 213, from: "2024-01-26", until: "2024-08-23", n_speeches: 1000 },
  { session: 217, from: "2025-01-24", until: "2025-06-21", n_speeches: 2000 },
];
const TOTAL = 10_000;

function word(term: string, n: number, hits: number[], topicId: number | null = null): SessionWord {
  return { term, n, hits, topicId };
}

describe("rankBySession（会期で「いつもより多く出た語」を並べる）", () => {
  it("件数順ではなく率で並べる（一般語を上に出さない）", () => {
    const words = [
      // どの会期でも多い一般語。第213回でも100件出るが、全期間でも1,000件ある
      word("国民", 1000, [100, 200]),
      // 第213回に集中した語。件数は少ないが、全期間の6割がこの会期
      word("裏金", 100, [60, 5]),
    ];
    const ranked = rankBySession(words, SESSIONS, 0, TOTAL);

    assert.equal(ranked[0].term, "裏金", "件数で並べると国民が上に来てしまう");
    assert.equal(ranked[0].hits, 60);
    // (60/1000) / (100/10000) = 6.0
    assert.equal(ranked[0].lift, 6);
    // (100/1000) / (1000/10000) = 1.0 ＝ いつもどおり
    assert.equal(ranked[1].lift, 1);
  });

  it("分母で割る（発言数の多い会期ほど件数が増えるだけの語を上げない）", () => {
    // 同じ語が第217回で倍の件数。ただし分母も倍なので率は同じ
    const words = [word("予算", 300, [100, 200])];
    const a = rankBySession(words, SESSIONS, 0, TOTAL, { minHits: 1 });
    const b = rankBySession(words, SESSIONS, 1, TOTAL, { minHits: 1 });

    assert.equal(a[0].lift, b[0].lift, "件数が倍でも分母が倍なら同じ扱いにする");
  });

  it("件数の下限を置く（3件が9件になっただけの語を3倍として出さない）", () => {
    const words = [
      word("珍語", 10, [9, 1]),      // lift は高いが9件しかない
      word("暫定税率", 200, [100, 20]),
    ];
    const ranked = rankBySession(words, SESSIONS, 0, TOTAL);

    assert.deepEqual(ranked.map((r) => r.term), ["暫定税率"]);
  });

  it("下限を下げれば出る（既定値に依存していないことの確認）", () => {
    const words = [word("珍語", 10, [9, 1])];
    assert.equal(rankBySession(words, SESSIONS, 0, TOTAL, { minHits: 5 }).length, 1);
  });

  it("同じ率なら件数の多い順。それも同じなら語順で決める（並びを揺らさない）", () => {
    const words = [
      word("い", 100, [50, 0]),
      word("あ", 100, [50, 0]),
      word("う", 200, [100, 0]),
    ];
    const ranked = rankBySession(words, SESSIONS, 0, TOTAL, { minHits: 1 });

    assert.deepEqual(ranked.map((r) => r.term), ["う", "あ", "い"]);
  });

  it("limit で切る", () => {
    const words = Array.from({ length: 50 }, (_, i) => word(`語${i}`, 100, [50, 0]));
    assert.equal(rankBySession(words, SESSIONS, 0, TOTAL, { limit: 10 }).length, 10);
  });

  it("争点語の印を落とさない（一覧から争点語ページへ寄せるため）", () => {
    const words = [word("裏金", 100, [60, 5], 41)];
    assert.equal(rankBySession(words, SESSIONS, 0, TOTAL)[0].topicId, 41);
  });

  describe("計算にならない入力で NaN を並べない", () => {
    const words = [word("何か", 100, [60, 5])];

    it("会期が範囲外なら空", () => {
      assert.deepEqual(rankBySession(words, SESSIONS, 9, TOTAL), []);
    });

    it("会期の分母が0なら空", () => {
      const broken = [{ ...SESSIONS[0], n_speeches: 0 }];
      assert.deepEqual(rankBySession(words, broken, 0, TOTAL), []);
    });

    it("全期間の分母が0なら空", () => {
      assert.deepEqual(rankBySession(words, SESSIONS, 0, 0), []);
    });

    it("全期間の件数が0の語は落とす（0除算にしない）", () => {
      assert.deepEqual(rankBySession([word("幽霊", 0, [60, 5])], SESSIONS, 0, TOTAL), []);
    });

    it("会期の件数が欠けていても落ちない", () => {
      assert.deepEqual(rankBySession([word("欠け", 100, [])], SESSIONS, 0, TOTAL), []);
    });
  });
});

describe("formatSpan", () => {
  it("年をまたがないなら年を繰り返さない", () => {
    assert.equal(formatSpan("2024-01-26", "2024-08-23"), "2024年1月26日〜8月23日");
  });

  it("年をまたぐなら両方に年を出す", () => {
    assert.equal(formatSpan("2025-10-21", "2026-01-22"), "2025年10月21日〜2026年1月22日");
  });
});
