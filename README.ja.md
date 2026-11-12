# 4D プラグインのパイプラインと開発ガイド

[English](README.md) | 日本語

正規の[エージェントスキル](SKILL.md)と
[パイプライン生成ガイド](docs/plugin-pipeline.md)から始めてください。
過去の [example](https://github.com/miyako/4d-plugin-template/tree/example)
ブランチはこの契約と同期していません。リリース手順をコピーしないでください。

正規の[プラグイン開発ガイド](.github/instructions/plugin-dev.instructions.md#required-github-secrets-for-release)には、利用者が実行する同梱の `scripts/set-release-secrets.sh` によるリリース認証情報の設定が含まれます。

正規の[ネイティブプラグインのパイプライン生成](docs/plugin-pipeline.md)は、
このリポジトリの `main` が管理します。追跡対象のテンプレート、制御された設定、
共通の受け入れ検証と決定的な生成チェックを提供します。リリース用アクションは
**Bump version**（ソースのみ更新）と **Build**（バージョン・タグ入力なし）の2つです。
別の **Build and Test** ワークフローは署名なしの検証を行います。
Build はネイティブビルド前に不変の実行対象リビジョンとバージョンの公開可否を
事前確認し、パッケージ検証後にのみ `vVERSION` を公開します。
`example` ブランチは実行可能なひな形として別途更新します。
古い `miyako-4d-plugin-tutorial` ブランチは履歴であり、生成元ではありません。

**Canonical checks** ワークフローはこのソースリポジトリをテスト・lint します。
プラグインのビルド・署名・公開は行いません。

実際のチュートリアルは [4d-plugin-example](https://github.com/miyako/4d-plugin-example)
にあり、この正規ソースをサブモジュールとして固定します。リポジトリ名や利用側の
URL を変更する前に[リポジトリの互換性に関する注意](docs/plugin-pipeline.md#repository-identity-and-submodule-compatibility)
を参照してください。
