"""AppTest で全画面と取り込みの流れを動かし、例外が出ないか確認する（デバッグ用）。"""

import os
from dataclasses import asdict
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

APP_DIR = Path(sys.argv[1])
sys.path.insert(0, str(APP_DIR))

work = Path(tempfile.mkdtemp(prefix="karte_ui_"))
shutil.copytree(APP_DIR / "sample_data" / "customers", work / "customers")
os.environ["KARTE_DATA_DIR"] = str(work)

import openai  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

import app  # noqa: E402

CALLS = []


class FakeResponses:
    def parse(self, **kwargs):
        CALLS.append(kwargs)
        if kwargs["text_format"].__name__ == "QAAnswer":
            return SimpleNamespace(
                output_parsed=app.QAAnswer(answer="- 山田 花子 様（袴の持ち込み）", referenced_ids=["20260916_1210_山田花子", "nope"])
            )
        return SimpleNamespace(output_parsed=FAKE_DRAFT)


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.responses = FakeResponses()


openai.OpenAI = FakeOpenAI

FAKE_DRAFT = app.KarteDraft.model_validate(
    dict(
        customer_name="伊藤 まい", customer_name_kana=None, phone="090-0000-1234", email=None,
        status_guess="予約確定", shoot_type="家族写真", shoot_date="2026-11-08", shoot_time="10:00",
        location="ロケ", people_count=4, family="夫、長女（6歳）、長男（1歳）", costume="赤いワンピース",
        plan="ロケーションプラン", options=None, price=33000, delivery=None, payment_status="未入金",
        personality="明るい", preferences=None, requests="紅葉の中で", ng="長男が人見知り", tone="絵文字多め",
        mood_tags=["明るい"], source="Instagram", source_detail=None, anniversaries=[],
        children=[{"name": "長女", "age": 6, "birthday": None}], review=None, next_opportunity="長女が来年入学",
        summary=["a", "b", "c"], prep_checklist=["紅葉の確認"], next_message="伊藤様、ありがとうございました。",
    )
)


def check(at: AppTest, label: str) -> None:
    if at.exception:
        print(f"❌ {label}:")
        for exc in at.exception:
            print(exc.value)
            print("\n".join(exc.stack_trace[-15:]) if exc.stack_trace else "")
        sys.exit(1)
    print(f"✅ {label}")


def new_app() -> AppTest:
    at = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=60)
    at.run()
    return at


# --- 各画面 -------------------------------------------------------------------
at = new_app()
check(at, "ホーム")
assert any(app.APP_NAME in m.value for m in at.markdown), "タイトルがない"
for page in ("import", "list", "ask", "settings", "home"):
    at.session_state["page"] = page
    at.run()
    check(at, f"画面: {page}")

# --- サイドバーのメニュー ------------------------------------------------------------
at.button(key="nav_list").click().run()
check(at, "サイドバーから一覧へ")
assert at.session_state["page"] == "list"

# --- 一覧：絞り込み → 詳細 -----------------------------------------------------------
at.text_input(key="flt_keyword").input("袴").run()
check(at, "一覧：キーワード絞り込み")
assert any("2件 / 全6件" in c.value for c in at.caption), [c.value for c in at.caption]
at.text_input(key="flt_keyword").input("").run()
at.checkbox(key="flt_repeat").check().run()
check(at, "一覧：リピーターのみ")
at.checkbox(key="flt_repeat").uncheck().run()

at.session_state["selected_id"] = "20260916_1210_山田花子"
at.run()
check(at, "カルテ詳細")
assert any("リピーター" in i.value for i in at.info), "リピーター表示がない"

# 進み具合の変更
rid = "20260916_1210_山田花子"
status_box = [s for s in at.selectbox if s.key and s.key.startswith(f"q_status_{rid}")][0]
status_box.set_value("撮影済み")
at.button(key=f"q_save_{rid}").click().run()
check(at, "進み具合の変更")
repo = app.KarteRepository(work)
assert repo.load(rid).record.status == "撮影済み"
assert app.list_backups(work), "自動バックアップがない"

# 編集フォームの保存
form_buttons = [b for b in at.button if "変更を保存する" in b.label]
assert form_buttons, "編集フォームのボタンがない"
memo = [t for t in at.text_area if t.key and t.key.startswith(f"edit_{rid}") and t.key.endswith("_memo")][0]
memo.input("テストのメモ")
form_buttons[0].click().run()
check(at, "編集フォームの保存")
assert repo.load(rid).record.memo == "テストのメモ"

# 過去のご依頼を開く
other = "20250302_1305_山田花子"
at.button(key=f"open_{rid}_{other}").click().run()
check(at, "過去のご依頼を開く")
assert at.session_state["selected_id"] == other

# 削除
at.checkbox(key=f"del_confirm_{other}").check().run()
at.button(key=f"del_{other}").click().run()
check(at, "削除（ゴミ箱へ）")
assert (work / "trash" / f"{other}.md").exists()
shutil.move(str(work / "trash" / f"{other}.md"), str(work / "customers" / f"{other}.md"))

at.button(key="nav_home").click().run()
check(at, "ホームに戻る")

# --- AIに質問 ----------------------------------------------------------------------
at.button(key="nav_ask").click().run()
for preset, _ in app.PRESETS[:5]:
    at.button(key=f"preset_{preset}").click().run()
    check(at, f"よく使う質問: {preset}")
