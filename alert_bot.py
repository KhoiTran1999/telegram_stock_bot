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

# ====== CẤU HÌNH ======
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # chat id nơi bot sẽ gửi cảnh báo
TIMEZONE = "Asia/Ho_Chi_Minh"

WATCH_FILE = "watch_list.json"      # danh sách mã đang theo dõi
STATE_FILE = "alerts_state.json"    # đã cảnh báo mốc nào rồi (để không spam)

# Cổ phiếu: cảnh báo theo phần trăm
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]
# Chỉ số: cảnh báo theo điểm tuyệt đối
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


# ====== HÀM TIỆN ÍCH ĐỌC/GHI FILE ======
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


# ====== LẤY GIÁ TỪ VNSTOCK ======
def get_quote(symbol: str):
    """
    symbol có thể là mã cổ phiếu (HPG, SSI, NLG)
    hoặc chỉ số (VNINDEX, VN30).
    Ta sẽ dùng chung stock_quote() của vnstock.
    """
    try:
        q = stock_quote(symbol, source="VCI")
        return {
            "price": q["close"].iloc[-1],
            "change": q["change"].iloc[-1],                 # thay đổi tuyệt đối
            "pct": q["percent_change"].iloc[-1],            # thay đổi %
            "vol": q["volume"].iloc[-1] if "volume" in q else 0,
        }
    except Exception as e:
        print(f"Lỗi lấy dữ liệu {symbol}: {e}")
        return None


# ====== GỬI TIN NHẮN TELEGRAM ======
def send_msg(text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        print("Lỗi gửi Telegram:", e)


# ====== CHỌN NGƯỠNG CẢNH BÁO ======
def pick_new_level(value: float, levels: list[float]):
    """
    Chọn mốc mạnh nhất đã vượt.
    Ví dụ value = +23 điểm, levels = [10,20,30,...] -> trả 20.
    value = -12 điểm -> trả -10.
    """
    chosen = None
    for lvl in levels:
        if lvl > 0 and value >= lvl:
            if chosen is None or lvl > chosen:
                chosen = lvl
        elif lvl < 0 and value <= lvl:
            if chosen is None or lvl < chosen:
                chosen = lvl
    return chosen


# ====== VÒNG LẶP CẢNH BÁO (CHẠY SONG SONG) ======
def alert_loop():
    """
    Loop vô tận:
    - chỉ chạy alert trong giờ VN: Thứ 2-6, 09h00-15h00
    - mỗi ~15 giây quét một lần
    - nếu vượt mốc mới thì gửi tin
    """
    vn_tz = pytz.timezone(TIMEZONE)

    while True:
        now = datetime.datetime.now(vn_tz)

        # chỉ báo trong giờ thị trường
        is_weekday = now.weekday() <= 4        # 0=Mon ... 6=Sun
        is_session = (now.hour > 8 and now.hour < 15)  # ~09h-14h59

        if is_weekday and is_session:
            state = load_json(STATE_FILE, {})
            watch_data = load_json(WATCH_FILE, {"list": []})
            watch_list = watch_data.get("list", [])

            messages = []
            header_added = False

            for sym in watch_list:
                quote = get_quote(sym)
                if not quote:
                    continue

                price = quote["price"]
                change_abs = quote["change"]
                pct = quote["pct"]

                # heuristic: coi VNINDEX, VN30 là index
                is_index = sym.upper().startswith("VN")

                # tính ngưỡng mới đạt
                new_lvl = pick_new_level(
                    change_abs if is_index else pct,
                    INDEX_POINT_LEVELS if is_index else STOCK_LEVELS
                )

                prev_lvl = state.get(sym, 0)

                if new_lvl and new_lvl != prev_lvl:
                    # cập nhật state để tránh spam lặp lại
                    state[sym] = new_lvl

                    if not header_added:
                        messages.append(f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*")
                        messages.append("--------------------------------")
                        header_added = True

                    going_up = new_lvl > 0
                    icon = "🟢" if going_up else "🔴"
                    fun_line = random.choice(FUN_UP if going_up else FUN_DOWN)

                    if is_index:
                        # index: báo theo điểm tuyệt đối
                        messages.append(
                            f"{icon} *{sym} {change_abs:+.2f} điểm* "
                            f"({pct:+.2f}%) tại {price:,.2f}\n"
                            f"_{fun_line}_"
                        )
                    else:
                        # cổ phiếu: báo theo phần trăm
                        messages.append(
                            f"{icon} *{sym} {pct:+.2f}%* "
                            f"tại {price:,.2f}\n"
                            f"_{fun_line}_"
                        )

            if messages:
                final_text = "\n".join(messages)
                print(">>> GỬI CẢNH BÁO:")
                print(final_text)
                send_msg(final_text)
                save_json(STATE_FILE, state)
            else:
                # debug log nhẹ cho bạn xem trong Render logs
                print(now.strftime("[%H:%M:%S] Không có cảnh báo mới"))

        # nghỉ 15 giây rồi lặp
        time.sleep(15)


# ====== CÁC LỆNH TELEGRAM /add /remove /list ======
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Xin chào! Mình là bot cảnh báo chứng khoán realtime.\n"
        "Lệnh có sẵn:\n"
        "• /add <MÃ>    -> thêm theo dõi (VD: /add HPG, /add VNINDEX)\n"
        "• /remove <MÃ> -> bỏ theo dõi\n"
        "• /list        -> xem danh sách hiện tại\n"
        "Bot sẽ nhắn cảnh báo khi biến động mạnh."
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Thiếu mã. Ví dụ: /add HPG hoặc /add VNINDEX"
        )
        return

    symbol = context.args[0].upper().strip()
    data = load_json(WATCH_FILE, {"list": []})
    lst = data.get("list", [])

    if symbol not in lst:
        lst.append(symbol)
        data["list"] = lst
        save_json(WATCH_FILE, data)
        await update.message.reply_text(f"✅ Đã thêm {symbol} vào danh sách theo dõi.")
    else:
        await update.message.reply_text(f"ℹ️ {symbol} đã có sẵn trong danh sách.")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Thiếu mã. Ví dụ: /remove SSI"
        )
        return

    symbol = context.args[0].upper().strip()
    data = load_json(WATCH_FILE, {"list": []})
    lst = data.get("list", [])

    if symbol in lst:
        lst.remove(symbol)
        data["list"] = lst
        save_json(WATCH_FILE, data)
        await update.message.reply_text(f"🗑️ Đã xoá {symbol} khỏi danh sách.")
    else:
        await update.message.reply_text(f"❌ {symbol} không có trong danh sách.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_json(WATCH_FILE, {"list": []})
    lst = data.get("list", [])

    if not lst:
        await update.message.reply_text("📭 Chưa theo dõi mã nào.")
    else:
        await update.message.reply_text(
            "📊 Đang theo dõi:\n" + ", ".join(lst)
        )


# ====== BOOT BOT ======
def main():
    # khởi tạo Telegram bot (polling)
    app = ApplicationBuilder().token(TOKEN).build()

    # đăng ký lệnh
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))

    # chạy thread cảnh báo song song
    t = threading.Thread(target=alert_loop, daemon=True)
    t.start()

    print("🚀 Bot đã khởi động, đang polling Telegram & gửi alert realtime...")
    app.run_polling()


if __name__ == "__main__":
    main()
