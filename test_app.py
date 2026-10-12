"""Photo Karte のロジック層のテスト（UIはテストしない）。

実行方法:
    pytest -q
"""

from __future__ import annotations

import os
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx  # openai 3.x が使うHTTPライブラリ
import openai
import pytest

# このPCの studio_settings.toml に左右されないよう、公開用の既定の名前でテストする
os.environ["KARTE_SETTINGS_FILE"] = str(Path(__file__).resolve().parent / "存在しない設定ファイル.toml")

import app  # noqa: E402

TODAY = date(2026, 10, 11)


# -----------------------------------------------------------------------------
# テスト用の部品
# -----------------------------------------------------------------------------


def make_draft(**overrides) -> app.KarteDraft:
    data = dict(
        customer_name="山田 花子",
        customer_name_kana="やまだ はなこ",
        phone="090-1234-5678",
        email=None,
        status_guess="予約確定",
        shoot_type="家族写真",
        shoot_date="2027-04-10",
        shoot_time="10:00",
        location="桜並木の公園（ロケ）",
        people_count=4,
        family="夫婦、長女（5歳）、長男（2歳）",
        costume=None,
        plan="ロケーションプラン",
        options=None,
        price=33000,
        delivery="オンラインでデータ納品",
        payment_status="未入金",
        personality="明るく丁寧",
        preferences=None,
        requests="満開の桜並木の下で撮りたい",
        ng="長男は大きな音が苦手",
        tone="絵文字が多い",
        mood_tags=["#明るい", "丁寧", "明るい", " "],
        source="Instagram",
        source_detail="友人の投稿",
        anniversaries=[{"label": "結婚記念日", "date": "2018/6/1"}, {"label": " ", "date": None}],
        children=[{"name": "さくらちゃん", "age": 5, "birthday": None}, {"name": None, "age": None, "birthday": None}],
        review=None,
        next_opportunity="長女が来年七五三（7歳）",
        summary=["一つ目", "二つ目", "三つ目", "四つ目"],
        prep_checklist=["開花状況を確認する", "  "],
        next_message="  山田様、ありがとうございました。  ",
    )
    data.update(overrides)
    return app.KarteDraft.model_validate(data)


def make_karte(
    rid: str = "20260101_1000_山田花子",
    cid: str = "C0001",
    name: str | None = "山田 花子",
    status: str = "予約確定",
    shoot_date: str | None = None,
    shoot_time: str | None = None,
    payment: str = "不明",
    registered: str = "2026-01-01 10:00",
    shoot_type: str | None = "家族写真",
    children: list | None = None,
    summary: list | None = None,
    messages: list | None = None,
    phone: str | None = None,
    line_name: str | None = None,
    kana: str | None = None,
    email: str | None = None,
    requests: str | None = None,
) -> app.Karte:
    record = app.KarteRecord(
        request_id=rid,
        customer_id=cid,
        status=status,
        registered_at=registered,
        customer=app.CustomerInfo(name=name, name_kana=kana, line_name=line_name, phone=phone, email=email),
        shoot=app.ShootInfo(
            type=shoot_type,
            date=shoot_date,
            time=shoot_time,
            season=app.season_from_date(shoot_date),
            payment_status=payment,
        ),
        profile=app.ProfileInfo(requests=requests),
        future=app.FutureInfo(children=children or []),
        summary=summary or [],
    )
    return app.Karte(record, messages or [], Path(f"{rid}.md"))


def msg(ts: str, text: str, role: str = "customer", sender: str = "はなこ") -> app.Message:
    return app.Message(ts, sender, role, text)


STANDARD_CSV = (
    "送信者タイプ,送信者名,送信日,送信時刻,内容\n"
    "User,はなこ,2026/09/01,10:00:00,はじめまして。家族写真をお願いしたいです。\n"
    "Account,studio SAMPLE,2026/09/01,10:15:00,お問い合わせありがとうございます。\n"
    "User,はなこ,2026/09/02,09:05:00,よろしくお願いします！\n"
)


# -----------------------------------------------------------------------------
# T-01〜T-08 CSVの読み込み
# -----------------------------------------------------------------------------


def test_t01_standard_csv_utf8_bom():
    chat = app.parse_line_csv(("﻿" + STANDARD_CSV).encode("utf-8"), source_file="talk.csv")
    assert chat.structured is True
    assert chat.encoding == "utf-8-sig"
    assert chat.source_file == "talk.csv"
    assert len(chat.messages) == 3
    assert [m.role for m in chat.messages] == ["customer", "studio", "customer"]
    assert chat.messages[0].timestamp == "2026/09/01 10:00"
    assert chat.messages[1].sender == "studio SAMPLE"
    assert chat.line_name == "はなこ"
    assert chat.period == "2026/09/01 〜 2026/09/02"


