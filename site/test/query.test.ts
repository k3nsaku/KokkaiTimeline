/**
 * 検索SQLと件数SQLが同じものを数えているかの回帰テスト。
 *
 * `countQuery()` は結果取得とは**別のSQL**なので、絞り込みを片方だけに足すと
 * 一覧の中身と画面上部の件数が黙って食い違う。実際に会議名の絞り込みが件数に
 * 効いておらず、2,594件と557件が入れ替わっていた
 * （docs/PITFALLS.md）。**FTS・争点語・2文字語の3経路すべてにある。**
 *
 * 年DBと同じ形の小さなDBを `node:sqlite` でメモリに作り、
 * 「searchQuery が返した行数」と「countQuery が返した数」を突き合わせる。
 */

import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { describe, it } from "node:test";

import {
  canonicalQuery, countQuery, fillBounds, monthBoundsQuery, monthlyQuery, monthsInPeriod,
  searchQuery, splitTerms, toFullWidth, toMatchExpr, toWordKey,
  wordPlan, wordProbeKeys, periodOf, periodOfIssueId, periodOfSpeechId,
  periodsInYearRange, unsearchableTerms, yearsOfPeriods,
  type QueryPlan, type SearchOptions,
} from "../src/lib/query.ts";

// --- 期間DBを模したデータ -------------------------------------------------

const MEETINGS = [
  { issue_id: "A", name: "内閣委員会", house: "衆議院" },
  { issue_id: "B", name: "予算委員会", house: "参議院" },
];

/** 争点語「安全保障」と2文字語「憲法」が、会議・議員・発言者種別に
 *  ばらけて出てくるようにしてある（どれか1つの条件だけで数が動くと困る）。 */
const SPEECHES = [
  { issue: "A", pol: 1,    kind: "議員",       body: "安全保障の在り方について憲法の観点から伺う。" },
  { issue: "A", pol: 1,    kind: "議員",       body: "経済安全保障推進法の運用について。" },
  { issue: "A", pol: 2,    kind: "議員",       body: "憲法改正の議論を進めるべきだ。" },
  { issue: "A", pol: 2,    kind: "議員",       body: "安全保障環境の変化を踏まえた対応が要る。" },
  { issue: "A", pol: null, kind: "参考人",     body: "安全保障については専門家として憲法にも触れたい。" },
  { issue: "B", pol: 1,    kind: "議員",       body: "予算における安全保障関連経費の内訳を問う。" },
  { issue: "B", pol: 1,    kind: "議員",       body: "地方財政の話をする。" },
  // 議員1は両方の会議で「憲法」に触れる。片方だけの絞り込みでも数が動くようにするため
  // （両方が同じ会議に寄っていると「会議名 + 議員」の組が素通りしてしまう）
  { issue: "B", pol: 1,    kind: "議員",       body: "憲法の議論は予算委員会でも避けられない。" },
  { issue: "B", pol: 1,    kind: "議員",       body: "予算委員会でも憲法と安全保障の関係に触れておく。" },
  { issue: "B", pol: 2,    kind: "議員",       body: "憲法審査会の運営について申し上げる。" },
  { issue: "B", pol: 2,    kind: "議員",       body: "安全保障と憲法の関係を整理したい。" },
  { issue: "B", pol: 3,    kind: "議員",       body: "安全保障の議論は丁寧にやるべきだ。" },
  { issue: "B", pol: null, kind: "政府参考人等", body: "安全保障の運用実態をご説明する。" },
  // ★会議録の英数字は全部全角。半角で打たれても引けなければならない（docs/DECISIONS.md）。
  //   小文字を含む語（ＳＤＧｓ）は、大文字に寄せると引けなくなるので入れてある
  { issue: "B", pol: 3,    kind: "議員",       body: "ＬＧＢＴ理解増進法とＳＤＧｓの推進について伺う。" },
  // ★2文字の全角ラテン（docs/DECISIONS.md）。FTS では**原理的に引けない**ので語彙に入れる
  { issue: "A", pol: 2,    kind: "議員",       body: "ＡＩの利活用とＧ７での議論について伺う。" },
  { issue: "B", pol: 1,    kind: "議員",       body: "生成ＡＩの規制は憲法との関係でも論点になる。" },
];

/** 発言を月にばらけさせる。**rowid の昇順が日付の昇順**でなければならない
 *  （`build_db.py` の `load()` がそう並べる。月別の集計はこれに乗っている）。 */
const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04"];
const dateOf = (i: number) => `${MONTHS[Math.min(Math.floor(i / 4), MONTHS.length - 1)]}-01`;

