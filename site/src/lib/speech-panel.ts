/**
 * 発言の本文パネル。一覧に**重ねて**出す。
 *
 * 一覧側を触ったら隠し、隅の再表示ボタンで**中身とスクロール位置ごと**戻す。
 * 一覧の幅（`--max: 56rem`）を変えずに本文を広く出すための作り。
 *
 * ## 壊してはいけないもの
 *
 * ★ **抜粋はリンクのまま。** 横取りするのは「広い画面・修飾キー無し・左クリック」
 *   だけで、それ以外は `/speech/<id>` へ普通に遷移する（`render.ts`）。
 *   狭い画面とJS無効で機能が消えないのはこれが理由。
 *
 * ★ **幅の判定を JS に書かない。** CSS の `@media` が `:root` に `--panel-ok: 1` を
 *   立て、ここはそれを読むだけにする。JS と CSS に同じ数値を書くと必ずずれる。
 *
 * ★ **「隠す」と「閉じる」は別。** 隠すは状態を保つ（再表示できる）、
 *   閉じるは捨てる。戻るボタンは**閉じる**に対応させる。
 *
 * ## 履歴
 *
 * 開いたら `pushState` で `/speech/<id>` にする。アドレスバーが共有できるURLのまま
 * になり、リロードすれば本物のページが出る。**隠す・再表示では履歴を触らない**
 * （表示の状態であって遷移ではない）。
 */

import { speechDetail, speechContext, type SpeechDetail } from "./db";
import { renderContextList, splitContext } from "./speech-view";
import { describeError, escapeHtml, formatDate, paragraphs } from "./format";

let root: HTMLElement | null = null;
let bodyEl: HTMLElement | null = null;
let reopenBtn: HTMLButtonElement | null = null;
/** 隠す直前の状態。**DOMは捨てないので、戻すのはスクロール位置だけで足りる。** */
let hidden = false;
let openedFrom: HTMLElement | null = null;
/** パネルを開く前のURL。閉じたらここへ戻す */
let urlBeforeOpen: string | null = null;
let currentLabel = "";

function panelAllowed(): boolean {
  // CSS が単一の情報源。`--panel-ok` は @media の中でだけ 1 になる
  return getComputedStyle(document.documentElement)
    .getPropertyValue("--panel-ok").trim() === "1";
}

function build(): void {
  if (root) return;
  root = document.createElement("aside");
  root.className = "speech-panel";
  root.hidden = true;
  root.setAttribute("role", "complementary");
  root.setAttribute("aria-label", "発言の本文");
  root.innerHTML = `
    <div class="speech-panel-bar">
      <button type="button" class="speech-panel-hide" title="一覧に戻る（あとで続きから読めます）">
        隠す
      </button>
      <button type="button" class="speech-panel-close" title="閉じる">閉じる</button>
    </div>
    <div class="speech-panel-body" tabindex="-1"></div>`;
  document.body.append(root);
  bodyEl = root.querySelector<HTMLElement>(".speech-panel-body")!;

  root.querySelector(".speech-panel-hide")!.addEventListener("click", hide);
  root.querySelector(".speech-panel-close")!.addEventListener("click", () => close());

  reopenBtn = document.createElement("button");
  reopenBtn.type = "button";
  reopenBtn.className = "speech-panel-reopen";
  reopenBtn.hidden = true;
  reopenBtn.addEventListener("click", reopen);
  document.body.append(reopenBtn);
}

function renderDetail(s: SpeechDetail): string {
  const speaker = s.politician_id
    ? `<a href="/politician/${s.politician_id}">${escapeHtml(s.speaker)}</a>`
    : escapeHtml(s.speaker);

  const meta = [
    s.speaker_position, s.speaker_group,
    `第${s.session}回国会`, `${s.house}・${s.meeting}${s.issue ? ` ${s.issue}` : ""}`,
    formatDate(s.date),
  ].filter(Boolean).map((x) => `<span>${escapeHtml(String(x))}</span>`).join("");

  const links = [
    `<a href="/speech/${encodeURIComponent(s.speech_id)}">この発言のページ</a>`,
    `<a href="${escapeHtml(s.speech_url)}" rel="noopener">原典（国会会議録）</a>`,
    s.meeting_url ? `<a href="${escapeHtml(s.meeting_url)}" rel="noopener">会議録全体</a>` : "",
  ].filter(Boolean).join("");

  // 前の発言は本文の上、後の発言は下（`speech-view.ts`）。
  // 中身は後から届くので、器だけ先に置く
  return `
    <div class="speech-panel-before"></div>
    <article class="speech-panel-main">
      <h2 class="speech-title">${speaker}<span class="kind">${escapeHtml(s.speaker_kind)}</span></h2>
      <div class="speech-meta detail">${meta}</div>
      <div class="body">${paragraphs(s.body).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}</div>
      <p class="sources small">${links}</p>
      <p class="small muted">国会会議録の本文そのままです。要約も編集もしていません。</p>
    </article>
    <div class="speech-panel-after"></div>`;
}