def test_t02_shift_jis_csv():
    chat = app.parse_line_csv(STANDARD_CSV.encode("cp932"))
    assert chat.encoding == "cp932"
    assert chat.messages[0].text == "はじめまして。家族写真をお願いしたいです。"
    assert chat.line_name == "はなこ"


def test_t03_preamble_rows_before_header():
    text = (
        "アカウント名,studio SAMPLE\n"
        "ユーザー名,山田はなこ\n"
        "ダウンロード日時,2026/10/11 16:00\n"
        "\n"
        "送信者名,送信日時,内容\n"
        "山田はなこ,2026/09/01 10:00,こんにちは\n"
        "studio SAMPLE,2026/09/01 10:05,ありがとうございます\n"
    )
    chat = app.parse_line_csv(text.encode("utf-8"))
    assert chat.structured is True
    assert chat.account_name == "studio SAMPLE"
    assert chat.line_name == "山田はなこ"
    assert [m.role for m in chat.messages] == ["customer", "studio"]
    assert chat.messages[1].timestamp == "2026/09/01 10:05"


def test_t04_datetime_column_and_english_headers():
    text = (
        "Type,Timestamp,From,Text\n"
        "user,2026-09-01 10:00:00,Hana,Hello\n"
        "account,2026-09-01 10:01:00,studio SAMPLE,Thanks\n"
    )
    chat = app.parse_line_csv(text.encode("utf-8"))
    assert [m.role for m in chat.messages] == ["customer", "studio"]
    assert [m.timestamp for m in chat.messages] == ["2026/09/01 10:00", "2026/09/01 10:01"]
    assert chat.line_name == "Hana"

    text_ja = "送信日時,送信者,メッセージ\n2026/09/01 10:00,はなこ,こんにちは\n"
    chat_ja = app.parse_line_csv(text_ja.encode("utf-8"))
    assert chat_ja.structured is True
    assert chat_ja.messages[0].text == "こんにちは"
    assert chat_ja.messages[0].role == "unknown"  # 送信者タイプもアカウント名も分からない


def test_t05_multiline_message_in_quotes():
    text = '送信者タイプ,送信者名,送信日,送信時刻,内容\r\nUser,はなこ,2026/09/01,10:00,"1行目\r\n2行目"\r\n'
    chat = app.parse_line_csv(text.encode("utf-8"))
    assert len(chat.messages) == 1
    assert chat.messages[0].text == "1行目\n2行目"


def test_t06_unknown_format_falls_back_to_raw_text():
    raw = "なにかのメモ,です\n1,2\n"
    chat = app.parse_line_csv(raw.encode("utf-8"))
    assert chat.structured is False
    assert len(chat.messages) == 1
    assert chat.messages[0].text == raw.strip()
    assert chat.line_name is None


@pytest.mark.parametrize("data", [b"", b"   \n  ", "送信者タイプ,送信者名,送信日,送信時刻,内容\n".encode()])
def test_t07_empty_file_raises_friendly_error(data):
    with pytest.raises(ValueError) as error:
        app.parse_line_csv(data)
    assert "CSV" in str(error.value) or "空" in str(error.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026/3/1", "2026/03/01"),
        ("2026-03-01 9:05", "2026/03/01 09:05"),
        ("2026年3月1日 9時5分", "2026/03/01 09:05"),
        ("2026/03/01 09:05:30", "2026/03/01 09:05"),
        ("2026/03/01 午後3:05", "2026/03/01 15:05"),
        ("2026/02/30 10:00", "2026/02/30 10:00"),  # 存在しない日付は元のまま
        ("きのう", "きのう"),
        ("", ""),
    ],
)
def test_t08_normalize_timestamp(value, expected):
    assert app.normalize_timestamp(value) == expected


# -----------------------------------------------------------------------------
# T-09〜T-11 シーズン・ファイル名・お客様ID
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027-04-10", "春（桜）"),
        ("2026-10-20", "秋（紅葉）"),
        ("2027-01-05", "冬"),
        ("2026-12-01", "冬"),
        ("2026-07-07", "夏"),
        (None, None),
        ("未定", None),
    ],
)
def test_t09_season_from_date(value, expected):
    assert app.season_from_date(value) == expected


def test_t10_make_filename():
    when = datetime(2026, 10, 11, 16, 30)
    assert app.make_filename(when, "山田 花子 様") == "20261011_1630_山田花子.md"
    assert app.make_filename(when, 'a/b:c*?"<>|d') == "20261011_1630_abcd.md"
    assert app.make_filename(when, None) == "20261011_1630_名前未登録.md"
    assert app.make_filename(when, "   ") == "20261011_1630_名前未登録.md"
    existing = {"20261011_1630_山田花子.md", "20261011_1630_山田花子_2.md"}
    assert app.make_filename(when, "山田花子", existing) == "20261011_1630_山田花子_3.md"
    long_name = app.make_filename(when, "あ" * 50)
    assert long_name == f"20261011_1630_{'あ' * app.FILENAME_NAME_MAX}.md"


