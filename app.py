"""Photo Karte（フォト カルテ）

LINE公式アカウントのトーク履歴CSVから、写真スタジオ向けの「顧客カルテ」を作るアプリ。
スタジオ名・アプリ名は、公開しない設定ファイル studio_settings.toml で変更できる。

このファイルは次の2層で構成されています。
    1. ロジック層 … Streamlitに依存しない処理（test_app.py でテストする）
    2. UI層       … Streamlitの画面（`streamlit run app.py` で main() が動く）
"""

from __future__ import annotations

import codecs
import csv
import difflib
import html
import io
import os
import re
import subprocess
import sys
import tomllib
import unicodedata
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
import streamlit as st
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# =============================================================================
# 1. ロジック層
# =============================================================================

# -----------------------------------------------------------------------------
# 1-1. 定数・設定
# -----------------------------------------------------------------------------

APP_VERSION = "1.0.0"
DEFAULT_STUDIO_NAME = "studio SAMPLE"
DEFAULT_APP_NAME = "Photo Karte"
SETTINGS_FILENAME = "studio_settings.toml"


def load_studio_settings(path: Optional[Path] = None) -> dict[str, str]:
    """スタジオ名・アプリ名を設定ファイルから読む。

    設定ファイル（studio_settings.toml）は .gitignore に入っていて公開されない。
    ファイルが無い・壊れている場合は、公開用の架空の名前を使う。
    環境変数 KARTE_SETTINGS_FILE で、読むファイルを変更できる（テスト用）。
    """
    if path is None:
        env = os.environ.get("KARTE_SETTINGS_FILE")
        path = Path(env).expanduser() if env else Path(__file__).resolve().parent / SETTINGS_FILENAME
    settings = {"studio_name": DEFAULT_STUDIO_NAME, "app_name": DEFAULT_APP_NAME}
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return settings
    for key in settings:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
    return settings


_STUDIO_SETTINGS = load_studio_settings()
APP_NAME = _STUDIO_SETTINGS["app_name"]
STUDIO_NAME = _STUDIO_SETTINGS["studio_name"]

DEFAULT_MODEL = "gpt-6.1-sol"
MODEL_CHOICES = {
    "gpt-6.1-sol": "gpt-6.1-sol（標準・おすすめ）",
    "gpt-6-astra": "gpt-6-astra（最高性能・料金高め）",
    "gpt-6-luna": "gpt-6-luna（低コスト・高速）",
}
CUSTOM_MODEL_KEY = "__custom__"

STATUSES = ["お問い合わせ", "予約確定", "撮影済み", "納品済み", "キャンセル"]
ACTIVE_STATUSES = ("お問い合わせ", "予約確定")
SHOOT_TYPES = [
    "成人式",
    "ブライダル・前撮り",
    "七五三",
    "家族写真",
    "お宮参り・ニューボーン",
    "マタニティ",
    "入園・入学・卒業",
    "プロフィール",
    "商品撮影",
    "動画撮影",
    "その他",
]
PAYMENT_STATUSES = ["未入金", "一部入金", "入金済み", "不明"]
PAYMENT_ORDER = ["不明", "未入金", "一部入金", "入金済み"]
SOURCES = ["Instagram", "紹介", "Web検索", "LINE", "以前のご利用", "その他", "不明"]
SEASONS = ["春（桜）", "夏", "秋（紅葉）", "冬"]

# お子さまの行事（七五三は満年齢の年の11月、入学・二十歳のつどいは学年＝4月2日〜翌年4月1日生まれで決まる）
SHICHIGOSAN_AGES = (3, 5, 7)
HALF_YEAR = timedelta(days=182)

StatusLiteral = Literal[tuple(STATUSES)]
ShootTypeLiteral = Literal[tuple(SHOOT_TYPES)]
PaymentLiteral = Literal[tuple(PAYMENT_STATUSES)]
SourceLiteral = Literal[tuple(SOURCES)]

AI_CONVERSATION_LIMIT = 60_000  # AIに渡すやり取りの最大文字数
QA_CONTEXT_LIMIT = 40_000  # 質問のときにAIに渡すカルテ要約の最大文字数
QA_RAW_TOP_N = 3  # 原文も添えるカルテの数
QA_RAW_LIMIT = 3_000  # 添える原文の最大文字数（1件あたり）
BACKUP_KEEP = 30  # 残すバックアップの数
CANDIDATE_MIN_SCORE = 30  # 同じお客様の候補として表示する最低点
CANDIDATE_DEFAULT_SCORE = 70  # この点以上なら候補を初期選択にする
FILENAME_NAME_MAX = 30

CONVERSATION_HEADING = "## LINEのやり取り（原文）"
UNKNOWN_TS = "日時不明"
NO_NAME = "名前未登録"
EMPTY = "—"
WEEKDAYS_JA = "月火水木金土日"


def get_data_dir() -> Path:
    """データの保存先。環境変数 KARTE_DATA_DIR で変更できる（テスト・デモ用）。"""
    app_dir = Path(__file__).resolve().parent
    env = os.environ.get("KARTE_DATA_DIR")
    if env:
        path = Path(env).expanduser()
        return path if path.is_absolute() else app_dir / path
    return app_dir / "data"


# -----------------------------------------------------------------------------
# 1-2. データモデル
# -----------------------------------------------------------------------------


@dataclass
class Message:
    """LINEのやり取り1件。"""

    timestamp: str  # "YYYY/MM/DD HH:MM"。分からなければ ""
    sender: str
    role: str  # "customer" / "studio" / "unknown"
    text: str

    def key(self) -> tuple[str, str]:
        """重複判定に使うキー（日時と本文）。"""
        return (self.timestamp, normalize_text_for_key(self.text))


@dataclass
class ParsedChat:
    """CSVを読み込んだ結果。"""

    messages: list[Message]
    line_name: Optional[str]
    account_name: Optional[str]
    structured: bool  # 列を判定できたかどうか
    encoding: str
    source_file: str = ""

    @property
    def period(self) -> str:
        return chat_period(self.messages)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParsedChat:
        return cls(**{**data, "messages": messages_from_dicts(data.get("messages"))})


def messages_to_dicts(messages: list[Message]) -> list[dict[str, str]]:
    """画面の状態（session_state）に入れるため、ただの辞書にする。"""
    return [asdict(m) for m in messages]


def messages_from_dicts(data: Optional[list[dict[str, str]]]) -> list[Message]:
    return [Message(**d) for d in data or []]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class CustomerInfo(_Model):
    name: Optional[str] = None
    name_kana: Optional[str] = None
    line_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class ShootInfo(_Model):
    type: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD
    time: Optional[str] = None  # HH:MM
    season: Optional[str] = None
    location: Optional[str] = None
    people_count: Optional[int] = None
    family: Optional[str] = None
    costume: Optional[str] = None
    plan: Optional[str] = None
    options: Optional[str] = None
    price: Optional[int] = None
    delivery: Optional[str] = None
    payment_status: str = "不明"


class ProfileInfo(_Model):
    personality: Optional[str] = None
    preferences: Optional[str] = None
    requests: Optional[str] = None
    ng: Optional[str] = None
    tone: Optional[str] = None
    mood_tags: list[str] = Field(default_factory=list)


class Anniversary(_Model):
    label: str = ""
    date: Optional[str] = None


class Child(_Model):
    name: Optional[str] = None
    age: Optional[int] = None
    birthday: Optional[str] = None


class FutureInfo(_Model):
    source: str = "不明"
    source_detail: Optional[str] = None
    anniversaries: list[Anniversary] = Field(default_factory=list)
    children: list[Child] = Field(default_factory=list)
    review: Optional[str] = None
    next_opportunity: Optional[str] = None


class KarteRecord(_Model):
    """カルテ1件（＝ご依頼1件）。Markdownの front matter に保存する内容。"""

    request_id: str = ""
    customer_id: str = ""
    status: str = "お問い合わせ"
    registered_at: str = ""  # "YYYY-MM-DD HH:MM"
    updated_at: str = ""
    customer: CustomerInfo = Field(default_factory=CustomerInfo)
    shoot: ShootInfo = Field(default_factory=ShootInfo)
    profile: ProfileInfo = Field(default_factory=ProfileInfo)
    future: FutureInfo = Field(default_factory=FutureInfo)
    summary: list[str] = Field(default_factory=list)
    prep_checklist: list[str] = Field(default_factory=list)
    next_message: str = ""
    memo: str = ""
    source_file: str = ""
    message_count: int = 0
    chat_period: str = ""

    @property
    def display_name(self) -> str:
        return self.customer.name or self.customer.line_name or NO_NAME

    def shoot_date_value(self) -> Optional[date]:
        return parse_iso_date(self.shoot.date)

    def registered_dt(self) -> Optional[datetime]:
        try:
            return datetime.strptime(self.registered_at, "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return None

    def sort_date(self) -> date:
        """並べ替え用の日付（撮影日。未定なら登録日）。"""
        shoot = self.shoot_date_value()
        if shoot:
            return shoot
        registered = self.registered_dt()
        return registered.date() if registered else date.min


@dataclass
class Karte:
    """保存済みのカルテ（データ・やり取り・ファイルの場所）。"""

    record: KarteRecord
    messages: list[Message]
    path: Path


# --- AIに返してもらう形（Structured Outputs 用。全項目が必須・null可） -----------


class DraftAnniversary(BaseModel):
    label: str = Field(description="記念日の名前（例：結婚記念日、長女の誕生日）")
    date: Optional[str] = Field(description="YYYY-MM-DD。分からなければ null")


class DraftChild(BaseModel):
    name: Optional[str] = Field(description="お子さまの呼び名")
    age: Optional[int] = Field(description="やり取り時点の満年齢")
    birthday: Optional[str] = Field(description="誕生日 YYYY-MM-DD。分からなければ null")


class KarteDraft(BaseModel):
    customer_name: Optional[str] = Field(description="お客様の本名（名乗った名前）")
    customer_name_kana: Optional[str] = Field(description="お客様のお名前のふりがな")
    phone: Optional[str] = Field(description="電話番号")
    email: Optional[str] = Field(description="メールアドレス")
    status_guess: StatusLiteral = Field(description="やり取りから読み取れる進み具合")
    shoot_type: Optional[ShootTypeLiteral] = Field(description="撮影の種類")
    shoot_date: Optional[str] = Field(description="確定した撮影日 YYYY-MM-DD")
    shoot_time: Optional[str] = Field(description="撮影開始時刻 HH:MM")
    location: Optional[str] = Field(description="撮影場所")
    people_count: Optional[int] = Field(description="撮影する人数")
    family: Optional[str] = Field(description="家族構成・同行者")
    costume: Optional[str] = Field(description="衣装")
    plan: Optional[str] = Field(description="プラン")
    options: Optional[str] = Field(description="オプション")
    price: Optional[int] = Field(description="料金（税込・円）")
    delivery: Optional[str] = Field(description="納品方法")
    payment_status: PaymentLiteral = Field(description="お支払い状況")
    personality: Optional[str] = Field(description="雰囲気・人柄と、その根拠")
    preferences: Optional[str] = Field(description="好みの雰囲気・テイスト")
    requests: Optional[str] = Field(description="撮影へのご要望")
    ng: Optional[str] = Field(description="避けたいこと・配慮が必要なこと")
    tone: Optional[str] = Field(description="会話のトーン")
    mood_tags: list[str] = Field(description="雰囲気タグ（3〜6個、#なし）")
    source: SourceLiteral = Field(description="スタジオを知ったきっかけ")
    source_detail: Optional[str] = Field(description="知ったきっかけの詳細")
    anniversaries: list[DraftAnniversary] = Field(description="記念日")
    children: list[DraftChild] = Field(description="お子さま")
    review: Optional[str] = Field(description="撮影後の感想・口コミ")
    next_opportunity: Optional[str] = Field(description="次のご依頼につながりそうなこと")
    summary: list[str] = Field(description="ご依頼内容の3行まとめ")
    prep_checklist: list[str] = Field(description="撮影前の準備メモ（チェックリスト）")
    next_message: str = Field(description="次回のご案内文の下書き（200文字程度）")


class QAAnswer(BaseModel):
    answer: str = Field(description="質問への回答（Markdown）")
    referenced_ids: list[str] = Field(description="回答の根拠にしたカルテID")


# -----------------------------------------------------------------------------
# 1-3. 小さな共通処理（文字・日付）
# -----------------------------------------------------------------------------


def clean_str(value: Any) -> Optional[str]:
    """前後の空白を取り、空なら None にする。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_lines(values: Any) -> list[str]:
    """リストから空の要素を除き、前後の空白を取る。"""
    return [str(v).strip() for v in (values or []) if v is not None and str(v).strip()]


def clean_tags(tags: Any) -> list[str]:
    """雰囲気タグから「#」や空白を除き、重複をなくす。"""
    result: list[str] = []
    for tag in tags or []:
        for part in re.split(r"[\s,、，]+", str(tag)):
            part = part.strip().lstrip("#＃").strip()
            if part and part not in result:
                result.append(part)
    return result


def normalize_text_for_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_iso_date(value: Any) -> Optional[date]:
    """'2026-04-10' / '2026/4/10' / '2026年4月10日' を date に変換する。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = unicodedata.normalize("NFKC", str(value)).strip()
    match = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def clean_date(value: Any) -> Optional[str]:
    parsed = parse_iso_date(value)
    return parsed.isoformat() if parsed else None


def clean_time(value: Any) -> Optional[str]:
    """'10:00' / '9:5' / '10時' / '10時30分' を 'HH:MM' に変換する。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})(?:[:時](\d{1,2})分?|時)", text)
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def season_from_date(value: Any) -> Optional[str]:
    """撮影日からシーズンを決める。"""
    parsed = parse_iso_date(value)
    if parsed is None:
        return None
    month = parsed.month
    if 3 <= month <= 5:
        return "春（桜）"
    if 6 <= month <= 8:
        return "夏"
    if 9 <= month <= 11:
        return "秋（紅葉）"
    return "冬"


