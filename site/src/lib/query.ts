/**
 * 検索の SQL 組み立てと、年をまたぐページ送りの計算。
 *
 * **ここには sql.js-httpvfs を持ち込まない。** DBとの通信は `db.ts` の仕事で、
 * この層は「どんな SQL とパラメータを投げるか」「返ってきた行をどう均すか」
 * だけを扱う純粋な関数にしてある。ブラウザ無しで動くので、
 * `site/test/` から直接呼んで検証できる（実際にそこで壊れた履歴がある）。
 *
 * SQL は `docs/PHASE1_PROTOTYPE.md` の実測に縛られている。**書き換える前に読むこと。**
 *
 *   1. 「新しい順」は必ず `ORDER BY rowid DESC`。`ORDER BY date DESC` にすると
 *      一時B-TREEができてヒット全件を読みに行く（検索で204MB転送）
 *   2. ページ送りは OFFSET ではなく rowid の keyset
 *      （OFFSET 80 で134リクエスト・4.1秒。keyset なら何ページ目でも同じコスト）
 */

export interface SpeechRow {
  speech_id: string;
  date: string;
  speaker: string;
  speaker_group: string | null;
  speaker_position: string | null;
  politician_id: number | null;
  issue_id: string;
  speech_order: number;
  meeting: string;
  house: string;
  /** 検索語の周辺。FTS 経由なら snippet()、争点語経由なら substr() で作る */
  snippet: string;
  /** snippet が SQLite の snippet() 由来か（＝マーカーが入っているか） */
  marked: boolean;
  rowid: number;
  year: number;
}

export interface SearchOptions {
  /** FTS に投げる語。争点語の場合は topicId を使うのでこちらは空 */
  query?: string;
  topicId?: number;
  politicianId?: number;
  meetingName?: string;
  years?: number[];
  limit?: number;
  /** 年 → その年で「ここより前」を続きとして読む rowid。keyset ページング用 */
  before?: Record<number, number>;
  /**
   * `resolveQuery()` の結果。**1回だけ解いて、ページ送りでは使い回す。**
   * 省略すると FTS 扱いになる（＝2文字語は引けない）ので、
   * 任意語の検索では必ず渡すこと。
   */
  plan?: QueryPlan;
}

export interface SearchPage {
  rows: SpeechRow[];
  /** 次ページ用のカーソル。空なら打ち止め */
  before: Record<number, number>;
  done: boolean;
}

/**
 * 検索語をどの索引で引くか。
 *
 *   fts  : 全部3文字以上。FTS5 trigram（1.5〜6秒）
 *   word : 2文字以下の語がある。`word_hit` を引く
 *   none : 2文字以下の語が語彙に無い。**引けない**ので、代わりの案を出す
 */
export type QueryPlan =
  | { mode: "fts"; match: string }
  | { mode: "word"; driver: string; filters: string[] }
  | { mode: "none"; unsupported: string[] };

// --- 検索語 ---------------------------------------------------------------

/**
 * 検索語の英数字を全角に寄せる。**会議録の英数字は全部全角で、半角で打つと0件になる**
 * （`AI` 0件 / `ＡＩ` 5,394件、`LGBT` 0件 / `ＬＧＢＴ` 811件）。
 *
 * 実測（`data/kokkai.db` 全件）: 議員の発言 512,247件のうち**半角英字を含むのは
 * 54件（0.01%）**しかなく、中身は URL と英語の引用だった。**寄せて失うものは無い。**
 * 半角数字は 0.9% の発言にあるが（箇条書きの番号）、数字だけの語で引く場面は稀。
 *
 * **★ NFKC を使わないこと。** NFKC は全角→半角に潰す**逆方向**の正規化で、
 * 使うと全滅する。ここでやるのは `A-Za-z0-9` を +0xFEE0 する片方向の写像だけ。
 *
 * **大文字小文字は寄せない。** FTS5 の trigram は全角ラテンも畳むので幅だけ揃えれば足りる
 * （実測: `ｌｇｂｔ` も `ＬＧＢＴ` も 55件・`data/dist/kokkai-2025.db`）。
 * 逆に大文字へ寄せると `ｉＰＳ` `ＳＤＧｓ` `ＩｏＴ`（全角ラテンの3.5%）が引けなくなる。
 */