at.text_input(key="api_key").input("sk-test").run()
at.button(key="preset_ai_requests").click().run()
check(at, "よく使う質問: ai_requests（AI）")
at.chat_input[0].set_value("袴を持ち込んだお客様は？").run()
check(at, "自由な質問")
history = at.session_state["qa_history"]
assert len(history) == 2 and history[-1]["references"] == ["20260916_1210_山田花子"], history

# --- 取り込み（ステップ2〜3。ファイル選択はAppTest非対応のため、読み込み結果を直接渡す） ---------
chat = app.parse_line_csv((APP_DIR / "sample_data" / "line_chat_sample.csv").read_bytes(), "line_chat_sample.csv")
assert chat.structured and chat.line_name == "まい🍁" and len(chat.messages) == 7, chat
at.button(key="nav_import").click().run()
check(at, "取り込み：ステップ1")
record = app.build_record_from_draft(FAKE_DRAFT, chat.messages, chat.line_name, chat.source_file)
kartes = repo.load_all()
at.session_state["imp_chat"] = chat.to_dict()
at.session_state["imp_record"] = record.model_dump()
at.session_state["imp_keys"] = [m.key() for m in chat.messages]
at.session_state["imp_candidates"] = [asdict(c) for c in app.find_customer_candidates(record.customer, [k.record for k in kartes])]
at.session_state["imp_step"] = 2
at.run()
check(at, "取り込み：ステップ2（はじめてのお客様）")
at.button[[b.label for b in at.button].index("次へ →")].click().run()
check(at, "取り込み：ステップ3")
save_button = [b for b in at.button if "この内容で保存する" in b.label][0]
save_button.click().run()
check(at, "取り込み：保存")
done = at.session_state["imp_done"]
assert done and (work / "customers" / f"{done}.md").exists(), done
saved = repo.load(done).record
assert saved.customer_id == "C0006" and saved.customer.name == "伊藤 まい", saved
print("   保存されたファイル:", f"{done}.md")

# 同じCSVをもう一度取り込む → 既存のお客様＋「このご依頼を更新」が初期選択
at.button[[b.label for b in at.button].index("📥 続けて取り込む")].click().run()
kartes = repo.load_all()
at.session_state["imp_chat"] = chat.to_dict()
at.session_state["imp_record"] = record.model_dump()
at.session_state["imp_keys"] = [m.key() for m in chat.messages]
at.session_state["imp_candidates"] = [asdict(c) for c in app.find_customer_candidates(record.customer, [k.record for k in kartes])]
at.session_state["imp_step"] = 2
at.run()
check(at, "再取り込み：ステップ2")
radios = at.radio
assert radios[0].value == "C0006", radios[0].value
assert radios[1].value == done, radios[1].value
at.button[[b.label for b in at.button].index("次へ →")].click().run()
check(at, "再取り込み：ステップ3（更新）")
assert at.session_state["imp_mode"] == "update"
[b for b in at.button if "この内容で保存する" in b.label][0].click().run()
check(at, "再取り込み：保存（更新）")
assert at.session_state["imp_done"] == done
assert len(list((work / "customers").glob("*.md"))) == 7

# 既存のお客様の「新しいご依頼」：保存済みのやり取りは除き、AIで整理し直す
calls_before = len(CALLS)
at.button[[b.label for b in at.button].index("📥 続けて取り込む")].click().run()
kartes = repo.load_all()
chat2 = app.ParsedChat.from_dict(chat.to_dict())
chat2.messages.append(app.Message("2027/03/01 10:00", "まい🍁", "customer", "来年の桜の時期に、入学記念の撮影もお願いしたいです！"))
at.session_state["imp_chat"] = chat2.to_dict()
at.session_state["imp_record"] = record.model_dump()
at.session_state["imp_keys"] = [m.key() for m in chat2.messages]
at.session_state["imp_candidates"] = [asdict(c) for c in app.find_customer_candidates(record.customer, [k.record for k in kartes])]
at.session_state["imp_step"] = 2
at.run()
check(at, "新しいご依頼：ステップ2")
at.radio[1].set_value("__new_request__").run()
at.button[[b.label for b in at.button].index("次へ →")].click().run()
check(at, "新しいご依頼：ステップ3")
assert at.session_state["imp_mode"] == "new_request"
assert len(at.session_state["imp_final_messages"]) == 1, at.session_state["imp_final_messages"]
assert len(CALLS) == calls_before + 1, "AIで整理し直していない"
[b for b in at.button if "この内容で保存する" in b.label][0].click().run()
check(at, "新しいご依頼：保存")
new_done = at.session_state["imp_done"]
assert repo.load(new_done).record.customer_id == "C0006"
assert len(list((work / "customers").glob("*.md"))) == 8

# --- 設定 --------------------------------------------------------------------------
at.button(key="nav_settings").click().run()
check(at, "設定")
at.button[[b.label for b in at.button].index("今すぐバックアップする")].click().run()
check(at, "設定：今すぐバックアップ")
at.button[[b.label for b in at.button].index("書き出す")].click().run()
check(at, "設定：匿名化の書き出し")
assert at.session_state["last_export"] and Path(at.session_state["last_export"]).exists()

# --- APIキーなしのとき --------------------------------------------------------------
at2 = new_app()
at2.session_state["page"] = "import"
at2.run()
check(at2, "APIキーなしの取り込み画面")

print("AI呼び出し回数（偽物）:", len(CALLS))
print("作業フォルダ:", work)
print("すべてOK")
