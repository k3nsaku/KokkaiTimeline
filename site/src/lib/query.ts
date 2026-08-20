/**
 * 検索の SQL 組み立てと、期間をまたぐページ送りの計算。
 *
 * **ここには sql.js-httpvfs を持ち込まない。** DBとの通信は `db.ts` の仕事で、
 * この層は「どんな SQL とパラメータを投げるか」「返ってきた行をどう均すか」
 * だけを扱う純粋な関数にしてある。ブラウザ無しで動くので、
 * `site/test/` から直接呼んで検証できる（実際にそこで壊れた履歴がある）。
 *
 * SQL は `docs/DECISIONS.md` の実測に縛られている。**書き換える前に読むこと。**
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
  /** 対象の期間ID（`2026H1`）。省略すると目録にある全期間 */
  periods?: string[];
  limit?: number;
  /** 期間ID → その期間で「ここより前」を続きとして読む rowid。keyset ページング用 */
  before?: Record<string, number>;
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
  before: Record<string, number>;
  done: boolean;
}

/**
 * 検索語をどの索引で引くか。
 *
 *   fts  : 全部3文字以上。FTS5 trigram（1.5〜6秒）
 *   word : 2文字以下の語がある。`word_hit` を引く
 *
 * **「引けない語」という状態は無い。** 2文字語の索引は本文の2文字窓を全部持つので
 * （`scripts/build_db.py` の `build_word_index`）、索引に無い語は素直に0件になる。
 * 以前は語彙リストに載っている語しか引けず、`mode: "none"` で代わりの案を出していた。
 */
export type QueryPlan =
  | { mode: "fts"; match: string }
  /** driver / filters とも、2文字以下の語は `toWordKey()` で畳んだ形が入る
   *  （索引も本文もそれで引ける）。3文字以上の語は打たれたまま。 */
  | { mode: "word"; driver: string; filters: string[] };

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
 * 2文字語の語彙（`word.term`）を引くためのキー。**全角ラテンだけ大文字に畳む。**
 *
 * **ここは `toFullWidth()` と役割が違う。** 全角化は3経路すべてに掛けるが、
 * 大文字化は**2文字語の経路だけ**に掛ける。理由:
 *
 *   - FTS 経路は畳んではいけない。FTS5 の trigram が自分で大小を畳むうえ、
 *     畳むと `ＳＤＧｓ` `ｉＰＳ` `ＩｏＴ` が引けなくなる（`docs/DECISIONS.md`）
 *   - word 経路は畳まないといけない。`w.term = ?` は BINARY 比較で、SQLite は
 *     畳んでくれない（NOCASE は ASCII 限定で全角に効かない）。`ai` と打たれると
 *     `ａｉ` になり、語彙の `ＡＩ` に当たらず「引けない」と出る
 *
 * **3文字以上の語には掛けないこと**（`ＳＤＧｓ` が `ＳＤＧＳ` になって本文に当たらない）。
 * 索引側は `scripts/build_db.py` の `fold_word_run()` が同じ写像を掛けてある。
 */
export function toWordKey(term: string): string {
  return term.replace(/[ａ-ｚ]/g, (c) =>
    String.fromCharCode(c.charCodeAt(0) - 0x20));
}

/**
 * 画面・入力欄・URL に出す形。**実際に引いた語に寄せる。**
 *
 * 全角化（`toFullWidth`）に加えて、**2文字以下の語だけ**大文字に畳む。
 * `g7` と打つと索引は `Ｇ７` を引くので、`ｇ７` と見せると嘘になるうえ、
 * `?q=ｇ７` と `?q=Ｇ７` が同じ検索の別URLになる（`docs/DECISIONS.md` の「画面とURLは実際に引いた語に寄せる」）。
 *
 * **3文字以上には掛けない。** `ＳＤＧｓ` を `ＳＤＧＳ` と書き換えて見せる筋合いは無い
 * （FTS は畳んで引くので、見せる側で寄せる必要も無い）。
 * 空白は打たれたまま残す（畳むと、ただ2つ空けただけで但し書きが出る）。
 */