async function fillContext(s: SpeechDetail): Promise<void> {
  const beforeHost = bodyEl?.querySelector<HTMLElement>(".speech-panel-before");
  const afterHost = bodyEl?.querySelector<HTMLElement>(".speech-panel-after");
  const main = bodyEl?.querySelector<HTMLElement>(".speech-panel-main");
  if (!beforeHost || !afterHost || !bodyEl) return;

  // ★ 前の発言を本文より上に差し込むと、そのぶん本文が下へ押し出される。
  //   読みたいのは本文なので、差し込んだあとに先頭へ合わせ直す。
  //   届くまでに利用者が動かしていたら、そちらを尊重して触らない
  const atTop = bodyEl.scrollTop === 0;
  const rows = await speechContext(s.issue_id, s.speech_order, 3);
  const { before, after } = splitContext(rows, s.speech_order);
  beforeHost.innerHTML = renderContextList(before, { label: "この前の発言", compact: true });
  afterHost.innerHTML = renderContextList(after, { label: "この後の発言" });

  // offsetTop は「位置指定された最も近い先祖」からの距離なので、
  // 器の position に依存しない矩形の差で動かす。
  //
  // 本文が短くて後の発言も少ないと、**下に送れる分が足りず先頭まで届かない**
  // （実測でスクロール上限に当たり62px残った）。ブラウザの通常の挙動で、
  // 埋め草を足せば消せるが、そのために画面の半分を空白にする価値は無い
  if (atTop && before.length && main) {
    bodyEl.scrollTop +=
      main.getBoundingClientRect().top - bodyEl.getBoundingClientRect().top;
  }
}

async function open(speechId: string, trigger: HTMLElement): Promise<void> {
  build();
  if (!root || !bodyEl) return;

  // 開きっぱなしで別の抜粋を押したときは、履歴を積み増さない
  // ★ `hash` まで含めること。検索条件は `#` に載っている（search.astro）ので、
  //   落とすと本文パネルを閉じたときに検索結果が消える
  if (root.hidden) urlBeforeOpen = location.pathname + location.search + location.hash;
  openedFrom = trigger;
  hidden = false;
  root.hidden = false;
  reopenBtn!.hidden = true;
  document.body.classList.add("has-speech-panel");
  bodyEl.scrollTop = 0;
  bodyEl.innerHTML = `<p class="status" role="status">読み込み中…</p>`;
  bodyEl.focus();

  try {
    const s = await speechDetail(speechId);
    if (!s) {
      bodyEl.innerHTML = `<p class="error">この発言は見つかりませんでした。</p>`;
      return;
    }
    bodyEl.innerHTML = renderDetail(s);
    currentLabel = `${s.speaker} ${Number(s.date.slice(5, 7))}/${Number(s.date.slice(8, 10))}`;
    reopenBtn!.textContent = `${currentLabel} の続きを読む`;
    history.pushState({ speechPanel: true }, "",
      `/speech/${encodeURIComponent(s.speech_id)}`);
    await fillContext(s);
  } catch (err) {
    bodyEl.innerHTML = `<p class="error">読み込みに失敗しました: ${escapeHtml(describeError(err))}</p>`;
    console.error(err);
  }
}

/** 状態を保ったまま引っ込める。**中身もスクロール位置もそのまま残る。** */
function hide(): void {
  if (!root || root.hidden) return;
  root.hidden = true;
  hidden = true;
  document.body.classList.remove("has-speech-panel");
  if (reopenBtn && currentLabel) reopenBtn.hidden = false;
}

function reopen(): void {
  if (!root) return;
  root.hidden = false;
  hidden = false;
  document.body.classList.add("has-speech-panel");
  reopenBtn!.hidden = true;
  bodyEl?.focus();
}

/** 捨てる。履歴も開く前に戻す。 */
function close(fromPopstate = false): void {
  if (!root) return;
  root.hidden = true;
  hidden = false;
  currentLabel = "";
  document.body.classList.remove("has-speech-panel");
  if (reopenBtn) reopenBtn.hidden = true;
  if (bodyEl) bodyEl.innerHTML = "";
  if (!fromPopstate && urlBeforeOpen && location.pathname.startsWith("/speech")) {
    history.pushState({}, "", urlBeforeOpen);
  }
  openedFrom?.focus();
  openedFrom = null;
}

export function initSpeechPanel(): void {
  document.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement | null)
      ?.closest<HTMLAnchorElement>("a[data-speech-id]");
    if (!link) return;
    // 新しいタブ・別ウィンドウ・中クリックは横取りしない（リンクのままにする）
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if ((event as MouseEvent).button !== 0) return;
    if (!panelAllowed()) return;   // 狭い画面はそのまま遷移する
    event.preventDefault();
    void open(link.dataset.speechId!, link);
  });

  // ★ 「フォーカスが戻ったら」は使えない。一覧の本文をマウスで押しても
  //   focus は飛ばない（押した先が focusable でないため）ので pointerdown で見る。
  //
  // ★ 範囲を `main` などに絞らない。**左右の余白（.wrap の外）も対象**で、
  //   絞ると「一覧の脇をクリックしたのに隠れない」になる（実際にそうなっていた）。
  //   パネルと再表示ボタン以外はどこでも隠す、が正しい。
  document.addEventListener("pointerdown", (event) => {
    if (!root || root.hidden) return;
    const target = event.target as HTMLElement | null;
    if (!target) return;
    if (root.contains(target) || reopenBtn?.contains(target)) return;
    if (target.closest("a[data-speech-id]")) return;   // 別の発言は「差し替え」
    // パネル内の文字を選択している最中は触らない（引用のコピーを邪魔しない）
    if (!window.getSelection()?.isCollapsed) return;
    hide();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !root) return;
    if (!root.hidden) close();
    else if (hidden) { /* 隠している間の Esc は何もしない */ }
  });

  // 戻るボタンは「閉じる」に対応させる
  window.addEventListener("popstate", () => {
    if (root && (!root.hidden || hidden)) close(true);
  });

  // 画面が狭くなったらパネルは成立しない。開いていれば畳む
  window.addEventListener("resize", () => {
    if (!panelAllowed() && root && (!root.hidden || hidden)) close(true);
  });
}
