# worker.py
import asyncio
import json
import datetime
import pytz
import logging
import os
import random
from vnstock import Trading, Quote
import redis
from dotenv import load_dotenv

# Import các hàm DB cần thiết
# Lưu ý: Đảm bảo file db_utils.py nằm cùng thư mục
from db_utils import (
    get_all_watch,
    get_all_pro_chat_ids,
    get_bot_active,
    get_stock_alert_enabled_map,
    get_users_with_stock_alert_off,
    get_vn30f1m_enabled_map
)

# --- CẤU HÌNH CƠ BẢN ---
load_dotenv()
TIMEZONE = "Asia/Ho_Chi_Minh"
INSTANCE_ID = "WORKER_01" # Định danh cho Worker
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://google.com") # URL của Gateway

# Cấu hình Redis Output
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CHANNEL_OUTBOUND = 'telegram_outbound'

# Cấu hình Loop
FETCHER_INTERVAL_SECONDS = 20
TICKER_INTERVAL_SECONDS = 10

# Cache cục bộ của Worker
_stock_current_price_cache = {} 
_stock_current_watch_cache = {}
_stock_alert_disabled_cache = set()
ALERT_STATE = {}

# --- CẤU HÌNH VN30F1M ---
VN30F1M_SYMBOL = "VN30F1M"
VN30F1M_DELTA_THRESHOLD = 5    # ±5 điểm báo 1 lần
VN30F1M_TICK_SECONDS = 5       # Chu kỳ quét nhanh hơn cổ phiếu

# State VN30
_vn30f1m_anchor = None
_vn30f1m_ref_price = None
_vn30f1m_date = None
_vn30f1m_current_price_cache = None # Cache giá live

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
log = logging.getLogger("Worker")

# Kết nối Redis (Dùng chung)
try:
    r_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    log.info(f"[{INSTANCE_ID}] ✅ Kết nối Redis thành công.")
except Exception as e:
    log.error(f"[{INSTANCE_ID}] ❌ Lỗi kết nối Redis: {e}")
    r_client = None

# Khởi tạo Trading object (VCI)
try:
    stock_trading = Trading(source="VCI")
except:
    stock_trading = None

# --- CÁC HÀM HELPER ---

def push_telegram_msg(chat_id, text, reply_markup=None, msg_type='GENERAL'):
    """
    Hàm bắn tin nhắn vào Redis Channel để Gateway xử lý.
    """
    if not r_client:
        log.error("Redis client chưa sẵn sàng.")
        return

    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "msg_type": msg_type
        }
        
        # Serialize nút bấm (nếu có)
        if reply_markup:
            if hasattr(reply_markup, 'to_dict'):
                payload["reply_markup"] = reply_markup.to_dict()
            elif isinstance(reply_markup, dict):
                payload["reply_markup"] = reply_markup
            # Nếu là chuỗi JSON sẵn thì thôi

        r_client.publish(REDIS_CHANNEL_OUTBOUND, json.dumps(payload))
        # log.info(f"📤 Pushed to Redis for {chat_id}")
    except Exception as e:
        log.error(f"❌ Lỗi push Redis: {e}")

def in_session_vietnam():
    """Kiểm tra giờ giao dịch"""
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()
    if weekday > 4: return False # T7, CN
    hm = now.hour * 60 + now.minute
    # 09:15 - 11:30 và 13:00 - 14:45
    return (555 <= hm <= 690) or (780 <= hm <= 887)

def next_session_start(now):
    """Tính thời gian ngủ nếu ngoài giờ giao dịch"""
    vn_tz = pytz.timezone(TIMEZONE)
    if now.tzinfo is None: now = vn_tz.localize(now)
    
    weekday = now.weekday()
    hm = now.hour * 60 + now.minute
    date = now.date()
    
    def at(d, h, m):
        return vn_tz.localize(datetime.datetime(d.year, d.month, d.day, h, m))

    if weekday > 4: # Cuối tuần -> T2 09:15
        days = (7 - weekday) or 1
        return at(date + datetime.timedelta(days=days), 9, 15)
    
    if hm < 555: return at(date, 9, 15)
    if 690 < hm < 780: return at(date, 13, 0)
    if hm >= 887: # Hết phiên -> Mai 09:15
        next_d = date + datetime.timedelta(days=1)
        if next_d.weekday() > 4: next_d += datetime.timedelta(days=2)
        return at(next_d, 9, 15)
        
    return now + datetime.timedelta(seconds=60)

