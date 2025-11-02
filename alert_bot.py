import os
import json
import time
import random
import threading
import datetime
import pytz
import requests
from flask import Flask
from vnstock import *
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import asyncio

# =======================
# CẤU HÌNH
# =======================
TOKEN = os.getenv("TELEGRAM_TOKEN")  # lấy từ biến môi trường Render
TIMEZONE = "Asia/Ho_Chi_Minh"

WATCH_FILE = "watch_list.json"      # lưu danh sách mã theo dõi theo từng chat
STATE_FILE = "alerts_state.json"    # lưu mốc cảnh báo đã gửi theo từng chat

# Ngưỡng cảnh báo:
# - Cổ phiếu: theo % thay đổi
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]
# - Chỉ số (VNINDEX, VN30): theo điểm tuyệt đối thay đổi
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
# HÀM ĐỌC/GHI JSON
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
# DỮ LIỆU THEO TỪNG CHAT
# =======================
def get_watch_for_chat(chat_id: int):
    """
    Trả về (all_watch, chat_key, list_theo_doi)
    all_watch = toàn bộ watch_list.json
    chat_key = str(chat_id)
    list_theo_doi = danh sách mã của chat_id đó
    """
    all_watch = load_json(WATCH_FILE, {})
    chat_key = str(chat_id)
    if chat_key not in all_watch:
        all_watch[chat_key] = {"list": []}
    return all_watch, chat_key, all_watch[chat_key]["list"]

def update_watch_for_chat(all_watch):
    save_json(WATCH_FILE, all_watch)

def get_state_for_all():
    """
    Đọc trạng thái cảnh báo đã gửi (alerts_state.json)
    Format:
    {
      "123456789": { "HPG": 4, "VNINDEX": 20 },
      "-987654321": { "VN30": -10 }
    }
    """
    all_state = load_json(STATE_FILE, {})
    return all_state

def save_state_for_all(all_state):
    save_json(STATE_FILE, all_state)

# =======================
# LẤY GIÁ / THÔNG TIN TỪ VNSTOCK
# =======================
def get_quote(symbol: str):
    """
    symbol có thể là mã cổ phiếu (HPG, SSI, NLG...)
    hoặc chỉ số (VNINDEX, VN30...)
    """
    try:
        q = stock_quote(symbol, source="VCI")
        return {
            "price": q["close"].iloc[-1],        # giá/điểm hiện tại
            "change_abs": q["change"].iloc[-1],  # thay đổi tuyệt đối (điểm)
            "pct": q["percent_change"].iloc[-1], # thay đổi %
            "vol": q["volume"].iloc[-1] if "volume" in q else 0,
        }
    except Exception as e:
        print(f"[WARN] Lỗi lấy dữ liệu {symbol}: {e}")
        return None

# =======================
# GỬI TELEGRAM
# =======================
def send_msg_to(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"[INFO] Sent to {chat_id}: {r.status_code}")
    except Exception as e:
        print(f"[ERROR] gửi Telegram tới {chat_id} lỗi: {e}")

# =======================
# CHỌN MỐC CẢNH BÁO
# =======================
def pick_new_level(value: float, levels: list[float]):
    """
    Chọn mốc mạnh nhất đã đạt:
    - value dương -> trả mốc dương lớn nhất <= value
    - value âm -> trả mốc âm nhỏ nhất >= value
    Ví dụ:
      value = +23, levels = [10,20,30,...] -> 20
      value = -13, levels = [-10,-20,-30,...] -> -10
    """
    chosen = None
    for lvl in levels:
        if lvl > 0 and value >= lvl:
            if chosen is None or lvl > chosen:
                chosen = lvl
        elif lvl < 0 and value <= lvl:
            if chosen is None or lvl < chosen:
                chosen = lvl
    return chosen  # None nếu chưa chạm ngưỡng

