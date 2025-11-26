# worker.py
import asyncio
import json
import datetime
import pytz
import logging
import os
import random
from vnstock import Trading, Quote, Screener, Finance, Company
import redis
from dotenv import load_dotenv
from google import genai
import uuid
import time
import pandas as pd
pd.set_option('future.no_silent_downcasting', True)
from typing import Any
import feedparser
import re
import csv
from datetime import timedelta
from chart_utils import generate_mini_chart
from db_utils import (
    get_all_watch,
    get_all_pro_chat_ids,
    get_bot_active,
    get_users_with_stock_alert_off,
    get_vn30f1m_enabled_map,
    get_recent_bctc_notified,
    get_recent_analysis_reports,
    get_recent_news_seen,
    get_historical_valuation_from_redis,
    save_historical_valuation_to_redis,
    get_bot_active,
    has_news_seen,
    mark_news_seen,
    cleanup_old_news_seen,
    cleanup_old_pending_orders,
    has_bctc_notified,
    mark_bctc_notified,
    has_report_seen,
    mark_report_seen,
    save_bot_message,
)
from report_cache import (
    make_report_cache_key,
    save_report_to_redis,
    get_report_from_redis,
    delete_report_from_redis,
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
REDIS_CHANNEL_INBOUND = 'worker_inbound'

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

# Biến chặn VCI theo ngày
_vci_blocked_date = None

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

# --- CẤU HÌNH GEMINI (Worker) ---
GEMINI_KEYS = [os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2")]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k] # Lọc key rỗng

def call_gemini_safe(model_id, contents, config=None):
    """Hàm gọi Gemini an toàn (Failover)"""
    last_error = None
    for api_key in GEMINI_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_id, contents=contents, config=config
            )
            return getattr(resp, "text", "").strip()
        except Exception as e:
            last_error = e
            continue
    log.error(f"All Gemini keys failed: {last_error}")
    return None

# --- CÁC HÀM HELPER ---

def push_telegram_msg(chat_id, text, reply_markup=None, msg_type='GENERAL', **kwargs):
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
        
        # Merge thêm các tham số phụ (như delete_id) vào payload
        payload.update(kwargs)

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

def extract_json_from_text(text: str) -> str:
    """
    Dùng Regex để trích xuất phần JSON {...} nằm giữa văn bản rác.
    """
    if not text: return ""
    
    # 1. Tìm chuỗi bắt đầu bằng { và kết thúc bằng } (kể cả xuống dòng)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        text = match.group(0)
    
    # 2. Clean các lỗi sơ đẳng (Markdown)
    text = text.replace("```json", "").replace("```", "").strip()
    
    return text

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



async def worker_inbound_loop():
    """
    [WORKER] Lắng nghe lệnh từ Gateway (ví dụ: User gõ /report).
    """
    log.info(f"[{INSTANCE_ID}] 🎧 Worker lắng nghe lệnh từ '{REDIS_CHANNEL_INBOUND}'...")
    try:
        pubsub = r_client.pubsub()
        pubsub.subscribe(REDIS_CHANNEL_INBOUND)

        while True:
            message = pubsub.get_message(ignore_subscribe_messages=True)
            if message:
                try:
                    payload = json.loads(message['data'])
                    cmd = payload.get('cmd')
                    
                    if cmd == "GEN_REPORT":
                        chat_id = payload.get('chat_id')
                        symbols = payload.get('symbols')

                        # [MỚI] Lấy loading_msg_id từ payload
                        loading_id = payload.get('loading_msg_id')

                        # Chạy async để không block việc nhận lệnh khác
                        asyncio.create_task(process_report_for_user(chat_id, symbols, loading_msg_id=loading_id))
                        
                    # [MỚI] Xử lý lệnh chạy Weekly ngay lập tức
                    elif cmd == "RUN_WEEKLY_NOW":
                        admin_id = payload.get('admin_id')
                        log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Weekly từ {admin_id}")
                        # Chạy background task để không block việc nhận lệnh khác
                        asyncio.create_task(execute_weekly_batch(requester_id=admin_id))

                    elif cmd == "GEN_INFO":
                        chat_id = payload.get('chat_id')
                        symbol = payload.get('symbol')
                        loading_id = payload.get('loading_msg_id')
                        
                        # Chạy Async
                        asyncio.create_task(process_profile_for_user(
                            chat_id, 
                            symbol, 
                            loading_msg_id=loading_id
                        ))

                    elif cmd == "GEN_SCREENER":
                        chat_id = payload.get('chat_id')
                        loading_id = payload.get('loading_msg_id')
                        asyncio.create_task(process_screener_view(chat_id, loading_id))

                    elif cmd == "FORCE_SCREENER":
                        admin_id = payload.get('admin_id')
                        asyncio.create_task(process_force_update_screener(admin_id))

                except Exception as e:
                    log.error(f"Inbound Error: {e}")
            
            await asyncio.sleep(0.1)
    except Exception as e:
        log.error(f"Worker Inbound Crash: {e}")
        await asyncio.sleep(5)


# ==============================================
# TIN TỨC (RSS)
# ==============================================
NEWS_FEED_TYPE_SPECIALIZED = "SPECIALIZED"
NEWS_FEED_TYPE_MACRO = "MACRO"

# 1. Tin chuyên ngành (Quét từ khóa)
RSS_FEEDS_SPECIALIZED = {
    "CHUNG_KHOAN": [
        "https://vneconomy.vn/chung-khoan.rss",
        "https://vneconomy.vn/tai-chinh.rss",
        "https://vneconomy.vn/kinh-te-so.rss",
        "https://vneconomy.vn/dau-tu.rss",
        "https://vietstock.vn/830/chung-khoan/co-phieu.rss",
        "https://vietstock.vn/739/chung-khoan/giao-dich-noi-bo.rss",
        "https://vietstock.vn/3358/chung-khoan/etf-va-cac-quy.rss",
        "https://vietstock.vn/145/chung-khoan/y-kien-chuyen-gia.rss",
        "https://vietstock.vn/582/nhan-dinh-phan-tich/phan-tich-co-ban.rss",
        "https://vneconomy.vn/tin-moi.rss",
        "https://vneconomy.vn/tieu-diem.rss",
    ],
    "DOANH_NGHIEP": [
        "https://vneconomy.vn/nhip-cau-doanh-nghiep.rss",
        "https://vneconomy.vn/thi-truong.rss",
        "https://vneconomy.vn/tieu-dung.rss",
        "https://vneconomy.vn/dan-sinh.rss",
        "https://vneconomy.vn/kinh-te-xanh.rss",
        "https://vneconomy.vn/cong-nghe-startup.rss",
        "https://vietstock.vn/737/doanh-nghiep/hoat-dong-kinh-doanh.rss",
        "https://vietstock.vn/738/doanh-nghiep/co-tuc.rss",
        "https://vietstock.vn/764/doanh-nghiep/tang-von-m-a.rss",
        "https://vietstock.vn/746/doanh-nghiep/ipo-co-phan-hoa.rss",

    ],
    "BAT_DONG_SAN": [
        "https://vneconomy.vn/dau-tu-ha-tang.rss",
        "https://vneconomy.vn/dia-oc.rss",
        "https://vietstock.vn/4220//bat-dong-san/thi-truong-nha-dat.rss",
        "https://vietstock.vn/4222/bat-dong-san/du-an.rss",
        "https://vietstock.vn/4266/bat-dong-san/bao-hiem-va-thue-nha-dat.rss",

    ],
}

# 2. Tin vĩ mô (Broadcast cho tất cả)
RSS_FEEDS_MACRO = [
    "https://vneconomy.vn/tin-moi.rss",
    "https://vneconomy.vn/tieu-diem.rss",
    "https://vietstock.vn/143/chung-khoan/chinh-sach.rss",
    "https://vietstock.vn/16312/tai-chinh/tai-san-so.rss",
    "https://vietstock.vn/761/kinh-te/vi-mo.rss",
]

# Chu kỳ quét RSS (giây)
NEWS_SPECIALIZED_INTERVAL_SECONDS = 30 * 60   # 30 phút
NEWS_MACRO_INTERVAL_SECONDS = 60 * 60        # 60 phút

# Số bài tối đa gửi cho mỗi user / mỗi vòng quét tin
NEWS_MAX_ARTICLES_PER_CHAT = 3          # tin chuyên ngành
NEWS_MACRO_MAX_ARTICLES_PER_RUN = 3     # tin vĩ mô (broadcast)

# Số bài RSS tối đa xử lý mỗi vòng (sau khi gộp & sort theo published)
NEWS_MAX_RSS_ENTRIES_PER_RUN = 80

# Thời gian tối đa coi bài báo là "tươi" (theo pubDate)
MAX_NEWS_AGE_DAYS = 14  # chỉ gửi bài trong 14 ngày gần nhất

#===============================================
def load_company_keywords_from_csv(path: str = "ssi_master_list.csv") -> dict[str, list[str]]:
    """
    Đọc danh sách công ty từ file CSV (cột: symbol, name, industry, floor),
    trả về dict[symbol] = [symbol, tên đầy đủ, tên rút gọn].
    """
    mapping: dict[str, list[str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}

            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                name = (row.get("name") or "").strip()
                if not sym or not name:
                    continue

                # Tạo tên rút gọn bằng cách bỏ bớt mấy từ phổ biến
                short = re.sub(
                    r"\b(Công ty|Cổ phần|Tập đoàn|TNHH|Ngân hàng|Thương mại|Đầu tư|Phát triển|Kỹ thuật|Tài chính)\b",
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
                short = re.sub(r"\s+", " ", short).strip()

                keywords = {sym}
                keywords.add(name)
                if len(short) > 2:
                    keywords.add(short)

                mapping[sym] = [k for k in keywords if len(k) > 2]

        log.info(f"[{INSTANCE_ID}][COMPANY] Đã load {len(mapping)} công ty từ {path}.")
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][COMPANY] Lỗi đọc CSV {path}: {e}")
    return mapping

# Map symbol -> list keyword (mã + tên doanh nghiệp)
COMPANY_KEYWORDS= load_company_keywords_from_csv("ssi_master_list.csv")

def is_fresh_news(
    pub_dt: datetime.datetime | None,
    now: datetime.datetime | None = None,
) -> bool:
    """
    Trả về True nếu bài đủ "tươi" theo ngưỡng MAX_NEWS_AGE_DAYS.

    - Nếu pub_dt = None -> cho qua (coi là tươi, vì không có thông tin ngày).
    - Nếu pubDt cũ hơn MAX_NEWS_AGE_DAYS ngày -> False.
    """
    if pub_dt is None:
        return True

    vn_tz = pytz.timezone(TIMEZONE)

    # Bổ sung timezone nếu thiếu
    if pub_dt.tzinfo is None:
        pub_dt = vn_tz.localize(pub_dt)

    if now is None:
        now = datetime.datetime.now(vn_tz)
    else:
        # nếu now không có tz thì cũng gắn VN TZ
        if now.tzinfo is None:
            now = vn_tz.localize(now)

    age = now - pub_dt
    return age.days <= MAX_NEWS_AGE_DAYS



def load_company_keywords_from_csv(path: str = "ssi_master_list.csv") -> dict[str, list[str]]:
    """
    Đọc danh sách công ty từ file CSV (cột: symbol, name, industry, floor),
    trả về dict[symbol] = [symbol, tên đầy đủ, tên rút gọn].
    """
    mapping: dict[str, list[str]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}

            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                name = (row.get("name") or "").strip()
                if not sym or not name:
                    continue

                # Tạo tên rút gọn bằng cách bỏ bớt mấy từ phổ biến
                short = re.sub(
                    r"\b(Công ty|Cổ phần|Tập đoàn|TNHH|Ngân hàng|Thương mại|Đầu tư|Phát triển|Kỹ thuật|Tài chính)\b",
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
                short = re.sub(r"\s+", " ", short).strip()

                keywords = {sym}
                keywords.add(name)
                if len(short) > 2:
                    keywords.add(short)

                mapping[sym] = [k for k in keywords if len(k) > 2]

        log.info(f"[{INSTANCE_ID}][COMPANY] Đã load {len(mapping)} công ty từ {path}.")
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][COMPANY] Lỗi đọc CSV {path}: {e}")
    return mapping

def fetch_rss_entries_for_urls(urls: list[str]) -> list[dict[str, Any]]:
    """
    Đọc danh sách RSS, trả về list các dict:
    {
        'title': str,
        'link': str,
        'summary': str,
        'published': datetime | None (Asia/Ho_Chi_Minh),
        'source': str,
    }

    ⚠️ Quan trọng: GỘP THEO LINK
    - Nếu 1 bài xuất hiện trong nhiều feed khác nhau → chỉ giữ 1 bản ghi theo link.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    by_link: dict[str, dict[str, Any]] = {}

    for url in urls:
        try:
            feed = feedparser.parse(url)
            source_title = getattr(feed.feed, "title", None) or url

            for entry in getattr(feed, "entries", []):
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or "").strip()
                if not title or not link:
                    continue

                summary = (getattr(entry, "summary", "") or "").strip()

                published_dt = None
                published_parsed = getattr(entry, "published_parsed", None)
                if published_parsed:
                    try:
                        ts = time.mktime(published_parsed)
                        published_dt = datetime.datetime.fromtimestamp(ts, vn_tz)
                    except Exception:
                        published_dt = None

                # Nếu đã có link này rồi → chỉ cập nhật thêm nguồn (source) nếu muốn
                if link in by_link:
                    # Gộp tên nguồn lại cho vui, không bắt buộc
                    old_source = by_link[link].get("source") or ""
                    if source_title and source_title not in old_source:
                        by_link[link]["source"] = (
                            old_source + " | " + source_title if old_source else source_title
                        )
                    # Không cần làm gì thêm
                    continue

                by_link[link] = {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": published_dt,
                    "source": source_title,
                }

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][RSS] Lỗi đọc RSS {url}: {e}")

    items = list(by_link.values())

    # Sắp xếp bài mới nhất lên trên
    def _sort_key(it: dict[str, Any]):
        dt = it.get("published")
        if isinstance(dt, datetime.datetime):
            return dt
        return datetime.datetime(1970, 1, 1, tzinfo=vn_tz)

    items.sort(key=_sort_key, reverse=True)
    return items


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
        
        if not get_bot_active() or not in_session_vietnam():
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

# =================================
# Daily_user_digest_loop
# =================================
def _clean_vnstock_columns(df):
    """Chuẩn hóa tên cột vnstock (MultiIndex) -> tên đơn giản."""
    new_columns = []
    for col in df.columns:
        if isinstance(col, tuple): col_name = col[-1]
        else: col_name = str(col)
        clean_name = col_name.lower().strip().replace('/', '').replace(' ', '_').replace('(', '').replace(')', '')
        new_columns.append(clean_name)
    df.columns = new_columns
    return df

def get_next_7am(now: datetime.datetime, vn_tz) -> datetime.datetime:
    """
    Tính mốc 07:00 sáng tiếp theo (cho vòng lặp quét báo cáo).
    - Nếu hôm nay < 07:00 -> 07:00 hôm nay.
    - Nếu hôm nay >= 07:00 -> 07:00 ngày mai.
    """
    # 1. Xác định 07:00 hôm nay
    target_today = now.replace(hour=7, minute=0, second=0, microsecond=0)
    
    # 2. Nếu đã qua 07:00 hôm nay, mục tiêu là 07:00 ngày mai
    if now >= target_today:
        target_tomorrow = target_today + datetime.timedelta(days=1)
        return target_tomorrow
    
    # 3. Nếu chưa tới 07:00 hôm nay, mục tiêu là hôm nay
    else:
        return target_today
    
async def sleep_until(dt_target: datetime.datetime, vn_tz):
    """
    Ngủ đến thời điểm dt_target (local VN).
    Cắt nhỏ nếu khoảng cách quá dài để tránh sleep quá lâu 1 lần.
    """
    while True:
        now = datetime.datetime.now(vn_tz)
        seconds = (dt_target - now).total_seconds()
        if seconds <= 0:
            break
        # ngủ tối đa 1 giờ mỗi lần cho an toàn
        await asyncio.sleep(min(seconds, 3600))

async def summarize_daily_news_with_ai(news_items: list) -> dict | None:
    """
    Phiên bản ULTIMATE: Tối ưu hóa tư duy nhà đầu tư, dynamic tags và chống trùng lặp.
    """
    if not news_items:
        return None

    # 1. Chuẩn bị dữ liệu thô (Kèm NHÃN GỢI Ý từ Code)
    # Tăng nhẹ giới hạn lên 90 tin để bao phủ rộng hơn
    items_to_process = news_items[:90]
    raw_text = ""
    for i, item in enumerate(items_to_process, 1):
        source_hint = f"[{item.get('source', 'N/A').upper()}]"
        raw_text += f"{i}. {source_hint} {item['title']} -- Link: {item['link']}\n"

    # 2. PROMPT "ULTIMATE"
    prompt = f"""
Bạn là Giám đốc Chiến lược (Chief Strategy Officer) của một quỹ đầu tư lớn tại Việt Nam.
Nhiệm vụ: Soạn thảo bản tin "Market Intelligence" gửi cho các nhà đầu tư VIP vào đầu ngày.

DỮ LIỆU ĐẦU VÀO (Kèm gợi ý [TAG] từ robot thu thập):
{raw_text}

---
### 🧠 QUY TRÌNH TƯ DUY XỬ LÝ (Chain-of-Thought):

1.  **THẨM ĐỊNH & SÀNG LỌC (Strict Filtering):**
    * **Loại bỏ ngay:** Tin rác, tin quảng cáo, tin đời sống/pháp luật (vụ án, thẩm mỹ viện, tai nạn), tin trùng lặp.
    * **Phân loại lại (Re-classify):** Đừng tin hoàn toàn vào [TAG] của robot.
        * Tin về *Tỉnh/Thành phố, Bộ ngành, Lãi suất, Giá vàng/Dầu* -> Bắt buộc là **MACRO**.
        * Tin về *Công ty niêm yết, Tập đoàn lớn* -> Bắt buộc là **CORPORATE**.

2.  **CHẤM ĐIỂM TÁC ĐỘNG (Impact Scoring):**
    * Chỉ chọn tin có khả năng làm giá cổ phiếu biến động (Score >= 6).

3.  **VIẾT NỘI DUNG (Investor Style):**
    * Không viết kiểu báo chí ("cho biết", "theo đó"). Viết kiểu dân tài chính: Ngắn, trực diện, tập trung vào con số/kết quả.
    * Ví dụ: Thay vì "VNM công bố trả cổ tức", viết "VNM chốt quyền cổ tức 15% tiền mặt".

---
### 📝 YÊU CẦU ĐẦU RA (JSON FORMAT):

{{
  "headline": [
    // Chọn ĐÚNG 3 tin chấn động nhất thị trường (Score 9-10).
    // Yêu cầu: Dynamic Tag (VD: "Vĩ mô", "Bank", "BĐS", "Thế giới"). KHÔNG dùng tag "HOT" chung chung.
    {{ "text": "Nội dung tóm tắt...", "link": "URL", "tag": "Tên Nhóm Tin" }}
  ],
  
  "corporate": [
    // Tối đa 8 tin doanh nghiệp tiêu biểu nhất.
    // QUAN TRỌNG: Không lặp lại tin đã đưa vào mục "headline".
    {{ 
      "ticker": "ABC", // BẮT BUỘC suy luận mã 3 chữ cái (VD: HPG, VHM, STB). Nếu không chắc chắn 100%, để null.
      "text": "Nội dung tóm tắt (tập trung KQKD, Cổ tức, Dự án)", 
      "link": "URL" 
    }}
  ],
  
  "macro": [
    // Tối đa 5 tin vĩ mô quan trọng nhất.
    // QUAN TRỌNG: Không lặp lại tin đã đưa vào mục "headline".
    {{ "text": "Nội dung tóm tắt...", "link": "URL" }}
  ],
  
  "sentiment_score": 7, // Thang điểm: 1-3 (Tiêu cực), 4-6 (Thận trọng/Trung lập), 7-10 (Tích cực/Hưng phấn).
  "comment": "Nhận định xu hướng dòng tiền và tâm lý thị trường dựa trên các tin trên (dưới 20 từ)."
}}
"""

    # 3. Gọi Gemini (Giữ nguyên)
    try:
        config = {"response_mime_type": "application/json"}
        json_str = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-pro", 
            contents=prompt,
            config=config
        )
        clean_json_str = json_str.replace("```json", "").replace("```", "").strip()
        import json
        return json.loads(clean_json_str)
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi tóm tắt JSON AI: {e}")
        return None

async def calculate_historical_valuation_task():
    """
    TÁC VỤ NẶNG: Tính trung bình P/E, P/B 5 năm cho toàn thị trường.
    Đã FIX: Áp dụng _clean_vnstock_columns để xử lý MultiIndex.
    """
    log.info(f"[{INSTANCE_ID}] 🧮 Bắt đầu tính toán định giá lịch sử (Mean Reversion)...")
    
    # 1. BỌC TRY/EXCEPT TỔNG QUÁT ĐỂ NGĂN TASK SẬP
    try:
        # 1. Lấy danh sách mã (Lọc sơ bộ để giảm tải)
        screener = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
        # --- LOGIC LỌC CƠ SỞ (Giữ nguyên) ---
        MIN_MARKET_CAP = 5000 
        MIN_TRADING_VAL = 10 
        screener['market_cap'] = pd.to_numeric(screener['market_cap'], errors='coerce').fillna(0)
        liq_col = 'total_trading_value'
        if 'avg_trading_value_20d' in screener.columns:
            liq_col = 'avg_trading_value_20d' 
        screener[liq_col] = pd.to_numeric(screener[liq_col], errors='coerce').fillna(0)

        valid_df = screener[
            (screener['market_cap'] >= MIN_MARKET_CAP) & 
            (screener[liq_col] >= MIN_TRADING_VAL)
        ]
        valid_tickers = valid_df['ticker'].tolist()
        log.info(f"[{INSTANCE_ID}] Tìm thấy {len(valid_tickers)} mã đủ tiêu chuẩn (Vốn hóa > {MIN_MARKET_CAP} tỷ, GTGD > {MIN_TRADING_VAL} tỷ).")
        
        history_data = {}
        count = 0
        consecutive_errors = 0 

        # 2. Loop và tính toán
        for i, sym in enumerate(valid_tickers):
            
            # 🔥 [RATE LIMIT] Nghỉ dài hơn sau mỗi 20 mã
            if i > 0 and i % 20 == 0:
                log.info(f"[{INSTANCE_ID}] 💤 Đã xử lý {i} mã. Nghỉ 5s để tránh Rate Limit...")
                await asyncio.sleep(5) 

            # 🔥 [RATE LIMIT] Nếu gặp lỗi liên tiếp > 5 lần (bị chặn IP)
            if consecutive_errors > 5:
                log.warning(f"[{INSTANCE_ID}] ⚠️ Phát hiện bị chặn liên tục. Ngủ 120s để cooldown...")
                await asyncio.sleep(120) 
                consecutive_errors = 0 

            # BỌC TRY/EXCEPT TỪNG MÃ
            try:
                # Gọi Finance lấy dữ liệu năm (có timeout)
                fin_df = await asyncio.wait_for(
                    asyncio.to_thread(lambda: Finance(symbol=sym, source='VCI').ratio(period='year', lang='vi')),
                    timeout=30.0 
                )
                
                if fin_df is not None and not fin_df.empty:
                    
                    # 🔥🔥🔥 FIX: GỌI HÀM LÀM SẠCH CỘT Ở ĐÂY 🔥🔥🔥
                    fin_df = _clean_vnstock_columns(fin_df) 
                    
                    df_5y = fin_df.head(5) 
                    
                    # Ép kiểu số (Tên cột đã được làm sạch thành 'pe' và 'pb')
                    pe_series = pd.to_numeric(df_5y['pe'], errors='coerce')
                    pb_series = pd.to_numeric(df_5y['pb'], errors='coerce')
                    
                    # Lọc bỏ giá trị âm/0 (lỗ)
                    pe_series = pe_series[pe_series > 0]
                    pb_series = pb_series[pb_series > 0]
                    
                    if len(pe_series) >= 3 and len(pb_series) >= 3:
                        history_data[sym] = {
                            'pe_avg': pe_series.mean(),
                            'pb_avg': pb_series.mean()
                        }
                        count += 1
                        consecutive_errors = 0 
                
                # 🔥 [RATE LIMIT] Tăng delay cơ bản 
                await asyncio.sleep(1.5) 
                
            except asyncio.TimeoutError:
                consecutive_errors += 1
                log.warning(f"Lỗi tính toán {sym}: Timeout (30s). Nghỉ 5s rồi thử mã kế tiếp.")
                await asyncio.sleep(5.0)
                continue
            except Exception as e:
                consecutive_errors += 1
                log.warning(f"Lỗi tính toán {sym} (Exception: {type(e).__name__}): {e}")
                # Nếu lỗi từng mã lẻ tẻ, nghỉ 5s rồi thử mã kế tiếp
                await asyncio.sleep(5.0)
                continue
        
        # 3. Lưu vào Redis
        if history_data:
            await asyncio.to_thread(save_historical_valuation_to_redis, history_data)
            log.info(f"[{INSTANCE_ID}] ✅ Hoàn tất tính toán. Đã lưu dữ liệu lịch sử cho {count} mã.")
        else:
            log.warning(f"[{INSTANCE_ID}] ⚠️ Không tính được dữ liệu lịch sử nào.")
            
    # XỬ LÝ LỖI TỔNG QUÁT (Task sẽ ngủ 120s rồi thoát khỏi hàm, không raise)
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] ❌ LỖI NGHIÊM TRỌNG (Mean Reversion Task) - Dừng 120s rồi thoát: {e}")
        await asyncio.sleep(120)

async def get_top_mean_reversion_stocks(limit=5):
    """
    Lấy Top cổ phiếu rẻ nhất theo chiến lược Mean Reversion cho Digest.
    Trả về danh sách các dict chứa đầy đủ thông tin định dạng cho UI.
    """
    try:
        # 1. Lấy dữ liệu lịch sử từ Redis
        hist_data = await asyncio.to_thread(get_historical_valuation_from_redis)
        if not hist_data:
            log.warning(f"[{INSTANCE_ID}] Digest: Chưa có dữ liệu định giá lịch sử. Đang kích hoạt tính toán...")
            asyncio.create_task(calculate_historical_valuation_task())
            return []

        # 2. Lấy dữ liệu hiện tại từ Screener API
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX,UPCOM"}, limit=1700))
        
        processed_items = []
        
        for index, row in screener_df.iterrows():
            sym = row['ticker']
            if sym not in hist_data: continue
            
            try:
                pe_cur = float(row['pe'])
                pb_cur = float(row['pb'])
            except: continue

            pe_avg = hist_data[sym]['pe_avg']
            pb_avg = hist_data[sym]['pb_avg']
            
            # 🔥 [FIX]: Loại bỏ cổ phiếu lỗ (PE âm) hoặc lỗi dữ liệu (<= 0)
            if pe_cur <= 0 or pb_cur <= 0: continue
            if pe_avg <= 0 or pb_avg <= 0: continue

            # --- TÍNH TOÁN LOGIC ---
            pe_discount = (pe_cur - pe_avg) / pe_avg
            pb_discount = (pb_cur - pb_avg) / pb_avg
            avg_discount = (pe_discount + pb_discount) / 2
            
            # Helper định dạng UI
            def get_ui_meta(discount):
                pct_val = abs(discount) * 100
                if discount < -0.1: return "diff-good", f"▼ {pct_val:.1f}%"
                elif discount > 0.1: return "diff-bad", f"▲ {pct_val:.1f}%"
                else: 
                    sign = "▲" if discount > 0 else "▼"
                    return "", f"{sign} {pct_val:.1f}%"

            pe_class, pe_diff_str = get_ui_meta(pe_discount)
            pb_class, pb_diff_str = get_ui_meta(pb_discount)
            
            if avg_discount < -0.1: signal_class, signal_text = "sig-cheap", "Định giá Rẻ"
            elif avg_discount > 0.1: signal_class, signal_text = "sig-expensive", "Đắt"
            else: signal_class, signal_text = "sig-fair", "Hợp lý"
            
            processed_items.append({
                "symbol": sym,
                "avg_discount_raw": avg_discount,
                "pe_cur": f"{pe_cur:.1f}", "pe_avg": f"{pe_avg:.1f}",
                "pe_class": pe_class, "pe_diff_str": pe_diff_str,
                "pb_cur": f"{pb_cur:.1f}", "pb_avg": f"{pb_avg:.1f}",
                "pb_class": pb_class, "pb_diff_str": pb_diff_str,
                "signal_class": signal_class, "signal_text": signal_text
            })

        # 3. Sắp xếp (Rẻ nhất lên đầu)
        processed_items.sort(key=lambda x: x['avg_discount_raw'])
        return processed_items[:limit]

    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi get_top_mean_reversion_stocks: {e}")
        return []
    
async def nightly_valuation_loop():
    """
    Chạy tính toán định giá Mean Reversion vào 02:00 sáng mỗi ngày.
    Lưu kết quả vào Redis để 07:00 sáng Digest chỉ việc lấy ra dùng.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] Khởi động loop tính toán đêm (02:00).")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # 1. Tính thời gian ngủ tới 02:00 sáng hôm sau (hoặc hôm nay nếu chưa tới)
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        
        wait_sec = (target - now).total_seconds()
        log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL {loop_id}] Ngủ {wait_sec/3600:.1f}h tới {target.strftime('%Y-%m-%d %H:%M')} để tính định giá.")
        
        await asyncio.sleep(wait_sec)

        # 2. Thức dậy & Kiểm tra
        if not get_bot_active():
            log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] Bot đang TẮT, bỏ qua lần chạy này.")
            await asyncio.sleep(60)
            continue

        # 3. Chạy tính toán (Task nặng)
        try:
            log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] 🌙 02:00! Bắt đầu tính toán Historical Valuation...")
            # Gọi trực tiếp và chờ cho xong (vì lúc này đêm khuya, không sợ block ai)
            await calculate_historical_valuation_task()
            
        except Exception as e:
            log.error(f"[{INSTANCE_ID}][NIGHTLY_VAL] ❌ Lỗi: {e}")
            await asyncio.sleep(300) # Ngủ 5p tránh lỗi lặp
    
