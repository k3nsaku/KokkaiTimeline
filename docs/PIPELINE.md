# 日次更新の運用

`.github/workflows/daily.yml`。**これがこのプロジェクト唯一の定期実行**で、
落ちても数日放置できるように作ってある（会議録は消えないので、次に走れば追いつく）。

---

## 何が毎日走るか

| 手順 | やること | 目安 |
|---|---|---|
| 1 | R2 から生データ・議員マスタ・語彙・目録を戻す | 1〜2分 |
| 2 | Wikidata の議員リスト（**無いときと月初だけ**） | 通常0分 / 取るときは数分 |
| 3 | **前月と当月**の会議録を `--force` で取り直す | 会期中は20〜60分（NDLへ3秒間隔） |
| 4 | 集計の材料になる単一DBを作り直す（`--no-fts`） | 3〜5分 |
| 5 | 議員マスタ・争点語の集計・週次トレンド | 5〜15分 |
| 5b | 頻出語レイヤー（`build_frequent.py`） | 約1分 |
| 6 | **当年**（1月は前年も）の配信用DBを作り直す | 30〜60秒 |
| 7 | 年DBと目録を R2 へ | 1〜2分 |
| 8 | サイトをビルドして Pages へ | 1〜2分 |

過去年のDBは触らない。**変わるのは当年の1ファイルだけ**なので、
CDN キャッシュは過去年ぶんが効き続ける。

**実測（2026-08-06 の初回実行 / run 31086294777）: 全体で4分37秒。**
上の目安は会期中の見積もりで、**閉会中はこの1/10で終わる。**
内訳は手順3（取得）が約1分半、手順4〜5が約2分、残りが1分。
会期中に伸びるのは手順3だけなので、**最悪でも上の目安の合計を超えない。**

**取り直す範囲を当月だけにしない。** 会議録は会議の当日には出ない。月末の会議が
翌月になって公開された分と、前月以前に入った訂正を、当月だけだと二度と拾えない
（`fetch_range.py` はその月を完成済みとして扱うので、`--force` の範囲から外れた
時点で永久に更新されなくなる）。前月1日から取り直すぶん、**1月は前年12月に触る**
ので、当年に加えて前年のDBも作り直す（`--year` は複数指定できる）。

### 走らせないもの

- **`scripts/build_words.py`（2文字語の語彙）** — 下の「語彙を作り直すとき」を読むこと
- **全年の再構築** — 手動（`workflow_dispatch` の `all_years`）
- **`scripts/fetch_wikidata.py` を毎日** — SPARQL エンドポイントは共用資源。
  ただし**手元に `data/raw/wikidata_members.json` が無ければ必ず取る**
  （無いまま進むと `build_politicians.py` がその場で終了して、以降の手順に届かない）

---

## もう一つのワークフロー: CI（`.github/workflows/ci.yml`）

**日次更新は `npm run build` しか回さない**（テストを通さずに本番へ配る）。
その穴を塞ぐのが CI で、`site/**` を触った push と PR で `npm run check`
（`astro check` + テスト87件）を回す。**`src/lib/query.ts` を壊す変更はここで止める。**

- **`npm run build` は回さない。** ビルドには `data/politicians.json` と
  `data/dist/topics.json` が要るが、どちらも生成物でリポジトリに入っていない
  （R2 が保管庫）。再現するには R2 の資格情報が要り、関門としては重すぎる
- **`npm run check` はデータファイルを読まない。** `site-data.ts` の読み込みは
  ページの事前生成のときだけ走る。生成物が無い clean clone で通ることを確認済み
- `site/**` 以外（`scripts/` や `data/topics.json`）の変更では走らない。
  **Python 側にテストは無い**ので、そこは今のところ関門が無い

---

## ★ DBの差し替えとキャッシュ

年DBのURLには **`?v=<中身の指紋>`** が付く（`manifest.json` の `version`）。
これが無いと2つ壊れる。

1. **開きっぱなしのページが壊れる。** `sql.js-httpvfs` は読んだページを
   オフセットで覚えているので、裏でファイルが差し替わると
   「古いページと新しいページが混ざったDB」になり、
   `no such table: speech_fts` のような形で落ちる。
2. **CDN を毎日パージしないといけなくなる。** 同じURLで中身だけ変わるため。

世代がURLに入っていれば、新しく開いたページは別のURLとして取り直し、
古いURLはエッジに残ったまま（＝中身が揃ったまま）になる。
だからDBは長く握らせてよく、**短くするのは `manifest.json` だけ**。

それでも取りこぼす場合の保険として、問い合わせが失敗したら
**目録を取り直してワーカを作り直し、1回だけやり直す**（`site/src/lib/db.ts`）。
やり直しでは `&retry=` を足してURLを変える。**同じURLを引き直しても救えない。**

### ★ `immutable` は付けない（2026-08-02 に外した）

