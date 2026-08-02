# 日次更新の運用（ROADMAP §3.4）

`.github/workflows/daily.yml`。**これがこのプロジェクト唯一の定期実行**で、
落ちても数日放置できるように作ってある（会議録は消えないので、次に走れば追いつく）。

---

## 何が毎日走るか

| 手順 | やること | 目安 |
|---|---|---|
| 1 | R2 から生データ・議員マスタ・語彙・目録を戻す | 1〜2分 |
| 2 | **当月**の会議録を `--force` で取り直す | 会期中は10〜30分（NDLへ3秒間隔） |
| 3 | 集計の材料になる単一DBを作り直す（`--no-fts`） | 3〜5分 |
| 4 | 議員マスタ・争点語の集計・週次トレンド | 5〜15分 |
| 5 | **当年**の配信用DBを作り直す | 30〜60秒 |
| 6 | 年DBと目録を R2 へ | 1〜2分 |
| 7 | サイトをビルドして Pages へ | 1〜2分 |

過去年のDBは触らない。**変わるのは当年の1ファイルだけ**なので、
CDN キャッシュは過去年ぶんが効き続ける。

### 走らせないもの

- **`scripts/build_words.py`（2文字語の語彙）** — 下の「語彙を作り直すとき」を読むこと
- **全年の再構築** — 手動（`workflow_dispatch` の `all_years`）

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
だからDBは `immutable` で長く握らせてよく、**短くするのは `manifest.json` だけ**。

それでも取りこぼす場合の保険として、問い合わせが失敗したら
**目録を取り直してワーカを作り直し、1回だけやり直す**（`site/src/lib/db.ts`）。

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

安全網として、各年DBの `meta` テーブルに語彙の指紋を入れてあり、
`manifest.json` にも載る。食い違うと `build_db.py` が警告を出し、
ワークフローは**その場で失敗する**（「語彙が年をまたいで揃っているか」の手順）。

同じことが `data/topics.json`（争点語）にも当てはまる。争点語を足したら全年を作り直す。

---

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

Workers & Pages → Create → Pages → **Direct Upload**（Git連携ではない）。
名前は `kokkai-timeline`。ここが `PAGES_PROJECT`。

> Git連携にしないのは、サイトのビルドに `data/politicians.json` などの
> 生成物が要るため。それらはリポジトリに入れていないので、Cloudflare 側では
> ビルドできない。GitHub Actions でビルドして成果物だけ送る。

初回のデプロイが済んだら、Pages → Custom domains → `kokkai-timeline.com` を当てる。
`www` は使わない（サイト側の正規URLを apex に揃えてある。
`astro.config.mjs` の `site` と `<link rel="canonical">`）。
`www` からのアクセスを拾いたければ、Redirect Rule で apex に 301 する。

### 6. Pages 用の API トークンを作る

**R2 のトークンとは置き場所が違う。** My Profile → API Tokens → Create Token。

| 項目 | 値 |
|---|---|
| Token name | `github-actions-pages-deploy` |
| Permission | **Account → Cloudflare Pages → Edit** |

---

## GitHub 側でやること

Settings → Secrets and variables → Actions。

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
aws s3 cp data\politicians.json s3://kokkai-timeline-state/politicians.json --endpoint-url $R2
aws s3 cp data\words.json s3://kokkai-timeline-state/words.json --endpoint-url $R2

# cache-control は日次ジョブと同じにする（DBはURLに世代が入るので immutable）
Get-ChildItem data\dist\kokkai-*.db | ForEach-Object {
  aws s3 cp $_.FullName "s3://kokkai-timeline/$($_.Name)" --endpoint-url $R2 `
    --cache-control "public, max-age=31536000, immutable"
}
aws s3 cp data\dist\manifest.json s3://kokkai-timeline/manifest.json --endpoint-url $R2 `
  --cache-control "public, max-age=300"
```

**確認**: ブラウザで `https://db.kokkai-timeline.com/manifest.json` を開く。
JSONが見えればカスタムドメインと公開設定が通っている。
403/404 ならその先へ進んでも無駄なので、ここで直す。

そのあと Actions から `workflow_dispatch` で1回手動実行して、通ることを確かめる。

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