def test_t11_next_customer_id():
    assert app.next_customer_id([]) == "C0001"
    records = [make_karte(cid="C0009").record, make_karte(cid="C0002").record, make_karte(cid="").record]
    assert app.next_customer_id(records) == "C0010"


# -----------------------------------------------------------------------------
# T-12〜T-15 同じお客様の照合
# -----------------------------------------------------------------------------


def test_t12_phone_match_with_different_format():
    records = [
        make_karte(cid="C0001", name="佐藤 美咲", phone="09012345678").record,
        make_karte(cid="C0002", name="山田 花子", phone="080-0000-0000").record,
    ]
    identity = app.CustomerInfo(name="別の名前", phone="090-1234-5678")
    candidates = app.find_customer_candidates(identity, records)
    assert candidates[0].customer_id == "C0001"
    assert candidates[0].score == 100
    assert "電話番号が一致" in candidates[0].reasons

    identity_intl = app.CustomerInfo(phone="+81 90-1234-5678")
    assert app.find_customer_candidates(identity_intl, records)[0].customer_id == "C0001"


def test_t13_name_match_ignores_spaces_width_and_honorific():
    records = [make_karte(cid="C0003", name="山田　花子", kana="ヤマダ ハナコ").record]
    candidates = app.find_customer_candidates(app.CustomerInfo(name="山田花子 様", name_kana="やまだはなこ"), records)
    assert len(candidates) == 1
    assert candidates[0].score == 70
    assert candidates[0].reasons == ["お名前が一致", "ふりがなが一致"]


def test_t14_line_name_only_and_no_match():
    records = [make_karte(cid="C0004", name="鈴木 一郎", line_name="いちろう").record]
    line_only = app.find_customer_candidates(app.CustomerInfo(line_name="いちろう"), records)
    assert [(c.customer_id, c.score) for c in line_only] == [("C0004", 50)]
    assert app.find_customer_candidates(app.CustomerInfo(name="田中 次郎", line_name="じろう"), records) == []
    assert app.find_customer_candidates(app.CustomerInfo(), records) == []


def test_t14b_similar_name_is_low_score_candidate():
    records = [make_karte(cid="C0005", name="山田花子").record]
    candidates = app.find_customer_candidates(app.CustomerInfo(name="山田華子"), records)
    assert [(c.customer_id, c.score, c.reasons) for c in candidates] == [("C0005", 30, ["お名前が似ている"])]


def test_t15_multiple_requests_grouped_into_one_candidate():
    records = [
        make_karte(rid="20250401_1000_山田花子", cid="C0001", registered="2025-04-01 10:00", phone="09011112222").record,
        make_karte(rid="20260901_1000_山田花子", cid="C0001", registered="2026-09-01 10:00", phone="09011112222").record,
        make_karte(rid="20260101_1000_佐藤", cid="C0002", name="佐藤 美咲").record,
    ]
    candidates = app.find_customer_candidates(app.CustomerInfo(name="山田 花子", phone="090-1111-2222"), records)
    assert len(candidates) == 1
    assert candidates[0].request_ids == ["20260901_1000_山田花子", "20250401_1000_山田花子"]
    assert candidates[0].reasons == ["電話番号が一致", "お名前が一致"]


# -----------------------------------------------------------------------------
# T-16〜T-17 Markdownの作成・読み込み
# -----------------------------------------------------------------------------


def full_record() -> app.KarteRecord:
    record = app.build_record_from_draft(
        make_draft(), [msg("2026/09/01 10:00", "a")], line_name="はなこ", source_file="talk.csv"
    )
    record.request_id = "20261011_1630_山田花子"
    record.customer_id = "C0001"
    record.registered_at = record.updated_at = "2026-10-11 16:30"
    record.memo = "1行目のメモ\n2行目のメモ"
    record.next_message = "山田様\nありがとうございました。\n---\nまたお待ちしております。"
    return record


def test_t16_markdown_round_trip():
    record = full_record()
    messages = [
        msg("2026/09/01 10:00", "はじめまして。\n\n家族写真を\nお願いしたいです。"),
        msg("2026/09/01 10:15", "ありがとうございます！", role="studio", sender="studio SAMPLE"),
        msg("", "日時の分からないメッセージ", role="unknown", sender="だれか"),
        msg("2026/09/02", "時刻なし", role="customer", sender=""),
    ]
    text = app.render_markdown(record, messages)
    assert text.startswith("---\n")
    assert "# 山田 花子 様｜家族写真（2027-04-10）" in text
    assert app.CONVERSATION_HEADING in text
    parsed_record, parsed_messages = app.parse_markdown(text)
    assert parsed_record == record
    assert parsed_messages == messages