export function toFullWidth(input: string): string {
  return input.replace(/[A-Za-z0-9]/g, (c) =>
    String.fromCharCode(c.charCodeAt(0) + 0xfee0));
}

/**
 * 検索語を空白で割る。**全角化はここを通す**ので、FTS・争点語・2文字語の
 * 3経路とも（`toMatchExpr` / `resolveQuery` 経由で）同じ正規化を受ける。
 * ハイライトに渡す語もここから取ること。半角のままだと本文に当たらない。
 */
export function splitTerms(input: string): string[] {
  return toFullWidth(input).trim().split(/[\s　]+/).filter(Boolean);
}

/** trigram にそのまま渡すと記号が演算子として解釈されるので、フレーズとして囲む。 */
export function toMatchExpr(input: string): string {
  const words = splitTerms(input);
  // 二重引用符はフレーズの区切りなので、FTS5 の作法どおり2つ重ねて逃がす
  return words.map((w) => `"${w.replace(/"/g, '""')}"`).join(" AND ");
}

/** speech_id は `<issue_id>_<連番>` で、issue_id の末尾8桁が日付。年DBの選択に使う。 */
export function yearOfSpeechId(speechId: string): number | null {
  const m = /^(.+?)_(\d+)$/.exec(speechId);
  const year = Number(m?.[1].slice(-8, -4));
  return Number.isInteger(year) && year > 1900 ? year : null;
}

// --- 結果を取る SQL -------------------------------------------------------

/** 検索結果と議員ページで共通に要る列。meeting を JOIN するのは会議名を出すため。
 *  meeting は1年で約1,100行（150KB程度）しかなく、最初の数件でワーカのページ
 *  キャッシュに乗るので、追加のリクエストはほぼ初回だけで済む。 */
const RESULT_COLS = `
  s.speech_id, s.date, s.speaker, s.speaker_group, s.speaker_position,
  s.politician_id, s.issue_id, s.speech_order, s.rowid AS rowid,
  m.name AS meeting, m.house AS house`;

interface Shape { politician: boolean; meeting: boolean; before: boolean }

/** 全文検索。**`ORDER BY f.rowid DESC`**（＝日付の降順）。 */
function ftsSql(o: Shape) {
  return `
    SELECT ${RESULT_COLS},
           snippet(speech_fts, 0, char(1), char(2), '…', 24) AS snippet
    FROM speech_fts f
    JOIN speech s ON s.rowid = f.rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE speech_fts MATCH ?
      ${o.before ? "AND f.rowid < ?" : ""}
      ${o.politician ? "AND s.politician_id = ?" : ""}
      ${o.meeting ? "AND m.name = ?" : ""}
    ORDER BY f.rowid DESC LIMIT ?`;
}

/**
 * 争点語での検索。FTS を通さず `topic_hit` を引く。
 * 2文字語（憲法・年金）は FTS では**原理的に引けない**し、引ける語でも 3.3倍速い。
 *
 * snippet() が使えないので `instr()` で語の位置を求めて周辺を切り出す。
 * 別表記でヒットした発言では instr が 0 を返すが、その場合は先頭から出る。
 */
function topicSql(o: Shape) {
  return `
    SELECT ${RESULT_COLS},
           substr(s.body, max(1, instr(s.body, t.term) - 40), 160) AS snippet
    FROM topic_hit h
    JOIN topic t ON t.id = h.topic_id
    JOIN speech s ON s.rowid = h.speech_rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE h.topic_id = ?
      ${o.before ? "AND h.speech_rowid < ?" : ""}
      ${o.politician ? "AND s.politician_id = ?" : ""}
      ${o.meeting ? "AND m.name = ?" : ""}
    ORDER BY h.speech_rowid DESC LIMIT ?`;
}

/**
 * 2文字語の検索。FTS5 の trigram は3文字未満のトークンを作れないので、
 * 「増税」「憲法」「年金」「原発」は**原理的に FTS では引けない**。
 * `word` / `word_hit`（機械抽出の語彙、`scripts/build_words.py`）を引く。
 *
 * 複数語のときは**いちばん珍しい2文字語を起点**にして、残りは `instr()` で絞る。
 * 走査する行数が起点の語の件数で頭打ちになるので、起点の選び方が効く。
 */