def normalize_timestamp(value: str) -> str:
    """CSVの日時を 'YYYY/MM/DD HH:MM' にそろえる。変換できなければ元の文字のまま返す。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if not match:
        return text
    try:
        day = date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return text
    rest = text[match.end():]
    time_match = re.search(r"(\d{1,2})[:時](\d{1,2})", rest)
    if not time_match:
        return day.strftime("%Y/%m/%d")
    hour, minute = int(time_match[1]), int(time_match[2])
    if ("午後" in rest or "pm" in rest.lower()) and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        return day.strftime("%Y/%m/%d")
    return f"{day:%Y/%m/%d} {hour:02d}:{minute:02d}"


def chat_period(messages: list[Message]) -> str:
    """やり取りの期間（'2026/09/01 〜 2026/10/10'）。"""
    days = sorted(m.timestamp[:10] for m in messages if re.match(r"\d{4}/\d{2}/\d{2}", m.timestamp))
    if not days:
        return ""
    return days[0] if days[0] == days[-1] else f"{days[0]} 〜 {days[-1]}"


def format_jp_date(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日（{WEEKDAYS_JA[value.weekday()]}）"


def format_price(price: Optional[int]) -> Optional[str]:
    return f"{price:,}円" if price is not None else None


def escape_markdown(text: str) -> str:
    """お客様の文章を、Markdownとして解釈されないように表示用に変換する。"""
    escaped = re.sub(r"([\\`*_\[\]<>#|~$])", r"\\\1", text or "")
    lines = []
    for line in escaped.split("\n"):
        line = re.sub(r"^(\s*)([-+])(\s)", r"\1\\\2\3", line)
        line = re.sub(r"^(\s*\d+)\.(\s)", r"\1\\.\2", line)
        lines.append(line)
    return "  \n".join(lines)


# -----------------------------------------------------------------------------
# 1-4. CSVの読み込み
# -----------------------------------------------------------------------------

_HEADER_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("sender_type", ("タイプ", "種別", "type")),
    ("datetime", ("日時", "datetime", "timestamp")),
    ("date", ("日付", "送信日", "date")),
    ("time", ("時刻", "時間", "time")),
    ("content", ("内容", "メッセージ", "本文", "テキスト", "message", "content", "text", "body")),
    ("sender_name", ("送信者", "名前", "ユーザー", "表示名", "sender", "name", "user", "from")),
]
_CUSTOMER_TYPE_WORDS = ("user", "ユーザー", "友だち", "友達", "お客", "customer")
_STUDIO_TYPE_WORDS = ("account", "アカウント", "管理", "スタッフ", "staff", "bot", "admin", "manager", "operator")


def decode_bytes(data: bytes) -> tuple[str, str]:
    """文字コードを自動で見分けて文字列にする。"""
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        text, encoding = data.decode("utf-16"), "utf-16"
    else:
        for encoding in ("utf-8-sig", "cp932"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text, encoding = data.decode("utf-8", errors="replace"), "utf-8（一部読めない文字あり）"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("ファイルが空です。LINE公式アカウントの管理画面から、もう一度CSVをダウンロードしてください。")
    return text, encoding


def _classify_header(cell: str) -> Optional[str]:
    header = unicodedata.normalize("NFKC", cell or "").strip().lower()
    if not header or len(header) > 20:
        return None
    for field_name, words in _HEADER_RULES:
        if any(word in header for word in words):
            return field_name
    return None


def _find_header(rows: list[list[str]]) -> tuple[Optional[int], dict[str, int]]:
    """先頭15行の中から見出しの行を探す。"""
    for index, row in enumerate(rows[:15]):
        mapping: dict[str, int] = {}
        for col, cell in enumerate(row):
            field_name = _classify_header(cell)
            if field_name and field_name not in mapping:
                mapping[field_name] = col
        if "content" in mapping and {"datetime", "date", "sender_name"} & mapping.keys():
            return index, mapping
    return None, {}


def _parse_preamble(rows: list[list[str]]) -> dict[str, str]:
    """見出しより前の行から、アカウント名・ユーザー名を読み取る。"""
    meta: dict[str, str] = {}
    for row in rows:
        cells = [c.strip() for c in row if c and c.strip()]
        if not cells:
            continue
        if len(cells) >= 2:
            key, value = cells[0], cells[1]
        else:
            match = re.match(r"^(.+?)[:：]\s*(.+)$", cells[0])
            if not match:
                continue
            key, value = match[1], match[2]
        key = unicodedata.normalize("NFKC", key).lower()
        if any(word in key for word in ("アカウント", "account")):
            meta.setdefault("account", value)
        elif any(word in key for word in ("ユーザー", "友だち", "友達", "トーク相手", "user", "表示名", "名前", "お客様")):
            meta.setdefault("user", value)
    return meta


def _role_from_type(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower()
    if not text:
        return "unknown"
    if any(word in text for word in _CUSTOMER_TYPE_WORDS):
        return "customer"
    if any(word in text for word in _STUDIO_TYPE_WORDS):
        return "studio"
    return "unknown"


def _clean_sender(value: str) -> str:
    """Markdownの見出しを壊す文字を置き換える。"""
    return re.sub(r"\s+", " ", (value or "").replace("*", "＊").replace("｜", "|")).strip()


def parse_line_csv(data: bytes, source_file: str = "") -> ParsedChat:
    """LINE公式アカウントのトーク履歴CSVを読み込む。

    列を判定できない場合は、全文を1件のメッセージとして返す（structured=False）。
    """
    text, encoding = decode_bytes(data)
    header_index: Optional[int] = None
    mapping: dict[str, int] = {}
    rows: list[list[str]] = []
    for delimiter in (",", "\t"):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error:
            continue
        header_index, mapping = _find_header(rows)
        if header_index is not None:
            break

    if header_index is None:
        return ParsedChat(
            messages=[Message("", "", "unknown", text.strip())],
            line_name=None,
            account_name=None,
            structured=False,
            encoding=encoding,
            source_file=source_file,
        )

    meta = _parse_preamble(rows[:header_index])

    def cell(row: list[str], name: str) -> str:
        col = mapping.get(name)
        return row[col].strip() if col is not None and col < len(row) else ""

    raw: list[tuple[str, str, str, str]] = []
    for row in rows[header_index + 1:]:
        if not any(c.strip() for c in row):
            continue
        content = cell(row, "content")
        if not content:
            continue
        if "datetime" in mapping:
            timestamp = normalize_timestamp(cell(row, "datetime"))
        else:
            timestamp = normalize_timestamp(f"{cell(row, 'date')} {cell(row, 'time')}".strip())
        raw.append((cell(row, "sender_type"), _clean_sender(cell(row, "sender_name")), timestamp, content))

    if not raw:
        raise ValueError("CSVの中にメッセージが見つかりませんでした。トーク履歴のCSVかどうか確認してください。")

    account = meta.get("account")
    studio_names = {normalize_name(n) for n in (account, STUDIO_NAME) if n}
    studio_found = any(normalize_name(sender) in studio_names for _, sender, _, _ in raw if sender)
    messages: list[Message] = []
    for sender_type, sender, timestamp, content in raw:
        role = _role_from_type(sender_type) if "sender_type" in mapping else "unknown"
        if role == "unknown" and studio_found and sender:
            role = "studio" if normalize_name(sender) in studio_names else "customer"
        messages.append(Message(timestamp, sender, role, content))

    line_name = meta.get("user")
    if not line_name:
        customer_senders = Counter(m.sender for m in messages if m.role == "customer" and m.sender)
        line_name = customer_senders.most_common(1)[0][0] if customer_senders else None

    return ParsedChat(
        messages=messages,
        line_name=line_name,
        account_name=account,
        structured=True,
        encoding=encoding,
        source_file=source_file,
    )


# -----------------------------------------------------------------------------
# 1-5. やり取りの表示・AI用の整形
# -----------------------------------------------------------------------------

ROLE_LABELS = {"customer": "お客様", "studio": "スタジオ"}


def speaker_label(message: Message) -> str:
    base = ROLE_LABELS.get(message.role)
    if base:
        return f"{base}（{message.sender}）" if message.sender else base
    return message.sender


def _label_to_role(label: str) -> tuple[str, str]:
    for role, base in ROLE_LABELS.items():
        if label == base:
            return role, ""
        if label.startswith(f"{base}（") and label.endswith("）"):
            return role, label[len(base) + 1:-1]
    return "unknown", label


def format_conversation_for_ai(
    messages: list[Message], line_name: Optional[str], today: date, limit: int = AI_CONVERSATION_LIMIT
) -> str:
    """AIに渡す入力文を作る。長すぎる場合は新しい方から limit 文字を残す。"""
    lines = []
    for m in messages:
        if not m.timestamp and not m.sender:
            lines.append(m.text)
        else:
            lines.append(f"[{m.timestamp or UNKNOWN_TS}] {speaker_label(m) or '（発言者不明）'}: {m.text}")
    conversation = "\n".join(lines)
    if len(conversation) > limit:
        conversation = "（古いやり取りは省略しました）\n" + conversation[-limit:]
    return (
        f"今日の日付: {today.isoformat()}\n"
        f"お客様のLINE名: {line_name or '不明'}\n\n"
        f"# やり取り\n{conversation}"
    )


# -----------------------------------------------------------------------------
# 1-6. AIの下書き → カルテ
# -----------------------------------------------------------------------------


def build_record_from_draft(
    draft: KarteDraft, messages: list[Message], line_name: Optional[str], source_file: str = ""
) -> KarteRecord:
    """AIの下書きを、保存用のカルテ（お客様ID・ファイル名はまだ無い状態）に変換する。"""
    shoot_date = clean_date(draft.shoot_date)
    return KarteRecord(
        status=draft.status_guess if draft.status_guess in STATUSES else "お問い合わせ",
        customer=CustomerInfo(
            name=clean_str(draft.customer_name),
            name_kana=clean_str(draft.customer_name_kana),
            line_name=clean_str(line_name),
            phone=clean_str(draft.phone),
            email=clean_str(draft.email),
        ),
        shoot=ShootInfo(
            type=draft.shoot_type if draft.shoot_type in SHOOT_TYPES else None,
            date=shoot_date,
            time=clean_time(draft.shoot_time),
            season=season_from_date(shoot_date),
            location=clean_str(draft.location),
            people_count=draft.people_count,
            family=clean_str(draft.family),
            costume=clean_str(draft.costume),
            plan=clean_str(draft.plan),
            options=clean_str(draft.options),
            price=draft.price,
            delivery=clean_str(draft.delivery),
            payment_status=draft.payment_status if draft.payment_status in PAYMENT_STATUSES else "不明",
        ),
        profile=ProfileInfo(
            personality=clean_str(draft.personality),
            preferences=clean_str(draft.preferences),
            requests=clean_str(draft.requests),
            ng=clean_str(draft.ng),
            tone=clean_str(draft.tone),
            mood_tags=clean_tags(draft.mood_tags),
        ),
        future=FutureInfo(
            source=draft.source if draft.source in SOURCES else "不明",
            source_detail=clean_str(draft.source_detail),
            anniversaries=[
                Anniversary(label=a.label.strip(), date=clean_date(a.date))
                for a in draft.anniversaries
                if a.label and a.label.strip()
            ],
            children=[
                Child(name=clean_str(c.name), age=c.age, birthday=clean_date(c.birthday))
                for c in draft.children
                if clean_str(c.name) or c.age is not None or clean_date(c.birthday)
            ],
            review=clean_str(draft.review),
            next_opportunity=clean_str(draft.next_opportunity),
        ),
        summary=clean_lines(draft.summary)[:3],
        prep_checklist=clean_lines(draft.prep_checklist),
        next_message=(draft.next_message or "").strip(),
        source_file=source_file,
        message_count=len(messages),
        chat_period=chat_period(messages),
    )


# -----------------------------------------------------------------------------
# 1-7. 同じお客様の照合
# -----------------------------------------------------------------------------


@dataclass
class Candidate:
    """同じお客様の候補。"""

    customer_id: str
    display_name: str
    score: int
    reasons: list[str]
    request_ids: list[str]


def normalize_name(value: Any) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(様|さま|さん)$", "", text)
    return text.lower()


def normalize_kana(value: Any) -> str:
    """ふりがなを、ひらがなにそろえる。"""
    text = normalize_name(value)
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def normalize_phone(value: Any) -> str:
    if not value:
        return ""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))
    if text.startswith("+81"):
        text = "0" + text[3:].lstrip("-")
    return re.sub(r"\D", "", text)


def normalize_email(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lower()


def _match_reasons(identity: CustomerInfo, record: KarteRecord) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    phone = normalize_phone(identity.phone)
    if len(phone) >= 8 and phone == normalize_phone(record.customer.phone):
        matches.append((100, "電話番号が一致"))
    email = normalize_email(identity.email)
    if email and email == normalize_email(record.customer.email):
        matches.append((90, "メールアドレスが一致"))
    name, other_name = normalize_name(identity.name), normalize_name(record.customer.name)
    if name and other_name:
        if name == other_name:
            matches.append((70, "お名前が一致"))
        elif min(len(name), len(other_name)) >= 2 and difflib.SequenceMatcher(None, name, other_name).ratio() >= 0.75:
            matches.append((30, "お名前が似ている"))
    kana = normalize_kana(identity.name_kana)
    if kana and kana == normalize_kana(record.customer.name_kana):
        matches.append((60, "ふりがなが一致"))
    line_name = normalize_name(identity.line_name)
    if line_name and line_name == normalize_name(record.customer.line_name):
        matches.append((50, "LINE名が一致"))
    return matches


def find_customer_candidates(identity: CustomerInfo, records: list[KarteRecord]) -> list[Candidate]:
    """保存済みのカルテから、同じお客様の候補を点数の高い順に返す。"""
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.customer_id:
            continue
        group = groups.setdefault(record.customer_id, {"matches": {}, "records": []})
        group["records"].append(record)
        for score, reason in _match_reasons(identity, record):
            group["matches"][reason] = max(score, group["matches"].get(reason, 0))

    candidates = []
    for customer_id, group in groups.items():
        if not group["matches"]:
            continue
        score = max(group["matches"].values())
        if score < CANDIDATE_MIN_SCORE:
            continue
        records_sorted = sorted(group["records"], key=lambda r: r.registered_at, reverse=True)
        reasons = [r for r, _ in sorted(group["matches"].items(), key=lambda item: -item[1])]
        candidates.append(
            Candidate(
                customer_id=customer_id,
                display_name=records_sorted[0].display_name,
                score=score,
                reasons=reasons,
                request_ids=[r.request_id for r in records_sorted],
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.customer_id))
    return candidates


def next_customer_id(records: list[KarteRecord]) -> str:
    numbers = [int(m[1]) for r in records if (m := re.fullmatch(r"C(\d+)", r.customer_id or ""))]
    return f"C{(max(numbers) + 1) if numbers else 1:04d}"


# -----------------------------------------------------------------------------
# 1-8. やり取りとカルテの統合（既存のご依頼の更新・新しいご依頼）
# -----------------------------------------------------------------------------


def _message_sort_key(message: Message) -> str:
    return message.timestamp if re.match(r"\d{4}/\d{2}/\d{2}", message.timestamp) else "~"


def merge_messages(old: list[Message], new: list[Message]) -> list[Message]:
    """2つのやり取りを、重複を除いて時刻順に統合する。"""
    seen = {m.key() for m in old}
    merged = list(old)
    for message in new:
        if message.key() not in seen:
            merged.append(message)
            seen.add(message.key())
    return sorted(merged, key=_message_sort_key)


def filter_new_messages(messages: list[Message], existing: list[Message]) -> list[Message]:
    """すでに保存済みのメッセージを除く。"""
    existing_keys = {m.key() for m in existing}
    return [m for m in messages if m.key() not in existing_keys]


def message_overlap(a: list[Message], b: list[Message]) -> int:
    """2つのやり取りで重なっているメッセージの数。"""
    return len({m.key() for m in a} & {m.key() for m in b})


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == "不明"


def fill_empty(new: BaseModel, old: BaseModel) -> BaseModel:
    """new の空の項目を old の値で埋めたコピーを返す（入れ子にも対応）。"""
    updates: dict[str, Any] = {}
    for name in type(new).model_fields:
        new_value, old_value = getattr(new, name), getattr(old, name, None)
        if isinstance(new_value, BaseModel) and isinstance(old_value, BaseModel):
            updates[name] = fill_empty(new_value, old_value)
        elif _is_empty(new_value) and not _is_empty(old_value):
            updates[name] = old_value
    return new.model_copy(update=updates, deep=True)


def _advance(order: list[str], old: str, new: str) -> str:
    """進み具合などを「先の段階」へだけ進める。"""
    old_index = order.index(old) if old in order else -1
    new_index = order.index(new) if new in order else -1
    return new if new_index > old_index else old


def merge_record_for_update(existing: KarteRecord, new: KarteRecord) -> KarteRecord:
    """既存のご依頼を、新しい下書きで更新する。"""
    merged: KarteRecord = fill_empty(new, existing)  # type: ignore[assignment]
    merged.request_id = existing.request_id
    merged.customer_id = existing.customer_id
    merged.registered_at = existing.registered_at
    merged.memo = existing.memo
    merged.status = _advance(STATUSES, existing.status, new.status)
    merged.shoot.payment_status = _advance(PAYMENT_ORDER, existing.shoot.payment_status, new.shoot.payment_status)
    merged.shoot.season = season_from_date(merged.shoot.date)
    return merged


# -----------------------------------------------------------------------------
# 1-9. Markdownの作成・読み込み
# -----------------------------------------------------------------------------

_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _KarteDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_KarteDumper.add_representer(str, _str_representer)

_MESSAGE_HEADER_RE = re.compile(r"^\*\*(?P<ts>[^｜*\n]+)｜(?P<label>[^*\n]*)\*\*\s*$")


def sanitize_filename_part(name: Optional[str]) -> str:
    text = unicodedata.normalize("NFC", name or "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(様|さま|さん)$", "", text)
    text = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", text).lstrip(".")
    return text[:FILENAME_NAME_MAX]


def make_filename(when: datetime, name: Optional[str], existing: set[str] | None = None) -> str:
    """'YYYYMMDD_HHMM_お名前.md' を作る。同じ名前があれば '_2' などを付ける。"""
    existing = existing or set()
    base = f"{when:%Y%m%d_%H%M}_{sanitize_filename_part(name) or NO_NAME}"
    filename, number = f"{base}.md", 2
    while filename in existing:
        filename, number = f"{base}_{number}.md", number + 1
    return filename


def _v(value: Any) -> str:
    """表示用。空なら「—」。"""
    if value is None or value == "" or value == []:
        return EMPTY
    return str(value).replace("\n", "<br>").replace("|", "｜")


def render_body(record: KarteRecord, include_title: bool = True) -> str:
    """人が読むためのカルテ本文（やり取りの原文は含まない）。"""
    c, s, p, f = record.customer, record.shoot, record.profile, record.future
    lines: list[str] = []
    if include_title:
        lines += [f"# {record.display_name} 様｜{s.type or '撮影の種類未設定'}（{s.date or '撮影日未定'}）", ""]
    lines += [
        f"> お客様ID：{record.customer_id or EMPTY}　／　進み具合：{record.status}　／　登録：{record.registered_at or EMPTY}",
        "",
        "## 3行まとめ",
    ]
    lines += [f"- {line}" for line in record.summary] or [f"- {EMPTY}"]
    lines += [
        "",
        "## 基本情報",
        "| 項目 | 内容 |",
        "|---|---|",
        f"| お名前 | {_v(c.name)} |",
        f"| ふりがな | {_v(c.name_kana)} |",
        f"| LINE名 | {_v(c.line_name)} |",
        f"| 電話番号 | {_v(c.phone)} |",
        f"| メール | {_v(c.email)} |",
        "",
        "## 撮影内容",
        "| 項目 | 内容 |",
        "|---|---|",
        f"| 撮影の種類 | {_v(s.type)} |",
        f"| 撮影日 | {_v(s.date)} |",
        f"| 時刻 | {_v(s.time)} |",
        f"| シーズン | {_v(s.season)} |",
        f"| 場所 | {_v(s.location)} |",
        f"| 人数 | {_v(f'{s.people_count}名' if s.people_count is not None else None)} |",
        f"| 家族構成 | {_v(s.family)} |",
        f"| 衣装 | {_v(s.costume)} |",
        f"| プラン | {_v(s.plan)} |",
        f"| オプション | {_v(s.options)} |",
        f"| 料金 | {_v(format_price(s.price))} |",
        f"| 納品方法 | {_v(s.delivery)} |",
        f"| お支払い | {_v(s.payment_status)} |",
        "",
        "## 雰囲気・人柄",
        f"タグ：{' '.join('#' + t for t in p.mood_tags) or EMPTY}",
        "",
        f"- 雰囲気・人柄：{_v(p.personality)}",
        f"- 好み：{_v(p.preferences)}",
        f"- ご要望：{_v(p.requests)}",
        f"- NGなこと：{_v(p.ng)}",
        f"- 会話のトーン：{_v(p.tone)}",
        "",
        "## 撮影前の準備メモ",
    ]
    lines += [f"- [ ] {item}" for item in record.prep_checklist] or [f"- {EMPTY}"]
    anniversaries = "、".join(f"{a.label}（{a.date or '日付不明'}）" for a in f.anniversaries)
    children = "、".join(
        f"{ch.name or 'お子さま'}（{f'{ch.age}歳' if ch.age is not None else '年齢不明'}"
        f"{f'・{ch.birthday}生まれ' if ch.birthday else ''}）"
        for ch in f.children
    )
    source = f.source + (f"（{f.source_detail}）" if f.source_detail else "")
    lines += [
        "",
        "## 今後に活かす情報",
        f"- どこで知ったか：{_v(source)}",
        f"- 記念日：{_v(anniversaries)}",
        f"- お子さま：{_v(children)}",
        f"- 感想・口コミ：{_v(f.review)}",
        f"- 次のご依頼のきっかけ：{_v(f.next_opportunity)}",
        "",
        "## 次回のご案内文（下書き）",
        record.next_message or EMPTY,
        "",
        "## メモ",
        record.memo or EMPTY,
    ]
    return "\n".join(lines)


def render_markdown(record: KarteRecord, messages: list[Message]) -> str:
    """カルテ1件分のMarkdown（front matter＋本文＋原文）を作る。"""
    front_matter = yaml.dump(
        record.model_dump(mode="python"),
        Dumper=_KarteDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    parts = ["---", front_matter.rstrip("\n"), "---", "", render_body(record), "", CONVERSATION_HEADING, ""]
    if not messages:
        parts += ["（やり取りはありません）", ""]
    for message in messages:
        parts += [f"**{message.timestamp or UNKNOWN_TS}｜{speaker_label(message)}**", message.text, ""]
    return "\n".join(parts).rstrip("\n") + "\n"


def _plain(value: Any) -> Any:
    """YAMLが日付型に変換した値を文字列に戻す。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def parse_conversation(section: str) -> list[Message]:
    messages: list[Message] = []
    current: Optional[tuple[str, str]] = None
    buffer: list[str] = []

    def flush() -> None:
        if current is None:
            return
        while buffer and not buffer[0].strip():
            buffer.pop(0)
        while buffer and not buffer[-1].strip():
            buffer.pop()
        timestamp, label = current
        role, sender = _label_to_role(label)
        messages.append(Message("" if timestamp == UNKNOWN_TS else timestamp, sender, role, "\n".join(buffer)))

    for line in section.split("\n"):
        match = _MESSAGE_HEADER_RE.match(line)
        if match:
            flush()
            current, buffer = (match["ts"], match["label"]), []
        elif current is not None:
            buffer.append(line)
    flush()
    return messages


