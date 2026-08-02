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

| バケット | 用途 | 公開 |
|---|---|---|
| `kokkai-timeline` | 年DB（約2.1GB）と `manifest.json` | **公開**（カスタムドメイン） |
| `kokkai-timeline-state` | 生データ（約1.2GB）・議員マスタ・語彙 | 非公開 |

分けるのは、公開バケットにカスタムドメインを付けると**バケット全体が見える**ため。
1.2GBの生データを配る必要はない。合計 3.3GB で、無料枠10GBに収まる。

### 2. 公開バケットにカスタムドメインを付ける

**ドメインが1個要る。** 条件は「**同じ Cloudflare アカウントにゾーンとして
登録されていること**」（Cloudflare の要件）。Registrar で取れば取った時点でゾーンになる。
他所で取ったものはネームサーバを向けるか、partial (CNAME) setup で登録する。

R2 → バケット → Settings → Public access → Custom domain。
例: `db.<ドメイン>`。ここが `PUBLIC_DB_BASE` になる。

**カスタムドメインにするのは CDN キャッシュを効かせるため。** Cloudflare は
`r2.dev` を「レート制限があり開発用途に限る」とし、キャッシュを含む機能は
カスタムドメイン経由でしか使えないと明記している。1検索で約80リクエスト飛ぶので、
`r2.dev` のままでは実用にならない。

**要るのは1個だけ。** サイト側（Pages）は `<プロジェクト名>.pages.dev` が無料で付くので、
ドメインを当てるかどうかは任意。当てるならサブドメインを分ければ1個で足りる:

| ホスト | 向き先 | 必須か |
|---|---|---|
| `db.<ドメイン>` | R2（公開バケット） | **必須** |
| `<ドメイン>` / `www.<ドメイン>` | Pages | 任意（`pages.dev` でもよい） |

> パイプラインが通るかの確認だけなら `r2.dev` でもできる。`PUBLIC_DB_BASE` は
> GitHub の Variable なので、あとで差し替えて再ビルドすれば切り替わる。
> **ただしそのまま公開しない。**

### 3. 公開バケットに CORS を設定する

サイト（Pages）とDB（R2）は別オリジンになる。**これが無いと検索が何も動かない。**
R2 → バケット → Settings → CORS policy:

```json
[
  {
    "AllowedOrigins": ["https://<サイトのドメイン>"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["range"],
    "ExposeHeaders": ["content-range", "content-length", "etag"],
    "MaxAgeSeconds": 86400
  }
]
```

`ExposeHeaders` に `content-range` が要る。sql.js-httpvfs は Range で読むので、
これが見えないとファイルの大きさを判断できない。

### 4. R2 の S3 API キーを作る

R2 → Manage R2 API Tokens → Create API token。権限は **Object Read & Write**、
対象は上の2バケット。出てくる Access Key ID / Secret Access Key を控える。

### 5. Cloudflare Pages のプロジェクトを作る

Workers & Pages → Create → Pages → **Direct Upload**（Git連携ではない）。
名前は任意。ここが `PAGES_PROJECT`。

> Git連携にしないのは、サイトのビルドに `data/politicians.json` などの
> 生成物が要るため。それらはリポジトリに入れていないので、Cloudflare 側では
> ビルドできない。GitHub Actions でビルドして成果物だけ送る。

### 6. Pages 用の API トークンを作る

My Profile → API Tokens → Create Token。権限は **Account → Cloudflare Pages → Edit**。

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

| 名前 | 例 |
|---|---|
| `R2_PUBLIC_BUCKET` | `kokkai-timeline` |
| `R2_STATE_BUCKET` | `kokkai-timeline-state` |
| `PUBLIC_DB_BASE` | `https://db.<ドメイン>` |
| `PAGES_PROJECT` | `kokkai-timeline` |

### リポジトリを public にするか

**Actions の無料枠は public リポジトリなら実質無制限、private だと月2,000分。**
このジョブは1回20〜60分なので、private だと月600〜1,800分。**入るが余裕は無い。**
会期中に取得が伸びると超える月が出る。public にするのが素直。

---

## 初回の流し込み

ワークフローは「R2 に前回の状態がある」前提で書いてあるが、
無ければ落とさずに進む（`|| true`）。ただし**生データの初回投入だけは手元からやる**
ほうが速い。1.2GB を取り直すと5時間かかる。

```bash
aws s3 sync data/raw/speeches s3://kokkai-timeline-state/raw/speeches \
  --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com
aws s3 cp data/politicians.json s3://kokkai-timeline-state/politicians.json --endpoint-url ...
aws s3 cp data/words.json       s3://kokkai-timeline-state/words.json       --endpoint-url ...

for f in data/dist/kokkai-*.db data/dist/manifest.json; do
  aws s3 cp "$f" "s3://kokkai-timeline/$(basename "$f")" --endpoint-url ...
done
```

そのあと Actions から `workflow_dispatch` で1回手動実行して、通ることを確かめる。

---

## 失敗したとき

GitHub は**定期実行が失敗すると、そのワークフローファイルを最後に変更した人にメールする**。
これで「メールが飛ぶだけでよい」という要件は満たしている。追加の通知は入れていない。

数日止まっても壊れない。次に走ったときに当月を取り直すので追いつく。

| 症状 | 見るところ |
|---|---|
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
