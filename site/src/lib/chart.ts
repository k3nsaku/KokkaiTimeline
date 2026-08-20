/**
 * 頻度推移のグラフ。ビルド時に SVG を組み立てる（描画ライブラリは入れない）。
 *
 * ★**必ず分母で割る。** 国会は通年で開いていないので、件数のままグラフにすると
 *   「開催日数が多い月」が争点に見える。`data/dist/topics.json` は月ごとの
 *   総発言数（`speech_totals`）を持っていて、それがこの分母（CLAUDE.md）。
 */

export interface SeriesPoint {
  month: string;
  hits: number;
  total: number;
  /** 1,000発言あたりの出現件数 */
  rate: number;
}

export interface SeriesOptions {
  /**
   * 分母が分からない月でも、ヒットがあれば残す。**検索結果のグラフ用。**
   *
   * 分母（`dist/topics.json` の `speech_totals`）は日次で作り直されるが、
   * 期間DBのほうが新しいことがありうる。既定（落とす）のままだと、
   * **当たっているのにグラフから消える月**ができる。
   */
  keepHitsWithoutTotal?: boolean;
}

/** 件数と分母から出現率の系列を作る。分母0の月（国会が開いていない）は落とす。 */
export function toSeries(
  months: string[], hits: number[], totals: number[], opts: SeriesOptions = {},
): SeriesPoint[] {
  return months.map((month, i) => ({
    month,
    hits: hits[i] ?? 0,
    total: totals[i] ?? 0,
    rate: totals[i] ? ((hits[i] ?? 0) / totals[i]) * 1000 : 0,
  })).filter((p) => p.total > 0 || (opts.keepHitsWithoutTotal && p.hits > 0));
}

const W = 720, H = 210, PAD_L = 44, PAD_R = 8, PAD_T = 12, PAD_B = 28;

function escape(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
}

/** 縦軸に何を出すか。**件数と率は別のことを言う**（下の注記）。 */
export type MonthlyMode = "rate" | "count";

/**
 * 月ごとの棒グラフ。
 *
 * 棒にしているのは、国会が開いていない月が飛び飛びに抜けるため。
 * 折れ線にすると、抜けた区間を線でつないでしまい、そこにデータがあるように見える。
 *
 * ★ **`mode` で言っていることが変わる。**
 *
 *   - `rate`（既定）: 1,000発言あたりの件数。**その月の総発言数で割る。**
 *     争点語（`/topic`）と頻出語（`/word`）はこちら。「どれだけ話題だったか」を出す
 *   - `count`: 件数そのもの。検索結果ページの既定。**国会が長く開かれた月ほど
 *     大きく出る**ので、注記を必ず添える
 *
 *   実測（`data/dist/kokkai-2025H1.db`・`安全保障`）: 件数では1月が6か月で最低の
 *   68件・4月が最多の706件だが、率では1月が 74.9 で突出し4月（35.8）の2倍になる
 *   （1月の議員発言は908件しかない）。**同じデータで結論が逆になる。**
 */