def parse_markdown(text: str) -> tuple[KarteRecord, list[Message]]:
    """カルテのMarkdownを読み込む。"""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("ファイルの先頭に「---」で始まる項目欄（front matter）がありません。")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i] == "---")
    except StopIteration as exc:
        raise ValueError("項目欄（front matter）の終わりの「---」が見つかりません。") from exc
    data = yaml.load("\n".join(lines[1:end]), Loader=_YamlLoader) or {}
    record = KarteRecord.model_validate(_plain(data))
    body = "\n".join(lines[end + 1:])
    heading = re.search(rf"^{re.escape(CONVERSATION_HEADING)}\s*$", body, re.MULTILINE)
    messages = parse_conversation(body[heading.end():]) if heading else []
    return record, messages


# -----------------------------------------------------------------------------
# 1-10. 保存（ファイルの読み書き）
# -----------------------------------------------------------------------------


class KarteRepository:
    """data/customers/ のカルテを読み書きする。"""

    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.customers_dir = self.base / "customers"
        self.backups_dir = self.base / "backups"
        self.exports_dir = self.base / "exports"
        self.trash_dir = self.base / "trash"
        for directory in (self.customers_dir, self.backups_dir, self.exports_dir, self.trash_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.load_errors: list[str] = []

    def path_for(self, request_id: str) -> Path:
        return self.customers_dir / f"{request_id}.md"

    def signature(self) -> tuple:
        """ファイルの変更を検知するための値（画面のキャッシュ用）。"""
        return tuple(
            sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.customers_dir.glob("*.md"))
        )

    def load_all(self) -> list[Karte]:
        self.load_errors = []
        kartes = []
        for path in sorted(self.customers_dir.glob("*.md")):
            try:
                record, messages = parse_markdown(path.read_text(encoding="utf-8"))
            except (ValueError, yaml.YAMLError, ValidationError, UnicodeDecodeError) as exc:
                self.load_errors.append(f"{path.name}（{type(exc).__name__}）")
                continue
            if not record.request_id:
                record.request_id = path.stem
            kartes.append(Karte(record, messages, path))
        return kartes

    def load(self, request_id: str) -> Karte:
        path = self.path_for(request_id)
        record, messages = parse_markdown(path.read_text(encoding="utf-8"))
        return Karte(record, messages, path)

    def _write(self, path: Path, record: KarteRecord, messages: list[Message]) -> None:
        temp = path.with_suffix(".md.tmp")
        temp.write_text(render_markdown(record, messages), encoding="utf-8")
        os.replace(temp, path)

    def save_new(self, record: KarteRecord, messages: list[Message], now: Optional[datetime] = None) -> KarteRecord:
        """新しいカルテとして保存する（ファイル名・登録日時・お客様IDを決める）。"""
        now = now or datetime.now()
        record = record.model_copy(deep=True)
        existing_names = {p.name for p in self.customers_dir.glob("*.md")}
        filename = make_filename(now, record.customer.name or record.customer.line_name, existing_names)
        record.request_id = filename[:-3]
        record.registered_at = record.updated_at = now.strftime("%Y-%m-%d %H:%M")
        if not record.customer_id:
            record.customer_id = next_customer_id([k.record for k in self.load_all()])
        record.message_count = len(messages)
        record.chat_period = chat_period(messages)
        record.shoot.season = season_from_date(record.shoot.date)
        self._write(self.customers_dir / filename, record, messages)
        return record

    def update(self, record: KarteRecord, messages: list[Message], now: Optional[datetime] = None) -> KarteRecord:
        """既存のカルテを上書きする（ファイル名は変えない）。"""
        path = self.path_for(record.request_id)
        if not path.exists():
            raise FileNotFoundError(f"カルテ「{record.request_id}」が見つかりません。")
        record = record.model_copy(deep=True)
        record.updated_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        record.message_count = len(messages)
        record.chat_period = chat_period(messages)
        record.shoot.season = season_from_date(record.shoot.date)
        self._write(path, record, messages)
        return record

    def move_to_trash(self, request_id: str) -> Path:
        """カルテをゴミ箱フォルダへ移す（完全には消さない）。"""
        source = self.path_for(request_id)
        target = self.trash_dir / source.name
        number = 2
        while target.exists():
            target = self.trash_dir / f"{source.stem}_{number}.md"
            number += 1
        source.rename(target)
        return target