async def daily_user_digest_loop():
    """
    [WORKER] Tính toán Digest sáng (AI + Data) và đẩy sang Gateway.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Digest Loop (07:00)...")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        if not get_bot_active():
            log.info(f"[{INSTANCE_ID}] Bot đang OFF (DB). Alert Loop ngủ 60s.")
            await asyncio.sleep(60) # Ngủ lâu hơn để đỡ spam DB
            continue

        # 1. Ngủ tới 07:00 sáng
        target = get_next_7am(now, vn_tz)
        wait_sec = (target - now).total_seconds()
        
        # Log nhẹ để biết bao lâu nữa chạy
        if wait_sec > 60:
            log.info(f"[DIGEST] Ngủ {wait_sec/3600:.1f}h tới {target.strftime('%H:%M %d/%m')}")
        
        await sleep_until(target, vn_tz)

        if not get_bot_active():
            await asyncio.sleep(60); continue

        log.info("[DIGEST] 🌅 07:00! Bắt đầu tạo bản tin...")

        try:
            # 2. Thu thập dữ liệu (Song song)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            since_utc = now_utc - datetime.timedelta(hours=24)

            (
                bctc_rows, report_rows, macro_rows, spec_rows,
                all_watch, pro_chat_ids, top_value_stocks
            ) = await asyncio.gather(
                asyncio.to_thread(get_recent_bctc_notified, since_utc),
                asyncio.to_thread(get_recent_analysis_reports, since_utc),
                asyncio.to_thread(get_recent_news_seen, "MACRO", since_utc),
                asyncio.to_thread(get_recent_news_seen, "SPECIALIZED", since_utc),
                asyncio.to_thread(get_all_watch),
                asyncio.to_thread(get_all_pro_chat_ids),
                get_top_mean_reversion_stocks(limit=5)
            )

            # 3. Xử lý AI Tin tức
            all_news = []
            for r in macro_rows: all_news.append({"title": r[0], "link": r[1], "source": "Vĩ mô"})
            for r in spec_rows: all_news.append({"title": r[0], "link": r[1], "source": "DN"})
            
            ai_text = "_Không có tin nổi bật._"
            ai_data = None
            
            if all_news:
                ai_data = await summarize_daily_news_with_ai(all_news) # Hàm này bạn đã copy sang
                if ai_data:
                    lines = []
                    if ai_data.get('headline'):
                        lines.append("⚡ *TIÊU ĐIỂM*")
                        for i in ai_data['headline']: lines.append(f"• {i['text']}")
                    if ai_data.get('comment'):
                        lines.append(f"\n🧠 *AI:* {ai_data['comment']}")
                    ai_text = "\n".join(lines)

            # 4. Tạo Payload cho từng User
            # (Logic mapping dữ liệu vào payload giữ nguyên như cũ)
            # ... [Đoạn code map dữ liệu bctc, reports vào digest_payloads] ...
            # Để ngắn gọn, mình giả định bạn copy logic map từ file cũ vào đây.
            # Nếu cần mình sẽ viết chi tiết phần map này.
            
            # Giả sử đã có digest_payloads = {chat_id: {...data...}}
            
            # --- ĐOẠN LOGIC MAP DỮ LIỆU (Rút gọn để bạn copy vào) ---
            digest_payloads = {}
            watch_to_chats = {}
            for ck, blk in all_watch.items():
                try: cid = int(ck); digest_payloads[cid] = {"is_pro": (cid in pro_chat_ids or cid == ADMIN_ID), "ai_news": ai_data, "value_stocks": [], "bctc": [], "reports": []}
                except: continue
                for s in blk.get("list", []): watch_to_chats.setdefault(str(s).upper(), []).append(cid)

            if top_value_stocks:
                for cid, pl in digest_payloads.items():
                    if pl["is_pro"]: pl["value_stocks"] = top_value_stocks

            for sym, (y, q, t) in {str(s).upper(): (y,q,t) for s,y,q,t in bctc_rows}.items():
                t_str = t.astimezone(vn_tz).strftime("%H:%M %d/%m")
                for cid in watch_to_chats.get(sym, []):
                    pl = digest_payloads[cid]
                    if pl["is_pro"] or not any(x['symbol']==sym for x in pl['bctc']):
                        pl["bctc"].append({"symbol": sym, "year": y, "quarter": q, "time": t_str, "is_locked": not pl["is_pro"]})
            
            # (Tương tự cho Reports - Copy từ file cũ)
            # --------------------------------------------------------

            # 5. BẮN SANG REDIS
            count = 0
            for chat_id, data in digest_payloads.items():
                # Lưu Web App Data vào Redis (chung Redis connection)
                digest_id = uuid.uuid4().hex
                r_client.set(f"digest_web:{digest_id}", json.dumps(data), ex=86400)
                
                web_url = f"{BASE_URL}/digest/{digest_id}"
                
                # Tạo nút bấm
                kb = {
                    "inline_keyboard": [[
                        {"text": "📰 Xem Chi Tiết (Web App) 🚀", "web_app": {"url": web_url}}
                    ]]
                }
                
                msg_text = (
                    f"🌅 *BẢN TIN SÁNG {now.strftime('%d/%m')}* 🤖\n\n"
                    f"{ai_text}\n\n"
                    f"👉 *Nhấn nút dưới để xem chi tiết danh mục!*"
                )

                # GỬI VỚI CỜ HIỆU "DIGEST"
                push_telegram_msg(
                    chat_id=chat_id,
                    text=msg_text,
                    reply_markup=kb,
                    msg_type="DIGEST" # <--- Cờ hiệu quan trọng
                )
                count += 1
            
            log.info(f"[DIGEST] Đã đẩy {count} bản tin sang Gateway.")

        except Exception as e:
            log.error(f"[DIGEST] Lỗi: {e}")
            await asyncio.sleep(60)

# ==============================
# BÁO CÁO TÀI CHÍNH (BCTC) LOOP
# ==============================

BCTC_MONTHS = [1, 4, 5, 10] # Tháng có thể ra BCTC

# Ngày bắt đầu check trong từng tháng (có thể chỉnh)
BCTC_START_DAY_BY_MONTH = {
    1: 1,   # Tháng 1 -> BCTC Quý 4 năm trước
    4: 1,   # Tháng 4 -> BCTC Quý 1
    5: 1,   # Tháng 5 -> BCTC Quý 2
    10: 1,  # Tháng 10 -> BCTC Quý 3
}


def get_bctc_period_for_date(dt: datetime.datetime) -> tuple[int, int] | None:
    """
    Map tháng hiện tại sang (year, quarter) BCTC tương ứng:
    - Tháng 1  -> BCTC Quý 4 năm trước
    - Tháng 4  -> BCTC Quý 1 năm nay
    - Tháng 5  -> BCTC Quý 2 năm nay
    - Tháng 10 -> BCTC Quý 3 năm nay
    """
    m = dt.month
    y = dt.year
    if m == 1:
        return y - 1, 4
    if m == 4:
        return y, 1
    if m == 5:
        return y, 2
    if m == 10:
        return y, 3
    return None


def get_next_bctc_period_2am(now: datetime.datetime, vn_tz) -> datetime.datetime:
    """
    Tính mốc 02:00 sáng đầu kỳ quý sau (tháng BCTC kế tiếp).
    Dùng BCTC_START_DAY_BY_MONTH làm ngày bắt đầu.
    """
    months = BCTC_MONTHS
    year = now.year

    # Tìm tháng BCTC tiếp theo
    for _ in range(3):  # tối đa nhảy 2 năm là dư
        for m in months:
            start_day = BCTC_START_DAY_BY_MONTH.get(m, 1)
            target_naive = datetime.datetime(year, m, start_day, 2, 0, 0)
            target = vn_tz.localize(target_naive)
            if target > now:
                return target
        year += 1  # nếu chưa tìm thấy trong năm hiện tại thì sang năm sau

    # fallback: nếu có gì sai sai thì cho ngủ 1 ngày
    return now + datetime.timedelta(days=1)


async def sleep_until(dt_target: datetime.datetime, vn_tz):
    """
    Ngủ đến thời điểm dt_target (local VN).
    Cắt nhỏ nếu khoảng cách quá dài để tránh sleep quá lâu 1 lần.
    """
    while True:
        now = datetime.datetime.now(vn_tz)
        seconds = (dt_target - now).total_seconds()
        if seconds <= 0:
            break
        # ngủ tối đa 1 giờ mỗi lần cho an toàn
        await asyncio.sleep(min(seconds, 3600))

async def financial_Statements_notice_loop():
    """
    (ĐÃ SỬA) Quét BCTC và chỉ thông báo cho user Pro + Admin.
    (SỬA LẦN 2: CHỈ THU THẬP, KHÔNG GỬI TIN 8:00)
    """

    vn_tz = pytz.timezone(TIMEZONE)

    while True:

        # ... (Toàn bộ logic kiểm tra BOT_ACTIVE, tính toán kỳ BCTC,
        #      ngủ đến 02:00 sáng giữ nguyên) ...
        
        now = datetime.datetime.now(pytz.timezone(TIMEZONE))
        period = get_bctc_period_for_date(now)
        period_label = f"Quý {period[1]}/{period[0]}" if period else "N/A"
        log.info(
            f"[{INSTANCE_ID}][BCTC] Loop BCTC đang chạy – now = {now.strftime('%Y-%m-%d %H:%M:%S')}, "
            f"period = {period_label}"
        )

        if not get_bot_active():
            await asyncio.sleep(60)
            continue

        now = datetime.datetime.now(vn_tz)

        if now.month not in BCTC_MONTHS:
            target = get_next_bctc_period_2am(now, vn_tz)
            log.info(
                f"[{INSTANCE_ID}][BCTC] Không phải tháng BCTC (tháng {now.month}), "
                f"ngủ tới {target}."
            )
            await sleep_until(target, vn_tz)
            continue

        period = get_bctc_period_for_date(now)
        if not period:
            log.warning(f"[{INSTANCE_ID}][BCTC] Không map được kỳ BCTC, sleep 1 ngày.")
            await asyncio.sleep(24 * 3600)
            continue

        year, quarter = period
        period_label = f"Quý {quarter}/{year}"
        month = now.month
        start_day = BCTC_START_DAY_BY_MONTH.get(month, 1)

        first_2am_this_month = vn_tz.localize(
            datetime.datetime(now.year, month, start_day, 2, 0, 0)
        )
        if now < first_2am_this_month:
            log.info(
                f"[{INSTANCE_ID}][BCTC] Tháng {month} nhưng mới ngày {now.day}, "
                f"ngủ tới {first_2am_this_month} để bắt đầu crawl BCTC {period_label}."
            )
            await sleep_until(first_2am_this_month, vn_tz)
            continue

        today = now.date()
        two_am_today = vn_tz.localize(
            datetime.datetime(today.year, today.month, today.day, 2, 0, 0)
        )

        # ❗️ SỬA: XÓA BIẾN eight_am_today VÌ KHÔNG CẦN NỮA
        # eight_am_today = vn_tz.localize(
        #     datetime.datetime(today.year, today.month, today.day, 8, 0, 0)
        # )

        now = datetime.datetime.now(vn_tz)
        if now < two_am_today:
            log.info(
                f"[{INSTANCE_ID}][BCTC] Hôm nay {today} chưa tới 02:00, ngủ tới {two_am_today}."
            )
            await sleep_until(two_am_today, vn_tz)

        # 1.1️⃣ 02:00 -> CRAWL BCTC (Giữ nguyên logic crawl)
        log.info(f"[{INSTANCE_ID}][BCTC] 02:00 – bắt đầu crawl BCTC {period_label} cho hôm nay.")

        try:
            all_watch = await asyncio.to_thread(get_all_watch)
            pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][BCTC] Lỗi get_all_watch: {e}")
            tomorrow = today + datetime.timedelta(days=1)
            target = vn_tz.localize(
                datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day, 2, 0, 0)
            )
            await sleep_until(target, vn_tz)
            continue

        symbol_set: set[str] = set()
        for chat_key, info in all_watch.items():
            try:
                chat_id = int(chat_key)
                if chat_id not in pro_chat_ids and chat_id != ADMIN_ID:
                    continue
            except Exception:
                continue

            syms = info.get("list") if isinstance(info, dict) else info
            if not syms:
                continue
            for sym in syms:
                s = str(sym).upper().strip()
                if s:
                    symbol_set.add(s)

        pending_after = 0
        for sym in sorted(symbol_set):
            try:
                already = await asyncio.to_thread(
                    has_bctc_notified, sym, year, quarter
                )
            except Exception as e:
                log.warning(
                    f"[{INSTANCE_ID}][BCTC] Lỗi has_bctc_notified({sym}): {e}"
                )
                continue
            if already:
                continue

            try:
                available = await asyncio.to_thread(
                    check_bctc_available, sym, year, quarter
                )
            except Exception as e:
                log.warning(
                    f"[{INSTANCE_ID}][BCTC] Lỗi check_bctc_available({sym}): {e}"
                )
                pending_after += 1
                continue
            if not available:
                pending_after += 1
                continue

            try:
                # Chỉ đánh dấu, KHÔNG add vào queue gửi 8:00 nữa
                await asyncio.to_thread(mark_bctc_notified, sym, year, quarter)
                
                # ❗️ SỬA: XÓA DÒNG NÀY
                # await asyncio.to_thread(add_bctc_queue, sym, year, quarter, today)
                
            except Exception as e:
                log.warning(
                    f"[{INSTANCE_ID}][BCTC] Lỗi mark_bctc_notified({sym}, Q{quarter}/{year}): {e}"
                )

            await asyncio.sleep(0.2)

        still_pending = pending_after > 0
        log.info(
            f"[{INSTANCE_ID}][BCTC] Crawl xong BCTC (chỉ cho Pro user) {period_label} hôm nay. "
            f"still_pending = {still_pending}."
        )

        # ❗️ SỬA: XÓA TOÀN BỘ KHỐI LOGIC GỬI TIN LÚC 8:00
        # (Xóa từ "Đợi tới 08:00" đến hết "clear_bctc_queue_entry")
        
        # 3️⃣ Quyết định ngủ tới khi nào (Giữ nguyên)
        now = datetime.datetime.now(vn_tz)
        if still_pending:
            tomorrow = today + datetime.timedelta(days=1)
            target = vn_tz.localize(
                datetime.datetime(
                    tomorrow.year, tomorrow.month, tomorrow.day, 2, 0, 0
                )
            )
            log.info(
                f"[{INSTANCE_ID}][BCTC] Vẫn còn mã (của Pro) chưa có BCTC {period_label}, "
                f"ngủ tới {target}."
            )
            await sleep_until(target, vn_tz)
        else:
            target = get_next_bctc_period_2am(now, vn_tz)
            log.info(
                f"[{INSTANCE_ID}][BCTC] Đã hoàn thành BCTC {period_label} cho tất cả mã (của Pro), "
                f"ngủ tới {target} (kỳ quý sau)."
            )
            await sleep_until(target, vn_tz)

#-------------------------------------------
def get_next_7am(now: datetime.datetime, vn_tz) -> datetime.datetime:
    """
    Tính mốc 07:00 sáng tiếp theo (cho vòng lặp quét báo cáo).
    - Nếu hôm nay < 07:00 -> 07:00 hôm nay.
    - Nếu hôm nay >= 07:00 -> 07:00 ngày mai.
    """
    # 1. Xác định 07:00 hôm nay
    target_today = now.replace(hour=7, minute=0, second=0, microsecond=0)
    
    # 2. Nếu đã qua 07:00 hôm nay, mục tiêu là 07:00 ngày mai
    if now >= target_today:
        target_tomorrow = target_today + datetime.timedelta(days=1)
        return target_tomorrow
    
    # 3. Nếu chưa tới 07:00 hôm nay, mục tiêu là hôm nay
    else:
        return target_today

async def analysis_report_loop():
    """
    (TÍNH NĂNG MỚI - ĐÃ SỬA)
    Quét báo cáo phân tích 1 LẦN/NGÀY lúc 07:00 SÁNG.
    (SỬA LẦN 2: CHỈ THU THẬP, KHÔNG GỬI TIN)
    (SỬA LẦN 3: GỠ PAYWALL - THU THẬP CHO TẤT CẢ USER)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    warmed_up = False 

    while True:
        loop_id += 1
        loop_label = f"[{INSTANCE_ID}][REPORT_SCAN {loop_id}]"
        now = datetime.datetime.now(vn_tz)

        if not get_bot_active():
            log.info(f"{loop_label} Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue
            
        try:
            # ... (Logic chờ 7:00 sáng giữ nguyên) ...
            target_7am = get_next_7am(now, vn_tz)
            log.info(f"{loop_label} Đang chờ cho đến {target_7am.strftime('%Y-%m-%d %H:%M')}.")
            await sleep_until(target_7am, vn_tz)
            
            if not get_bot_active():
                log.info(f"{loop_label} Thức dậy lúc 07:00 nhưng bot TẮT, bỏ qua.")
                continue
            
            log.info(f"{loop_label} 07:00! Bắt đầu quét báo cáo...")

            # 1. GATHER
            # ❗️ SỬA: GỠ BỎ PAYWALL (1)
            # pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
            all_watch = await asyncio.to_thread(get_all_watch)

            # 2. MAP
            # ❗️ SỬA: Đổi tên biến (không còn là "pro" nữa)
            all_symbol_set: set[str] = set()

            for chat_key, user_block in all_watch.items():
                try:
                    chat_id = int(chat_key)
                except Exception:
                    continue
                
                # ❗️ SỬA: GỠ BỎ PAYWALL (2)
                # (Không cần check if chat_id == ADMIN_ID or chat_id in pro_chat_ids)
                watch_list = user_block.get("list", []) or []
                for sym in watch_list:
                    s = str(sym).upper().strip()
                    if s and len(s) == 3:
                        all_symbol_set.add(s)

            # ❗️ SỬA: Cập nhật log
            if not all_symbol_set:
                log.info(f"{loop_label} Không có mã nào trong watchlist, bỏ qua lần quét này.")
                continue 

            log.info(f"{loop_label} Bắt đầu quét báo cáo cho {len(all_symbol_set)} mã (tất cả user).")
            
            # 3. FETCH & PROCESS
            # ❗️ SỬA: Lặp qua all_symbol_set
            for symbol in sorted(all_symbol_set):
                if not get_bot_active(): 
                    log.info(f"{loop_label} Bot TẮT giữa chừng, dừng quét.")
                    break
                
                # ... (Logic bên trong (company.reports, mark_report_seen) giữ nguyên) ...
                try:
                    company = Company(symbol=symbol)
                    df = await asyncio.to_thread(company.reports)
                    
                    if df is None or df.empty:
                        log.info(f"{loop_label} Không có báo cáo cho {symbol}.")
                        await asyncio.sleep(3) 
                        continue
                    
                    log.info(f"{loop_label} Tìm thấy {len(df)} báo cáo cho {symbol}. Đang lọc...")

                    # 4. CHECK & MARK
                    for row in df.itertuples():
                        title = getattr(row, "name", "")
                        link = getattr(row, "link", "")
                        date_str = getattr(row, "date", "")
                        
                        if not link or not date_str:
                            continue 

                        is_seen = await asyncio.to_thread(has_report_seen, link, date_str)
                        
                        if not is_seen:
                            await asyncio.to_thread(
                                mark_report_seen, symbol, link, title, date_str
                            )
                            
                            if not warmed_up:
                                log.info(f"{loop_label} (Warm-up) Đã đánh dấu: {symbol} - {title}")
                            else:
                                log.info(f"{loop_label} (MỚI) Phát hiện: {symbol} - {title}")
                            
                            await asyncio.sleep(0.2) 

                except Exception as e:
                    log.warning(f"{loop_label} Lỗi khi xử lý mã {symbol}: {e}")

                await asyncio.sleep(3) 
            
            warmed_up = True
            
            log.info(f"{loop_label} Đã quét xong báo cáo phân tích (chỉ thu thập).")

        except Exception as e:
            log.error(f"{loop_label} Lỗi nghiêm trọng: {e}")
            await asyncio.sleep(600)
        
        log.info(f"{loop_label} Hoàn tất lần quét 07:00 (chỉ thu thập).")

#-------------------------------------------

async def nightly_valuation_loop():
    """
    Chạy tính toán định giá Mean Reversion vào 02:00 sáng mỗi ngày.
    Lưu kết quả vào Redis để 07:00 sáng Digest chỉ việc lấy ra dùng.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] Khởi động loop tính toán đêm (02:00).")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # 1. Tính thời gian ngủ tới 02:00 sáng hôm sau (hoặc hôm nay nếu chưa tới)
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        
        wait_sec = (target - now).total_seconds()
        log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL {loop_id}] Ngủ {wait_sec/3600:.1f}h tới {target.strftime('%Y-%m-%d %H:%M')} để tính định giá.")
        
        await asyncio.sleep(wait_sec)

        # 2. Thức dậy & Kiểm tra
        if not get_bot_active():
            log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] Bot đang TẮT, bỏ qua lần chạy này.")
            await asyncio.sleep(60)
            continue

        # 3. Chạy tính toán (Task nặng)
        try:
            log.info(f"[{INSTANCE_ID}][NIGHTLY_VAL] 🌙 02:00! Bắt đầu tính toán Historical Valuation...")
            # Gọi trực tiếp và chờ cho xong (vì lúc này đêm khuya, không sợ block ai)
            await calculate_historical_valuation_task()
            
        except Exception as e:
            log.error(f"[{INSTANCE_ID}][NIGHTLY_VAL] ❌ Lỗi: {e}")
            await asyncio.sleep(300) # Ngủ 5p tránh lỗi lặp

#-------------------------------------------
async def restore_reminder_loop():
    """
    [WORKER] Chỉ nhắc nhở Admin bằng Text.
    Việc tạo file và gửi file sẽ do Admin chủ động làm bằng lệnh /backup_core.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    last_reminder_month = None

    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Restore Reminder Loop...")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)
        
        # 1. Ngủ 1 tiếng (Check mỗi giờ)
        await asyncio.sleep(3600)

        if not get_bot_active():
            continue

        # 2. Logic kiểm tra ngày 7
        # Nếu hôm nay là ngày 7 VÀ chưa nhắc trong tháng này
        current_month = now.strftime("%Y-%m")
        
        if now.day == 7 and last_reminder_month != current_month:
            # Kiểm tra giờ cho lịch sự (ví dụ chỉ nhắc sau 8h sáng)
            if now.hour >= 8:
                if ADMIN_ID:
                    msg = (
                        f"⏰ **NHẮC NHỞ BẢO TRÌ ĐỊNH KỲ (Tháng {now.month})**\n\n"
                        f"Hôm nay là ngày 7. Đã đến lúc sao lưu dữ liệu Core.\n"
                        f"👉 Vui lòng gõ lệnh `/backup_core` để tải bản backup về.\n"
                        f"👉 Sau đó kiểm tra và chạy `/restore_core` nếu cần chuyển Database."
                    )
                    
                    # Bắn tin nhắc nhở
                    push_telegram_msg(ADMIN_ID, msg, msg_type="SYSTEM_MSG")
                    
                    log.info(f"[REMINDER] Đã nhắc Admin bảo trì tháng {current_month}")
                    last_reminder_month = current_month

# ==============================
# BÁO CÁO TÀI CHÍNH (BCTC) Function
# ==============================

# Chu kỳ wake-up khi đang trong tháng BCTC (10 phút)
BCTC_ACTIVE_LOOP_SLEEP = 600
# Khi ngoài tháng BCTC: ngủ 6 tiếng
BCTC_OUTSIDE_LOOP_SLEEP = 6 * 3600
    
def infer_latest_quarter_from_df(df: pd.DataFrame) -> tuple[int, int] | None:
    """
    Lấy (year, quarter) mới nhất từ DataFrame BCTC.

    Dựa trên 2 cột:
    - 'year'
    - 'quarter'
    """
    if df is None or df.empty:
        return None

    if "year" not in df.columns or "quarter" not in df.columns:
        log.warning(f"[BCTC] DF không có cột 'year' / 'quarter'. Columns = {list(df.columns)}")
        return None

    try:
        tmp = df[["year", "quarter"]].dropna()
        if tmp.empty:
            return None

        tmp["_y"] = tmp["year"].astype(int)
        tmp["_q"] = tmp["quarter"].astype(int)

        row = tmp.sort_values(["_y", "_q"]).iloc[-1]
        return int(row["_y"]), int(row["_q"])
    except Exception as e:
        log.warning(f"[BCTC] infer_latest_quarter_from_df lỗi: {e}")
        return None


def check_bctc_available(symbol: str, target_year: int, target_quarter: int) -> bool:
    """
    True nếu vnstock đã có BCTC quý (target_year, target_quarter) cho mã này.

    Logic:
    - Gọi Finance(symbol, source='TCBS')
    - Lấy income_statement & balance_sheet theo kỳ 'quarter'
    - Đoán quý mới nhất từ 2 bảng này
    - Nếu quý mới nhất >= quý cần tìm -> coi như đã có BCTC
    """
    sym = str(symbol).upper().strip()
    if not sym:
        return False

    try:
        fin = Finance(symbol=sym, source="TCBS")
    except Exception as e:
        log.warning(f"[BCTC] Lỗi tạo Finance({sym}): {e}")
        return False

    dfs: list[pd.DataFrame] = []

    # KQKD theo quý
    try:
        is_df = fin.income_statement(period="quarter", lang="vi", dropna=False)
        dfs.append(is_df)
    except Exception as e:
        log.debug(f"[BCTC] income_statement(quater) lỗi cho {sym}: {e}")

    # CĐKT theo quý
    try:
        bs_df = fin.balance_sheet(period="quarter", lang="vi", dropna=False)
        dfs.append(bs_df)
    except Exception as e:
        log.debug(f"[BCTC] balance_sheet(quater) lỗi cho {sym}: {e}")

    if not dfs:
        return False

    latest_yq: tuple[int, int] | None = None

    for df in dfs:
        yq = infer_latest_quarter_from_df(df)
        if not yq:
            continue

        if latest_yq is None:
            latest_yq = yq
        else:
            ly, lq = latest_yq
            cy, cq = yq
            if (cy > ly) or (cy == ly and cq > lq):
                latest_yq = yq

    if not latest_yq:
        return False

    latest_y, latest_q = latest_yq
    log.info(
        f"[BCTC] {sym} – quý mới nhất: Q{latest_q}/{latest_y}, "
        f"đang so với Q{target_quarter}/{target_year}"
    )

    # Nếu quý mới nhất >= quý cần tìm → xem như đã có BCTC của quý đó
    return (latest_y > target_year) or (
        latest_y == target_year and latest_q >= target_quarter
    )


# ===============================================================
# HÀM HELPER TÍNH GIỜ QUÉT (Dùng chung cho cả 2 loop tin tức)
# ===============================================================
def get_seconds_until_next_scan(now: datetime.datetime, target_hours: list[int]) -> float:
    """
    Tính số giây từ 'now' đến mốc giờ cố định tiếp theo trong danh sách target_hours.
    Ví dụ: target_hours=[6, 18] -> Sẽ trả về giây tới 06:00 hoặc 18:00 gần nhất.
    """
    candidates = []
    for h in target_hours:
        # Tạo mốc giờ h:00:00 hôm nay
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        # Nếu giờ này đã qua, dời sang ngày mai
        if t <= now:
            t += datetime.timedelta(days=1)
        candidates.append(t)
    
    # Lấy mốc thời gian gần nhất trong tương lai
    next_run = min(candidates)
    return (next_run - now).total_seconds()


# ===============================================================
# LOOP TIN CHUYÊN NGÀNH (Sửa đổi: Quét 06:00 & 18:00)
# ===============================================================
async def news_specialized_loop():
    """
    Quét RSS chuyên ngành 2 LẦN/NGÀY (06:00 và 18:00).
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    
    # 🕒 CẤU HÌNH GIỜ QUÉT (Sáng & Chiều)
    SCAN_HOURS = [6, 18] 

    all_specialized_urls: list[str] = []
    for urls in RSS_FEEDS_SPECIALIZED.values():
        all_specialized_urls.extend(urls)

    log.info(f"[{INSTANCE_ID}][NEWS_SPEC] Đã chuyển sang chế độ quét theo lịch: {SCAN_HOURS}h hàng ngày.")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # 1. TÍNH TOÁN THỜI GIAN NGỦ TỚI PHIÊN TIẾP THEO
        # Nếu bot vừa khởi động (loop_id=1), ta có thể cho chạy ngay hoặc chờ. 
        # Ở đây mình chọn logic: Chờ đúng giờ mới chạy để tiết kiệm resource.
        wait_seconds = get_seconds_until_next_scan(now, SCAN_HOURS)
        next_run_str = (now + datetime.timedelta(seconds=wait_seconds)).strftime("%H:%M %d/%m")
        
        log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Ngủ {wait_seconds/3600:.1f}h. Quét tiếp theo lúc: {next_run_str}")
        
        await asyncio.sleep(wait_seconds)

        # 2. THỨC DẬY & KIỂM TRA ACTIVE
        if not get_bot_active():
            log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Đến giờ quét nhưng Bot đang TẮT.")
            await asyncio.sleep(60) # Chờ 1 phút rồi check lại schedule
            continue

        # 3. THỰC HIỆN QUÉT (Logic cũ giữ nguyên)
        try:
            log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] 🟢 Bắt đầu quét tin chuyên ngành...")
            
            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, all_specialized_urls
            )
            
            # Giới hạn số lượng để xử lý nhanh
            if len(entries) > NEWS_MAX_RSS_ENTRIES_PER_RUN:
                entries = entries[:NEWS_MAX_RSS_ENTRIES_PER_RUN]

            new_entries = []
            scan_now = datetime.datetime.now(vn_tz) # Lấy giờ thực tế lúc quét
            
            for it in entries:
                link = (it.get("link") or "").strip()
                if not link: continue
                
                # Check tươi mới & Check đã xem
                pub_dt = it.get("published")
                if not is_fresh_news(pub_dt, scan_now): continue
                
                is_seen = await asyncio.to_thread(
                    has_news_seen,
                    NEWS_FEED_TYPE_SPECIALIZED,
                    link,
                )
                if not is_seen:
                    new_entries.append(it)
            
            if not new_entries:
                log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Không có bài mới.")
                continue

            # Lưu vào DB (Chỉ thu thập)
            unique_count = 0
            for it in new_entries:
                await asyncio.to_thread(
                    mark_news_seen,
                    NEWS_FEED_TYPE_SPECIALIZED,
                    link=it["link"],
                    guid=None,
                    title=it["title"],
                    published=it["published"],
                )
                unique_count += 1

            log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] ✅ Đã lưu {unique_count} tin mới (chờ Digest sáng).")

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Lỗi tổng quát: {e}")
        
        # (Sau khi quét xong, vòng lặp quay lại đầu -> tính giờ ngủ tiếp theo)


# ===============================================================
# 2. LOOP TIN VĨ MÔ (Sửa đổi: Quét 06:00 & 18:00)
# ===============================================================
async def news_macro_loop():
    """
    Quét RSS vĩ mô 2 LẦN/NGÀY (06:00 và 18:00).
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    
    # 🕒 CẤU HÌNH GIỜ QUÉT
    SCAN_HOURS = [6, 18]

    log.info(f"[{INSTANCE_ID}][NEWS_MACRO] Đã chuyển sang chế độ quét theo lịch: {SCAN_HOURS}h hàng ngày.")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # 1. TÍNH TOÁN THỜI GIAN NGỦ
        wait_seconds = get_seconds_until_next_scan(now, SCAN_HOURS)
        next_run_str = (now + datetime.timedelta(seconds=wait_seconds)).strftime("%H:%M %d/%m")

        log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Ngủ {wait_seconds/3600:.1f}h. Quét tiếp theo lúc: {next_run_str}")
        
        await asyncio.sleep(wait_seconds)

        # 2. THỨC DẬY & KIỂM TRA ACTIVE
        if not get_bot_active():
            log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Đến giờ quét nhưng Bot đang TẮT.")
            await asyncio.sleep(60)
            continue

        # 3. THỰC HIỆN QUÉT (Logic cũ giữ nguyên)
        try:
            log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] 🟢 Bắt đầu quét tin vĩ mô...")

            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, RSS_FEEDS_MACRO
            )
            
            if len(entries) > NEWS_MAX_RSS_ENTRIES_PER_RUN:
                entries = entries[:NEWS_MAX_RSS_ENTRIES_PER_RUN]

            new_entries = []
            scan_now = datetime.datetime.now(vn_tz)
            
            for it in entries:
                link = (it.get("link") or "").strip()
                if not link: continue
                
                pub_dt = it.get("published")
                if not is_fresh_news(pub_dt, scan_now): continue
                
                is_seen = await asyncio.to_thread(
                    has_news_seen,
                    NEWS_FEED_TYPE_MACRO,
                    link,
                )
                if not is_seen:
                    new_entries.append(it)
            
            if not new_entries:
                log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Không có bài mới.")
                continue

            # Lưu vào DB
            unique_count = 0
            for it in new_entries:
                await asyncio.to_thread(
                    mark_news_seen,
                    NEWS_FEED_TYPE_MACRO,
                    link=it["link"],
                    guid=None,
                    title=it["title"],
                    published=it["published"],
                )
                unique_count += 1
            
            log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] ✅ Đã lưu {unique_count} tin vĩ mô mới.")

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Lỗi tổng quát: {e}")