# Biến chặn VCI theo ngày
_vci_blocked_date = None

#============ Alert_Loop ===============

async def fetch_data_smart(symbols: list):
    """
    Logic lấy giá (VCI -> Fallback TCBS) copy từ alert_bot cũ.
    """
    global _vci_blocked_date
    results = {}
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_date = now.date()

    def _norm_price(p):
        if p is None: return 0.0
        p = float(p)
        if 0 < p < 500: return p * 1000.0
        return p

    skip_vci = (_vci_blocked_date == today_date)

    # 1. VCI
    if not skip_vci:
        try:
            def _run_vci():
                t = Trading(source="VCI")
                return t.price_board(symbols)
            
            df = await asyncio.wait_for(asyncio.to_thread(_run_vci), timeout=20.0)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    try:
                        sym = str(row.get(('listing', 'symbol'), row.get('symbol'))).upper().strip()
                        match_p = _norm_price(row.get(('match', 'match_price')))
                        ref_p = _norm_price(row.get(('listing', 'ref_price')))
                        if match_p == 0 and ref_p > 0: match_p = ref_p
                        pct = ((match_p - ref_p) / ref_p * 100) if ref_p > 0 else 0.0
                        results[sym] = {"price": match_p, "pct": pct}
                    except: continue
        except Exception:
            _vci_blocked_date = today_date

    # 2. TCBS Fallback (Code rút gọn cho ví dụ)
    missing = [s for s in symbols if s not in results]
    if missing:
        try:
            # ... (Logic fallback TCBS giữ nguyên hoặc đơn giản hóa) ...
            pass 
        except: pass
        
    return results

# --- LOOP 1: FETCHER (Lấy giá) ---
async def stock_price_fetcher_loop():
    global _stock_current_price_cache, _stock_current_watch_cache
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Fetcher Loop...")

    while True:
        now = datetime.datetime.now(vn_tz)
        
        # Check Bot Active từ DB
        if not get_bot_active():
            log.info("Bot is OFF (DB). Fetcher sleeping...")
            await asyncio.sleep(60)
            continue

        if not in_session_vietnam():
            next_start = next_session_start(now)
            sleep_sec = (next_start - now).total_seconds()
            log.info(f"Ngoài giờ giao dịch. Ngủ {sleep_sec:.0f}s tới {next_start.strftime('%H:%M')}")
            await asyncio.sleep(sleep_sec)
            continue

        try:
            # 1. Lấy Watchlist từ DB
            all_watch = await asyncio.to_thread(get_all_watch)
            all_symbols = set()
            for block in all_watch.values():
                for sym in (block.get("list", []) or []):
                    if len(str(sym)) == 3: all_symbols.add(str(sym).upper())
            
            if not all_symbols:
                await asyncio.sleep(10)
                continue

            # 2. Gọi Fetcher
            data = await fetch_data_smart(list(all_symbols))
            
            if data:
                _stock_current_price_cache = data
                _stock_current_watch_cache = all_watch
            
        except Exception as e:
            log.error(f"Fetcher Error: {e}")

        await asyncio.sleep(FETCHER_INTERVAL_SECONDS)