export function canonicalQuery(input: string): string {
  return toFullWidth(input).replace(/[^\s　]+/g, (t) => (t.length < 3 ? toWordKey(t) : t));
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
  return phraseAnd(splitTerms(input));
}

function phraseAnd(words: string[]): string {
  // 二重引用符はフレーズの区切りなので、FTS5 の作法どおり2つ重ねて逃がす
  return words.map((w) => `"${w.replace(/"/g, '""')}"`).join(" AND ");
}

/**
 * 索引（`word` テーブル）に有るか問い合わせるキー。**2文字以下の語だけ**を畳んで返す。
 * 空なら全部3文字以上＝FTS で引けるので、索引を引く必要そのものが無い。
 */
export function wordProbeKeys(terms: string[]): string[] {
  return terms.filter((t) => t.length < 3).map(toWordKey);
}

// 2文字語の索引が見ている文字クラス。**`build_db.py` の `WORD_RUN_PATTERN` と同じ区切り。**
// 索引は「同じクラスの連続」の中を2文字ずつ切るので、**クラスをまたぐ2文字は入らない**。
const CHAR_CLASSES = [/^[一-鿿々]$/, /^[ァ-ヴー]$/, /^[Ａ-Ｚａ-ｚ０-９]$/];

function classOf(ch: string): number {
  return CHAR_CLASSES.findIndex((re) => re.test(ch));
}

/**
 * **仕様上どうやっても0件になる語**を返す（打たれたままの形で）。
 *
 * 索引に「入っていない」ではなく「入りようがない」ものだけを挙げる。DBを引かずに
 * 判定できるので、**問い合わせる前に画面で説明できる**。3文字以上は FTS が
 * 文字種を問わず拾うので、ここに来るのは2文字以下だけ:
 *
 *   - **1文字**（索引の項はちょうど2文字なので当たらない）
 *   - **文字種をまたぐ2文字**（`踏ま` `お金`）。索引は漢字・カタカナ・全角英数の
 *     **連続の中**しか切らないため。`治体`（自治体の中）が引けるのに `踏ま` が
 *     引けないのはこれで、語の区切りとは関係がない
 *   - **ひらがなだけの2文字**（`ため`）。ひらがなはどのクラスにも入っていない
 *
 * ここを緩めると索引が急に太る（実測: 文字種のまたぎを許すだけで**2.9倍**・
 * 最大ファイルが 377MB → 約487MB で 512MB の上限に張り付く）。`docs/DECISIONS.md`。
 */
export function unsearchableTerms(terms: string[]): string[] {
  return terms.filter((t) => {
    if (t.length >= 3) return false;
    if (t.length === 1) return true;
    const first = classOf(t[0]);
    return first < 0 || first !== classOf(t[1]);
  });
}

/**
 * 索引の引き当て結果から `QueryPlan` を組む。**DBに触らない**ので `site/test/` から検証できる。
 *
 * @param terms  `splitTerms()` の結果（打たれたまま・全角化済み）
 * @param counts `wordProbeKeys()` で引けた語 → 対象期間の合計件数。
 *               **どの期間にも無い語は入っていない**（＝0件）
 */