async def news_cleanup_loop():
    """
    Loop dọn dẹp định kỳ mỗi 24h:
    1. Xoá news_seen cũ (> 180 ngày).
    2. Xoá đơn hàng PENDING treo (> 3 ngày).
    """
    RETENTION_DAYS_NEWS = 180
    RETENTION_DAYS_ORDERS = 3  # Xóa đơn treo sau 3 ngày
    INTERVAL_SECONDS = 24 * 60 * 60  # 24 giờ

    while True:
        try:
            # 1. Dọn News (Code cũ)
            deleted_news = await asyncio.to_thread(
                cleanup_old_news_seen,
                RETENTION_DAYS_NEWS,
            )
            if deleted_news > 0:
                log.info(f"[MAINTENANCE] Đã xoá {deleted_news} bản ghi news_seen cũ.")

            # 2. 🔥 [MỚI] Dọn đơn hàng PENDING quá hạn
            deleted_orders = await asyncio.to_thread(
                cleanup_old_pending_orders,
                RETENTION_DAYS_ORDERS
            )
            if deleted_orders > 0:
                log.info(f"[MAINTENANCE] 🧹 Đã xóa {deleted_orders} đơn hàng PENDING quá hạn (> {RETENTION_DAYS_ORDERS} ngày).")

        except Exception as e:
            log.warning(f"[MAINTENANCE] Lỗi khi dọn dẹp DB: {e}")

        await asyncio.sleep(INTERVAL_SECONDS)

