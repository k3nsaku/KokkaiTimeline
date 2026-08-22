# 日次更新の運用

`.github/workflows/daily.yml`。**これがこのプロジェクト唯一の定期実行**で、
落ちても数日放置できるように作ってある（会議録は消えないので、次に走れば追いつく）。

---

## 何が毎日走るか

| 手順 | やること | 目安 |
|---|---|---|
| 1 | R2 から生データ・議員マスタ・目録を戻す | 1〜2分 |
| 2 | Wikidata の議員リスト（**無いときと月初だけ**）。取れたらその場で R2 へ | 通常0分 / 取るときは数分 |
| 3 | **前月と当月**の会議録を `--force` で取り直す | 会期中は20〜60分（NDLへ3秒間隔） |
| 4 | 集計の材料になる単一DBを作り直す（`--no-fts`） | 3〜5分 |
| 5 | 議員マスタ・争点語の集計・週次トレンド | 5〜15分 |
| 5b | 頻出語レイヤー（`build_frequent.py`） | 約1分 |
| 5c | 議員ごとの発言数の推移（`build_activity.py`） | 数秒 |
| 6 | **触った期間**（7月と1月は前の期間も）の配信用DBを作り直す | 10〜60秒 |
| 7 | **配信物を検証する**（`scripts/verify_dist.py`。下記） | 数秒 |
| 8 | サイトの型検査・テスト（`npm run check`）とビルド | 2〜3分 |
| 9 | **議員ID台帳に差分があれば commit / push** | 数秒 |
| 10 | 期間DBと目録を R2 へ | 1〜2分 |
| 11 | Cloudflare Pages へデプロイ | 1分 |
| 12 | 次回のための状態（生データ・議員マスタ）を R2 へ | 1〜2分 |
| 13 | **配ったものを公開URLごしに検算する**（`scripts/verify_published.py`。下記） | 数秒 |

閉じた期間のDBは触らない。**変わるのは1〜2ファイルだけ**なので、
CDN キャッシュは残りが効き続ける。配信DBは半期で割ってある（`2026H1` = 1〜6月）。

### ★ 順番の原則: 外に出すのは検証を全部終えてから

**R2 と Pages に配ってしまうと取り消せない。** だから手順7〜9（検証と台帳）を
手順10〜11（公開）より前に置いてある。完全な同時更新はできないが、
**検証に落ちたものを公開しない**順序にはできる。

- **議員ID台帳（手順9）が公開より前にあるのは意図的。** 新しい議員IDでページを
  配ったあとに push が落ちると、翌日は採番からやり直しになり、
  **前日に配ったURLが誰も指さない場所になる**
- **手順12（状態の保存）だけは公開のあと。** 外から見えないうえ、
  **落ちても翌日に自然に追いつく**ものしか置いていない
  （生データは直近2か月ぶんを毎日 `--force` で取り直す。議員マスタは毎回作り直す）
- **★ Wikidata の議員リストだけは手順12に入れない。取れたその場（手順2）で書き戻す。**
  取り直すのは**月初だけ**なので、ここが最後まで持ち回られてジョブの終わりで落ちると、
  状態バケットに古いリストが残ったまま**翌月まで取り直しの機会が来ない**
  （公開物は新しいのに名寄せの材料だけ1か月古い、という形で静かに劣化する）
- 目録をDBより後に上げる原則（手順10の中）と、URLの `?v=` による世代分離は変えていない

### ★ 手順7: 配信物の検証（`scripts/verify_dist.py`）

```bash
python scripts/verify_dist.py              # data/dist にあるDB全部（実測 12期間で約20秒）
python scripts/verify_dist.py --id 2026H2  # 触った期間だけ。日次はこちら
```

見るもの:

| | 落ちると何が起きるか |
|---|---|
| `PRAGMA quick_check` | 転送やビルドが途中で切れたDBを配る |
| 目録との `size` / `version` / 収録範囲の一致 | `?v=` がずれ、古いキャッシュと混ざったDBを読む |
| 期間IDの連続（12期間そろっているか） | **検索が特定の期間だけ0件**になる（目録が痩せる） |
| `meta.topics` の指紋 | `/topic/<id>` が**別の争点の発言を黙って出す** |
| `page_size` / `journal_mode` | 遅くなる／`sql.js-httpvfs` から開けない |
| 分割規則が `build_db.py` と `query.ts` で同じか | サイトが**存在しないファイル名**を組み立てる |
| `speech_fts` があるか・争点語が入っているか | 検索が `no such table: speech_fts` で落ちる／`/topic` が全部0件 |
| **3経路のスモークテスト** | 下記 |