export function wordPlan(terms: string[], counts: Map<string, number>): QueryPlan {
  // 2文字以下だけ畳む。**3文字以上に掛けてはいけない**（`ＳＤＧｓ` → `ＳＤＧＳ` で本文に当たらない）
  const keys = terms.map((t) => (t.length < 3 ? toWordKey(t) : t));
  const shortAt = terms.map((_, i) => i).filter((i) => terms[i].length < 3);
  // 語が無い（空の検索）ときは、当たりようのない driver を返して0件にする。
  // `MATCH ''` は FTS5 の構文エラーになるので、FTS 経路に流してはいけない
  if (!terms.length) return { mode: "word", driver: "", filters: [] };
  if (!shortAt.length) return { mode: "fts", match: phraseAnd(terms) };

  // いちばん珍しい2文字語を起点にする。走査する行数がこれで決まる。
  // **どこにも無い語（count 0）はそのまま起点になる** ＝ 全期間で0件。
  // これでよい: 索引は本文の2文字窓を全部持っているので、無い＝本当に出てこない。
  //
  // **残りは添字で外す。** 畳むと元の語と別物になりうるので、値で比較してはいけない
  // （`ai 増税` の driver は `ＡＩ`。`ａｉ` が filters に残ると instr が 0 を返して全滅する）
  const at = (i: number) => counts.get(keys[i]) ?? 0;
  const driverAt = shortAt.reduce((best, i) => (at(i) < at(best) ? i : best));
  return { mode: "word", driver: keys[driverAt], filters: keys.filter((_, i) => i !== driverAt) };
}

// --- 期間（配信DBの分割単位）----------------------------------------------

/** 目録が持つ分割規則。`scripts/build_db.py` の `--period` と同じ語。 */
export type PeriodRule = "half" | "year";

/**
 * 日付（`YYYY-MM-DD`）→ 期間ID。**`scripts/build_db.py` の `period_of()` と同じ写像。**
 * 片方だけ変えると、存在しないファイルを引きに行って検索が丸ごと止まる。
 *
 * 半期にしているのは、1ファイルが 512MB を超えると**黙って CDN キャッシュから
 * 外れる**ため（RTT 8ms → 77ms）。満年は実測 368〜419MB で余裕が無い。
 *
 * **期間は必ず年に閉じている。** だから利用者に見せる絞り込みは「年」のままにできる
 * （1年＝2ファイル、日付の取りこぼしなし）。
 */
export function periodOf(date: string, rule: PeriodRule = "half"): string {
  const year = date.slice(0, 4);
  if (rule === "year") return year;
  return `${year}H${date.slice(5, 7) <= "06" ? "1" : "2"}`;
}

/** 期間ID → 年。期間は年に閉じているので先頭4文字でよい。 */
export function yearOfPeriod(period: string): number {
  return Number(period.slice(0, 4));
}

/** 目録の期間IDから、絞り込みに出す年の一覧を作る（古い順）。 */
export function yearsOfPeriods(periods: string[]): number[] {
  return [...new Set(periods.map(yearOfPeriod))].sort((a, b) => a - b);
}

/**
 * 年の範囲（両端を含む）に重なる期間ID。
 *
 * **利用者に見せる絞り込みは年のまま**で、ここでファイルに直す。
 * 期間が年に閉じている（`periodOf` の注記）ので、この写像で日付の取りこぼしが出ない。
 * 会期で割るとここが成立しない——年をまたぐ会期があるため、
 * 「2023年だけ」を選べなくなる。
 */
export function periodsInYearRange(periods: string[], from: number, until: number): string[] {
  const [lo, hi] = from <= until ? [from, until] : [until, from];
  return periods.filter((p) => yearOfPeriod(p) >= lo && yearOfPeriod(p) <= hi);
}

/** `YYYYMMDD` を `YYYY-MM-DD` に。issue_id / speech_id から日付を取り出すため。 */
function dashed(compact: string): string {
  return `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)}`;
}

/** issue_id は末尾8桁が日付。期間DBの選択に使う。 */
export function periodOfIssueId(issueId: string, rule: PeriodRule = "half"): string | null {
  const date = dashed(issueId.slice(-8));
  return Number(date.slice(0, 4)) > 1900 ? periodOf(date, rule) : null;
}

/** speech_id は `<issue_id>_<連番>`。期間DBの選択に使う。 */
export function periodOfSpeechId(speechId: string, rule: PeriodRule = "half"): string | null {
  const m = /^(.+?)_(\d+)$/.exec(speechId);
  return m ? periodOfIssueId(m[1], rule) : null;
}