# ===============================================================
# SESSION NOTICE LOOP
# ===============================================================
# Thời điểm gửi thông báo trước giờ mở/đóng phiên (giờ VN)
NOTICE_SPECS = [
    {
        "label": "MORNING_OPEN",
        "hour": 9,
        "minute": 10,  # trước mở phiên sáng 5 phút (09:10)
        "text": "⏰ Phiên sáng sắp mở lúc 09:15. Bạn tranh thủ xem lại danh mục và các mức giá mục tiêu nhé.",
    },
    {
        "label": "MORNING_CLOSE",
        "hour": 11,
        "minute": 25,  # trước đóng phiên sáng 5 phút (11:25)
        "text": "🔔 Phiên sáng sắp kết thúc lúc 11:30. Bạn cân nhắc các lệnh còn treo nhé.",
    },
    {
        "label": "AFTERNOON_OPEN",
        "hour": 12,
        "minute": 55,  # trước mở phiên chiều 5 phút (12:55)
        "text": "⏰ Phiên chiều sắp mở lúc 13:00. Nhớ kiểm tra lại danh mục và chiến lược giao dịch.",
    },
    {
        "label": "AFTERNOON_CLOSE",
        "hour": 14,
        "minute": 40,  # trước đóng phiên chiều 5 phút (14:40)
        "text": "🔔 Phiên giao dịch chiều sắp kết thúc lúc 14:45... Hứa hẹn mang đến những thông tin hữu ích cho danh mục của bạn 📊",
    },
    {
        "label": "EOD_SUMMARY",
        "hour": 15,
        "minute": 00,  # Tổng kết cuối phiên
        "text": "📊 Tổng kết cuối phiên: StockBot đang gửi báo cáo danh mục của bạn...",
    },
]