const TOPIC_ID = 1;
const TOPIC_TERM = "安全保障";
const WORD_TERM = "憲法";
/** 2文字の全角ラテン。**語彙は大文字に畳んである**（`build_words.py` の `fold()`）。 */
const LATIN_WORD = "ＡＩ";
const DIGIT_WORD = "Ｇ７";
const VOCABULARY = [WORD_TERM, LATIN_WORD, DIGIT_WORD];

function fixture(): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  db.exec(`
    CREATE TABLE meeting (
      issue_id TEXT PRIMARY KEY, session INTEGER NOT NULL, house TEXT NOT NULL,
      name TEXT NOT NULL, issue TEXT, date TEXT NOT NULL,
      meeting_url TEXT, pdf_url TEXT);
    CREATE TABLE speech (
      speech_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, speech_order INTEGER NOT NULL,
      date TEXT NOT NULL, speaker TEXT NOT NULL, speaker_yomi TEXT, speaker_group TEXT,
      speaker_position TEXT, speaker_role TEXT, body TEXT NOT NULL, start_page INTEGER,
      speech_url TEXT NOT NULL, is_speech INTEGER NOT NULL, speaker_kind TEXT NOT NULL,
      politician_id INTEGER);
    CREATE TABLE topic (
      id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE, category TEXT, variants TEXT,
      n_speeches INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE topic_hit (
      topic_id INTEGER NOT NULL, speech_rowid INTEGER NOT NULL, n INTEGER NOT NULL,
      PRIMARY KEY (topic_id, speech_rowid)) WITHOUT ROWID;
    CREATE TABLE word (
      id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE,
      n_speeches INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE word_hit (
      word_id INTEGER NOT NULL, speech_rowid INTEGER NOT NULL,
      PRIMARY KEY (word_id, speech_rowid)) WITHOUT ROWID;
    CREATE VIRTUAL TABLE speech_fts USING fts5(
      body, content='speech', content_rowid='rowid', tokenize='trigram');
    -- ★ 月境界の seek はこの索引だけで済ませる（build_db.py と同じ）
    CREATE INDEX idx_speech_date ON speech(date);
  `);

  const meeting = db.prepare(
    "INSERT INTO meeting VALUES (?, 217, ?, ?, '1号', '2026-01-01', NULL, NULL)");
  for (const m of MEETINGS) meeting.run(m.issue_id, m.house, m.name);

  const speech = db.prepare(`
    INSERT INTO speech (rowid, speech_id, issue_id, speech_order, date, speaker, body,
                        speech_url, is_speech, speaker_kind, politician_id)
    VALUES (?, ?, ?, ?, ?, '誰か', ?, 'http://example.invalid', 1, ?, ?)`);
  SPEECHES.forEach((s, i) => {
    speech.run(i + 1, `S${i + 1}`, s.issue, i + 1, dateOf(i), s.body, s.kind, s.pol);
  });

  // ★ 索引は「議員」だけに張る（build_db.py と同じ）。rebuild は使わない
  db.exec(`INSERT INTO speech_fts(rowid, body)
           SELECT rowid, body FROM speech WHERE speaker_kind = '議員'`);

  const indexed = SPEECHES
    .map((s, i) => ({ ...s, rowid: i + 1 }))
    .filter((s) => s.kind === "議員");

  const topicRows = indexed.filter((s) => s.body.includes(TOPIC_TERM));
  db.prepare("INSERT INTO topic VALUES (?, ?, NULL, '[]', ?)")
    .run(TOPIC_ID, TOPIC_TERM, topicRows.length);
  const hit = db.prepare("INSERT INTO topic_hit VALUES (?, ?, 1)");
  for (const s of topicRows) hit.run(TOPIC_ID, s.rowid);

  // 語彙は部分文字列で索引化する（「憲法改正」の中の「憲法」も引けないと意味がない）。
  // build_db.py の build_word_index() と同じ数え方
  const word = db.prepare("INSERT INTO word VALUES (?, ?, ?)");
  const whit = db.prepare("INSERT INTO word_hit VALUES (?, ?)");
  VOCABULARY.forEach((term, i) => {
    const rows = indexed.filter((s) => s.body.includes(term));
    word.run(i + 1, term, rows.length);
    for (const s of rows) whit.run(i + 1, s.rowid);
  });

  return db;
}

// --- 突き合わせ -----------------------------------------------------------

/** 検索SQLが返す行数。カーソル無し・LIMIT は全件が入る大きさにする。 */
function fetched(db: DatabaseSync, opts: SearchOptions, plan?: QueryPlan): number {
  const [sql, params] = searchQuery({ ...opts, limit: 1000 }, plan, undefined);
  return db.prepare(sql).all(...(params as never[])).length;
}