| | |
|---|---|
| 年DB | `public, max-age=3600, s-maxage=31536000` |
| `manifest.json` | `public, max-age=300` |

`immutable` を付けていた時期に、**Chrome のブラウザキャッシュに壊れたものが入り、
全ページが `SQLite: file is not a database` になる事故を1日に2回踏んだ**
（シークレットと他ブラウザでは正常 ＝ プロファイル固有）。`immutable` は
「再検証するな」の意味なので、**Ctrl+Shift+R でも直らず**、過去年は差し替わらないので
`?v=` も変わらない。利用者は手でキャッシュを消すまで直せない。

`?v=` で世代を分けている以上、`immutable` が防いでいるのは条件付きリクエストだけ。
外しても:

- **エッジは `s-maxage` で1年保持する**ので CDN の効きは落ちない（RTT 8ms のまま）
- 中身が変われば `?v=` で別URLになるので、ブラウザ側を短くしても不整合は起きない
- コストは1時間ごとの条件付きリクエスト1本（エッジが 304 を 8ms で返す）

`site/src/lib/db.ts` の `&retry=` は残す。1時間待たずにその場で復旧させるため。

## ★ 語彙を作り直すとき

**`build_words.py` を日次で回してはいけない。**

2文字語の語彙は各年DBに焼き込まれている。当年だけ作り直すと当年の語彙だけが
新しくなり、検索は「語彙は年によらない」前提でいちばん新しい年だけを見て
「その語を引けるか」を判定するので、**新語を引くと過去年が黙って0件になる**。
エラーも警告も出ない。

作り直すときは必ずこの順で:

```bash
python scripts/build_words.py                                  # data/words.json
python scripts/build_db.py --split-by-year --page-size 8192    # 全年（約6分）
```

CI からやるなら `workflow_dispatch` を `all_years=true` で実行する。

**★作り直したら `words.json` を状態バケットにも上げること。**
日次は実行のたびに `s3://<state>/words.json` から語彙を**戻してから**DBを作る。
ここが古いと、手元とR2の年DBをいくら新しくしても**次の日次で古い語彙に巻き戻る**
（最後に同じものが書き戻されるので自然には直らない）。

```bash
aws s3 cp data/words.json s3://kokkai-timeline-state/words.json --endpoint-url $R2
```

直近でこれをやったのは **2026-08-03（`docs/DECISIONS.md`・2文字の全角ラテン）**。
語彙 16,058 → **16,264件**。`build_words.py` の `RUN_PATTERN` と `build_db.py` の
`WORD_RUN_PATTERN` を**両方**直し（片方だけは禁止）、全年を作り直して
R2・状態バケット・Pages まで手で反映済み。

安全網として、各年DBの `meta` テーブルに語彙の指紋を入れてあり、
`manifest.json` にも載る。食い違うと `build_db.py` が警告を出し、
ワークフローは**その場で失敗する**（「語彙が年をまたいで揃っているか」の手順）。

**同じことが `data/topics.json`（争点語）にも当てはまる。** ただし壊れ方は違う。
語彙は「引けなくなる」だけだが、争点語は**`topic_hit` の `topic_id` がずれる**ので、
`/topic/<id>` が**別の争点の発言を出す**。語を1つ足すだけでも全年を作り直すこと。

---

## 設定の進み具合（2026-08-06 時点・**公開済み**）

**すべて通った。** 以下は再構築するときの参照用に残してある。