function wordSql(o: Shape & { filters: number }) {
  return `
    SELECT ${RESULT_COLS},
           substr(s.body, max(1, instr(s.body, ?) - 40), 160) AS snippet
    FROM word w
    JOIN word_hit h ON h.word_id = w.id
    JOIN speech s ON s.rowid = h.speech_rowid
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE w.term = ?
      ${o.before ? "AND h.speech_rowid < ?" : ""}
      ${"AND instr(s.body, ?) > 0 ".repeat(o.filters)}
      ${o.politician ? "AND s.politician_id = ?" : ""}
      ${o.meeting ? "AND m.name = ?" : ""}
    ORDER BY h.speech_rowid DESC LIMIT ?`;
}

/**
 * 1年ぶんの検索 SQL とパラメータ。`cursor` はその年の「ここより前の rowid」。
 *
 * **`countQuery()` と対で直すこと。** 絞り込みを片方だけに足すと、
 * 一覧の中身と画面上部の件数が黙って食い違う。
 */
export function searchQuery(
  opts: SearchOptions, plan: QueryPlan | undefined, cursor: number | undefined,
): [string, unknown[]] {
  const shape: Shape = {
    politician: opts.politicianId != null,
    meeting: !!opts.meetingName,
    before: cursor != null,
  };
  const params: unknown[] = [];
  let sql: string;

  if (opts.topicId != null) {
    sql = topicSql(shape);
    params.push(opts.topicId);
    if (cursor != null) params.push(cursor);
  } else if (plan?.mode === "word") {
    sql = wordSql({ ...shape, filters: plan.filters.length });
    params.push(plan.driver, plan.driver);       // 1つ目は snippet を切り出す位置用
    if (cursor != null) params.push(cursor);
    params.push(...plan.filters);
  } else {
    sql = ftsSql(shape);
    params.push(plan?.mode === "fts" ? plan.match : toMatchExpr(opts.query ?? ""));
    if (cursor != null) params.push(cursor);
  }

  if (shape.politician) params.push(opts.politicianId);
  if (shape.meeting) params.push(opts.meetingName);
  params.push(opts.limit ?? 20);
  return [sql, params];
}

/** 議員の発言タイムライン（キーワード無し）。
 *  **`speech(politician_id)` だけの索引で `ORDER BY rowid DESC` を降順スキャンさせる。**
 *  索引に date を足すと一時B-TREEに落ちて 27 → 509 リクエストになる。 */
export function timelineQuery(
  politicianId: number, cursor: number | undefined, limit: number,
): [string, unknown[]] {
  return [`
    SELECT ${RESULT_COLS}, substr(s.body, 1, 160) AS snippet
    FROM speech s
    JOIN meeting m ON m.issue_id = s.issue_id
    WHERE s.politician_id = ? ${cursor != null ? "AND s.rowid < ?" : ""}
    ORDER BY s.rowid DESC LIMIT ?`,
    cursor != null ? [politicianId, cursor, limit] : [politicianId, limit]];
}

// --- 件数の SQL -----------------------------------------------------------

/**
 * 件数の SQL とパラメータ。絞り込みが無ければ索引だけで数えられる。
 *
 * **`searchQuery()` と同じ条件を必ず全部付けること。** 件数は結果取得とは
 * 別のSQLなので、片方だけ絞ると一覧と画面上部の件数が食い違う
 * （実際に会議名の絞り込みが件数に効いておらず、2,594件と557件が入れ替わっていた）。
 */
