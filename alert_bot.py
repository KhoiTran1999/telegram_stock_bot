import os
import json
import time
import random
import datetime
import asyncio
import pytz
import requests
from vnstock import *
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from flask import Flask
from hypercorn.asyncio import serve
from hypercorn.config import Config

# =======================
# CẤU HÌNH
# =======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
TIMEZONE = "Asia/Ho_Chi_Minh"

WATCH_FILE = "watch_list.json"
STATE_FILE = "alerts_state.json"

# ⚙️ Trạng thái bot và ID admin
BOT_ACTIVE = True
ADMIN_ID = 1088200599  # 👉 thay bằng Telegram ID của bạn

# Mốc cảnh báo
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]
INDEX_POINT_LEVELS = [10, 20, 30, 40, -10, -20, -30, -40]

FUN_UP = [
    "Căng đét luôn 🔥",
    "Lên như ngựa phi 🐎",
    "Tiền vào mạnh quá 💸",
    "Bò điên đang chạy 🚀",
]
FUN_DOWN = [
    "Cháy margin chưa 😭",
    "Rớt như thang không phanh 🪂",
    "Cẩn thận margin call 🔔",
    "Thị trường đỏ rực luôn 🩸",
]

# =======================
# HÀM ĐỌC / GHI JSON
# =======================
def load_json(path, default=None):
    if not os.path.exists(path):
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default or {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# =======================
# DỮ LIỆU THEO USER
# =======================
def get_watch_for_chat(chat_id: int):
    all_watch = load_json(WATCH_FILE, {})
    chat_key = str(chat_id)
    if chat_key not in all_watch:
        all_watch[chat_key] = {"list": []}
    return all_watch, chat_key, all_watch[chat_key]["list"]

def update_watch_for_chat(all_watch):
    save_json(WATCH_FILE, all_watch)

def get_state_for_all():
    return load_json(STATE_FILE, {})

def save_state_for_all(all_state):
    save_json(STATE_FILE, all_state)

# =======================
# LẤY GIÁ
# =======================
def get_quote(symbol: str):
    try:
        q = stock_quote(symbol, source="VCI")
        return {
            "price": q["close"].iloc[-1],
            "change_abs": q["change"].iloc[-1],
            "pct": q["percent_change"].iloc[-1],
        }
    except Exception as e:
        print(f"[WARN] Lỗi lấy dữ liệu {symbol}: {e}")
        return None

# =======================
# GỬI TIN NHẮN TELEGRAM
# =======================
def send_msg_to(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    params = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        print("Lỗi gửi Telegram:", e)

# =======================
# On/Off Chat Bot
# =======================
async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bạn không có quyền tắt bot.")
        return
    BOT_ACTIVE = False
    await update.message.reply_text("🔴 Bot đã tắt (đang bảo trì).")


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bạn không có quyền bật bot.")
        return
    BOT_ACTIVE = True
    await update.message.reply_text("🟢 Bot đã bật lại.")


# =======================
# TIỆN ÍCH
# =======================
def pick_new_level(value: float, levels: list[float]):
    chosen = None
    for lvl in levels:
        if lvl > 0 and value >= lvl:
            if chosen is None or lvl > chosen:
                chosen = lvl
        elif lvl < 0 and value <= lvl:
            if chosen is None or lvl < chosen:
                chosen = lvl
    return chosen

def in_session_vietnam():
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()
    if weekday > 4:
        return False
    hhmm = now.hour * 100 + now.minute
    return 900 <= hhmm <= 1500

# =======================
# VÒNG LẶP CẢNH BÁO
# =======================
async def alert_loop():
    vn_tz = pytz.timezone(TIMEZONE)
    while True:
        try:
            now = datetime.datetime.now(vn_tz)
            if not in_session_vietnam():
                print(now.strftime("[DEBUG %H:%M:%S] Ngoài giờ giao dịch"))
                await asyncio.sleep(15)
                continue

            all_watch = load_json(WATCH_FILE, {})
            all_state = get_state_for_all()

            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", [])
                if chat_key not in all_state:
                    all_state[chat_key] = {}
                personal_state = all_state[chat_key]

                messages = []
                header_added = False

                for sym in watch_list:
                    quote = get_quote(sym)
                    if not quote:
                        continue

                    price = quote["price"]
                    change_abs = quote["change_abs"]
                    pct = quote["pct"]
                    is_index = sym.upper().startswith("VN")

                    new_lvl = pick_new_level(
                        change_abs if is_index else pct,
                        INDEX_POINT_LEVELS if is_index else STOCK_LEVELS,
                    )

                    prev_lvl = personal_state.get(sym, 0)

                    if new_lvl and new_lvl != prev_lvl:
                        personal_state[sym] = new_lvl
                        if not header_added:
                            messages.append(f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*")
                            messages.append("--------------------------------")
                            header_added = True

                        going_up = new_lvl > 0
                        icon = "🟢" if going_up else "🔴"
                        fun_line = random.choice(FUN_UP if going_up else FUN_DOWN)

                        if is_index:
                            messages.append(
                                f"{icon} *{sym} {change_abs:+.2f} điểm* "
                                f"({pct:+.2f}%) tại {price:,.2f}\n_{fun_line}_"
                            )
                        else:
                            messages.append(
                                f"{icon} *{sym} {pct:+.2f}%* "
                                f"tại {price:,.2f}\n_{fun_line}_"
                            )

                    elif new_lvl is None:
                        personal_state[sym] = 0

                if messages:
                    final_text = "\n".join(messages)
                    print(f"[INFO] Send alert to {chat_id}")
                    send_msg_to(chat_id, final_text)

                all_state[chat_key] = personal_state

            save_state_for_all(all_state)

        except Exception as e:
            print(f"[ERROR] alert_loop exception: {e}")

        await asyncio.sleep(15)

# =======================
# COMMAND HANDLERS
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì, vui lòng quay lại sau.")
        return
    
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot cảnh báo chứng khoán realtime.\n\n"
        "Lệnh:\n"
        "• /add <MÃ> – thêm mã theo dõi\n"
        "• /remove <MÃ> – xoá mã khỏi danh sách\n"
        "• /list – xem danh sách của bạn\n\n"
        "Bot sẽ gửi cảnh báo khi mã hoặc chỉ số biến động mạnh trong giờ giao dịch."
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì, vui lòng quay lại sau.")
        return
    
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Ví dụ: /add HPG hoặc /add VNINDEX")
        return

    symbol = context.args[0].upper().strip()

    # thử lấy quote
    quote_test = get_quote(symbol)

    # load danh sách hiện có
    all_watch, chat_key, lst = get_watch_for_chat(chat_id)

    # nếu mã chưa có trong danh sách -> thêm vào
    just_added = False
    if symbol not in lst:
        lst.append(symbol)
        all_watch[chat_key]["list"] = lst
        update_watch_for_chat(all_watch)
        just_added = True

    # phản hồi cho user
    if quote_test:
        # lấy được giá -> confirm đẹp
        await update.message.reply_text(
            (
                "✅ Đã thêm vào danh sách theo dõi.\n" if just_added else
                "ℹ️ Mã này đã có sẵn trong danh sách theo dõi của bạn.\n"
            )
            + f"• Mã: {symbol}\n"
            + f"• Giá hiện tại: {quote_test['price']:,.2f}\n"
            + f"• Thay đổi: {quote_test['pct']:+.2f}%"
        )
    else:
        # KHÔNG lấy được giá -> có thể lỗi mạng / chặn IP / thị trường đóng
        await update.message.reply_text(
            (
                "⚠️ Mình đã lưu mã này vào danh sách theo dõi của bạn, "
                "nhưng hiện tại không lấy được dữ liệu giá realtime cho mã đó.\n"
                "• Mã: " + symbol + "\n"
                "👉 Có thể do:\n"
                "- Gõ sai mã\n"
                "- Thị trường đang đóng\n"
                "- Server dữ liệu từ chối IP của host\n"
                "Mình sẽ vẫn cố gửi cảnh báo nếu sau này lấy được giá."
            )
        )




async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì, vui lòng quay lại sau.")
        return
    
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("⚠️ Ví dụ: /remove SSI")
        return

    symbol = context.args[0].upper().strip()
    all_watch, chat_key, lst = get_watch_for_chat(chat_id)

    if symbol in lst:
        lst.remove(symbol)
        all_watch[chat_key]["list"] = lst
        update_watch_for_chat(all_watch)
        await update.message.reply_text(f"🗑️ Đã xoá {symbol} khỏi danh sách.")
    else:
        await update.message.reply_text(f"❌ {symbol} không có trong danh sách.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì, vui lòng quay lại sau.")
        return
    
    chat_id = update.effective_chat.id
    _, _, lst = get_watch_for_chat(chat_id)
    if not lst:
        await update.message.reply_text("📭 Bạn chưa theo dõi mã nào.")
    else:
        await update.message.reply_text("📊 Danh sách theo dõi:\n" + ", ".join(lst))

# =======================
# FLASK SERVER
# =======================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is alive ✅"

# =======================
# CHẠY SONG SONG: TELEGRAM + FLASK + ALERT
# =======================
async def main():
    if not TOKEN:
        raise RuntimeError("❌ Thiếu TELEGRAM_TOKEN trong biến môi trường!")

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
    tg_app.add_handler(CommandHandler("on", cmd_on))
    tg_app.add_handler(CommandHandler("off", cmd_off))


    print(">> Telegram polling started (Render unified async-safe mode).")

    config = Config()
    config.bind = ["0.0.0.0:10000"]

    # ✅ KHỞI TẠO MANUAL (thay vì run_polling)
    print(">> Waiting 10s before starting polling (avoid Telegram conflict)...")
    await asyncio.sleep(10)
    await tg_app.initialize()
    
    await tg_app.start()
    await tg_app.updater.start_polling()

    try:
        # chạy Flask và alert loop song song
        await asyncio.gather(
            serve(flask_app, config),
            alert_loop(),
        )
    finally:
        # dọn dẹp khi Render dừng service
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


# =======================
# MAIN
# =======================
if __name__ == "__main__":
    print("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