# --- LOOP 2: ALERT (So sánh & Bắn tin) ---
async def alert_loop():
    global ALERT_STATE, _stock_alert_disabled_cache
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Alert Loop...")
    
    # Load danh sách chặn alert lần đầu
    _stock_alert_disabled_cache = await asyncio.to_thread(get_users_with_stock_alert_off)

    FUN_UP = ["Tăng mạnh quá! 🚀", "Xanh tím rồi! 💜", "Tiền vào như nước 🌊"]
    FUN_DOWN = ["Giảm rồi... 📉", "Đỏ lửa 🔥", "Bình tĩnh quan sát 👀"]

    while True:
        now = datetime.datetime.now(vn_tz)
        
        if not get_bot_active():
            await asyncio.sleep(60); continue
        if not in_session_vietnam():
            await asyncio.sleep(60); continue

        try:
            quote_cache = _stock_current_price_cache
            all_watch = _stock_current_watch_cache
            
            if not quote_cache:
                await asyncio.sleep(5); continue

            # Lấy danh sách Pro (để lọc limit user thường)
            pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
            # Refresh danh sách tắt alert
            _stock_alert_disabled_cache = await asyncio.to_thread(get_users_with_stock_alert_off)

            for chat_key, user_block in all_watch.items():
                try: chat_id = int(chat_key)
                except: continue

                # Nếu user tắt alert thì bỏ qua
                if chat_id in _stock_alert_disabled_cache: continue

                watch_list = user_block.get("list", [])
                if not watch_list: continue

                # Logic giới hạn (Pro/Free)
                is_pro = (chat_id in pro_chat_ids) or (chat_id == ADMIN_ID)
                processing_list = watch_list if is_pro else watch_list[:1]

                if chat_key not in ALERT_STATE: ALERT_STATE[chat_key] = {}
                personal_state = ALERT_STATE[chat_key]
                
                messages = []
                buttons = { "inline_keyboard": [] } # Cấu trúc nút bấm Telegram

                for sym in processing_list:
                    sym_u = str(sym).upper()
                    quote = quote_cache.get(sym_u)
                    if not quote: continue

                    price = quote.get('price')
                    pct = quote.get('pct')
                    
                    # Logic so sánh state (đã rút gọn)
                    last_pct = personal_state.get(sym_u, {}).get('last_pct', 0.0)
                    delta = float(pct) - float(last_pct)
                    
                    if abs(delta) >= 2.0: # Ngưỡng 2%
                        icon = "🟢" if pct >= 0 else "🔴"
                        fun_line = random.choice(FUN_UP if pct >= 0 else FUN_DOWN)
                        
                        msg = (
                            f"{icon} *{sym_u} {'tăng' if pct>=0 else 'giảm'} {pct:+.2f}%*\n"
                            f"Giá: {price:,.0f}\n_{fun_line}_"
                        )
                        messages.append(msg)
                        
                        # Nút bấm soi chart
                        chart_url = f"{BASE_URL}/chart/{sym_u}"
                        buttons["inline_keyboard"].append([
                            {"text": f"📊 Soi Chart {sym_u}", "web_app": {"url": chart_url}}
                        ])

                        # Cập nhật state
                        personal_state[sym_u] = { "last_pct": float(pct), "last_alert_at": now.isoformat() }

                # Bắn tin sang Redis nếu có biến động
                if messages:
                    header = f"⏰ *Cảnh báo {now.strftime('%H:%M')}*"
                    body = "\n".join(messages) + "\n" + header
                    
                    # GỌI HÀM PUSH THAY VÌ GỬI TRỰC TIẾP
                    push_telegram_msg(
                        chat_id=chat_id, 
                        text=body, 
                        reply_markup=buttons if buttons["inline_keyboard"] else None,
                        msg_type="STOCK_ALERT"
                    )
                    log.info(f"🔔 Pushed alert for {chat_id}")

        except Exception as e:
            log.error(f"Alert Loop Error: {e}")

        await asyncio.sleep(TICKER_INTERVAL_SECONDS)

#============ VN30F1M_Loop ===============

def _vn30f1m_reset_if_new_day(now):
    """Reset anchor đầu ngày"""
    global _vn30f1m_date, _vn30f1m_anchor, _vn30f1m_ref_price
    if (_vn30f1m_date is None) or (now.date() != _vn30f1m_date):
        _vn30f1m_date = now.date()
        _vn30f1m_anchor = None
        _vn30f1m_ref_price = None
        log.info(f"[VN30] New day: {_vn30f1m_date}. Reset anchors.")

def _vn30f1m_clear_after_close():
    global _vn30f1m_anchor
    if _vn30f1m_anchor is not None:
        log.info("[VN30] Close session. Clear anchor.")
    _vn30f1m_anchor = None

async def _vn30f1m_process_tick(price: float):
    """
    Xử lý logic so sánh giá. 
    Trả về: Nội dung tin nhắn (String) nếu Trigger, ngược lại None.
    """
    global _vn30f1m_anchor, _vn30f1m_ref_price

    if _vn30f1m_anchor is None or _vn30f1m_ref_price is None:
        return None

    delta_trigger = float(price) - float(_vn30f1m_anchor)
    
    # Trigger nếu biến động >= 5 điểm
    if abs(delta_trigger) >= VN30F1M_DELTA_THRESHOLD:
        delta_display = float(price) - float(_vn30f1m_ref_price)
        direction = "tăng" if delta_display > 0 else "giảm"
        icon = "🟢" if delta_display > 0 else "🔴"
        trend_icon = "🚀" if delta_display > 0 else "📉"
        now_str = datetime.datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")

        text = (
            f"{icon} *VN30F1M {direction} {abs(delta_display):.1f} điểm*\n"
            f"Giá hiện tại: *{float(price):.1f}*\n"
            f"(So với TC: {_vn30f1m_ref_price:.1f})\n"
            f"{trend_icon} _Cập nhật lúc {now_str}_"
        )
        
        # Cập nhật mốc anchor mới
        _vn30f1m_anchor = float(price)
        log.info(f"[VN30] 🔔 Trigger! {price} (Delta: {delta_trigger})")
        return text
    
    return None