function counted(db: DatabaseSync, opts: SearchOptions, plan?: QueryPlan): number {
  const [sql, params] = countQuery(opts, plan);
  const row = db.prepare(sql).get(...(params as never[])) as { n: number } | undefined;
  return row?.n ?? 0;
}

/** 検索の3経路。UI からは `resolveQuery()` がこのどれかを選ぶ。 */
const MODES: { name: string; opts: SearchOptions; plan?: QueryPlan }[] = [
  {
    name: "FTS（3文字以上）",
    opts: { query: TOPIC_TERM },
    plan: { mode: "fts", match: toMatchExpr(TOPIC_TERM) },
  },
  {
    name: "争点語（topic_hit）",
    opts: { topicId: TOPIC_ID },
  },
  {
    name: "2文字語（word_hit）",
    opts: { query: WORD_TERM },
    plan: { mode: "word", driver: WORD_TERM, filters: [] },
  },
  {
    name: "2文字語 + 絞り込みの語",
    opts: { query: `${WORD_TERM} 安全保障` },
    plan: { mode: "word", driver: WORD_TERM, filters: ["安全保障"] },
  },
];

/** 絞り込みの組み合わせ。**どれも件数SQLに効いていなければならない。** */
const FILTERS: { name: string; extra: SearchOptions }[] = [
  { name: "絞り込み無し", extra: {} },
  { name: "会議名", extra: { meetingName: "内閣委員会" } },
  { name: "議員", extra: { politicianId: 1 } },
  { name: "会議名 + 議員", extra: { meetingName: "内閣委員会", politicianId: 1 } },
  { name: "一致が0件になる会議名", extra: { meetingName: "存在しない委員会" } },
];

describe("searchQuery と countQuery が同じものを数える", () => {
  for (const mode of MODES) {
    for (const f of FILTERS) {
      it(`${mode.name} × ${f.name}`, () => {
        const db = fixture();
        try {
          const opts = { ...mode.opts, ...f.extra };
          const rows = fetched(db, opts, mode.plan);
          assert.equal(counted(db, opts, mode.plan), rows);
        } finally {
          db.close();
        }
      });
    }
  }
});

describe("絞り込みが実際に効いている", () => {
  it("会議名で絞ると件数が減る（減らなければ絞れていない）", () => {
    const db = fixture();
    try {
      for (const mode of MODES) {
        const all = counted(db, mode.opts, mode.plan);
        const filtered = counted(db, { ...mode.opts, meetingName: "内閣委員会" }, mode.plan);
        assert.ok(all > 0, `${mode.name}: 素の件数が0では検証にならない`);
        assert.ok(filtered < all,
                  `${mode.name}: 会議名で絞っても件数が ${all} のまま`);
      }
    } finally {
      db.close();
    }
  });

  it("結果の行がすべて指定した会議のもの", () => {
    const db = fixture();
    try {
      for (const mode of MODES) {
        const [sql, params] = searchQuery(
          { ...mode.opts, meetingName: "内閣委員会", limit: 1000 }, mode.plan, undefined);
        const rows = db.prepare(sql).all(...(params as never[])) as { meeting: string }[];
        assert.ok(rows.length > 0, `${mode.name}: 0件では検証にならない`);
        assert.ok(rows.every((r) => r.meeting === "内閣委員会"), mode.name);
      }
    } finally {
      db.close();
    }
  });
});

// --- 月別の件数 -----------------------------------------------------------
//
// 検索結果ページのグラフ。**日付では GROUP BY せず rowid の範囲で割る**ので、
// 「バケットが本当に日付と一致しているか」をここで押さえる。
// ずれても件数の合計は合ってしまうため、合計だけ見ていると気づけない。

/** 月別の件数（月 → 件数）。**境界も本番と同じ SQL で引く。** */
function monthly(
  db: DatabaseSync, opts: SearchOptions, plan?: QueryPlan, months = MONTHS,
): Map<string, number> {
  const [boundsSql, boundsParams] = monthBoundsQuery(months);
  const raw = db.prepare(boundsSql).all(...(boundsParams as never[])) as
    { i: number; at: number | null }[];
  const bounds = fillBounds(
    [...raw].sort((a, b) => a.i - b.i).map((r) => r.at), months.length);

  const [sql, params] = monthlyQuery(opts, plan, bounds);
  const rows = db.prepare(sql).all(...(params as never[])) as { b: number; n: number }[];
  const out = new Map<string, number>();
  for (const r of rows) out.set(months[r.b], (out.get(months[r.b]) ?? 0) + r.n);
  return out;
}