/** speech_id の年。**表示の見出し用**（引き先の決定には期間IDを使う）。 */
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
 * `word` / `word_hit`（本文の2文字窓を全部入れた索引、`scripts/build_db.py`）を引く。
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

// --- 件数と月別の SQL -----------------------------------------------------
//
// **ヒットの集合（FROM と WHERE）は `hitSource()` 1か所で組む。** 件数と月別は
// そこに `COUNT(*)` を被せるか、月のバケットで割るかの違いしかない。
// 3経路 × 絞り込みの組み合わせを2か所に書くと、片方だけ直したときに
// 一覧と件数が黙って食い違う（実際にそうなった。docs/PITFALLS.md）。

/** ヒットの集合。`from` と `where` に `SELECT` を被せて使う。 */
interface HitSource {
  from: string;
  where: string;
  params: unknown[];
  /** 発言の rowid を指す式。月のバケットはこれを範囲で割る */
  rowid: string;
}

/**
 * 検索条件 → ヒットの集合（FROM と WHERE）。
 *
 * **`searchQuery()` と同じ条件を必ず全部付けること。** 結果取得とは別のSQLなので、
 * 片方だけ絞ると一覧と画面上部の件数が食い違う
 * （実際に会議名の絞り込みが件数に効いておらず、2,594件と557件が入れ替わっていた）。
 */
function hitSource(opts: SearchOptions, plan: QueryPlan | undefined): HitSource {
  const byPolitician = opts.politicianId != null;
  const byMeeting = !!opts.meetingName;
  // 議員で絞るなら speech が要る。**meeting は speech 経由でしか繋がらない**
  const needsSpeech = byPolitician || byMeeting;

  const joinMeeting = byMeeting ? " JOIN meeting m ON m.issue_id = s.issue_id" : "";
  const filterSql = (byPolitician ? " AND s.politician_id = ?" : "")
                  + (byMeeting ? " AND m.name = ?" : "");
  const filterParams: unknown[] = [
    ...(byPolitician ? [opts.politicianId] : []),
    ...(byMeeting ? [opts.meetingName] : []),
  ];

  if (opts.topicId != null) {
    return {
      from: "FROM topic_hit h"
        + (needsSpeech ? " JOIN speech s ON s.rowid = h.speech_rowid" : "") + joinMeeting,
      where: `h.topic_id = ?${filterSql}`,
      params: [opts.topicId, ...filterParams],
      rowid: "h.speech_rowid",
    };
  }

  if (plan?.mode === "word") {
    // 絞り込みの語は本文を見るので speech が要る（instr）
    const withBody = plan.filters.length > 0;
    return {
      from: "FROM word w JOIN word_hit h ON h.word_id = w.id"
        + (withBody || needsSpeech ? " JOIN speech s ON s.rowid = h.speech_rowid" : "")
        + joinMeeting,
      where: "w.term = ?"
        + " AND instr(s.body, ?) > 0".repeat(plan.filters.length) + filterSql,
      params: [plan.driver, ...plan.filters, ...filterParams],
      rowid: "h.speech_rowid",
    };
  }

  const match = plan?.mode === "fts" ? plan.match : toMatchExpr(opts.query ?? "");
  return {
    from: "FROM speech_fts f"
      + (needsSpeech ? " JOIN speech s ON s.rowid = f.rowid" : "") + joinMeeting,
    where: `speech_fts MATCH ?${filterSql}`,
    params: [match, ...filterParams],
    rowid: "f.rowid",
  };
}

/**
 * 件数の SQL とパラメータ。
 *
 * **絞り込みが無いときは索引の1行で答えが出る**ので、そこだけ別扱いにしてある
 * （2文字語は `word.n_speeches`、争点語は `topic_hit` の索引だけを数える）。
 * 月別（`monthlyQuery`）にはこの近道が無い——月ごとに分けるには、
 * どのみち当たった発言を1件ずつ数え直すしかない。
 */