export function monthlyChart(
  points: SeriesPoint[], label: string, mode: MonthlyMode = "rate",
): string {
  if (!points.length) return "";

  const value = (p: SeriesPoint) => (mode === "count" ? p.hits : p.rate);
  const max = Math.max(...points.map(value), 1);
  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_T - PAD_B;
  const bandW = innerW / points.length;
  const barW = Math.max(1.5, bandW * 0.72);

  const y = (v: number) => PAD_T + innerH - (v / max) * innerH;

  const bars = points.map((p, i) => {
    const x = PAD_L + i * bandW + (bandW - barW) / 2;
    const top = y(value(p));
    // 分母が小さい月は率が跳ねやすいので薄くして、数字を鵜呑みにさせない。
    // **件数のグラフでは薄くしない**（割っていないので跳ねようがない）
    const faint = mode === "rate" && p.total < 500;
    const detail = p.total
      ? `${p.hits}件 / ${p.total}発言（1,000発言あたり ${p.rate.toFixed(1)}件）`
      : `${p.hits}件`;
    return `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" ` +
      `height="${Math.max(0, PAD_T + innerH - top).toFixed(1)}" ` +
      `class="bar${faint ? " faint" : ""}">` +
      `<title>${escape(p.month)}: ${detail}` +
      `${faint ? "\n※その月は発言数が少なく、率が振れやすくなっています" : ""}</title></rect>`;
  }).join("");

  // 年が変わるところにだけ目盛りを打つ。月を全部出すと読めない
  const yearTicks = points.map((p, i) => ({ p, i }))
    .filter(({ p, i }) => i === 0 || p.month.slice(0, 4) !== points[i - 1].month.slice(0, 4))
    .map(({ p, i }) => {
      const x = PAD_L + i * bandW;
      return `<line x1="${x.toFixed(1)}" y1="${PAD_T}" x2="${x.toFixed(1)}" y2="${PAD_T + innerH}" class="grid" />` +
        `<text x="${(x + 2).toFixed(1)}" y="${H - 8}" class="tick">${p.month.slice(0, 4)}</text>`;
    }).join("");

  // 目盛りの桁数は系列全体で揃える。0.0 と 18 が縦に並ぶと読み取りにくい。
  // 件数は整数（0.5件は無い）
  const digits = mode === "count" ? 0 : max < 10 ? 1 : 0;
  // ★ 件数は**整数の位置**に打つ。`max / 2` の位置に置いて数字だけ丸めると、
  //   線と数字がずれる（最大1件の月しか無い語だと、0.5 の線に「1」が出て
  //   上の「1」と2つ並ぶ）。重なった値は畳んで本数を減らす
  const levels = mode === "count"
    ? [...new Set([0, Math.round(max / 2), max])]
    : [0, max / 2, max];
  const yTicks = levels.map((v) => {
    const yy = y(v);
    return `<line x1="${PAD_L}" y1="${yy.toFixed(1)}" x2="${W - PAD_R}" y2="${yy.toFixed(1)}" class="grid" />` +
      `<text x="${PAD_L - 6}" y="${(yy + 4).toFixed(1)}" class="tick right">${v.toFixed(digits)}</text>`;
  }).join("");

  const caption = mode === "count"
    ? `縦軸は<strong>その月の件数</strong>。
    <strong>国会が長く開かれた月ほど大きく出ます</strong>（開会中と閉会中で
    月の発言数が桁で違うため）。話題としての大きさを比べるなら
    「1,000発言あたり」に切り替えてください。`
    : `縦軸は<strong>1,000発言あたりの出現件数</strong>。その月の総発言数で割ってある
    （国会は通年で開いていないので、件数のままだと開催日数の多い月が大きく出る）。
    薄い棒はその月の発言数が500件未満で、率が振れやすいところ。`;

  return `
<figure class="chart">
  <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escape(label)}" preserveAspectRatio="none">
    ${yTicks}${yearTicks}${bars}
  </svg>
  <figcaption class="small muted">${caption}</figcaption>
</figure>`;
}

export interface StackedSeries {
  label: string;
  /** `months` と同じ長さ。足りない分は0として扱う */
  values: number[];
  /** CSS の色（`var(--cat-1)` など）。**系列と一緒に渡す** */
  color: string;
}

/**
 * 積み上げ棒グラフ。月ごとの内訳を出す。
 *
 * ★ **色は呼ぶ側が決めて渡す。** ここで順番に振ると、絞り込みで系列が減ったときに
 *   生き残りが塗り替わる（同じ委員会が別の色になる）。
 *
 * ★ **これは率ではなく件数のグラフ。** `monthlyChart()` と違って分母で割らない。
 *   割るのは「その語が話題になった度合い」を見るときで、こちらは
 *   「いつ何件発言したか」そのものを出す。**呼ぶ側が意味を取り違えないこと。**
 *
 * 積み上げの区切りには2pxの隙間を空ける（隣り合う色の境目が溶けないように）。
 */