/** 答え合わせ用。**検索結果そのものを日付でまとめる**（重いので本番ではやらない）。 */
function monthlyByDate(
  db: DatabaseSync, opts: SearchOptions, plan?: QueryPlan,
): Map<string, number> {
  const [sql, params] = searchQuery({ ...opts, limit: 1000 }, plan, undefined);
  const rows = db.prepare(sql).all(...(params as never[])) as { date: string }[];
  const out = new Map<string, number>();
  for (const r of rows) {
    const month = r.date.slice(0, 7);
    out.set(month, (out.get(month) ?? 0) + 1);
  }
  return out;
}

describe("月別の件数（検索結果のグラフ）", () => {
  for (const mode of MODES) {
    for (const f of FILTERS) {
      it(`${mode.name} × ${f.name}: 合計が件数と一致する`, () => {
        const db = fixture();
        try {
          const opts = { ...mode.opts, ...f.extra };
          const sum = [...monthly(db, opts, mode.plan).values()].reduce((a, b) => a + b, 0);
          assert.equal(sum, counted(db, opts, mode.plan));
        } finally {
          db.close();
        }
      });

      it(`${mode.name} × ${f.name}: 月の割り当てが日付と一致する`, () => {
        const db = fixture();
        try {
          const opts = { ...mode.opts, ...f.extra };
          const got = monthly(db, opts, mode.plan);
          const want = monthlyByDate(db, opts, mode.plan);
          // 0件の月は返らないので、あるものだけを突き合わせる
          assert.deepEqual([...got].sort(), [...want].sort());
        } finally {
          db.close();
        }
      });
    }
  }

  it("月をまたいで数が分かれている（1つの月に寄っていたら検証にならない）", () => {
    const db = fixture();
    try {
      const got = monthly(db, MODES[0].opts, MODES[0].plan);
      assert.ok(got.size >= 2, `月が ${got.size} 種類しか出ていない`);
    } finally {
      db.close();
    }
  });

  it("発言の無い月は 0 件になる（境界が NULL でも壊れない）", () => {
    const db = fixture();
    try {
      // 2026-05 以降にはデータが無い。**その月の seek は NULL を返す**
      const months = [...MONTHS, "2026-05", "2026-06"];
      const got = monthly(db, MODES[0].opts, MODES[0].plan, months);
      assert.equal(got.get("2026-05"), undefined);
      assert.equal(got.get("2026-06"), undefined);
      // 手前の月は変わらない
      assert.deepEqual([...got].sort(), [...monthly(db, MODES[0].opts, MODES[0].plan)].sort());
    } finally {
      db.close();
    }
  });
});

describe("monthBoundsQuery（月の先頭 rowid）", () => {
  it("GROUP BY で数えた境界と一致する", () => {
    const db = fixture();
    try {
      const [sql, params] = monthBoundsQuery(MONTHS);
      const got = (db.prepare(sql).all(...(params as never[])) as { i: number; at: number }[])
        .sort((a, b) => a.i - b.i).map((r) => r.at);
      // ★ こちらが答え。全走査になるので本番では使わない
      const want = (db.prepare(
        "SELECT substr(date, 1, 7) AS m, MIN(rowid) AS at FROM speech GROUP BY m ORDER BY m")
        .all() as { m: string; at: number }[]).map((r) => r.at);
      assert.deepEqual(got, want);
    } finally {
      db.close();
    }
  });

  it("speech の行を読まない（covering index だけで済む）", () => {
    const db = fixture();
    try {
      const [sql, params] = monthBoundsQuery(MONTHS);
      const plan = (db.prepare(`EXPLAIN QUERY PLAN ${sql}`)
        .all(...(params as never[])) as { detail: string }[]).map((r) => r.detail).join(" / ");
      assert.ok(plan.includes("COVERING INDEX idx_speech_date"), plan);
    } finally {
      db.close();
    }
  });
});

describe("monthsInPeriod（期間DBに入っている月）", () => {
  it("半期は6か月ぶん", () => {
    assert.deepEqual(monthsInPeriod("2025H1"),
                     ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]);
    assert.deepEqual(monthsInPeriod("2025H2"),
                     ["2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"]);
  });

  it("year 規則なら12か月ぶん", () => {
    assert.equal(monthsInPeriod("2025").length, 12);
    assert.equal(monthsInPeriod("2025")[0], "2025-01");
    assert.equal(monthsInPeriod("2025")[11], "2025-12");
  });

  it("目録の収録範囲で詰める（空振りする seek を減らす）", () => {
    assert.deepEqual(monthsInPeriod("2026H2", "2026-07-01", "2026-07-31"), ["2026-07"]);
    assert.deepEqual(monthsInPeriod("2025H1", "2025-01-24", "2025-06-21"),
                     ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]);
  });
});