export function countQuery(
  opts: SearchOptions, plan: QueryPlan | undefined,
): [string, unknown[]] {
  const plain = opts.politicianId == null && !opts.meetingName;

  if (plain && opts.topicId != null) {
    return ["SELECT COUNT(*) AS n FROM topic_hit WHERE topic_id = ?", [opts.topicId]];
  }
  // 絞り込みが何も無ければ word.n_speeches に答えが入っている（1行読むだけ）
  if (plain && plan?.mode === "word" && !plan.filters.length) {
    return ["SELECT n_speeches AS n FROM word WHERE term = ?", [plan.driver]];
  }

  const hits = hitSource(opts, plan);
  return [`SELECT COUNT(*) AS n ${hits.from} WHERE ${hits.where}`, hits.params];
}

// --- 月別の件数 -----------------------------------------------------------

/**
 * 月ごとの件数の SQL。**日付では絶対に GROUP BY しない。**
 *
 * `substr(s.date, 1, 7)` でまとめると、当たった発言の**行を1件ずつ読みに行く**。
 * 実測（`data/dist/kokkai-2025H1.db`・ローカル）で `安全保障` が 1.0ms → 170.6ms、
 * `風力発電` が 0.4ms → 26.4ms。HTTP Range 越しなら「ヒット件数ぶんの
 * ランダム読み」そのもので、`ORDER BY date DESC` と同じ穴に落ちる
 * （docs/PITFALLS.md）。
 *
 * 代わりに **`speech.rowid` が日付の昇順**であること（`build_db.py` の `load()`）を
 * 使って、rowid の範囲でバケットに割る。索引（FTS の docid リスト・`word_hit`・
 * `topic_hit`）だけで済み、**件数を数えるのと同じ手間で月別が出る。**
 *
 * @param bounds 月ごとの先頭 rowid。`monthBoundsQuery()` で引いたもの。
 *               返る `b` は `bounds` の添字（＝月の添字）。
 */
export function monthlyQuery(
  opts: SearchOptions, plan: QueryPlan | undefined, bounds: number[],
): [string, unknown[]] {
  const hits = hitSource(opts, plan);
  // 先頭の月に下限は要らない（それより前の発言はこの期間DBに無い）
  const cuts = bounds.slice(1);
  const bucket = cuts.length
    ? `CASE${cuts.map((_, i) => ` WHEN ${hits.rowid} < ? THEN ${i}`).join("")} ELSE ${cuts.length} END`
    : "0";
  return [
    `SELECT ${bucket} AS b, COUNT(*) AS n ${hits.from} WHERE ${hits.where} GROUP BY b`,
    // ★ CASE は SELECT 句にあるので、**バケットの境界が先に束縛される**
    [...cuts, ...hits.params],
  ];
}

/**
 * 月ごとの先頭 rowid を引く SQL。**`idx_speech_date` の seek を月数ぶん並べるだけ。**
 *
 * `MIN(rowid) ... GROUP BY substr(date, 1, 7)` は全走査になるので使わない。
 * 索引は `(date, rowid)` の順に並んでいるので、「その月の1日以降で最初の1件」が
 * そのままその月の先頭 rowid になる（実測でも `GROUP BY` の結果と一致する）。
 * covering index だけで済み、`speech` の行は1件も読まない。
 *
 * その月に発言が無ければ、返るのは**次に発言があった月の先頭**（＝幅0のバケット）。
 * どの月にも無ければ NULL が返る（`fillBounds()` が埋める）。
 */
export function monthBoundsQuery(months: string[]): [string, unknown[]] {
  const sql = months.map((_, i) =>
    `SELECT ${i} AS i, (SELECT rowid FROM speech WHERE date >= ? ORDER BY date LIMIT 1) AS at`)
    .join(" UNION ALL ");
  return [sql, months.map((m) => `${m}-01`)];
}