export function stackedMonthlyChart(
  months: string[], series: StackedSeries[], label: string,
): string {
  if (!months.length || !series.length) return "";

  const totals = months.map((_, i) =>
    series.reduce((sum, s) => sum + (s.values[i] ?? 0), 0));
  const max = Math.max(...totals, 1);

  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_T - PAD_B;
  const bandW = innerW / months.length;
  const barW = Math.max(1.5, bandW * 0.72);
  const h = (v: number) => (v / max) * innerH;

  const bars = months.map((month, i) => {
    const x = PAD_L + i * bandW + (bandW - barW) / 2;
    let bottom = PAD_T + innerH;
    return series.map((s) => {
      const v = s.values[i] ?? 0;
      if (!v) return "";
      const height = h(v);
      const top = bottom - height;
      bottom = top;
      // 2px の隙間。積み上げの境目が溶けると内訳が読めなくなる
      const drawn = Math.max(0.5, height - 2);
      return `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" ` +
        `height="${drawn.toFixed(1)}" fill="${s.color}">` +
        `<title>${escape(month)} ${escape(s.label)}: ${v}件</title></rect>`;
    }).join("");
  }).join("");

  const yearTicks = months.map((m, i) => ({ m, i }))
    .filter(({ m, i }) => i === 0 || m.slice(0, 4) !== months[i - 1].slice(0, 4))
    .map(({ m, i }) => {
      const x = PAD_L + i * bandW;
      return `<line x1="${x.toFixed(1)}" y1="${PAD_T}" x2="${x.toFixed(1)}" y2="${PAD_T + innerH}" class="grid" />` +
        `<text x="${(x + 2).toFixed(1)}" y="${H - 8}" class="tick">${m.slice(0, 4)}</text>`;
    }).join("");

  const yTicks = [0, max / 2, max].map((v) => {
    const yy = PAD_T + innerH - h(v);
    return `<line x1="${PAD_L}" y1="${yy.toFixed(1)}" x2="${W - PAD_R}" y2="${yy.toFixed(1)}" class="grid" />` +
      `<text x="${PAD_L - 6}" y="${(yy + 4).toFixed(1)}" class="tick right">${Math.round(v)}</text>`;
  }).join("");

  // ★ 系列が2つ以上あるなら凡例は必ず出す。色だけで区別させない。
  //
  // ★★ 色は **SVG の fill 属性**で塗る。`style="background:…"` にしてはいけない。
  //    CSP が `style-src 'self'`（`'unsafe-inline'` 無し）なので、**インラインの
  //    style 属性は黙って無視される**。エラーも出ず、色だけが消える。
  //    実際、凡例をこれで作って色無しの見本が並んだ（`site/public/_headers`）。
  const legend = series.map((s) =>
    `<li><svg class="swatch" viewBox="0 0 10 10" aria-hidden="true">` +
    `<rect width="10" height="10" rx="2" fill="${s.color}" /></svg>` +
    `${escape(s.label)}</li>`).join("");

  return `
<figure class="chart">
  <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escape(label)}" preserveAspectRatio="none">
    ${yTicks}${yearTicks}${bars}
  </svg>
  <ul class="chart-legend">${legend}</ul>
</figure>`;
}

export interface KaihaRate {
  kaiha: string;
  hits: number;
  total: number;
  rate: number;
}

/** 会派ごとの出現率。既定を会派にするのは、政党を特定できない発言が3.6%あるため。 */
export function kaihaRates(
  kaiha: string[], series: Record<string, number[]>, totals: Record<string, number[]>,
): KaihaRate[] {
  const sum = (xs?: number[]) => (xs ?? []).reduce((a, b) => a + b, 0);
  return kaiha
    .map((name) => {
      const hits = sum(series[name]), total = sum(totals[name]);
      return { kaiha: name, hits, total, rate: total ? (hits / total) * 1000 : 0 };
    })
    // 発言数が少ない会派は率が跳ねる。比較の土俵に乗せない
    .filter((r) => r.total >= 2000)
    .sort((a, b) => b.rate - a.rate);
}