# =======================
# KIỂM TRA GIỜ GIAO DỊCH VN
# =======================
def in_session_vietnam():
    """
    Gửi cảnh báo trong khung:
    - Thứ 2 -> Thứ 6
    - từ 09:00 đến 15:00 giờ VN
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday > 4:
        return False
    hhmm = now.hour * 100 + now.minute
    return (hhmm >= 900 and hhmm <= 1500)

# =======================
# VÒNG LẶP CẢNH BÁO
# =======================
def alert_loop():
    """
    Thread chính để quét giá và gửi cảnh báo.
    Cứ mỗi ~15 giây:
    - Duyệt qua từng chat_id trong watch_list.json
    - Lấy từng mã trong list của chat_id đó
    - Kiểm tra ngưỡng biến động mới
    - Gửi tin cho đúng chat_id đó
    - Cập nhật state để tránh spam
    Chỉ hoạt động trong giờ giao dịch VN.
    """
    vn_tz = pytz.timezone(TIMEZONE)

    while True:
        try:
            now = datetime.datetime.now(vn_tz)

            if not in_session_vietnam():
                print(now.strftime("[DEBUG %H:%M:%S] Ngoài giờ giao dịch"))
                time.sleep(15)
                continue

            # Tải toàn bộ watch_list và state
            all_watch = load_json(WATCH_FILE, {})
            all_state = get_state_for_all()

            # Duyệt từng chat
            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", [])

                # Lấy state của riêng chat_id
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

                    # Nếu tên mã bắt đầu bằng "VN" => coi như chỉ số (VNINDEX, VN30...)
                    is_index = sym.upper().startswith("VN")

                    new_lvl = pick_new_level(
                        change_abs if is_index else pct,
                        INDEX_POINT_LEVELS if is_index else STOCK_LEVELS
                    )

                    prev_lvl = personal_state.get(sym, 0)

                    # Nếu vừa chạm mốc mới khác mốc cũ => gửi cảnh báo
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
                            # ví dụ: VNINDEX +20.35 điểm (+1.75%)
                            messages.append(
                                f"{icon} *{sym} {change_abs:+.2f} điểm* "
                                f"({pct:+.2f}%) tại {price:,.2f}\n"
                                f"_{fun_line}_"
                            )
                        else:
                            # ví dụ: HPG +4.20%
                            messages.append(
                                f"{icon} *{sym} {pct:+.2f}%* "
                                f"tại {price:,.2f}\n"
                                f"_{fun_line}_"
                            )
                    elif new_lvl is None:
                        # Chưa đạt mốc -> reset để lần sau nếu bùng mạnh sẽ báo lại
                        personal_state[sym] = 0

                # Nếu chat này có cảnh báo mới -> gửi riêng cho chat này
                if messages:
                    final_text = "\n".join(messages)
                    print(f"[INFO] Send alert to {chat_id}:\n{final_text}")
                    send_msg_to(chat_id, final_text)

                # Ghi lại state chat này
                all_state[chat_key] = personal_state

            # Lưu state sau khi duyệt hết các chat
            save_state_for_all(all_state)

        except Exception as e:
            print(f"[ERROR] alert_loop exception: {e}")

        # nghỉ 15 giây rồi lặp lại
        time.sleep(15)

# =======================
# HANDLER CHO CÁC LỆNH TELEGRAM
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot cảnh báo chứng khoán realtime (mỗi người 1 danh sách riêng).\n"
        "Lệnh:\n"
        "• /add <MÃ>    -> thêm theo dõi (VD: /add HPG hoặc /add VNINDEX)\n"
        "• /remove <MÃ> -> bỏ theo dõi\n"
        "• /list        -> xem danh sách đang theo dõi\n"
        "Bot sẽ nhắn khi biến động mạnh trong giờ thị trường (T2-T6, 09h-15h)."
    )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Thiếu mã. Ví dụ: /add HPG hoặc /add VNINDEX")
        return

    symbol = context.args[0].upper().strip()
    all_watch, chat_key, lst = get_watch_for_chat(chat_id)

    if symbol not in lst:
        lst.append(symbol)
        all_watch[chat_key]["list"] = lst
        update_watch_for_chat(all_watch)
        await update.message.reply_text(
            f"✅ Đã thêm {symbol} vào danh sách theo dõi của bạn."
        )
    else:
        await update.message.reply_text(
            f"ℹ️ {symbol} đã có sẵn trong danh sách của bạn."
        )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("⚠️ Thiếu mã. Ví dụ: /remove SSI")
        return

    symbol = context.args[0].upper().strip()
    all_watch, chat_key, lst = get_watch_for_chat(chat_id)

    if symbol in lst:
        lst.remove(symbol)
        all_watch[chat_key]["list"] = lst
        update_watch_for_chat(all_watch)
        await update.message.reply_text(
            f"🗑️ Đã xoá {symbol} khỏi danh sách theo dõi của bạn."
        )
    else:
        await update.message.reply_text(
            f"❌ {symbol} không có trong danh sách của bạn."
        )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _, _, lst = get_watch_for_chat(chat_id)

    if not lst:
        await update.message.reply_text("📭 Bạn chưa theo dõi mã nào.")
    else:
        await update.message.reply_text(
            "📊 Danh sách theo dõi của bạn:\n" + ", ".join(lst)
        )

# =======================
# FLASK KEEPALIVE SERVER
# =======================
app = Flask(__name__)

@app.route("/")
def home():
    # Render sẽ ping port này để confirm service "đang sống"
    return "Bot is running ✅"

def run_flask():
    # Mở 1 cổng (10000) để Render coi đây là web service thật sự
    port = int(os.getenv("PORT", "10000"))
    # host=0.0.0.0 để Render truy cập được
    app.run(host="0.0.0.0", port=port)

# =======================
# TELEGRAM BOT POLLING
# =======================
def run_telegram_bot():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN trong biến môi trường.")

    async def start_bot():
        tg_app = ApplicationBuilder().token(TOKEN).build()

        tg_app.add_handler(CommandHandler("start", cmd_start))
        tg_app.add_handler(CommandHandler("add", cmd_add))
        tg_app.add_handler(CommandHandler("remove", cmd_remove))
        tg_app.add_handler(CommandHandler("list", cmd_list))

        print(">> Telegram polling started (Render-safe mode).")
        # tắt signal handler để chạy trong thread phụ
        await tg_app.run_polling(close_loop=False, stop_signals=None)

    # Tạo loop riêng trong thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_bot())


# =======================
# MAIN
# =======================
def main():
    print("🚀 Khởi động bot đa người dùng + Flask keepalive cho Render Web Service...")

    # Thread 1: vòng lặp cảnh báo chứng khoán
    t_alert = threading.Thread(target=alert_loop, daemon=True)
    t_alert.start()

    # Thread 2: Telegram bot polling
    t_tg = threading.Thread(target=run_telegram_bot, daemon=True)
    t_tg.start()

    # Thread 3: Flask web server (Render cần port mở)
    run_flask()

if __name__ == "__main__":
    main()