# -----------------------------------------------------------------------------
# 1-11. バックアップ
# -----------------------------------------------------------------------------


def list_backups(base_dir: Path) -> list[Path]:
    """バックアップのZIP（新しい順）。"""
    return sorted((Path(base_dir) / "backups").glob("karte_backup_*.zip"), key=lambda p: p.name, reverse=True)


def create_backup(base_dir: Path, keep: int = BACKUP_KEEP, now: Optional[datetime] = None) -> Path:
    """data/customers/ をZIPにまとめ、古いものは keep 個を残して消す。"""
    base_dir = Path(base_dir)
    backups_dir = base_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path, number = backups_dir / f"karte_backup_{stamp}.zip", 2
    while path.exists():
        path, number = backups_dir / f"karte_backup_{stamp}_{number}.zip", number + 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted((base_dir / "customers").glob("*.md")):
            archive.write(file, arcname=f"customers/{file.name}")
    for old in list_backups(base_dir)[keep:]:
        old.unlink()
    return path


# -----------------------------------------------------------------------------
# 1-12. 個人情報を伏せたコピー
# -----------------------------------------------------------------------------

MASK = "〇〇"
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+81[-\s]?|0)\d{1,4}[-\s(（]?\d{1,4}[-\s)）]?\d{3,4}")
_POSTAL_RE = re.compile(r"〒?\s?\d{3}-\d{4}")


def _known_strings(record: KarteRecord) -> list[str]:
    """伏せる対象の文字（名前など）を集める。長いものから順に置き換える。"""
    words: set[str] = set()
    for value in (record.customer.name, record.customer.name_kana):
        if not value:
            continue
        words.add(value.strip())
        words.add(re.sub(r"\s+", "", value))
        words.update(part for part in re.split(r"\s+", value.strip()) if len(part) >= 2)
    if record.customer.line_name and len(record.customer.line_name.strip()) >= 2:
        words.add(record.customer.line_name.strip())
    for child in record.future.children:
        if child.name:
            words.add(child.name.strip())
            short = re.sub(r"(ちゃん|くん|君|さん)$", "", child.name.strip())
            if len(short) >= 2:
                words.add(short)
    for value in (record.customer.email, record.customer.phone):
        if value:
            words.add(value.strip())
    return sorted((w for w in words if w), key=len, reverse=True)


def mask_text(text: str, known: list[str]) -> str:
    text = _EMAIL_RE.sub("***@***", text)
    text = _PHONE_RE.sub("***-****-****", text)
    text = _POSTAL_RE.sub("〒***-****", text)
    for word in known:
        text = text.replace(word, MASK)
    return text