**スモークテストは FTS・2文字語・争点語の3経路を代表語で1件ずつ実際に引く。**
引き先が3つに分かれていて壊れ方も別々だからで（[PITFALLS.md](PITFALLS.md)）、
どれも「エラー」ではなく **0件**か**中身の入れ替わり**として出る。
争点語では引けた発言の**本文にその語が入っているか**まで見る
（`topic_id` のずれは件数が正しいまま中身だけ変わるので、これでしか捕まらない）。

**発言が1,000件未満の期間では代表語を引くところだけ飛ばす**（半期の開始直後＝
1月1日・7月1日は、まだ会議録が入っていないDBができる）。飛ばしたことはログに出る。

**★ 表があるかどうかは件数のしきい値の外で見る。** 「発言が少ないから引けない」と
「`speech_fts` が無いから引けない」は別の壊れ方で、後者は
`--no-fts` で作った配信DBをそのまま配ってしまう事故になる。
**構造の検査を件数の少なさで免除しないこと。**

**閉会中の実測は全体で4分半〜8分**（内訳は手順3が約1分半、手順4〜5が約2分、残り）。
上の目安の表は会期中の見積もりで、**会期中に伸びるのは手順3（取得）だけ**なので、
最悪でも表の合計を超えない。

**取り直す範囲を当月だけにしない。** 会議録は会議の当日には出ない。月末の会議が
翌月になって公開された分と、前月以前に入った訂正を、当月だけだと二度と拾えない
（`fetch_range.py` はその月を完成済みとして扱うので、`--force` の範囲から外れた
時点で永久に更新されなくなる）。前月1日から取り直すぶん、**7月は6月に、1月は前年12月に
触る**ので、当該期間に加えて前の期間も作り直す（`--id` は複数指定できる）。

### ★ 手順13: 配ったものの検算（`scripts/verify_published.py`）

```bash
# 日次はこれ（配信されている目録が手元と同じかも見る）
PUBLIC_DB_BASE=https://db.kokkai-timeline.com python scripts/verify_published.py --expect-local
# 手元から様子を見るとき（手元の目録は配信より古いのが普通なので --expect-local は付けない）
python scripts/verify_published.py --base https://db.kokkai-timeline.com
```

**手順7（`verify_dist.py`）と役割が違う。混ぜないこと。**

| | 見る対象 | 落ちたら |
|---|---|---|
| 手順7 | **配る前**の手元のファイル | 配らない（関門） |
| 手順13 | **配ったあと**の公開URLが返すもの | もう配ってある。**知らせるだけ** |

だから手順13は**ジョブの一番最後**に置いてある。ここで落ちても Pages のデプロイと
状態の書き戻しは済んでいて、実行だけが赤くなる（月1回の `gh run list` で気づく）。

見るもの: `Content-Type` / `Accept-Ranges` / `Content-Length` が目録の `size` と一致するか /
先頭16バイトが SQLite か / `page_size` が 8192 か / 配信中の目録が手元と同じか。

**`Accept-Ranges` はブラウザからは見えない**（R2 の CORS が公開していないので
`getAllResponseHeaders()` に出ない）。ここでしか確かめられない。

> **★ `User-Agent` を名乗ること。** 既定の `Python-urllib/3.x` は Cloudflare に
> **403 で弾かれる**（2026-08-19 実測）。消すと「配信が壊れている」ではなく
> 「検算が届かない」で落ちて、原因を取り違える。

### 走らせないもの

- **全期間の再構築** — 手動（`workflow_dispatch` の `all_years`）
- **`scripts/fetch_wikidata.py` を毎日** — SPARQL エンドポイントは共用資源。
  ただし**手元に `data/raw/wikidata_members.json` が無ければ必ず取る**
  （無いまま進むと `build_politicians.py` がその場で終了して、以降の手順に届かない）

---

## もう一つのワークフロー: CI（`.github/workflows/ci.yml`）

`site/**` を触った push と PR で `npm run check`（`astro check` + 回帰テスト）を回す。
**`src/lib/query.ts` を壊す変更はここで止める。**

**日次更新も同じ `npm run check` を配る前に通す**（手順8）。CI のほうが速く気づけるが、
CI は `site/**` を触ったときしか走らないので、**毎日必ず通る関門は日次更新側**にある。

