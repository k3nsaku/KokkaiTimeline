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

/** 件数と分母から出現率の系列を作る。分母0の月（国会が開いていない）は落とす。 */
export function toSeries(months: string[], hits: number[], totals: number[]): SeriesPoint[] {
  return months.map((month, i) => ({
    month,
    hits: hits[i] ?? 0,
    total: totals[i] ?? 0,
    rate: totals[i] ? ((hits[i] ?? 0) / totals[i]) * 1000 : 0,
  })).filter((p) => p.total > 0);
}

const W = 720, H = 210, PAD_L = 44, PAD_R = 8, PAD_T = 12, PAD_B = 28;

function escape(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]!));
}

/**
 * 月ごとの出現率を棒グラフにする。
 *
 * 棒にしているのは、国会が開いていない月が飛び飛びに抜けるため。
 * 折れ線にすると、抜けた区間を線でつないでしまい、そこにデータがあるように見える。
 */
export function monthlyChart(points: SeriesPoint[], label: string): string {
  if (!points.length) return "";

  const max = Math.max(...points.map((p) => p.rate), 1);
  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_T - PAD_B;
  const bandW = innerW / points.length;
  const barW = Math.max(1.5, bandW * 0.72);

  const y = (rate: number) => PAD_T + innerH - (rate / max) * innerH;

  const bars = points.map((p, i) => {
    const x = PAD_L + i * bandW + (bandW - barW) / 2;
    const top = y(p.rate);
    // 分母が小さい月は率が跳ねやすいので薄くして、数字を鵜呑みにさせない
    const faint = p.total < 500;
    return `<rect x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" ` +
      `height="${Math.max(0, PAD_T + innerH - top).toFixed(1)}" ` +
      `class="bar${faint ? " faint" : ""}">` +
      `<title>${escape(p.month)}: ${p.hits}件 / ${p.total}発言（1,000発言あたり ${p.rate.toFixed(1)}件）` +
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

  // 目盛りの桁数は系列全体で揃える。0.0 と 18 が縦に並ぶと読み取りにくい
  const digits = max < 10 ? 1 : 0;
  const yTicks = [0, max / 2, max].map((v) => {
    const yy = y(v);
    return `<line x1="${PAD_L}" y1="${yy.toFixed(1)}" x2="${W - PAD_R}" y2="${yy.toFixed(1)}" class="grid" />` +
      `<text x="${PAD_L - 6}" y="${(yy + 4).toFixed(1)}" class="tick right">${v.toFixed(digits)}</text>`;
  }).join("");

  return `
<figure class="chart">
  <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${escape(label)}" preserveAspectRatio="none">
    ${yTicks}${yearTicks}${bars}
  </svg>
  <figcaption class="small muted">
    縦軸は<strong>1,000発言あたりの出現件数</strong>。その月の総発言数で割ってある
    （国会は通年で開いていないので、件数のままだと開催日数の多い月が大きく出る）。
    薄い棒はその月の発言数が500件未満で、率が振れやすいところ。
  </figcaption>
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
