# ==============================================
# alert_bot.py - phiên bản dùng OpenRouter + MiniMax M2
# ==============================================
import os
import json
import random
import datetime
import asyncio
import pytz
import requests
import pandas as pd
import math
import uuid
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from flask import Flask
from hypercorn.asyncio import serve
from hypercorn.config import Config
from vnstock import Trading, Quote
from db_utils import (
    init_db,
    get_all_watch,
    get_watch_list_for_chat,
    save_watch_list_for_chat,
)

# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

# ID phiên bản khởi động (dùng để phân biệt log khi chạy nhiều instance)
INSTANCE_ID = str(uuid.uuid4())[:8]

# Log gọn, không bị Render flush trùng
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bot")
log.info(f"[BOOT] Instance {INSTANCE_ID} starting...")

# Thiết lập OpenRouter API (MiniMax M2)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    log.warning(
        "⚠️ OPENROUTER_API_KEY chưa được cấu hình – chức năng báo cáo 16:00 và /report sẽ không hoạt động."
    )

BOT_ACTIVE = True
ALERT_STATE = {}

# Thời gian giãn cách giữa 2 lần báo cùng một mốc cho cùng 1 mã (giây)
ALERT_COOLDOWN_SECONDS = 15 * 60  # 15 phút

# Ngưỡng cảnh báo
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]
INDEX_POINT_LEVELS = [10, 20, 30, 40, -10, -20, -30, -40]

# Câu thoại ngẫu nhiên (thông báo + cà khịa nhẹ 😏)
FUN_UP = [
    "Tăng mạnh quá, chắc lại kéo để xả cho F0 rồi 😏",
    "Bò điên quay lại rồi, ai bán non sáng nay chắc tiếc 😬",
    "Giá đang bay, chắc tin tốt sắp ra... hoặc tin xấu chưa ra 🤔",
    "Tăng kiểu này là mai báo lại đăng 'nhà đầu tư lạc quan trở lại' 📈",
    "Đang kéo index cho đẹp bảng điện chứ gì, nhìn quen lắm 😎",
    "Cổ phiếu tăng mà tài khoản vẫn chưa về bờ, lạ ghê 😅",
    "Lại xanh, chắc đội lái muốn test lòng tin nhà đầu tư 💚",
    "Cứ tăng thế này là lại có người tự tin all-in rồi 🤑",
    "Thị trường tăng, ai cũng nghĩ mình là thiên tài đầu tư 🧠",
    "Tăng cho vui thôi, còn giữ được bao lâu thì hên xui 😏",
]

FUN_DOWN = [
    "Giảm rồi, ai bảo không chốt khi còn đỉnh 😏",
    "Đỏ nhẹ cho vui thôi, mai giảm mạnh hơn 😬",
    "Thị trường rơi mà vẫn bình tĩnh à, chắc chưa margin 😎",
    "Giá giảm, lòng tin nhà đầu tư cũng giảm theo 📉",
    "Mới hôm qua còn hô 'mua thêm', nay lại hỏi 'có nên cắt chưa' 🤣",
    "Cú chỉnh nhẹ cho nhớ đời luôn 🩸",
    "Giảm kiểu này là bắt đầu đổ lỗi cho FED rồi đó 😅",
    "Đỏ rực vậy mà vẫn nói 'đi ngang tích lũy' 😏",
    "Cổ phiếu giảm, môi giới bỗng nhiên im lặng lạ thường 📞",
    "Thị trường kiểm tra độ kiên nhẫn của anh em... và anh em lại rớt test 😭",
]


# ==============================================
# HÀM TIỆN ÍCH
# ==============================================
def get_state_for_all():
    return ALERT_STATE


def save_state_for_all(all_state):
    global ALERT_STATE
    ALERT_STATE = all_state


def in_session_vietnam() -> bool:
    """Giờ giao dịch Việt Nam: 09:15–11:30, 13:00–14:47 (T2–T6)."""
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()
    if weekday > 4:
        return False
    hm = now.hour * 60 + now.minute
    return (555 <= hm <= 690) or (780 <= hm <= 887)


