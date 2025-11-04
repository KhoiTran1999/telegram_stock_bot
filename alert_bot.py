# ==============================================
# alert_bot.py - phiên bản clean & ổn định 100%
# ==============================================
import os
import json
import random
import datetime
import asyncio
import pytz
import requests
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
from vnstock import Trading
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
ADMIN_ID = 1088200599

INSTANCE_ID = str(uuid.uuid4())[:8]

# Log gọn, không bị Render flush trùng
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bot")
log.info(f"[BOOT] Instance {INSTANCE_ID} starting...")

BOT_ACTIVE = True
ALERT_STATE = {}

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


async def _collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tự động lưu chat_id vào DB nếu chưa có."""
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
        lst = get_watch_list_for_chat(chat_id)
        if lst is None:
            save_watch_list_for_chat(chat_id, [])


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
                    if chat_key not in all_state:
                        all_state[chat_key] = {}
                    personal_state = all_state[chat_key]

                    messages = []
                    for sym in watch_list:
                        quote = get_quote(sym)
                        if not quote:
                            continue
                        price, pct, change_abs = quote["price"], quote["pct"], quote["change_abs"]
                        is_index = sym.upper().startswith("VN")
                        metric = change_abs if is_index else pct
                        new_lvl = pick_new_level(
                            metric, INDEX_POINT_LEVELS if is_index else STOCK_LEVELS
                        )
                        prev_lvl = personal_state.get(sym, 0)

                        if new_lvl and new_lvl != prev_lvl:
                            personal_state[sym] = new_lvl
                            icon = "🟢" if new_lvl > 0 else "🔴"
                            fun_line = random.choice(FUN_UP if new_lvl > 0 else FUN_DOWN)
                            price_str = f"{float(price):,.2f}" if price else "N/A"
                            pct_str = f"{float(pct):+.2f}%" if pct else "N/A"
                            messages.append(
                                f"{icon} *{sym} {pct_str}* tại {price_str}\n_{fun_line}_"
                            )
                        elif new_lvl is None:
                            personal_state[sym] = 0

                    if messages:
                        header = f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*\n--------------------------------"
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
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _collector))

    async def run_telegram():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        await asyncio.Event().wait()

    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    await asyncio.gather(
        serve(flask_app, config),
        alert_loop(),
        run_telegram(),
    )


if __name__ == "__main__":
    log.info("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