| # | 作業 | 状態 |
|---|---|---|
| — | ドメイン `kokkai-timeline.com`（Registrar / DNSSEC有効） | ✅ |
| 1 | R2 バケット2つ（`kokkai-timeline` APAC / `kokkai-timeline-state`） | ✅ |
| 2 | カスタムドメイン `db.kokkai-timeline.com` | ✅ 疎通確認済み |
| 3 | CORS ポリシー | ✅ curl で確認済み |
| 3.5 | **Cache Rule `r2-db-cache`** | ✅ RTT 77ms→8ms を確認 |
| 3.5b | クエリ文字列でキャッシュを素通りできる（コストの穴） | ⚠ **通知だけ入れた**（2026-08-03）。free では塞ぎ切れない。手順3.5 の囲み |
| 3.6 | サイトの セキュリティヘッダ（`site/public/_headers`） | ✅ CSP 込み・実機で全ページ型を確認 |
| 4 | R2 APIキー `github-actions-daily-update` | ✅ |
| — | データの初回投入（下の「初回の流し込み」） | ✅ バイト数一致を確認 |
| 5 | Cloudflare Pages プロジェクト（Direct Upload） | ✅ 初回デプロイ済み |
| 6 | Pages 用トークン `github-actions-pages-deploy` | ✅ |
| — | 年DB6個の `cache-control` 打ち直し（`immutable` を消す） | ✅ 6年とも確認・Purge 済み |
| — | 法務（`/disclaimer` `/privacy`・運営者表示・連絡先） | ✅ docs/SCOPE.md |
| — | **Cloudflare Email Routing**（`info@kokkai-timeline.com` → Gmail） | ✅ 2026-08-06 送受信テスト済み。★下記 |
| — | 争点語のレビュー（79→82件） | ✅ docs/DECISIONS.md |
| — | **年DB6個・目録・`words.json`・Pages を手で反映**（2026-08-03） | ✅ ★下記 |
| — | **GitHub にリポジトリを作って push**（`k3nsaku/KokkaiTimeline` public / 既定 `master`） | ✅ 2026-08-06 |
| — | GitHub の **Variables 4つ** | ✅ 2026-08-06 |
| — | GitHub の **Secrets 4つ** | ✅ 2026-08-06 |
| — | **Actions の Workflow permissions を Read and write に** | ✅ 2026-08-06（台帳の書き戻し用） |
| — | `daily.yml` が Actions に登録される | ✅ 2026-08-06。**初回 push では登録されなかった**（下の囲み） |
| — | `workflow_dispatch` で手動実行（**`all_years` は `false` でよい**） | ✅ 2026-08-06 全ステップ成功・4分37秒 |
| — | `kokkai-timeline.com` を Pages に当てる | ✅ 2026-08-06 **＝公開** |
| — | `www.kokkai-timeline.com` | ❌ 当てていない（apex のみ。要否は ROADMAP で保留） |

> ### ★ 連絡先は Cloudflare Email Routing（2026-08-06）
>
> `info@kokkai-timeline.com` を Gmail へ転送している。**サーバは増えていない**
> （Cloudflare 側の設定だけで動く。「常時稼働プロセスを持たない」に触れない）。
> Gmail 側でエイリアスを作ってあるので、返信もこのアドレスから出る。
>
> **サイトの表示は `site/src/lib/operator.ts` の1か所だけ。**
> `/disclaimer` `/privacy` `/about` はそこを見ている。アドレスを変えるときは
> **Cloudflare の転送ルールと Gmail のエイリアスも一緒に**直すこと。
> 訂正依頼の窓口なので、**届かない状態は「窓口が無い」のと同じ**
> （名誉毀損の抗弁としても弱くなる。[SCOPE.md](SCOPE.md)）。
>
> ドメインを移したり作り直したりしたら、**必ず1通送って着信を確かめる。**

> ### ★ 全年の作り直しは 2026-08-03 に手で済ませた
>
> 争点語を 79 → 82 件に変え（`topic_id` がずれる）、さらに
> **2文字語の語彙を 16,058 → 16,264 件に変えた**（`vocabulary` の指紋が変わる）。
> どちらも全年の作り直しが要るもので、**手元で作り直して R2 に上げ済み**。
> `words.json` も状態バケットに置いた。
>
> **したがって Actions の初回は `all_years=false` でよい。** 当年だけ作り直す
> 通常の経路が正しく回るかを見るほうが、いまは情報量が多い。
>
> 手で上げるときは **DBを先に、目録を後に**（順番を逆にすると、まだ無いDBをサイトが引きにいく）。
> **`words.json` を状態バケットに上げ忘れると、日次が古い語彙を戻してきて
> `ＡＩ` が引けない状態に巻き戻る**（最後に同じものが書き戻されるので自然には直らない）。

## Cloudflare 側でやること

**ここはダッシュボードでの手作業。** 全部やり終えるまでワークフローは動かない。

### 1. R2 バケットを2つ作る

| バケット | 用途 | Location | Storage class | 公開 |
|---|---|---|---|---|
| `kokkai-timeline` | 年DB（約2.1GB）と `manifest.json` | APAC（Automatic で可） | Standard | **公開**（カスタムドメイン） |
| `kokkai-timeline-state` | 生データ（約1.2GB）・議員マスタ・語彙 | Automatic で可 | Standard | 非公開 |

**Jurisdiction（`Specify` の EU など）は選ばない。** データ所在地の法規制向けの設定で、
作成後は変更できない。地域ヒントとは別物。

**状態バケットには Custom Domain も CORS も設定しない。** 手順3・手順4は
**公開バケットだけ**の作業。状態バケットを読むのは GitHub Actions だけで、
認証付きの S3 API（`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`）から触るため、
公開の口も、ブラウザ向けの CORS も要らない。付ければ生データ1.2GBを
誰でも取れるようにするだけになる。

> 紛らわしいので明示しておくと、**公開バケットへの書き込みもカスタムドメインは通らない。**
> 日次更新は両方のバケットに S3 エンドポイント経由で書く。
> `db.kokkai-timeline.com` と CORS は、**サイトが年DBを読むためだけ**に要る。