def get_next_notice_after(now: datetime.datetime):
    """
    Tìm mốc thông báo tiếp theo (mở/đóng phiên) sau thời điểm 'now'
    ở các ngày làm việc kế tiếp (bỏ qua T7, CN).
    """
    vn_tz = pytz.timezone(TIMEZONE)
    candidates = []

    for offset in range(0, 7):  # tối đa nhìn trước 1 tuần là đủ
        day = now.date() + datetime.timedelta(days=offset)
        if day.weekday() > 4:  # 5 = T7, 6 = CN
            continue
        for spec in NOTICE_SPECS:
            dt = vn_tz.localize(
                datetime.datetime(day.year, day.month, day.day, spec["hour"], spec["minute"])
            )
            if dt > now:
                candidates.append((dt, spec))

    if not candidates:
        return None, None

    # lấy mốc gần nhất
    dt, spec = min(candidates, key=lambda x: x[0])
    return dt, spec

# --- HELPER FORMAT EOD ---
def fmt_price(p):
    """Giá: 26500 -> 26.500"""
    if p is None: return "--"
    return f"{int(p):,}".replace(",", ".")

def fmt_volume(v):
    """Vol: 1500000 -> 1.5M"""
    if v is None: return "--"
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000: return f"{v/1_000:.0f}K"
    return str(int(v))

