# 安全性

このサイトは**閲覧者に登録させないし、運営者の個人情報も置いていない**。
だから守るべきは**改ざん**と**閲覧者の検索語**の2つに絞られる。

**「個人情報が無いから軽い」は半分しか合っていない。** 運営者の情報は無いが、
**閲覧者が何を検索したか**は残る。政治的関心を示す語はそれ自体がセンシティブになりうる。

個別の落とし穴は [PITFALLS.md](PITFALLS.md)「公開まわり」、なぜそうしたかは
[DECISIONS.md](DECISIONS.md) §4 と §8。**この文書は「いまどうなっているか」と
「何が運営者の手にしか無いか」を書く。**

---

## 脅威と、いまの手当て

| 脅威 | 実現に要るもの | 手当て |
|---|---|---|
| **日次CIの供給網** → 配信DB・サイト・リポジトリを丸ごと書き換える | 依存パッケージか action の乗っ取り1件 | 露出面を4つ塞いだ（下） |
| **閲覧者の検索語が外に出る** | 何も要らない（普通に検索するだけ） | `#` に移し、文面を実態に合わせた |
| **Wikidata 経由の改ざん** | Wikidata を編集するだけ（資格情報が要らない） | 出口は公式サイトURL1本に限定 |
| **運営コンソールへの CSRF** | コンソール起動中に悪意あるページを開かせる | 4つの検査で塞いだ |
| **配信DBの改ざん** | R2 の鍵 | escape + CSP で script 実行には化けない |

見直しの対象は公開環境への受動的なHTTP確認・依存監査・Git履歴の秘密情報スキャン。
**侵入・大量通信・負荷試験はしていない。**

---

## 1. 検索語

- 検索条件は **`#q=…`**（`search.astro`）。`?q=` は HTTPS でも配信事業者に届く
- **フラグメント化しても塞げない2経路**（JS 無効・古い `?q=` URLの初回）は
  `/privacy` に明記してある。限界の表は [DECISIONS.md](DECISIONS.md) §4
- **`/search` には解析タグを出さない。** フラグメントは HTTP 要求に載らないが、
  **そのページで動くスクリプトからは読める**。除外は `operator.ts` の
  `ANALYTICS_EXCLUDED_PATHS` に置き、`Base.astro`（出すか）と
  `privacy.astro`（何と書くか）が**同じ配列を読む**。代償は検索ページの閲覧数が取れないこと
- **上限は `canonicalQuery()` に掛ける**（`splitTerms()` だけだと画面とURLに切る前の語が残る）。
  `run()` は `pushUrl` が偽でも `replaceState` でURLを直す
- **URLは解析の前に切る**（`readUrl()` の `MAX_URL_LENGTH`）。`maxlength` は JS からの
  代入を止めない。実測: 1.8MB のフラグメントで開いても画面は応答し（636ms）、
  入力欄もURLも 64文字に揃う。上限ぎりぎりの正当なクエリ（16語 × 64文字・URL 9,262文字）は無傷

## 2. 日次CIの供給網

| | 前 | 後 |
|---|---|---|
| R2 の鍵 | ワークフロー直下の `env`（全 step から見える） | それを使う4 step だけ |
| wrangler | `npx --yes wrangler@4`（毎回 npm から最新） | 版を固定した devDependency・`npm ci` の完全性検査 |
| actions | `@v4` / `@v5`（動かせるタグ） | コミットSHAで固定 |
| checkout | 認証情報を `.git/config` に残す | `persist-credentials: false`（台帳を push する step にだけ渡す） |

★ **actions を上げるときはコメントの版も一緒に直す。** SHA だけ変えて版のコメントが
古いままだと次に読む人が判断できない。**版はリリース一覧で見る**
（タグ一覧だけだと prerelease と正式リリースを区別できない）。

## 3. 運営コンソール（`scripts/admin.py`）

`127.0.0.1` への bind だけでは足りない。**閲覧者のブラウザは内側にいる**し、
`Content-Type: text/plain` の POST は CORS のプリフライトを起こさない。

| 守るもの | やっていること |
|---|---|
| CSRF / DNS リバインディング | Origin・Host・Content-Type・起動ごとの合言葉の**4つ**（どれか1つに頼らない） |
| 同時保存の衝突 | 読み込み→検証→書き込みを `SAVE_LOCK` で直列化。一時ファイル名は `tempfile.mkstemp` で一意 |
| **別タブによる編集消失** | GET が返す `revision`（中身の sha256）を POST に添えさせ、違えば **409** |
| 版と表示のずれ | **1回の read から本文と版の両方を作る**（`load_with_revision()`）。2回読むと間の保存を取りこぼす |
| `--port 80` | ポート無しの `Host` も許す（既定ポートではブラウザが `:80` を付けない） |

実測で確認済み: 外部 Origin（`text/plain` / `application/json`）・`Sec-Fetch-Site: cross-site`・
`Host: evil.example`・合言葉なし はすべて **403**。12並行の保存で12件成功・残骸なし。
タブA→タブBの順の保存で B は **409**（A の変更が残る）。

## 4. Wikidata から公開ページに出る自由文字列

