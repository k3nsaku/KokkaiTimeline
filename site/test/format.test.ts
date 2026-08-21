/**
 * 画面に出す文言の回帰テスト。
 *
 * `describeError()` は**エラーメッセージの文字列に依存している**。
 * sql.js-httpvfs を上げたときに文言が変わると、言い換えが黙って効かなくなり、
 * 利用者には生の英語だけが残る（エラーにはならない）。ここで固定しておく。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { describeError, jsonScript } from "../src/lib/format.ts";

describe("describeError", () => {
  it("配信DBを読めないときはキャッシュ削除を促す", () => {
    // sql.js-httpvfs が実際に投げてくる形（2026-08-19 に実機で観測したもの）
    const err = new Error("SQLite: file is not a database");
    const text = describeError(err);
    assert.match(text, /キャッシュ/);
    assert.doesNotMatch(text, /SQLite/);
  });

  it("知らないエラーは言い換えない", () => {
    const err = new Error("no such table: speech_fts");
    assert.equal(describeError(err), "Error: no such table: speech_fts");
  });

  it("Error でないものも落ちない", () => {
    assert.equal(describeError("目録を読めません: 503"), "目録を読めません: 503");
    assert.equal(describeError(null), "null");
  });
});

describe("jsonScript", () => {
  it("</script> でタグを閉じさせない", () => {
    // 配信DBや生成JSONが書き換えられたときに、埋め込みがHTMLから抜け出さないこと。
    // **CSP に頼らない**（CSP を緩めた瞬間に静かに効かなくなる作りにしない）
    const out = jsonScript({ term: "</script><img src=x onerror=alert(1)>" });
    assert.doesNotMatch(out, /<\/script/i);
    assert.ok(!out.includes("<"));
  });

  it("読む側は素の JSON.parse のままでよい", () => {
    const value = { term: "<a>", n: 3, list: ["＜", "安全保障"] };
    assert.deepEqual(JSON.parse(jsonScript(value)), value);
  });
});
