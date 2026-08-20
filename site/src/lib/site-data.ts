/**
 * ビルド時にだけ読むデータ。`data/` を直接読む（サイトへコピーしない）。
 *
 * 議員マスタと争点語は年DBの中にも入っているが、**ページの事前生成に使う分は
 * ここから読む**。議員1,111人・争点語79件のページを静的HTMLとして出しておくと、
 * 議員名で検索されたときに中身のあるページが返る。発言そのものは
 * ブラウザがDBから引く（650,785件は事前生成できない）。
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DATA = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "data");

function readJson<T>(...rel: string[]): T {
  return JSON.parse(readFileSync(path.join(DATA, ...rel), "utf-8")) as T;
}

export interface Affiliation {
  kaiha: string | null;
  /** 会派から特定できたときだけ入る。**NULL は「不明」で `無所属` とは意味が違う** */
  party: string | null;
  party_unresolved: string | null;
  start_date: string;
  end_date: string;
  n_speeches: number;
}

export interface Politician {
  id: number;
  name: string;
  name_kana: string | null;
  house: string | null;
  wikidata_id: string | null;
  official_url: string | null;
  n_speeches: number;
  first_date: string | null;
  last_date: string | null;
  affiliations: Affiliation[];
}

export interface Topic {
  id: number;
  term: string;
  category: string | null;
  variants: string[];
  n_speeches: number;
  n_occurrences: number;
}

/** 月×会派の出現件数と**分母**。頻度推移はこれだけで描ける。 */
export interface TopicSeries {
  months: string[];
  kaiha: string[];
  /** 会派（と `*` ＝全体）ごとの、その月の総発言数。**必ずこれで割る** */
  speech_totals: Record<string, number[]>;
  /**
   * 会議名ごとの、その月の議員発言数。**検索を会議名で絞ったときの分母。**
   *
   * 会派の分母（`speech_totals`）とは別物で、こちらは検索の絞り込みと1対1。
   * ブラウザから数えると `speaker_kind` の索引を全部舐めることになるので
   * 配ってある（`scripts/build_topics.py` の `meeting_totals()`）。
   */
  meeting_totals: Record<string, number[]>;
  topics: Topic[];
  series: Record<string, Record<string, number[]>>;
}

export interface TrendingTerm {
  term: string;
  n: number;
  lift: number;
  topic_id: number | null;
}

export interface Trending {
  through: string;
  sitting_days_per_window: number;
  windows: { from: string; until: string; sitting_days: number; n_speeches: number;
             terms: TrendingTerm[] }[];
}

/**
 * 機械抽出の頻出語（`scripts/build_frequent.py`）。
 *
 * **争点語（`TopicSeries`）とは別の層。混ぜないこと**（CLAUDE.md）。
 * あちらは運営が選んだ82件で、こちらは会議録から機械的に採った500件。
 * 件数は**部分文字列で数えた発言数**なので、検索結果の件数と一致する。
 */
export interface FrequentWord {
  term: string;
  n: number;
  /** ピーク年の出現率 ÷ 中央値の年の出現率。一覧はこの順に並んでいる */
  burst: number;
  peak: string;
  /** 争点語にも載っている語。そちらのページへ寄せる */
  topic_id: number | null;
  /** 月ごとの発言数。`Frequent.months` と同じ並び */
  series: number[];
  /**
   * 会期ごとの発言数。`Frequent.sessions` と同じ並び。
   * **月の合算では作れない**（会期は月境界で始まらない。`lib/frequent.ts`）
   */
  sessions: number[];
}

/** 国会の会期。発言の少ない会期（首班指名だけの特別国会など）は載っていない。 */
export interface FrequentSession {
  session: number;
  from: string;
  until: string;
  /** その会期の議員発言数。**割るのはこれ** */
  n_speeches: number;
  n_meetings: number;
}

export interface Frequent {
  months: string[];
  /** その月の議員の発言数。**必ずこれで割る** */
  speech_totals: number[];
  sessions: FrequentSession[];
  params: {
    top: number; min_df: number; min_standalone: number; min_standalone2: number;
    dup_ratio: number; min_session_speeches: number;
  };
  words: FrequentWord[];
}

/**
 * 議員ごとの発言数の推移（`scripts/build_activity.py`）。
 *
 * ★ 出すのは**数だけ**で、中身には触れない。並べるのも**その議員のページの中**だけで、
 *   議員をまたいだ順位は作らない（docs/SCOPE.md）。
 * ★ `months` は**全期間で固定**（その議員の発言がある月だけに詰めない）。
 *   いつ発言が始まり、いつ止まったかが見えるのはそのため。
 */
export interface Activity {
  months: string[];
  params: { top_committees: number };
  politicians: Record<string, {
    /** 発言数の多い順。最後が「その他」のことがある */
    committees: string[];
    /** `committees` と同じ並び。`[月の添字, 件数]` の疎な形 */
    series: [number, number][][];
  }>;
}

/**
 * 配信DBの目録（`scripts/build_db.py` の `write_manifest`）。
 *
 * 分割の単位は**期間**（既定は半期・`2026H1`）。利用者に見せる絞り込みは年のままで、
 * 年 → 期間の変換は `periodsInYearRange()` が行う（`lib/query.ts`）。
 */
export interface Manifest {
  period: "half" | "year";
  periods: string[];
  databases: { id: string; file: string; size: number; from?: string; to?: string }[];
}

let cached: {
  politicians?: Politician[];
  topics?: TopicSeries;
  trending?: Trending;
  manifest?: Manifest;
  frequent?: Frequent;
  activity?: Activity;
} = {};

export function politicians(): Politician[] {
  cached.politicians ??= readJson<{ politicians: Politician[] }>("politicians.json").politicians;
  return cached.politicians;
}

export function topicSeries(): TopicSeries {
  cached.topics ??= readJson<TopicSeries>("dist", "topics.json");
  return cached.topics;
}

export function trending(): Trending {
  cached.trending ??= readJson<Trending>("dist", "trending.json");
  return cached.trending;
}

export function frequent(): Frequent {
  cached.frequent ??= readJson<Frequent>("dist", "frequent.json");
  return cached.frequent;
}

export function activity(): Activity {
  cached.activity ??= readJson<Activity>("dist", "politician_activity.json");
  return cached.activity;
}

/** 疎な `[月の添字, 件数]` を、`months` と同じ長さの密な配列に戻す。 */
export function densify(sparse: [number, number][], length: number): number[] {
  const dense = new Array<number>(length).fill(0);
  for (const [i, n] of sparse) dense[i] = n;
  return dense;
}

export function manifest(): Manifest {
  cached.manifest ??= readJson<Manifest>("dist", "manifest.json");
  return cached.manifest;
}

/** 表示に使う「現在の所属」。会派の時系列のうち最後のもの。 */
export function latestAffiliation(p: Politician): Affiliation | null {
  if (!p.affiliations.length) return null;
  return [...p.affiliations].sort((a, b) => a.end_date.localeCompare(b.end_date)).at(-1) ?? null;
}
