import os
import json
import time
import random
import threading
import datetime
import pytz
import requests
from vnstock import *
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =======================
# CẤU HÌNH CƠ BẢN
# =======================
TOKEN = os.getenv("TELEGRAM_TOKEN")  # bắt buộc phải có
TIMEZONE = "Asia/Ho_Chi_Minh"

WATCH_FILE = "watch_list.json"      # Lưu danh sách mã theo dõi theo từng chat
STATE_FILE = "alerts_state.json"    # Lưu mốc đã cảnh báo theo từng chat

# Cổ phiếu: cảnh báo theo % thay đổi
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]

# Chỉ số: cảnh báo theo điểm tuyệt đối thay đổi
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
    """
    Đọc file JSON. Nếu chưa có thì trả default.
    """
    if not os.path.exists(path):
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default or {}

def save_json(path, data):
    """
    Ghi file JSON (pretty, utf-8).
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# =======================
# HÀM CHO DỮ LIỆU THEO CHAT
# =======================
def get_watch_for_chat(chat_id: int):
    """
    Lấy danh sách mã theo dõi cho 1 chat cụ thể.
    Trả về (toàn bộ_data, chat_key, list_cua_chat)
    """
    all_watch = load_json(WATCH_FILE, {})
    chat_key = str(chat_id)
    if chat_key not in all_watch:
        all_watch[chat_key] = {"list": []}
    return all_watch, chat_key, all_watch[chat_key]["list"]

def update_watch_for_chat(all_watch):
    """
    Ghi lại watch_list.json sau khi sửa đổi.
    """
    save_json(WATCH_FILE, all_watch)

def get_state_for_chat(chat_id: int):
    """
    Lấy trạng thái cảnh báo đã gửi cho chat này.
    state lưu dạng:
    {
      "123456789": { "HPG": 4, "VNINDEX": 20, ... },
      "-987654": { ... }
    }
    """
    all_state = load_json(STATE_FILE, {})
    chat_key = str(chat_id)
    if chat_key not in all_state:
        all_state[chat_key] = {}
    return all_state, chat_key, all_state[chat_key]

def update_state_for_chat(all_state):
    """
    Ghi lại alerts_state.json sau khi sửa đổi.
    """
    save_json(STATE_FILE, all_state)

# =======================
# LẤY GIÁ / THÔNG TIN TỪ VNSTOCK
# =======================
def get_quote(symbol: str):
    """
    symbol có thể là mã cổ phiếu (HPG, SSI...)
    hoặc chỉ số (VNINDEX, VN30...)
    """
    try:
        q = stock_quote(symbol, source="VCI")
        return {
            "price": q["close"].iloc[-1],           # giá/điểm hiện tại
            "change_abs": q["change"].iloc[-1],     # thay đổi tuyệt đối (điểm)
            "pct": q["percent_change"].iloc[-1],    # thay đổi %
            "vol": q["volume"].iloc[-1] if "volume" in q else 0,
        }
    except Exception as e:
        print(f"[WARN] Lỗi lấy dữ liệu {symbol}: {e}")
        return None

# =======================
# GỬI TELEGRAM
# =======================
def send_msg_to(chat_id: int, text: str):
    """
    Gửi tin nhắn tới từng chat ID cụ thể.
    """
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
# NGƯỠNG CẢNH BÁO
# =======================
def pick_new_level(value: float, levels: list[float]):
    """
    Chọn mốc mạnh nhất đã đạt.
    - value >0 (ví dụ +23 điểm hoặc +4.5%)
      -> trả về mốc dương lớn nhất <= value (10,20,30,...)
    - value <0 (ví dụ -13 điểm hoặc -4.2%)
      -> trả về mốc âm nhỏ nhất >= value (-10,-20,-30,...)
    """
    chosen = None
    for lvl in levels:
        if lvl > 0 and value >= lvl:
            if chosen is None or lvl > chosen:
                chosen = lvl
        elif lvl < 0 and value <= lvl:
            if chosen is None or lvl < chosen:
                chosen = lvl
    return chosen  # có thể None nếu chưa chạm mốc nào

# =======================
# THỜI GIAN GIAO DỊCH VN
# =======================
def in_session_vietnam():
    """
    Chỉ gửi cảnh báo trong giờ thị trường:
    T2-T6, 09:00 -> 15:00 giờ VN.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday > 4:
        return False
    # Rộng tay một chút (8h50 -> 15h10) nếu muốn
    hhmm = now.hour * 100 + now.minute
    return (hhmm >= 900 and hhmm <= 1500)

