# ライセンスが及ぶ範囲

**[LICENSE](LICENSE) は MIT。ただし対象はこのリポジトリに入っているものだけ。**

`LICENSE` 本体にこの説明を書かないこと。GitHub のライセンス判定は既知の
ライセンス文との一致率で見るので、追記があると「未検出」になり、
リポジトリ画面に MIT と表示されなくなる（2026-08-06 に実際にそうなった）。

## MIT で提供するもの

- `scripts/` — 取得・DB構築・名寄せ・集計（Python）
- `site/` — 公開サイトの実装（Astro / TypeScript）
- `prototype/` — 性能計測に使った実験台
- `docs/` `README.md` `CLAUDE.md` — ドキュメント
- `data/*.json` の5ファイル — 手で維持している資産
  （`topics.json` `politician_ids.json` `party_map.json` `party_overrides.json`
  `topic_denylist.json`）

## MIT が及ばないもの

**国会会議録の本文。** このリポジトリには**1バイトも入っていない。**
`data/raw/` と `data/*.db` `data/dist/` は `.gitignore` で、実体は
実行時に国立国会図書館の API から取る。**こちらが再配布していないので、
こちらがライセンスを付ける立場にない。**

出典: [国立国会図書館 国会会議録検索システム](https://kokkai.ndl.go.jp/api.html)

会議録データの扱いをこのプロジェクトがどう整理しているかは
[docs/SCOPE.md](docs/SCOPE.md)「法的な整理」にある。要点は3つ:

- 発言そのものは公的記録で、**著作権法40条1項**により政治上の演説等は自由に利用できる
- ただし**データベースとしての著作権は国立国会図書館に帰属する**ので、
  丸ごとミラーして代替物を作る形は取らない
- **全レコードに原典URLを付ける**（著作権法48条の出所明示義務）

**この整理は専門家のレビューを経ていない。** 収益化・評価機能の追加など、
リスクプロファイルが変わる変更を加えるときは弁護士に確認すること。

---

## English

The [LICENSE](LICENSE) (MIT) covers only what is contained in this repository:
the Python scripts, the Astro site implementation, the documentation, and the
five hand-maintained JSON files under `data/`.

**It does not cover the parliamentary record itself, which is not part of this
repository.** No transcript text is committed here (`data/raw/`, `data/*.db`
and `data/dist/` are gitignored); it is fetched at run time from the National
Diet Library's Diet Record Search System API. Because this project does not
redistribute that data, it is not in a position to license it. See
[docs/SCOPE.md](docs/SCOPE.md) for how the project handles it.