describe("検索の前提", () => {
  it("索引に入るのは議員の発言だけ（参考人は検索に出ない）", () => {
    const db = fixture();
    try {
      const [sql, params] = searchQuery(
        { query: TOPIC_TERM, limit: 1000 },
        { mode: "fts", match: toMatchExpr(TOPIC_TERM) }, undefined);
      const rows = db.prepare(sql).all(...(params as never[])) as { speech_id: string }[];
      // 参考人・政府参考人等にも「安全保障」を含む発言があるが、索引には入らない。
      // （speech_id はデータから引く。行を足したときに番号がずれても壊れないように）
      const outside = SPEECHES
        .map((s, i) => ({ ...s, id: `S${i + 1}` }))
        .filter((s) => s.kind !== "議員" && s.body.includes(TOPIC_TERM));
      assert.ok(outside.length > 0, "索引外の発言が無ければ検証にならない");
      for (const s of outside) {
        assert.equal(rows.some((r) => r.speech_id === s.id), false, `${s.id}（${s.kind}）`);
      }
    } finally {
      db.close();
    }
  });

  it("2文字語は FTS では引けない（word_hit が要る理由）", () => {
    const db = fixture();
    try {
      const n = db.prepare("SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?")
        .get(toMatchExpr(WORD_TERM)) as { n: number };
      assert.equal(n.n, 0, "trigram が2文字を引けてしまうなら word_hit の前提が変わる");

      // word_hit なら引ける
      const plan: QueryPlan = { mode: "word", driver: WORD_TERM, filters: [] };
      assert.ok(counted(db, { query: WORD_TERM }, plan) > 0);
    } finally {
      db.close();
    }
  });

  it("半角で打った英数字でも引ける（会議録は全部全角）", () => {
    const db = fixture();
    try {
      // 素の半角では0件。**これが直っていないと利用者は「ＡＩ」に辿り着けない**
      const raw = db.prepare("SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?")
        .get('"LGBT"') as { n: number };
      assert.equal(raw.n, 0, "半角で引けてしまうなら全角化の前提が変わる");

      // splitTerms を通せば引ける（3経路とも同じ入口を通る）
      const plan: QueryPlan = { mode: "fts", match: toMatchExpr("LGBT") };
      const rows = fetched(db, { query: "LGBT" }, plan);
      assert.equal(rows, 1);
      assert.equal(counted(db, { query: "LGBT" }, plan), rows);
    } finally {
      db.close();
    }
  });

  it("大文字に寄せない（ＳＤＧｓ のような語が引けなくなる）", () => {
    const db = fixture();
    try {
      // FTS5 の trigram は全角ラテンの大小を畳むので、どちらで打っても引ける
      for (const q of ["SDGs", "sdgs", "ＳＤＧｓ"]) {
        const plan: QueryPlan = { mode: "fts", match: toMatchExpr(q) };
        assert.equal(fetched(db, { query: q }, plan), 1, q);
      }
      // 全角化そのものは大小を保つ。ここで大文字化すると `ｉＰＳ` `ＩｏＴ` が壊れる
      assert.equal(toFullWidth("SDGs"), "ＳＤＧｓ");
    } finally {
      db.close();
    }
  });

  it("新しい順は rowid の降順で返る", () => {
    const db = fixture();
    try {
      const [sql, params] = searchQuery({ topicId: TOPIC_ID, limit: 1000 }, undefined, undefined);
      const rows = db.prepare(sql).all(...(params as never[])) as { rowid: number }[];
      const ids = rows.map((r) => r.rowid);
      assert.deepEqual(ids, [...ids].sort((a, b) => b - a));
    } finally {
      db.close();
    }
  });

  it("カーソルより後（古い側）だけを返す", () => {
    const db = fixture();
    try {
      const page1 = searchQuery({ topicId: TOPIC_ID, limit: 2 }, undefined, undefined);
      const rows1 = db.prepare(page1[0]).all(...(page1[1] as never[])) as { rowid: number }[];
      const cursor = rows1[rows1.length - 1].rowid;

      const page2 = searchQuery({ topicId: TOPIC_ID, limit: 2 }, undefined, cursor);
      const rows2 = db.prepare(page2[0]).all(...(page2[1] as never[])) as { rowid: number }[];
      assert.ok(rows2.every((r) => r.rowid < cursor));
    } finally {
      db.close();
    }
  });
});

/**
 * 検索語の全角化（`docs/DECISIONS.md`）。
 *
 * **ここを NFKC で書き直さないための固定。** NFKC は全角→半角に潰す逆方向の
 * 正規化で、使うと会議録（全部全角）に1件も当たらなくなる。
 */