# --- LOOP VN30: FETCHER (Lấy giá Hybrid) ---
async def vn30f1m_price_fetcher_loop():
    global _vn30f1m_current_price_cache, _vn30f1m_ref_price, _vn30f1m_anchor
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu VN30 Fetcher...")

    while True:
        now = datetime.datetime.now(vn_tz)
        
        if not get_bot_active():
            await asyncio.sleep(30); continue
        
        if not in_session_vietnam():
            next_start = next_session_start(now)
            await asyncio.sleep((next_start - now).total_seconds())
            continue

        try:
            today_str = now.strftime('%Y-%m-%d')
            
            def _fetch():
                # 1. Giá khớp (Live 1m)
                q = Quote(symbol=VN30F1M_SYMBOL, source='VCI')
                df = q.history(start=today_str, end=today_str, interval='1m')
                p_now = float(df.iloc[-1]['close']) if df is not None and not df.empty else None
                
                # 2. Giá tham chiếu (Board)
                p_ref = None
                if _vn30f1m_ref_price is None and stock_trading:
                    try:
                        row = stock_trading.price_board([VN30F1M_SYMBOL]).iloc[0]
                        val = row.get(('listing', 'ref_price')) or row.get('ref_price')
                        if val: p_ref = float(val)
                    except: pass
                return p_now, p_ref

            price_now, price_ref = await asyncio.to_thread(_fetch)

            if price_now:
                _vn30f1m_current_price_cache = price_now
                
                if _vn30f1m_ref_price is None and price_ref:
                    _vn30f1m_ref_price = price_ref
                    # Init anchor bằng Ref nếu chưa có
                    if _vn30f1m_anchor is None: _vn30f1m_anchor = price_ref

        except Exception as e:
            log.error(f"VN30 Fetch Error: {e}")

        await asyncio.sleep(VN30F1M_TICK_SECONDS)

# --- LOOP VN30: ALERT & BROADCAST ---
async def vn30f1m_alert_loop():
    """
    Vừa check giá, vừa gửi tin (Broadcast) qua Redis luôn.
    Thay thế cho cả alert_loop và broadcast_loop cũ.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu VN30 Alert Loop...")

    while True:
        now = datetime.datetime.now(vn_tz)
        _vn30f1m_reset_if_new_day(now)

        if not get_bot_active() or not in_session_vietnam():
            _vn30f1m_clear_after_close()
            await asyncio.sleep(60)
            continue

        try:
            price = _vn30f1m_current_price_cache
            if price is None:
                await asyncio.sleep(5); continue

            # 1. Xử lý logic
            alert_text = await _vn30f1m_process_tick(float(price))

            # 2. Nếu có biến động -> Gửi tin (Broadcast)
            if alert_text:
                # Lấy danh sách user bật VN30
                user_map = await asyncio.to_thread(get_vn30f1m_enabled_map)
                count = 0
                
                for chat_id, enabled in user_map.items():
                    if enabled:
                        # PUSH REDIS
                        push_telegram_msg(chat_id, alert_text, msg_type="VN30_ALERT")
                        count += 1
                
                log.info(f"[VN30] Pushed alert to {count} users.")

        except Exception as e:
            log.error(f"VN30 Alert Error: {e}")

        await asyncio.sleep(VN30F1M_TICK_SECONDS)

# --- MAIN ENTRY POINT ---
async def main():
    log.info(f"[{INSTANCE_ID}] Worker starting...")
    
    # Chạy song song 2 loop
    await asyncio.gather(
        stock_price_fetcher_loop(),
        alert_loop(),
        vn30f1m_price_fetcher_loop(),
        vn30f1m_alert_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped.")