def test_t17_markdown_without_conversation_and_invalid_file():
    record = full_record()
    text = app.render_markdown(record, [])
    parsed_record, parsed_messages = app.parse_markdown(text)
    assert parsed_record == record
    assert parsed_messages == []

    only_front = "---\nrequest_id: x\nshoot:\n  date: 2027-04-10\n  time: '10:00'\n---\n本文だけ\n"
    record2, messages2 = app.parse_markdown(only_front)
    assert record2.shoot.date == "2027-04-10"  # YAMLの日付型は文字列に戻る
    assert messages2 == []

    with pytest.raises(ValueError):
        app.parse_markdown("本文だけのファイル")
    with pytest.raises(ValueError):
        app.parse_markdown("---\nrequest_id: x\n")


# -----------------------------------------------------------------------------
# T-18 AIの下書き → カルテ
# -----------------------------------------------------------------------------


def test_t18_build_record_from_draft():
    messages = [msg("2026/09/01 10:00", "a"), msg("2026/09/03 12:00", "b")]
    record = app.build_record_from_draft(make_draft(), messages, line_name="はなこ", source_file="talk.csv")
    assert record.status == "予約確定"
    assert record.customer.name == "山田 花子"
    assert record.customer.line_name == "はなこ"
    assert record.shoot.date == "2027-04-10"
    assert record.shoot.season == "春（桜）"
    assert record.shoot.price == 33000
    assert record.profile.mood_tags == ["明るい", "丁寧"]
    assert record.summary == ["一つ目", "二つ目", "三つ目"]
    assert record.prep_checklist == ["開花状況を確認する"]
    assert record.next_message == "山田様、ありがとうございました。"
    assert [a.model_dump() for a in record.future.anniversaries] == [{"label": "結婚記念日", "date": "2018-06-01"}]
    assert [c.name for c in record.future.children] == ["さくらちゃん"]
    assert record.message_count == 2
    assert record.chat_period == "2026/09/01 〜 2026/09/03"
    assert record.source_file == "talk.csv"
    assert record.request_id == "" and record.customer_id == ""

    broken = app.build_record_from_draft(make_draft(shoot_date="来週", shoot_time="夕方", customer_name="  "), [], None)
    assert broken.shoot.date is None
    assert broken.shoot.season is None
    assert broken.shoot.time is None
    assert broken.customer.name is None
    assert broken.display_name == app.NO_NAME


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10:00", "10:00"), ("9:5", "09:05"), ("10時", "10:00"), ("10時30分", "10:30"), ("25:00", None), ("夕方", None)],
)
def test_t18b_clean_time(value, expected):
    assert app.clean_time(value) == expected


# -----------------------------------------------------------------------------
# T-19〜T-20 統合・更新
# -----------------------------------------------------------------------------


def test_t19_merge_filter_overlap():
    old = [msg("2026/09/01 10:00", "a"), msg("2026/09/02 10:00", "b")]
    new = [msg("2026/09/02 10:00", "b "), msg("2026/09/03 10:00", "c"), msg("2026/08/31 09:00", "z")]
    merged = app.merge_messages(old, new)
    assert [m.text for m in merged] == ["z", "a", "b", "c"]
    assert [m.text for m in app.filter_new_messages(new, old)] == ["c", "z"]
    assert app.message_overlap(old, new) == 1


def test_t20_merge_record_for_update():
    existing = full_record()
    existing.status = "撮影済み"
    existing.memo = "大事なメモ"
    existing.shoot.payment_status = "入金済み"
    new = app.build_record_from_draft(
        make_draft(status_guess="予約確定", phone=None, payment_status="未入金", review="とても良かったです", summary=[]),
        [],
        "はなこ",
    )
    merged = app.merge_record_for_update(existing, new)
    assert merged.request_id == existing.request_id
    assert merged.customer_id == existing.customer_id
    assert merged.registered_at == existing.registered_at
    assert merged.memo == "大事なメモ"
    assert merged.status == "撮影済み"  # 前の段階には戻らない
    assert merged.shoot.payment_status == "入金済み"
    assert merged.customer.phone == existing.customer.phone  # 空なら既存の値を残す
    assert merged.summary == existing.summary
    assert merged.future.review == "とても良かったです"  # 新しい情報は反映する

    advanced = app.merge_record_for_update(existing, new.model_copy(update={"status": "納品済み"}))
    assert advanced.status == "納品済み"


# -----------------------------------------------------------------------------
# T-21 保存
# -----------------------------------------------------------------------------