describe("検索語の全角化", () => {
  it("英数字だけを全角にする", () => {
    assert.equal(toFullWidth("AI"), "ＡＩ");
    assert.equal(toFullWidth("LGBT法案"), "ＬＧＢＴ法案");
    assert.equal(toFullWidth("G7"), "Ｇ７");
  });

  it("全角で打たれた語を壊さない（NFKC との違い）", () => {
    assert.equal(toFullWidth("ＡＩ"), "ＡＩ");
    assert.equal("ＡＩ".normalize("NFKC"), "AI");   // ← これをやると全滅する
  });

  it("日本語と記号には触らない", () => {
    assert.equal(toFullWidth("安全保障"), "安全保障");
    assert.equal(toFullWidth("こども家庭庁・ＤＸ"), "こども家庭庁・ＤＸ");
  });

  it("splitTerms が全角化の入口（ここを通れば3経路とも直る）", () => {
    assert.deepEqual(splitTerms("AI 規制"), ["ＡＩ", "規制"]);
    assert.deepEqual(splitTerms("  LGBT　理解増進 "), ["ＬＧＢＴ", "理解増進"]);
    // ハイライトに渡す語もここから取るので、本文（全角）に当たる形になる
    assert.deepEqual(splitTerms("iPS細胞"), ["ｉＰＳ細胞"]);
  });

  it("toMatchExpr も全角化された語をフレーズにする", () => {
    assert.equal(toMatchExpr("LGBT"), '"ＬＧＢＴ"');
    assert.equal(toMatchExpr("AI 規制"), '"ＡＩ" AND "規制"');
  });
});

/**
 * 2文字の全角ラテン（`docs/DECISIONS.md`）。`ＡＩ` 5,394件・`ＤＸ` 3,038件・
 * `ＧＸ` 2,692件・`Ｇ７` 4,438件。**FTS5 の trigram では原理的に引けない**ので、
 * `word` / `word_hit` の語彙に入れてある。
 *
 * 語彙は**全角ラテンを大文字に畳んだ形**で持つ（`build_words.py` の `fold()`）。
 * `w.term = ?` は BINARY 比較で SQLite が畳んでくれないため、
 * 畳まないと `ai` と打たれた分が `ａｉ` になって当たらない。
 */
describe("2文字の全角ラテン", () => {
  it("FTS では引けない（語彙に入れるしかない理由）", () => {
    const db = fixture();
    try {
      for (const q of [LATIN_WORD, DIGIT_WORD]) {
        const n = db.prepare("SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?")
          .get(toMatchExpr(q)) as { n: number };
        assert.equal(n.n, 0, `${q}: trigram が2文字を引けてしまうなら前提が変わる`);
      }
    } finally {
      db.close();
    }
  });

  it("半角・小文字で打っても word 経路で引ける", () => {
    const db = fixture();
    try {
      // 「ＡＩ」を含む発言があること自体は先に押さえておく（0件では検証にならない）
      const expected = SPEECHES.filter((s) => s.kind === "議員" && s.body.includes(LATIN_WORD));
      assert.ok(expected.length > 1, "ＡＩ を含む議員の発言が複数無いと検証にならない");

      for (const q of ["AI", "ai", "Ai", "ＡＩ", "ａｉ"]) {
        const plan = wordPlan(splitTerms(q), new Map([[LATIN_WORD, expected.length]]));
        assert.deepEqual(plan, { mode: "word", driver: LATIN_WORD, filters: [] }, q);
        assert.equal(fetched(db, { query: q }, plan), expected.length, q);
        assert.equal(counted(db, { query: q }, plan), expected.length, q);
      }
    } finally {
      db.close();
    }
  });

  it("数字を含む語も引ける（Ｇ７・５Ｇ のため語彙に数字を入れてある）", () => {
    const db = fixture();
    try {
      const expected = SPEECHES.filter((s) => s.kind === "議員" && s.body.includes(DIGIT_WORD));
      assert.ok(expected.length > 0);
      const plan = wordPlan(splitTerms("G7"), new Map([[DIGIT_WORD, expected.length]]));
      assert.deepEqual(plan, { mode: "word", driver: DIGIT_WORD, filters: [] });
      assert.equal(fetched(db, { query: "G7" }, plan), expected.length);
      assert.equal(counted(db, { query: "G7" }, plan), expected.length);
    } finally {
      db.close();
    }
  });

  it("畳むのは2文字以下だけ（ＳＤＧｓ に掛けると壊れる）", () => {
    // 掛けたらどうなるかを固定しておく。**3文字以上に掛けてはいけない**
    assert.equal(toWordKey("ＳＤＧｓ"), "ＳＤＧＳ");
    // 語彙に問い合わせるのは2文字以下だけなので、ＳＤＧｓ はそもそも来ない
    assert.deepEqual(wordProbeKeys(splitTerms("SDGs")), []);
    assert.deepEqual(wordProbeKeys(splitTerms("ai SDGs")), [LATIN_WORD]);
    // 全部3文字以上なら語彙を引く必要が無い（＝FTS）
    assert.equal(wordPlan(splitTerms("SDGs"), new Map()).mode, "fts");
  });

  it("畳むのはラテンだけ（漢字・カタカナ・記号に触らない）", () => {
    assert.equal(toWordKey("憲法"), "憲法");
    assert.equal(toWordKey("デジタル"), "デジタル");
    assert.equal(toWordKey("ＡＩ"), "ＡＩ");
  });

  it("画面とURLには実際に引いた語を出す（同じ検索が別URLにならない）", () => {
    for (const q of ["AI", "ai", "Ai", "ＡＩ", "ａｉ"]) {
      assert.equal(canonicalQuery(q), LATIN_WORD, q);
    }
    assert.equal(canonicalQuery("g7"), DIGIT_WORD);
    // 3文字以上は畳まない。**ここが崩れると `ＳＤＧｓ` を書き換えて見せることになる**
    assert.equal(canonicalQuery("SDGs"), "ＳＤＧｓ");
    assert.equal(canonicalQuery("ai SDGs 規制"), "ＡＩ ＳＤＧｓ 規制");
    // 空白は打たれたまま（畳むと、2つ空けただけで「〜として検索した」が出る）
    assert.equal(canonicalQuery("憲法  年金"), "憲法  年金");
  });
});

