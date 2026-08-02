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

export interface Manifest {
  years: number[];
  databases: { year: number; file: string; size: number }[];
}

/** 2文字語の語彙（`scripts/build_words.py`）。中身は使わず件数だけ要る。 */
export interface Words {
  min_df: number;
  source_speeches: number;
  words: Record<string, number>;
}

let cached: {
  politicians?: Politician[];
  topics?: TopicSeries;
  trending?: Trending;
  manifest?: Manifest;
  words?: Words;
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

export function manifest(): Manifest {
  cached.manifest ??= readJson<Manifest>("dist", "manifest.json");
  return cached.manifest;
}

/** 2文字語の語彙の件数。「何語まで引けるか」を正直に出すために使う。 */
export function wordCount(): number {
  cached.words ??= readJson<Words>("words.json");
  return Object.keys(cached.words.words).length;
}

/** 表示に使う「現在の所属」。会派の時系列のうち最後のもの。 */
export function latestAffiliation(p: Politician): Affiliation | null {
  if (!p.affiliations.length) return null;
  return [...p.affiliations].sort((a, b) => a.end_date.localeCompare(b.end_date)).at(-1) ?? null;
}
