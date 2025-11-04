import os
import json
import random
import datetime
import asyncio
import pytz
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from flask import Flask
from hypercorn.asyncio import serve
from hypercorn.config import Config
from db_utils import (
    init_db,
    get_all_watch,
    get_watch_list_for_chat,
    save_watch_list_for_chat,
)

# Test unique instance ID
import uuid
INSTANCE_ID = str(uuid.uuid4())[:8]
print(f"[BOOT] Instance {INSTANCE_ID} starting...")

# =======================
# CẤU HÌNH
# =======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
TIMEZONE = "Asia/Ho_Chi_Minh"
PORT = int(os.getenv("PORT", "10000"))  # Render sẽ set PORT, nếu không thì dùng 10000

# WATCH_FILE và STATE_FILE không dùng nữa
# Dữ liệu watch list lưu trong PostgreSQL
ALERT_STATE = {}          # trạng thái cảnh báo trong RAM
ALERT_STARTED = False     # đã khởi động alert_loop hay chưa
ALERT_TASK = None         # task alert_loop (nếu cần debug)

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
# DỮ LIỆU NGƯỜI DÙNG (Postgres + RAM)
# =======================

def get_state_for_all():
    global ALERT_STATE
    return ALERT_STATE

def save_state_for_all(all_state):
    global ALERT_STATE
    ALERT_STATE = all_state


# =======================
# LẤY GIÁ (Realtime từ Trading.price_board)
# =======================
def get_quote(symbol: str):
    """
    Lấy dữ liệu realtime cho 1 mã (cổ phiếu hoặc index) từ Trading(source='VCI').price_board()

    Trả về dict:
    {
        "price": float | None,        # giá khớp hiện tại
        "pct": float | None,          # % thay đổi so với tham chiếu
        "change_abs": float | None    # chênh lệch tuyệt đối so với tham chiếu (dùng cho VNINDEX)
    }

    Nếu không lấy được dữ liệu => return None
    """
    from vnstock import Trading
    import math

    try:
        trading = Trading(source='VCI')
        df = trading.price_board([symbol])

        if df is None or len(df) == 0:
            print(f"[WARN] Không có dữ liệu trả về cho {symbol}")
            return None

        row = df.iloc[0]

        # Helper: convert NaN-like -> None, và numpy scalar -> float bình thường
        def norm(x):
            # None
            if x is None:
                return None
            # np types -> cast về float hoặc int bình thường
            try:
                # ví dụ np.int64(26700) -> 26700
                if hasattr(x, "item"):
                    x = x.item()
            except Exception:
                pass
            # NaN -> None
            if isinstance(x, float) and math.isnan(x):
                return None
            return x

        # --- lấy giá khớp hiện tại ---
        # ('match', 'match_price')
        match_price = norm(row.get(("match", "match_price")))
        # --- giá tham chiếu (có thể nằm ở 'match.reference_price' hoặc 'listing.ref_price') ---
        ref_price = norm(
            row.get(("match", "reference_price"))
            if ("match", "reference_price") in row
            else row.get(("listing", "ref_price"))
        )

        # fallback price: nếu chưa có match_price (chưa khớp lệnh) thì dùng ref_price
        price = match_price if match_price is not None else ref_price

        # chênh lệch tuyệt đối (điểm thay đổi)
        change_abs = None
        if match_price is not None and ref_price is not None:
            try:
                change_abs = float(match_price) - float(ref_price)
            except Exception:
                change_abs = None

        # phần trăm thay đổi (%)
        pct_change = None
        if match_price is not None and ref_price is not None and ref_price != 0:
            try:
                pct_change = (float(match_price) - float(ref_price)) / float(ref_price) * 100.0
            except Exception:
                pct_change = None

        out = {
            "price": price,           # vd 26700
            "pct": pct_change,        # vd +1.23
            "change_abs": change_abs  # vd +3.5 điểm
        }

        # In debug chi tiết để theo dõi server trả gì
        # print("[DEBUG RAW]", symbol, dict(row))
        print(f"[{INSTANCE_ID}] [QUOTE OK] {symbol} -> {out}")

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

# ======================================================
# 📨 _collector: lưu chat_id của bất kỳ ai từng nhắn cho bot
# ======================================================
from db_utils import save_watch_list_for_chat, get_watch_list_for_chat

