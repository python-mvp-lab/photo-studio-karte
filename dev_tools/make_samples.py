"""GitHub公開用のダミーカルテ（架空の人物）を作るスクリプト。"""

import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(sys.argv[1])
sys.path.insert(0, str(APP_DIR))

import app  # noqa: E402

out = APP_DIR / "sample_data"
customers = out / "customers"
if customers.exists():
    shutil.rmtree(customers)
repo = app.KarteRepository(out)


def M(ts, role, text, sender=None):
    return app.Message(ts, sender or ("studio SAMPLE" if role == "studio" else "はなこ"), role, text)


def save(record, messages, when, cid=None):
    record.customer_id = cid or ""
    return repo.save_new(record, messages, now=when)


R = app.KarteRecord
C = app.CustomerInfo
S = app.ShootInfo
P = app.ProfileInfo
F = app.FutureInfo

# 1. 山田様（家族写真・去年の桜）
save(
    R(
        status="納品済み",
        customer=C(name="山田 花子", name_kana="やまだ はなこ", line_name="はなこ", phone="090-0000-0001"),
        shoot=S(type="家族写真", date="2025-04-12", time="10:00", location="桜並木の公園（ロケ）", people_count=3,
                family="夫婦、長男（4歳）", plan="ロケーションプラン", price=33000, delivery="オンラインでデータ納品",
                payment_status="入金済み"),
        profile=P(personality="明るく、やり取りがとても丁寧", preferences="自然な笑顔、柔らかい雰囲気",
                  requests="満開の桜並木の下で撮りたい", ng="長男は大きな音が苦手", tone="絵文字が多く親しみやすい",
                  mood_tags=["明るい", "丁寧", "自然体希望"]),
        future=F(source="Instagram", source_detail="友人の投稿を見て", children=[app.Child(name="そうたくん", age=4)],
                 anniversaries=[app.Anniversary(label="結婚記念日", date="2018-06-01")],
                 review="息子の自然な表情がたくさん残せて、家族の宝物になりました！",
                 next_opportunity="長男が2026年に七五三（5歳）"),
        summary=["2025年4月12日、家族3人で桜のロケーション撮影", "桜並木を背景に自然な笑顔を希望",
                 "長男は大きな音が苦手なので配慮が必要"],
        prep_checklist=["前日に桜の開花状況を確認して連絡する", "長男の好きなキャラクターを確認する"],
        next_message="山田様、先日は桜の撮影にお越しいただきありがとうございました🌸\nそうたくんの七五三の時期が近づいてきましたね。秋の紅葉の中での撮影もおすすめです。ご希望があればお気軽にご相談ください。",
    ),
    [
        M("2025/03/01 20:10", "customer", "はじめまして！桜の時期に家族写真をお願いしたいです🌸"),
        M("2025/03/02 09:00", "studio", "お問い合わせありがとうございます！ご希望の日程を教えてください。"),
        M("2025/03/02 12:30", "customer", "山田花子です。4月12日の午前はいかがでしょうか？息子は大きな音が苦手です。"),
        M("2025/03/02 13:00", "studio", "4月12日10:00で承りました。ゆっくり進めますのでご安心ください。"),
        M("2025/04/20 19:00", "customer", "写真届きました！息子の自然な表情がたくさん残せて、家族の宝物になりました！"),
    ],
    datetime(2025, 3, 2, 13, 5),
)

# 2. 山田様（七五三・リピート）
save(
    R(
        status="予約確定",
        customer=C(name="山田 花子", name_kana="やまだ はなこ", line_name="はなこ", phone="090-0000-0001"),
        shoot=S(type="七五三", date="2026-11-08", time="13:00", location="スタジオ＋神社", people_count=3,
                family="夫婦、長男（5歳）", costume="袴（祖父のものを持ち込み）", plan="七五三プラン", price=44000,
                payment_status="未入金"),
        profile=P(personality="前回同様、明るく丁寧", requests="祖父の袴で撮りたい。前回と同じ桜並木でも数カット",
                  ng="長男は大きな音が苦手", mood_tags=["明るい", "丁寧", "リピーター"]),
        future=F(source="以前のご利用", children=[app.Child(name="そうたくん", age=5)]),
        summary=["2026年11月8日、長男の七五三をスタジオと神社で撮影", "祖父の袴を持ち込み", "前回の桜撮影と同じ公園でも撮影希望"],
        prep_checklist=["袴の着付け時間を確認する", "神社の撮影許可を確認する", "前回の写真を見返して構図を変える"],
        next_message="山田様、七五三のご予約ありがとうございます。当日はそうたくんのペースで進めますね。",
    ),
    [
        M("2026/09/15 21:00", "customer", "お久しぶりです！今年は息子の七五三をお願いしたいです。祖父の袴を着せたいです。"),
        M("2026/09/16 09:10", "studio", "ご連絡ありがとうございます！11月8日13:00はいかがでしょうか。"),
        M("2026/09/16 12:00", "customer", "その日でお願いします！"),
    ],
    datetime(2026, 9, 16, 12, 10),
    cid="C0001",
)