def test_t21_repository_save_update_trash(tmp_path):
    repo = app.KarteRepository(tmp_path)
    now = datetime(2026, 10, 11, 16, 30)
    draft_record = app.build_record_from_draft(make_draft(), [], "はなこ")
    messages = [msg("2026/09/01 10:00", "はじめまして")]

    saved = repo.save_new(draft_record, messages, now=now)
    assert saved.request_id == "20261011_1630_山田花子"
    assert saved.customer_id == "C0001"
    assert saved.registered_at == "2026-10-11 16:30"
    assert (tmp_path / "customers" / "20261011_1630_山田花子.md").exists()

    same_name = repo.save_new(draft_record, messages, now=now)
    assert same_name.request_id == "20261011_1630_山田花子_2"
    assert same_name.customer_id == "C0002"

    repeat = draft_record.model_copy(update={"customer_id": "C0001"})
    repeat_saved = repo.save_new(repeat, messages, now=datetime(2026, 10, 12, 9, 0))
    assert repeat_saved.customer_id == "C0001"

    kartes = repo.load_all()
    assert len(kartes) == 3
    assert {k.record.request_id for k in kartes} == {
        "20261011_1630_山田花子",
        "20261011_1630_山田花子_2",
        "20261012_0900_山田花子",
    }
    assert kartes[0].messages == messages

    changed = saved.model_copy(update={"status": "撮影済み"})
    updated = repo.update(changed, messages, now=datetime(2026, 10, 20, 18, 0))
    assert updated.request_id == saved.request_id
    assert updated.updated_at == "2026-10-20 18:00"
    assert repo.load(saved.request_id).record.status == "撮影済み"

    trashed = repo.move_to_trash(saved.request_id)
    assert trashed.parent == tmp_path / "trash"
    assert not repo.path_for(saved.request_id).exists()
    assert len(repo.load_all()) == 2

    (tmp_path / "customers" / "broken.md").write_text("壊れたファイル", encoding="utf-8")
    assert len(repo.load_all()) == 2
    assert repo.load_errors and repo.load_errors[0].startswith("broken.md")

    with pytest.raises(FileNotFoundError):
        repo.update(app.KarteRecord(request_id="存在しない"), [])


# -----------------------------------------------------------------------------
# T-22 バックアップ
# -----------------------------------------------------------------------------


def test_t22_backup_and_rotation(tmp_path):
    repo = app.KarteRepository(tmp_path)
    repo.save_new(app.build_record_from_draft(make_draft(), [], None), [msg("2026/09/01 10:00", "a")])
    first = app.create_backup(tmp_path, now=datetime(2026, 10, 11, 10, 0, 0))
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
    assert len(names) == 1 and names[0].startswith("customers/") and names[0].endswith(".md")

    for second in range(1, 6):
        app.create_backup(tmp_path, keep=3, now=datetime(2026, 10, 11, 10, 0, second))
    backups = app.list_backups(tmp_path)
    assert len(backups) == 3
    assert backups[0].name == "karte_backup_20261011_100005.zip"

    duplicate = app.create_backup(tmp_path, keep=10, now=datetime(2026, 10, 11, 10, 0, 5))
    assert duplicate.name == "karte_backup_20261011_100005_2.zip"


# -----------------------------------------------------------------------------
# T-23 個人情報を伏せたコピー
# -----------------------------------------------------------------------------


def test_t23_anonymize_and_export(tmp_path):
    record = full_record()
    record.customer.email = "hana@example.com"
    record.profile.requests = "山田花子です。さくらの着物姿を撮りたい"
    messages = [
        msg(
            "2026/09/01 10:00",
            "山田です。電話は090-1234-5678、メールは hana@example.com です。〒506-0001 に住んでいます。さくらも一緒です。",
        ),
        msg("2026/09/01 10:05", "やまだ はなこ様、ありがとうございます", role="studio", sender="studio SAMPLE"),
    ]
    masked, masked_messages = app.anonymize_record(record, messages)
    text = app.render_markdown(masked, masked_messages)
    for secret in ("山田", "花子", "やまだ", "はなこ", "090-1234-5678", "hana@example.com", "506-0001", "さくら"):
        assert secret not in text, secret
    assert masked.customer_id == "C0001"
    assert masked.request_id == "20261011_1630_C0001"
    assert masked.future.children[0].name == "お子さま1"
    assert masked_messages[0].sender == ""
    assert app.speaker_label(masked_messages[0]) == "お客様"
    assert record.customer.name == "山田 花子"  # 元のデータは変わらない

    repo = app.KarteRepository(tmp_path)
    saved = repo.save_new(record, messages, now=datetime(2026, 10, 11, 16, 30))
    original = repo.path_for(saved.request_id).read_text(encoding="utf-8")
    zip_path = app.export_anonymized(repo.load_all(), repo.exports_dir, now=datetime(2026, 10, 12, 9, 0))
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        content = archive.read(names[0]).decode("utf-8")
    assert names == ["anonymized_20261012_0900/20261011_1630_C0001.md"]
    assert "山田" not in content
    assert repo.path_for(saved.request_id).read_text(encoding="utf-8") == original