分けるのは、公開バケットにカスタムドメインを付けると**バケット全体が見える**ため。
1.2GBの生データを配る必要はない。合計 3.3GB で、無料枠10GBに収まる。

**Location は作成時にしか指定できない**（同じ名前で作り直しても、ヒントが尊重されるのは
最初の1回だけ）。公開バケットの読み手は日本の有権者なので APAC。
「Automatic」は**作成リクエストの発信元**に近い場所が選ばれるので、日本から作れば APAC になる。

> **状態バケットの ENAM は「できれば」程度。** 読むのは GitHub Actions のランナー
> （主に米国）だけで毎回1.2GB落とすため、理屈では ENAM が速い。ただし
> **ダッシュボードの作成画面では地域ヒントを選べない**（`Specify` は Jurisdiction ＝
> EU等のデータ所在地規制で、まったくの別物。選ばないこと）。
> APAC のままでも日次ジョブが1〜3分伸びるだけなので、**Automatic で流してよい**。
> どうしても ENAM にするなら S3 API から作る:
> `aws s3api create-bucket --bucket kokkai-timeline-state
> --create-bucket-configuration LocationConstraint=ENAM --endpoint-url ...`

**Storage class は両方 Standard。** Infrequent Access は
①年DBが Range で常時読まれる（取り出し料金が乗る）
②**最低保存期間30日**があり、当月の生データは毎日置き換えるので置換のたびに30日分を払う
—— の2点で合わない。そもそも無料枠10GBに収まっているので、安くなる余地が無い。

**Object Lifecycle Rules の `Default Multipart Abort` は有効のままにする。**
約400MBのDBを `aws s3 cp` で上げる＝マルチパートなので、ジョブが途中で死ぬと
中途半端なパートが容量を食う。それを自動で掃除してくれる。

### 2. 公開バケットにカスタムドメインを付ける（**公開バケットだけ**）

ドメインは **`kokkai-timeline.com`**（Cloudflare Registrar で取得済み。
同じアカウントのゾーンなので、そのまま使える）。

| ホスト | 向き先 |
|---|---|
| **`db.kokkai-timeline.com`** | R2（公開バケット） |
| `kokkai-timeline.com` | Cloudflare Pages（サイト） |

R2 object storage → `kokkai-timeline` → **Settings** → **Custom Domains** → **Add** →
`db.kokkai-timeline.com` → Continue → **Connect Domain**。
同じアカウントのゾーンなので DNS レコードは自動で足される。

**`Public Development URL`（＝`r2.dev`）は Enable しない。** 画面には
「Public access: Disabled」と出るが、それはこちらの状態で、**そのままで正しい**。
Cloudflare も「2つは独立していて、カスタムドメインを使うのに `r2.dev` を
有効にする必要はない」と明記している。有効にすると、レート制限がありキャッシュも
効かない経路が 2.09GB のDBにもう1本生えるだけで、得るものが無い。

**カスタムドメインにするのは CDN キャッシュを効かせるため。** Cloudflare は
`r2.dev` を「レート制限があり開発用途に限る」とし、キャッシュを含む機能は
カスタムドメイン経由でしか使えないと明記している。1検索で約80リクエスト飛ぶので、
`r2.dev` のままでは実用にならない。

### 3. 公開バケットに CORS を設定する（**公開バケットだけ**）

サイト（Pages）とDB（R2）は別オリジンになる。**これが無いと検索が何も動かない。**
R2 → バケット → Settings → CORS policy:

```json
[
  {
    "AllowedOrigins": [
      "https://kokkai-timeline.com",
      "https://kokkai-timeline.pages.dev"
    ],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["range"],
    "ExposeHeaders": ["content-range", "content-length", "etag"],
    "MaxAgeSeconds": 86400
  }
]
```

`ExposeHeaders` に `content-range` が要る。sql.js-httpvfs は Range で読むので、
これが見えないとファイルの大きさを判断できない。

`pages.dev` も入れてあるのは、カスタムドメインを当てる前に疎通を確かめられるようにするため。
**プレビュー配置（`<ハッシュ>.kokkai-timeline.pages.dev`）は別オリジンになるので通らない。**
確認は本番配置か、カスタムドメインでやること。

### 3.5. ★ Cache Rule を作る（**これが無いとキャッシュされない**）

**カスタムドメインを付けただけではエッジに乗らない。** Cloudflare は
**拡張子でキャッシュ判定していて、`.db` も `.json` も既定の対象外**
（`cf-cache-status: DYNAMIC` ＝ リクエスト時点でキャッシュ対象外と判定された）。
この構成は待ち時間が「リクエスト数 × RTT」でほぼ決まるので、ここを飛ばすと
**RTT が 8ms → 77ms（約10倍）**になる。実測は下の表。

Caching → Cache Rules → Create rule:

| 項目 | 値 |
|---|---|
| Rule name | `r2-db-cache` |
| 条件 | `(http.host eq "db.kokkai-timeline.com")` |
| Cache eligibility | **Eligible for cache** |
| Edge TTL | **Use cache-control header if present, bypass cache if not** |
| Browser TTL | **Respect origin TTL** |
| Status code TTL | 設定しない |
| Cache key | **触れない**（`Custom Cache Key` は Enterprise 限定。下記） |

> ### ★ クエリ文字列でキャッシュを素通りできる（free では塞ぎ切れない）
>
> **既定ではクエリ文字列がまるごとキャッシュキーに入る。** つまり
> `kokkai-2025.db?v=...&junk=1` `&junk=2` … とゴミを変えるだけで、
> **同じファイルに対していくらでも MISS を作れる**（2026-08-03 に実測して確認）。
> MISS はエッジを抜けて R2 の `GetObject`（Class B）に届く。
> 無料枠は月1,000万・超過は100万あたり $0.36 なので、**外から課金を積み増せる。**
>
> **正攻法は Cache Key の Query String を `v` だけ include にすることだが、
> これは Enterprise 限定**（Cache Rules も Page Rules も `Custom Cache Key` は Enterprise）。
> free で取れる手は3つで、どれも一長一短:
>
> | | できること | 限界 |
> |---|---|---|
> | **① 通知を入れる** ✅ 済（2026-08-03） | R2 の使用量が跳ねたら気づける | 防げはしない。**だが気づければ手は打てる** |
> | **② Rate limiting rule 1本**（無料枠） | 単発の雑な連打を止める | free は**カウント10秒・ブロック10秒に固定**。しかも閾値を下げすぎると正常な利用を弾く（下記） |
> | **③ クエリ文字列を全部やめる** | **完全に塞げる** | 現構成の利点を1つ手放す。実際に叩かれてから（下記） |
>
> **②③は入れていない**（2026-08-03 判断）。被害は $0.36/百万リクエストで、
> 気づいて止められれば大した額にならない。②は効きが弱く、③は purge の失敗が
> 「DBは新しいのにエッジが古い」＝ `immutable` で実際に踏んだのと同型の事故を生む。
> **起きていない攻撃のために、踏んだことのある事故の再発リスクを取らない。**
>
> **②の閾値を下げすぎないこと。** 1回の検索は1年DBあたり平均80・最悪187リクエストの束で、
> 6年を並列に引くので**1回の検索で約500、重い語だと1,000を超える**。
> さらに「さらに読み込む」で積み増す。**10秒あたり2,000未満にすると普通の利用者が落ちる。**
> その閾値だと単一IPで平均100req/s は通ってしまうので、**止まるのは事故と雑なスクレイパまで。**
>
> **③をやるなら**（＝実際に課金が動いてから）、`Cache Level: Ignore Query String` は
> Page Rules 経由なら **free でも使える**。ただし `?v=` が効かなくなるので、
> 単体でやると**DBを差し替えた瞬間にエッジが古いまま1年固まる**。必ずセットで:
>
> - 版をオブジェクトキーに入れる（`kokkai-2025-<指紋>.db`）か、
> - 差し替えた年のURLをワークフローから purge する（API 1本）
>
> そのうえで WAF カスタムルール（free で5本）で
> `http.host eq "db.kokkai-timeline.com" and http.request.uri.query ne ""` を Block すれば、
> ゴミ付きリクエストはエッジより手前で落ちて **R2 に1回も届かない。**
> 代償は「CDNのパージが要らない」という現構成の利点で、
> **purge の失敗が新しい壊れ方（DBは新しいのにエッジが古い）を生む**。
>
> #### 叩かれたときにやること（先に決めておく）
>
> 1. Security → **Under Attack Mode** を入れる（一時的にサイトも重くなるが即効）
> 2. Rate limiting rule の閾値を下げる
> 3. 最後の手段: 公開バケットのカスタムドメインを外す。**サイトは壊れるが課金は止まる**
>
### 3.6. サイト側のセキュリティヘッダ

`site/public/_headers` に入れてある（Pages が読む。**R2 側には効かない**）。
CSP・`X-Frame-Options`・`Permissions-Policy`・HSTS。

**触ったら実機で確かめること。壊れてもエラーが出ず、機能だけ静かに消える。**
`script-src 'self'` を入れた時点でメールのコピーボタンが黙って消えた
（Astro が小さいスクリプトをHTMLに直接埋めていたため）。
`astro.config.mjs` の `inlineStylesheets: "never"` と
`vite.build.assetsInlineLimit: 0` がそれを防いでいる。**片方だけ戻さないこと。**

`connect-src` に年DBのホストが入っているので、**`PUBLIC_DB_BASE` を変えたら
`_headers` も変える**。

### 4. R2 の S3 API キーを作る

R2 → Manage R2 API Tokens → Create API token。