def pick_new_level(value: float | None, levels: list[float]):
    if value is None:
        return None
    chosen = None
    for lvl in levels:
        if lvl > 0 and value >= lvl:
            if chosen is None or lvl > chosen:
                chosen = lvl
        elif lvl < 0 and value <= lvl:
            if chosen is None or lvl < chosen:
                chosen = lvl
    return chosen


def get_quote(symbol: str):
    """Lấy giá realtime của 1 mã cổ phiếu hoặc index."""
    try:
        trading = Trading(source="VCI")
        df = trading.price_board([symbol])
        if df is None or len(df) == 0:
            return None
        row = df.iloc[0]

        def norm(x):
            if x is None:
                return None
            try:
                if hasattr(x, "item"):
                    x = x.item()
            except Exception:
                pass
            if isinstance(x, float) and math.isnan(x):
                return None
            return x

        match_price = norm(row.get(("match", "match_price")))
        ref_price = norm(
            row.get(("match", "reference_price"))
            if ("match", "reference_price") in row
            else row.get(("listing", "ref_price"))
        )

        price = match_price if match_price is not None else ref_price
        change_abs = (
            float(match_price) - float(ref_price)
            if match_price is not None and ref_price is not None
            else None
        )
        pct_change = (
            ((float(match_price) - float(ref_price)) / float(ref_price)) * 100.0
            if match_price and ref_price and ref_price != 0
            else None
        )

        out = {"price": price, "pct": pct_change, "change_abs": change_abs}
        log.info(f"[{INSTANCE_ID}] [QUOTE OK] {symbol} -> {out}")
        return out
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [QUOTE FAIL] {symbol}: {e}")
        return None