# 3. 佐藤様（成人式の前撮り）
save(
    R(
        status="予約確定",
        customer=C(name="佐藤 美咲", name_kana="さとう みさき", line_name="みさき", phone="080-0000-0002"),
        shoot=S(type="成人式", date="2026-10-25", time="09:30", location="スタジオ", people_count=1, costume="振袖（レンタル）",
                plan="前撮りプラン", options="家族写真追加", price=55000, payment_status="未入金"),
        profile=P(personality="落ち着いていて、こだわりがはっきりしている", preferences="クールで大人っぽい雰囲気",
                  requests="紅葉を背景にした屋外カットも数枚", ng="横顔のアップは苦手", mood_tags=["落ち着き", "こだわり強め", "大人っぽい"]),
        future=F(source="Web検索", source_detail="「成人式 前撮り」で検索"),
        summary=["2026年10月25日、成人式の前撮りをスタジオで撮影", "クールで大人っぽい雰囲気を希望", "家族写真の追加オプションあり"],
        prep_checklist=["振袖レンタル店と着付け時間を調整する", "紅葉の色づきを前日に確認する"],
        next_message="佐藤様、前撮りのご予約ありがとうございます。当日のヘアメイクのイメージ写真があれば、事前にお送りください。",
    ),
    [
        M("2026/08/10 18:00", "customer", "成人式の前撮りをお願いしたいです。大人っぽい雰囲気が好きです。", sender="みさき"),
        M("2026/08/11 09:00", "studio", "ありがとうございます！10月25日9:30はいかがでしょうか。"),
        M("2026/08/11 20:00", "customer", "お願いします。横顔のアップは苦手です。", sender="みさき"),
    ],
    datetime(2026, 8, 11, 20, 30),
)

# 4. 鈴木様（ブライダル前撮り・去年の紅葉）
save(
    R(
        status="納品済み",
        customer=C(name="鈴木 一郎", name_kana="すずき いちろう", line_name="Ichiro", email="ichiro@example.com"),
        shoot=S(type="ブライダル・前撮り", date="2025-10-19", time="14:00", location="川沿いの公園（ロケ）", people_count=2,
                costume="白無垢・紋付袴", plan="和装ロケーションプラン", price=88000, payment_status="入金済み"),
        profile=P(personality="おおらかで、お任せしてくれるタイプ", requests="紅葉と川を一緒に", mood_tags=["おまかせ派", "穏やか"]),
        future=F(source="紹介", source_detail="山田様からのご紹介", anniversaries=[app.Anniversary(label="結婚式", date="2025-11-23")],
                 review="紅葉がきれいで、一生の思い出になりました。", next_opportunity="結婚1周年の記念撮影"),
        summary=["2025年10月19日、和装の前撮りを川沿いの公園で撮影", "紅葉と川を一緒に写したい", "山田様からのご紹介"],
        next_message="鈴木様、もうすぐ結婚1周年ですね。記念の撮影もぜひご検討ください🍁",
    ),
    [
        M("2025/08/01 10:00", "customer", "山田さんの紹介で連絡しました。和装の前撮りをお願いしたいです。", sender="Ichiro"),
        M("2025/08/01 12:00", "studio", "ご紹介ありがとうございます！紅葉の時期がおすすめです。"),
    ],
    datetime(2025, 8, 1, 12, 30),
)

# 5. 高橋様（マタニティ・日程未定）
save(
    R(
        status="お問い合わせ",
        customer=C(name="高橋 ゆい", line_name="ゆい"),
        shoot=S(type="マタニティ", payment_status="不明"),
        profile=P(requests="12月中の平日。候補：12/3、12/10", ng="長時間の立ち姿勢は避けたい", mood_tags=["やさしい", "控えめ"]),
        future=F(source="Instagram"),
        summary=["マタニティ撮影のお問い合わせ", "12月中の平日を希望（日程未定）", "長時間の立ち姿勢は避けたい"],
    ),
    [M("2026/10/05 11:00", "customer", "マタニティフォトをお願いしたいです。12月の平日で考えています。", sender="ゆい")],
    datetime(2026, 10, 5, 11, 30),
)

# 6. 田中様（プロフィール撮影）
save(
    R(
        status="予約確定",
        customer=C(name="田中 健太", line_name="Kenta"),
        shoot=S(type="プロフィール", date="2026-10-15", time="11:00", location="スタジオ", people_count=1, price=16500,
                payment_status="一部入金"),
        profile=P(requests="名刺とWebサイト用。笑顔と真顔の両方", mood_tags=["ビジネス", "きっちり"]),
        future=F(source="Web検索"),
        summary=["2026年10月15日、プロフィール写真をスタジオで撮影", "名刺とWebサイト用", "笑顔と真顔の両方を撮影"],
        prep_checklist=["背景紙（グレー・白）を準備する", "納品サイズ（名刺・Web）を確認する"],
    ),
    [M("2026/10/01 09:00", "customer", "プロフィール写真をお願いします。名刺とWebサイト用です。", sender="Kenta")],
    datetime(2026, 10, 1, 9, 30),
)

for name in ("backups", "exports", "trash"):
    shutil.rmtree(out / name, ignore_errors=True)
for path in sorted(customers.glob("*.md")):
    print(path.name)