| 項目 | 値 |
|---|---|
| Token name | `github-actions-daily-update` |
| Token type | **Account API Token** |
| Permission | **Object Read & Write** |
| バケット | `kokkai-timeline` と `kokkai-timeline-state` の**両方** |
| TTL | 無期限 |

- **User API Token にしない。** 個人ユーザに紐づくので、そのユーザがアカウントから
  外れると無効になる。Account token は手で失効させるまで有効
- **Admin Read & Write にしない。** バケットの作成・削除までできてしまう。
  パイプラインがやるのはオブジェクトの読み書きだけ
- **TTL を切らない。** 切るとその日に黙ってパイプラインが止まる
- **Secret Access Key は一度しか表示されない**

トークン名は「失効させたら何が壊れるか」が分かるものにする。
Pages 用（手順6）は `github-actions-pages-deploy` にして対にしておく。

### 5. Cloudflare Pages のプロジェクトを作る

**手順6のトークンを先に作ること。** ダッシュボードからは作り切れない（下記）。

> **Git連携にしない。** サイトのビルドに `data/politicians.json` などの生成物が
> 要るため。それらはリポジトリに入れていないので、Cloudflare 側ではビルドできない。
> GitHub Actions でビルドして成果物だけ送る。

> **Workers ではなく Pages。** ダッシュボードの `Create application` は Workers の
> 入口で、Pages は「Looking to deploy Pages? Get started」の隅のリンクにある。
> `Upload your static files` を選ぶと Pages ではなく **Worker** ができてしまう。
> **無料枠が違う**（Pages は静的ファイルのリクエストが無制限、Workers は
> 1日10万リクエスト）。バズっても財布が痛まない構成にするのが前提なので Pages。

★ **ダッシュボードの Drag and drop は使えない。1,000ファイルが上限で、
このサイトは 1,211ファイル**（議員ページだけで1,111枚ある）。**将来も超え続ける。**
名前を入れた時点でプロジェクトだけは作られるので、アップロードは Wrangler でやる。

```powershell
$env:CLOUDFLARE_API_TOKEN = '＜手順6のトークン＞'
$env:CLOUDFLARE_ACCOUNT_ID = '＜アカウントID＞'

# ダッシュボードで作っていなければ
npx --yes wrangler@4 pages project create kokkai-timeline --production-branch main

npx --yes wrangler@4 pages project list   # Production branch が main か確かめる
npx --yes wrangler@4 pages deploy site/dist --project-name kokkai-timeline --branch main
```

名前は `kokkai-timeline`。ここが `PAGES_PROJECT`。
`--branch main` は Pages 上の「本番かプレビューか」のラベルで、**git のブランチ名
（`master`）とは無関係**。Direct Upload なので Cloudflare は git を見ない。
プロジェクトの Production branch と食い違うとプレビュー扱いになり、
**カスタムドメインに出ない。**

初回のデプロイが済んだら、Pages → Custom domains → `kokkai-timeline.com` を当てる。
`www` は使わない（サイト側の正規URLを apex に揃えてある。
`astro.config.mjs` の `site` と `<link rel="canonical">`）。
`www` からのアクセスを拾いたければ、Redirect Rule で apex に 301 する。

### 6. Pages 用の API トークンを作る（**手順5より先**）

**R2 のトークンとは置き場所が違う。** アカウントのトップではなく
My Profile → API Tokens → Create Token → 一番下の **Custom token**。

| 項目 | 値 |
|---|---|
| Token name | `github-actions-pages-deploy` |
| Permission | **Account → Cloudflare Pages → Edit** の1行だけ（Zone は要らない） |
| TTL | **空のまま**（＝無期限）。欄を押すとカレンダーが開くが、日付を選ばずに Esc で閉じる |

---

## GitHub 側でやること

Settings → Secrets and variables → Actions。

> ### ★ 最初の push に含めたワークフローは Actions に登録されないことがある
>
> 2026-08-06 に踏んだ。**リポジトリ作成と同時の push で入った `daily.yml` が、
> 5分待っても Actions に出てこなかった**（`gh api repos/<owner>/<repo>/actions/workflows`
> が `total_count: 0`）。Actions は有効、public、既定ブランチは `master`、
> YAML も妥当で、**症状はエラーではなく「ただ存在しない」。**
>
> GitHub はその push で**変更されたワークフローを登録する。**
> ブランチを作る初回 push では取りこぼすことがあり、以後そのファイルを
> 一度も変更しないと再登録の機会が来ない。
>
> **直し方: `daily.yml` に何か1行変更を入れて push すればよい**（10秒で登録される）。
> 切り分けるなら、些細なワークフローを別名で1つ push してみること。
> **そちらが即座に登録されるなら、YAML の中身は無実。**

### Secrets