export function countQuery(
  opts: SearchOptions, plan: QueryPlan | undefined,
): [string, unknown[]] {
  const byPolitician = opts.politicianId != null;
  const byMeeting = !!opts.meetingName;
  const plain = !byPolitician && !byMeeting;

  // speech を JOIN 済みの問い合わせに足す分。順番はパラメータと揃える
  const joinMeeting = byMeeting ? "JOIN meeting m ON m.issue_id = s.issue_id" : "";
  const filterSql = (byPolitician ? "AND s.politician_id = ? " : "")
                  + (byMeeting ? "AND m.name = ? " : "");
  const filterParams: unknown[] = [
    ...(byPolitician ? [opts.politicianId] : []),
    ...(byMeeting ? [opts.meetingName] : []),
  ];

  if (opts.topicId != null) {
    return plain
      ? ["SELECT COUNT(*) AS n FROM topic_hit WHERE topic_id = ?", [opts.topicId]]
      : [`SELECT COUNT(*) AS n FROM topic_hit h
          JOIN speech s ON s.rowid = h.speech_rowid ${joinMeeting}
          WHERE h.topic_id = ? ${filterSql}`, [opts.topicId, ...filterParams]];
  }

  if (plan?.mode === "word") {
    // 絞り込みが何も無ければ word.n_speeches に答えが入っている（1行読むだけ）
    if (!plan.filters.length && plain) {
      return ["SELECT n_speeches AS n FROM word WHERE term = ?", [plan.driver]];
    }
    return [`SELECT COUNT(*) AS n FROM word w
             JOIN word_hit h ON h.word_id = w.id
             JOIN speech s ON s.rowid = h.speech_rowid ${joinMeeting}
             WHERE w.term = ?
               ${"AND instr(s.body, ?) > 0 ".repeat(plan.filters.length)}
               ${filterSql}`,
            [plan.driver, ...plan.filters, ...filterParams]];
  }

  const match = plan?.mode === "fts" ? plan.match : toMatchExpr(opts.query ?? "");
  return plain
    ? ["SELECT COUNT(*) AS n FROM speech_fts WHERE speech_fts MATCH ?", [match]]
    : [`SELECT COUNT(*) AS n FROM speech_fts f
        JOIN speech s ON s.rowid = f.rowid ${joinMeeting}
        WHERE speech_fts MATCH ? ${filterSql}`, [match, ...filterParams]];
}

// --- 年をまたぐページ送り -------------------------------------------------

/**
 * 年ごとの結果を全体の「新しい順」に均し、次ページのカーソルを決める。
 *
 * 年DBは日付で綺麗に分かれている（build_db.py が年で分ける）ので、新しい年から
 * 順に並べるだけで全体が日付の降順になる。マージソートは要らない。
 *
 * **ただし年ごとに LIMIT を掛けているので、そのまま連結してはいけない。**
 * 6年ぶんなら 20件のつもりが 120件返るうえ、「2026年の21件目」を飛ばして
 * 2025年へ進んでしまう。次ページで飛ばした分を後ろに足すと、画面上の
 * 「新しい順」がそこで崩れる。全体で limit 件に切り、**出さなかった年は
 * カーソルを進めない**（次ページで同じところから引き直す。読んだページは
 * ワーカのキャッシュに残っているので安い）。
 *
 * カーソルは年ごとに「ここより前の rowid」。LIMIT に満たなかった年は読み切りなので
 * 0 を入れ、次から問い合わせ自体をしない。OFFSET を使わないのは、5ページ目が
 * 134リクエスト・4.1秒になるため（`docs/PHASE1_PROTOTYPE.md` §4）。
 *
 * @param years   新しい順に並んだ対象年
 * @param perYear years と同じ並びの、年ごとの取得結果（それぞれ最大 limit 件）
 */
export function mergePages(
  years: number[], perYear: SpeechRow[][], limit: number, before: Record<number, number>,
): SearchPage {
  const rows: SpeechRow[] = [];
  const next: Record<number, number> = {};

  years.forEach((year, i) => {
    const got = perYear[i];
    if (before[year] === 0 || !got.length) { next[year] = 0; return; }  // その年は読み切り

    const take = got.slice(0, limit - rows.length);
    if (!take.length) {
      // 枠が尽きて1件も出せなかった年。**カーソルを進めない**（次ページで同じ続きから）
      if (year in before) next[year] = before[year];
      return;
    }
    rows.push(...take);
    // 出し切っていて、かつ LIMIT にも届いていなければ、その年はもう無い
    next[year] = take.length === got.length && got.length < limit ? 0 : take[take.length - 1].rowid;
  });

  return { rows, before: next, done: years.every((y) => next[y] === 0) };
}