# -----------------------------------------------------------------------------
# T-24〜T-26 集計・絞り込み
# -----------------------------------------------------------------------------


@pytest.fixture()
def schedule_kartes() -> dict[str, app.Karte]:
    return {
        "k1": make_karte(rid="k1", cid="C0001", shoot_date="2026-10-20", shoot_time="10:00", payment="未入金"),
        "k2": make_karte(rid="k2", cid="C0002", shoot_date="2026-10-05", status="撮影済み", payment="入金済み"),
        "k3": make_karte(rid="k3", cid="C0003", shoot_date="2026-11-03", status="お問い合わせ", payment="一部入金"),
        "k4": make_karte(rid="k4", cid="C0004", shoot_date="2026-10-25", status="キャンセル", payment="未入金"),
        "k5": make_karte(rid="k5", cid="C0005", shoot_date="2025-10-15", status="納品済み"),
        "k6": make_karte(rid="k6", cid="C0006", shoot_date="2025-04-05", status="納品済み"),
        "k7": make_karte(rid="k7", cid="C0007", status="お問い合わせ", registered="2026-10-01 10:00"),
        "k8": make_karte(rid="k8", cid="C0008", shoot_date="2026-10-20", shoot_time="09:00"),
    }


def test_t24_schedule_aggregations(schedule_kartes):
    kartes = list(schedule_kartes.values())
    ids = lambda items: [k.record.request_id for k in items]  # noqa: E731
    assert ids(app.upcoming_shoots(kartes, TODAY)) == ["k8", "k1", "k3"]
    assert ids(app.shoots_in_month(kartes, TODAY)) == ["k2", "k8", "k1"]
    season, year, last = app.same_season_last_year(kartes, TODAY)
    assert (season, year, ids(last)) == ("秋（紅葉）", 2025, ["k5"])
    assert ids(app.unpaid_requests(kartes)) == ["k1", "k3"]
    assert ids(app.undated_requests(kartes)) == ["k7"]
    assert app.next_shoot_checklist(kartes, TODAY).record.request_id == "k8"
    assert app.next_shoot_checklist([], TODAY) is None


def test_t25_upcoming_child_events():
    kartes = [
        make_karte(
            rid="old", cid="C0001", name="山田 花子", registered="2024-05-01 10:00",
            children=[app.Child(name="さくらちゃん", age=3)],
        ),
        make_karte(
            rid="new", cid="C0001", name="山田 花子", registered="2025-05-01 10:00",
            children=[app.Child(name="さくらちゃん", age=4)],
        ),
        make_karte(
            rid="seijin", cid="C0002", name="佐藤 美咲", registered="2026-01-01 10:00",
            children=[app.Child(name="ゆうと", birthday="2007-01-01"), app.Child(name="年齢不明")],
        ),
    ]
    rows = app.upcoming_child_events(kartes, TODAY)
    summary = [(r["時期"], r["行事"], r["お子さま"], r["カルテID"]) for r in rows]
    # 「今年」が先。同じ時期の中はお客様名の順（佐藤 → 山田）
    assert summary == [
        ("今年", "七五三（5歳）", "さくらちゃん", "new"),
        ("来年", "二十歳のつどい（成人式）", "ゆうと", "seijin"),
        ("来年", "小学校入学（目安）", "さくらちゃん", "new"),
    ]
    assert rows[0]["今の年齢（目安）"] == "5歳"