def fmt_value(v):
    """Value: 150 tỷ -> 150 Tỷ"""
    if v is None or v == 0: return "--"
    if v >= 1_000_000_000: return f"{v/1_000_000_000:.0f} Tỷ"
    if v >= 1_000_000: return f"{v/1_000_000:.0f} Tr"
    return "--"

# --- FETCHER RIÊNG CHO EOD (Dùng Quote History 1D) ---
async def fetch_full_eod_data(symbols):
    """
    Lấy dữ liệu EOD đầy đủ: Giá, %, Vol, Value.
    Tự động fix lỗi đơn vị giá (x1000).
    """
    if not symbols: return []
    
    async def _get_one(sym):
        try:
            def _call_api():
                # Lấy 5 ngày gần nhất để chắc chắn có dữ liệu
                today = datetime.datetime.now()
                start_d = (today - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
                end_d = today.strftime('%Y-%m-%d')
                
                quote = Quote(symbol=sym, source='VCI')
                return quote.history(start=start_d, end=end_d, interval='1D')

            df = await asyncio.to_thread(_call_api)
            
            if df is None or df.empty: return None

            # Lấy dòng mới nhất
            last = df.iloc[-1]
            close = float(last['close'])
            volume = float(last['volume'])
            
            # [FIX QUAN TRỌNG] Nếu giá < 500 (tức là đơn vị nghìn), nhân 1000
            if close < 500:
                close *= 1000

            # Tính giá trị ước tính
            trading_val = close * volume

            # Tính % thay đổi
            pct = 0.0
            if len(df) >= 2:
                prev = float(df.iloc[-2]['close'])
                if prev < 500: prev *= 1000 # Fix cả giá tham chiếu
                
                if prev > 0:
                    pct = ((close - prev) / prev) * 100

            # Màu sắc
            bg_cls = "bg-ref"
            text_cls = "t-ref"
            sign = ""
            
            if pct > 0:
                bg_cls = "bg-up"; text_cls = "t-up"; sign = "+"
            elif pct < 0:
                bg_cls = "bg-down"; text_cls = "t-down"

            return {
                "symbol": sym,
                "price": fmt_price(close),
                "pct": f"{sign}{pct:.2f}",
                "vol_str": fmt_volume(volume),
                "val_str": fmt_value(trading_val),
                "bg_cls": bg_cls,
                "text_cls": text_cls
            }
        except Exception as e:
            log.warning(f"[EOD_FETCH] Lỗi mã {sym}: {e}")
            return None

    # Chạy song song tất cả các mã
    tasks = [_get_one(s) for s in symbols]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]


async def send_eod_summary_worker():
    """
    [WORKER] Xử lý nặng: Lấy giá EOD, Vẽ Chart, Gọi AI -> Bắn sang Gateway.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_str = now.strftime("%d/%m/%Y")
    
    log.info(f"[{INSTANCE_ID}][EOD] 🚀 Bắt đầu tính toán EOD Summary...")

    # 1. VẼ CHART VNINDEX (Song song)
    task_vni = generate_mini_chart("VNINDEX")
    task_v30 = generate_mini_chart("VN30")
    
    # --- 2. LẤY DỮ LIỆU INDEX ---
    def _get_index_data(symbol):
        try:
            q = Quote(symbol=symbol, source='VCI')
            df = q.history(start=(now - datetime.timedelta(days=5)).strftime('%Y-%m-%d'), 
                           end=now.strftime('%Y-%m-%d'), interval='1D')
            if df is None or len(df) < 2: return None
            
            last = df.iloc[-1]
            prev = df.iloc[-2]
            
            price = float(last['close'])
            change = price - float(prev['close'])
            pct = (change / float(prev['close'])) * 100
            
            cls = "t-ref"
            if change > 0: cls = "t-up"
            elif change < 0: cls = "t-down"
            sign = "+" if change > 0 else ""
            
            return {
                "price": f"{price:,.2f}",
                "change_str": f"{sign}{change:,.2f} ({sign}{pct:.2f}%)",
                "cls": cls,
                "raw_price": price, "raw_change": change, "raw_vol": float(last['volume'])
            }
        except Exception: return None

    # Chạy song song lấy số liệu Index
    vni_data, v30_data = await asyncio.gather(
        asyncio.to_thread(_get_index_data, 'VNINDEX'),
        asyncio.to_thread(_get_index_data, 'VN30')
    )

    # Chờ chart xong
    chart_vni, chart_v30 = await asyncio.gather(task_vni, task_v30)
    
    market_data = {
        "vnindex": {**vni_data, "chart_html": chart_vni},
        "vn30": {**v30_data, "chart_html": chart_v30},
        "ai_comment": "Thị trường hôm nay..." # Gọi AI ở đây nếu muốn
    }

    # 3. LẤY DANH SÁCH USER
    all_watch = await asyncio.to_thread(get_all_watch)
    if not all_watch: return

    # Gom tất cả mã cần lấy dữ liệu
    all_symbols = set()
    for blk in all_watch.values():
        for s in blk.get("list", []): all_symbols.add(str(s).upper())
    

    stock_data_list = await fetch_full_eod_data(list(all_symbols))
    stock_map = {item['symbol']: item for item in stock_data_list}

    # 4. TẠO GÓI TIN VÀ GỬI
    count = 0
    for chat_key, block in all_watch.items():
        try:
            chat_id = int(chat_key)
            watch_list = block.get("list", [])
            if not watch_list: continue

            # Lọc cổ phiếu của user
            user_stocks = [stock_map[s] for s in watch_list if s in stock_map]
            
            # Tạo Payload Web App
            payload = {
                "market_data": market_data,
                "user_stocks": user_stocks,
                "generated_at": now.strftime("%H:%M %d/%m"),
                "is_pro": True 
            }
            
            # Lưu Redis
            digest_id = uuid.uuid4().hex
            r_client.set(f"eod_web:{digest_id}", json.dumps(payload), ex=86400)
            
            web_url = f"{BASE_URL}/eod/{digest_id}"
            kb = {
                "inline_keyboard": [[
                    {"text": "📊 Xem Tổng Kết & Biểu Đồ", "web_app": {"url": web_url}}
                ]]
            }
            
            msg_text = (
                f"🇻🇳 *Tổng kết phiên {today_str}*\n"
                f"VN-INDEX: {vni_data['price']} {vni_data['change_str']}\n"
                f"👉 Nhấn nút để xem chi tiết."
            )

            # 🔥 GỬI LỆNH EOD_SUMMARY (Để Gateway ghim)
            push_telegram_msg(
                chat_id=chat_id,
                text=msg_text,
                reply_markup=kb,
                msg_type="EOD_SUMMARY" # <--- Gateway sẽ nhận ra loại này để GHIM
            )
            count += 1

        except Exception as e:
            log.error(f"EOD User Error: {e}")

    log.info(f"[{INSTANCE_ID}][EOD] ✅ Đã đẩy {count} bản tin EOD sang Gateway.")

    # Chờ 10 giây cho Gateway gửi hết tin EOD rồi mới ra lệnh dọn dẹp
    await asyncio.sleep(10)
    
    # Bắn tín hiệu đặc biệt (chat_id=0 vì đây là lệnh hệ thống, không gửi cho user nào)
    push_telegram_msg(
        chat_id=0, 
        text="SYSTEM_COMMAND", 
        msg_type="TRIGGER_CLEANUP"
    )
    log.info(f"[{INSTANCE_ID}][EOD] 🧹 Đã gửi lệnh TRIGGER_CLEANUP sang Gateway.")


async def session_notice_loop():
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Session Notice Loop...")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # Check Bot Active
        if not get_bot_active():
            await asyncio.sleep(60); continue

        next_dt, spec = get_next_notice_after(now) # Hàm helper đã copy sang
        if not next_dt:
            await asyncio.sleep(3600); continue

        # Ngủ tới giờ G
        delay = max((next_dt - now).total_seconds(), 1)
        log.info(f"[SESSION] Ngủ {delay:.0f}s tới {spec['label']} ({next_dt.strftime('%H:%M')})")
        await asyncio.sleep(delay)

        if not get_bot_active(): continue

        # XỬ LÝ GỬI TIN
        try:
            label = spec.get("label")
            
            # Case 1: EOD (Nặng đô)
            if label == "EOD_SUMMARY":
                await send_eod_summary_worker()
            
            # Case 2: Thông báo thường (Nhẹ nhàng)
            else:
                text = spec["text"]
                all_watch = await asyncio.to_thread(get_all_watch)
                count = 0
                for chat_key in all_watch.keys():
                    try:
                        chat_id = int(chat_key)
                        # Bắn tin thường
                        push_telegram_msg(chat_id, text, msg_type="SESSION_NOTICE")
                        count += 1
                    except: pass
                log.info(f"[SESSION] Đã bắn thông báo '{label}' cho {count} user.")

        except Exception as e:
            log.error(f"Session Loop Error: {e}")
            await asyncio.sleep(60)

# =====================================================
# WEEKLY_REPORT_LOOP
# =====================================================

async def get_financial_context_string(symbols: list[str]) -> str:
    """
    [MỚI] Tạo dữ liệu đầu vào cho AI dựa trên chiến lược Mean Reversion.
    Kết hợp: Screener API Realtime + Redis Cache Lịch sử 5 năm.
    """
    if not symbols: return ""

    # 1. Lấy dữ liệu Lịch sử (Redis)
    hist_data = await asyncio.to_thread(get_historical_valuation_from_redis) or {}
    
    # 2. Lấy dữ liệu Hiện tại (Snapshot Screener)
    try:
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX,UPCOM"}, limit=1700))
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] Lỗi lấy Screener context: {e}")
        screener_df = None

    lines = ["\n🔍 **DỮ LIỆU TÀI CHÍNH (REALTIME vs TRUNG BÌNH 5 NĂM):**"]
    lines.append("(Sử dụng số liệu dưới đây để đánh giá Đắt/Rẻ theo Mean Reversion)")
    lines.append("-" * 60)

    # Map dữ liệu để tra cứu nhanh
    curr_map = {}
    if screener_df is not None and not screener_df.empty:
        screener_df['ticker'] = screener_df['ticker'].astype(str).str.upper().str.strip()
        curr_map = screener_df.set_index('ticker').to_dict('index')

    for sym in symbols:
        sym_u = sym.upper()
        lines.append(f"📌 **{sym_u}**:")
        
        curr = curr_map.get(sym_u, {})
        hist = hist_data.get(sym_u, {})
        
        # Chỉ số
        pe_cur = float(curr.get('pe', 0) or 0)
        pb_cur = float(curr.get('pb', 0) or 0)
        pe_avg = float(hist.get('pe_avg', 0) or 0)
        pb_avg = float(hist.get('pb_avg', 0) or 0)

        info = []
        if pe_cur > 0 and pe_avg > 0:
            diff = (pe_cur - pe_avg) / pe_avg * 100
            state = "RẺ HƠN" if diff < 0 else "ĐẮT HƠN"
            info.append(f"   - P/E: {pe_cur:.1f}x (TB 5 năm: {pe_avg:.1f}x) -> {state} {abs(diff):.1f}%")
        
        if pb_cur > 0 and pb_avg > 0:
            diff = (pb_cur - pb_avg) / pb_avg * 100
            state = "RẺ HƠN" if diff < 0 else "ĐẮT HƠN"
            info.append(f"   - P/B: {pb_cur:.1f}x (TB 5 năm: {pb_avg:.1f}x) -> {state} {abs(diff):.1f}%")

        if not info:
            lines.append("   ⚠️ (Thiếu dữ liệu lịch sử hoặc lỗ)")
        else:
            lines.extend(info)

    lines.append("-" * 60)
    return "\n".join(lines)

def call_chatgpt_for_report(symbols: list[str]) -> str:
    """
    (PHIÊN BẢN MỚI - MEAN REVERSION CONTEXT)
    Gọi Gemini tạo báo cáo danh mục với dữ liệu so sánh lịch sử.
    """
    if not GEMINI_KEYS:
        raise RuntimeError("GEMINI_API_KEY chưa được cấu hình.")

    # Giới hạn số mã để đảm bảo tốc độ
    if len(symbols) > 6:
        symbols = symbols[:6]

    # --- 1. TẠO CONTEXT (Dùng hàm async mới viết ở trên) ---
    # Vì hàm call_chatgpt_for_report thường được gọi trong to_thread (sync wrapper),
    # nên ta cần chạy hàm async này trong event loop hiện tại hoặc loop mới.
    
    try:
        # Cách gọi async function từ trong hàm sync (chạy trong thread)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        context_str = loop.run_until_complete(get_financial_context_string(symbols))
        loop.close()
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi tạo context tài chính cho AI: {e}")
        context_str = ""

    # --- 2. CHUẨN BỊ PROMPT ---
    symbols_str = ", ".join(symbols)
    vn_tz = pytz.timezone(TIMEZONE)
    date_str = datetime.datetime.now(vn_tz).strftime('%d/%m/%Y')

    prompt = f"""
