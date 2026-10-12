#!/bin/bash
# 顧客カルテアプリを起動します（ダブルクリックしてください）

cd "$(dirname "$0")" || exit 1

# 別のMacからフォルダをコピーした場合などは、仮想環境が使えないので作り直してもらう
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import streamlit" >/dev/null 2>&1; then
  echo "まだ準備ができていません。先に「初回セットアップ.command」をダブルクリックしてください。"
  read -r -p "Enter キーを押すと閉じます"
  exit 1
fi

source .venv/bin/activate

# Streamlit が初回に聞いてくるメールアドレスの入力を省略する
mkdir -p "$HOME/.streamlit"
if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
  printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
fi

echo "=========================================="
echo "  顧客カルテアプリを起動しています"
echo "  ブラウザが自動で開くまでお待ちください"
echo ""
echo "  ※ アプリを使っている間は、この画面を閉じないでください"
echo "  ※ 終わるときは、この画面を閉じればOKです"
echo "=========================================="
streamlit run app.py
