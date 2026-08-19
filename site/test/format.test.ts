/**
 * 画面に出す文言の回帰テスト。
 *
 * `describeError()` は**エラーメッセージの文字列に依存している**。
 * sql.js-httpvfs を上げたときに文言が変わると、言い換えが黙って効かなくなり、
 * 利用者には生の英語だけが残る（エラーにはならない）。ここで固定しておく。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { describeError } from "../src/lib/format.ts";

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
