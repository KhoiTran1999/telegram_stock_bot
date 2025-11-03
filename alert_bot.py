import os
import json
import random
import datetime
import asyncio
import pytz
import requests
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
# LẤY GIÁ (Realtime từ Trading.price_board)
# =======================
def get_quote(symbol: str):
    """
    Lấy giá realtime từ Trading.price_board().

    Trả về dict:
    {
        "price": <giá hiện tại (match_price hoặc ref_price)>,
        "pct": <% thay đổi so với tham chiếu>,
        "change_abs": <điểm thay đổi tuyệt đối so với tham chiếu>
    }

    Nếu không lấy được thì trả None.
    """
    from vnstock import Trading

    try:
        trading = Trading(source='VCI')
        df = trading.price_board([symbol])

        if df is None or len(df) == 0:
            print(f"[WARN] Không có dữ liệu trả về cho {symbol}")
            return None

        row = df.iloc[0]

        match_price = row.get("match_price", None)     # giá khớp hiện tại
        ref_price = row.get("ref_price", None)         # giá tham chiếu
        pct_change = row.get("percent_change", None)   # % thay đổi
        # với index: price_board có thể không trả match_price mà chỉ có ref_price

        # chọn giá hiển thị: ưu tiên match_price, fallback ref_price
        price = match_price if match_price is not None else ref_price

        # tính chênh lệch tuyệt đối để đo cho index (VNINDEX/VN30)
        change_abs = None
        if match_price is not None and ref_price is not None:
            try:
                change_abs = float(match_price) - float(ref_price)
            except Exception:
                change_abs = None

        out = {
            "price": price,
            "pct": pct_change,
            "change_abs": change_abs,
        }

        print(f"[QUOTE OK] {symbol} -> {out}")
        return out

    except Exception as e:
        print(f"[QUOTE FAIL] {symbol}: {type(e).__name__}: {e}")
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
def pick_new_level(value: float | None, levels: list[float]):
    """
    value: % thay đổi (cổ phiếu) hoặc điểm thay đổi (index).
    Có thể là None. Nếu None -> trả None luôn.
    """
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

def in_session_vietnam():
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday > 4:
        return False
    hhmm = now.hour * 100 + now.minute
    # cả pre-open lẫn ATC: 09:00 - 15:00
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
                    pct = quote["pct"]
                    change_abs = quote["change_abs"]

                    # xác định mã là index hay cổ phiếu
                    # ví dụ: VNINDEX, VN30 -> index
                    is_index = sym.upper().startswith("VN")

                    # chọn biến động để so sánh level
                    metric_value = change_abs if is_index else pct
                    new_lvl = pick_new_level(
                        metric_value,
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

                        # format giá
                        if price is not None:
                            try:
                                price_str = f"{float(price):,.2f}"
                            except Exception:
                                price_str = str(price)
                        else:
                            price_str = "N/A"

                        # format pct
                        if pct is not None:
                            try:
                                pct_str = f"{float(pct):+.2f}%"
                            except Exception:
                                pct_str = f"{pct}%"
                        else:
                            pct_str = "N/A"

                        # format change_abs (cho index)
                        if change_abs is not None:
                            try:
                                abs_str = f"{float(change_abs):+.2f}"
                            except Exception:
                                abs_str = str(change_abs)
                        else:
                            abs_str = "N/A"

                        if is_index:
                            # ví dụ: VNINDEX +12.3 điểm (+1.23%) tại 1,245.6
                            messages.append(
                                f"{icon} *{sym} {abs_str} điểm* "
                                f"({pct_str}) tại {price_str}\n_{fun_line}_"
                            )
                        else:
                            # ví dụ: HPG +4.20% tại 30,900
                            messages.append(
                                f"{icon} *{sym} {pct_str}* "
                                f"tại {price_str}\n_{fun_line}_"
                            )

                    elif new_lvl is None:
                        # reset state nếu không còn vượt ngưỡng
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

    # lấy quote realtime (có thể là None nếu IP bị chặn / ngoài giờ)
    quote_test = get_quote(symbol)

    # load watchlist user
    all_watch, chat_key, lst = get_watch_for_chat(chat_id)

    # thêm mã vào watchlist nếu chưa có
    just_added = False
    if symbol not in lst:
        lst.append(symbol)
        all_watch[chat_key]["list"] = lst
        update_watch_for_chat(all_watch)
        just_added = True

    # phản hồi cho user
    if quote_test:
        raw_price = quote_test.get("price")
        raw_pct = quote_test.get("pct")

        # format giá hiện tại
        if raw_price is not None:
            try:
                price_str = f"{float(raw_price):,.2f} VND"
            except Exception:
                price_str = f"{raw_price} VND"
        else:
            price_str = "N/A"

        # format % thay đổi
        if raw_pct is not None:
            try:
                pct_str = f"{float(raw_pct):+.2f}%"
            except Exception:
                pct_str = f"{raw_pct}%"
        else:
            pct_str = "N/A"

        await update.message.reply_text(
            (
                "✅ Đã thêm vào danh sách theo dõi.\n" if just_added
                else "ℹ️ Mã này đã có sẵn trong danh sách theo dõi của bạn.\n"
            )
            + f"• Mã: {symbol}\n"
            + f"• Giá hiện tại: {price_str}\n"
            + f"• Biến động: {pct_str}"
        )

    else:
        await update.message.reply_text(
            (
                "⚠️ Mình đã lưu mã này vào danh sách theo dõi của bạn, "
                "nhưng hiện tại không lấy được dữ liệu realtime.\n"
                if just_added
                else "ℹ️ Mã này đã có sẵn trong danh sách theo dõi của bạn, "
                     "nhưng hiện tại không lấy được dữ liệu realtime.\n"
            )
            + f"• Mã: {symbol}\n"
            "👉 Có thể do:\n"
            "- Mã ít thanh khoản / chưa khớp lệnh\n"
            "- Ngoài giờ giao dịch\n"
            "- Server dữ liệu từ chối IP host\n"
            "Mình vẫn sẽ cố gửi cảnh báo khi có biến động."
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

    # tránh conflict khi redeploy
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
