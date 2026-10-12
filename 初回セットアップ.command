#!/bin/bash
# 顧客カルテアプリの初回セットアップ（最初に1回だけダブルクリックしてください）

cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "  顧客カルテアプリの準備をしています"
echo "=========================================="

if ! command -v python3 >/dev/null 2>&1; then
  echo ""
  echo "Python が見つかりませんでした。"
  echo "manual.md の「1-1. Python を入れる」の手順を行ってから、もう一度ダブルクリックしてください。"
  read -r -p "Enter キーを押すと閉じます"
  exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo ""
  echo "Python のバージョンが古いようです（3.11 以上が必要です）。"
  echo "manual.md の「1-1. Python を入れる」の手順で、新しい Python を入れてください。"
  read -r -p "Enter キーを押すと閉じます"
  exit 1
fi

# --clear：別のMacからコピーした古い仮想環境が残っていても、作り直す
python3 -m venv --clear .venv || { echo "仮想環境を作れませんでした。"; read -r -p "Enter キーを押すと閉じます"; exit 1; }
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt || { echo "ライブラリを入れられませんでした。インターネット接続を確認してください。"; read -r -p "Enter キーを押すと閉じます"; exit 1; }

echo ""
echo "=========================================="
echo "  準備が完了しました！"
echo "  次からは「起動.command」をダブルクリックしてください。"
echo "=========================================="
read -r -p "Enter キーを押すと閉じます"
