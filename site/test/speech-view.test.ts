/**
 * 前後の発言の振り分け（`splitContext`）の回帰テスト。
 *
 * `/speech` と本文パネルの両方がこれを使う。ここがずれると
 * **対象の発言が抜粋としても二重に出る**か、前の発言が後ろ側に混ざる。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  renderContextList, splitContext, type ContextRow,
} from "../src/lib/speech-view.ts";

function row(order: number, speaker = `話者${order}`): ContextRow {
  return {
    speech_id: `id_${order}`, speech_order: order, speaker,
    speaker_kind: "議員", politician_id: order, head: `${order}番目の発言`,
  };
}

const orders = (rows: ContextRow[]) => rows.map((r) => r.speech_order);

describe("splitContext（前後の振り分け）", () => {
  it("対象より前は before、後は after", () => {
    const { before, after } = splitContext([row(3), row(4), row(5), row(6), row(7)], 5);
    assert.deepEqual(orders(before), [3, 4]);
    assert.deepEqual(orders(after), [6, 7]);
  });

  it("対象そのものはどちらにも入れない（本文で出すので二重にしない）", () => {
    const { before, after } = splitContext([row(4), row(5), row(6)], 5);
    assert.equal(before.some((r) => r.speech_order === 5), false);
    assert.equal(after.some((r) => r.speech_order === 5), false);
  });

  it("順序が乱れて渡されても speech_order で並べ直す", () => {
    const { before, after } = splitContext([row(7), row(3), row(6), row(4)], 5);
    assert.deepEqual(orders(before), [3, 4]);
    assert.deepEqual(orders(after), [6, 7]);
  });

  it("会議の先頭なら before は空", () => {
    const { before, after } = splitContext([row(0), row(1), row(2)], 0);
    assert.deepEqual(orders(before), []);
    assert.deepEqual(orders(after), [1, 2]);
  });

  it("会議の末尾なら after は空", () => {
    const { before, after } = splitContext([row(8), row(9), row(10)], 10);
    assert.deepEqual(orders(before), [8, 9]);
    assert.deepEqual(orders(after), []);
  });

  it("対象しか無ければ両方とも空", () => {
    assert.deepEqual(splitContext([row(5)], 5), { before: [], after: [] });
  });

  it("空の入力でも落ちない", () => {
    assert.deepEqual(splitContext([], 5), { before: [], after: [] });
  });

  it("渡された配列を書き換えない", () => {
    const rows = [row(7), row(3)];
    splitContext(rows, 5);
    assert.deepEqual(orders(rows), [7, 3]);
  });
});

describe("renderContextList", () => {
  it("空なら何も出さない（見出しだけが残らないように）", () => {
    assert.equal(renderContextList([], { label: "この前の発言" }), "");
  });

  it("パネルが横取りできるよう data-speech-id を付ける", () => {
    const html = renderContextList([row(4)], { label: "この前の発言" });
    assert.match(html, /data-speech-id="id_4"/);
    assert.match(html, /href="\/speech\/id_4"/);
  });

  it("発言者名を HTML エスケープする", () => {
    const html = renderContextList(
      [{ ...row(4), speaker: `<script>x</script>`, politician_id: null }],
      { label: "この前の発言" });
    assert.equal(html.includes("<script>"), false);
    assert.match(html, /&lt;script&gt;/);
  });

  it("見出しもエスケープする", () => {
    const html = renderContextList([row(4)], { label: `"><b>` });
    assert.equal(html.includes("<b>"), false);
  });

  it("politician_id が無ければリンクにしない", () => {
    const html = renderContextList([{ ...row(4), politician_id: null }],
      { label: "この前の発言" });
    assert.equal(html.includes("/politician/"), false);
  });
});