/**
 * `resolveQuery()` の判断部分。**DBに触らない純粋関数**に切り出してあるのでここで検証できる。
 *
 * ★ここが壊れると「引けるはずの語が黙って0件」になる。
 *   特に driver を**値で**外すと、畳んだ結果（`ＡＩ`）と打たれた語（`ａｉ`）が
 *   別物なので filters に残り、`instr(body, 'ａｉ')` が 0 を返して全滅する。
 */
describe("wordPlan（どの索引で引くかの判断）", () => {
  const counts = new Map([[LATIN_WORD, 5232], ["憲法", 1369], ["増税", 314]]);

  it("2文字語が1つなら filters は空", () => {
    assert.deepEqual(wordPlan(splitTerms("ai"), counts),
                     { mode: "word", driver: LATIN_WORD, filters: [] });
  });

  it("畳んだ語を filters に残さない（残すと instr が 0 を返す）", () => {
    const plan = wordPlan(splitTerms("ai 増税"), counts);
    assert.deepEqual(plan, { mode: "word", driver: "増税", filters: [LATIN_WORD] });
    // 打たれたままの `ａｉ` が混ざっていたら本文（全角大文字）に当たらない
    assert.equal(plan.mode === "word" && plan.filters.includes("ａｉ"), false);
  });

  it("いちばん珍しい語を起点にする（走査行数がこれで決まる）", () => {
    assert.deepEqual(wordPlan(splitTerms("ai 憲法"), counts),
                     { mode: "word", driver: "憲法", filters: [LATIN_WORD] });
    assert.deepEqual(wordPlan(splitTerms("憲法 増税"), counts),
                     { mode: "word", driver: "増税", filters: ["憲法"] });
  });

  it("3文字以上の語は畳まずに filters へ入れる", () => {
    assert.deepEqual(wordPlan(splitTerms("ai SDGs"), counts),
                     { mode: "word", driver: LATIN_WORD, filters: ["ＳＤＧｓ"] });
  });

  it("索引に無い語はそのまま起点にする（＝0件。引けない語という状態は無い）", () => {
    // 索引は本文の2文字窓を全部持っているので、無い＝本当に出てこない。
    // `w.term = 'ＱＺ'` が1行も当たらず、全期間で0件になる
    assert.deepEqual(wordPlan(splitTerms("ｑｚ"), counts),
                     { mode: "word", driver: "ＱＺ", filters: [] });
  });

  it("片方が索引に無いなら、そちらを起点にする（0件が確定するので最速）", () => {
    assert.deepEqual(wordPlan(splitTerms("ai qz"), counts),
                     { mode: "word", driver: "ＱＺ", filters: [LATIN_WORD] });
  });

  it("空の検索は当たりようのない起点にする（MATCH '' は構文エラーになる）", () => {
    assert.deepEqual(wordPlan(splitTerms("   "), counts),
                     { mode: "word", driver: "", filters: [] });
  });
});