/** 月の数だけ並んでいない・NULL が混じっている境界を埋める。
 *  NULL は「その月以降に発言が無い」なので、**当たりようのない大きな値**にする
 *  （バケットの幅が0になるだけで、前の月の集計は壊れない）。 */
export function fillBounds(raw: (number | null | undefined)[], count: number): number[] {
  const out: number[] = [];
  for (let i = 0; i < count; i++) {
    const at = raw[i];
    out.push(typeof at === "number" ? at : Number.MAX_SAFE_INTEGER);
  }
  return out;
}

/**
 * 期間DBに入っている月（`YYYY-MM`・古い順）。
 *
 * 期間IDから月の幅を出し、目録の収録範囲（`from` / `to`）で詰める。
 * **無い月まで並べると、そのぶん seek が空振りする**だけなので詰めておく。
 */
export function monthsInPeriod(period: string, from?: string, to?: string): string[] {
  const year = yearOfPeriod(period);
  const half = period.slice(4);
  const first = half === "H2" ? 7 : 1;
  const last = half === "H1" ? 6 : 12;

  const months: string[] = [];
  for (let m = first; m <= last; m++) {
    const key = `${year}-${String(m).padStart(2, "0")}`;
    if (from && key < from.slice(0, 7)) continue;
    if (to && key > to.slice(0, 7)) continue;
    months.push(key);
  }
  return months;
}

// --- 期間をまたぐページ送り -----------------------------------------------

/**
 * 期間ごとの結果を全体の「新しい順」に均し、次ページのカーソルを決める。
 *
 * 期間DBは日付で綺麗に分かれている（build_db.py が期間で分ける）ので、新しい期間から
 * 順に並べるだけで全体が日付の降順になる。マージソートは要らない。
 * **この前提は分割単位を変えても崩してはいけない**（会期で割ると日付が重なりうる）。
 *
 * **ただし期間ごとに LIMIT を掛けているので、そのまま連結してはいけない。**
 * 12期間ぶんなら 20件のつもりが 240件返るうえ、「2026H1の21件目」を飛ばして
 * 2025H2へ進んでしまう。次ページで飛ばした分を後ろに足すと、画面上の
 * 「新しい順」がそこで崩れる。全体で limit 件に切り、**出さなかった期間は
 * カーソルを進めない**（次ページで同じところから引き直す。読んだページは
 * ワーカのキャッシュに残っているので安い）。
 *
 * カーソルは期間ごとに「ここより前の rowid」。LIMIT に満たなかった期間は読み切りなので
 * 0 を入れ、次から問い合わせ自体をしない。OFFSET を使わないのは、5ページ目が
 * 134リクエスト・4.1秒になるため（`docs/DECISIONS.md`）。
 *
 * @param periods   新しい順に並んだ対象期間
 * @param perPeriod periods と同じ並びの、期間ごとの取得結果（それぞれ最大 limit 件）
 */
export function mergePages(
  periods: string[], perPeriod: SpeechRow[][], limit: number, before: Record<string, number>,
): SearchPage {
  const rows: SpeechRow[] = [];
  const next: Record<string, number> = {};

  periods.forEach((period, i) => {
    const got = perPeriod[i];
    if (before[period] === 0 || !got.length) { next[period] = 0; return; }  // 読み切り

    const take = got.slice(0, limit - rows.length);
    if (!take.length) {
      // 枠が尽きて1件も出せなかった期間。**カーソルを進めない**（次ページで同じ続きから）
      if (period in before) next[period] = before[period];
      return;
    }
    rows.push(...take);
    // 出し切っていて、かつ LIMIT にも届いていなければ、その期間はもう無い
    next[period] = take.length === got.length && got.length < limit
      ? 0 : take[take.length - 1].rowid;
  });

  return { rows, before: next, done: periods.every((p) => next[p] === 0) };
}