- **`npm run build` は回さない。** ビルドには `data/politicians.json` と
  `data/dist/topics.json` が要るが、どちらも生成物でリポジトリに入っていない
  （R2 が保管庫）。再現するには R2 の資格情報が要り、関門としては重すぎる
- **`npm run check` はデータファイルを読まない。** `site-data.ts` の読み込みは
  ページの事前生成のときだけ走る。生成物が無い clean clone で通ることを確認済み
- `site/**` 以外（`scripts/` や `data/topics.json`）の変更では走らない。
  **Python 側に単体テストは無い。** そこの関門は日次更新の手順7
  （`verify_dist.py`）だけで、**出来上がった配信物を見る**形になっている
  （スクリプトそのものではなく、結果を検証する）

---

## ★ DBの差し替えとキャッシュ

期間DBのURLには **`?v=<中身の指紋>`** が付く（`manifest.json` の `version`）。
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
| 期間DB | `public, max-age=3600, s-maxage=31536000` |
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

### ★ `--content-type` は必ず明示する（2026-08-19）

```bash
aws s3 cp "$db" "s3://$PUBLIC_BUCKET/$(basename "$db")" $R2 \
  --content-type "application/vnd.sqlite3" \
  --cache-control "public, max-age=3600, s-maxage=31536000"
```

**省くと `aws-cli` が拡張子から推測する** ＝ **ランナーの mime データベース次第**で
付いたり付かなかったりする。実際、2026-08-18 の日次が差し替えた `kokkai-2026H2.db`
だけ `Content-Type` が落ちた（8/12 に上げた11本には `application/vnd.sqlite3` が
付いていた）。**ヘッダが欠けてもエラーにはならず、日次も通る。**

同じ日に Chrome の1つの窓口だけ 2026H2 で `file is not a database` が出ている
（[ROADMAP.md](ROADMAP.md)「未解決の不具合」）。**因果は証明できていない**が、
容疑者を1つ減らすために固定した。気づく手は手順13（`verify_published.py`）。

**既に配ってあるものを直すとき**は、中身を上げ直さずメタデータだけ差し替える:

```powershell
aws s3 cp "s3://kokkai-timeline/kokkai-2026H2.db" "s3://kokkai-timeline/kokkai-2026H2.db" `
  --endpoint-url $R2 --metadata-directive REPLACE `
  --content-type "application/vnd.sqlite3" `
  --cache-control "public, max-age=3600, s-maxage=31536000"