def test_t26_filter_records():
    kartes = [
        make_karte(rid="a", cid="C0001", shoot_date="2026-04-05", shoot_type="家族写真", status="納品済み",
                   messages=[msg("2026/03/01 10:00", "着物を持ち込みたいです")]),
        make_karte(rid="b", cid="C0001", shoot_date="2026-10-20", shoot_type="七五三"),
        make_karte(rid="c", cid="C0002", name="佐藤 美咲", shoot_date="2025-10-10", shoot_type="成人式",
                   requests="振袖で桜"),
        make_karte(rid="d", cid="C0003", name="鈴木 一郎", status="お問い合わせ", registered="2026-09-01 10:00"),
    ]
    ids = lambda items: [k.record.request_id for k in items]  # noqa: E731
    assert ids(app.filter_records(kartes)) == ["b", "d", "a", "c"]
    assert ids(app.filter_records(kartes, keyword="着物")) == ["a"]
    assert ids(app.filter_records(kartes, keyword="佐藤 振袖")) == ["c"]
    assert ids(app.filter_records(kartes, keyword="佐藤 着物")) == []
    assert ids(app.filter_records(kartes, types=["七五三", "成人式"])) == ["b", "c"]
    assert ids(app.filter_records(kartes, year=2026)) == ["b", "d", "a"]
    assert ids(app.filter_records(kartes, seasons=["秋（紅葉）"])) == ["b", "c"]
    assert ids(app.filter_records(kartes, statuses=["お問い合わせ"])) == ["d"]
    assert ids(app.filter_records(kartes, repeat_only=True)) == ["b", "a"]

    rows = app.karte_table_rows(kartes[:1] + kartes[3:], app.customer_request_counts(kartes))
    assert rows[0]["ご依頼"] == "🔁 2件"
    assert rows[1]["撮影日"] == "未定"
    assert rows[1]["ご依頼"] == "初めて"


# -----------------------------------------------------------------------------
# T-27 AIに渡すカルテの選び方
# -----------------------------------------------------------------------------


def test_t27_select_context():
    kimono = make_karte(rid="kimono", cid="C0001", summary=["七五三で着物を持ち込み"],
                        messages=[msg("2026/09/01 10:00", "祖母の着物を持ち込みたいです")])
    others = [make_karte(rid=f"other{i}", cid=f"C01{i:02d}", name=f"別の人{i}", summary=["家族写真"]) for i in range(3)]
    kartes = others + [kimono]

    context, included = app.select_context(kartes, "着物を持ち込んだお客様は？")
    assert set(included) == {"kimono", "other0", "other1", "other2"}
    assert included[0] == "kimono"
    assert "カルテID: kimono" in context
    assert "祖母の着物を持ち込みたいです" in context  # 関連の高いカルテには原文も添える

    small_limit = len(app.karte_card(kimono)) + 10
    _, included_small = app.select_context(kartes, "着物を持ち込んだお客様は？", limit=small_limit)
    assert included_small == ["kimono"]

    qa_input = app.build_qa_input("質問です", context, TODAY)
    assert qa_input.startswith("今日の日付: 2026-10-11")


def test_t27b_format_conversation_for_ai_truncates_old_messages():
    messages = [msg("2026/09/01 10:00", "古い" * 50), msg("2026/09/02 10:00", "新しいメッセージ")]
    text = app.format_conversation_for_ai(messages, "はなこ", TODAY, limit=40)
    assert "今日の日付: 2026-10-11" in text
    assert "お客様のLINE名: はなこ" in text
    assert "（古いやり取りは省略しました）" in text
    assert text.endswith("新しいメッセージ")

    raw = app.format_conversation_for_ai([app.Message("", "", "unknown", "生のテキスト")], None, TODAY)
    assert raw.endswith("# やり取り\n生のテキスト")


def test_t27c_escape_markdown():
    escaped = app.escape_markdown("# 見出し\n- 箇条書き\n1. 番号\n料金は$100 *強調*")
    assert escaped.startswith("\\# 見出し")
    assert "\\- 箇条書き" in escaped
    assert "1\\. 番号" in escaped
    assert "\\$100 \\*強調\\*" in escaped
    assert "  \n" in escaped


# -----------------------------------------------------------------------------
# T-28 AIサービス（偽のクライアントで確認）
# -----------------------------------------------------------------------------


class FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed):
        self.responses = FakeResponses(parsed)


def test_t28_extract_draft_with_fake_client():
    draft = make_draft()
    client = FakeClient(draft)
    service = app.OpenAIService(model="gpt-test", client=client)
    result = service.extract_draft([msg("2026/09/01 10:00", "はじめまして")], "はなこ", TODAY)
    assert result is draft
    call = client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert call["text_format"] is app.KarteDraft
    assert call["store"] is False
    assert call["instructions"] == app.EXTRACTION_SYSTEM_PROMPT
    assert "はじめまして" in call["input"]
    assert "今日の日付: 2026-10-11" in call["input"]

    with pytest.raises(app.AIResponseError):
        app.OpenAIService(client=FakeClient(None)).extract_draft([], None, TODAY)


def test_t28b_answer_question_filters_unknown_ids():
    kartes = [make_karte(rid="A"), make_karte(rid="B", cid="C0002")]
    client = FakeClient(app.QAAnswer(answer="答え", referenced_ids=["A", "存在しない", "A"]))
    result = app.OpenAIService(client=client).answer_question("質問", kartes, TODAY)
    assert result.answer == "答え"
    assert result.referenced_ids == ["A"]
    assert client.responses.calls[0]["text_format"] is app.QAAnswer
    assert "カルテID: A" in client.responses.calls[0]["input"]

    empty_client = FakeClient(None)
    empty = app.OpenAIService(client=empty_client).answer_question("質問", [], TODAY)
    assert "まだカルテが登録されていません" in empty.answer
    assert empty_client.responses.calls == []  # カルテがなければAIを呼ばない