Bạn là chuyên gia phân tích chứng khoán Việt Nam theo trường phái **Mean Reversion** (Đầu tư giá trị dựa trên sự đảo chiều về trung bình).
Hãy phân tích danh mục đầu tư: {symbols_str} (Ngày báo cáo: {date_str}).

{context_str}

YÊU CẦU FORMAT OUTPUT (JSON THUẦN):
{{
  "general_market_comment": "Đoạn văn (khoảng 3-4 câu) tổng quan về thị trường và định hướng danh mục.",
  "portfolio_health_score": 8.5, 
  "stocks": [
    {{
      "symbol": "MÃ",
      "industry": "Tên ngành",
      "action": "Mua / Nắm giữ / Hạ tỷ trọng / Theo dõi",
      "analysis": "Phân tích chi tiết (1000-1200 ký tự). BẮT BUỘC trình bày thành các ý gạch đầu dòng (•), mỗi ý xuống dòng (\\n) riêng biệt. Nội dung phải bao hàm các khía cạnh sau:\\n• KQKD & Lợi thế: Tăng trưởng doanh thu/LN, thị phần, biên lợi nhuận...\\n• Động lực (Catalyst): Dự án mới, game M&A, chính sách ủng hộ...\\n• Định giá: So sánh P/E, P/B với trung bình ngành (đã cung cấp ở trên) để kết luận Đắt/Rẻ.\\n• Rủi ro: Pháp lý, tỷ giá, chi phí đầu vào...",
      "key_metrics": "Tóm tắt chỉ số (VD: P/E 10.x (TB 15.x)"
    }}
  ]
}}

LƯU Ý:
1. **QUAN TRỌNG:** Nếu P/E hoặc P/B thấp hơn trung bình 5 năm > 10%, hãy coi đó là tín hiệu tích cực (Rẻ). Ngược lại là rủi ro (Đắt).
2. Trường `analysis` là MỘT CHUỖI VĂN BẢN (String) chứa các ký tự xuống dòng (\\n) để tách ý, KHÔNG được là object JSON.
3. Giọng văn: Khách quan, sắc sảo, dựa trên số liệu.
4. Tuyệt đối trung thực với số liệu đã cung cấp trong phần 'DỮ LIỆU TÀI CHÍNH'.

