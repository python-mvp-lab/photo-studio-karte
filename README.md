# Photo Karte（photo-studio-karte）

LINE公式アカウントでのお客様とのやり取りを取り込み、AIで整理して **「顧客カルテ」をMarkdownで保存する**、写真スタジオ向けのローカル顧客管理アプリです。

個人で写真スタジオを営むカメラマン（成人式・ブライダル・七五三・家族写真など）が、PCに不慣れでもボタン操作だけで使えるように作りました。

## 解決したかった課題

| 課題 | 解決方法 |
|---|---|
| LINE公式アカウントのチャット履歴が約半年で消える | トーク履歴CSVを取り込み、PC内にMarkdownとして無期限に保存 |
| リピーターか見分けられず、前回の依頼内容も分からない | 電話番号・名前・LINE名などで「同じお客様」の候補を自動で提示し、お客様IDで紐づけ |
| 予約内容はノートに手書き。やり取りの雰囲気が残らない | 原文を全文保存。AIが3行まとめ・雰囲気タグ・撮影前の準備メモを作成 |
| 顧客データをチャットボットやLP改善に活かしたい | 構造化Markdown（YAML front matter）＋個人情報を伏せたコピーの書き出し |

## 主な機能

- 📥 **CSVの取り込み**
  - 文字コード（UTF-8 / Shift_JIS / UTF-16）と、見出しの行・列の並びを自動で判定します。
  - 形式が分からないCSVでも、全文をAIに渡して処理を続けます。
- 🤖 **AIによるカルテの下書き**（OpenAI Responses API ＋ Structured Outputs）
  - 項目の抽出、3行まとめ、雰囲気タグ、撮影前の準備メモ、次回のご案内文の下書きを作ります。
- 🔁 **リピーターの照合**
  - 点数を付けて同じお客様の候補を出し、人がボタンで確定する半自動の方式です。
  - 既存のご依頼の更新（やり取りの統合と重複除去）や、新しいご依頼の追加（保存済みのやり取りを除く）に対応します。
- 📋 **一覧と絞り込み**
  - キーワード・撮影の種類・年・シーズン（桜／紅葉）・進み具合・リピーターで絞り込めます。
- 📅 **ホームで予定を確認**
  - 撮影予定・撮影日が未定のご依頼・最近のカルテを表示します。
- 💬 **AIへの質問**
  - 「よく使う質問」ボタン6つがあり、そのうち5つはAIを使わず即座に答えます。
  - 自由な質問では、根拠にしたカルテを一緒に表示します。
- 💾 **自動バックアップ**
  - 保存・更新・削除のたびにZIPで保存し、新しいものから30個を残します。
- 🕶️ **個人情報を伏せたコピーの書き出し**
  - 名前・電話番号・メールアドレス・お子さまの名前・郵便番号を伏せます。

## 技術スタック

- Python 3.13 / Streamlit 1.65
- OpenAI API（既定のモデルは `gpt-6.1-sol`。サイドバーで切り替えできます）
- pydantic（データモデル・AI出力のスキーマ）、PyYAML、pandas
- pytest（ロジック層のテスト67件）＋ Streamlit AppTest（画面の自動確認）

## 設計のポイント

- **保存形式はMarkdown＋YAML front matterで、データベースは使いません**
  - 1ご依頼＝1ファイル（`data/customers/YYYYMMDD_HHMM_お名前.md`）です。
  - 人が読めて、そのままチャットボットの学習データやRAG（検索して答えるAI）の材料にできます。
- **UIとロジックを分けています**
  - `app.py` の前半はStreamlitに依存しないロジック層、後半がUI層です。
  - `test_app.py` ではロジック層だけをテストします。
  - AIのクライアントは差し替えられるので、テストでは偽のクライアントを使います。
- **AIの誤りを前提にしています**
  - AIは下書きまでを担当し、人が確認・修正してから保存します。
  - 質問への回答では、実在しないカルテIDを取り除いてから根拠として表示します。
  - AIで答えられることでも、予定や未入金の確認はルールで計算します（正確で無料）。
- **プライバシーに配慮しています**
  - APIキーは画面を開いている間だけ記憶し、保存しません。
  - Streamlitは `localhost` 限定で動かし、OpenAIには `store=False` で送ります。
  - `data/` はGitの管理対象外です。
- **スタジオ名・アプリ名は設定ファイルで切り替えます**
  - 公開するコードでは架空の名前（「studio SAMPLE」「Photo Karte」）にしてあります。
  - 実際の名前は、公開しない `studio_settings.toml` に書きます（見本は `studio_settings.example.toml`）。

## ディレクトリ構成

```
├── app.py                    # アプリ本体（ロジック層 / UI層）
├── test_app.py               # ロジック層のテスト
├── requirements.txt
├── specification.md          # 仕様書
├── manual.md                 # 使い方マニュアル（非エンジニア向け）
├── 初回セットアップ.command   # ダブルクリックで環境構築（macOS）
├── 起動.command               # ダブルクリックで起動（macOS）
├── studio_settings.example.toml  # スタジオ名・アプリ名の設定の見本
├── .streamlit/config.toml    # テーマ・localhost限定の設定
├── dev_tools/
│   ├── ui_check.py           # 全画面と取り込みの流れの自動確認（AppTest・偽のAIを使用）
│   └── make_samples.py       # サンプルカルテの作成
└── sample_data/              # 架空の人物のダミーデータ
    ├── line_chat_sample.csv
    └── customers/*.md
```

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

スタジオ名とアプリ名を変える場合は、見本をコピーして書き換えます（このファイルはGitの管理対象外です）。

```bash
cp studio_settings.example.toml studio_settings.toml
```

## サンプルデータで試す

`sample_data/` には、架空の人物のカルテ6件と、取り込み用のサンプルCSVが入っています。

```bash
cp -R sample_data demo_data
KARTE_DATA_DIR=demo_data streamlit run app.py
```

- 「📥 取り込み」では `sample_data/line_chat_sample.csv` を使えます（取り込みには OpenAI APIキーが必要です）。
- 一覧・ホーム・AIを使わない質問ボタンは、APIキーがなくても動きます。

## テスト

```bash
pytest -q                                  # ロジック層のテスト
python dev_tools/ui_check.py "$PWD"        # 全画面の自動確認（作業用の一時フォルダで行います）
```

## データとプライバシーについて

- 実際のお客様データは `data/` に保存され、`.gitignore` でリポジトリから除外しています。
- 実際のスタジオ名を書く `studio_settings.toml` も、リポジトリから除外しています。
- このリポジトリに含まれるお客様情報は、すべて架空のダミーデータです。