async def _collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lưu chat_id vào DB nếu chưa có (để lần sau /announce gửi được).
    """
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
        lst = get_watch_list_for_chat(chat_id)
        if lst is None:
            lst = []
        # nếu chưa có record -> lưu rỗng
        save_watch_list_for_chat(chat_id, lst)


# ======================================================
# 📢 ANNOUNCE: Admin broadcast đến toàn bộ user đã từng dùng bot
# ======================================================
async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Chỉ ADMIN_ID được dùng: /announce <nội dung>"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Bạn không có quyền dùng /announce.")
        return

    # Lấy nội dung sau /announce
    text_raw = update.message.text or ""
    parts = text_raw.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("⚠️ Cách dùng: /announce <nội dung cần gửi>")
        return

    announcement = parts[1].strip()
    if len(announcement) > 4096:
        announcement = announcement[:4096]

    # 🔹 Lấy danh sách người dùng từ DB
    from db_utils import get_all_watch
    all_watch = get_all_watch()
    if not all_watch:
        await update.message.reply_text("ℹ️ Chưa có ai trong danh sách để gửi thông báo.")
        return

    ok = fail = 0
    for chat_key in all_watch.keys():
        chat_id = int(chat_key)
        try:
            await context.bot.send_message(chat_id=chat_id, text=announcement, parse_mode="Markdown")
            ok += 1
        except Exception as e:
            print(f"[WARN] Gửi {chat_id} lỗi: {e}")
            fail += 1
        await asyncio.sleep(0.05)

    await update.message.reply_text(f"✅ Đã gửi: {ok} | ❌ Lỗi: {fail}")


# =======================
# Liệt kê tất cả chat_id + watch_list with command /debug_watch
# =======================
async def cmd_debug_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Không có quyền.")
        return

    all_watch = get_all_watch()
    if not all_watch:
        await update.message.reply_text("Không có dòng nào trong bot_watch.")
        return

    lines = []
    for chat_key, data in all_watch.items():
        lines.append(f"{chat_key}: {data.get('list', [])}")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n...(cắt bớt)"
    await update.message.reply_text(msg)


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
    loop_id = 0

    while True:
        loop_id += 1
        loop_start = datetime.datetime.now(vn_tz)
        try:
            now = loop_start

            if not in_session_vietnam():
                print(f"[{INSTANCE_ID}][LOOP {loop_id}] Ngoài giờ giao dịch")
            else:
                print(f"[{INSTANCE_ID}][LOOP {loop_id}] Bắt đầu vòng alert")

                all_watch = get_all_watch()
                all_state = get_state_for_all()

                for chat_key, user_block in all_watch.items():
                    chat_id = int(chat_key)
                    watch_list = user_block.get("list", [])
                    print(f"[{INSTANCE_ID}][LOOP {loop_id}] chat={chat_id}, watch={watch_list}")

                    if chat_key not in all_state:
                        all_state[chat_key] = {}
                    personal_state = all_state[chat_key]

                    messages = []
                    header_added = False

                    for sym in watch_list:
                        print(f"[{INSTANCE_ID}][LOOP {loop_id}] get_quote({sym})")
                        quote = get_quote(sym)
                        if not quote:
                            continue

                        price = quote["price"]
                        pct = quote["pct"]
                        change_abs = quote["change_abs"]

                        # xác định mã là index hay cổ phiếu
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
                        print(f"[{INSTANCE_ID}][LOOP {loop_id}] Send alert to {chat_id}")
                        send_msg_to(chat_id, final_text)

                    all_state[chat_key] = personal_state

                save_state_for_all(all_state)

        except Exception as e:
            print(f"[{INSTANCE_ID}][LOOP {loop_id}] [ERROR] alert_loop exception: {e}")

        # ngủ theo thời gian thực của vòng lặp (~15s mỗi vòng)
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(15 - elapsed, 0)
        print(f"[{INSTANCE_ID}][LOOP {loop_id}] Sleep {delay:.1f}s")
        await asyncio.sleep(delay)


# =======================
# COMMAND HANDLERS
# =======================
from db_utils import save_watch_list_for_chat
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
    chat_id = update.effective_chat.id
    save_watch_list_for_chat(chat_id, [])

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
    lst = get_watch_list_for_chat(chat_id)


    # thêm mã vào watchlist nếu chưa có
    just_added = False
    if symbol not in lst:
        lst.append(symbol)
        save_watch_list_for_chat(chat_id, lst)
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
    lst = get_watch_list_for_chat(chat_id)


    if symbol in lst:
        lst.remove(symbol)
        save_watch_list_for_chat(chat_id, lst)

        await update.message.reply_text(f"🗑️ Đã xoá {symbol} khỏi danh sách.")
    else:
        await update.message.reply_text(f"❌ {symbol} không có trong danh sách.")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ACTIVE
    if not BOT_ACTIVE:
        await update.message.reply_text("⚙️ Bot đang bảo trì, vui lòng quay lại sau.")
        return

    chat_id = update.effective_chat.id
    lst = get_watch_list_for_chat(chat_id)

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
    global ALERT_STARTED, ALERT_TASK

    print(f"[{INSTANCE_ID}] ✅ Starting bot main()...")

    # khởi tạo Telegram app
    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # đăng ký các handler
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("announce", cmd_announce))
    tg_app.add_handler(CommandHandler("debug_watch", cmd_debug_watch))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _collector), group=1)

    # khởi tạo DB
    init_db()

    # chỉ khởi động alert_loop đúng 1 lần
    if not ALERT_STARTED:
        ALERT_STARTED = True
        ALERT_TASK = asyncio.create_task(alert_loop())
        print(f"[{INSTANCE_ID}] 🚀 alert_loop started once")
    else:
        print(f"[{INSTANCE_ID}] ⚠️ alert_loop already started, skip duplicate")

    # cấu hình Flask / Hypercorn
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    await asyncio.gather(
        serve(flask_app, config),
        tg_app.run_polling(),
    )


# =======================
# MAIN
# =======================
if __name__ == "__main__":
    print("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