LUẬT NGHIÊM NGẶT VỀ JSON (STRICT RULE):
1. Trả về đúng định dạng JSON chuẩn (RFC 8259).
2. KHÔNG được có dấu phẩy (,) ở phần tử cuối cùng của danh sách hoặc object (Trailing comma prohibited).
3. Nếu trong nội dung văn bản có dấu ngoặc kép ("), HÃY thay thế bằng dấu nháy đơn (') hoặc escape nó (\").
4. KHÔNG thêm bất kỳ lời dẫn hay giải thích nào ngoài khối JSON.
"""

    log.info(f"[{INSTANCE_ID}] Gọi Gemini (Report Mean Reversion): {symbols_str}")

    try:
        raw_text = call_gemini_safe(
            model_id="gemini-2.5-flash",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        # Làm sạch mạnh bằng Regex
        clean_text = extract_json_from_text(raw_text)
        try:
            json.loads(clean_text)
        except Exception as json_err:
            log.error(f"❌ JSON LỖI CÚ PHÁP:\n{clean_text}") # In ra để copy ném vào jsonlint.com kiểm tra
            raise json_err
        return clean_text
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi Gemini Report: {e}")
        raise e

async def process_report_for_user(chat_id, symbols, source="on_demand", loading_msg_id=None):
    """
    Hàm chung: Gọi AI -> Lưu Cache -> Bắn tin về Gateway.
    """
    try:
        # 1. Kiểm tra/Cắt ngắn danh sách
        if len(symbols) > 6: symbols = symbols[:6]
        cache_key = make_report_cache_key(symbols)

        # 2. Gọi AI (Nặng)
        json_text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
        
        # 3. Lưu Cache
        save_report_to_redis(cache_key, json_text, source=source)

        # 4. Chuẩn bị Web App Data
        # (Logic này bạn copy từ cmd_report cũ để parse JSON ra object data)
        data = json.loads(json_text) 
        
        # Tạo link Web App
        digest_id = uuid.uuid4().hex
        # Lưu digest data cho Web App (TTL 24h)
        # Lưu ý: data của report AI cần được bọc lại để web app hiểu
        webapp_payload = {
            "portfolio_health_score": data.get("portfolio_health_score"),
            "general_market_comment": data.get("general_market_comment"),
            "stocks": data.get("stocks"),
            "is_pro": True
        }
        r_client.set(f"digest_web:report:{digest_id}", json.dumps(webapp_payload), ex=86400)
        
        web_url = f"{BASE_URL}/report/view/{cache_key}?chat_id={chat_id}" # Hoặc dùng digest_id nếu bạn sửa route webapp
        
        kb = {
            "inline_keyboard": [
                [{"text": "📊 Xem Báo Cáo Chi Tiết", "web_app": {"url": web_url}}],
                [{"text": "❌ Đóng", "callback_data": "close_msg"}]
            ]
        }
        
        # 5. Bắn kết quả về Gateway
        # Dùng msg_type="REPORT_RESULT" để Gateway biết mà xử lý (ví dụ xóa msg loading)
        push_telegram_msg(
            chat_id=chat_id,
            text=f"🚀 **Phân tích hoàn tất!**\nĐã xử lý xong danh mục: *{', '.join(symbols)}*",
            reply_markup=kb,
            msg_type="REPORT_RESULT",
            edit_id=loading_msg_id
        )
        
    except Exception as e:
        log.error(f"Report Process Error for {chat_id}: {e}")
        # Bắn tin lỗi về
        push_telegram_msg(chat_id, "⚠️ Lỗi khi tạo báo cáo. Vui lòng thử lại.", msg_type="GENERAL")

def seconds_until_next_weekly_report():
    """
    Tính số giây tới 09:00 sáng Chủ Nhật gần nhất (report tuần).
    - Nếu hôm nay là Chủ Nhật và giờ < 09:00 -> lấy 09:00 hôm nay.
    - Ngược lại -> 09:00 Chủ Nhật tuần kế tiếp.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)

    # Chủ Nhật = 6 trong weekday()
    SUNDAY = 6
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)

    if now.weekday() != SUNDAY or now >= target:
        # Tìm Chủ Nhật kế tiếp
        days_ahead = (SUNDAY - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        next_date = now.date() + datetime.timedelta(days=days_ahead)
        target = datetime.datetime(
            next_date.year, next_date.month, next_date.day, 9, 0, 0, tzinfo=vn_tz
        )

    return max((target - now).total_seconds(), 0)

async def execute_weekly_batch(requester_id=None):
    """
    [WORKER] Hàm thực thi logic gửi báo cáo tuần cho toàn bộ Pro User.
    Có thể được gọi bởi:
    1. weekly_report_loop (Tự động sáng CN)
    2. Lệnh Admin (Thủ công)
    """
    log.info(f"[{INSTANCE_ID}][WEEKLY_BATCH] 🚀 Bắt đầu chạy batch...")
    
    if requester_id:
        push_telegram_msg(requester_id, "⏳ Worker đã nhận lệnh. Đang bắt đầu gửi hàng loạt...", msg_type="SYSTEM_MSG")

    try:
        all_watch = await asyncio.to_thread(get_all_watch)
        pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
        
        count = 0
        skipped = 0
        
        for chat_key, block in all_watch.items():
            try:
                chat_id = int(chat_key)
                # Chỉ gửi cho Pro hoặc Admin
                if chat_id not in pro_chat_ids and chat_id != ADMIN_ID: 
                    skipped += 1
                    continue
                
                watch_list = block.get("list", [])
                symbols = [s.upper() for s in watch_list if not s.upper().startswith("VN")]
                
                if symbols:
                    # Gọi hàm xử lý chung (Process & Push to Gateway)
                    # source="weekly_loop" để ghi log cache đúng nguồn
                    await process_report_for_user(chat_id, symbols, source="weekly_loop")
                    count += 1
                    
                await asyncio.sleep(2) # Rate limit nhẹ
                
            except Exception as e:
                log.error(f"Weekly Batch Error {chat_key}: {e}")

        finish_msg = f"✅ **Hoàn tất Weekly Batch!**\n- Đã gửi: {count}\n- Bỏ qua (Free): {skipped}"
        log.info(f"[{INSTANCE_ID}] {finish_msg}")

        # Nếu là Admin chạy thủ công, báo cáo kết quả về
        if requester_id:
            push_telegram_msg(requester_id, finish_msg, msg_type="SYSTEM_MSG")

    except Exception as e:
        log.error(f"Weekly Batch Critical: {e}")
        if requester_id:
            push_telegram_msg(requester_id, f"❌ Lỗi Batch: {e}", msg_type="SYSTEM_MSG")

async def weekly_report_loop():
    """
    [WORKER] Loop canh giờ Chủ Nhật.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    REDIS_KEY_LAST_RUN = "worker_state:weekly_report_last_run"
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Weekly Scheduler...")

    while True:
        # 1. Tính thời gian ngủ tới 09:00 CN tiếp theo
        # (Giả sử bạn đã copy hàm seconds_until_next_weekly_report sang worker)
        wait_sec = seconds_until_next_weekly_report()
        
        # Log nếu thời gian chờ > 5 phút
        if wait_sec > 300:
            next_run_dt = datetime.datetime.now(vn_tz) + datetime.timedelta(seconds=wait_sec)
            log.info(f"[WEEKLY] Ngủ {wait_sec/3600:.1f}h tới {next_run_dt.strftime('%H:%M %d/%m')}")
        
        await asyncio.sleep(wait_sec)

        # 2. Thức dậy! Kiểm tra điều kiện
        if not get_bot_active():
            log.info("[WEEKLY] Thức dậy nhưng Bot TẮT. Ngủ tiếp 60s.")
            await asyncio.sleep(60)
            continue
        
        today_str = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")
        last_run = r_client.get(REDIS_KEY_LAST_RUN)
        if last_run == today_str:
             await asyncio.sleep(3600); continue

        # 🔥 GỌI HÀM BATCH MỚI
        await execute_weekly_batch(requester_id=None) # None vì là tự động

        # Lưu trạng thái
        r_client.set(REDIS_KEY_LAST_RUN, today_str, ex=86400 * 6)

# =====================================================
# INFO
# =====================================================

def call_gemini_for_profile(symbol: str) -> str:
    """
    (PHIÊN BẢN WORKER) Gọi Gemini tạo hồ sơ doanh nghiệp.
    """
    sym = symbol.upper().strip()
    log.info(f"[{INSTANCE_ID}] Gọi Gemini (Profile): {sym}")

    prompt = f"""
Bạn là chuyên gia phân tích doanh nghiệp tại thị trường chứng khoán Việt Nam.
Hãy tạo một "Hồ sơ Doanh nghiệp" chi tiết cho mã cổ phiếu: {sym}

YÊU CẦU FORMAT:
Trả về **JSON thuần** (RFC 8259). Cấu trúc bắt buộc gồm các keys sau:
{{
  "overview": "...",
  "products": "...",
  "business_model": "...",
  "market_position": "...",
  "value_chain": "...",
  "moat": "...",
  "risks": "...",
  "leadership": "..."
}}

YÊU CẦU NỘI DUNG:
- Các trường nội dung phải trình bày gãy gọn, dùng ký tự xuống dòng (\\n) và gạch đầu dòng (•) để tách ý.
- Giọng văn khách quan, KHÔNG khuyến nghị mua bán.
- JSON không được chứa lỗi cú pháp (trailing comma, unescaped quotes).
"""

    try:
        raw_text = call_gemini_safe(
            model_id="gemini-2.5-flash", # Dùng Flash cho nhanh
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        
        # Làm sạch JSON bằng hàm Regex đã có
        return extract_json_from_text(raw_text)

    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi Gemini Profile: {e}")
        raise e

# Trong worker.py (Gần chỗ process_report_for_user)

from profile_cache import make_profile_cache_key, save_profile_to_redis # Nhớ import

async def process_profile_for_user(chat_id, symbol, loading_msg_id=None):
    """
    Xử lý logic Hồ sơ doanh nghiệp: Gọi AI -> Cache -> Trả kết quả.
    """
    try:
        sym = symbol.upper()
        cache_key = make_profile_cache_key(sym)

        # 1. Gọi AI
        json_text = await asyncio.to_thread(call_gemini_for_profile, sym)
        
        # 2. Lưu Cache
        save_profile_to_redis(cache_key, json_text, source="on_demand")

        # 3. Tạo Web App Link
        web_url = f"{BASE_URL}/info/{sym}?chat_id={chat_id}"
        
        # 4. Tạo Nút Bấm (Dọc)
        kb = {
            "inline_keyboard": [
                [{"text": f"📄 Mở Hồ Sơ {sym}", "web_app": {"url": web_url}}],
                [{"text": "❌ Đóng", "callback_data": "close_msg"}]
            ]
        }
        
        # 5. Gửi kết quả (SỬA tin nhắn loading cũ)
        push_telegram_msg(
            chat_id=chat_id,
            text=f"✅ **Hồ sơ doanh nghiệp: {sym}**\nĐã phân tích xong mô hình kinh doanh & vị thế.",
            reply_markup=kb,
            msg_type="INFO_RESULT",
            edit_id=loading_msg_id # <--- Quan trọng để sửa tin nhắn cũ
        )
        
        log.info(f"[{INSTANCE_ID}] Đã gửi Profile {sym} cho {chat_id}")

    except Exception as e:
        log.error(f"Profile Process Error: {e}")
        # Báo lỗi cho user (Sửa tin loading thành báo lỗi)
        push_telegram_msg(
            chat_id=chat_id,
            text=f"⚠️ Không thể lấy hồ sơ mã {symbol}. Vui lòng thử lại sau.",
            msg_type="ERROR",
            edit_id=loading_msg_id
        )

# =====================================================
# SCREENER VALUE
# =====================================================

async def process_screener_view(chat_id, loading_msg_id=None):
    """
    [WORKER] Xử lý lệnh /screener_value:
    1. Lấy History Data (Redis).
    2. Lấy Current Data (API).
    3. Tính toán & Tạo Web App.
    """
    try:
        # 1. Lấy dữ liệu lịch sử (Mean Reversion)
        hist_data = get_historical_valuation_from_redis()
        
        # Nếu chưa có dữ liệu lịch sử -> Báo user chờ hoặc chạy tính toán ngay (tùy chọn)
        # Ở đây ta chọn giải pháp an toàn: Báo lỗi để Admin chạy lại job đêm
        if not hist_data:
            push_telegram_msg(
                chat_id=chat_id,
                text="⚠️ Dữ liệu định giá lịch sử chưa sẵn sàng. Vui lòng thử lại sau hoặc báo Admin.",
                msg_type="ERROR",
                edit_id=loading_msg_id
            )
            return

        # 2. Lấy dữ liệu thị trường hiện tại (Screener)
        # Chạy trong thread để không block
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
        # 3. Tính toán so sánh (Logic Mean Reversion)
        processed_items = []
        for index, row in screener_df.iterrows():
            sym = row['ticker']
            if sym not in hist_data: continue
            
            try:
                pe_cur = float(row['pe'])
                pb_cur = float(row['pb'])
            except: continue

            pe_avg = hist_data[sym]['pe_avg']
            pb_avg = hist_data[sym]['pb_avg']
            
            if pe_cur <= 0 or pb_cur <= 0: continue
            if pe_avg <= 0 or pb_avg <= 0: continue

            # Tính % Discount
            pe_discount = (pe_cur - pe_avg) / pe_avg
            pb_discount = (pb_cur - pb_avg) / pb_avg
            avg_discount = (pe_discount + pb_discount) / 2
            
            # Helper UI (Copy lại logic cũ để tạo class màu sắc)
            def get_ui_meta(discount):
                pct_val = abs(discount) * 100
                if discount < -0.1: return "diff-good", f"▼ {pct_val:.1f}%"
                elif discount > 0.1: return "diff-bad", f"▲ {pct_val:.1f}%"
                else: return "", f"{'▲' if discount>0 else '▼'} {pct_val:.1f}%"

            pe_class, pe_diff_str = get_ui_meta(pe_discount)
            pb_class, pb_diff_str = get_ui_meta(pb_discount)
            
            if avg_discount < -0.1: signal_class, signal_text = "sig-cheap", "Định giá Rẻ"
            elif avg_discount > 0.1: signal_class, signal_text = "sig-expensive", "Đắt"
            else: signal_class, signal_text = "sig-fair", "Hợp lý"

            processed_items.append({
                'symbol': sym,
                'pe_cur': pe_cur, 'pe_avg': pe_avg,
                'pe_class': pe_class, 'pe_diff_str': pe_diff_str,
                'pb_cur': pb_cur, 'pb_avg': pb_avg,
                'pb_class': pb_class, 'pb_diff_str': pb_diff_str,
                'signal_class': signal_class, 'signal_text': signal_text,
                'avg_discount': avg_discount
            })

        # 4. Sắp xếp & Lưu Cache Web App
        processed_items.sort(key=lambda x: x['avg_discount'])
        top_items = processed_items[:50] # Top 50 mã rẻ nhất

        digest_id = uuid.uuid4().hex
        vn_tz = pytz.timezone(TIMEZONE)
        payload = {
            "items": top_items,
            "generated_time": datetime.datetime.now(vn_tz).strftime("%H:%M %d/%m/%Y")
        }
        
        # Lưu vào Redis cho Web App đọc (TTL 1 giờ)
        r_client.set(f"digest_web:screener_val:{digest_id}", json.dumps(payload), ex=3600)

        # 5. Gửi kết quả về Gateway (Nút dọc)
        web_url = f"{BASE_URL}/screener_result/{digest_id}?chat_id={chat_id}"
        
        kb = {
            "inline_keyboard": [
                [{"text": "🚀 Xem Bảng Xếp Hạng", "web_app": {"url": web_url}}],
                [{"text": "❌ Đóng", "callback_data": "close_msg"}]
            ]
        }

        push_telegram_msg(
            chat_id=chat_id,
            text=f"💎 **Định Giá Cổ Phiếu (Mean Reversion)**\n\n✅ Đã lọc được {len(processed_items)} mã tiềm năng.\n👉 Nhấn nút để xem chi tiết.",
            reply_markup=kb,
            msg_type="SCREENER_RESULT",
            edit_id=loading_msg_id # <--- Sửa tin nhắn Loading
        )
        
        log.info(f"[{INSTANCE_ID}] Screener calculated for {chat_id}")

    except Exception as e:
        log.error(f"Screener Error: {e}")
        push_telegram_msg(chat_id, "⚠️ Lỗi hệ thống khi lọc cổ phiếu.", msg_type="ERROR", edit_id=loading_msg_id)

async def process_force_update_screener(admin_id):
    """
    [WORKER] Chạy lại tính toán lịch sử (Task nặng).
    """
    log.info(f"[{INSTANCE_ID}] Admin {admin_id} requested FORCE UPDATE SCREENER.")
    push_telegram_msg(admin_id, "⏳ Worker đang tính toán lại dữ liệu định giá (mất khoảng 5-10 phút)...", msg_type="SYSTEM_MSG")
    
    try:
        start = time.time()
        await calculate_historical_valuation_task() # Hàm này đã có ở worker.py từ bước trước
        duration = time.time() - start
        
        push_telegram_msg(admin_id, f"✅ **Hoàn tất cập nhật Screener!**\n⏱ Thời gian: {duration/60:.1f} phút.", msg_type="SYSTEM_MSG")
        
    except Exception as e:
        log.error(f"Force Update Error: {e}")
        push_telegram_msg(admin_id, f"❌ Lỗi cập nhật: {e}", msg_type="ERROR")

# =========== MAIN ENTRY POINT ============
async def main():
    log.info(f"[{INSTANCE_ID}] Worker starting...")
    
    # Chạy song song 2 loop
    await asyncio.gather(
        stock_price_fetcher_loop(),
        alert_loop(),
        #----------------------------
        vn30f1m_price_fetcher_loop(),
        vn30f1m_alert_loop(),
        #-------------------------
        daily_user_digest_loop(),
        nightly_valuation_loop(),
        news_specialized_loop(),
        news_macro_loop(),
        analysis_report_loop(),
        financial_Statements_notice_loop(),
        news_cleanup_loop(),
        #-------------------------
        session_notice_loop(),
        #-------------------------
        restore_reminder_loop(),
        #-------------------------
        worker_inbound_loop(),
        weekly_report_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped.")