**出口は公式サイトURL（P856）1本だけ。** 氏名は会議録から採って Wikidata で上書きせず、
政党は `party_map` の候補内からしか選ばれず、院は固定文字列2つ。
URLには scheme の検査を置いてある（`build_politicians.py` の `safe_url()`）。

`http://` は落としていない（400件超あり、消すと大量のリンクが黙って消える）。
代わりに表示を**「公式サイト（Wikidata 掲載・未確認）」**にし、
`rel="noopener nofollow external"` を付けてある。
**リンク先が本物かどうかは検証していない。**

## 5. その他

- `set:html={JSON.stringify(...)}` は `jsonScript()` 経由（`</script>` で抜け出せない）
- `404.astro` がある（無いと Pages が存在しないパスにトップを **200** で返す）
- 検索語の語数・長さに上限（`query.ts` の `MAX_TERMS` / `MAX_TERM_LENGTH` / `MAX_URL_LENGTH`）
- `npm audit` は **0件**（astro 7.2.4 / sharp 0.35.3 / esbuild 0.28.2 / nanoid 3.3.18）

---

## 確かめて、問題が無かったもの

- **エスケープが全経路で徹底している。** `highlight()` は「エスケープ → マーカー置換」の順。
  `render.ts` / `speech-view.ts` / `speech-panel.ts` / `chart.ts` すべて escape 済み
- **CSP が実機で効いている。** 配信DBを改ざんされても script 実行には化けない。
  **ここが最後の砦**なので、`_headers` を緩めるときは何が守られなくなるかを見ること
- **SQL は全部プレースホルダ束縛。** `${}` で埋まるのは列名・静的断片・`instr()` の
  繰り返し回数だけで、利用者の文字列は SQL 本文に入らない
- **R2 の CORS は `https://kokkai-timeline.com` 限定**（別 Origin には ACAO を返さない）。
  バケット一覧は 404
- **Git 履歴に秘密は無い**（すべて `${{ secrets.* }}` かドキュメントのプレースホルダ）。
  GitHub 側の secret scanning と push protection も有効
- **ワークフローに script injection の経路が無い。** `${{ }}` に入るのは `date` 由来の
  出力・`vars.*`・boolean 型の `inputs` だけ
- Python 側に `subprocess` / `eval` / `pickle` / TLS 検証の無効化が無く、外部通信は
  NDL と Wikidata の https のみ
- `admin.py` / `prototype/server.js` / dev サーバはすべて `127.0.0.1` bind、
  パストラバーサル対策あり

> **astro を 5 系から 7 系に上げたときの唯一の breaking change** は、`site-data.ts` が
> `import.meta.url` から `data/` を辿っていたのがバンドル再配置で `site/data/` を
> 指して落ちたこと。**cwd 基点に変えてある。**

---

## 運営者の手にしか無いもの

**コードでは閉じられない。** ダッシュボードや GitHub の設定なので**リポジトリには現れず、
ここが唯一の記録になる**（`OPERATIONS.local.md` は端末ごとの実績で、失うと消える）。

| 設定 | どこ | いまの値 |
|---|---|---|
| 既定のワークフロー権限 | GitHub → Actions → General | `read`。`daily.yml` が自分で `contents: write` を宣言するので台帳の書き戻しは通る |
| Dependabot | Settings → Code security | alerts と security updates の両方。★**alerts が先** —— 無効だと `automated-security-fixes` は黙って無視される |
| `master` のブランチ保護 | Settings → Rules | ruleset `protect-master`（active・bypass 0件）。ルールは **`deletion` と `non_fast_forward` の2つだけ**。★**PR必須は入れない**（日次の台帳 push が止まる） |
| Cloudflare の資格情報 | Cloudflare | Pages トークンは `Cloudflare Pages: 編集` の1行・アカウント1つ。R2 キーは Object Read & Write・バケット2つ。★**IPフィルタと TTL は空のまま**（ランナーのIPは変わる／失効すると最大1か月配信が止まる） |
| GitHub の 2FA | GitHub → Settings | 有効・方式は **passkey**・Recovery codes 保存済み。★API では方式まで読めないので画面で見る |
| `db.` の HTTPS 強制 | Cloudflare → SSL/TLS | 「常に HTTPS を使用」ON。暗号化モードは**フル**。**ゾーン HSTS は入れない**（[DECISIONS.md](DECISIONS.md)） |
| 請求アラート | Cloudflare | 疎通確認済み。★**Rate Limiting は入れない**（[PIPELINE.md](PIPELINE.md) 3.5b の実測） |

## まだ残っているもの

| やること | いつ | なぜ |
|---|---|---|
| **議員ID台帳の書き戻しを確認** | 議員が増えた日に1回 | ★**まだ一度も通っていない**（差分が出た日にしか走らない）。`persist-credentials: false` ＋ ヘッダ経由トークン ＋ ruleset 下での初回になる。落ちたまま放置すると台帳を失い、公開後のURLが全部変わる |
| 公式サイトURLの見直し | 6か月 | 平文 http が400件超。**リンク先が本物かは検証していない**（失効ドメインの取り直しはコードでは防げない） |