# =======================
# VÒNG LẶP CẢNH BÁO
# =======================
def alert_loop():
    """
    Chạy liên tục trên thread riêng.
    Mỗi ~15s:
    - Duyệt qua TỪNG chat_id
    - Lấy list mã của chat đó
    - Xem mã nào chạm ngưỡng cảnh báo mới
    - Gửi tin nhắn riêng cho chat đó
    - Cập nhật state riêng cho chat đó
    """

    vn_tz = pytz.timezone(TIMEZONE)

    while True:
        try:
            now = datetime.datetime.now(vn_tz)

            if not in_session_vietnam():
                # Ngoài giờ: chỉ log nhẹ nhàng cho biết bot vẫn sống
                print(now.strftime("[DEBUG %H:%M:%S] Ngoài giờ giao dịch"))
                time.sleep(15)
                continue

            # load toàn bộ watch_list và state một lần cho hiệu suất
            all_watch = load_json(WATCH_FILE, {})
            all_state = load_json(STATE_FILE, {})

            # Duyệt từng chat
            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", [])

                # state cá nhân của chat
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

                    # heuristic: VNINDEX, VN30... -> index => cảnh báo theo điểm
                    is_index = sym.upper().startswith("VN")

                    new_lvl = pick_new_level(
                        change_abs if is_index else pct,
                        INDEX_POINT_LEVELS if is_index else STOCK_LEVELS
                    )

                    prev_lvl = personal_state.get(sym, 0)

                    if new_lvl and new_lvl != prev_lvl:
                        # Cập nhật state để chống spam lần sau
                        personal_state[sym] = new_lvl

                        if not header_added:
                            messages.append(f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*")
                            messages.append("--------------------------------")
                            header_added = True

                        going_up = new_lvl > 0
                        icon = "🟢" if going_up else "🔴"
                        fun_line = random.choice(FUN_UP if going_up else FUN_DOWN)

                        if is_index:
                            # ví dụ: VNINDEX +20 điểm
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
                        # Chưa đạt ngưỡng -> reset để chuẩn bị lần sau
                        personal_state[sym] = 0

                # Nếu chat này có message cảnh báo mới -> gửi riêng cho chat này
                if messages:
                    final_text = "\n".join(messages)
                    print(f"[INFO] Send alert to {chat_id}:\n{final_text}")
                    send_msg_to(chat_id, final_text)

                # Ghi lại state đã cập nhật cho chat này
                all_state[chat_key] = personal_state

            # Sau khi duyệt hết tất cả chat -> lưu file state chung
            save_json(STATE_FILE, all_state)

        except Exception as e:
            # tránh thread chết im lặng
            print(f"[ERROR] alert_loop exception: {e}")

        # nghỉ 15 giây rồi check tiếp
        time.sleep(15)

# =======================
# HANDLERS CHO LỆNH TELEGRAM
# =======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot cảnh báo chứng khoán realtime (theo dõi riêng từng người).\n"
        "Lệnh:\n"
        "• /add <MÃ>    -> thêm theo dõi (VD: /add HPG hoặc /add VNINDEX)\n"
        "• /remove <MÃ> -> bỏ theo dõi\n"
        "• /list        -> xem danh sách đang theo dõi\n"
        "Bot sẽ gửi cảnh báo khi biến động mạnh trong giờ thị trường (T2-T6, 09h-15h)."
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
# MAIN
# =======================
def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_TOKEN trong biến môi trường.")

    # Khởi động bot Telegram (polling updates từ Telegram)
    app = ApplicationBuilder().token(TOKEN).build()

    # Đăng ký các lệnh
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))

    # Chạy vòng lặp cảnh báo ở thread riêng
    t = threading.Thread(target=alert_loop, daemon=True)
    t.start()

    print("🚀 Bot đa người dùng đã khởi động. Đang polling Telegram & gửi alert realtime...")
    app.run_polling()

if __name__ == "__main__":
    main()