| 名前 | 中身 |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare のアカウントID |
| `R2_ACCESS_KEY_ID` | 手順4のキー |
| `R2_SECRET_ACCESS_KEY` | 手順4のシークレット |
| `CLOUDFLARE_API_TOKEN` | 手順6のトークン |

### Variables

| 名前 | 値 |
|---|---|
| `R2_PUBLIC_BUCKET` | `kokkai-timeline` |
| `R2_STATE_BUCKET` | `kokkai-timeline-state` |
| `PUBLIC_DB_BASE` | `https://db.kokkai-timeline.com` |
| `PAGES_PROJECT` | `kokkai-timeline` |

### リポジトリを public にするか

**Actions の無料枠は public リポジトリなら実質無制限、private だと月2,000分。**
このジョブは1回20〜60分なので、private だと月600〜1,800分。**入るが余裕は無い。**
会期中に取得が伸びると超える月が出る。public にするのが素直。

**2026-08-06 に public で作った**（`k3nsaku/KokkaiTimeline`）。
閉会中の実測は1回4分37秒なので private でも入る月はあるが、会期中の上振れを
毎月見張るより public のほうが運用工数の制約に合う。

---

## 初回の流し込み

ワークフローは「R2 に前回の状態がある」前提で書いてあるが、
無ければ落とさずに進む（`|| true`）。ただし**初回だけは手元から入れる**。

- 生データ1.2GB を取り直すと**5時間**かかる（NDLへ3秒間隔）
- **過去年のDBはここでしか上がらない。** 日次ジョブが作り直すのは当年だけなので、
  2021〜2025 を手で上げておかないと永久に欠ける

PowerShell（Windows）で:

```powershell
$env:AWS_ACCESS_KEY_ID = '＜Access Key ID＞'
$env:AWS_SECRET_ACCESS_KEY = '＜Secret Access Key＞'
$env:AWS_DEFAULT_REGION = 'auto'
# aws-cli v2 のチェックサム既定値で R2 が落ちることがある。先回りして外す
$env:AWS_REQUEST_CHECKSUM_CALCULATION = 'when_required'
$env:AWS_RESPONSE_CHECKSUM_VALIDATION = 'when_required'
$R2 = 'https://＜アカウントID＞.r2.cloudflarestorage.com'

# 疎通確認。**`aws s3 ls`（バケット一覧）は AccessDenied になるが、それが正しい。**
# ListBuckets はアカウント全体を見る管理操作で、Object Read & Write には入っていない。
# 確認はバケットの中を見る形でやる（空なら無出力・エラー無しが成功）
aws s3 ls s3://kokkai-timeline/ --endpoint-url $R2

aws s3 sync data\raw\speeches s3://kokkai-timeline-state/raw/speeches --endpoint-url $R2
# ★ 名寄せの材料。**これを置き忘れると日次更新が議員マスタの更新で止まる。**
#   data/raw/ は .gitignore なので、ランナーはここからしか受け取れない
aws s3 cp data\raw\wikidata_members.json s3://kokkai-timeline-state/raw/wikidata_members.json --endpoint-url $R2
aws s3 cp data\raw\wikidata_terms.json s3://kokkai-timeline-state/raw/wikidata_terms.json --endpoint-url $R2
aws s3 cp data\politicians.json s3://kokkai-timeline-state/politicians.json --endpoint-url $R2
aws s3 cp data\words.json s3://kokkai-timeline-state/words.json --endpoint-url $R2

# cache-control は日次ジョブと同じにする。**immutable は付けない**
# （エッジは s-maxage で1年。上の「immutable は付けない」）
Get-ChildItem data\dist\kokkai-*.db | ForEach-Object {
  aws s3 cp $_.FullName "s3://kokkai-timeline/$($_.Name)" --endpoint-url $R2 `
    --cache-control "public, max-age=3600, s-maxage=31536000"
}
aws s3 cp data\dist\manifest.json s3://kokkai-timeline/manifest.json --endpoint-url $R2 `
  --cache-control "public, max-age=300"
```

### 既にR2にあるものの `cache-control` を打ち直す

**`cache-control` はオブジェクトのメタデータなので、上げ直すまで古いままになる。**
2026-08-02 より前に投入した年DBは `immutable` が付いているので、一度だけ打ち直す。
400MBのDBを上げ直さなくても、**同じキーへのコピーでメタデータだけ差し替えられる**:

```powershell
Get-ChildItem data\dist\kokkai-*.db | ForEach-Object {
  aws s3 cp "s3://kokkai-timeline/$($_.Name)" "s3://kokkai-timeline/$($_.Name)" `
    --endpoint-url $R2 --metadata-directive REPLACE `
    --cache-control "public, max-age=3600, s-maxage=31536000"
}