/**
 * **仕様上0件になる語**の判定。画面で「引けません」と出す根拠になるので、
 * ここが緩いと**引ける語まで引けないと言ってしまう**。
 *
 * 索引は漢字・カタカナ・全角英数の**連続の中**しか2文字に切らない
 * （`build_db.py` の `WORD_RUN_PATTERN`）。
 */
describe("unsearchableTerms（引きようがない語）", () => {
  const of = (q: string) => unsearchableTerms(splitTerms(q));

  it("同じ文字種の2文字は引ける（語の途中でもよい）", () => {
    assert.deepEqual(of("治体"), []);      // 自治体の中。語として自立していなくても引ける
    assert.deepEqual(of("憲法"), []);
    assert.deepEqual(of("コロ"), []);      // カタカナ同士
    assert.deepEqual(of("AI"), []);        // 全角化されて `ＡＩ`
    assert.deepEqual(of("G7"), []);        // 全角英字＋全角数字は同じクラス
  });

  it("文字種をまたぐ2文字は引けない", () => {
    assert.deepEqual(of("踏ま"), ["踏ま"]);   // 漢字＋ひらがな
    assert.deepEqual(of("お金"), ["お金"]);   // ひらがな＋漢字
    assert.deepEqual(of("ため"), ["ため"]);   // ひらがなはどのクラスにも入らない
  });

  it("1文字は引けない（索引の項はちょうど2文字）", () => {
    assert.deepEqual(of("米"), ["米"]);
    assert.deepEqual(of("Ａ"), ["Ａ"]);
  });

  it("3文字以上は文字種をまたいでも引ける（FTS が拾う）", () => {
    assert.deepEqual(of("踏まえ"), []);
    assert.deepEqual(of("受け止め"), []);
    assert.deepEqual(of("ＳＤＧｓ"), []);
  });

  it("複数語なら引けないものだけを返す（打たれたままの形で）", () => {
    assert.deepEqual(of("安全保障 踏ま"), ["踏ま"]);
    assert.deepEqual(of("踏ま お金"), ["踏ま", "お金"]);
    assert.deepEqual(of("安全保障 予算"), []);
  });

  it("空の入力では何も返さない（「引けません」を出さない）", () => {
    assert.deepEqual(of("   "), []);
  });
});

/**
 * 配信DBの分割単位（期間）。**`scripts/build_db.py` の `period_of()` と同じ写像**で
 * なければならない。食い違うと存在しないファイルを引きに行って検索が丸ごと止まる。
 */
describe("periodOf（DBの分割単位）", () => {
  it("半期は7月1日で切る", () => {
    assert.equal(periodOf("2026-01-23"), "2026H1");
    assert.equal(periodOf("2026-06-30"), "2026H1");
    assert.equal(periodOf("2026-07-01"), "2026H2");
    assert.equal(periodOf("2026-12-31"), "2026H2");
  });

  it("year 規則なら年をそのまま返す", () => {
    assert.equal(periodOf("2026-07-01", "year"), "2026");
  });

  it("期間IDは辞書順が時系列順（mergePages の並べ替えがこれに乗る）", () => {
    const shuffled = ["2022H1", "2021H2", "2026H1", "2021H1", "2025H2"];
    assert.deepEqual([...shuffled].sort(),
                     ["2021H1", "2021H2", "2022H1", "2025H2", "2026H1"]);
  });

  it("speech_id / issue_id から期間を割り出す（末尾8桁が日付）", () => {
    assert.equal(periodOfSpeechId("120214261X00120260123_0"), "2026H1");
    assert.equal(periodOfSpeechId("120214261X00120260701_12"), "2026H2");
    assert.equal(periodOfIssueId("120214261X00120260123"), "2026H1");
    assert.equal(periodOfSpeechId("こわれている"), null);
  });

  it("年の絞り込みを期間に直す（**取りこぼしが出ないこと**）", () => {
    const all = ["2024H1", "2024H2", "2025H1", "2025H2", "2026H1", "2026H2"];
    // 「2025年だけ」が厳密に選べる。会期で割るとこれが成立しない
    assert.deepEqual(periodsInYearRange(all, 2025, 2025), ["2025H1", "2025H2"]);
    assert.deepEqual(periodsInYearRange(all, 2024, 2025),
                     ["2024H1", "2024H2", "2025H1", "2025H2"]);
    // 逆順に選ばれても同じ（UIの from > until）
    assert.deepEqual(periodsInYearRange(all, 2025, 2024),
                     ["2024H1", "2024H2", "2025H1", "2025H2"]);
    assert.deepEqual(yearsOfPeriods(all), [2024, 2025, 2026]);
  });
});