def get_perf_history(symbol: str):
    """
    Lấy chính xác:
    - Giá hiện tại (close gần nhất)
    - % thay đổi so với:
        + 1 ngày trước (trading day gần nhất trước đó)
        + 1 tuần trước (close gần nhất trước ngày -7)
        + 1 tháng trước (close gần nhất trước ngày -30)
    Dùng dữ liệu lịch sử từ vnstock Quote.history (nguồn VCI).
    """
    try:
        vn_tz = pytz.timezone(TIMEZONE)
        today = datetime.datetime.now(vn_tz).date()

        # Lấy tầm 60 ngày gần nhất là đủ để tính 1 tháng
        start_date = (today - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        quote = Quote(symbol=symbol, source="VCI")
        df = quote.history(start=start_date, end=end_date, interval="1D")

        if df is None or len(df) == 0:
            log.warning(f"[{INSTANCE_ID}] [PERF] {symbol}: không có dữ liệu history")
            return None

        # Chuẩn hoá time -> datetime + sort
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

        last = df.iloc[-1]
        last_date = last["time"].date()
        price = float(last["close"])

        def find_price_before(target_date: datetime.date):
            """Tìm giá close gần nhất TRƯỚC hoặc BẰNG target_date."""
            sub = df[df["time"].dt.date <= target_date]
            if sub.empty:
                return None
            return float(sub.iloc[-1]["close"])

        # % NGÀY: so với trading day gần nhất trước hôm nay
        prev_price = find_price_before(last_date - datetime.timedelta(days=1))
        if prev_price is not None and prev_price != 0:
            day_pct = (price - prev_price) / prev_price * 100.0
        else:
            day_pct = None

        # % TUẦN: so với close gần nhất trước (hôm nay - 7 ngày)
        week_price = find_price_before(last_date - datetime.timedelta(days=7))
        if week_price is not None and week_price != 0:
            week_pct = (price - week_price) / week_price * 100.0
        else:
            week_pct = None

        # % THÁNG: so với close gần nhất trước (hôm nay - 30 ngày)
        month_price = find_price_before(last_date - datetime.timedelta(days=30))
        if month_price is not None and month_price != 0:
            month_pct = (price - month_price) / month_price * 100.0
        else:
            month_pct = None

        return {
            "price": price,
            "day_pct": day_pct,
            "week_pct": week_pct,
            "month_pct": month_pct,
        }

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [PERF FAIL] {symbol}: {e}")
        return None


def format_perf_line(sym: str, perf: dict) -> str:
    price = perf.get("price")
    day_pct = perf.get("day_pct")
    week_pct = perf.get("week_pct")
    month_pct = perf.get("month_pct")

    price_str = f"{float(price):,.2f} đ" if price is not None else "N/A"
    day_str = f"{day_pct:+.2f}%" if day_pct is not None else "N/A"
    week_str = f"{week_pct:+.2f}%" if week_pct is not None else "N/A"
    month_str = f"{month_pct:+.2f}%" if month_pct is not None else "N/A"

    return (
        f"- {sym}: giá hiện tại {price_str}, "
        f"ngày {day_str}, tuần {week_str}, tháng {month_str}"
    )


def build_prompt_for_symbols(symbols: list[str]) -> str:
    """
    Lấy GIÁ + % NGÀY / TUẦN / THÁNG thật từ lịch sử giá,
    rồi build prompt gửi vào LLM (MiniMax M2 qua OpenRouter).
    """
    lines = []
    for sym in symbols:
        perf = get_perf_history(sym)
        if not perf:
            continue
        lines.append(format_perf_line(sym, perf))

    if not lines:
        return (
            "Danh mục hiện tại không có dữ liệu giá, hãy trả lời gọn: "
            "Chưa có dữ liệu để tổng hợp báo cáo hôm nay."
        )

    data_block = "\n".join(lines)

    prompt = f"""
Bạn là chuyên gia phân tích chứng khoán Việt Nam.

Hãy viết một bản tin Telegram gửi cho khách hàng sau phiên giao dịch hôm nay.

Dữ liệu danh mục của khách (giá + % thay đổi):
{data_block}

YÊU CẦU:
1. Với từng mã, hãy:
   - Nhắc lại giá hiện tại và % thay đổi trong ngày, tuần, tháng (dựa đúng vào dữ liệu ở trên).
   - Nếu không có tin mới đáng chú ý, hãy đưa ra nhận định & phân tích:
     điểm mạnh, rủi ro, và gợi ý chiến lược nắm giữ / chốt lời 
     (nhưng KHÔNG dùng giọng ép buộc kiểu 'phải mua/bán').
2. Viết bằng tiếng Việt, giọng điệu chuyên nghiệp nhưng dễ hiểu, phù hợp gửi qua Telegram.
3. Có thể dùng emoji cho sinh động, nhưng không lạm dụng.
4. Không đặt câu hỏi ở cuối bản tin, chỉ tóm tắt / kết luận nhẹ.

FORMAT:
- Mỗi mã theo block:

🔹 MÃ
- Giá hiện tại: ...
- Biến động: Ngày ..., Tuần ..., Tháng ...
- Nhận định: ...

- Cuối cùng thêm 1–2 câu tổng kết chung về danh mục (thiên về ngành nào, rủi ro / cơ hội tổng thể).

Hãy trả về đúng nội dung tin nhắn Telegram, KHÔNG giải thích thêm.
"""
    return prompt.strip()


def call_chatgpt_for_report(symbols: list[str]) -> str:
    """Gọi OpenRouter (MiniMax M2) để sinh bản tin báo cáo danh mục."""
    if not OPENROUTER_API_KEY:
        return (
            "⚠️ Hệ thống chưa cấu hình OPENROUTER_API_KEY nên chưa tạo được báo cáo tự động."
        )

    prompt = build_prompt_for_symbols(symbols)

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # 2 header này OpenRouter khuyến khích gửi thêm
        "HTTP-Referer": "https://github.com/Thinpad/telegram_stock_bot",
        "X-Title": "Telegram Stock Bot",
    }
    body = {
        "model": "minimax/minimax-m2:free",  # MiniMax M2 free trên OpenRouter
        "messages": [
            {
                "role": "system",
                "content": "Bạn là chuyên gia chứng khoán Việt Nam, trả lời ngắn gọn, rõ ràng, phù hợp gửi qua Telegram.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.4,
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
        if resp.status_code != 200:
            log.warning(
                f"[{INSTANCE_ID}][LLM ERROR] HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM trả lỗi HTTP). Bạn thử lại sau nhé."

        data = resp.json()
        if "error" in data:
            log.warning(f"[{INSTANCE_ID}][LLM ERROR] {data['error']}")
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM báo lỗi nội bộ)."

        choices = data.get("choices")
        if not choices:
            log.warning(
                f"[{INSTANCE_ID}][LLM ERROR] Response không có 'choices': {list(data.keys())}"
            )
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (không nhận được nội dung phù hợp)."

        content = choices[0]["message"]["content"]
        return content.strip()

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][LLM EXCEPTION] {e}")
        return "⚠️ Hiện tại không tạo được báo cáo danh mục do lỗi kết nối LLM."


def seconds_until_next_1600():
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + datetime.timedelta(days=1)

    # Nếu rơi vào cuối tuần thì nhảy tới thứ 2
    while target.weekday() > 4:  # 5 = T7, 6 = CN
        target = target + datetime.timedelta(days=1)

    return max((target - now).total_seconds(), 0)


async def daily_report_loop():
    """Gửi báo cáo danh mục cho từng user lúc 16:00 từ thứ 2–6."""
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    while True:
        loop_id += 1
        wait_sec = seconds_until_next_1600()
        log.info(f"[{INSTANCE_ID}][DAILY {loop_id}] Ngủ tới 16:00, còn {wait_sec:.0f}s")
        await asyncio.sleep(wait_sec)

        now = datetime.datetime.now(vn_tz)
        if now.weekday() > 4:
            log.info(f"[{INSTANCE_ID}][DAILY {loop_id}] Rơi vào cuối tuần, bỏ qua.")
            continue

        try:
            log.info(
                f"[{INSTANCE_ID}][DAILY {loop_id}] Bắt đầu gửi báo cáo danh mục 16:00"
            )
            all_watch = get_all_watch()  # từ db_utils

            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", []) or []
                if not watch_list:
                    continue

                # Bỏ các index VNINDEX, VN30... khỏi báo cáo nếu không muốn
                symbols = [s.upper() for s in watch_list if not s.upper().startswith("VN")]
                if not symbols:
                    continue

                try:
                    # Gọi LLM sinh nội dung (chạy trong thread tránh block loop)
                    text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
                    send_msg_to(chat_id, text)
                    log.info(
                        f"[{INSTANCE_ID}][DAILY {loop_id}] Đã gửi báo cáo cho {chat_id}"
                    )
                except Exception as e:
                    log.warning(
                        f"[{INSTANCE_ID}][DAILY {loop_id}] Lỗi gửi báo cáo cho {chat_id}: {e}"
                    )

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][DAILY {loop_id}] ERROR: {e}")
            # Nếu lỗi thì 5 phút sau thử lại
            await asyncio.sleep(300)


def send_msg_to(chat_id: int, text: str):
    """Gửi tin nhắn Telegram trực tiếp (ít lỗi hơn PTB trong async)."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        log.warning(f"[WARN] Telegram send error: {e}")


# ==============================================
# COMMAND HANDLERS
# ==============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì.")
        return
    chat_id = update.effective_chat.id
    save_watch_list_for_chat(chat_id, [])
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot cảnh báo chứng khoán realtime.\n\n"
        "Lệnh:\n"
        "• /add <MÃ> – thêm mã theo dõi\n"
        "• /remove <MÃ> – xoá mã khỏi danh sách\n"
        "• /list – xem danh sách của bạn\n\n"
        "Bot sẽ gửi cảnh báo khi mã hoặc chỉ số biến động mạnh trong giờ giao dịch."
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì.")
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("⚠️ Ví dụ: /add HPG hoặc /add VNINDEX")
        return
    symbol = context.args[0].upper().strip()
    lst = get_watch_list_for_chat(chat_id)
    if symbol not in lst:
        lst.append(symbol)
        save_watch_list_for_chat(chat_id, lst)
        await update.message.reply_text(f"✅ Đã thêm {symbol} vào danh sách.")
    else:
        await update.message.reply_text(f"ℹ️ {symbol} đã có trong danh sách.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì.")
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("⚠️ Ví dụ: /remove SSI")
        return
    symbol = context.args[0].upper().strip()
    lst = get_watch_list_for_chat(chat_id)
    if symbol in lst:
        lst.remove(symbol)
        save_watch_list_for_chat(chat_id, lst)
        await update.message.reply_text(f"🗑️ Đã xoá {symbol} khỏi danh sách.")
    else:
        await update.message.reply_text(f"❌ {symbol} không có trong danh sách.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì.")
        return
    chat_id = update.effective_chat.id
    lst = get_watch_list_for_chat(chat_id)
    if not lst:
        await update.message.reply_text("📭 Danh sách trống.")
    else:
        await update.message.reply_text("📊 Danh sách theo dõi:\n" + ", ".join(lst))


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chưa cấu hình ADMIN_ID thì không cho xài để tránh lộ bot
    if ADMIN_ID is None:
        await update.message.reply_text("⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    # Chỉ cho đúng admin dùng
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Dùng: /announce <nội dung>")
        return

    text = " ".join(context.args)
    all_watch = get_all_watch()
    count = 0
    for chat_key in all_watch.keys():
        chat_id = int(chat_key)
        try:
            await context.bot.send_message(chat_id, f"📢 {text}")
            count += 1
        except Exception as e:
            log.warning(f"[WARN] Announce lỗi {chat_id}: {e}")
    await update.message.reply_text(f"✅ Đã gửi đến {count} người.")


async def cmd_allwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chỉ cho admin dùng
    if ADMIN_ID is None:
        await update.message.reply_text("⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    all_watch = get_all_watch()
    if not all_watch:
        await update.message.reply_text("📭 Chưa có user nào lưu danh sách theo dõi.")
        return

    # Thống kê symbol -> số user đang theo dõi
    symbol_counts = {}
    detail_lines = []

    for chat_key, block in all_watch.items():
        lst = block.get("list", []) or []
        # thống kê theo mã
        for sym in lst:
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1

        # chi tiết theo từng user
        if lst:
            detail_lines.append(f"- {chat_key}: {', '.join(lst)}")
        else:
            detail_lines.append(f"- {chat_key}: (trống)")

    # Phần thống kê theo mã
    stats_lines = []
    for sym, cnt in sorted(symbol_counts.items()):
        stats_lines.append(f"{sym}: {cnt} user")

    header = (
        "📋 *Tổng hợp danh sách mã đang được theo dõi*\n"
        f"👥 Tổng số user: {len(all_watch)}\n"
        f"🏷️ Tổng số mã khác nhau: {len(symbol_counts)}\n\n"
        "📌 *Thống kê theo mã:*\n"
        + "\n".join(stats_lines)
        + "\n\n📌 *Chi tiết theo từng user (chat_id):*"
    )

    # Ghép text và tự chia nhỏ nếu quá dài
    max_len = 3500
    parts = []
    current = header
    for line in detail_lines:
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += "\n" + line
    parts.append(current)

    for part in parts:
        await update.message.reply_text(part, parse_mode="Markdown")


async def _collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tự động lưu chat_id vào DB nếu chưa có."""
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
        lst = get_watch_list_for_chat(chat_id)
        if lst is None:
            save_watch_list_for_chat(chat_id, [])


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi báo cáo danh mục ngay lập tức cho user để test."""
    if not update or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    watch = get_watch_list_for_chat(chat_id)
    watch_list = watch or []

    symbols = [s.upper() for s in watch_list if not s.upper().startswith("VN")]
    if not symbols:
        await update.message.reply_text(
            "Danh mục của bạn hiện đang trống, hãy /add mã trước đã nhé."
        )
        return

    await update.message.reply_text(
        "⏳ Đang tổng hợp báo cáo danh mục, vui lòng đợi trong giây lát..."
    )
    text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
    await update.message.reply_text(text)


# ==============================================
# VÒNG LẶP CẢNH BÁO
# ==============================================
async def alert_loop():
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    while True:
        loop_id += 1
        loop_start = datetime.datetime.now(vn_tz)
        try:
            now = loop_start
            if not in_session_vietnam():
                log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Ngoài giờ giao dịch")
            else:
                log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Bắt đầu vòng alert")
                all_watch = get_all_watch()
                all_state = get_state_for_all()

                for chat_key, user_block in all_watch.items():
                    chat_id = int(chat_key)
                    watch_list = user_block.get("list", [])
                    if not watch_list:
                        continue

                    # Khởi tạo state riêng cho user nếu chưa có
                    if chat_key not in all_state:
                        all_state[chat_key] = {}
                    personal_state = all_state[chat_key]

                    messages = []
                    for sym in watch_list:
                        quote = get_quote(sym)
                        if not quote:
                            continue
                        price, pct, change_abs = (
                            quote["price"],
                            quote["pct"],
                            quote["change_abs"],
                        )

                        is_index = sym.upper().startswith("VN")
                        metric = change_abs if is_index else pct
                        new_lvl = pick_new_level(
                            metric,
                            INDEX_POINT_LEVELS if is_index else STOCK_LEVELS,
                        )

                        # Lấy state cũ (tương thích cả kiểu cũ [int] lẫn kiểu mới [dict])
                        state_entry = personal_state.get(sym, {})
                        if isinstance(state_entry, dict):
                            prev_lvl = state_entry.get("last_level", 0)
                            last_alert_at_str = state_entry.get("last_alert_at")
                        else:
                            # state kiểu cũ chỉ lưu level
                            prev_lvl = state_entry or 0
                            last_alert_at_str = None

                        last_alert_at = None
                        if last_alert_at_str:
                            try:
                                last_alert_at = datetime.datetime.fromisoformat(
                                    last_alert_at_str
                                )
                            except Exception:
                                last_alert_at = None

                        should_alert = False

                        if new_lvl is not None:
                            if new_lvl != prev_lvl:
                                # Mốc mới khác mốc lần báo gần nhất -> báo ngay
                                should_alert = True
                            else:
                                # Trùng đúng mốc cũ -> chỉ báo nếu đã qua cooldown
                                if (
                                    last_alert_at is None
                                    or (
                                        now - last_alert_at
                                    ).total_seconds()
                                    >= ALERT_COOLDOWN_SECONDS
                                ):
                                    should_alert = True

                        if should_alert:
                            icon = "🟢" if new_lvl > 0 else "🔴"
                            fun_line = random.choice(
                                FUN_UP if new_lvl > 0 else FUN_DOWN
                            )
                            price_str = (
                                f"{float(price):,.2f}" if price is not None else "N/A"
                            )
                            pct_str = (
                                f"{float(pct):+.2f}%" if pct is not None else "N/A"
                            )

                            messages.append(
                                f"{icon} *{sym} {pct_str}* tại {price_str}\n_{fun_line}_"
                            )

                            # Cập nhật state mới: ghi lại mốc và thời gian báo
                            personal_state[sym] = {
                                "last_level": new_lvl,
                                "last_alert_at": now.isoformat(),
                            }
                        else:
                            # Không báo nhưng vẫn giữ lại state cũ để tính cooldown
                            if sym not in personal_state:
                                personal_state[sym] = {
                                    "last_level": 0,
                                    "last_alert_at": None,
                                }

                    # Gửi nếu có bất kỳ mã nào cần báo
                    if messages:
                        header = (
                            f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*\n"
                            "--------------------------------"
                        )
                        send_msg_to(chat_id, header + "\n" + "\n".join(messages))

                    all_state[chat_key] = personal_state

                save_state_for_all(all_state)

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][LOOP {loop_id}] ERROR: {e}")

        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(15 - elapsed, 0)
        log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Sleep {delay:.1f}s")
        await asyncio.sleep(delay)


# ==============================================
# FLASK KEEPALIVE
# ==============================================
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return f"✅ Bot is alive. Instance {INSTANCE_ID}"


# ==============================================
# MAIN
# ==============================================
async def main():
    log.info(f"[{INSTANCE_ID}] ✅ Starting bot main()...")
    init_db()

    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("announce", cmd_announce))
    tg_app.add_handler(CommandHandler("allwatch", cmd_allwatch))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _collector))
    tg_app.add_handler(CommandHandler("report", cmd_report))

    async def run_telegram():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        await asyncio.Event().wait()

    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    await asyncio.gather(
        serve(flask_app, config),
        alert_loop(),         # loop cảnh báo realtime
        daily_report_loop(),  # loop gửi báo cáo 16:00 T2–T6
        run_telegram(),
    )


if __name__ == "__main__":
    log.info("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