# 打ち直せたか確認（CacheControl の欄を見る）
aws s3api head-object --bucket kokkai-timeline --key kokkai-2025.db --endpoint-url $R2
```

**中身は変わらないので `?v=` も変わらない。** エッジには古いヘッダのまま載った
オブジェクトが残っているので、**打ち直したら Caching → Configuration →
Purge Everything を1回だけ実行する**（しないと最長1年、古い `immutable` が配られ続ける）。
パージ後の初回アクセスは `MISS` の 210〜260ms に戻るが、1回で載り直す。

**確認**: ブラウザで `https://db.kokkai-timeline.com/manifest.json` を開く。
JSONが見えればカスタムドメインと公開設定が通っている。
403/404 ならその先へ進んでも無駄なので、ここで直す。

**バイト数の突き合わせ**（切れた転送を見逃すと、壊れたDBが配られる）:

```powershell
aws s3 ls s3://kokkai-timeline/ --endpoint-url $R2
```

出てくるサイズが `data\dist\` の実物と一致すること。2026-08-02 の投入時は
6個のDBと `manifest.json` すべてが一致し、目録に記録されたサイズとも整合していた。

### 手で Pages に上げるとき（Actions を通さない場合）

GitHub より先に本番で確かめたいときに使う。2026-08-03 に実際にこれで通した。

**★ビルドに `PUBLIC_DB_BASE` を必ず付ける。** 付け忘れると `/db`（開発サーバ用の
相対パス）を指した `dist` ができ、**上げてもDBを引けないサイトが本番に載る**。
エラーも出ないので気づきにくい（実際に一度作ってしまった）。

```powershell
cd site
$env:PUBLIC_DB_BASE = 'https://db.kokkai-timeline.com'
npm run build
cd ..
```

上げる前に、`dist` が本当に本番のDBを指しているか見る（0件ならビルドし直し）:

```powershell
Select-String -Path site\dist\_astro\*.js -Pattern 'db\.kokkai-timeline\.com' | Measure-Object
```

デプロイは Actions と同じコマンド。**`--branch main` は Pages の Production
branch に合わせるラベル**で、手元の git のブランチ名とは無関係:

```powershell
$env:CLOUDFLARE_API_TOKEN = '＜github-actions-pages-deploy のトークン＞'
$env:CLOUDFLARE_ACCOUNT_ID = '＜アカウントID＞'
npx --yes wrangler@4 pages deploy site\dist --project-name kokkai-timeline --branch main
```

**確認は件数で取る。** 2026-08-03 の反映後、全年（2021〜2026）で
`ＡＩ` 5,412 / `Ｇ７` 4,439 / `ＤＸ` 3,039 / `ＧＸ` 2,692 / `ＥＵ` 2,579。
「引けない」と出たら、年DBか目録のどちらかが古いままか、パージが効いていない。

そのあと Actions から `workflow_dispatch` で1回手動実行して、通ることを確かめる。

### 初回実行で特に見るところ

- **`2文字語の語彙が年をまたいで揃っているか` の手順で落ちないか。**
  落ちたら語彙がずれている（「語彙を作り直すとき」を読む）
- **`議員ID台帳が増えていたらコミットする` が push できるか。**
  ここが失敗したら放置しない。台帳を失うと公開後のURLが全部変わる
- **目録の `years` が6年そろっているか。** CI は当年しかDBを置かないので、
  前回の目録を引き継げていないと当年1年に痩せる

---

## 失敗したとき

GitHub は**定期実行が失敗すると、そのワークフローファイルを最後に変更した人にメールする**。
これで「メールが飛ぶだけでよい」という要件は満たしている。追加の通知は入れていない。

数日止まっても壊れない。次に走ったときに当月を取り直すので追いつく。

| 症状 | 見るところ |
|---|---|
| `aws s3 ls` が AccessDenied | **正常。** バケット一覧は管理操作で、Object Read & Write には含まれない。`aws s3 ls s3://<バケット>/` で確かめる |
| 検索が過去年だけ0件 | 語彙の食い違い。上の「語彙を作り直すとき」 |
| 検索がまったく動かない | R2 の CORS（手順3）。ブラウザのコンソールに CORS エラーが出る |
| サイトは出るが発言が出ない | `PUBLIC_DB_BASE` の値、カスタムドメインの疎通 |
| 議員IDの台帳が push できない | **放置しない。** 台帳を失うと公開後のURLが全部変わる |

---

## 費用

| | 使用量 | 無料枠 |
|---|---|---|
| R2 ストレージ | 3.3 GB | 10 GB |
| R2 Class A（書き込み） | 月およそ100回 | 月100万回 |
| R2 Class B（読み出し） | 1検索あたり約80回 → 月12万検索 | 月1,000万回 |
| R2 下り | 0円（R2は下り無料） | — |
| Cloudflare Pages | 月500ビルドまで。日次なら30 | — |
| GitHub Actions | public なら実質無制限 | — |

**実際にかかるのはドメイン代（月250円）だけ。** 予算は月1,000円。