```

**★ 中身が変わらないので `?v=` も変わらず、エッジは `s-maxage` で1年握ったまま。**
打ち直しただけでは配信に出ない。そのURLをパージするか、
**当期のDBなら翌日の日次で別の `?v=` になるのを待てばよい**（毎日中身が変わるため）。

## 争点語を変えるとき

**語を足すだけなら、全期間のDBの作り直しは要らない**（2026-08-21）。
**消す・書き直す・別表記を足すときは要る**（下の表）。

> **編集は運営コンソールから**（`python scripts/admin.py` の「争点語」タブ）。
> 候補（週次トレンド・頻出語500件のうち未登録のもの）と、**その語が全期間で
> 何件当たるか**がその場で出る（配信済みDBを検索と同じ経路で引く。実測 4〜17ms）。
> id の採番・半角英数・引けない語・二重計上はツールが止める。
> **書き換えるのは `data/topics.json` だけ**で、集計は下のコマンドを手で回す。

```bash
python scripts/build_topics.py     # dist/topics.json・trending.json を作り直すだけ
```

配信済みDBが持っていない語には `dist/topics.json` の `indexed` が偽で付き、
サイトは `topic_hit` を使わず**普通の検索経路**（2文字語は `word` / 3文字以上は FTS）で出す。
実測で 82語のうち80語は両経路の結果が完全に一致する（食い違うのは別表記を持つ2語）。
**正しさは変わらず、3文字以上の語だけ 3.3倍遅くなる。**

`data/topics.json` の `id` は**不変の識別子**。追加は未使用の最大値+1、削除しても
再利用しない。`build_db.py` / `build_topics.py` は id の無いリストを受け付けない。

### 作り直しが要るのは「足す」以外

| 変えたもの | 作り直しまでの間 | なぜ |
|---|---|---|
| **足しただけ** | 足した語だけ検索経路（正しい・遅い） | 古い期間DBに `topic_hit` が無いだけ |
| **`variants` を足した・変えた** | その語が**代表表記しか出ない** | 検索経路は AND しか無く、別表記を合算できない。ページにその旨を出す |
| **`term` を書き直した（id はそのまま）** | **全部**が検索経路（正しい・遅い） | 配信済みDBは**古い語**のヒットを持っている。指紋が合わないので全部を信じない |
| **語を消した** | **全部**が検索経路（正しい・遅い） | 消した語の表記が手元に無く、指紋を照合できない |

下2つで全部が落ちるのは**照合できないものを信じない**ため。遅くなるだけで、
間違ったものは出ない（`stamp_indexed()` が期間ごとに照合して理由をログに出す）。

```bash
python scripts/build_topics.py
python scripts/build_db.py --split --page-size 8192     # 全期間（実測 約6分・12本）
```

CI からやるなら `workflow_dispatch` を `all_years=true` で実行する。

### 速くしたくなったら（任意）

足した語を `topic_hit` に載せる作業は**いつやってもよい**。次に全期間を作り直した
ときに自動で載る。**2文字語は載せても速くならない**ので、放っておいてよい
（実測: word 経路 451req ≒ 争点語経路 471req）。

> **目録（`manifest.json`）が期間ごとの争点語を持っている。** `{"ids": "1-82", "fp": …}`。
> ここが実物とずれていると、持っていない語を `topic_hit` で引いて**0件**になる。
> DBを作り直さずに目録だけ直すなら `python scripts/build_db.py --manifest-only`。
>
> ★ **手元の `data/dist` は当期だけ配信より古い**（日次が毎日作り直しているため）。
> そのまま上げると**その期間の `size` / `version` / 収録範囲が過去の世代に巻き戻る** ——
> `?v=` が戻り、エッジに残っていれば1か月前のDBを配る。`--manifest-only` は世代が
> 変わる期間を警告に出すので、**当期が出たら上げた後に日次を1回回すこと**
> （`gh workflow run daily.yml`）。**2026-08-21 に実際に踏んだ。**

## ★ 分割の単位を変えるとき

配信DBは半期で割ってある。単位は2か所に書いてあり、**必ず両方を同時に変える**:

- `scripts/build_db.py` の `period_of()`（`--period half|year`）
- `site/src/lib/query.ts` の `periodOf()`

片方だけ変えると、サイトが**存在しないファイル名を組み立てて検索が丸ごと止まる**。
変えたら全期間を作り直し、R2 の古いファイルを消すこと（目録から消えても R2 には残る）。

判断の材料は `docs/DECISIONS.md`「DBは半期ごとに分割する」。
**1ファイル 512MB を超えると黙って CDN キャッシュから外れる**のが唯一の制約で、
日次はその手前（480MB）で失敗するようにしてある。

---

## Cloudflare 側でやること

**ここはダッシュボードでの手作業。** 全部やり終えるまでワークフローは動かない。

### 1. R2 バケットを2つ作る

| バケット | 用途 | Location | Storage class | 公開 |
|---|---|---|---|---|
| `kokkai-timeline` | 期間DB（約2.4GB）と `manifest.json` | APAC（Automatic で可） | Standard | **公開**（カスタムドメイン） |
| `kokkai-timeline-state` | 生データ（約1.2GB）・議員マスタ | Automatic で可 | Standard | 非公開 |

**Jurisdiction（`Specify` の EU など）は選ばない。** データ所在地の法規制向けの設定で、
作成後は変更できない。地域ヒントとは別物。

**状態バケットには Custom Domain も CORS も設定しない。** 手順3・手順4は
**公開バケットだけ**の作業。状態バケットを読むのは GitHub Actions だけで、
認証付きの S3 API（`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`）から触るため、
公開の口も、ブラウザ向けの CORS も要らない。付ければ生データ1.2GBを
誰でも取れるようにするだけになる。

> 紛らわしいので明示しておくと、**公開バケットへの書き込みもカスタムドメインは通らない。**
> 日次更新は両方のバケットに S3 エンドポイント経由で書く。
> `db.kokkai-timeline.com` と CORS は、**サイトが期間DBを読むためだけ**に要る。

分けるのは、公開バケットにカスタムドメインを付けると**バケット全体が見える**ため。
1.2GBの生データを配る必要はない。合計は約3.7GBで、無料枠10GBに収まる。

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
①期間DBが Range で常時読まれる（取り出し料金が乗る）
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

> ### ★ 公開URLを `Origin` 無しで引かない（2026-08-21 に踏んだ）
>
> R2 は **`Origin` の付いた要求にだけ** CORS ヘッダを返し、応答に `Vary: Origin` を
> 付けない。**`Origin` 無しの要求が先にエッジのキャッシュを埋めると、
> `Access-Control-Allow-Origin` の無い応答がそのまま全ブラウザに配られる。**
> 目録で起きるとサイトが丸ごと止まる（`Failed to fetch`。どの期間DBにも辿り着けない）。
>
> **発生源は検算だった。** 日次は「配る → 検算する」の順で、`verify_published.py`
> （Python の `urllib`）が `Origin` を送っていなかったため、**デプロイのたびに
> 5分間の穴**が開いていた（目録は `max-age=300`）。いまは `Origin` を付けて引き、
> 返ってきた CORS ヘッダまで検算に含めている。`curl` で手当てするときも
> `-H 'Origin: https://kokkai-timeline.com'` を付けること。

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
> **②の閾値を下げすぎないこと。** 1回の検索は1ファイルあたり平均80・最悪187リクエストの束で、
> 12期間を並列に引くので**1回の検索で約500、重い語だと1,000を超える**。
> さらに「さらに読み込む」で積み増す。**10秒あたり2,000未満にすると普通の利用者が落ちる。**
> その閾値だと単一IPで平均100req/s は通ってしまうので、**止まるのは事故と雑なスクレイパまで。**
>
> **③をやるなら**（＝実際に課金が動いてから）、`Cache Level: Ignore Query String` は
> Page Rules 経由なら **free でも使える**。ただし `?v=` が効かなくなるので、
> 単体でやると**DBを差し替えた瞬間にエッジが古いまま1年固まる**。必ずセットで:
>
> - 版をオブジェクトキーに入れる（`kokkai-2025H1-<指紋>.db`）か、
> - 差し替えた期間のURLをワークフローから purge する（API 1本）
>
> そのうえで WAF カスタムルール（free で5本）で
> `http.host eq "db.kokkai-timeline.com" and http.request.uri.query ne ""` を Block すれば、
> ゴミ付きリクエストはエッジより手前で落ちて **R2 に1回も届かない。**
> 代償は「CDNのパージが要らない」という現構成の利点で、
> **purge の失敗が新しい壊れ方（DBは新しいのにエッジが古い）を生む**。
>
> **実測がこの判断を支えている**（下の「費用」）。**月1,000円に届かせるには
> 約2,800万リクエスト/月**（＝11 req/s を30日間）が要るのに、いまは枠の 0.07%。
> **1,000倍以上の跳ね上がりが要る**ので、①の通知が鳴らないほうがおかしい。
> ②を入れて普通の利用者を弾く危険を負う理由が、実測からも無い。
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

`connect-src` に期間DBのホストが入っているので、**`PUBLIC_DB_BASE` を変えたら
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
このサイトは1,700ファイルを超える**（議員ページだけで約1,100枚）。**将来も増え続ける。**
名前を入れた時点でプロジェクトだけは作られるので、アップロードは Wrangler でやる。

★ 下の `npx --yes wrangler@4` は**この初期構築（手元で1回）だけ**。
**日次更新では使っていない** —— あれは実行のたびに npm から最新の 4.x を落として、
Cloudflare のトークンが見えるプロセスとして走らせることになる。
`daily.yml` は `site` の devDependency に**版を固定した** wrangler を
`npm ci`（lockfile の完全性検査つき）で入れ、`npx --no-install wrangler` で呼ぶ。
**日次側をここに合わせて戻さないこと**（docs/SECURITY.md）。

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

### 7. 連絡先を Email Routing で作る

`info@kokkai-timeline.com` を Gmail へ転送する。**サーバは増えない**（Cloudflare 側の
設定だけで動くので「常時稼働プロセスを持たない」に触れない）。Gmail 側でエイリアスを
作れば、返信もこのアドレスから出る。

- **サイトの表示は `site/src/lib/operator.ts` の1か所だけ**（`/disclaimer` `/privacy`
  `/about` がそこを見る）。アドレスを変えるときは**転送ルールと Gmail のエイリアスも一緒に**
- 訂正依頼の窓口なので、**届かない状態は「窓口が無い」のと同じ**
  （名誉毀損の抗弁としても弱くなる。[SCOPE.md](SCOPE.md)）
- ドメインを移したり作り直したりしたら、**必ず1通送って着信を確かめる**

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

# cache-control は日次ジョブと同じにする。**immutable は付けない**
# （エッジは s-maxage で1年。上の「immutable は付けない」）
Get-ChildItem data\dist\kokkai-*.db | ForEach-Object {
  aws s3 cp $_.FullName "s3://kokkai-timeline/$($_.Name)" --endpoint-url $R2 `
    --cache-control "public, max-age=3600, s-maxage=31536000"
}
aws s3 cp data\dist\manifest.json s3://kokkai-timeline/manifest.json --endpoint-url $R2 `
  --cache-control "public, max-age=300"