def _mask_value(value: Any, known: list[str]) -> Any:
    if isinstance(value, str):
        return mask_text(value, known)
    if isinstance(value, dict):
        return {k: _mask_value(v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_value(v, known) for v in value]
    return value


def anonymize_record(record: KarteRecord, messages: list[Message]) -> tuple[KarteRecord, list[Message]]:
    """名前・連絡先などを伏せたコピーを作る（元のデータは変えない）。"""
    known = _known_strings(record)
    data = _mask_value(record.model_dump(mode="python"), known)
    masked = KarteRecord.model_validate(data)
    masked.request_id = anonymized_stem(record)
    masked.customer_id = record.customer_id
    masked.registered_at = record.registered_at
    masked.updated_at = record.updated_at
    masked.source_file = ""
    masked.customer = CustomerInfo(
        name=MASK if record.customer.name else None,
        name_kana=None,
        line_name=MASK if record.customer.line_name else None,
        phone="***" if record.customer.phone else None,
        email="***" if record.customer.email else None,
    )
    for index, child in enumerate(masked.future.children, start=1):
        child.name = f"お子さま{index}"
    masked_messages = [
        Message(
            m.timestamp,
            "" if m.role == "customer" else mask_text(m.sender, known),
            m.role,
            mask_text(m.text, known),
        )
        for m in messages
    ]
    return masked, masked_messages


def anonymized_stem(record: KarteRecord) -> str:
    registered = record.registered_dt()
    stamp = registered.strftime("%Y%m%d_%H%M") if registered else "00000000_0000"
    return f"{stamp}_{record.customer_id or 'C0000'}"


def export_anonymized(kartes: list[Karte], exports_dir: Path, now: Optional[datetime] = None) -> Path:
    """全カルテの匿名化コピーをフォルダとZIPに書き出し、ZIPの場所を返す。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    exports_dir = Path(exports_dir)
    out_dir, number = exports_dir / f"anonymized_{stamp}", 2
    while out_dir.exists():
        out_dir, number = exports_dir / f"anonymized_{stamp}_{number}", number + 1
    out_dir.mkdir(parents=True)
    used: set[str] = set()
    for karte in kartes:
        record, messages = anonymize_record(karte.record, karte.messages)
        stem, number = record.request_id, 2
        while f"{stem}.md" in used:
            stem, number = f"{record.request_id}_{number}", number + 1
        record.request_id = stem
        used.add(f"{stem}.md")
        (out_dir / f"{stem}.md").write_text(render_markdown(record, messages), encoding="utf-8")
    zip_path = out_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(out_dir.glob("*.md")):
            archive.write(file, arcname=f"{out_dir.name}/{file.name}")
    return zip_path


# -----------------------------------------------------------------------------
# 1-13. 検索・集計（よく使う質問ボタンなど）
# -----------------------------------------------------------------------------


def customer_request_counts(kartes: list[Karte]) -> Counter:
    return Counter(k.record.customer_id for k in kartes if k.record.customer_id)


def sort_kartes(kartes: list[Karte]) -> list[Karte]:
    """撮影日（未定なら登録日）の新しい順。"""
    return sorted(kartes, key=lambda k: (k.record.sort_date(), k.record.registered_at), reverse=True)


def search_text(karte: Karte) -> str:
    """キーワード検索の対象になる文字列。"""
    parts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)
        elif value is not None:
            parts.append(str(value))

    collect(karte.record.model_dump(mode="python"))
    parts += [m.text for m in karte.messages]
    return unicodedata.normalize("NFKC", "\n".join(parts)).lower()


def record_year(record: KarteRecord) -> Optional[int]:
    sort_date = record.sort_date()
    return sort_date.year if sort_date != date.min else None


def filter_records(
    kartes: list[Karte],
    keyword: str = "",
    types: tuple | list = (),
    year: Optional[int] = None,
    seasons: tuple | list = (),
    statuses: tuple | list = (),
    repeat_only: bool = False,
) -> list[Karte]:
    """一覧画面の絞り込み。キーワードは空白で区切るとすべてを含むものに絞る。"""
    counts = customer_request_counts(kartes)
    words = [w for w in re.split(r"\s+", unicodedata.normalize("NFKC", keyword or "").lower()) if w]
    result = []
    for karte in kartes:
        record = karte.record
        if types and record.shoot.type not in types:
            continue
        if year is not None and record_year(record) != year:
            continue
        if seasons and record.shoot.season not in seasons:
            continue
        if statuses and record.status not in statuses:
            continue
        if repeat_only and counts[record.customer_id] < 2:
            continue
        if words:
            text = search_text(karte)
            if not all(word in text for word in words):
                continue
        result.append(karte)
    return sort_kartes(result)


def _by_shoot_datetime(karte: Karte) -> tuple:
    return (karte.record.shoot_date_value() or date.max, karte.record.shoot.time or "")


def upcoming_shoots(kartes: list[Karte], today: date) -> list[Karte]:
    """これからの撮影予定（今日以降・お問い合わせ／予約確定）。"""
    items = [
        k
        for k in kartes
        if k.record.status in ACTIVE_STATUSES and (d := k.record.shoot_date_value()) is not None and d >= today
    ]
    return sorted(items, key=_by_shoot_datetime)


def undated_requests(kartes: list[Karte]) -> list[Karte]:
    """撮影日が決まっていない、進行中のご依頼。"""
    items = [k for k in kartes if k.record.status in ACTIVE_STATUSES and k.record.shoot_date_value() is None]
    return sorted(items, key=lambda k: k.record.registered_at, reverse=True)


def shoots_in_month(kartes: list[Karte], today: date) -> list[Karte]:
    items = [
        k
        for k in kartes
        if k.record.status != "キャンセル"
        and (d := k.record.shoot_date_value()) is not None
        and (d.year, d.month) == (today.year, today.month)
    ]
    return sorted(items, key=_by_shoot_datetime)


def same_season_last_year(kartes: list[Karte], today: date) -> tuple[str, int, list[Karte]]:
    """今と同じシーズンの、去年の撮影。"""
    season = season_from_date(today) or ""
    year = today.year - 1
    items = [
        k
        for k in kartes
        if k.record.status != "キャンセル"
        and (d := k.record.shoot_date_value()) is not None
        and d.year == year
        and season_from_date(d) == season
    ]
    return season, year, sorted(items, key=_by_shoot_datetime)


def unpaid_requests(kartes: list[Karte]) -> list[Karte]:
    items = [
        k
        for k in kartes
        if k.record.status != "キャンセル" and k.record.shoot.payment_status in ("未入金", "一部入金")
    ]
    return sorted(items, key=_by_shoot_datetime)


def next_shoot_checklist(kartes: list[Karte], today: date) -> Optional[Karte]:
    upcoming = upcoming_shoots(kartes, today)
    return upcoming[0] if upcoming else None


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 2月29日
        return value.replace(year=value.year + years, day=28)


def age_on(birthday: date, day: date) -> int:
    """満年齢。"""
    return day.year - birthday.year - ((day.month, day.day) < (birthday.month, birthday.day))


def estimate_birthday(child: Child, as_of: Optional[date]) -> tuple[Optional[date], bool]:
    """(誕生日, 正確かどうか)。年齢しか分からないときは、その年齢の真ん中（半年前）の日と見なす。"""
    birthday = parse_iso_date(child.birthday)
    if birthday:
        return birthday, True
    if child.age is not None and as_of is not None:
        return _shift_years(as_of, -child.age) - HALF_YEAR, False
    return None, False


def school_entry_year(birthday: date) -> int:
    """小学校に入学する年（4月2日〜翌年4月1日生まれが同じ学年）。"""
    return birthday.year + (7 if (birthday.month, birthday.day) >= (4, 2) else 6)


def child_event_dates(birthday: date) -> list[tuple[str, date]]:
    """お子さまが迎える行事と、そのおおよその日付。"""
    entry = school_entry_year(birthday)
    events = [(f"七五三（{n}歳）", date(birthday.year + n, 11, 15)) for n in SHICHIGOSAN_AGES]
    events += [
        ("小学校入学", date(entry, 4, 1)),
        ("中学校入学", date(entry + 6, 4, 1)),
        ("二十歳のつどい（成人式）", date(entry + 14, 1, 10)),
    ]
    return sorted(events, key=lambda event: event[1])


def upcoming_child_events(kartes: list[Karte], today: date) -> list[dict[str, str]]:
    """今日から来年末までに行事を迎えるお子さまを探す（目安）。"""
    latest: dict[tuple[str, str], tuple[Karte, Child]] = {}
    for karte in sorted(kartes, key=lambda k: k.record.registered_at):
        for index, child in enumerate(karte.record.future.children):
            key = (karte.record.customer_id, normalize_name(child.name) or f"#{index}")
            latest[key] = (karte, child)

    limit = date(today.year + 1, 12, 31)
    found: list[tuple[date, str, dict[str, str]]] = []
    for karte, child in latest.values():
        record = karte.record
        registered = record.registered_dt()
        birthday, exact = estimate_birthday(child, registered.date() if registered else None)
        if birthday is None:
            continue
        for event, when in child_event_dates(birthday):
            if not today <= when <= limit:
                continue
            row = {
                "時期": f"{when.year}年{when.month}月ごろ",
                "行事": event,
                "お客様": f"{record.display_name} 様",
                "お子さま": child.name or "お子さま",
                "今の年齢（目安）": f"{age_on(birthday, today)}歳",
                "計算のもと": "誕生日" if exact else "年齢から推定",
                "前回のご依頼": f"{record.shoot.type or '種類未設定'}（{record.shoot.date or '撮影日未定'}）",
                "カルテID": record.request_id,
            }
            found.append((when, row["お客様"], row))
    return [row for *_, row in sorted(found, key=lambda item: (item[0], item[1]))]


def karte_table_rows(kartes: list[Karte], counts: Counter) -> list[dict[str, str]]:
    """一覧表の行。"""
    rows = []
    for karte in kartes:
        record = karte.record
        shoot_date = record.shoot_date_value()
        count = counts.get(record.customer_id, 0)
        rows.append(
            {
                "撮影日": shoot_date.strftime("%Y/%m/%d") if shoot_date else "未定",
                "時刻": record.shoot.time or "",
                "お名前": f"{record.display_name} 様",
                "撮影の種類": record.shoot.type or "",
                "シーズン": record.shoot.season or "",
                "進み具合": record.status,
                "お支払い": record.shoot.payment_status,
                "ご依頼": f"🔁 {count}件" if count >= 2 else "初めて",
                "カルテID": record.request_id,
            }
        )
    return rows


# -----------------------------------------------------------------------------
# 1-14. AI（OpenAI）
# -----------------------------------------------------------------------------

EXTRACTION_PROMPT_TEMPLATE = """\
あなたは写真スタジオ「{studio_name}」の顧客カルテ作成アシスタントです。
LINE公式アカウントでのお客様とスタジオのやり取り（時系列のメッセージ）を読み、
カメラマンが後から見返すための「顧客カルテ」の下書きを作成します。

# 基本ルール
1. やり取りに書かれている事実だけを使い、推測で事実を作らない。分からない項目は null（リストは空配列）にする。
2. customer_name には、お客様が名乗った名前・予約時に伝えた名前を入れる。LINEの表示名がニックネームの場合は入れない。
3. 日付は YYYY-MM-DD、時刻は HH:MM（24時間表記）で書く。「来週の土曜」などの相対的な表現は、そのメッセージの送信日時を基準に計算する。
   撮影日が候補日のままで確定していない場合は shoot_date を null にし、候補日を requests に書く。
4. price は税込の合計金額を整数（円）で書く。分からなければ null。
5. 選択肢が決まっている項目は、必ず選択肢の中から選ぶ。どれにも当てはまらなければ「その他」、情報がなければ「不明」を選ぶ。
6. 文章はすべて日本語で、カメラマン本人が読むメモとして簡潔に書く。

# 各項目の書き方
- status_guess: やり取りから読み取れる進み具合。
- summary: ご依頼内容を3つの文でまとめる（1文40文字以内）。
- mood_tags: お客様の雰囲気・人柄を表す短いタグを3〜6個（「#」は付けない）。
  例：明るい、丁寧、こだわり強め、おまかせ派、お子さま人見知り
- personality / tone: 文面から受ける印象と、その根拠になった言動。
- preferences: 好みの雰囲気・テイスト。
- requests: 撮影へのご要望（撮りたいカット、背景、小物、同行者、候補日など）。
- ng: 避けたいこと、苦手なこと、配慮が必要なこと。
- prep_checklist: 撮影当日までに準備・確認すべきことを、1項目1文のチェックリストで書く。ご要望の撮り忘れ防止を最優先にする。
- payment_status: 入金済み・一部入金の連絡があれば、その状態にする。支払い方法や支払い時期の案内だけで、入金の連絡がなければ「未入金」。料金や支払いの話が一切なければ「不明」。
- source: どこでスタジオを知ったか。source_detail にはその具体的な内容。
- children: お子さまの呼び名と年齢（分かる範囲）。誕生日が分かれば birthday に YYYY-MM-DD。
- anniversaries: 誕生日・結婚記念日など、今後のご案内に使える記念日。
- review: 撮影後の感想・口コミ（あれば）。
- next_opportunity: 次のご依頼につながりそうなこと（例：下のお子さまが来年七五三、成人式の前撮りを検討中）。
- next_message: LINEでそのまま送れる、次回のご案内文の下書き（200文字程度）。
  お客様のお名前とやり取りの内容に触れ、季節（桜・紅葉）や今後の行事を踏まえた自然な文章にする。押し売りの印象を与えない。
"""


def build_extraction_prompt(studio_name: str) -> str:
    return EXTRACTION_PROMPT_TEMPLATE.format(studio_name=studio_name)


EXTRACTION_SYSTEM_PROMPT = build_extraction_prompt(STUDIO_NAME)

QA_SYSTEM_PROMPT = """\
あなたは写真スタジオのカメラマンを手伝うアシスタントです。
渡された「顧客カルテ」の情報だけを根拠に、カメラマンの質問に日本語で答えてください。

# ルール
1. カルテに書かれていないことは推測せず、「カルテには見当たりません」と答える。
2. 回答に使ったカルテの「カルテID」を referenced_ids にすべて入れる。
3. 人数や件数を答えるときは、該当するお客様のお名前も挙げる。
4. 箇条書きを使って短く答える。PCに不慣れな人が読むので、専門用語は使わない。
5. 「今月」「去年」などは、今日の日付を基準に解釈する。
"""

PRESET_REQUESTS_QUESTION = (
    "これまでのお客様から、よくいただくご要望・ご希望を、多い順に5つ教えてください。"
    "それぞれ、該当するお客様のお名前も挙げてください。"
)


class AIResponseError(Exception):
    """AIの返答を読み取れなかったとき。"""


def karte_card(karte: Karte) -> str:
    """AIに渡すカルテの要約カード。"""
    r = karte.record
    c, s, p, f = r.customer, r.shoot, r.profile, r.future
    children = "、".join(
        f"{ch.name or 'お子さま'}({f'{ch.age}歳' if ch.age is not None else '年齢不明'})" for ch in f.children
    )
    anniversaries = "、".join(f"{a.label} {a.date or ''}".strip() for a in f.anniversaries)
    lines = [
        f"### カルテID: {r.request_id}",
        f"お客様ID: {r.customer_id} / お名前: {r.display_name}（LINE名: {c.line_name or '不明'}）"
        f" / 進み具合: {r.status} / お支払い: {s.payment_status}",
        f"撮影: {s.type or '種類未設定'} / {s.date or '撮影日未定'} {s.time or ''} / {s.season or ''}"
        f" / 場所: {s.location or '不明'} / 人数: {s.people_count or '不明'} / 料金: {format_price(s.price) or '不明'}",
        f"3行まとめ: {' / '.join(r.summary)}",
        f"雰囲気タグ: {'、'.join(p.mood_tags)}",
        f"人柄: {p.personality or ''} / 好み: {p.preferences or ''} / ご要望: {p.requests or ''}"
        f" / NG: {p.ng or ''} / トーン: {p.tone or ''}",
        f"お子さま: {children} / 記念日: {anniversaries}",
        f"どこで知ったか: {f.source}{f'（{f.source_detail}）' if f.source_detail else ''}"
        f" / 感想: {f.review or ''} / 次のきっかけ: {f.next_opportunity or ''}",
    ]
    if r.memo:
        lines.append(f"メモ: {r.memo}")
    return "\n".join(lines)


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[\s、。,.!?！？「」『』（）()・/:：]+", "", unicodedata.normalize("NFKC", text).lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


def relevance(question: str, text: str) -> int:
    """質問とカルテの関連度（質問の2文字の組み合わせがいくつ含まれるか）。"""
    return len(_bigrams(question) & _bigrams(text))


def select_context(
    kartes: list[Karte],
    question: str,
    limit: int = QA_CONTEXT_LIMIT,
    raw_top_n: int = QA_RAW_TOP_N,
    raw_limit: int = QA_RAW_LIMIT,
) -> tuple[str, list[str]]:
    """AIに渡すカルテを選び、(渡す文章, 含めたカルテID) を返す。"""
    scored = []
    for karte in kartes:
        card = karte_card(karte)
        raw = "\n".join(m.text for m in karte.messages)
        scored.append((relevance(question, card + raw), karte.record.sort_date(), karte, card))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    total = sum(len(card) for *_, card in scored)
    selected = []
    used = 0
    for score, _, karte, card in scored:
        if total > limit and used + len(card) > limit:
            continue
        selected.append((score, karte, card))
        used += len(card)

    blocks = []
    for index, (score, karte, card) in enumerate(selected):
        block = card
        if index < raw_top_n and score > 0 and karte.messages:
            raw = "\n".join(f"[{m.timestamp or UNKNOWN_TS}] {speaker_label(m)}: {m.text}" for m in karte.messages)
            block += f"\nやり取りの原文（一部）:\n{raw[:raw_limit]}"
        blocks.append(block)
    return "\n\n".join(blocks), [karte.record.request_id for _, karte, _ in selected]


def build_qa_input(question: str, context: str, today: date) -> str:
    return f"今日の日付: {today.isoformat()}\n\n# 質問\n{question}\n\n# 顧客カルテ\n{context}"


class OpenAIService:
    """OpenAI APIを使う処理。テストでは client に偽物を渡せる。"""

    def __init__(self, api_key: str = "", model: str = DEFAULT_MODEL, client: Any = None):
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, timeout=180, max_retries=2)
        self.client = client
        self.model = model

    def _parse(self, instructions: str, user_input: str, schema: type[BaseModel]) -> Any:
        response = self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=user_input,
            text_format=schema,
            store=False,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise AIResponseError("AIの返答を読み取れませんでした。もう一度お試しください。")
        return parsed

    def extract_draft(self, messages: list[Message], line_name: Optional[str], today: date) -> KarteDraft:
        user_input = format_conversation_for_ai(messages, line_name, today)
        return self._parse(EXTRACTION_SYSTEM_PROMPT, user_input, KarteDraft)

    def answer_question(self, question: str, kartes: list[Karte], today: date) -> QAAnswer:
        if not kartes:
            return QAAnswer(answer="まだカルテが登録されていません。先に「取り込み」からカルテを作ってください。", referenced_ids=[])
        context, _ = select_context(kartes, question)
        result = self._parse(QA_SYSTEM_PROMPT, build_qa_input(question, context, today), QAAnswer)
        existing = {k.record.request_id for k in kartes}
        referenced = list(dict.fromkeys(rid for rid in result.referenced_ids if rid in existing))
        return QAAnswer(answer=result.answer, referenced_ids=referenced)


def friendly_error_message(exc: BaseException, model: str = DEFAULT_MODEL) -> str:
    """AIのエラーを、次に何をすればよいか分かる日本語にする。"""
    import openai

    if isinstance(exc, AIResponseError):
        return str(exc)
    if isinstance(exc, openai.AuthenticationError):
        return "APIキーが正しくないようです。サイドバーのAPIキーを、もう一度コピー＆ペーストしてください。"
    if isinstance(exc, openai.PermissionDeniedError):
        return f"このAPIキーでは、AIモデル「{model}」を使う権限がありません。サイドバーで別のモデルを選んでください。"
    if isinstance(exc, openai.NotFoundError):
        return f"AIモデル「{model}」が見つかりません。サイドバーのAIモデルを「{DEFAULT_MODEL}」に戻してください。"
    if isinstance(exc, openai.RateLimitError):
        if "insufficient_quota" in str(exc) or getattr(exc, "code", None) == "insufficient_quota":
            return "OpenAIの利用残高が足りません。OpenAIの管理画面で、残高（クレジット）を確認してください。"
        return "AIが混み合っています。1分ほど待ってから、もう一度お試しください。"
    if isinstance(exc, openai.APITimeoutError):
        return "AIの返答に時間がかかりすぎました。少し待ってから、もう一度お試しください。"
    if isinstance(exc, openai.APIConnectionError):
        return "インターネットに接続できませんでした。Wi-Fiがつながっているか確認してください。"
    if isinstance(exc, openai.BadRequestError):
        return "AIへのお願いの内容に問題がありました（やり取りが長すぎる可能性があります）。"
    return f"思わぬエラーが起きました。アプリを開き直してもう一度お試しください。（詳細：{exc}）"


# =============================================================================
# 2. UI層（Streamlit）
# =============================================================================

PAGES = [
    ("home", "🏠 ホーム"),
    ("import", "📥 取り込み"),
    ("list", "📋 カルテ一覧"),
    ("ask", "💬 AIに質問"),
    ("settings", "⚙️ 設定・管理"),
]
IMPORT_STEPS = ["ファイルを選ぶ", "お客様の確認", "内容の確認・保存"]
NEW_CUSTOMER = "__new_customer__"
NEW_REQUEST = "__new_request__"

CSS = """
<style>
.karte-brand {text-align: center; color: #333333; margin: 0.2rem 0 0.8rem 0; line-height: 1.5;}
.karte-brand .sub {font-size: 0.72rem; letter-spacing: 0.55em; padding-left: 0.55em; color: #8C6D3F;}
.karte-brand .main {font-size: 1.45rem; font-weight: 300; letter-spacing: 0.3em; padding-left: 0.3em;}
.karte-brand.large .main {font-size: 2.1rem;}
.karte-brand.large .sub {font-size: 0.85rem;}
[class*="st-key-bigbtn_"] button {min-height: 100px; border-radius: 14px;}
[class*="st-key-bigbtn_"] button p {font-size: 1.25rem; font-weight: 600;}
</style>
"""


def brand_html(large: bool = False) -> str:
    size = " large" if large else ""
    name = html.escape(APP_NAME)
    return f'<div class="karte-brand{size}"><div class="sub">studio</div><div class="main">{name}</div></div>'


def init_state() -> None:
    defaults: dict[str, Any] = {
        "page": "home",
        "api_key": "",
        "model_choice": DEFAULT_MODEL,
        "custom_model": "",
        "selected_id": None,
        "qa_history": [],
        "preset": None,
        "last_export": None,
        "uploader_nonce": 0,
        "imp_nonce": 0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    reset_import(keep_done=True, only_missing=True)


def reset_import(keep_done: bool = False, only_missing: bool = False) -> None:
    """取り込み画面の状態を初期化する。"""
    values = {
        "imp_step": 1,
        "imp_chat": None,
        "imp_record": None,
        "imp_keys": None,
        "imp_candidates": [],
        "imp_final_record": None,
        "imp_final_messages": None,
        "imp_mode": None,
        "imp_target_id": None,
    }
    if not keep_done:
        values["imp_done"] = None
    for key, value in values.items():
        if only_missing:
            st.session_state.setdefault(key, value)
        else:
            st.session_state[key] = value
    st.session_state.setdefault("imp_done", None)
    if not only_missing:
        st.session_state.imp_nonce += 1


def set_page(page: str) -> None:
    st.session_state.page = page
    if page != "list":
        st.session_state.selected_id = None


def open_karte(request_id: str, clear_key: Optional[str] = None) -> None:
    """カルテの詳細を開く。"""
    st.session_state.selected_id = request_id
    st.session_state.page = "list"
    if clear_key:
        st.session_state.pop(clear_key, None)
    st.rerun()


def current_model() -> str:
    choice = st.session_state.get("model_choice", DEFAULT_MODEL)
    if choice == CUSTOM_MODEL_KEY:
        return (st.session_state.get("custom_model") or "").strip() or DEFAULT_MODEL
    return choice


def get_service() -> Optional[OpenAIService]:
    api_key = (st.session_state.get("api_key") or "").strip()
    return OpenAIService(api_key=api_key, model=current_model()) if api_key else None


def has_api_key() -> bool:
    return bool((st.session_state.get("api_key") or "").strip())


@st.cache_data(show_spinner=False)
def _cached_load(base_dir: str, signature: tuple) -> tuple[list[Karte], list[str]]:
    repo = KarteRepository(Path(base_dir))
    kartes = repo.load_all()
    return kartes, repo.load_errors


def load_kartes(repo: KarteRepository) -> tuple[list[Karte], list[str]]:
    return _cached_load(str(repo.base), repo.signature())


def after_write(repo: KarteRepository) -> None:
    """保存・更新・削除のあとに自動バックアップを取る。"""
    try:
        create_backup(repo.base)
    except OSError as exc:
        st.toast(f"自動バックアップに失敗しました：{exc}", icon="⚠️")


def render_sidebar() -> None:
    with st.sidebar:
        st.markdown(brand_html(), unsafe_allow_html=True)
        st.text_input(
            "🔑 OpenAI APIキー",
            type="password",
            key="api_key",
            placeholder="sk-...",
            help="取り込み（AIで整理）と、AIへの質問に使います。キーはこの画面を開いている間だけ使い、保存しません。",
        )
        if has_api_key():
            st.caption("✅ APIキーが入力されています")
        else:
            st.caption("⚠️ 取り込みとAIへの質問に必要です")
        st.selectbox(
            "🤖 AIモデル",
            options=[*MODEL_CHOICES, CUSTOM_MODEL_KEY],
            format_func=lambda key: MODEL_CHOICES.get(key, "その他（モデル名を入力）"),
            key="model_choice",
        )
        if st.session_state.model_choice == CUSTOM_MODEL_KEY:
            st.text_input("モデル名", key="custom_model", placeholder=DEFAULT_MODEL)
        st.divider()
        st.markdown("**メニュー**")
        for page, label in PAGES:
            st.button(
                label,
                key=f"nav_{page}",
                type="primary" if st.session_state.page == page else "secondary",
                width="stretch",
                on_click=set_page,
                args=(page,),
            )
        st.caption(f"{APP_NAME} v{APP_VERSION}")


def show_karte_table(kartes: list[Karte], counts: Counter, key: str) -> None:
    """カルテの表。行をクリックすると詳細を開く。"""
    if not kartes:
        return
    df = pd.DataFrame(karte_table_rows(kartes, counts))
    event = st.dataframe(
        df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key=key
    )
    rows = event.selection.rows if event is not None else []
    if rows:
        open_karte(kartes[rows[0]].record.request_id, clear_key=key)


def render_chat_log(messages: list[Message]) -> None:
    """やり取りを吹き出しで表示する。"""
    if not messages:
        st.caption("やり取りはありません。")
        return
    for message in messages:
        if message.role == "customer":
            name, avatar = "user", "🙂"
        elif message.role == "studio":
            name, avatar = "assistant", "📷"
        else:
            name, avatar = "assistant", "💬"
        with st.chat_message(name, avatar=avatar):
            label = speaker_label(message)
            st.caption(f"{message.timestamp or UNKNOWN_TS}{f'｜{label}' if label else ''}")
            st.markdown(escape_markdown(message.text))


def _cell(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return clean_str(value)


def _lines(text: str) -> list[str]:
    return clean_lines((text or "").split("\n"))


def render_record_editor(record: KarteRecord, prefix: str, submit_label: str) -> Optional[KarteRecord]:
    """カルテの全項目を修正するフォーム。保存ボタンが押されたら修正後のカルテを返す。"""
    c, s, p, f = record.customer, record.shoot, record.profile, record.future
    with st.form(key=f"{prefix}_form", border=True):
        st.markdown("##### 👤 基本情報")
        col1, col2 = st.columns(2)
        name = col1.text_input("お名前", value=c.name or "", key=f"{prefix}_name")
        kana = col2.text_input("ふりがな", value=c.name_kana or "", key=f"{prefix}_kana")
        line_name = col1.text_input("LINE名", value=c.line_name or "", key=f"{prefix}_line")
        phone = col2.text_input("電話番号", value=c.phone or "", key=f"{prefix}_phone")
        email = col1.text_input("メールアドレス", value=c.email or "", key=f"{prefix}_email")
        status = col2.selectbox(
            "進み具合", STATUSES, index=STATUSES.index(record.status) if record.status in STATUSES else 0,
            key=f"{prefix}_status",
        )
        customer_id = col1.text_input(
            "お客様ID", value=record.customer_id, key=f"{prefix}_customer_id",
            placeholder="保存するときに自動で決まります",
            help="同じお客様のカルテをまとめたいときは、もう一方のお客様ID（例：C0003）に書き換えてください。",
        )

        st.markdown("##### 📷 撮影内容")
        col1, col2, col3 = st.columns(3)
        type_options = ["（未選択）", *SHOOT_TYPES]
        shoot_type = col1.selectbox(
            "撮影の種類", type_options,
            index=type_options.index(s.type) if s.type in SHOOT_TYPES else 0, key=f"{prefix}_type",
        )
        shoot_date = col2.date_input(
            "撮影日（未定なら空欄）", value=parse_iso_date(s.date), format="YYYY/MM/DD", key=f"{prefix}_date"
        )
        shoot_time = col3.text_input("開始時刻（例 10:00）", value=s.time or "", key=f"{prefix}_time")
        undecided = col2.checkbox("撮影日は未定", value=s.date is None, key=f"{prefix}_undecided")
        location = col1.text_input("場所", value=s.location or "", key=f"{prefix}_location")
        people = col3.number_input("人数", min_value=0, max_value=100, value=s.people_count, step=1, key=f"{prefix}_people")
        family = st.text_input("家族構成・同行者", value=s.family or "", key=f"{prefix}_family")
        col1, col2 = st.columns(2)
        costume = col1.text_input("衣装", value=s.costume or "", key=f"{prefix}_costume")
        plan = col2.text_input("プラン", value=s.plan or "", key=f"{prefix}_plan")
        options = col1.text_input("オプション", value=s.options or "", key=f"{prefix}_options")
        delivery = col2.text_input("納品方法", value=s.delivery or "", key=f"{prefix}_delivery")
        price = col1.number_input("料金（円・税込）", min_value=0, value=s.price, step=1000, key=f"{prefix}_price")
        payment = col2.selectbox(
            "お支払い", PAYMENT_STATUSES,
            index=PAYMENT_STATUSES.index(s.payment_status) if s.payment_status in PAYMENT_STATUSES else 3,
            key=f"{prefix}_payment",
        )

        st.markdown("##### 📝 3行まとめ")
        summary = st.text_area("1行に1つ", value="\n".join(record.summary), height=100, key=f"{prefix}_summary")

        st.markdown("##### 🙂 雰囲気・人柄")
        tags = st.text_input(
            "雰囲気タグ（スペースで区切る）", value=" ".join(f"#{t}" for t in p.mood_tags), key=f"{prefix}_tags"
        )
        col1, col2 = st.columns(2)
        personality = col1.text_area("雰囲気・人柄", value=p.personality or "", height=90, key=f"{prefix}_personality")
        tone = col2.text_area("会話のトーン", value=p.tone or "", height=90, key=f"{prefix}_tone")
        preferences = col1.text_area("好み", value=p.preferences or "", height=90, key=f"{prefix}_preferences")
        requests = col2.text_area("ご要望", value=p.requests or "", height=90, key=f"{prefix}_requests")
        ng = st.text_area("NGなこと・配慮が必要なこと", value=p.ng or "", height=70, key=f"{prefix}_ng")

        st.markdown("##### ✅ 撮影前の準備メモ")
        checklist = st.text_area(
            "1行に1つ", value="\n".join(record.prep_checklist), height=120, key=f"{prefix}_checklist"
        )

        st.markdown("##### 🌱 今後に活かす情報")
        col1, col2 = st.columns(2)
        source = col1.selectbox(
            "どこで知ったか", SOURCES, index=SOURCES.index(f.source) if f.source in SOURCES else len(SOURCES) - 1,
            key=f"{prefix}_source",
        )
        source_detail = col2.text_input("どこで知ったか（詳しく）", value=f.source_detail or "", key=f"{prefix}_source_detail")
        st.caption("記念日（行を追加・削除できます）")
        anniversaries_df = st.data_editor(
            pd.DataFrame(
                {
                    "記念日の名前": pd.Series([a.label for a in f.anniversaries], dtype="object"),
                    "日付（YYYY-MM-DD）": pd.Series([a.date for a in f.anniversaries], dtype="object"),
                }
            ),
            num_rows="dynamic", hide_index=True, width="stretch", key=f"{prefix}_anniversaries",
        )
        st.caption("お子さま（行を追加・削除できます）")
        children_df = st.data_editor(
            pd.DataFrame(
                {
                    "呼び名": pd.Series([ch.name for ch in f.children], dtype="object"),
                    "年齢": pd.Series([ch.age for ch in f.children], dtype="Int64"),
                    "誕生日（YYYY-MM-DD）": pd.Series([ch.birthday for ch in f.children], dtype="object"),
                }
            ),
            num_rows="dynamic", hide_index=True, width="stretch", key=f"{prefix}_children",
            column_config={"年齢": st.column_config.NumberColumn(min_value=0, max_value=120, step=1)},
        )
        review = st.text_area("感想・口コミ", value=f.review or "", height=70, key=f"{prefix}_review")
        next_opportunity = st.text_area(
            "次のご依頼のきっかけ", value=f.next_opportunity or "", height=70, key=f"{prefix}_next_opportunity"
        )

        st.markdown("##### ✉️ 次回のご案内文（下書き）")
        next_message = st.text_area("LINEに貼り付ける文章", value=record.next_message, height=140, key=f"{prefix}_next_message")
        st.markdown("##### 🗒️ メモ（自由に書けます）")
        memo = st.text_area("メモ", value=record.memo, height=100, key=f"{prefix}_memo", label_visibility="collapsed")

        submitted = st.form_submit_button(submit_label, type="primary", width="stretch")

    if not submitted:
        return None

    anniversaries = []
    for _, row in anniversaries_df.iterrows():
        label = _cell(row.get("記念日の名前"))
        if label:
            anniversaries.append(Anniversary(label=label, date=clean_date(_cell(row.get("日付（YYYY-MM-DD）")))))
    children = []
    for _, row in children_df.iterrows():
        child_name, birthday = _cell(row.get("呼び名")), clean_date(_cell(row.get("誕生日（YYYY-MM-DD）")))
        age_value = row.get("年齢")
        age = None if age_value is None or pd.isna(age_value) else int(age_value)
        if child_name or age is not None or birthday:
            children.append(Child(name=child_name, age=age, birthday=birthday))

    final_date = None if undecided or shoot_date is None else shoot_date.isoformat()
    return record.model_copy(
        update={
            "customer_id": unicodedata.normalize("NFKC", customer_id or "").strip().upper() or record.customer_id,
            "status": status,
            "customer": CustomerInfo(
                name=clean_str(name), name_kana=clean_str(kana), line_name=clean_str(line_name),
                phone=clean_str(phone), email=clean_str(email),
            ),
            "shoot": ShootInfo(
                type=None if shoot_type == "（未選択）" else shoot_type,
                date=final_date,
                time=clean_time(shoot_time) or clean_str(shoot_time),
                season=season_from_date(final_date),
                location=clean_str(location),
                people_count=int(people) if people is not None else None,
                family=clean_str(family),
                costume=clean_str(costume),
                plan=clean_str(plan),
                options=clean_str(options),
                price=int(price) if price is not None else None,
                delivery=clean_str(delivery),
                payment_status=payment,
            ),
            "profile": ProfileInfo(
                personality=clean_str(personality), preferences=clean_str(preferences),
                requests=clean_str(requests), ng=clean_str(ng), tone=clean_str(tone),
                mood_tags=clean_tags([tags]),
            ),
            "future": FutureInfo(
                source=source, source_detail=clean_str(source_detail), anniversaries=anniversaries,
                children=children, review=clean_str(review), next_opportunity=clean_str(next_opportunity),
            ),
            "summary": _lines(summary),
            "prep_checklist": _lines(checklist),
            "next_message": (next_message or "").strip(),
            "memo": (memo or "").strip(),
        },
        deep=True,
    )


# --- 🏠 ホーム -------------------------------------------------------------------


def page_home(kartes: list[Karte], today: date) -> None:
    st.markdown(brand_html(large=True), unsafe_allow_html=True)
    st.markdown(f"<p style='text-align:center'>今日は {format_jp_date(today)} です</p>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    col1.button("📥 LINEのやり取りを取り込む", key="bigbtn_import", width="stretch", type="primary",
                on_click=set_page, args=("import",))
    col2.button("📋 カルテ一覧を見る", key="bigbtn_list", width="stretch", on_click=set_page, args=("list",))
    col1, col2 = st.columns(2)
    col1.button("💬 AIに質問する", key="bigbtn_ask", width="stretch", on_click=set_page, args=("ask",))
    col2.button("⚙️ 設定・バックアップ", key="bigbtn_settings", width="stretch", on_click=set_page, args=("settings",))

    st.markdown("#### 進み具合")
    status_counts = Counter(k.record.status for k in kartes)
    for col, status in zip(st.columns(len(STATUSES)), STATUSES, strict=True):
        col.metric(status, f"{status_counts.get(status, 0)}件", border=True)

    counts = customer_request_counts(kartes)
    st.markdown("#### 📅 これからの撮影予定")
    upcoming = upcoming_shoots(kartes, today)
    if upcoming:
        st.caption("行をクリックすると、カルテが開きます。")
        show_karte_table(upcoming, counts, key="home_upcoming")
    else:
        st.info("これからの撮影予定はありません。")

    undated = undated_requests(kartes)
    if undated:
        with st.expander(f"📝 撮影日がまだ決まっていないご依頼（{len(undated)}件）"):
            show_karte_table(undated, counts, key="home_undated")

    st.markdown("#### 🕒 最近登録したカルテ")
    recent = sorted(kartes, key=lambda k: k.record.registered_at, reverse=True)[:5]
    if recent:
        show_karte_table(recent, counts, key="home_recent")
    else:
        st.info("まだカルテがありません。「📥 LINEのやり取りを取り込む」から始めましょう。")


# --- 📥 取り込み ------------------------------------------------------------------


def render_import_steps(step: int) -> None:
    st.progress(step / len(IMPORT_STEPS), text=f"ステップ {step}/{len(IMPORT_STEPS)}：{IMPORT_STEPS[step - 1]}")


def page_import(repo: KarteRepository, kartes: list[Karte], today: date) -> None:
    st.markdown("### 📥 LINEのやり取りを取り込む")

    done = st.session_state.imp_done
    if done:
        st.success(f"保存しました：{done}.md（自動でバックアップも取りました）")
        col1, col2 = st.columns(2)
        if col1.button("📋 このカルテを見る", width="stretch", type="primary"):
            st.session_state.imp_done = None
            open_karte(done)
        if col2.button("📥 続けて取り込む", width="stretch"):
            st.session_state.imp_done = None
            st.session_state.uploader_nonce += 1
            st.rerun()
        return

    step = st.session_state.imp_step
    render_import_steps(step)
    if step == 1:
        _import_step1(kartes, today)
    elif step == 2:
        _import_step2(kartes, today)
    else:
        _import_step3(repo, kartes)


def _import_step1(kartes: list[Karte], today: date) -> None:
    st.markdown("LINE公式アカウントの管理画面からダウンロードした、**トーク履歴のCSVファイル**を選んでください。")
    with st.expander("CSVのダウンロード方法を見る"):
        st.markdown(
            "1. PCで **LINE Official Account Manager** にログインします。\n"
            "2. 上のメニューの **「チャット」** を開きます。\n"
            "3. 取り込みたいお客様のトークを開きます。\n"
            "4. トーク画面のメニューから **トーク履歴のダウンロード（CSV）** を選びます。\n\n"
            "※ メニューの名前や場所は、LINE側の変更で変わることがあります。"
        )
    uploaded = st.file_uploader(
        "CSVファイルを選ぶ（ここにドラッグ＆ドロップもできます）",
        type=["csv"],
        key=f"uploader_{st.session_state.uploader_nonce}",
    )
    if uploaded is None:
        return
    try:
        chat = parse_line_csv(uploaded.getvalue(), source_file=uploaded.name)
    except ValueError as exc:
        st.error(str(exc))
        return

    st.success(f"読み込めました：メッセージ {len(chat.messages)}件　{chat.period}")
    if not chat.structured:
        st.warning("CSVの形式を自動で判定できなかったため、中身をそのままAIに渡して整理します。")
    st.write(f"お客様のLINE名：**{chat.line_name or '（CSVからは分かりませんでした）'}**")
    with st.expander("中身を確認する（最初の10件）"):
        render_chat_log(chat.messages[:10])

    if not has_api_key():
        st.warning("AIで整理するには、左のサイドバーに **OpenAI APIキー** を入力してください。")
    if st.button("🤖 AIで整理する（1分ほどかかります）", type="primary", width="stretch", disabled=not has_api_key()):
        service = get_service()
        assert service is not None
        with st.spinner("AIがやり取りを読んで、カルテの下書きを作っています…"):
            try:
                draft = service.extract_draft(chat.messages, chat.line_name, today)
            except Exception as exc:  # noqa: BLE001 - 画面に分かりやすく表示する
                st.error(friendly_error_message(exc, current_model()))
                return
        record = build_record_from_draft(draft, chat.messages, chat.line_name, chat.source_file)
        candidates = find_customer_candidates(record.customer, [k.record for k in kartes])
        # 画面の状態には、ただの辞書を入れる（Streamlitは再実行のたびにクラスを作り直すため）
        st.session_state.imp_chat = chat.to_dict()
        st.session_state.imp_record = record.model_dump()
        st.session_state.imp_keys = [m.key() for m in chat.messages]
        st.session_state.imp_candidates = [asdict(c) for c in candidates]
        st.session_state.imp_step = 2
        st.rerun()


def _request_label(karte: Karte) -> str:
    r = karte.record
    return f"{r.shoot.date or '撮影日未定'}　{r.shoot.type or '種類未設定'}（{r.status}）"


def _import_step2(kartes: list[Karte], today: date) -> None:
    chat = ParsedChat.from_dict(st.session_state.imp_chat)
    record = KarteRecord.model_validate(st.session_state.imp_record)
    candidates = [Candidate(**c) for c in st.session_state.imp_candidates]
    by_id = {k.record.request_id: k for k in kartes}

    st.markdown("#### このお客様は、以前にご依頼いただいた方ですか？")
    c = record.customer
    st.caption(
        f"今回のお客様：{record.display_name}（LINE名：{c.line_name or '—'}／電話：{c.phone or '—'}／メール：{c.email or '—'}）"
    )
    options = [NEW_CUSTOMER, *[cand.customer_id for cand in candidates]]
    labels = {NEW_CUSTOMER: "🆕 はじめてのお客様として登録する"}
    for cand in candidates:
        labels[cand.customer_id] = (
            f"🔁 {cand.display_name} 様（{cand.customer_id}）— {'・'.join(cand.reasons)}／過去のご依頼 {len(cand.request_ids)}件"
        )
    default = 1 if candidates and candidates[0].score >= CANDIDATE_DEFAULT_SCORE else 0
    if not candidates:
        st.info("過去のカルテに、同じお客様らしい方は見つかりませんでした。")
    choice = st.radio(
        "お客様を選んでください", options, index=default, format_func=labels.get,
        key=f"imp_customer_{st.session_state.imp_nonce}",
    )

    request_choice = NEW_REQUEST
    customer_kartes: list[Karte] = []
    if choice != NEW_CUSTOMER:
        customer_kartes = sort_kartes([k for k in kartes if k.record.customer_id == choice])
        with st.container(border=True):
            st.markdown("**このお客様の過去のご依頼**")
            for karte in customer_kartes:
                st.markdown(f"- {_request_label(karte)}：{' / '.join(karte.record.summary[:1]) or EMPTY}")
        overlaps = {k.record.request_id: message_overlap(chat.messages, k.messages) for k in customer_kartes[:5]}
        request_options = [NEW_REQUEST, *overlaps]
        request_labels = {NEW_REQUEST: "🆕 新しいご依頼として登録する"}
        for request_id, overlap in overlaps.items():
            note = f"（やり取りが{overlap}件重なっています）" if overlap else ""
            request_labels[request_id] = f"✏️ このご依頼を更新する：{_request_label(by_id[request_id])}{note}"
        best = max(overlaps.items(), key=lambda item: item[1], default=(None, 0))
        request_default = request_options.index(best[0]) if best[1] > 0 else 0
        request_choice = st.radio(
            "今回のやり取りは、どちらですか？", request_options, index=request_default,
            format_func=request_labels.get, key=f"imp_request_{st.session_state.imp_nonce}_{choice}",
        )

    col1, col2 = st.columns(2)
    if col1.button("← ファイルを選び直す", width="stretch"):
        reset_import()
        st.rerun()
    if not col2.button("次へ →", type="primary", width="stretch"):
        return

    # 今回保存するやり取りを決める
    if choice == NEW_CUSTOMER:
        mode, messages, target = "new_customer", chat.messages, None
    elif request_choice == NEW_REQUEST:
        existing = [m for k in customer_kartes for m in k.messages]
        mode, messages, target = "new_request", filter_new_messages(chat.messages, existing), None
        if not messages:
            st.warning("新しいやり取りが見つかりませんでした。すべて過去のカルテに保存済みです。「このご依頼を更新する」を選んでください。")
            return
    else:
        target = by_id[request_choice]
        mode, messages = "update", merge_messages(target.messages, chat.messages)

    # やり取りが変わった場合は、AIでもう一度整理する
    if [m.key() for m in messages] != st.session_state.imp_keys:
        service = get_service()
        if service is None:
            st.warning("AIで整理し直すために、サイドバーに OpenAI APIキー を入力してください。")
            return
        with st.spinner("過去のやり取りと合わせて、AIがもう一度整理しています…"):
            try:
                draft = service.extract_draft(messages, chat.line_name, today)
            except Exception as exc:  # noqa: BLE001
                st.error(friendly_error_message(exc, current_model()))
                return
        record = build_record_from_draft(draft, messages, chat.line_name, chat.source_file)

    if mode == "new_request":
        record = record.model_copy(deep=True)
        record.customer_id = choice
        record.customer = fill_empty(record.customer, customer_kartes[0].record.customer)
    elif mode == "update":
        record = merge_record_for_update(target.record, record)

    st.session_state.imp_final_record = record.model_dump()
    st.session_state.imp_final_messages = messages_to_dicts(messages)
    st.session_state.imp_mode = mode
    st.session_state.imp_target_id = target.record.request_id if target else None
    st.session_state.imp_step = 3
    st.rerun()


def _import_step3(repo: KarteRepository, kartes: list[Karte]) -> None:
    record = KarteRecord.model_validate(st.session_state.imp_final_record)
    messages = messages_from_dicts(st.session_state.imp_final_messages)
    mode: str = st.session_state.imp_mode

    if mode == "update":
        st.info(f"✏️ 既存のご依頼を更新します：**{record.request_id}.md**（ファイル名は変わりません）")
    elif mode == "new_request":
        st.info(f"🔁 {record.display_name} 様（{record.customer_id}）の**新しいご依頼**として保存します。")
    else:
        st.info("🆕 **はじめてのお客様**として保存します。")
    st.caption(
        f"保存するやり取り：{len(messages)}件　{chat_period(messages)}　"
        "／ AIが作った下書きです。間違いがあれば直してから、いちばん下の「保存する」を押してください。"
    )
    with st.expander("やり取りの原文を見る"):
        with st.container(height=400):
            render_chat_log(messages)

    if st.button("← お客様の確認に戻る"):
        st.session_state.imp_step = 2
        st.rerun()

    edited = render_record_editor(record, prefix=f"imp_{st.session_state.imp_nonce}", submit_label="💾 この内容で保存する")
    if edited is None:
        return
    try:
        saved = repo.update(edited, messages) if mode == "update" else repo.save_new(edited, messages)
    except OSError as exc:
        st.error(f"保存できませんでした：{exc}")
        return
    after_write(repo)
    reset_import()
    st.session_state.imp_done = saved.request_id
    st.rerun()


# --- 📋 カルテ一覧 ----------------------------------------------------------------


def page_list(repo: KarteRepository, kartes: list[Karte], today: date) -> None:
    counts = customer_request_counts(kartes)
    selected_id = st.session_state.selected_id
    by_id = {k.record.request_id: k for k in kartes}
    if selected_id and selected_id in by_id:
        render_record_detail(repo, by_id[selected_id], kartes, counts)
        return

    st.markdown("### 📋 カルテ一覧")
    if not kartes:
        st.info("まだカルテがありません。「📥 取り込み」からLINEのやり取りを取り込んでください。")
        return

    with st.container(border=True):
        col1, col2, col3 = st.columns([2, 2, 1])
        keyword = col1.text_input("🔎 キーワード（名前・タグ・ご要望など）", key="flt_keyword")
        types = col2.multiselect("撮影の種類", SHOOT_TYPES, key="flt_types")
        years = sorted({y for k in kartes if (y := record_year(k.record))}, reverse=True)
        year = col3.selectbox("年", ["すべて", *years], key="flt_year")
        col1, col2, col3 = st.columns([2, 2, 1])
        seasons = col1.multiselect("シーズン", SEASONS, key="flt_seasons")
        statuses = col2.multiselect("進み具合", STATUSES, key="flt_statuses")
        repeat_only = col3.checkbox("リピーターのみ", key="flt_repeat")

    filtered = filter_records(
        kartes, keyword=keyword, types=types, year=None if year == "すべて" else year,
        seasons=seasons, statuses=statuses, repeat_only=repeat_only,
    )
    st.caption(f"{len(filtered)}件 / 全{len(kartes)}件　｜　行をクリックすると、カルテの詳細が開きます。")
    if filtered:
        show_karte_table(filtered, counts, key="list_table")
    else:
        st.info("条件に合うカルテはありません。")


def _save_quick_change(repo: KarteRepository, karte: Karte, status: str, payment: str) -> None:
    record = karte.record.model_copy(deep=True)
    record.status = status
    record.shoot.payment_status = payment
    repo.update(record, karte.messages)
    after_write(repo)
    st.toast("変更を保存しました", icon="✅")


def render_record_detail(repo: KarteRepository, karte: Karte, kartes: list[Karte], counts: Counter) -> None:
    record = karte.record
    rid = record.request_id
    if st.button("← 一覧に戻る"):
        st.session_state.selected_id = None
        st.rerun()

    st.markdown(f"## {record.display_name} 様")
    st.caption(
        f"お客様ID {record.customer_id}｜{record.shoot.type or '種類未設定'}｜撮影日 {record.shoot.date or '未定'}"
        f"｜カルテID {rid}"
    )
    if record.profile.mood_tags:
        st.markdown(" ".join(f"`#{t}`" for t in record.profile.mood_tags))
    if counts.get(record.customer_id, 0) >= 2:
        st.info(f"🔁 リピーターのお客様です（ご依頼 {counts[record.customer_id]}件）。「過去のご依頼」タブで前回の内容を確認できます。")

    with st.container(border=True):
        col1, col2, col3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        status = col1.selectbox(
            "進み具合", STATUSES, index=STATUSES.index(record.status) if record.status in STATUSES else 0,
            key=f"q_status_{rid}_{record.updated_at}",
        )
        payment = col2.selectbox(
            "お支払い", PAYMENT_STATUSES,
            index=PAYMENT_STATUSES.index(record.shoot.payment_status) if record.shoot.payment_status in PAYMENT_STATUSES else 3,
            key=f"q_payment_{rid}_{record.updated_at}",
        )
        if col3.button("変更を保存", type="primary", width="stretch", key=f"q_save_{rid}"):
            _save_quick_change(repo, karte, status, payment)
            st.rerun()

    tabs = st.tabs(["📄 カルテ", "🧾 準備メモ", "✉️ ご案内文", "💬 LINE原文", "🔁 過去のご依頼", "✏️ 編集", "🗑️ 削除"])
    with tabs[0]:
        st.markdown(render_body(record, include_title=False))
        if sys.platform == "darwin" and st.button("📂 このファイルの場所を開く", key=f"reveal_{rid}"):
            subprocess.run(["open", "-R", str(karte.path)], check=False)
    with tabs[1]:
        st.caption("撮影当日の確認用です（チェックは保存されません）。")
        if record.prep_checklist:
            for index, item in enumerate(record.prep_checklist):
                st.checkbox(item, key=f"chk_{rid}_{index}")
        else:
            st.info("準備メモはありません。「✏️ 編集」タブで追加できます。")
        if record.profile.requests:
            st.markdown(f"**ご要望：** {record.profile.requests}")
        if record.profile.ng:
            st.markdown(f"**NG・配慮：** {record.profile.ng}")
    with tabs[2]:
        if record.next_message:
            st.caption("右上のコピーボタンでコピーして、LINEに貼り付けてください。")
            st.code(record.next_message, language=None, wrap_lines=True)
        else:
            st.info("ご案内文はありません。「✏️ 編集」タブで追加できます。")
    with tabs[3]:
        st.caption(f"{len(karte.messages)}件　{record.chat_period}")
        with st.container(height=520):
            render_chat_log(karte.messages)
    with tabs[4]:
        others = sort_kartes([k for k in kartes if k.record.customer_id == record.customer_id and k.record.request_id != rid])
        if not others:
            st.info("このお客様のほかのご依頼はありません。")
        for other in others:
            with st.container(border=True):
                st.markdown(f"**{_request_label(other)}**")
                for line in other.record.summary:
                    st.markdown(f"- {line}")
                if st.button("このカルテを開く", key=f"open_{rid}_{other.record.request_id}"):
                    open_karte(other.record.request_id)
    with tabs[5]:
        edited = render_record_editor(record, prefix=f"edit_{rid}_{record.updated_at}", submit_label="💾 変更を保存する")
        if edited is not None:
            repo.update(edited, karte.messages)
            after_write(repo)
            st.toast("カルテを保存しました", icon="✅")
            st.rerun()
    with tabs[6]:
        st.warning("削除したカルテは「ゴミ箱」フォルダ（data/trash）に移ります。必要になったら元に戻せます。")
        confirm = st.checkbox("このカルテを削除します", key=f"del_confirm_{rid}")
        if st.button("🗑️ 削除する", disabled=not confirm, key=f"del_{rid}"):
            repo.move_to_trash(rid)
            after_write(repo)
            st.session_state.selected_id = None
            st.toast("カルテをゴミ箱に移しました", icon="🗑️")
            st.rerun()


# --- 💬 AIに質問 ------------------------------------------------------------------

PRESETS = [
    ("this_month", "📅 今月の撮影予定"),
    ("next_prep", "🧾 次の撮影の準備メモ"),
    ("last_season", "🍁 去年の同じシーズンのお客様"),
    ("child_events", "🎎 次の行事が近いお客様"),
    ("unpaid", "💴 お支払いがまだのご依頼"),
    ("ai_requests", "💬 よくいただくご要望は？（AI）"),
]


def _set_preset(preset: str) -> None:
    st.session_state.preset = preset


def _ask_ai(question: str, kartes: list[Karte], today: date) -> None:
    service = get_service()
    if service is None:
        st.warning("AIに質問するには、サイドバーに OpenAI APIキー を入力してください。")
        return
    with st.spinner("AIがカルテを読んで、答えを考えています…"):
        try:
            result = service.answer_question(question, kartes, today)
        except Exception as exc:  # noqa: BLE001
            st.error(friendly_error_message(exc, current_model()))
            return
    st.session_state.qa_history.append(
        {"question": question, "answer": result.answer, "references": result.referenced_ids}
    )


def _render_preset(preset: str, kartes: list[Karte], counts: Counter, today: date) -> None:
    label = dict(PRESETS)[preset]
    st.markdown(f"##### {label}")
    if preset == "this_month":
        items = shoots_in_month(kartes, today)
        st.caption(f"{today.year}年{today.month}月：{len(items)}件")
        if items:
            show_karte_table(items, counts, key="preset_table")
        else:
            st.info("今月の撮影予定はありません。")
    elif preset == "next_prep":
        karte = next_shoot_checklist(kartes, today)
        if karte is None:
            st.info("これからの撮影予定はありません。")
            return
        r = karte.record
        st.markdown(f"**{r.shoot.date} {r.shoot.time or ''}　{r.display_name} 様　{r.shoot.type or ''}**（{r.shoot.location or '場所未定'}）")
        for item in r.prep_checklist or ["準備メモはありません"]:
            st.markdown(f"- [ ] {item}")
        if r.profile.requests:
            st.markdown(f"**ご要望：** {r.profile.requests}")
        if r.profile.ng:
            st.markdown(f"**NG・配慮：** {r.profile.ng}")
        if st.button("このカルテを開く", key="preset_open"):
            open_karte(r.request_id)
    elif preset == "last_season":
        season, year, items = same_season_last_year(kartes, today)
        st.caption(f"{year}年の{season}：{len(items)}件　今年もご案内してみませんか？")
        if items:
            show_karte_table(items, counts, key="preset_table")
        else:
            st.info("該当するお客様はいません。")
    elif preset == "child_events":
        rows = upcoming_child_events(kartes, today)
        st.caption(
            "今日から来年末までの行事です。お子さまの誕生日（分からなければ年齢）から計算した目安です。"
            "入学と二十歳のつどいは学年（4月2日〜翌年4月1日生まれ）で計算しています。"
        )
        if rows:
            df = pd.DataFrame(rows)
            event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                                 selection_mode="single-row", key="preset_child_table")
            if event is not None and event.selection.rows:
                open_karte(rows[event.selection.rows[0]]["カルテID"], clear_key="preset_child_table")
        else:
            st.info("該当するお客様はいません（お子さまの年齢が分かるカルテが必要です）。")
    elif preset == "unpaid":
        items = unpaid_requests(kartes)
        st.caption(f"{len(items)}件")
        if items:
            show_karte_table(items, counts, key="preset_table")
        else:
            st.info("お支払いがまだのご依頼はありません。")


def page_ask(kartes: list[Karte], today: date) -> None:
    st.markdown("### 💬 AIに質問")
    counts = customer_request_counts(kartes)

    st.markdown("#### よく使う質問")
    st.caption("ボタンを押すだけで答えが出ます（「AI」と書いたもの以外は、AIを使わないので無料です）。")
    for row_start in range(0, len(PRESETS), 3):
        for col, (preset, label) in zip(st.columns(3), PRESETS[row_start:row_start + 3], strict=False):
            col.button(label, key=f"preset_{preset}", width="stretch", on_click=_set_preset, args=(preset,))

    preset = st.session_state.preset
    if preset == "ai_requests":
        st.session_state.preset = None
        _ask_ai(PRESET_REQUESTS_QUESTION, kartes, today)
    elif preset:
        with st.container(border=True):
            _render_preset(preset, kartes, counts, today)

    st.divider()
    st.markdown("#### 自由に質問する")
    st.caption("例：「去年の秋に紅葉で撮影したご家族は？」「着物の持ち込みがあったお客様は？」「Instagramから来たお客様は何人？」")
    by_id = {k.record.request_id: k for k in kartes}
    for index, entry in enumerate(st.session_state.qa_history):
        with st.chat_message("user", avatar="🙂"):
            st.markdown(escape_markdown(entry["question"]))
        with st.chat_message("assistant", avatar="📷"):
            st.markdown(entry["answer"])
            if entry["references"]:
                st.caption("参照したカルテ（クリックで中身を確認できます）")
            for rid in entry["references"]:
                karte = by_id.get(rid)
                if karte is None:
                    continue
                with st.expander(f"📄 {rid}"):
                    for line in karte.record.summary:
                        st.markdown(f"- {line}")
                    if st.button("このカルテを開く", key=f"ref_{index}_{rid}"):
                        open_karte(rid)
    if st.session_state.qa_history and st.button("会話をリセットする"):
        st.session_state.qa_history = []
        st.rerun()

    if not has_api_key():
        st.caption("⚠️ 自由な質問には、サイドバーに OpenAI APIキー を入力してください。")
    question = st.chat_input("質問を入力して Enter", disabled=not has_api_key())
    if question:
        _ask_ai(question, kartes, today)
        st.rerun()


# --- ⚙️ 設定・管理 ----------------------------------------------------------------


def page_settings(repo: KarteRepository, kartes: list[Karte]) -> None:
    st.markdown("### ⚙️ 設定・管理")

    with st.container(border=True):
        st.markdown("#### 💾 バックアップ")
        backups = list_backups(repo.base)
        latest = backups[0].name if backups else "まだありません"
        st.write(f"最新のバックアップ：**{latest}**（全{len(backups)}個。新しいものから{BACKUP_KEEP}個を自動で残します）")
        st.caption("カルテを保存・変更・削除するたびに、自動でバックアップを取っています。")
        col1, col2 = st.columns(2)
        if col1.button("今すぐバックアップする", width="stretch"):
            create_backup(repo.base)
            st.toast("バックアップを取りました", icon="💾")
            st.rerun()
        if backups:
            col2.download_button(
                "⬇️ 最新のバックアップをダウンロード", data=backups[0].read_bytes(), file_name=backups[0].name,
                mime="application/zip", width="stretch",
            )
        st.caption("💡 PCの故障に備えて、月に1回はダウンロードして、USBメモリや iCloud Drive に保存しておくと安心です。")

    with st.container(border=True):
        st.markdown("#### 🕶️ 個人情報を伏せたコピーを作る")
        st.write(
            "お名前・ふりがな・LINE名・電話番号・メールアドレス・お子さまのお名前・郵便番号を伏せたコピーを作ります。"
            "GitHubのサンプル、チャットボットの学習、LP改善の分析に使えます。元のカルテは変わりません。"
        )
        st.warning("自動で伏せていますが、完全ではありません。外に出す前に、必ず中身を目で確認してください。")
        if st.button("書き出す", disabled=not kartes):
            path = export_anonymized(kartes, repo.exports_dir)
            st.session_state.last_export = str(path)
            st.toast(f"{len(kartes)}件を書き出しました", icon="🕶️")
        export = st.session_state.last_export
        if export and Path(export).exists():
            st.download_button(
                f"⬇️ {Path(export).name} をダウンロード", data=Path(export).read_bytes(),
                file_name=Path(export).name, mime="application/zip",
            )

    with st.container(border=True):
        st.markdown("#### 📁 データの保存場所")
        st.code(str(repo.base), language=None)
        st.caption(f"カルテ {len(kartes)}件（customers）／ バックアップ（backups）／ 書き出し（exports）／ ゴミ箱（trash）")
        if sys.platform == "darwin" and st.button("📂 Finderで開く"):
            subprocess.run(["open", str(repo.base)], check=False)

    with st.container(border=True):
        st.markdown("#### ℹ️ アプリ情報")
        st.write(f"{APP_NAME} v{APP_VERSION}　／　AIモデル：{current_model()}")


# --- 起動 ------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="📷", layout="wide", initial_sidebar_state="expanded")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()

    repo = KarteRepository(get_data_dir())
    kartes, load_errors = load_kartes(repo)
    render_sidebar()
    if load_errors:
        st.warning(f"読み込めなかったカルテがあります：{'、'.join(load_errors)}")

    today = date.today()
    page = st.session_state.page
    if page == "import":
        page_import(repo, kartes, today)
    elif page == "list":
        page_list(repo, kartes, today)
    elif page == "ask":
        page_ask(kartes, today)
    elif page == "settings":
        page_settings(repo, kartes)
    else:
        page_home(kartes, today)


if __name__ == "__main__":
    main()