# -----------------------------------------------------------------------------
# T-29 エラー表示
# -----------------------------------------------------------------------------

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls, status: int, body=None):
    return cls(message="error", response=httpx.Response(status, request=_REQUEST), body=body)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(openai.AuthenticationError, 401), "APIキーが正しくない"),
        (_status_error(openai.PermissionDeniedError, 403), "権限がありません"),
        (_status_error(openai.NotFoundError, 404), "が見つかりません"),
        (_status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), "残高が足りません"),
        (_status_error(openai.RateLimitError, 429), "混み合っています"),
        (_status_error(openai.BadRequestError, 400), "長すぎる可能性"),
        (openai.APITimeoutError(request=_REQUEST), "時間がかかりすぎました"),
        (openai.APIConnectionError(request=_REQUEST), "インターネットに接続できません"),
        (app.AIResponseError("読み取れませんでした"), "読み取れませんでした"),
        (RuntimeError("boom"), "思わぬエラー"),
    ],
)
def test_t29_friendly_error_message(exc, expected):
    message = app.friendly_error_message(exc, model="gpt-x")
    assert expected in message
    if isinstance(exc, openai.NotFoundError):
        assert "gpt-x" in message


# -----------------------------------------------------------------------------
# T-30 画面の状態（session_state）用の変換
# -----------------------------------------------------------------------------


def test_t30_state_round_trip():
    chat = app.parse_line_csv(STANDARD_CSV.encode("utf-8"), source_file="talk.csv")
    data = chat.to_dict()
    assert isinstance(data["messages"][0], dict)
    assert app.ParsedChat.from_dict(data) == chat

    messages = chat.messages
    assert app.messages_from_dicts(app.messages_to_dicts(messages)) == messages
    assert app.messages_from_dicts(None) == []

    record = full_record()
    assert app.KarteRecord.model_validate(record.model_dump()) == record


# -----------------------------------------------------------------------------
# T-31 スタジオ名・アプリ名の設定ファイル
# -----------------------------------------------------------------------------


def test_t31_studio_settings(tmp_path):
    # テストでは公開用の既定の名前になっている
    assert app.STUDIO_NAME == app.DEFAULT_STUDIO_NAME == "studio SAMPLE"
    assert app.APP_NAME == app.DEFAULT_APP_NAME == "Photo Karte"
    assert "「studio SAMPLE」" in app.EXTRACTION_SYSTEM_PROMPT

    settings_file = tmp_path / "studio_settings.toml"
    settings_file.write_text('studio_name = "studio TEST"\napp_name = "TEST Karte"\n', encoding="utf-8")
    assert app.load_studio_settings(settings_file) == {"studio_name": "studio TEST", "app_name": "TEST Karte"}

    partial = tmp_path / "partial.toml"
    partial.write_text('app_name = "  "\nstudio_name = "studio X"\n', encoding="utf-8")
    assert app.load_studio_settings(partial) == {"studio_name": "studio X", "app_name": "Photo Karte"}

    broken = tmp_path / "broken.toml"
    broken.write_text("studio_name = ", encoding="utf-8")
    defaults = {"studio_name": "studio SAMPLE", "app_name": "Photo Karte"}
    assert app.load_studio_settings(broken) == defaults
    assert app.load_studio_settings(tmp_path / "none.toml") == defaults

    assert "「studio TEST」の顧客カルテ作成アシスタント" in app.build_extraction_prompt("studio TEST")

    # 見本の設定ファイルは、公開用の既定の名前と同じ
    example = Path(app.__file__).resolve().parent / "studio_settings.example.toml"
    assert app.load_studio_settings(example) == defaults


# -----------------------------------------------------------------------------
# その他
# -----------------------------------------------------------------------------


def test_get_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.delenv("KARTE_DATA_DIR", raising=False)
    assert app.get_data_dir() == Path(app.__file__).resolve().parent / "data"
    monkeypatch.setenv("KARTE_DATA_DIR", str(tmp_path))
    assert app.get_data_dir() == tmp_path
    monkeypatch.setenv("KARTE_DATA_DIR", "sample_data")
    assert app.get_data_dir() == Path(app.__file__).resolve().parent / "sample_data"


def test_customer_request_counts():
    kartes = [make_karte(cid="C0001"), make_karte(cid="C0001"), make_karte(cid="C0002"), make_karte(cid="")]
    assert app.customer_request_counts(kartes) == Counter({"C0001": 2, "C0002": 1})