```

### 手で Pages に上げるとき（Actions を通さない場合）

GitHub より先に本番で確かめたいときに使う。

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

**確認は件数で取る。** 全期間で `ＡＩ` 5,412 / `Ｇ７` 4,439 / `ＤＸ` 3,039 /
`ＧＸ` 2,692 / `ＥＵ` 2,579（2026-08-03 実測）。0件になったら、期間DBか目録の
どちらかが古いままか、パージが効いていない。

そのあと Actions から `workflow_dispatch` で1回手動実行して、通ることを確かめる。

### 初回実行で特に見るところ

- **`配信物を検証する` の手順で落ちないか。** 大きさ・目録の抜け・3経路の
  スモークテストはここで全部見る。落ちた行がそのまま原因を指す
- **`議員ID台帳が増えていたらコミットする` が push できるか。**
  ここが失敗したら放置しない。台帳を失うと公開後のURLが全部変わる。
  **公開より前の手順なので、ここで落ちるとその日は何も配られない**（それが正しい）
- **目録の `periods` が12期間そろっているか。** CI は触った期間しかDBを置かないので、
  前回の目録を引き継げていないと1〜2期間に痩せる（検証が落とす）

---

## 失敗したとき

GitHub は**定期実行が失敗すると、そのワークフローファイルを最後に変更した人にメールする**。
これで「メールが飛ぶだけでよい」という要件は満たしている。追加の通知は入れていない。

数日止まっても壊れない。次に走ったときに当月を取り直すので追いつく。

| 症状 | 見るところ |
|---|---|
| `aws s3 ls` が AccessDenied | **正常。** バケット一覧は管理操作で、Object Read & Write には含まれない。`aws s3 ls s3://<バケット>/` で確かめる |
| 検索が特定の期間だけ0件 | 目録の `periods` が痩せていないか。R2 の目録を復元できているか |
| 検索が急に10倍遅い | どれかのDBが 512MB を超えてキャッシュから外れた（`cf-cache-status`） |
| 検索がまったく動かない | R2 の CORS（手順3）。ブラウザのコンソールに CORS エラーが出る |
| サイトは出るが発言が出ない | `PUBLIC_DB_BASE` の値、カスタムドメインの疎通 |
| 議員IDの台帳が push できない | **放置しない。** 台帳を失うと公開後のURLが全部変わる |
| `配信物を検証する` が落ちた | **その日は何も配られていない**（公開より前の手順）。落ちた行が原因。直して `workflow_dispatch` で回し直す |
| 検証は通るのに実機で0件 | 検証は**手元にあるDBだけ**を見る。触っていない期間は目録の記載を引き継いだだけなので、R2 の実物と食い違っていることがある |

---

## 費用

直近30日の実測（2026-08 時点・公開バケット ＋ 状態バケット）:

| | 使用量 | 無料枠 | 使用率 |
|---|---:|---:|---:|
| **R2 ストレージ** | **約3.7 GB** | 10 GB | **37%** |
| R2 Class A（書き込み） | 約2千回 | 月100万回 | 0.2% |
| R2 Class B（読み出し） | 約7千回 | 月1,000万回 | **0.07%** |
| R2 下り | 0円（R2は下り無料） | — | — |
| Cloudflare Pages | 日次なら月30ビルド | 月500 | 6% |
| GitHub Actions | public なら実質無制限 | — | — |

**実際にかかるのはドメイン代（月250円）だけ。** 予算は月1,000円。

★ **監視すべきはリクエスト数ではなくストレージ。** 半期DBが増えるぶん
年 0.6〜0.9GB 育つので、10GB に当たるのはざっと8年後。ここが最初に課金へ届く。
