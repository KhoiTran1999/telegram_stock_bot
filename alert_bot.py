import os
import json
import random
import datetime
import asyncio
import pytz
import requests
import pandas as pd
pd.set_option('future.no_silent_downcasting', True)
import math
import uuid
import logging
import tempfile
from dotenv import load_dotenv
load_dotenv(override=True)
from digest_template import (
    DIGEST_HTML_TEMPLATE,
    DIGEST_404_TEMPLATE,
    PROFILE_HTML_TEMPLATE,
    PROFILE_404_TEMPLATE,
    REPORT_HTML_TEMPLATE,
    REPORT_404_TEMPLATE,
    SCREENER_HTML_TEMPLATE,
    LOCKED_FEATURE_TEMPLATE,
    EOD_HTML_TEMPLATE, 
    EOD_404_TEMPLATE,
    FLASH_VIEW_HTML_TEMPLATE,
    ADMIN_MOBILE_TEMPLATE,
)
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Update, WebAppInfo, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from telegram.constants import ChatAction
import telegram
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler
)
from telegram.request import HTTPXRequest
from flask import Flask, request, jsonify, render_template_string, Response
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi
from vnstock import Trading, Quote, Listing, Finance, Company, Screener
from db_utils import (
    init_db,
    get_all_watch,
    get_watch_list_for_chat,
    save_watch_list_for_chat,
    get_bot_active,
    set_bot_active,
    log_command_usage,
    get_command_stats,
    cleanup_old_pending_orders,
    save_bot_message,
    get_bot_messages_in_range,
    delete_bot_messages_in_range,
    load_stock_value_cache,
    has_news_seen,
    mark_news_seen,         
    has_bctc_notified,
    mark_bctc_notified,
    export_core_data,
    import_core_data,
    get_last_restore_month,
    get_conn,
    add_paid_user,
    is_user_pro,
    deactivate_paid_user,
    remove_paid_user,
    get_all_pro_chat_ids,
    cleanup_old_news_seen,
    get_user_pro_expiry,
    has_report_seen,
    mark_report_seen,
    get_recent_bctc_notified,
    get_recent_analysis_reports,
    get_recent_news_seen,
    create_pending_order,
    get_order_by_id,
    mark_order_as_paid,
    save_bot_message,
    get_messages_to_cleanup,
    delete_bot_log_record,
    get_latest_bot_message_id,
    save_historical_valuation_to_redis,
    get_historical_valuation_from_redis,
    upsert_user_info,
    get_admin_dashboard_data,
    get_user_orders,
    get_user_logs,
    get_user_configs,
    get_vn30f1m_enabled_map,
    set_vn30f1m_enabled,
    get_stock_alert_enabled_map,
    set_stock_alert_enabled,
    get_total_revenue_real,
    update_user_admin_note,
    get_banned_users,
    set_user_ban_status,
    check_trial_eligibility,
    activate_trial_package,
)
import psutil
import time
import subprocess
import re
import html
import csv
from telegram.error import BadRequest
from typing import Any, Optional
import html
import feedparser
from telegram.error import TelegramError
from urllib.parse import quote_plus
from news_seen_cache import (
    get_redis
)
from report_cache import (
    make_report_cache_key,
    get_report_from_redis,
    save_report_to_redis,
    delete_report_from_redis,
)
from profile_cache import (
    make_profile_cache_key,
    get_profile_from_redis,
    save_profile_to_redis,
)
from pathlib import Path
from google import genai
import uuid
import hmac
from chart_utils import (
    get_flash_view_data,
    draw_line_chart_fixed_ui,
    draw_orderbook_fixed_ui,
    generate_chart_html,
)
from decimal import Decimal
from functools import wraps

# --- HÀM HELPER VẼ THANH TIẾN TRÌNH ---
def make_progress_bar(percent: int, width: int = 8) -> str:
    """Tạo thanh loading dạng text: ▰▰▰▱▱"""
    filled = int(width * percent / 100)
    empty = width - filled
    return "▰" * filled + "▱" * empty

# ANTI-SPAM & LOCKING
_user_last_action_time = {}
_processing_users = set()
SPAM_COOLDOWN = 1.5  # Giây

def is_user_spamming(user_id: int) -> bool:
    """
    Kiểm tra spam. 
    Chỉ update thời gian nếu hành động được chấp nhận.
    """
    now = time.time()
    last_time = _user_last_action_time.get(user_id, 0)
    
    # Nếu chưa hết thời gian chờ -> TRẢ VỀ TRUE (SPAM)
    # 🔥 QUAN TRỌNG: KHÔNG được cập nhật _user_last_action_time ở đây!
    if now - last_time < SPAM_COOLDOWN:
        return True
    
    # Nếu đã hết thời gian chờ -> Cập nhật thời gian mới -> TRẢ VỀ FALSE (OK)
    _user_last_action_time[user_id] = now
    return False

# --- DECORATOR 1: CHỐNG SPAM & BLACKLIST (NÂNG CẤP) ---
def anti_spam_check(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_user:
            return await func(update, context, *args, **kwargs)
            
        user_id = update.effective_user.id
        
        # 1. CHECK BLACKLIST (Ưu tiên cao nhất - Im lặng tuyệt đối)
        if user_id in BANNED_CACHE:
            # Nếu muốn log debug thì mở dòng dưới, còn không thì im lặng luôn cho nhẹ
            # log.info(f"⛔ Ignored request from BANNED user: {user_id}")
            return 

        # 2. Check Spam Click (Logic cũ)
        if is_user_spamming(user_id):
            if update.callback_query:
                try: 
                    await update.callback_query.answer("⏳ Đang xử lý, bình tĩnh...", show_alert=False)
                except: pass
            return 
            
        return await func(update, context, *args, **kwargs)
    return wrapper


# --- DECORATOR 2: KHÓA TÁC VỤ NẶNG (Cho /report, /info...) ---
def task_locked(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat:
            return await func(update, context, *args, **kwargs)
            
        chat_id = update.effective_chat.id
        
        # Check Lock
        if chat_id in _processing_users:
            if update.callback_query:
                try: await update.callback_query.answer("⏳ Đang xử lý lệnh trước đó...", show_alert=True)
                except: pass
            else:
                try: await context.bot.send_message(chat_id, "⏳ Bot đang bận xử lý lệnh trước. Vui lòng đợi xong nhé!")
                except: pass
            return

        # Lock
        _processing_users.add(chat_id)
        
        try:
            return await func(update, context, *args, **kwargs)
        finally:
            # Unlock
            if chat_id in _processing_users:
                _processing_users.remove(chat_id)
    return wrapper

# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PASSENGER_PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

# 🔥 CACHE DANH SÁCH ĐEN (Lưu trong RAM để check siêu nhanh)
BANNED_CACHE = set()

# --- State trong RAM (Global)
_vn30f1m_anchor: float | None = None
_vn30f1m_ref_price: float | None = None
_vn30f1m_date: datetime.date | None = None

# Cache danh sách User BẬT tính năng
_vn30f1m_enabled_cache: set[int] = set() 
_stock_alert_enabled_cache: set[int] = set() # <--- MỚI: Cache Stock Alert

def reload_vn30f1m_enabled_cache():
    """Load lại tập chat_id đang bật nhận tin VN30 từ DB vào RAM."""
    global _vn30f1m_enabled_cache
    mp = get_vn30f1m_enabled_map()
    _vn30f1m_enabled_cache = {cid for cid, en in mp.items() if en}

def reload_stock_alert_enabled_cache():
    """Load lại tập chat_id đang bật nhận tin Stock Alert vào RAM."""
    global _stock_alert_enabled_cache
    mp = get_stock_alert_enabled_map()
    # Lưu ý: Logic ở DB là mặc định True, nên cache này sẽ chứa hầu hết user
    _stock_alert_enabled_cache = {cid for cid, en in mp.items() if en}

# 🗂 Thư mục tạm dùng chung cho backup/restore (tự động phù hợp Windows / Linux)
TMP_DIR = tempfile.gettempdir()

# 🧠 Application Telegram dùng chung cho webhook
tg_app = None
MAIN_LOOP = None

# ID phiên bản khởi động (dùng để phân biệt log khi chạy nhiều instance)
INSTANCE_ID = str(uuid.uuid4())[:8]

# 🚀 BIẾN MỚI: Dùng để giữ các tác vụ nền (loops)
BACKGROUND_TASKS = []

# Log gọn, không bị Render flush trùng
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("bot")
log.info(f"[BOOT] Instance {INSTANCE_ID} starting...")

# ==============================================
# CẤU HÌNH GEMINI MULTI-KEY (CHÍNH/PHỤ)
# ==============================================
# Lấy danh sách key từ môi trường, lọc bỏ key rỗng
_k1 = os.getenv("GEMINI_API_KEY")
_k2 = os.getenv("GEMINI_API_KEY_2")

GEMINI_KEYS = [k for k in [_k1, _k2] if k]

# Giữ biến này để tương thích với các đoạn check cũ (True nếu có ít nhất 1 key)
GEMINI_API_KEY = GEMINI_KEYS[0] if GEMINI_KEYS else None

if not GEMINI_KEYS:
    log.warning("⚠️ CHƯA CẤU HÌNH GEMINI_API_KEY nào cả! Các tính năng AI sẽ không hoạt động.")
else:
    log.info(f"[{INSTANCE_ID}] Đã cấu hình {len(GEMINI_KEYS)} API Key cho Gemini.")

# --- HÀM WRAPPER GỌI GEMINI AN TOÀN (Tự động đổi key) ---
def call_gemini_safe(model_id: str, contents: str, config: dict = None) -> str:
    """
    Hàm gọi Gemini có cơ chế Failover:
    - Thử Key 1 -> Nếu lỗi -> Thử Key 2 -> ...
    - Trả về text kết quả hoặc raise Exception nếu tất cả đều lỗi.
    """
    last_error = None
    
    for i, api_key in enumerate(GEMINI_KEYS):
        try:
            # Khởi tạo client với key hiện tại
            client = genai.Client(api_key=api_key)
            
            # Gọi API (Sync)
            resp = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config
            )
            
            text = getattr(resp, "text", "").strip()
            if not text:
                raise ValueError("Gemini trả về nội dung rỗng.")
                
            return text

        except Exception as e:
            # Che giấu key khi log để bảo mật
            masked_key = api_key[:5] + "..." + api_key[-4:]
            log.warning(f"[{INSTANCE_ID}] ⚠️ Gemini Key {i+1} ({masked_key}) gặp lỗi: {e}. Đang thử key tiếp theo...")
            last_error = e
            continue # Chuyển sang key tiếp theo trong vòng lặp
            
    # Nếu chạy hết vòng lặp mà vẫn không được
    log.error(f"[{INSTANCE_ID}] ❌ TẤT CẢ GEMINI KEYS ĐỀU THẤT BẠI.")
    raise last_error

# --- DÁN ĐÈ LÊN HÀM summarize_daily_news_with_ai CŨ ---

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

# Lấy Token bảo mật của SePay từ .env
SEPAY_TOKEN = os.getenv("SEPAY_TOKEN")
if not SEPAY_TOKEN:
    log.warning("⚠️ SEPAY_TOKEN chưa được cấu hình. Webhook sẽ KHÔNG an toàn.")

# Cấu hình gói Pro (ví dụ)
PRO_PACKAGE_AMOUNT = 99000  # 99.000 VNĐ
PRO_PACKAGE_DAYS = 30      # Cho 30 ngày
SEPAY_QR_BANK = os.getenv("SEPAY_QR_BANK")
SEPAY_QR_ACC = os.getenv("SEPAY_QR_ACC")

BOT_ACTIVE = None  # Sẽ được load từ DB trong main()

initial_active = None  # Trạng thái bot lúc khởi động (dùng trong lifespan)

_last_weekly_run_date = None

ALERT_STATE = {}

# Thời gian giãn cách giữa 2 lần báo cho cùng 1 mã (giây)
ALERT_COOLDOWN_SECONDS = 30 * 60  # 30 phút

# Ngưỡng bước biến động so với mốc gần nhất (tính theo %)
ALERT_PCT_STEP = 2.0  # Mỗi khi biến động >= 2% so với mốc gần nhất thì báo

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

# Map symbol -> list keyword (mã + tên doanh nghiệp)
COMPANY_KEYWORDS: dict[str, list[str]] = {}

# Thời gian tối đa coi bài báo là "tươi" (theo pubDate)
MAX_NEWS_AGE_DAYS = 14  # chỉ gửi bài trong 14 ngày gần nhất

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

# ==============================================
# HÀM TIỆN ÍCH
# ==============================================

async def send_md(bot: telegram.Bot, chat_id: int, text: str, msg_type: str = 'GENERAL', **kwargs):
    """
    Gửi tin nhắn Markdown an toàn (async) + Ghi log msg_type.
    """
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            **kwargs,
        )
        # 🔥 LƯU DB ASYNC (Chạy trong thread để không chặn) 🔥
        await asyncio.to_thread(save_bot_message, chat_id, msg.message_id, msg_type)
        return msg
    except BadRequest as e:
        # ... (giữ nguyên phần xử lý lỗi cũ của bạn) ...
        # Nhưng nhớ thêm save_bot_message vào chỗ retry thành công nếu cần
        pass
    except Exception as e:
        log.error(f"[Telegram Send Error] chat={chat_id}: {e}")

async def safe_edit_message(query, text, reply_markup, parse_mode="Markdown"):
    """
    Sửa tin nhắn an toàn. Nếu nội dung giống hệt cũ (Telegram báo lỗi),
    thì bỏ qua lỗi đó và coi như thành công.
    """
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except BadRequest as e:
        # Nếu lỗi là "Message is not modified" -> Bỏ qua
        if "not modified" in str(e):
            pass 
        # Nếu lỗi là "Message to edit not found" (User đã xóa tin) -> Bỏ qua
        elif "not found" in str(e):
            pass
        else:
            # Lỗi khác thì log ra để sửa
            log.warning(f"Lỗi safe_edit_message: {e}")
    except Exception as e:
        log.warning(f"Lỗi lạ safe_edit_message: {e}")

def load_industry_map_from_csv(path: str = "ssi_master_list.csv") -> dict[str, str]:
    """
    Đọc file CSV mapping ngành (crawl từ TOPI) và trả về dict:
        {symbol: industry}
    CSV mong đợi có cột: symbol, industry (hoặc industry_raw, industry).
    """
    mapping: dict[str, str] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}

            # Chuẩn hoá tên cột về lower để linh hoạt hơn
            cols = [c.lower() for c in reader.fieldnames]
            try:
                symbol_idx = cols.index("symbol")
            except ValueError:
                log.warning(f"[{INSTANCE_ID}][VALUE] CSV {path} không có cột 'symbol'.")
                return {}

            # Ưu tiên cột 'industry', nếu không có thì dùng 'industry_raw'
            industry_idx = None
            if "industry" in cols:
                industry_idx = cols.index("industry")
            elif "industry_raw" in cols:
                industry_idx = cols.index("industry_raw")
            else:
                log.warning(
                    f"[{INSTANCE_ID}][VALUE] CSV {path} không có 'industry'/'industry_raw'."
                )
                return {}

            for row in reader:
                values = list(row.values())
                sym = (values[symbol_idx] or "").strip().upper()
                ind = (values[industry_idx] or "").strip()
                if not sym or not ind:
                    continue
                mapping[sym] = ind

    except FileNotFoundError:
        log.warning(
            f"[{INSTANCE_ID}][VALUE] Không tìm thấy file {path}, fallback industry='Khác'."
        )
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi đọc CSV {path}: {e}")

    return mapping

def get_state_for_all():
    return ALERT_STATE

# --- HELPER CHO SCREENER MEAN REVERSION ---
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

def save_state_for_all(all_state):
    global ALERT_STATE
    ALERT_STATE = all_state

async def cleanup_after_eod():
    """
    Dọn dẹp tin nhắn rác sau khi kết thúc phiên.
    Xóa: STOCK_ALERT, VN30_ALERT, SESSION_NOTICE.
    """
    log.info("[CLEANUP] 🧹 Bắt đầu dọn dẹp tin nhắn phiên hôm nay...")
    
    # Các loại tin cần xóa
    TARGET_TYPES = ['STOCK_ALERT', 'VN30_ALERT', 'SESSION_NOTICE']
    
    # Lấy danh sách từ DB
    records = await asyncio.to_thread(get_messages_to_cleanup, TARGET_TYPES)
    
    if not records:
        log.info("[CLEANUP] Không có tin nhắn rác nào để xóa.")
        return

    log.info(f"[CLEANUP] Tìm thấy {len(records)} tin nhắn cần xóa.")
    
    count_deleted = 0
    for row in records:
        record_id, chat_id, msg_id = row
        
        try:
            # 1. Xóa trên Telegram
            try:
                await tg_app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except BadRequest as e:
                if "Message to delete not found" in str(e):
                    # User đã xóa rồi -> Không sao, vẫn xóa trong DB
                    pass
                else:
                    log.warning(f"[CLEANUP] Lỗi Tele xóa msg {msg_id}: {e}")
                    continue # Nếu lỗi khác (như rate limit nặng), tạm bỏ qua record này
            except Exception as e:
                log.warning(f"[CLEANUP] Lỗi lạ: {e}")
                continue

            # 2. Xóa trong DB (nếu xóa Tele thành công hoặc tin đã mất)
            await asyncio.to_thread(delete_bot_log_record, record_id)
            count_deleted += 1
            
            # 3. Rate Limit: Nghỉ 0.2s giữa các tin để tránh lỗi 429
            await asyncio.sleep(0.2)
            
        except Exception as e:
            log.error(f"[CLEANUP] Error loop item: {e}")

    log.info(f"[CLEANUP] ✅ Hoàn tất. Đã xóa {count_deleted}/{len(records)} tin nhắn.")

def in_session_vietnam() -> bool:
    """Giờ giao dịch Việt Nam: 09:15–11:30, 13:00–14:45 (T2–T6)."""
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    weekday = now.weekday()
    if weekday > 4:
        return False
    hm = now.hour * 60 + now.minute
    return (555 <= hm <= 690) or (780 <= hm <= 887)

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


def get_git_deploy_info() -> str | None:
    """
    Lấy thông tin phiên bản đang chạy từ Git/Render:
    - Branch
    - Commit hash
    - Commit message (nội dung những gì bạn đã làm)

    Trả về chuỗi Markdown, hoặc None nếu không lấy được.
    """
    # Render thường có sẵn các biến này
    commit_hash = os.getenv("RENDER_GIT_COMMIT")
    branch = os.getenv("RENDER_GIT_BRANCH")

    # Thử lấy commit message từ git log (nếu thư mục deploy còn giữ .git)
    commit_message = None
    try:
        raw = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%B"],
            stderr=subprocess.DEVNULL,
            text=False,   # ⭐ QUAN TRỌNG: lấy raw bytes
        )
        # Ép decode UTF-8, nếu có ký tự lạ thì thay bằng � cho an toàn
        commit_message = raw.decode("utf-8", errors="replace").strip() or None
    except Exception:
        # Ở môi trường production đôi khi không có .git, bỏ qua
        pass

    # Không có gì thì trả về None
    if not any([commit_hash, branch, commit_message]):
        return None

    # Làm sạch commit message để tránh lỗi Markdown
    if commit_message:
        commit_message = commit_message.replace("*", "").replace("`", "'")

    lines = ["📦 *Thông tin phiên bản đang chạy:*"]
    if branch:
        lines.append(f"• Branch: `{branch}`")
    if commit_hash:
        lines.append(f"• Commit: `{commit_hash[:7]}`")
    if commit_message:
        # Gộp nhiều dòng commit thành 1 – dễ đọc trên Telegram
        short_msg = commit_message.replace("\n", " / ")
        lines.append(f"• Nội dung commit: _{short_msg}_")

    return "\n".join(lines)


async def broadcast_to_all_watchers(text: str, target_audience: str = 'pro', msg_type: str = 'GENERAL'):
    """
    (ĐÃ SỬA) Gửi 1 thông báo tới user.
    - target_audience='pro': Chỉ gửi cho Pro User + Admin (Mặc định)
    - target_audience='all': Gửi cho TẤT CẢ user
    """

    # 1. Lấy danh sách TẤT CẢ user
    all_watch = await asyncio.to_thread(get_all_watch) 
    
    pro_chat_ids = set() # Khởi tạo set rỗng
    
    # 2. CHỈ lấy danh sách Pro nếu cần
    if target_audience == 'pro':
        pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
        log.info(f"[{INSTANCE_ID}][NOTICE] Broadcast (PRO-ONLY). Tổng users: {len(all_watch)}. Pro users: {len(pro_chat_ids)}.")
    else:
        log.info(f"[{INSTANCE_ID}][NOTICE] Broadcast (ALL USERS). Tổng users: {len(all_watch)}.")

    count = 0
    tasks = []
    for chat_key in all_watch.keys():
        try:
            chat_id = int(chat_key)
            
            # === LOGIC PAYWALL (Linh hoạt) ===
            if target_audience == 'pro':
                # Nếu target là 'pro', thì bỏ qua user thường
                if chat_id not in pro_chat_ids and chat_id != ADMIN_ID:
                    continue 
            
            # Nếu target_audience == 'all', không làm gì cả,
            # đi thẳng tới bước gửi
            # ================================

            tasks.append(send_md(tg_app.bot, chat_id, text, msg_type=msg_type))
            
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NOTICE] Lỗi chuẩn bị gửi cho {chat_key}: {e}")

    # Gửi song song
    results = await asyncio.gather(*tasks, return_exceptions=True)
    count = sum(1 for res in results if not isinstance(res, Exception))

    log.info(f"[{INSTANCE_ID}][NOTICE] Đã gửi thông báo tới {count} user (Target: {target_audience}).")

# Hàm helper để cập nhật info user
async def track_user_activity(update: Update):
    """Lưu thông tin user vào DB mỗi khi họ tương tác"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    if user:
        # Chạy trong thread để không block bot
        await asyncio.to_thread(
            upsert_user_info, 
            chat_id, 
            user.username, 
            user.full_name
        )

# ==============================================
# EOD SUMMARY MỚI (WEB APP + AI)
# ==============================================

async def call_gemini_eod_insight(market_data: dict) -> str:
    """
    Gọi Gemini nhận định thị trường cuối ngày (Multi-Key Support).
    Trả về JSON string chứa field 'ai_comment'.
    """
    if not GEMINI_KEYS:
        return "AI chưa được cấu hình."

    prompt = f"""
    Đóng vai một chuyên gia theo phương pháp Wyckoff sắc sảo và thực chiến.
    Hãy phân tích dữ liệu kết phiên hôm nay:
    {json.dumps(market_data, ensure_ascii=False)}

    Yêu cầu nội dung (Tối đa 100-120 từ):
    1. 🎯 Bắt mạch thị trường: Nhận định xu hướng dựa trên sự tăng giảm Điểm số và Khối lượng (Volume).
       - Giá tăng + Vol tăng => Tiền vào mạnh?
       - Giá tăng + Vol thấp => Kéo rướn/Xanh vỏ đỏ lòng?
       - Giá giảm + Vol cao => Xả hàng/Phân phối?
       - Giá giảm + Vol thấp => Tiết cung/Cạn lực bán?
    2. 🌪️ Tâm lý: Đánh giá tâm lý đám đông (Hưng phấn, Sợ hãi hay Thận trọng).
    3. 💡 Hành động ngày mai: Đưa ra "Key Action" ngắn gọn (VD: "Canh chốt lời", "Mua thăm dò", "Quan sát mốc X", "Ngồi im giữ tiền").

    Yêu cầu văn phong:
    - Giọng văn "bụi bặm", chuyên nghiệp kiểu dân trader (dùng từ như: rút chân, nổ vol, cạn cung, fomo, bull trap...).
    - Dùng emoji sinh động để nhấn mạnh (📈, 📉, 🛡️, 💣, 💎).

    OUTPUT JSON FORMAT:
    {{ "ai_comment": "Nội dung nhận định..." }}
"""
    
    try:
        # SỬ DỤNG call_gemini_safe TRONG THREAD
        text = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-pro",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        
        if text:
            data = json.loads(text)
            return data.get("ai_comment", "")
            
    except Exception as e:
        log.error(f"[EOD] Lỗi Gemini Multi-Key: {e}")
        
    return "Thị trường biến động. Hãy quan sát kỹ dòng tiền."

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

from chart_utils import generate_mini_chart

async def send_eod_summary():
    """
    [UPDATED V3] Gửi EOD Summary + AI + Charts (Modal).
    Tối ưu hiệu năng: Vẽ biểu đồ song song.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_str = now.strftime("%d/%m/%Y")
    
    log.info(f"[{INSTANCE_ID}][EOD] 🚀 Bắt đầu quy trình EOD Summary (With Charts)...")

    # --- 1. KHỞI TẠO TASK VẼ CHART THỊ TRƯỜNG (Song song) ---
    # Chúng ta vẽ chart ngay khi bắt đầu để tiết kiệm thời gian
    task_chart_vni = generate_mini_chart("VNINDEX")
    task_chart_v30 = generate_mini_chart("VN30")

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

    # Chờ Chart thị trường xong
    chart_vni_html, chart_v30_html = await asyncio.gather(task_chart_vni, task_chart_v30)

    # Đóng gói market_data (Kèm Chart HTML)
    market_data = {
        "vnindex": vni_data if vni_data else {"price": "---", "change_str": "---", "cls": "t-ref"},
        "vn30": v30_data if v30_data else {"price": "---", "change_str": "---", "cls": "t-ref"}
    }
    # Gán chart vào dict
    market_data["vnindex"]["chart_html"] = chart_vni_html
    market_data["vn30"]["chart_html"] = chart_v30_html

    # --- 3. GỌI AI INSIGHT ---
    ai_comment = "Thị trường đang biến động. Hãy quan sát kỹ dòng tiền."
    if vni_data:
        try:
            ai_comment = await call_gemini_eod_insight(market_data)
        except Exception as e:
            log.error(f"[EOD] Lỗi gọi AI: {e}")
    market_data["ai_comment"] = ai_comment

    # --- 4. LẤY DANH SÁCH USER & STOCK ---
    try:
        all_watch = await asyncio.to_thread(get_all_watch)
        if not all_watch: return
    except Exception: return

    all_symbols = set()
    for block in all_watch.values():
        for s in block.get("list", []):
            if len(str(s)) == 3: all_symbols.add(str(s).upper())
    
    if not all_symbols: return
    
    # Fetch số liệu Stock
    stock_data_list = await fetch_full_eod_data(list(all_symbols))
    
    # --- 5. VẼ CHART CỔ PHIẾU (SONG SONG) ---
    # Tạo dict map symbol -> task vẽ chart
    stock_chart_tasks = {item['symbol']: generate_mini_chart(item['symbol']) for item in stock_data_list}
    
    # Chạy tất cả
    chart_results = await asyncio.gather(*stock_chart_tasks.values())
    
    # Map kết quả HTML vào lại stock_data_list
    chart_map = dict(zip(stock_chart_tasks.keys(), chart_results))
    
    for item in stock_data_list:
        sym = item['symbol']
        item['chart_html'] = chart_map.get(sym, "") # Gán HTML chart vào item

    # Tạo Map để lookup nhanh
    stock_map = {item['symbol']: item for item in stock_data_list}

    # --- 6. GỬI TIN ---
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    tasks = []

    for chat_key, block in all_watch.items():
        try:
            chat_id = int(chat_key)
            watch_list = block.get("list", [])
            if not watch_list: continue

            user_stocks_ready = []
            for sym in watch_list:
                sym_u = str(sym).upper()
                if sym_u in stock_map:
                    user_stocks_ready.append(stock_map[sym_u])
            
            if not user_stocks_ready: continue

            # Payload
            payload = {
                "market_data": market_data,
                "user_stocks": user_stocks_ready,
                "generated_at": datetime.datetime.now(vn_tz).strftime("%H:%M %d/%m"),
                "is_pro": True # Giả định là Pro để hiện full tính năng
            }

            digest_id = uuid.uuid4().hex
            r = get_redis()
            r.set(f"digest_web:eod_web:{digest_id}", json.dumps(payload), ex=86400)
            
            web_app_url = f"{base_url}/eod/{digest_id}"
            
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Xem Tổng Kết & Biểu Đồ", web_app=WebAppInfo(url=web_app_url))
            ]])
            
            vni_text = f"VN-INDEX: {market_data['vnindex']['price']} {market_data['vnindex']['change_str']}"
            msg_text = (
                f"🇻🇳 *Tổng kết phiên {today_str}*\n"
                f"{vni_text}\n"
                f"👉 Nhấn nút để xem chi tiết & soi biểu đồ."
            )
            
            sent_msg = await send_md(tg_app.bot, chat_id, msg_text, reply_markup=kb, msg_type='EOD_SUMMARY')
            
            if sent_msg:
                try:
                    await tg_app.bot.pin_chat_message(
                        chat_id=chat_id, message_id=sent_msg.message_id, disable_notification=True
                    )
                except: pass
            
            tasks.append(asyncio.create_task(asyncio.sleep(0)))

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Lỗi gửi cho {chat_key}: {e}")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        
    log.info(f"[{INSTANCE_ID}][EOD] ✅ Hoàn tất gửi EOD (Modal Chart).")
    
    await asyncio.sleep(10)
    asyncio.create_task(cleanup_after_eod())

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

async def session_notice_loop():
    """
    Loop riêng để gửi thông báo:
    - Sắp mở phiên sáng / chiều
    - Sắp đóng phiên sáng / chiều
    Chạy song song với alert_loop, không ảnh hưởng gì tới lệnh Telegram.
    
    (ĐÃ SỬA LỖI BLOCKING I/O)
    """
    
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # ❗️ KIỂM TRA TRẠNG THÁI BOT
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][SESSION {loop_id}] [thông báo sắp mở / sắp đóng phiên] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue # Quay lại vòng lặp, kiểm tra BOT_ACTIVE tiếp

        next_dt, spec = get_next_notice_after(now)
        if not next_dt or not spec:
            # Trường hợp hiếm: không tìm được mốc, ngủ 1 giờ rồi tính lại
            log.warning(f"[{INSTANCE_ID}][SESSION {loop_id}] Không tìm được mốc thông báo, sleep 3600s")
            await asyncio.sleep(3600)
            continue

        delay = max((next_dt - now).total_seconds(), 1)
        log.info(
            f"[{INSTANCE_ID}][SESSION {loop_id}] Chờ {delay:.0f}s để gửi thông báo {spec['label']} lúc {next_dt}"
        )
        await asyncio.sleep(delay)

        # ❗️ KIỂM TRA LẦN NỮA SAU KHI NGỦ DẬY
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][SESSION {loop_id}] Thức dậy nhưng bot TẮT, bỏ qua thông báo.")
            continue

                # Đến giờ thông báo
        try:
            label = spec.get("label")
            if label == "EOD_SUMMARY":
                # 🔔 15:00 – Tổng kết cuối phiên
                await send_eod_summary()

                # Chờ 1 chút cho EOD đi hết rồi mới dọn dẹp
                await asyncio.sleep(10) 
                asyncio.create_task(cleanup_after_eod())
                # =========================
            else:
                # Các mốc khác: broadcast câu text cố định
                await broadcast_to_all_watchers(spec["text"], target_audience="all", msg_type="SESSION_NOTICE")
        except Exception as e:
            log.error(f"[{INSTANCE_ID}][SESSION {loop_id}] Lỗi khi xử lý thông báo {label}: {e}")


def next_session_start(now: datetime.datetime) -> datetime.datetime:
    """
    Tính thời điểm bắt đầu phiên giao dịch tiếp theo:
    - Nếu trước 09:15 -> 09:15 hôm nay
    - Nếu giữa 11:30–13:00 -> 13:00 hôm nay
    - Nếu sau 14:47 hoặc cuối tuần -> 09:15 ngày làm việc tiếp theo
    Hàm này dùng để cho alert_loop ngủ lâu hơn ngoài giờ giao dịch.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    if now.tzinfo is None:
        now = vn_tz.localize(now)

    weekday = now.weekday()
    hm = now.hour * 60 + now.minute  # phút trong ngày
    date = now.date()

    def at(d: datetime.date, hour: int, minute: int):
        return vn_tz.localize(datetime.datetime(d.year, d.month, d.day, hour, minute))

    # Nếu là T7 hoặc CN -> nhảy tới thứ 2, 09:15
    if weekday > 4:
        days_ahead = (7 - weekday) % 7
        if days_ahead == 0:
            days_ahead = 1
        next_date = date + datetime.timedelta(days=days_ahead)
        return at(next_date, 9, 15)

    # Thứ 2–6
    if hm < 555:  # trước 09:15
        return at(date, 9, 15)

    if 690 < hm < 780:  # giữa 11:30–13:00
        return at(date, 13, 0)

    if hm >= 887:  # sau 14:47 -> sang ngày làm việc tiếp theo 09:15
        next_date = date + datetime.timedelta(days=1)
        while next_date.weekday() > 4:
            next_date = next_date + datetime.timedelta(days=1)
        return at(next_date, 9, 15)

    # Trường hợp còn lại lý thuyết là đang trong phiên, không nên gọi hàm này,
    # fallback: cho 15s nữa.
    return now + datetime.timedelta(seconds=15)


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

def same_sign(a: float, b: float) -> bool:
    """Hai số cùng dấu (cùng dương hoặc cùng âm) hay không."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)


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
        
        # Làm đẹp log: hiển thị phần trăm, giá, và thay đổi tuyệt đối gọn hơn
        price_display = (
            f"{float(out['price']):,.0f}".replace(",", ".")
            if out.get("price") is not None
            else "None"
        )
        pct_display = (
            f"{out['pct']:+.2f}%" if out.get("pct") is not None else "None"
        )
        change_abs_display = (
            f"{int(out['change_abs']):,}".replace(",", ".")
            if out.get("change_abs") is not None
            else "None"
        )


        return out
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [QUOTE FAIL] {symbol}: {e}")
        return None


def get_perf_history(symbol: str):
    """
    Lấy hiệu suất giá (Ngày/Tuần/Tháng).
    (ĐÃ SỬA: Fallback VCI -> TCBS)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    today = datetime.datetime.now(vn_tz).date()
    start_date = (today - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")
    
    df = None
    
    # --- CÁCH 1: THỬ VCI ---
    try:
        quote = Quote(symbol=symbol, source="VCI")
        df = quote.history(start=start_date, end=end_date, interval="1D")
    except Exception:
        pass # Lỗi thì bỏ qua, xuống cách 2
        
    # --- CÁCH 2: THỬ TCBS (Nếu VCI tạch) ---
    if df is None or df.empty:
        try:
            quote = Quote(symbol=symbol, source="TCBS")
            df = quote.history(start=start_date, end=end_date, interval="1D")
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}] [PERF FAIL] {symbol}: Cả VCI và TCBS đều lỗi: {e}")
            return None

    if df is None or len(df) == 0:
        return None

    # Chuẩn hoá dữ liệu (TCBS và VCI có thể khác tên cột, vnstock3 thường chuẩn hóa về lower)
    # Đảm bảo có cột 'time' và 'close'
    try:
        df.columns = [c.lower() for c in df.columns] # Force lower case
        if 'time' not in df.columns or 'close' not in df.columns:
            return None
            
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

        last = df.iloc[-1]
        last_date = last["time"].date()
        
        # Xử lý đơn vị giá (TCBS có thể trả về 26.5 thay vì 26500)
        price = float(last["close"])
        if price < 500: price *= 1000
        
        # Hàm tìm giá quá khứ
        def find_price_before(target_date: datetime.date):
            sub = df[df["time"].dt.date <= target_date]
            if sub.empty: return None
            val = float(sub.iloc[-1]["close"])
            if val < 500: val *= 1000
            return val

        # % NGÀY
        prev_price = find_price_before(last_date - datetime.timedelta(days=1))
        day_pct = (price - prev_price) / prev_price * 100.0 if prev_price else None

        # % TUẦN (7 ngày)
        week_price = find_price_before(last_date - datetime.timedelta(days=7))
        week_pct = (price - week_price) / week_price * 100.0 if week_price else None

        # % THÁNG (30 ngày)
        month_price = find_price_before(last_date - datetime.timedelta(days=30))
        month_pct = (price - month_price) / month_price * 100.0 if month_price else None

        return {
            "price": int(price),
            "day_pct": day_pct,
            "week_pct": week_pct,
            "month_pct": month_pct,
        }

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [PERF ERROR] {symbol}: {e}")
        return None

def format_perf_line(sym: str, perf: dict) -> str:
    price = perf.get("price")
    day_pct = perf.get("day_pct")
    week_pct = perf.get("week_pct")
    month_pct = perf.get("month_pct")

    price_str = f"{price} vnđ" if price is not None else "N/A"
    day_str = f"{day_pct:+.2f}%" if day_pct is not None else "N/A"
    week_str = f"{week_pct:+.2f}%" if week_pct is not None else "N/A"
    month_str = f"{month_pct:+.2f}%" if month_pct is not None else "N/A"

    return (
        f"- {sym}: giá hiện tại {price_str}, "
        f"ngày {day_str}, tuần {week_str}, tháng {month_str}"
    )

# HELPER: PHÂN LOẠI LỖI VÀ NOTIFY ADMIN
def classify_error_quota(e: Exception) -> bool:
    """
    Thử đoán xem lỗi có liên quan đến quota / rate limit hay không.
    Dùng best-effort dựa trên nội dung message.
    """
    msg = (str(e) or "").lower()
    quota_keywords = [
        "quota",
        "rate limit",
        "resourceexhausted",
        "429",
        "too many requests",
        "exceeded",
    ]
    return any(kw in msg for kw in quota_keywords)


# Lưu các cache_key đã gửi lỗi cho Admin để tránh spam
REPORTED_REPORT_ERROR_KEYS: set[str] = set()
async def notify_admin_report_error_once(
    bot,
    cache_key: str,
    error: Exception,
) -> None:
    """
    Gửi thông báo lỗi tạo báo cáo danh mục cho Admin, mỗi cache_key chỉ gửi 1 lần.
    """
    global REPORTED_REPORT_ERROR_KEYS

    if ADMIN_ID is None:
        return

    key = cache_key or "UNKNOWN"
    if key in REPORTED_REPORT_ERROR_KEYS:
        return

    REPORTED_REPORT_ERROR_KEYS.add(key)

    # Giới hạn chi tiết lỗi để tránh message admin cũng quá dài
    err_detail = str(error)
    if len(err_detail) > 1500:
        err_detail = err_detail[:1500] + " ...[truncated]"

    msg = (
        f"⚠️ Lỗi khi gọi Gemini tạo báo cáo danh mục cho key `{key}`:\n"
        f"- Loại lỗi: {type(error).__name__}\n"
        f"- Chi tiết: {err_detail}"
    )

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=msg)
    except Exception as e2:
        log.warning(
            f"[{INSTANCE_ID}] Lỗi khi gửi thông báo báo cáo lỗi cho Admin: {e2}"
        )

# =====================================================
# HELPER CHO AI REPORT: INJECT REALTIME DATA TỪ REDIS
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
"""

    log.info(f"[{INSTANCE_ID}] Gọi Gemini (Report Mean Reversion): {symbols_str}")

    try:
        raw_text = call_gemini_safe(
            model_id="gemini-2.5-flash",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        # 🔥 FIX QUAN TRỌNG: Xóa markdown fences
        return raw_text.replace("```json", "").replace("```", "").strip()
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi Gemini Report: {e}")
        raise e

# ==============================================
# HÀM GỌI GEMINI CHO HỒ SƠ DOANH NGHIỆP (/info)
# ==============================================

def call_gemini_for_profile(symbol: str) -> str:
    """
    (PHIÊN BẢN JSON - MULTI-KEY) Gọi Gemini tạo hồ sơ doanh nghiệp.
    """
    if not GEMINI_KEYS:
        raise RuntimeError("GEMINI_API_KEY chưa được cấu hình.")

    sym = symbol.upper().strip()
    log.info(f"[{INSTANCE_ID}] Gọi Gemini (Multi-Key) cho hồ sơ: {sym}")

    prompt = f"""
Bạn là chuyên gia phân tích doanh nghiệp tại thị trường chứng khoán Việt Nam.
Hãy tạo một "Hồ sơ Doanh nghiệp" chi tiết cho mã cổ phiếu: {sym}

YÊU CẦU FORMAT:
Trả về **JSON thuần**. Cấu trúc bắt buộc gồm các keys sau:
{{
  "overview": "Tên đầy đủ, ngành nghề, lịch sử tóm tắt...",
  "products": "Các sản phẩm/dịch vụ cốt lõi...",
  "business_model": "Cách tạo ra lợi nhuận, khách hàng (B2B/B2C)...",
  "market_position": "Thị phần, vị thế trong ngành, đối thủ...",
  "value_chain": "Tự chủ nguyên liệu, gia công hay phân phối...",
  "moat": "Lợi thế cạnh tranh (thương hiệu, chi phí, công nghệ)...",
  "risks": "Rủi ro đặc thù (nguyên liệu, tỷ giá, pháp lý)...",
  "leadership": "Ban lãnh đạo chủ chốt và cơ cấu cổ đông..."
}}

YÊU CẦU NỘI DUNG:
- Các trường nội dung phải trình bày gãy gọn, dùng ký tự xuống dòng (\\n) và gạch đầu dòng (•) để tách ý.
- Giọng văn khách quan, KHÔNG khuyến nghị mua bán.
"""

    try:
        # GỌI HÀM WRAPPER TRỰC TIẾP
        return call_gemini_safe(
            model_id="gemini-2.5-flash-lite",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )

    except Exception as e:
        # log.error(...) -> để hàm gọi bên ngoài lo
        raise e

#--------------------------------------------

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

# =============================================
# HÀM XỬ LÝ TIN NHẮN KHÔNG RÕ NGHĨA
# =============================================

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý tin nhắn văn bản:
    - Nếu là mã cổ phiếu (3 chữ cái) -> Gợi ý nút bấm (Add / Info).
    - Nếu không hiểu -> Báo lỗi hướng dẫn dùng /help.
    """
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text.strip().upper()

    # --- LOGIC MỚI: SMART INPUT HANDLING ---
    # Kiểm tra: Đúng 3 ký tự VÀ là chữ cái (A-Z)
    if len(user_text) == 3 and user_text.isalpha():
        # Tạo 2 nút bấm Inline
        kb = [
            [
                InlineKeyboardButton(f"➕ Theo dõi {user_text}", callback_data=f"btn_add_{user_text}"),
                InlineKeyboardButton(f"📄 Soi hồ sơ", callback_data=f"btn_info_{user_text}")
            ],
            [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]
        ]
        
        # Gửi tin nhắn gợi ý
        await reply_md(
            update,
            f"🤔 Bạn đang quan tâm mã **{user_text}** phải không?\n"
            f"Chọn nhanh thao tác bên dưới nhé:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return
    # ---------------------------------------

    # Logic cũ (Xử lý user mới + Báo lỗi)
    try:
        # Tự động lưu chat_id vào DB nếu chưa có (giữ nguyên logic cũ của bạn)
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        if lst is None:
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])
    except Exception as e:
        log.warning(f"Lỗi khi auto-save chat_id {chat_id}: {e}")

    # --- 2. FALLBACK (GÕ BẬY BẠ / KHÔNG HIỂU) ---
    # Thay vì chỉ báo lỗi, hãy cung cấp lối thoát (Nút Dashboard & Help)
    kb_fallback = [
        [
            InlineKeyboardButton("🏠 Mở Dashboard", callback_data="back_to_start"),
            InlineKeyboardButton("❓ Hướng dẫn", callback_data="menu_help")
        ]
    ]

    # Phản hồi mặc định
    reply_text = (
        "😅 **Xin lỗi, mình chưa hiểu ý bạn.**\n\n"
        "💡 **Gợi ý:**\n"
        "• Gõ mã cổ phiếu 3 chữ cái (VD: `HPG`, `FPT`) để tra cứu.\n"
        "• Hoặc chọn tính năng nhanh bên dưới:"
    )
    await reply_md(update, reply_text, reply_markup=InlineKeyboardMarkup(kb_fallback))


@anti_spam_check
async def handle_quick_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý toàn bộ các nút bấm (CallbackQuery).
    """
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    
    # Kiểm tra bảo trì
    if not BOT_ACTIVE:
        await query.answer("⚙️ Hệ thống đang bảo trì.", show_alert=True)
        return

    # --- NHÓM 1: CÁC NÚT ĐÓNG / HỦY ---
    if data in ["close_msg", "close_list", "close_setting"]:
        await query.delete_message()
        return

    # --- NHÓM 2: MENU LIST, ADD, HELP, INFO, BACK ---
    # (Giữ nguyên logic cũ cho các nút này - Copy từ file cũ nếu cần hoặc dùng đoạn dưới)
    if data == "menu_list" or data == "back_to_list":
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        if not lst:
            kb = [[InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")]]
            await safe_edit_message(query, "📭 Danh mục trống.", InlineKeyboardMarkup(kb))
        else:
            keyboard = []
            row = []
            for sym in lst:
                row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row: keyboard.append(row)
            keyboard.append([InlineKeyboardButton("➕ Thêm mã", callback_data="menu_add")])
            keyboard.append([InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")])
            await safe_edit_message(query, "📋 **Quản lý danh mục**", InlineKeyboardMarkup(keyboard))
            
    elif data == "menu_add":
        kb = [[InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")]]
        await safe_edit_message(query, "➕ Gõ mã 3 chữ cái (VD: `HPG`) vào ô chat để thêm.", InlineKeyboardMarkup(kb))

    elif data == "menu_help":
        await cmd_help(update, context)

    elif data == "back_to_start":
        await cmd_start(update, context) # Gọi lại hàm start để vẽ lại menu
        await query.delete_message()

    elif data == "menu_setting":
        await cmd_setting(update, context) # Gọi hàm setting mới cập nhật ở trên
    
    elif data == "btn_trial_click":
        # Gọi lại hàm cmd_trial
        await cmd_trial(update, context)

    # --- NHÓM 3: XỬ LÝ BẬT/TẮT (SETTING) ---
    
    # 3.1. VN30F1M
    elif data in ("set_vn30_on", "set_vn30_off"):
        want_on = (data == "set_vn30_on")
        
        # Check Pro
        if want_on:
            is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
            if not is_pro:
                await query.answer("⚠️ Chỉ dành cho Gói Pro!", show_alert=True)
                return

        await asyncio.to_thread(set_vn30f1m_enabled, chat_id, want_on)
        reload_vn30f1m_enabled_cache()
        
        # Hiển thị thông báo nhỏ (Toast)
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} VN30F1M!")
        
        # 🔥 GỌI HÀM cmd_setting ĐỂ UPDATE GIAO DIỆN TẠI CHỖ 🔥
        # (Vì update có callback_query nên cmd_setting sẽ tự biết là cần edit message)
        await cmd_setting(update, context) 

    # 3.2. STOCK ALERT (MỚI)
    elif data in ("set_stock_on", "set_stock_off"):
        want_on = (data == "set_stock_on")
        
        await asyncio.to_thread(set_stock_alert_enabled, chat_id, want_on)
        reload_stock_alert_enabled_cache() 
        
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} Cảnh báo Stock!")
        
        # 🔥 GỌI HÀM cmd_setting ĐỂ UPDATE GIAO DIỆN TẠI CHỖ 🔥
        await cmd_setting(update, context)

    # --- NHÓM 4: CÁC TÁC VỤ KHÁC (Report, Info, Screener, Upgrade...) ---
    # (Copy logic cũ của bạn vào đây để không bị mất các tính năng đó)
    elif data == "menu_report": await cmd_report(update, context)
    elif data == "menu_screener": await cmd_screener_value(update, context)
    # [MỚI] XỬ LÝ NÚT SOI HỒ SƠ TỪ DASHBOARD
    elif data == "menu_info":
        chat_id = update.effective_chat.id
        
        # 1. Lấy danh sách watchlist
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        
        keyboard = []
        
        # 2. Tạo nút cho từng mã
        if lst:
            row = []
            for sym in lst:
                row.append(InlineKeyboardButton(sym, callback_data=f"btn_info_{sym}"))
                if len(row) == 3: 
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
        
        # 3. Nút điều hướng: Quay lại Dashboard (Thay vì Đóng)
        keyboard.append([InlineKeyboardButton("🔙 Quay lại Dashboard", callback_data="back_to_start")])
        
        msg_text = "📄 **Tra cứu Hồ sơ Doanh nghiệp**\n\n"
        if lst:
            msg_text += "👇 **Chọn mã trong danh mục để soi:**\n"
        else:
            msg_text += "📭 Danh mục trống. Hãy thêm mã trước.\n"
            
        msg_text += "\n👉 Hoặc gõ trực tiếp mã (VD: `MWG`) vào ô chat."

        # 4. Cập nhật tại chỗ (In-place)
        await query.edit_message_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("btn_info_"):
        symbol = data.split("_")[2]
        context.args = [symbol]
        await cmd_info(update, context)

    elif data == "btn_upgrade": await cmd_upgrade(update, context)

    elif data.startswith("btn_add_"):
        symbol = data.split("_")[2]
        
        # Lấy danh sách hiện tại
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        
        # Check giới hạn Pro (nếu cần)
        is_pro = await asyncio.to_thread(is_user_pro, chat_id)
        is_admin = (chat_id == ADMIN_ID)
        
        if not is_pro and not is_admin and len(lst) >= 1:
             # Nếu chưa có mã này mà list đã đầy -> Chặn
             if symbol not in lst:
                await query.answer("⚠️ Free chỉ được 1 mã. Nâng Pro để thêm!", show_alert=True)
                return 

        # Thêm vào DB nếu chưa có
        if symbol not in lst:
            lst.append(symbol)
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)
            await query.answer(f"✅ Đã thêm {symbol}")
        else:
            await query.answer("Đã có trong danh mục!", show_alert=True)

        # --- 🔥 QUAN TRỌNG: VẼ LẠI MENU DANH MỤC NGAY TẠI ĐÂY ---
        # (Copy logic hiển thị từ menu_list)
        if not lst:
            kb = [[InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")]]
            await safe_edit_message(query, "📭 Danh mục trống.", InlineKeyboardMarkup(kb))
        else:
            keyboard = []
            row = []
            for sym in lst:
                row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row: keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("➕ Thêm mã", callback_data="menu_add")])
            keyboard.append([InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")])
            
            # Update giao diện thành danh sách mới
            await safe_edit_message(query, "📋 **Quản lý danh mục**", InlineKeyboardMarkup(keyboard))


    # =========================================================
    # 2. XỬ LÝ NÚT XÓA (Sửa lại để tự quay về danh sách)
    # =========================================================
    elif data.startswith("btn_del_"):
        symbol = data.split("_")[2]
        
        # Lấy danh sách
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        
        # Xóa khỏi DB
        if symbol in lst:
            lst.remove(symbol)
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)
            await query.answer(f"🗑️ Đã xóa {symbol}")
        else:
            await query.answer("Mã không tồn tại!", show_alert=True)
        
        # --- 🔥 QUAN TRỌNG: VẼ LẠI MENU DANH MỤC ---
        # (Logic y hệt bên trên, để sau khi xóa xong user thấy ngay list mới)
        if not lst:
            kb = [[InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")]]
            await safe_edit_message(query, "📭 Danh mục trống.", InlineKeyboardMarkup(kb))
        else:
            keyboard = []
            row = []
            for sym in lst:
                row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row: keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("➕ Thêm mã", callback_data="menu_add")])
            keyboard.append([InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")])
            
            await safe_edit_message(query, "📋 **Quản lý danh mục**", InlineKeyboardMarkup(keyboard))

    elif data.startswith("mgr_"):
        # Menu quản lý mã
        symbol = data.split("_")[1]
        kb = [[InlineKeyboardButton("📄 Soi hồ sơ", callback_data=f"btn_info_{symbol}"), InlineKeyboardButton("🗑️ Xóa", callback_data=f"btn_del_{symbol}")], [InlineKeyboardButton("🔙 Quay lại", callback_data="back_to_list")]]
        await safe_edit_message(query, f"⚙️ **{symbol}**", InlineKeyboardMarkup(kb))


# =====================================================================
# =============== VALUE SCREENER (VNSTOCK API VERSION) ================
# =====================================================================

# -------------------------- Redis Helper --------------------------

def _ttl_until_midnight() -> int:
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=3, microsecond=0
    )
    return max(60, int((midnight - now).total_seconds()))

def seconds_until_next_weekday_screener():
    """
    Tính số giây tới 09:00 sáng ngày làm việc (T2-T6) tiếp theo.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    
    target_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

    # Nếu hôm nay là T2-T6 và chưa tới 9h sáng
    if 0 <= now.weekday() <= 4 and now < target_time:
        target = target_time
    else:
        # Ngược lại, tìm ngày T2-T6 tiếp theo
        days_ahead = 1
        next_date = now.date() + datetime.timedelta(days=days_ahead)
        while next_date.weekday() > 4: # Bỏ qua T7 (5), CN (6)
            next_date += datetime.timedelta(days=1)
        
        target = datetime.datetime(
            next_date.year, next_date.month, next_date.day, 9, 0, 0, tzinfo=vn_tz
        )

    return max((target - now).total_seconds(), 0)

def format_roe_pct(roe_decimal: float | None) -> str:
    """
    ROE trong DB là dạng thập phân (0.111 = 11.1%).
    - Nếu < 1%: in 2 chữ số thập phân
    - Nếu >= 1%: in 1 chữ số thập phân
    """
    if roe_decimal is None or math.isnan(roe_decimal):
        return "N/A"
    pct = roe_decimal * 100.0
    if abs(pct) < 1:
        return f"{pct:.2f}%"
    return f"{pct:.1f}%"

# ==============================================
# SMART DATA FETCHER (Async + Timeout 20s)
# ==============================================
# Biến toàn cục để lưu trạng thái chặn VCI theo ngày
# Nếu _vci_blocked_date == hôm nay -> Bỏ qua VCI
_vci_blocked_date = None 

# ==============================================
# SMART DATA FETCHER (Final Version: Circuit Breaker + TCBS 1m/1D)
# ==============================================

async def fetch_data_smart(symbols: list[str]) -> dict[str, dict]:
    """
    [UPDATED] Smart Fetcher: VCI PriceBoard -> Fallback TCBS.
    Tự động chuẩn hóa đơn vị giá (nếu < 500 đồng -> nhân 1000) để tránh lệch số.
    """
    global _vci_blocked_date
    
    results = {}
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_date = now.date()

    # Helper: Chuẩn hóa giá (x1000 nếu cần)
    def _norm_price(p):
        if p is None: return 0.0
        p = float(p)
        if 0 < p < 500: return p * 1000.0
        return p

    # 1. KIỂM TRA TRẠNG THÁI VCI
    skip_vci = (_vci_blocked_date == today_date)

    if not skip_vci:
        # --- NGUỒN 1: VCI (BATCH - NHANH NHẤT) ---
        try:
            def _run_vci():
                t = Trading(source="VCI")
                return t.price_board(symbols)

            df = await asyncio.wait_for(
                asyncio.to_thread(_run_vci), 
                timeout=20.0
            )
            
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    try:
                        sym = str(row.get(('listing', 'symbol'), row.get('symbol'))).upper().strip()
                        
                        # Lấy giá & chuẩn hóa ngay lập tức
                        match_p = _norm_price(row.get(('match', 'match_price')))
                        ref_p = _norm_price(row.get(('listing', 'ref_price')))
                        
                        # Nếu giá khớp = 0 (đầu phiên), dùng tham chiếu tạm
                        if match_p == 0 and ref_p > 0:
                            match_p = ref_p

                        pct = 0.0
                        if ref_p > 0:
                            pct = ((match_p - ref_p) / ref_p) * 100.0

                        results[sym] = {"price": match_p, "pct": pct, "ref": ref_p}
                    except: continue
                
                if len(results) == len(symbols):
                    return results
                
        except asyncio.TimeoutError:
            log.warning(f"[SMART] ⏳ VCI timeout. Chuyển sang TCBS.")
            _vci_blocked_date = today_date
        except Exception as e:
            log.warning(f"[SMART] ❌ VCI lỗi: {e}. Chuyển sang TCBS.")
            _vci_blocked_date = today_date

    # --- NGUỒN 2: TCBS (FALLBACK - CHẬM HƠN) ---
    missing_symbols = [s for s in symbols if s not in results]
    if not missing_symbols:
        return results

    try:
        today_str = today_date.strftime("%Y-%m-%d")
        start_str = (today_date - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        
        def _run_tcbs_1m_fallback(syms_to_run):
            tcbs_results = {}
            for sym in syms_to_run:
                try:
                    quote = Quote(symbol=sym, source="TCBS")
                    # Dùng nến 1m để lấy giá tươi nhất
                    df = quote.history(start=start_str, end=today_str, interval="1m")
                    
                    if df is not None and not df.empty:
                        last_row = df.iloc[-1]
                        
                        # Lấy giá & chuẩn hóa
                        current_price = _norm_price(last_row['close'])
                        
                        # Giả lập tham chiếu từ nến trước (để tính %)
                        ref_price = current_price
                        if len(df) >= 2:
                            prev_close = _norm_price(df.iloc[-2]['close'])
                            ref_price = prev_close
                        
                        pct = 0.0
                        if ref_price > 0:
                            pct = ((current_price - ref_price) / ref_price) * 100.0

                        tcbs_results[sym] = {"price": current_price, "pct": pct, "ref": ref_price}
                except: continue
            return tcbs_results

        tcbs_data = await asyncio.to_thread(_run_tcbs_1m_fallback, missing_symbols)
        results.update(tcbs_data)
                
    except Exception as e:
        log.error(f"[SMART] ❌ TCBS 1m Fatal Error: {e}")
        
    return results

def get_index_eod_vci(symbol: str):
    """
    Lấy dữ liệu EOD (Close, Change, Volume) từ VCI History.
    Dùng cho EOD Summary để đảm bảo độ ổn định và có Volume.
    """
    try:
        # Lấy 5 ngày gần nhất để chắc chắn có dữ liệu tính tham chiếu
        today = datetime.datetime.now().date()
        start_date = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
        
        # Gọi API VCI History
        quote = Quote(symbol=symbol, source="VCI")
        df = quote.history(start=start_date, end=end_date, interval="1D")
        
        if df is None or df.empty or len(df) < 2:
            return None

        # Lấy dòng mới nhất và dòng liền trước
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        price = float(last['close'])
        ref_price = float(prev['close'])
        change = price - ref_price
        pct = 0.0
        if ref_price > 0:
            pct = (change / ref_price) * 100.0
        volume = float(last['volume'])
        
        return {
            "price": price,
            "change": change,
            "pct": pct,
            "volume": volume
        }
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] Lỗi lấy EOD VCI cho {symbol}: {e}")
        return None

# ==============================================
# VÒNG LẶP CẢNH BÁO (CÓ CACHE SYMBOL) - ALERT_LOOP
# ==============================================

# [TỐI ƯU] Khởi tạo Trading object MỘT LẦN dùng chung
try:
    stock_trading = Trading(source="VCI")
    log.info(f"[{INSTANCE_ID}] Khởi tạo 'stock_trading' (VCI) dùng chung thành công.")
except Exception as e:
    stock_trading = None
    log.error(f"[{INSTANCE_ID}] KHÔNG THỂ KHỞI TẠO 'stock_trading' (VCI): {e}. Vòng lặp Alert sẽ không chạy.")

# [TỐI ƯU] Hàng đợi và Cache cho 3 tác vụ (Stock)
_stock_broadcast_queue = asyncio.Queue()
_stock_current_price_cache: dict[str, dict] = {} # Cache giá {HPG: {"price": ..., "pct": ...}}
_stock_current_watch_cache: dict[str, dict] = {} # Cache watchlist {chat_id: {"list": [...]}}
TICKER_INTERVAL_SECONDS = 10  # Tần suất Ticker (check cache)
FETCHER_INTERVAL_SECONDS = 20 # Tần suất Fetcher (gọi API)

# (Các hàm same_sign, get_quote... của bạn nằm ở đây)
def same_sign(a: float, b: float) -> bool:
    """Hai số cùng dấu (cùng dương hoặc cùng âm) hay không."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)

async def stock_price_fetcher_loop():
    """
    (TÁC VỤ 1 - FETCHER STOCK)
    - Đã thêm log chi tiết giá của 3 mã đầu tiên để debug.
    """
    global _stock_current_price_cache, _stock_current_watch_cache
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    BATCH_SIZE = 10 

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        if not BOT_ACTIVE:
            await asyncio.sleep(60)
            continue

        if not in_session_vietnam():
            next_start = next_session_start(now)
            delay = max((next_start - now).total_seconds(), 60.0)
            log.info(f"[{INSTANCE_ID}][FETCHER_STOCK] Ngoài giờ... Ngủ tới {next_start.strftime('%H:%M')}")
            await asyncio.sleep(delay)
            continue

        loop_start = now
        try:
            # 1. Lấy Watchlist
            all_watch = await asyncio.to_thread(get_all_watch)
            all_symbols = set()
            for block in all_watch.values():
                for sym in (block.get("list", []) or []):
                    if len(sym) == 3 and sym.isalpha():
                         all_symbols.add(sym.upper())
            
            if not all_symbols:
                await asyncio.sleep(30)
                continue

            symbols_list = sorted(list(all_symbols))

            # 2. Gọi Smart Fetcher theo Batch
            final_results = {}
            
            for i in range(0, len(symbols_list), BATCH_SIZE):
                batch_syms = symbols_list[i:i + BATCH_SIZE]
                
                # Gọi hàm Async Smart Fetcher
                batch_data = await fetch_data_smart(batch_syms)
                
                if batch_data:
                    final_results.update(batch_data)
                
                await asyncio.sleep(0.5) 

            # 3. Cập nhật Cache & LOG CHI TIẾT
            if final_results:
                _stock_current_price_cache = final_results
                _stock_current_watch_cache = all_watch
                
                # 🔥 LOG DEBUG: In ra giá của 3 mã đầu tiên để kiểm tra 🔥
                sample_log = []
                for sym, info in list(final_results.items())[:3]:
                    price = info.get('price')
                    sample_log.append(f"{sym}={price}")
                
                sample_str = " | ".join(sample_log)
                log.info(f"[{INSTANCE_ID}][FETCHER_STOCK] ✅ Cache updated: {len(final_results)} mã. Sample: [{sample_str}]")
            else:
                log.warning(f"[{INSTANCE_ID}][FETCHER_STOCK] ⚠️ Không lấy được dữ liệu nào.")

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][FETCHER_STOCK] Error: {e}")

        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(FETCHER_INTERVAL_SECONDS - elapsed, 1)
        await asyncio.sleep(delay)

async def alert_loop():
    """
    (TÁC VỤ 2 - TICKER - FIXED)
    - Mốc so sánh ban đầu LUÔN LÀ 0.0 (Giá tham chiếu).
    - Báo khi giá thay đổi >= 2% so với mốc gần nhất.
    - Đã thêm logic: Giới hạn Free User (1 mã) & Check Bật/Tắt Setting.
    """
    vn_tz = pytz.timezone(TIMEZONE)

    log.info(f"[{INSTANCE_ID}][TICKER_STOCK] Bắt đầu. Mốc khởi tạo = GIÁ THAM CHIẾU (0%).")
    
    # Load cache setting lần đầu khi khởi động loop
    reload_stock_alert_enabled_cache()

    while True:
        now = datetime.datetime.now(vn_tz)

        # 1. Kiểm tra điều kiện chạy
        if not BOT_ACTIVE:
            await asyncio.sleep(30)
            continue
        if not in_session_vietnam():
            await asyncio.sleep(60)
            continue

        loop_start = now
        try:
            all_watch = _stock_current_watch_cache
            quote_cache = _stock_current_price_cache
            all_state = get_state_for_all()

            if not all_watch or not quote_cache:
                await asyncio.sleep(TICKER_INTERVAL_SECONDS)
                continue

            # Lấy danh sách Pro mới nhất để check quyền hạn
            try:
                pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
            except Exception:
                pro_chat_ids = set()

            for chat_key, user_block in all_watch.items():
                try:
                    chat_id = int(chat_key)
                except Exception:
                    continue

                watch_list = user_block.get("list", []) or []
                if not watch_list:
                    continue

                # === 🔥 LOGIC 1: PHÂN QUYỀN & CẮT DANH SÁCH 🔥 ===
                # Nếu là Pro hoặc Admin -> Dùng full danh sách
                # Nếu là Free -> Chỉ lấy mã đầu tiên (watch_list[:1])
                is_pro = (chat_id in pro_chat_ids) or (chat_id == ADMIN_ID)
                
                if is_pro:
                    processing_list = watch_list
                else:
                    # Free user chỉ được xử lý mã đầu tiên trong danh sách
                    processing_list = watch_list[:1] if watch_list else []

                if chat_key not in all_state:
                    all_state[chat_key] = {}
                personal_state = all_state[chat_key]

                messages: list[str] = []

                # Tạo danh sách chứa các nút bấm
                buttons = []

                # Chỉ duyệt qua các mã được phép xử lý
                for sym in processing_list:
                    sym_u = str(sym).upper().strip()
                    if not sym_u:
                        continue

                    quote = quote_cache.get(sym_u)
                    if not quote:
                        continue

                    price = quote.get("price")
                    pct = quote.get("pct")  # pct = % so với Tham Chiếu

                    if price is None or pct is None:
                        continue

                    # === QUẢN LÝ TRẠNG THÁI (STATE) ===
                    state_entry = personal_state.get(sym_u, {})
                    last_alert_at_str = state_entry.get("last_alert_at")
                    last_pct = state_entry.get("last_pct")

                    # Parse thời gian alert cuối
                    last_alert_at = None
                    if last_alert_at_str:
                        try:
                            last_alert_at = datetime.datetime.fromisoformat(last_alert_at_str)
                            if last_alert_at.tzinfo is None:
                                last_alert_at = vn_tz.localize(last_alert_at)
                        except Exception:
                            last_alert_at = None

                    # Reset mốc về 0.0 nếu qua ngày mới
                    if last_pct is None or (last_alert_at and last_alert_at.date() != now.date()):
                        last_pct = 0.0 
                        last_alert_at = None 

                    # === TÍNH TOÁN BIẾN ĐỘNG ===
                    delta_pct = float(pct) - float(last_pct)
                    should_alert = abs(delta_pct) >= 2.0 # Ngưỡng 2%

                    if should_alert:
                        # 1. Chuẩn bị dữ liệu hiển thị
                        icon = "🟢" if pct >= 0 else "🔴"
                        direction = "tăng" if pct >= 0 else "giảm"
                        
                        price_str = f"{float(price):,.0f}".replace(",", ".")
                        pct_str = f"{float(pct):+.2f}%"
                        
                        fun_line = random.choice(FUN_UP if pct >= 0 else FUN_DOWN)

                        # 2. Tạo nội dung tin nhắn
                        msg = (
                            f"{icon} * {sym_u} {direction} {pct_str} Giá hiện tại: {price_str}*\n"
                            f"_{fun_line}_"
                        )
                        
                        # Nút soi chart
                        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
                        chart_url = f"{base_url}/chart/{sym_u}"

                        # Tạo nút và thêm vào danh sách (Mỗi mã 1 hàng)
                        buttons.append([
                            InlineKeyboardButton(f"📊 Soi Chart {sym_u}", web_app=WebAppInfo(url=chart_url))
                        ])
                        
                        # Lưu ý: Ở đây mình không attach button vào từng msg để gộp tin cho gọn
                        messages.append(msg)
                        
                        # 3. Cập nhật mốc mới vào State
                        personal_state[sym_u] = {
                            "last_pct": float(pct),
                            "last_alert_at": now.isoformat(),
                        }
                    
                    # Lưu mốc 0.0 nếu chưa có alert nào trong ngày
                    elif sym_u not in personal_state or (last_alert_at is None and state_entry.get("last_pct") != 0.0):
                         personal_state[sym_u] = {
                            "last_pct": 0.0,
                            "last_alert_at": state_entry.get("last_alert_at")
                        }

               # === LOGIC GỬI TIN ===
            if messages and (chat_id in _stock_alert_enabled_cache):
                header = (
                    "--------------------------------\n"
                    f"⏰ *Cảnh báo {now.strftime('%H:%M')}*"
                )
                messages_text = "\n".join(messages)
                body = messages_text + "\n" + header
                
                # Tạo Markup từ danh sách nút đã gom
                reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

                try:
                    # 🔥 QUAN TRỌNG: Đẩy cả 'markup' vào queue
                    _stock_broadcast_queue.put_nowait({
                        "chat_id": chat_id, 
                        "body": body,
                        "markup": reply_markup 
                    })
                except asyncio.QueueFull:
                        pass

            save_state_for_all(all_state)

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][TICKER] Lỗi: {e}")

        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(TICKER_INTERVAL_SECONDS - elapsed, 0.5)
        await asyncio.sleep(delay)

async def stock_broadcast_loop():
    """
    (TÁC VỤ 3 - BROADCASTER STOCK)
    """
    log.info("[BCASTER_STOCK] Bắt đầu. Chờ tin nhắn trong queue...")
    
    while True:
        try:
            # Chờ Ticker đẩy tin nhắn vào
            item = await _stock_broadcast_queue.get()
            
            if not BOT_ACTIVE:
                _stock_broadcast_queue.task_done()
                continue

            chat_id = item.get("chat_id")
            body = item.get("body")
            # --- LẤY MARKUP TỪ QUEUE ---
            markup_obj = item.get("markup") 
            
            if not chat_id or not body:
                _stock_broadcast_queue.task_done()
                continue
            
            # --- CHUYỂN MARKUP SANG JSON STRING ---
            # Vì send_msg_to dùng requests (HTTP request thuần), 
            # ta phải convert object InlineKeyboardMarkup thành JSON string.
            markup_json = markup_obj.to_json() if markup_obj else None

            # Gửi tin nhắn kèm reply_markup
            await asyncio.to_thread(
                send_msg_to, 
                chat_id, 
                body, 
                "Markdown", 
                False, 
                "STOCK_ALERT",
                markup_json  # <--- Truyền vào đây
            )

            _stock_broadcast_queue.task_done()
            await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            log.info("[BCASTER_STOCK] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[BCASTER_STOCK] Lỗi: {e}")
            if '_stock_broadcast_queue' in locals():
                try: _stock_broadcast_queue.task_done()
                except: pass

#-------------------------------------------

# ==============================================
# BÁO CÁO TUẦN 09:00 CHỦ NHẬT (CÓ CACHE + RETRY)
# ==============================================
async def execute_weekly_report(admin_update: Update | None = None):
    """
    (PHIÊN BẢN WEB APP JSON)
    Chạy, tính toán và gửi báo cáo tuần (Pro + Admin).
    Gửi nút mở Web App thay vì tin nhắn text dài.
    """
    global INSTANCE_ID, log, tg_app, BOT_ACTIVE, GEMINI_API_KEY, ADMIN_ID

    instance_label = f"[{INSTANCE_ID}][EXEC_WEEKLY]"
    admin_chat_id = admin_update.effective_chat.id if admin_update else None
    vn_tz = pytz.timezone(TIMEZONE)
    
    try:
        log.info(f"{instance_label} Bắt đầu chạy (trigger by: {'Admin' if admin_chat_id else 'Scheduler'}).")
        if admin_chat_id:
            await tg_app.bot.send_message(admin_chat_id, "⏳ Bắt đầu chạy tác vụ gửi Weekly Report (Web App Mode)...")

        if not BOT_ACTIVE:
            log.info(f"{instance_label} Bot TẮT, huỷ tác vụ.")
            return

        if not GEMINI_API_KEY:
            log.warning(f"{instance_label} Chưa có GEMINI_API_KEY, bỏ qua.")
            return

        # 1. Lấy dữ liệu user
        all_watch = await asyncio.to_thread(get_all_watch)
        pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)

        if not all_watch:
            log.info(f"{instance_label} Không có user nào theo dõi, bỏ qua.")
            return

        sent_count = 0
        skipped_count = 0
        error_count = 0

        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"

        # 2. Duyệt qua từng user
        for chat_key, user_block in all_watch.items():
            if not BOT_ACTIVE:
                log.info(f"{instance_label} Bot TẮT giữa chừng, dừng gửi.")
                break

            try:
                chat_id = int(chat_key)
            except: continue
            
            # === LOGIC PAYWALL: Chỉ gửi cho Pro & Admin ===
            if chat_id not in pro_chat_ids and chat_id != ADMIN_ID:
                skipped_count += 1
                continue

            watch_list = user_block.get("list", []) or []
            # Lọc bỏ index (VN...) và giới hạn 6 mã để đảm bảo JSON ổn định
            symbols = [s.upper() for s in watch_list if not s.upper().startswith("VN")]
            
            if not symbols:
                skipped_count += 1
                continue
            
            # Cắt ngắn danh sách nếu quá dài (giống logic /report)
            if len(symbols) > 6:
                symbols = symbols[:6]

            cache_key = make_report_cache_key(symbols)
            
            # === KIỂM TRA CACHE ===
            # Logic: 
            # - Nếu đã có cache JSON hợp lệ (do user tự chạy /report trước đó) -> Dùng lại.
            # - Nếu chưa có -> Gọi AI tạo mới -> Lưu Cache.
            
            # Max age ~6.9 ngày (để đảm bảo báo cáo tuần luôn mới nếu chưa có trong tuần)
            cached = get_report_from_redis(cache_key, max_age_days=6.9)
            
            json_text = None
            
            if cached is not None:
                # Cache HIT
                text_val, generated_at, is_error, wait_sec = cached
                if not is_error:
                    json_text = text_val
                    log.info(f"{instance_label} Dùng lại cache có sẵn cho {chat_id}.")
                else:
                    # Cache lỗi -> Bỏ qua user này để tránh spam lỗi, hoặc retry (ở đây chọn retry gọi AI)
                    log.info(f"{instance_label} Cache cũ bị lỗi, sẽ gọi AI lại cho {chat_id}.")
            
            # Nếu chưa có nội dung (Cache MISS), gọi AI
            if not json_text:
                try:
                    start = time.time()
                    # Gọi hàm call_chatgpt_for_report (đã update trả về JSON)
                    json_text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
                    duration = time.time() - start
                    
                    # Lưu vào Redis
                    save_report_to_redis(cache_key, json_text, source="weekly_loop")
                    log.info(f"{instance_label} Gemini JSON done in {duration:.1f}s cho {chat_id}")
                    
                except Exception as e:
                    log.error(f"{instance_label} Lỗi gọi AI cho {chat_id}: {e}")
                    # Lưu cache lỗi để tránh retry liên tục nếu user spam
                    save_report_to_redis(
                        cache_key, str(e), source="weekly_error", is_error=True, wait_sec=120
                    )
                    error_count += 1
                    continue # Bỏ qua user này, sang người kế tiếp

            # === GỬI TIN NHẮN WEB APP ===
            if json_text:
                try:
                    web_app_url = f"{base_url}/report/view/{cache_key}?chat_id={chat_id}"
                    
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("📊 Xem Báo Cáo Tuần", web_app=WebAppInfo(url=web_app_url))
                    ]])
                    
                    now_str = datetime.datetime.now(vn_tz).strftime('%d/%m')
                    msg = (
                        f"🗞 *Báo cáo danh mục tuần {now_str}*\n"
                        f"Phân tích chuyên sâu cho: *{', '.join(symbols)}*\n\n"
                        f"👉 Nhấn nút bên dưới để xem chi tiết nhận định thị trường và cổ phiếu của bạn."
                    )
                    
                    await send_md(tg_app.bot, chat_id, msg, reply_markup=kb)
                    sent_count += 1
                    
                except Exception as e:
                    log.warning(f"{instance_label} Lỗi gửi tin Telegram cho {chat_id}: {e}")

            # Sleep nhẹ để tránh flood
            await asyncio.sleep(2)

        final_msg = f"Hoàn tất Weekly Report — Gửi: {sent_count} | Bỏ qua: {skipped_count} | Lỗi: {error_count}."
        log.info(f"{instance_label} {final_msg}")
        
        if admin_chat_id:
            await tg_app.bot.send_message(admin_chat_id, f"✅ {final_msg}")

    except Exception as e:
        log.error(f"{instance_label} Lỗi tổng quát: {e}")
        if admin_chat_id:
            try:
                await tg_app.bot.send_message(admin_chat_id, f"❌ Lỗi tổng quát Weekly: {e}")
            except: pass

async def weekly_report_loop():
    """
    Gửi báo cáo danh mục vào 09:00 sáng Chủ Nhật.
    Đã fix lỗi gửi kép bằng cách kiểm tra ngày chạy (_last_weekly_run_date).
    """
    global _last_weekly_run_date
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    log.info(f"[{INSTANCE_ID}][WEEKLY] Khởi động loop báo cáo tuần (09:00 CN).")

    while True:
        loop_id += 1
        # 1. Tính thời gian ngủ tới 09:00 CN tiếp theo
        wait_sec = seconds_until_next_weekly_report()
        
        # Log nhẹ để biết bao lâu nữa chạy
        next_run_dt = datetime.datetime.now(vn_tz) + datetime.timedelta(seconds=wait_sec)
        log.info(f"[{INSTANCE_ID}][WEEKLY {loop_id}] Ngủ {wait_sec/3600:.1f}h tới {next_run_dt.strftime('%H:%M %d/%m')}")
        
        await asyncio.sleep(wait_sec)

        # 2. Thức dậy! Kiểm tra điều kiện
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][WEEKLY] Thức dậy nhưng Bot TẮT. Bỏ qua.")
            await asyncio.sleep(60)
            continue

        now = datetime.datetime.now(vn_tz)
        today_str = now.strftime("%Y-%m-%d")

        # Điều kiện 1: Phải là Chủ Nhật (0=T2, 6=CN)
        if now.weekday() != 6:
            log.warning(f"[{INSTANCE_ID}][WEEKLY] Thức dậy nhưng không phải Chủ Nhật? (Now: {now})")
            await asyncio.sleep(60)
            continue

        # Điều kiện 2: Phải đúng giờ (trong khoảng 09:00 - 09:59) để tránh chạy sai giờ
        if now.hour != 9:
             log.warning(f"[{INSTANCE_ID}][WEEKLY] Thức dậy nhưng sai giờ (Now: {now.hour}h). Bỏ qua.")
             await asyncio.sleep(60)
             continue

        # 🔥 [FIX LỖI GỬI KÉP]: Kiểm tra xem hôm nay đã chạy chưa?
        if _last_weekly_run_date == today_str:
            log.info(f"[{INSTANCE_ID}][WEEKLY] Hôm nay ({today_str}) đã chạy rồi. Không chạy lại.")
            # Ngủ 1 tiếng để chắc chắn qua khỏi khung giờ 9h
            await asyncio.sleep(3600)
            continue

        # 3. Chạy báo cáo
        try:
            log.info(f"[{INSTANCE_ID}][WEEKLY] 🚀 Bắt đầu chạy Weekly Report cho ngày {today_str}...")
            
            # Gọi hàm thực thi
            await execute_weekly_report(admin_update=None)
            
            # ✅ Đánh dấu là đã chạy hôm nay
            _last_weekly_run_date = today_str
            
        except Exception as e:
            log.error(f"[{INSTANCE_ID}][WEEKLY] ❌ Lỗi nghiêm trọng: {e}")
            await asyncio.sleep(300)

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
# 1. LOOP TIN CHUYÊN NGÀNH (Sửa đổi: Quét 06:00 & 18:00)
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
        if not BOT_ACTIVE:
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
        if not BOT_ACTIVE:
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


# ===== VN30F1M realtime alert (no Redis) =====================================
# Yêu cầu: mốc di động ±5 điểm, 15s/lần, gửi có thông báo, chiều dùng mốc sáng,
# reset theo ngày mới; user có thể bật/tắt, admin quản qua BOT_ACTIVE.

# --- Tham số riêng
VN30F1M_SYMBOL = "VN30F1M"
VN30F1M_DELTA_THRESHOLD = 5    # ±5 điểm
VN30F1M_TICK_SECONDS = 10        # chu kỳ quét

# --- State trong RAM
_vn30f1m_anchor: float | None = None      # ❗️ SỬA: Mốc di động (giá của lần alert cuối)
_vn30f1m_ref_price: float | None = None   # Mốc cố định (Giá Tham Chiếu để hiển thị tin nhắn)
_vn30f1m_date: datetime.date | None = None
_vn30f1m_enabled_cache: set[int] = set()  # tập chat_id đang bật

# --- [TỐI ƯU] Thêm các biến dùng chung cho 3 tác vụ ---
_vn30f1m_broadcast_queue = asyncio.Queue()
_vn30f1m_current_price_cache: float | None = None # ❗️ SỬA: Chỉ lưu giá live
quote_vn30f1m = Quote(symbol=VN30F1M_SYMBOL, source="vci") # Khởi tạo 1 lần

def ensure_bot_user_settings_table():
    """Bảng dùng chung cho toàn project, lưu settings dạng JSONB."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_user_settings (
                    chat_id     BIGINT PRIMARY KEY,
                    settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()

def reload_vn30f1m_enabled_cache():
    """Load lại tập chat_id đang bật nhận tin từ DB vào RAM."""
    global _vn30f1m_enabled_cache
    mp = get_vn30f1m_enabled_map()
    _vn30f1m_enabled_cache = {cid for cid, en in mp.items() if en}

def _vn30f1m_reset_if_new_day(now: datetime.datetime):
    """Đầu ngày mới: reset anchor và ref_price."""
    global _vn30f1m_date, _vn30f1m_anchor, _vn30f1m_ref_price
    if (_vn30f1m_date is None) or (now.date() != _vn30f1m_date):
        _vn30f1m_date = now.date()
        _vn30f1m_anchor = None 
        _vn30f1m_ref_price = None
        log.info(f"[VN30F1M] New trading day: {_vn30f1m_date}. Reset anchors.")

def _vn30f1m_clear_after_close():
    """Cuối ngày: xóa sạch state trong RAM (không dùng Redis)."""
    global _vn30f1m_anchor
    if _vn30f1m_anchor is not None:
        log.info("[VN30F1M] End of day → clear in-memory anchor.")
    _vn30f1m_anchor = None # ❗️ SỬA

def vn30f1m_day_healthcheck():
    """
    Health-check nhẹ đầu ngày theo đường ống bạn đã test:
    from vnstock import Quote; Quote(symbol="VN30F1M", source="vci").history(..., "1D")
    """
    try:
        today = datetime.datetime.now(pytz.timezone(TIMEZONE)).date()
        start = (today - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        end   = today.strftime("%Y-%m-%d")
        q = Quote(symbol=VN30F1M_SYMBOL, source="vci")
        df = q.history(start=start, end=end, interval="1D")
        if df is None or len(df) == 0:
            log.warning("[VN30F1M] LIVE_PROBE 1D empty.")
            return
        last_close = float(df.iloc[-1]["close"])
        log.info(f"[VN30F1M] LIVE_PROBE 1D last close ~ {last_close}")
    except Exception as e:
        log.warning(f"[VN30F1M] LIVE_PROBE error: {e}")

async def _vn30f1m_process_tick(price: float):
    global _vn30f1m_anchor, _vn30f1m_ref_price

    # 1. KIỂM TRA MỐC
    if _vn30f1m_anchor is None or _vn30f1m_ref_price is None:
        return 

    # 2. TÍNH TOÁN BIẾN ĐỘNG CHO TRIGGER (Dựa trên Anchor di động)
    delta_trigger = float(price) - float(_vn30f1m_anchor)
    
    # 3. KIỂM TRA ĐIỀU KIỆN TRIGGER (>= 5 điểm so với mốc gần nhất)
    if abs(delta_trigger) >= VN30F1M_DELTA_THRESHOLD:
        
        # 4. TÍNH TOÁN HIỂN THỊ (Dựa trên Giá Tham Chiếu cố định)
        delta_display = float(price) - float(_vn30f1m_ref_price)
        
        direction = "tăng" if delta_display > 0 else "giảm"
        icon = "🟢" if delta_display > 0 else "🔴"
        trend_icon = "🚀" if delta_display > 0 else "📉"
        
        now_str = datetime.datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")

        # Nội dung tin nhắn: Hiển thị số điểm so với Tham Chiếu
        text = (
            f"{icon} *VN30F1M {direction} {abs(delta_display):.1f} điểm*\n"
            f"Giá hiện tại: *{float(price):.1f}*\n"
            f"(So với TC: {_vn30f1m_ref_price:.1f})\n"
            f"{trend_icon} _Cập nhật lúc {now_str}_"
        )
        
        try:
            _vn30f1m_broadcast_queue.put_nowait(text)
            log.info(f"[VN30F1M] 🔔 ALERT: Trigger {abs(delta_trigger):.1f}pts. Display {abs(delta_display):.1f}pts (TC). New Anchor: {price}")
        except asyncio.QueueFull:
            pass

        # 5. CẬP NHẬT MỐC ANCHOR MỚI
        _vn30f1m_anchor = float(price)

async def vn30f1m_alert_loop():
    """
    (Tác vụ 2: Ticker - 5 giây)
    - Loop này KHÔNG gọi API (chỉ đọc cache).
    - KHÔNG gửi tin (chỉ đẩy vào queue).
    """
    ensure_bot_user_settings_table()
    reload_vn30f1m_enabled_cache()
    log.info(f"[VN30F1M][TICKER] Bắt đầu. Users đang bật: {len(_vn30f1m_enabled_cache)}")
    
    vn_tz = pytz.timezone(TIMEZONE)
    loop_count = 0 # Thêm biến đếm

    while True:

        loop_start = datetime.datetime.now(vn_tz)
        try:
            # 1. Kiểm tra điều kiện chạy
            if not BOT_ACTIVE:
                await asyncio.sleep(30)
                continue
            if not in_session_vietnam():
                await asyncio.sleep(60)
                continue

            now = loop_start
            _vn30f1m_reset_if_new_day(now)

            # --- HEARTBEAT LOG (MỚI): Mỗi 20 vòng (khoảng 60s) ---
            if loop_count % 20 == 0:
                current_p = _vn30f1m_current_price_cache
                log.info(f"[VN30F1M][TICKER] 💓 Heartbeat: Giá hiện tại={current_p}, Anchor={_vn30f1m_anchor}. Bot vẫn sống.")

            if not BOT_ACTIVE:
                await asyncio.sleep(30)
                continue

            if not in_session_vietnam():
                _vn30f1m_clear_after_close() # Clear mốc neo khi hết phiên
                
                now = datetime.datetime.now(vn_tz)
                
                # SỬ DỤNG HÀM next_session_start
                next_open = next_session_start(now)
                
                sleep_seconds = max(5, (next_open - now).total_seconds())
                log.info(f"[{INSTANCE_ID}][TICKER] Ngoài giờ. Ngủ tới {next_open.strftime('%H:%M')} ({int(sleep_seconds)}s)")
                await asyncio.sleep(sleep_seconds)
                continue
                
            price = _vn30f1m_current_price_cache
            
            if price is None:
                await asyncio.sleep(VN30F1M_TICK_SECONDS)
                continue

            # Gọi hàm xử lý (cực nhanh)
            await _vn30f1m_process_tick(float(price))

        except asyncio.CancelledError:
            log.info("[VN30F1M][TICKER] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[VN30F1M][TICKER] Lỗi: {e}")
        
        # Giữ nhịp 5 giây chính xác
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(0.1, VN30F1M_TICK_SECONDS - elapsed)
        await asyncio.sleep(delay)

async def vn30f1m_price_fetcher_loop():
    """
    (Tác vụ 1: Fetcher VN30F1M - Hybrid Mode)
    - Giá Hiện tại: Dùng Quote History 1m (Chính xác realtime, không bị delay/cache).
    - Giá Tham chiếu: Dùng Price Board (Chính xác theo sàn).
    - Anchor khởi tạo: LUÔN LẤY GIÁ THAM CHIẾU (nếu có) để so sánh nhịp đầu tiên.
    """
    global _vn30f1m_current_price_cache, _vn30f1m_anchor, _vn30f1m_ref_price
    global stock_trading # Sử dụng trading object toàn cục để gọi price_board
    
    vn_tz = pytz.timezone(TIMEZONE)
    FETCH_INTERVAL = 5
    
    log.info(f"[{INSTANCE_ID}][VN30F1M] Bắt đầu (Hybrid: Quote 1m + Board Ref)...")

    while True:
        loop_start = datetime.datetime.now(vn_tz)
        try:
            if not BOT_ACTIVE:
                await asyncio.sleep(30)
                continue
                
            if not in_session_vietnam():
                now = datetime.datetime.now(vn_tz)
                
                # SỬ DỤNG HÀM next_session_start ĐỂ TỰ TÌM 13:00 HOẶC 09:15
                next_open = next_session_start(now)
                
                sleep_seconds = max(5, (next_open - now).total_seconds())
                log.info(f"[{INSTANCE_ID}][VN30F1M] Ngoài giờ (Nghỉ trưa/Tối). Ngủ tới {next_open.strftime('%H:%M')} ({int(sleep_seconds)}s)")
                await asyncio.sleep(sleep_seconds)
                continue

            today_str = loop_start.strftime('%Y-%m-%d')
            
            def _fetch_hybrid():
                # 1. Lấy Giá Hiện Tại từ Quote History (Nến 1 phút) - Rất nhạy
                q = Quote(symbol=VN30F1M_SYMBOL, source='VCI')
                df_now = q.history(start=today_str, end=today_str, interval='1m')
                
                price_now = None
                if df_now is not None and not df_now.empty:
                    price_now = float(df_now.iloc[-1]['close'])
                
                # 2. Lấy Giá Tham Chiếu từ Price Board (Nếu chưa có) - Rất chuẩn
                price_ref = None
                if _vn30f1m_ref_price is None and stock_trading:
                    try:
                        # Gọi price_board chỉ để lấy Ref (nhẹ)
                        pb_df = stock_trading.price_board([VN30F1M_SYMBOL])
                        if pb_df is not None and not pb_df.empty:
                            row = pb_df.iloc[0]
                            val = row.get(('listing', 'ref_price')) or row.get('ref_price')
                            if val:
                                price_ref = float(val)
                    except Exception as e:
                        log.warning(f"[VN30F1M] Lỗi lấy Ref từ Board: {e}")

                return price_now, price_ref

            # Chạy trong thread
            current_p, ref_p = await asyncio.to_thread(_fetch_hybrid)

            # Cập nhật Cache
            if current_p is not None:
                _vn30f1m_current_price_cache = current_p
                
                # Cập nhật Tham Chiếu (ưu tiên lấy từ Board)
                if _vn30f1m_ref_price is None and ref_p is not None:
                    _vn30f1m_ref_price = ref_p
                    log.info(f"[{INSTANCE_ID}][VN30F1M] ⛳ Ref Price Set (PriceBoard): {ref_p}")
                
                # --- [THAY ĐỔI QUAN TRỌNG TẠI ĐÂY] ---
                # Anchor khởi tạo: Ưu tiên lấy REF PRICE
                if _vn30f1m_anchor is None:
                    if _vn30f1m_ref_price is not None:
                        _vn30f1m_anchor = _vn30f1m_ref_price
                        log.info(f"[{INSTANCE_ID}][VN30F1M] ⚓ Anchor khởi tạo theo THAM CHIẾU: {_vn30f1m_anchor}")
                    else:
                        # Fallback: Nếu chưa lấy được tham chiếu thì mới dùng giá hiện tại
                        _vn30f1m_anchor = current_p
                        log.info(f"[{INSTANCE_ID}][VN30F1M] ⚓ Anchor khởi tạo theo GIÁ HIỆN TẠI (do thiếu Ref): {_vn30f1m_anchor}")

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][VN30F1M] Lỗi Loop: {e}")
            await asyncio.sleep(5)
        
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(1, FETCH_INTERVAL - elapsed)
        await asyncio.sleep(delay)

async def vn30f1m_broadcast_loop():
    """
    (Tác vụ 3: Broadcaster VN30)
    - Đã thêm check BOT_ACTIVE.
    """
    log.info("[VN30F1M][BCASTER] Bắt đầu. Chờ tin nhắn trong queue...")
    
    while True:
        try:
            text = await _vn30f1m_broadcast_queue.get()
            
            # 🔥 CHẶN TẠI ĐÂY
            if not BOT_ACTIVE:
                _vn30f1m_broadcast_queue.task_done()
                continue

            if not text:
                _vn30f1m_broadcast_queue.task_done()
                continue

            # Logic gửi tin
            tasks = []
            for cid in list(_vn30f1m_enabled_cache):
                tasks.append(send_md(tg_app.bot, cid, text, msg_type="VN30_ALERT"))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            _vn30f1m_broadcast_queue.task_done()

        except asyncio.CancelledError:
            log.info("[VN30F1M][BCASTER] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[VN30F1M][BCASTER] Lỗi: {e}")
            try: _vn30f1m_broadcast_queue.task_done()
            except: pass

#-------------------------------


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

        if not BOT_ACTIVE:
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

        if not BOT_ACTIVE:
            log.info(f"{loop_label} Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue
            
        try:
            # ... (Logic chờ 7:00 sáng giữ nguyên) ...
            target_7am = get_next_7am(now, vn_tz)
            log.info(f"{loop_label} Đang chờ cho đến {target_7am.strftime('%Y-%m-%d %H:%M')}.")
            await sleep_until(target_7am, vn_tz)
            
            if not BOT_ACTIVE:
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
                if not BOT_ACTIVE: 
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

async def send_digest_with_pin(bot, chat_id: int, text: str, reply_markup):
    """
    [UPDATED] Sáng: Tháo ghim TOÀN BỘ tin cũ (Digest + EOD) -> Gửi tin mới -> Ghim tin mới.
    Dùng unpin_all_chat_messages để đảm bảo không còn tin rác cũ đọng lại.
    """
    # 1. Tháo ghim TOÀN BỘ các tin cũ trong chat để làm sạch bảng tin
    try:
        # Lệnh này sẽ gỡ ghim Digest cũ, EOD cũ và mọi tin ghim khác
        await bot.unpin_all_chat_messages(chat_id=chat_id)
    except Exception as e:
        # Bỏ qua lỗi nếu chat chưa có tin ghim nào hoặc bot không đủ quyền (hiếm gặp)
        # log.warning(f"[{INSTANCE_ID}] Lỗi unpin_all cho {chat_id}: {e}")
        pass

    # 2. Gửi tin mới (Lưu ý: msg_type='DAILY_DIGEST')
    msg = await send_md(bot, chat_id, text, msg_type='DAILY_DIGEST', reply_markup=reply_markup)

    # 3. Ghim tin mới
    if msg:
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True 
            )
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}] Lỗi ghim Digest mới cho {chat_id}: {e}")

async def daily_user_digest_loop():
    """
    Gửi bản tin tổng hợp (Digest) 7:00 sáng.
    (ĐÃ TỐI ƯU: Không tính toán nặng nữa, chỉ lấy từ Redis do Nightly Loop đã làm)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1
        loop_label = f"[{INSTANCE_ID}][USER_DIGEST {loop_id}]"
        now_local = datetime.datetime.now(vn_tz)

        if not BOT_ACTIVE:
            log.info(f"{loop_label} Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        # 1️⃣ Đợi tới 07:00 sáng
        target_7am = get_next_7am(now_local, vn_tz)
        log.info(f"{loop_label} Đang chờ đến {target_7am.strftime('%Y-%m-%d %H:%M')} (VN).")
        await sleep_until(target_7am, vn_tz)

        if not BOT_ACTIVE: continue
        
        log.info(f"{loop_label} 07:00! Bắt đầu quy trình Digest...")

        # 🔥 [THAY ĐỔI]: KHÔNG tính toán ở đây nữa.
        # Nightly Loop (02:00) đã làm việc này và lưu vào Redis rồi.
        # Hàm get_top_mean_reversion_stocks bên dưới sẽ tự đọc Redis.
        
        log.info(f"{loop_label} Bắt đầu thu thập dữ liệu gửi đi...")

        # 2️⃣ Thu thập dữ liệu (Song song)
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            since_utc = now_utc - datetime.timedelta(hours=24)

            (
                bctc_rows,
                report_rows,
                macro_rows,
                spec_rows,
                all_watch,
                pro_chat_ids,
                top_value_stocks,
            ) = await asyncio.gather(
                asyncio.to_thread(get_recent_bctc_notified, since_utc),
                asyncio.to_thread(get_recent_analysis_reports, since_utc),
                asyncio.to_thread(get_recent_news_seen, "MACRO", since_utc),
                asyncio.to_thread(get_recent_news_seen, "SPECIALIZED", since_utc),
                asyncio.to_thread(get_all_watch),
                asyncio.to_thread(get_all_pro_chat_ids),
                get_top_mean_reversion_stocks(limit=5), # <--- Hàm này sẽ lấy cache Redis đêm qua
            )
            
        except Exception as e:
            log.error(f"{loop_label} Lỗi nghiêm trọng khi gather data: {e}")
            await asyncio.sleep(600) 
            continue

        # ... (PHẦN CÒN LẠI CỦA HÀM GIỮ NGUYÊN KHÔNG ĐỔI) ...
        # ... (Logic AI News, Payload, Gửi tin...)
        
        # ============================================================
        # 🔥 XỬ LÝ TIN TỨC AI
        # ============================================================
        ai_news_data = None
        ai_telegram_text = ""

        all_news_items = []
        for row in macro_rows: all_news_items.append({"title": row[0], "link": row[1], "source": "Vĩ mô"})
        for row in spec_rows: all_news_items.append({"title": row[0], "link": row[1], "source": "Doanh nghiệp"})

        if all_news_items:
            log.info(f"{loop_label} 🤖 Đang gọi AI tóm tắt {len(all_news_items)} tin tức...")
            try:
                ai_news_data = await summarize_daily_news_with_ai(all_news_items)
                if ai_news_data:
                    lines = []
                    if ai_news_data.get('headline'):
                        lines.append("⚡ *TIÊU ĐIỂM*")
                        for item in ai_news_data['headline']:
                            lines.append(f"• {item['text']}")
                    if ai_news_data.get('comment'):
                        lines.append(f"\n🧠 *AI:* {ai_news_data['comment']}")
                    ai_telegram_text = "\n".join(lines)
            except Exception as e:
                log.error(f"{loop_label} Lỗi AI Summary: {e}")

        if not ai_telegram_text:
            ai_telegram_text = "_Hôm nay thị trường khá yên ắng, chưa có tin nổi bật._"

        # ============================================================
        # XỬ LÝ DỮ LIỆU KHÁC
        # ============================================================

        bctc_by_sym = {str(sym).upper(): (y, q, t) for (sym, y, q, t) in bctc_rows}
        reports_by_sym = {}
        for (s, t, l, p, c) in report_rows: reports_by_sym.setdefault(str(s).upper(), []).append((t, l, p))
        
        watch_to_chats = {}
        for chat_key, user_block in all_watch.items():
            try: chat_id = int(chat_key)
            except: continue
            for sym in user_block.get("list", []) or []:
                watch_to_chats.setdefault(str(sym).upper().strip(), []).append(chat_id)

        digest_payloads = {}
        def _get_payload(cid):
            if cid not in digest_payloads:
                digest_payloads[cid] = {
                    "is_pro": (cid in pro_chat_ids or cid == ADMIN_ID),
                    "ai_news": ai_news_data, 
                    "value_stocks": [], "bctc": [], "reports": []
                }
            return digest_payloads[cid]

        # 🔥 [FIX 2]: KHỞI TẠO PAYLOAD CHO TẤT CẢ USER CÓ WATCHLIST
        # Để đảm bảo dù Screener timeout hay không có BCTC, họ vẫn nhận được Tin Tức AI
        if all_watch:
            for chat_key in all_watch.keys():
                try:
                    cid = int(chat_key)
                    _get_payload(cid) # Gọi hàm này để init dict cho user
                except: continue

        # Fill dữ liệu chi tiết
        if top_value_stocks:
            for cid in all_watch.keys():
                try: cid = int(cid)
                except: continue
                if cid in pro_chat_ids or cid == ADMIN_ID: 
                    _get_payload(cid)["value_stocks"] = top_value_stocks

        if bctc_rows:
            for sym, (y, q, t) in bctc_by_sym.items():
                t_str = t.astimezone(vn_tz).strftime("%H:%M %d/%m")
                for cid in watch_to_chats.get(sym, []):
                    pl = _get_payload(cid)
                    is_locked = not pl["is_pro"]
                    if not is_locked or not any(x['symbol'] == sym for x in pl["bctc"]):
                        pl["bctc"].append({"symbol": sym, "year": y, "quarter": q, "time": t_str, "is_locked": is_locked})

        if report_rows:
            for sym, r_list in reports_by_sym.items():
                for cid in watch_to_chats.get(sym, []):
                    pl = _get_payload(cid)
                    last = r_list[0]; t_str = last[2].astimezone(vn_tz).strftime("%H:%M %d/%m") if last[2] else ""
                    if pl["is_pro"]:
                        for (title, link, pub) in r_list:
                            ts = pub.astimezone(vn_tz).strftime("%H:%M %d/%m") if pub else ""
                            pl["reports"].append({"symbol": sym, "title": title, "link": link, "time": ts, "is_locked": False})
                    else:
                        if not any(x['symbol'] == sym for x in pl["reports"]):
                            pl["reports"].append({"symbol": sym, "title": "Báo cáo phân tích (Pro)", "link": "#", "time": t_str, "is_locked": True})

        # 4️⃣ GỬI TIN NHẮN
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        tasks = []
        sent_count = 0

        for chat_id, data in digest_payloads.items():
            try:
                digest_id = uuid.uuid4().hex
                await asyncio.to_thread(save_digest_to_redis, digest_id, data)
                web_app_url = f"{base_url}/digest/{digest_id}"
                
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(text="📰 Xem Chi Tiết (Web App) 🚀", web_app=WebAppInfo(url=web_app_url))]])
                
                msg_text = (
                    f"🌅 *BẢN TIN SÁNG {now_local.strftime('%d/%m')}* 🤖\n\n"
                    f"{ai_telegram_text}\n\n"
                    f"👉 *Nhấn nút dưới để xem chi tiết danh mục của bạn!*"
                )
                
                tasks.append(send_digest_with_pin(tg_app.bot, chat_id, msg_text, kb))
                sent_count += 1
            except Exception as e:
                log.warning(f"{loop_label} Lỗi tạo task gửi cho {chat_id}: {e}")
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
        log.info(f"{loop_label} Hoàn tất gửi Digest cho {sent_count} user.")

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
        if not BOT_ACTIVE:
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
    Nhắc admin vào ngày 7 hằng tháng về việc backup/restore DB.

    Logic:
    - Nếu tháng hiện tại CHƯA có record /restore_core (get_last_restore_month != current YYYY-MM):
        · Từ ngày 7 trở đi: nhắc admin.
        · Lần nhắc đầu tiên mỗi tháng:
            + Tự động backup core data và gửi file JSON cho admin.
            + Gửi hướng dẫn chi tiết các bước đổi DB + restore.
        · Sau lần đầu: cứ mỗi 1 giờ nhắc lại 1 lần (tin nhắn ngắn, có thể để silent).
    - Khi admin chạy /restore_core thành công:
        · mark_restore_done_now() -> tháng đó coi như đã xong, loop ngừng nhắc.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    last_reminder_at = None  # datetime trong timezone VN
    full_msg_sent_for_month = None  # 'YYYY-MM' đã gửi hướng dẫn + auto backup

    while True:
        loop_id += 1

        # Nếu chưa set ADMIN_ID => không làm gì
        if ADMIN_ID is None:
            await asyncio.sleep(3600)
            continue

        now = datetime.datetime.now(vn_tz)
        month_key = now.strftime("%Y-%m")

        # Lấy tháng gần nhất đã restore từ DB (blocking -> chạy trong thread)
        try:
            last_restore_month = await asyncio.to_thread(get_last_restore_month)
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][RESTORE_REMIND {loop_id}] Lỗi get_last_restore_month: {e}")
            last_restore_month = None

        needs_restore = (last_restore_month != month_key)

        # Nếu tháng này đã restore rồi -> reset state, ngủ tiếp 1h
        if not needs_restore:
            full_msg_sent_for_month = None
            last_reminder_at = None
            await asyncio.sleep(3600)
            continue

        # Chưa restore, nhưng chưa tới ngày 7 => ngủ 1h
        if now.day < 7:
            await asyncio.sleep(3600)
            continue

        # Từ ngày 7 trở đi và chưa restore:
        # Đảm bảo tối thiểu 1h giữa các lần nhắc
        if last_reminder_at is not None:
            delta = (now - last_reminder_at).total_seconds()
            if delta < 3600:
                await asyncio.sleep(300)  # check lại sau 5 phút
                continue

        try:
            # Lần nhắc ĐẦU TIÊN của tháng -> auto backup + hướng dẫn chi tiết
            if full_msg_sent_for_month != month_key:
                # 1) Export core data
                payload = await asyncio.to_thread(export_core_data)

                # 2) Lưu file tạm
                ts = now.strftime("%Y%m%d_%H%M%S")
                filename = f"stockbot_core_backup_{month_key}_{ts}.json"
                tmp_path = os.path.join(TMP_DIR, filename)
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

                # 3) Gửi file backup auto
                await tg_app.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=open(tmp_path, "rb"),
                    filename=filename,
                    caption=(
                        f"📦 Auto backup dữ liệu core tháng {month_key} (lúc {ts}).\n"
                        "- Bao gồm: bot_watch, news_pref, bot_config, bctc_notified.\n"
                        "- Dùng file này cho /restore_core sau khi bạn tạo DB Postgres mới trên Render."
                    ),
                )

                # 4) Gửi hướng dẫn chi tiết
                instructions = (
                    f"⚠️ *Nhắc định kỳ ngày 7 hằng tháng: KIỂM TRA RESTORE DATABASE*\n\n"
                    f"Hiện tại bot *chưa ghi nhận* lần chạy lệnh `/restore_core` cho tháng *{month_key}*.\n\n"
                    "👉 Quy trình đề xuất để đổi database Render (bản free ~30 ngày):\n\n"
                    "1️⃣ *Export env từ database cũ trước và tạo database Postgres mới trên Render*\n"
                    "   • Vào trang Render → Postgres → New Database.\n\n"
                    "2️⃣ *Cập nhật biến môi trường `DATABASE_URL` cho web service*\n"
                    "   • Mở web service của bot trên Render.\n"
                    "   • Vào tab Environment.\n"
                    "   • Dán Internal Database URL của DB mới vào biến `DATABASE_URL`.\n"
                    "   • Bấm Save Changes.\n\n"
                    "3️⃣ *Restart / redeploy web service*\n"
                    "   • Bấm Manual Deploy → Clear build cache (hoặc Restart service).\n"
                    "   • Chờ bot khởi động lại (log có dòng đã set webhook).\n\n"
                    "4️⃣ *Restore dữ liệu core từ file backup*\n"
                    "   • Dùng file backup `.json` (auto backup ngày hôm nay hoặc tạo mới bằng lệnh `/backup_core`).\n"
                    "   • Gửi file đó cho bot, trong phần caption gõ: `/restore_core`.\n"
                    "   • Hoặc: gửi file trước, rồi reply `/restore_core` vào tin nhắn chứa file.\n"
                    "   • Bot sẽ khôi phục: watchlist của user, cài đặt tin tức, cấu hình bot, trạng thái BCTC đã thông báo.\n\n"
                    f"✅ Sau khi hoàn tất bước 4, bot sẽ coi *tháng {month_key}* đã restore xong và *dừng nhắc mỗi giờ*.\n"
                )

                await send_md(
                    tg_app.bot,
                    ADMIN_ID,
                    instructions,
                )

                full_msg_sent_for_month = month_key

            else:
                # Các lần nhắc tiếp theo trong tháng: tin nhắn ngắn, gửi silent cho đỡ ồn
                short_msg = (
                    f"⏰ Nhắc lại: Tháng {month_key} bạn vẫn *chưa chạy* `/restore_core`.\n"
                    "Khi nào rảnh hãy:\n"
                    "1) Tạo DB Postgres mới trên Render,\n"
                    "2) Cập nhật `DATABASE_URL` của web service,\n"
                    "3) Restart service,\n"
                    "4) Gửi file backup `.json` kèm caption `/restore_core` để khôi phục dữ liệu core."
                )
                await send_md(
                    tg_app.bot,
                    ADMIN_ID,
                    short_msg,
                    disable_notification=True,  # nhắc im lặng để đỡ phiền
                )

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][RESTORE_REMIND {loop_id}] Lỗi khi nhắc admin: {e}")

        last_reminder_at = datetime.datetime.now(vn_tz)
        await asyncio.sleep(300)  # sau 5 phút check lại


def clean_html_text(raw: str) -> str:
    """
    Làm sạch text lấy từ RSS:
    - Giải mã entity HTML (&#xxx; → ký tự, &amp; → & ...)
    - Sửa luôn trường hợp thiếu '&' (Ng#224;nh → Ngành)
    - Bỏ tag HTML
    - Gom khoảng trắng
    """
    if not raw:
        return ""

    text = str(raw)

    # 1) Giải mã entity chuẩn: &#224; → à, &amp; → &
    text = html.unescape(text)

    # 2) Sửa dạng thiếu '&': Ng#224;nh → Ngành
    def fix_num_entity(m: re.Match) -> str:
        try:
            code = int(m.group(1))
            return chr(code)
        except Exception:
            return m.group(0)

    text = re.sub(r"#(\d+);", fix_num_entity, text)

    # 3) Bỏ thẻ HTML (<p>, <br>, <strong>...)
    text = re.sub(r"<[^>]+>", " ", text)

    # 4) Gom khoảng trắng và trim
    text = re.sub(r"\s+", " ", text).strip()

    return text

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


#------------------------------------------------------

def escape_markdown_v2(text: str) -> str:
    """
    Escape tất cả ký tự đặc biệt theo Markdown V2:
    _ * [ ] ( ) ~ ` > # + - = | { } . !
    Dùng được cả với Markdown v1.
    """
    if text is None:
        text = ""
    return re.sub(r'([_\*\[\]\(\)~`>\#\+\-\=\|\{\}\.\!])', r'\\\1', str(text))

async def reply_md(update: Update, text: str, **kwargs):
    """
    Gửi tin nhắn Markdown an toàn.
    Tự động phát hiện context là Message hay CallbackQuery để reply đúng chỗ.
    """
    from telegram.error import BadRequest

    # --- SỬA ĐỔI QUAN TRỌNG ---
    # Dùng update.effective_message thay vì update.message
    # effective_message hoạt động cho cả tin nhắn thường lẫn nút bấm
    message = update.effective_message 
    if not message:
        log.warning("[reply_md] Không tìm thấy message để reply.")
        return
    # --------------------------

    async def _send(raw_text: str):
        return await message.reply_text(
            raw_text,
            parse_mode="Markdown",
            **kwargs,
        )

    try:
        return await _send(text)
    except BadRequest as e:
        msg = str(e)
        logging.warning(f"[Markdown error] {e} | text_len={len(text)}")

        # 1) Nếu lỗi do quá dài -> chia nhỏ
        if "Message is too long" in msg:
            MAX_LEN = 4000

            chunks = []
            remaining = text
            while remaining:
                if len(remaining) <= MAX_LEN:
                    chunks.append(remaining)
                    break

                split_pos = remaining.rfind("\n\n", 0, MAX_LEN)
                if split_pos == -1:
                    split_pos = remaining.rfind("\n", 0, MAX_LEN)
                if split_pos == -1:
                    split_pos = MAX_LEN

                chunks.append(remaining[:split_pos])
                remaining = remaining[split_pos:]

            last_msg = None
            for idx, chunk in enumerate(chunks, start=1):
                try:
                    last_msg = await _send(chunk)
                except BadRequest as e2:
                    safe_chunk = escape_markdown_v2(chunk)
                    last_msg = await _send(safe_chunk)

            return last_msg

        # 2) Các lỗi parse khác -> escape toàn bộ rồi gửi lại
        safe_text = escape_markdown_v2(text)
        return await _send(safe_text)


# Thêm tham số reply_markup vào cuối
def send_msg_to(chat_id: int, text: str, parse_mode: str | None = "Markdown", silent: bool = False, msg_type: str = 'GENERAL', reply_markup: str | None = None):
    """
    Gửi tin nhắn Telegram, có hỗ trợ msg_type và reply_markup (nút bấm).
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    def _do_send(t: str, mode: str | None, silent_flag: bool, r_markup: str | None):
        params = {
            "chat_id": chat_id,
            "text": t,
        }
        if mode:
            params["parse_mode"] = mode
        if silent_flag:
            params["disable_notification"] = True
        
        # --- THÊM ĐOẠN NÀY ---
        if r_markup:
            params["reply_markup"] = r_markup
        # ---------------------

        res = requests.get(url, params=params, timeout=10)
        return res.json()

    try:
        # Lần 1: gửi nguyên văn
        data = _do_send(text, parse_mode, silent, reply_markup) # <--- Truyền reply_markup vào

        # Nếu lỗi do Markdown -> escape và gửi lại
        if (
            not data.get("ok")
            and parse_mode == "Markdown"
            and "description" in data
            and "can't parse entities" in data["description"].lower()
        ):
            safe_text = escape_markdown_v2(text)
            # Lần 2: Retry cũng phải kèm nút bấm
            data = _do_send(safe_text, parse_mode, silent, reply_markup) 

        if data.get("ok") and "result" in data:
            msg_id = data["result"]["message_id"]
            save_bot_message(chat_id, msg_id, msg_type)
        else:
            log.warning(f"[WARN] Telegram send failed: {data}")

    except Exception as e:
        log.warning(f"[WARN] Telegram send error: {e}")

async def auto_on_after_delay(initial_active: bool):
    """
    Tự động bật lại bot sau 2 phút kể từ khi khởi động,
    nếu trạng thái ban đầu là OFF.
    """
    global BOT_ACTIVE, tg_app # 👈 Cần access global tg_app để lấy đối tượng bot

    # Nếu lúc start bot đang ON thì khỏi cần auto /on
    if initial_active:
        return

    await asyncio.sleep(120)  # 2 phút

    # Chỉ auto /on nếu tới lúc này bot vẫn đang OFF
    if BOT_ACTIVE is False:
        BOT_ACTIVE = True
        # Lưu trạng thái vào DB (chạy trong thread)
        await asyncio.to_thread(set_bot_active, True)
        
        log.info(f"[{INSTANCE_ID}] BOT auto switched ON after 2 minutes (initial OFF).")

        if ADMIN_ID:
            try:
                # 👇 ĐÃ SỬA: Thêm tham số 'tg_app.bot' vào đầu
                await send_md(
                    tg_app.bot, 
                    ADMIN_ID,
                    "✅ *Hệ thống đã được kích hoạt trở lại (auto /on sau 2 phút).* \n\n"
                    "Bot hiện đang ở trạng thái *hoạt động bình thường* và sẵn sàng phục vụ người dùng. 🚀"
                )
            except Exception as e:
                log.warning(f"[{INSTANCE_ID}] Lỗi khi gửi thông báo auto /on cho admin: {e}")

def load_seen_news_from_redis(feed_type: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Đọc danh sách bài đã seen từ Redis.
    Giả sử key có dạng: news_seen:MACRO:<hash>, value là JSON {"title": "...", "published": "..."}.
    Nếu value không phải JSON, sẽ fallback dùng raw value làm title.
    """
    ft = (feed_type or "").strip().upper()
    if not ft:
        return []

    pattern = f"news_seen:{ft}:*"
    r = get_redis()

    items: list[dict[str, Any]] = []
    for key in r.scan_iter(pattern, count=200):
        val = r.get(key)
        title = ""
        published_dt = None

        if val:
            try:
                obj = json.loads(val)
                title = obj.get("title") or ""
                pub_str = obj.get("published")
                if pub_str:
                    published_dt = datetime.datetime.fromisoformat(pub_str)
            except Exception:
                # Nếu không phải JSON, dùng raw value làm title
                if isinstance(val, (bytes, bytearray)):
                    title = val.decode("utf-8", errors="ignore")
                else:
                    title = str(val)

        if not published_dt:
            # Nếu không có thời gian, cho về mốc 1970 để sort cho dễ
            published_dt = datetime.datetime(1970, 1, 1)

        items.append(
            {
                "title": title,
                "published": published_dt,
            }
        )

    # Sắp xếp mới nhất trước
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:limit]

# ==============================================
# COMMAND HANDLERS
# ==============================================

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mở Admin Dashboard Web App"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return # Silent ignore

    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    # Tạo link kèm admin_id để xác thực đơn giản
    web_app_url = f"{base_url}/admin/dashboard?admin_id={user_id}"

    kb = [[InlineKeyboardButton("👑 Mở Admin Dashboard", web_app=WebAppInfo(url=web_app_url))]]
    
    await reply_md(update, "👇 Bấm bên dưới để vào trang quản trị:", reply_markup=InlineKeyboardMarkup(kb))

# (Nhớ thêm tg_app.add_handler(CommandHandler("admin", cmd_admin)) vào main)

# alert_bot.py

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Dashboard chính (Logic thông minh: Ẩn Trial nếu đã dùng/Pro/Admin).
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return
    
    await track_user_activity(update)
    chat_id = update.effective_chat.id
    
    # 1. Init watchlist nếu chưa có
    try:
        await asyncio.to_thread(log_command_usage, chat_id, "/start", ADMIN_ID)
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        if lst is None: await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])
    except: pass

    # 2. Kiểm tra trạng thái Trial/Pro
    # Hàm check_trial_eligibility trả về: 'OK' (Được dùng), 'IS_PRO', 'USED'
    trial_status = await asyncio.to_thread(check_trial_eligibility, chat_id)

    # Logic hiển thị: Chỉ hiện Trial nếu user ĐỦ ĐIỀU KIỆN ('OK')
    # Nếu là Admin thì cũng coi như không hiện (để giao diện gọn, hoặc tùy bạn)
    is_admin = (chat_id == ADMIN_ID)
    show_trial = (trial_status == 'OK') and not is_admin

    # --- MENU DASHBOARD CƠ BẢN ---
    kb = [
        [
            InlineKeyboardButton("📋 Danh mục", callback_data="menu_list"),
            InlineKeyboardButton("➕ Thêm mã", callback_data="menu_add")
        ],
        [
            InlineKeyboardButton("📄 Soi hồ sơ", callback_data="menu_info"),
            InlineKeyboardButton("💎 Lọc Cổ Phiếu", callback_data="menu_screener")
        ],
        [
            InlineKeyboardButton("📊 AI Report", callback_data="menu_report"),
            InlineKeyboardButton("⚙️ Tài khoản", callback_data="menu_setting")
        ]
    ]

    # Nếu đủ điều kiện Trial -> Thêm nút Kích hoạt
    if show_trial:
        kb.append([InlineKeyboardButton("🎁 Kích hoạt Dùng thử (Free)", callback_data="btn_trial_click")])
    
    # Thêm nút Hướng dẫn cuối cùng
    kb.append([InlineKeyboardButton("❓ Hướng dẫn", callback_data="menu_help")])

    # --- NỘI DUNG TIN NHẮN ---
    if show_trial:
        # Dành cho User Mới (Chưa dùng thử) -> Có chào mời
        welcome_msg = (
            "👋 *Chào bạn! Mình là Người Canh Bảng 🧑‍💻*\n"
            "Trợ lý đầu tư chứng khoán thông minh 24/7.\n\n"
            "🚀 **Tôi giúp gì cho bạn?**\n"
            "• **Báo tín hiệu:** Cảnh báo giá cổ phiếu và chỉ số realtime.\n"
            "• **Soi danh mục & Định giá:** Phân tích doanh nghiệp trong 5s.\n"
            "• **Sàng lọc:** Tìm cổ phiếu Rẻ/Đắt tự động.\n"
            "• **Báo cáo Tự động:** Gửi bản tin Sáng (7h), Chiều (15h) & Tuần (CN).\n\n"
            "🎁 **Tặng bạn 10 ngày dùng thử Full tính năng Pro!**\n"
            "Bấm nút **'🎁 Kích hoạt Dùng thử'** bên dưới để nhận ngay.\n\n"
            "----------------\n\n"
            "⚖️ *Người Canh Bảng* 🧑‍💻 _là công cụ hỗ trợ dữ liệu. Mọi thông tin chỉ mang tính tham khảo, nhà đầu tư tự chịu trách nhiệm với quyết định của mình._"
        )
    else:
        # Dành cho User Cũ / Pro / Admin -> Gọn gàng, đi thẳng vào vấn đề
        welcome_msg = (
        "⚖️ *Người Canh Bảng* 🧑‍💻 _là công cụ hỗ trợ dữ liệu. Mọi thông tin chỉ mang tính tham khảo, nhà đầu tư tự chịu trách nhiệm với quyết định của mình._\n\n"
        "👇 *Chọn nhanh tính năng bên dưới:*"
    )

    await reply_md(update, welcome_msg, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ NÂNG CẤP UX) Hướng dẫn sử dụng gọn gàng & Nút điều hướng """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    try:
        await asyncio.to_thread(log_command_usage, chat_id, "/help", ADMIN_ID)
    except: pass

    help_text = (
        "📘 ***HƯỚNG DẪN SỬ DỤNG NHANH***\n\n"
        
        "1️⃣ ***Quản lý Danh mục (Cực nhanh)***\n"
        "• Gõ mã 3 chữ cái (VD: `HPG`, `FPT`) vào chat để thêm nhanh hoặc Soi hồ sơ.\n"
        "• Bấm nút **[📋 Danh mục]** trên Dashboard để quản lý.\n\n"

        "2️⃣ ***Công cụ Phân tích AI (PRO)***\n"
        "• 📊 AI khám sức khỏe toàn bộ danh mục.\n"
        "• 📄 Soi hồ sơ doanh nghiệp (Lợi thế, Rủi ro).\n"
        "• 💎 Lọc cổ phiếu Rẻ/Đắt (Mean Reversion).\n\n"

        "3️⃣ ***Báo cáo Tự động (PRO)***\n"
        "Bot sẽ tự động gửi thông tin đến bạn (không cần gõ lệnh):\n"
        "• 🌅 **07:00 Hằng ngày:** Bản tin sáng (Tin tức + BCTC + Định giá cổ phiếu).\n"
        "• 🌆 **15:00 Hằng ngày:** Tổng kết cuối phiên.\n"
        "• 📅 **09:00 Chủ Nhật:** Báo cáo chuyên sâu danh mục tuần.\n\n"

        "4️⃣ ***Hệ thống***\n"
        "• ⚙️*Tài khoản*: Kiểm tra hạn dùng Pro & Cài đặt thông báo.\n"
        "• Nhấn /start: Về trang chủ.\n\n"

        "⚠️ ***Miễn trừ trách nhiệm:***\n"
        "_Các phân tích từ Bot được tạo tự động bởi thuật toán và AI dựa trên dữ liệu quá khứ. "
        "Thị trường tài chính luôn tiềm ẩn rủi ro, đây là thông tin tham khảo, không phải lời khuyên đầu tư._"
    )

    # Tạo bàn phím điều hướng
    kb = [
        [
            InlineKeyboardButton("🏠 Mở Dashboard", callback_data="back_to_start"),
            InlineKeyboardButton("💎 Nâng cấp Pro", callback_data="btn_upgrade")
        ],
        [
            InlineKeyboardButton("💬 Liên hệ Admin", url="https://t.me/KhoiTran99")
        ],
        [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]
    ]

    await reply_md(update, help_text, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bật bot (chỉ admin). (ĐÃ SỬA LỖI BLOCKING I/O)"""
    global BOT_ACTIVE

    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    BOT_ACTIVE = True
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(set_bot_active, True)

    msg = (
    "✅ *Hệ thống đã được kích hoạt trở lại.*\n\n"
    "Bot hiện đang ở trạng thái *hoạt động bình thường* và sẵn sàng phục vụ người dùng. 🚀"
    )


    log.info("[ADMIN] Bot đã bật (BOT_ACTIVE=True, lưu vào DB).")
    await reply_md(update, msg)

async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tắt bot (chỉ admin). (ĐÃ SỬA LỖI BLOCKING I/O)"""
    global BOT_ACTIVE

    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    BOT_ACTIVE = False
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(set_bot_active, False)

    msg = (
        "🛠️ *Hệ thống đã chuyển sang chế độ bảo trì.*\n\n"
        "Tất cả lệnh người dùng sẽ bị tạm ngưng. "
        "Trạng thái này đã được lưu trong cơ sở dữ liệu và sẽ giữ nguyên sau khi deploy. 🔒"
    )

    log.info("[ADMIN] Bot đã tắt (BOT_ACTIVE=False, lưu vào DB).")
    await reply_md(update, msg)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hiển thị trạng thái bot hiện tại (admin only). (ĐÃ SỬA LỖI BLOCKING I/O)"""
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    # ⭐️ SỬA: Chạy CSDL trong thread
    current_state = await asyncio.to_thread(get_bot_active)
    
    status = "🟢 Đang *hoạt động bình thường*" if current_state else "🔴 Đang *bảo trì*"
    await reply_md(update,
        f"{status}\n(Dữ liệu lấy trực tiếp từ cơ sở dữ liệu.)"
    )

async def cmd_vn30f1m_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Lấy trạng thái hiện tại (giả sử bạn có hàm helper này)
    mp = await asyncio.to_thread(get_vn30f1m_enabled_map) 
    enabled = bool(mp.get(chat_id, False))
    
    status_text = "🟢 ĐANG BẬT" if enabled else "🔴 ĐANG TẮT"
    
    # Tạo nút đảo ngược trạng thái
    btn_text = "🔴 Tắt ngay" if enabled else "🟢 Bật ngay"
    callback_action = "v30_off" if enabled else "v30_on"
    
    kb = [[InlineKeyboardButton(btn_text, callback_data=callback_action)]]
    
    await reply_md(
        update, 
        f"📉 **Trạng thái Phái sinh VN30F1M**\nHiện tại: {status_text}",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cmd_vn30f1m_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (ĐÃ SỬA) Bật nhận cảnh báo VN30F1M.
    Chỉ dành cho Pro User hoặc Admin.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return
        
    chat_id = update.effective_chat.id
    await asyncio.to_thread(log_command_usage, chat_id, "/vn30f1m_on", ADMIN_ID)

    # ===============================================
    # 💎 CHECK PAYWALL
    # ===============================================
    if not await check_pro_access(update, context):
        return # Dừng lại, vì check_pro_access đã gửi tin nhắn paywall rồi
    # -----------------------

    set_vn30f1m_enabled(chat_id, True)
    reload_vn30f1m_enabled_cache()
    
    await reply_md(update, "✅ (Pro) Đã *bật* nhận cảnh báo VN30F1M cho bạn.")


async def cmd_vn30f1m_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_vn30f1m_enabled(chat_id, False)
    reload_vn30f1m_enabled_cache()
    await reply_md(update, "Đã *tắt* nhận cảnh báo VN30F1M cho bạn.")

async def cmd_news_test_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem danh sách bài vĩ mô đã seen gần nhất trong Redis."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await reply_md(update, "⚠️ Lệnh này chỉ dành cho admin.")
        return

    await reply_md(update, "📡 Đang lấy danh sách bài *vĩ mô* từ Redis...")

    entries = await asyncio.to_thread(load_seen_news_from_redis, NEWS_FEED_TYPE_MACRO, 10)
    if not entries:
        await reply_md(update, "❌ Không có bài vĩ mô nào trong Redis (news_seen:MACRO).")
        return

    lines = ["🧪 *Các bài vĩ mô gần nhất:*"]
    for e in entries:
        pub_str = e["published"].strftime("%Y-%m-%d %H:%M") if e["published"] else ""
        lines.append(f"• {e['title']} ({pub_str})")

    await reply_md(update, "\n".join(lines))

async def cmd_news_test_specialized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem danh sách bài chuyên ngành đã seen gần nhất trong Redis."""
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await reply_md(update, "⚠️ Lệnh này chỉ dành cho admin.")
        return

    await reply_md(update, "📡 Đang lấy danh sách bài *chuyên ngành* từ Redis...")

    entries = await asyncio.to_thread(load_seen_news_from_redis, NEWS_FEED_TYPE_SPECIALIZED, 10)
    if not entries:
        await reply_md(update, "❌ Không có bài chuyên ngành nào trong Redis (news_seen:SPECIALIZED).")
        return

    lines = ["🧪 *Các bài chuyên ngành gần nhất:*"]
    for e in entries:
        pub_str = e["published"].strftime("%Y-%m-%d %H:%M") if e["published"] else ""
        lines.append(f"• {e['title']} ({pub_str})")

    await reply_md(update, "\n".join(lines))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 
    (ĐÃ SỬA - SMART MODE + TRACKING) Thêm mã vào watchlist.
    """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    # --- [MỚI] Cập nhật thông tin user ---
    await track_user_activity(update)
    # -------------------------------------

    chat_id = update.effective_chat.id
    await asyncio.to_thread(log_command_usage, chat_id, "/add", ADMIN_ID)

    if not context.args:
        await reply_md(update,
            "⚠️ Cách dùng: /add <MÃ>\n"
            "Ví dụ: /add HPG, /add SSI, /add VNM"
        )
        return

    symbol = context.args[0].strip().upper()

    # Gửi chat action
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass 

    # Validate sơ bộ
    if len(symbol) != 3 or not symbol.isalpha():
        await reply_md(update, "⚠️ Mã không hợp lệ. Chỉ hỗ trợ mã 3 chữ cái (VD: HPG).")
        return

    # --- GỌI SMART FETCHER ---
    try:
        data = await fetch_data_smart([symbol])
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [ADD] Lỗi fetch_data_smart {symbol}: {e}")
        await reply_md(update, f"⚠️ Lỗi hệ thống khi lấy dữ liệu *{symbol}*. Vui lòng thử lại sau.")
        return

    if not data or symbol not in data:
        await reply_md(update, f"⚠️ Không tìm thấy dữ liệu cho mã *{symbol}*.")
        return

    # Parse dữ liệu
    info = data[symbol]
    price = info.get('price')
    pct = info.get('pct')
    
    if price is None or price == 0:
         await reply_md(update, f"⚠️ Dữ liệu giá của *{symbol}* đang bị lỗi.")
         return

    # Lấy watchlist cũ
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
    
    if symbol in lst:
        symbols_text = ", ".join(lst) if lst else "—"
        await reply_md(update, f"ℹ️ *{symbol}* đã có trong danh sách theo dõi rồi.\n\n📋 Danh mục hiện tại: {symbols_text}")
        return

    # Kiểm tra Paywall
    is_pro = await asyncio.to_thread(is_user_pro, chat_id)
    is_admin = (chat_id == ADMIN_ID)
    
    if not is_pro and not is_admin and len(lst) >= 1:
        await reply_md(update,
            f"⚠️ Tài khoản miễn phí chỉ được theo dõi tối đa **1 mã**.\n"
            f"Vui lòng `/remove {lst[0]}` trước, hoặc nâng cấp Pro."
        )
        return

    # Lưu vào DB
    lst.append(symbol)
    await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)

    # Format tin nhắn phản hồi
    symbols_text = ", ".join(lst)
    price_str = f"{price:,.0f}".replace(",", ".")
    pct_sign = "+" if pct > 0 else ""
    pct_str = f"{pct_sign}{pct:.2f}%" if pct is not None else "—"
    
    try:
        ref_price = price / (1 + pct/100)
        change_abs = price - ref_price
        abs_str = f"{pct_sign}{change_abs:,.0f}".replace(",", ".")
    except:
        abs_str = "—"

    summary = ( 
        f"📈 *{symbol}* đã được thêm vào danh sách theo dõi.\n\n"
        f"💰 Giá hiện tại: *{price_str}*\n"
        f"📊 Thay đổi: *{pct_str}* ({abs_str})\n\n"
        f"📋 *Danh mục của bạn:*\n{symbols_text}" 
    )
    
    await reply_md(update, summary)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/remove", ADMIN_ID)

    if not context.args:
        await reply_md(update,"⚠️ Cách dùng: /remove <MÃ>\nVí dụ: /remove SSI")
        return
    
    await reply_md(update, f"🔎 vui lòng đợi...")

    symbol = context.args[0].upper().strip()
    # ⭐️ SỬA: Chạy CSDL trong thread
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []

    if symbol in lst:
        lst.remove(symbol)
        # ⭐️ SỬA: Chạy CSDL trong thread
        await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)

        if lst:
            symbols_text = ", ".join(lst)
            msg = (
                f"🗑️ Đã xoá *{symbol}* khỏi danh sách theo dõi.\n\n"
                "📋 *Danh sách mã bạn đang theo dõi hiện tại:*\n"
                f"{symbols_text}\n\n"
                "Bạn có thể dùng /add <MÃ> để thêm cổ phiếu mới."
            )
        else:
            msg = (
                f"🗑️ Đã xoá *{symbol}* khỏi danh sách theo dõi.\n\n"
                "📭 Hiện bạn không còn theo dõi mã nào.\n"
                "Dùng /add <MÃ> để bắt đầu thêm lại danh mục."
            )

        await reply_md(update, msg)
    else:
        await reply_md(update,
            f"❌ *{symbol}* không có trong danh sách theo dõi.\n"
            "Bạn có thể dùng /list để kiểm tra lại danh sách hiện tại."
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    # Lấy danh sách từ DB
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []

    if not lst:
        await reply_md(update, "📭 Danh mục trống. Dùng `/add <MÃ>` để thêm.")
        return

    # --- TẠO BÀN PHÍM TỪ DANH SÁCH ---
    # Mỗi hàng 3 mã
    keyboard = []
    row = []
    for sym in lst:
        # callback_data dạng: "mgr_HPG" (Manager - HPG)
        row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Thêm nút đóng
    keyboard.append([InlineKeyboardButton("❌ Đóng danh sách", callback_data="close_list")])

    await reply_md(
        update, 
        "📋 **Quản lý danh mục**\nBấm vào mã để xem tùy chọn:", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 
    (ĐÃ CẬP NHẬT) Xem trạng thái & Cài đặt.
    - Nếu dùng lệnh /setting -> Gửi tin nhắn mới.
    - Nếu bấm nút -> Sửa tin nhắn cũ (In-place update).
    """
    if not BOT_ACTIVE:
        if update.callback_query:
            await update.callback_query.answer("⚙️ Bot đang bảo trì.", show_alert=True)
        else:
            await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # Chỉ log command nếu là lệnh gõ tay (để tránh spam log khi bấm nút)
    if not update.callback_query:
        try:
            await asyncio.to_thread(log_command_usage, chat_id, "/setting", ADMIN_ID)
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except: pass

    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)

    # --- 1. LẤY DỮ LIỆU SONG SONG ---
    try:
        results = await asyncio.gather(
            asyncio.to_thread(get_user_pro_expiry, chat_id),
            asyncio.to_thread(get_vn30f1m_enabled_map),
            asyncio.to_thread(get_stock_alert_enabled_map),
            return_exceptions=True
        )
        
        expiry_date = results[0] if not isinstance(results[0], Exception) else None
        vn30_map = results[1] if not isinstance(results[1], Exception) else {}
        stock_map = results[2] if not isinstance(results[2], Exception) else {}
        
        vn30_enabled = bool(vn30_map.get(chat_id, False))
        stock_enabled = bool(stock_map.get(chat_id, True))

    except Exception as e:
        log.error(f"Setting error: {e}")
        msg_err = "⚠️ Lỗi lấy dữ liệu cài đặt."
        if update.callback_query:
            await update.callback_query.answer(msg_err, show_alert=True)
        else:
            await reply_md(update, msg_err)
        return

    # --- 2. BUILD NỘI DUNG TEXT ---
    lines = ["⚙️ *CÀI ĐẶT & TRẠNG THÁI TÀI KHOẢN* ⚙️\n"]

    # Trạng thái Pro
    if chat_id == ADMIN_ID:
        lines.append("👤 *Gói cước:* 😎 *ADMIN*")
    elif expiry_date and expiry_date.astimezone(vn_tz) > now:
        exp_str = expiry_date.astimezone(vn_tz).strftime("%H:%M %d/%m/%Y")
        lines.append(f"👤 *Gói cước:* 👑 *PRO*")
        lines.append(f"⏳ *Hết hạn:* {exp_str}")
    else:
        lines.append("👤 *Gói cước:* 🆓 *FREE*")
        lines.append("_Giới hạn: Theo dõi 1 mã, không có AI Report._")

    # Morning Digest
    lines.append("\n📰 *Bản tin sáng (Digest)*: TỰ ĐỘNG (07:00)")

    # Stock Alert
    lines.append("\n📊 *Cảnh báo Biến động cổ phiếu*")
    status_stock = "✅ *BẬT*" if stock_enabled else "❌ *TẮT*"
    lines.append(status_stock)

    # Phái sinh
    lines.append("\n📈 *Cảnh báo VN30F1M*")
    status_vn30 = "✅ *BẬT*" if vn30_enabled else "❌ *TẮT*"
    lines.append(status_vn30)

    # --- 3. TẠO BÀN PHÍM ĐIỀU KHIỂN ---
    
    vn30_btn = "🔴 Tắt cập nhật VN30F1M" if vn30_enabled else "🟢 Bật cập nhật VN30F1M"
    vn30_cb = "set_vn30_off" if vn30_enabled else "set_vn30_on"

    stock_btn = "🔴 Tắt cập nhật cổ phiếu" if stock_enabled else "🟢 Bật cập nhật cổ phiếu"
    stock_cb = "set_stock_off" if stock_enabled else "set_stock_on"

    kb = [
        [InlineKeyboardButton("💎 Nâng cấp / Gia hạn Pro", callback_data="btn_upgrade")],
        [InlineKeyboardButton(stock_btn, callback_data=stock_cb)], 
        [InlineKeyboardButton(vn30_btn, callback_data=vn30_cb)],   
        [InlineKeyboardButton("🔙 Dashboard", callback_data="back_to_start")]
    ]
    
    msg_text = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(kb)

    # --- 4. QUYẾT ĐỊNH: GỬI MỚI HAY SỬA CŨ ---
    if update.callback_query:
        # Nếu gọi từ nút bấm -> Sửa tin nhắn hiện tại (In-place)
        await safe_edit_message(update.callback_query, msg_text, reply_markup)
    else:
        # Nếu gọi từ lệnh /setting -> Gửi tin nhắn mới
        await reply_md(update, msg_text, reply_markup=reply_markup)

async def cmd_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Kích hoạt dùng thử 10 ngày (ĐÃ SỬA LỖI GỬI TIN ADMIN).
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    await track_user_activity(update) 
    
    try: await asyncio.to_thread(log_command_usage, chat_id, "/trial", ADMIN_ID)
    except: pass

    # 1. Kiểm tra điều kiện
    status = await asyncio.to_thread(check_trial_eligibility, chat_id)

    if status == 'IS_PRO':
        await reply_md(update, "😎 **Bạn đang là thành viên Pro rồi!**\nKhông cần kích hoạt dùng thử nữa.")
        return

    if status == 'USED':
        kb = [[InlineKeyboardButton("💎 Nâng cấp ngay (Chỉ 3k/ngày)", callback_data="btn_upgrade")]]
        await reply_md(
            update, 
            "😢 **Rất tiếc, bạn đã sử dụng hết lượt Dùng thử.**\n\nVui lòng nâng cấp gói Pro để tiếp tục.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # 3. Kích hoạt
    TRIAL_DAYS = 10
    await reply_md(update, "⏳ Đang kích hoạt gói dùng thử...")
    
    try:
        # Gọi hàm kích hoạt (chạy trực tiếp vì nó rất nhanh)
        activate_trial_package(chat_id, TRIAL_DAYS)
        
        msg_success = (
            f"🚀 **KÍCH HOẠT THÀNH CÔNG!**\n\n"
            f"Bạn đã nhận được **{TRIAL_DAYS} ngày** trải nghiệm Full tính năng Pro:\n\n"
            f"💎 **Các đặc quyền đã được mở khóa:**\n"
            f"• 📨 **Auto Report:** Tự động gửi bản tin Sáng (07:00), EOD (15:00) & Tuần (09:00 CN).\n"
            f"• 📊 **AI Report:** Phân tích sâu sức khỏe danh mục & khuyến nghị hành động.\n"
            f"• 🔍 **Screener:** Lọc cổ phiếu Rẻ/Đắt theo định giá lịch sử.\n"
            f"• 🏢 **Soi Hồ Sơ:** Phân tích mô hình kinh doanh, lợi thế cạnh tranh & rủi ro.\n"
            f"• 📉 **Phái Sinh:** Nhận tín hiệu cảnh báo VN30F1M realtime.\n"
            f"• 🔔 **Không Giới Hạn:** Theo dõi biến động giá cho toàn bộ danh mục (gói Free chỉ được 1 mã).\n\n"
            f"👉 **Hãy bắt đầu trải nghiệm ngay với** /start"
        )
        await reply_md(update, msg_success)
        
        # --- SỬA LỖI TẠI ĐÂY ---
        # Dùng context.bot.send_message thay vì send_msg_to
        if ADMIN_ID and chat_id != ADMIN_ID:
            user = update.effective_user
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID, 
                    text=f"👤 User {user.full_name} (ID: {chat_id}) vừa kích hoạt /trial."
                )
            except Exception: pass # Bỏ qua nếu lỗi gửi admin
            
    except Exception as e:
        log.error(f"Lỗi kích hoạt trial cho {chat_id}: {e}")
        # Sửa cả chỗ báo lỗi này luôn
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"⚠️ Lỗi hệ thống khi user {chat_id} trial: {e}"
                )
            except: pass
        await reply_md(update, f"⚠️ Lỗi hệ thống. Vui lòng thử lại sau.")

# Dùng dict lưu tạm xác nhận theo admin_id
pending_clear_confirmations = {}

async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA) Gửi thông báo tới TẤT CẢ user (Pro + Free). """
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await reply_md(update,"⛔ Chỉ admin mới có quyền dùng lệnh này.")
        return

    if not context.args:
        await reply_md(update,"❗ Vui lòng nhập nội dung thông báo sau lệnh /announce.")
        return

    text = " ".join(context.args)
    text = text.replace("\\n", "\n") # Cho phép admin dùng \n để xuống dòng

    await reply_md(update, f"🔎 Đang chuẩn bị gửi thông báo tới TẤT CẢ user...")

    try:
        # ⭐️ Đơn giản hoá: Chỉ cần gọi hàm broadcast với target='all'
        await broadcast_to_all_watchers(text, target_audience='all')
        
        # Đếm số lượng user (gọi lại DB, nhưng không sao, lệnh admin không thường xuyên)
        all_watch = await asyncio.to_thread(get_all_watch)
        sent_count = len(all_watch) 
        
        await reply_md(update, f"✅ Đã gửi thông báo tới TẤT CẢ *{sent_count}* người dùng.")

    except Exception as e:
        log.warning(f"Lỗi khi gửi /announce: {e}")
        await reply_md(update, f"⚠️ Lỗi khi gửi broadcast: {e}")

@task_locked
async def cmd_screener_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (PREMIUM) Bộ lọc Định giá (Mean Reversion).
    - Free: Hiện nút mở WebApp khóa.
    - Pro: Tính toán & Hiện nút mở WebApp kết quả.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # 1. Xác định quyền hạn
    is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"

    # ==================================================================
    # 🔴 NHÁNH 1: FREE USER (Gửi nút WebApp Locked)
    # ==================================================================
    if not is_pro:
        try: await asyncio.to_thread(log_command_usage, chat_id, "/screener_value (Free)", ADMIN_ID)
        except: pass
        
        # Link trỏ về route khóa
        web_app_url = f"{base_url}/screener/locked"
        
        kb = [
            [InlineKeyboardButton("💎 Mở Bộ Lọc (Pro Only)", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]
        ]
        
        await reply_md(
            update,
            "💎 **Bộ Lọc Cổ Phiếu Giá Trị (Mean Reversion)**\n\n"
            "Hệ thống quét toàn thị trường tìm mã Rẻ/Đắt so với lịch sử 5 năm.\n"
            "👇 Nhấn nút bên dưới để xem demo tính năng.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ==================================================================
    # 🟢 NHÁNH 2: PRO USER (Tính toán & Gửi kết quả)
    # ==================================================================
    try: await asyncio.to_thread(log_command_usage, chat_id, "/screener_value", ADMIN_ID)
    except: pass

    # 2. Kiểm tra dữ liệu lịch sử
    hist_data = await asyncio.to_thread(get_historical_valuation_from_redis)
    if not hist_data:
        await reply_md(update, "⏳ Hệ thống đang khởi tạo dữ liệu định giá lịch sử. Vui lòng thử lại sau 2-3 phút.")
        asyncio.create_task(calculate_historical_valuation_task())
        return

    # 3. Gửi tiến trình Loading
    progress_msg = await reply_md(update, f"💎 **Đang phân tích định giá thị trường...**\n`[{make_progress_bar(20)}] 20%`")

    try:
        # 4. Lấy dữ liệu Hiện tại (Screener)
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=progress_msg.message_id,
            text=f"📊 **Đang so sánh với dữ liệu quá khứ...**\n`[{make_progress_bar(60)}] 60%`", parse_mode="Markdown"
        )

        # 5. Xử lý logic so sánh
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
            
            # Bỏ qua mã lỗ hoặc dữ liệu sai
            if pe_cur <= 0 or pb_cur <= 0: continue
            if pe_avg <= 0 or pb_avg <= 0: continue

            # Tính toán độ lệch
            pe_discount = (pe_cur - pe_avg) / pe_avg
            pb_discount = (pb_cur - pb_avg) / pb_avg
            avg_discount = (pe_discount + pb_discount) / 2
            
            # Helper format hiển thị
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
                'symbol': sym,
                'pe_cur': pe_cur, 'pe_avg': pe_avg,
                'pe_class': pe_class, 'pe_diff_str': pe_diff_str,
                'pb_cur': pb_cur, 'pb_avg': pb_avg,
                'pb_class': pb_class, 'pb_diff_str': pb_diff_str,
                'signal_class': signal_class, 'signal_text': signal_text,
                'avg_discount': avg_discount
            })

        # 6. Sắp xếp & Lưu Cache Redis
        processed_items.sort(key=lambda x: x['avg_discount'])
        top_items = processed_items[:50] # Lấy Top 50 mã rẻ nhất

        digest_id = uuid.uuid4().hex
        vn_tz = pytz.timezone(TIMEZONE)
        payload = {
            "items": top_items,
            "generated_time": datetime.datetime.now(vn_tz).strftime("%H:%M %d/%m/%Y")
        }
        
        r = get_redis()
        r.set(f"digest_web:screener_val:{digest_id}", json.dumps(payload), ex=3600)

        # Link trỏ về route kết quả (thêm chat_id để bảo mật route này nếu cần)
        web_url = f"{base_url}/screener_result/{digest_id}?chat_id={chat_id}"
        
        await context.bot.delete_message(chat_id, progress_msg.message_id)
        
        kb = [[InlineKeyboardButton("🚀 Xem Bảng Xếp Hạng", web_app=WebAppInfo(url=web_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]

        await reply_md(
            update,
            f"💎 **Định Giá Cổ Phiếu (Mean Reversion)**\n\n"
            f"✅ Đã lọc được {len(processed_items)} mã tiềm năng.\n"
            f"👉 Nhấn nút bên dưới để xem Top cổ phiếu rẻ nhất.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    except Exception as e:
        log.error(f"Lỗi /screener_value: {e}")
        try:
            await context.bot.edit_message_text(chat_id, progress_msg.message_id, text="⚠️ Lỗi hệ thống. Vui lòng thử lại sau.")
        except: pass

async def cmd_screener_value_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    [UPDATED] Force refresh dữ liệu Mean Reversion (Lịch sử 5 năm).
    CHẠY NGẦM (Background) để tránh Timeout do tác vụ quá dài.
    """
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # 1. Thông báo xác nhận lệnh đã nhận
    await reply_md(
        update,
        "⏳ *Đã kích hoạt tác vụ làm mới dữ liệu Mean Reversion (Chạy ngầm)...*\n"
        "_Bot sẽ thông báo lại khi hoàn tất (dự kiến 10-15 phút)._"
    )

    # 2. Định nghĩa hàm wrapper để chạy ngầm và báo cáo kết quả sau
    async def _background_runner():
        try:
            start_time = time.time()
            # Gọi task nặng
            await calculate_historical_valuation_task()
            duration = time.time() - start_time
            
            # Gửi tin nhắn báo cáo thành công
            await send_md(
                context.bot, 
                chat_id, 
                f"✅ *Hoàn tất làm mới dữ liệu Screener!*\n⏱ Thời gian chạy: {duration/60:.1f} phút."
            )
        except Exception as e:
            log.error(f"Lỗi background screener_value_clear: {e}")
            await send_md(context.bot, chat_id, f"⚠️ Lỗi tác vụ ngầm Screener: {e}")

    # 3. Đẩy vào Event Loop chạy ngầm (Không await)
    asyncio.create_task(_background_runner())

# COMMAND: /delete_range YYYY-MM-DD HH:MM YYYY-MM-DD HH:MM
async def cmd_delete_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await reply_md(update, "⛔ Chỉ admin mới có quyền xoá tin nhắn.")
        return

    args = context.args
    vn_tz = pytz.timezone(TIMEZONE) # ✅ Đảm bảo vn_tz luôn được định nghĩa

    # ❗️Trường hợp không (hoặc thiếu) tham số:
    if len(args) < 4:
        now = datetime.datetime.now(vn_tz) # 'now' cần ở đây cho lệnh mẫu

        start_time = now - datetime.timedelta(minutes=1)
        end_time   = now + datetime.timedelta(minutes=1)

        start_str = start_time.strftime("%Y-%m-%d %H:%M")
        end_str   = end_time.strftime("%Y-%m-%d %H:%M")

        # Tin nhắn 1: cú pháp
        await reply_md(
            update,
            "❗️ Cú pháp: `/delete_range <từ ngày> <giờ> <đến ngày> <giờ>`"
        )

        # Tin nhắn 2: lệnh mẫu
        await reply_md(
            update,
            f"`/delete_range {start_str} {end_str}`"
        )
        return

    # ✅ Trường hợp có đủ 4 tham số:
    await reply_md(update, "🔎 vui lòng đợi...")

    try:
        # ✅✅✅ [SỬA LỖI] Khởi tạo 'now' và 'skipped_old'
        now = datetime.datetime.now(vn_tz)
        skipped_old = 0
        deleted = 0
        
        start_str = f"{args[0]} {args[1]}"
        end_str   = f"{args[2]} {args[3]}"
        start_time = vn_tz.localize(datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M"))
        end_time   = vn_tz.localize(datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M"))

        # ⭐️ Chạy truy vấn CSDL trong thread
        records = await asyncio.to_thread(get_bot_messages_in_range, start_time, end_time)
        if not records:
            await reply_md(update, "📭 Không có tin nhắn nào trong khoảng thời gian này.")
            return

        # ⭐️ Hàm sync để dùng với to_thread
        def _delete_message(chat_id, msg_id):
            url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
            params = {"chat_id": chat_id, "message_id": msg_id}
            try:
                # Sửa: Dùng POST thay vì GET cho deleteMessage để an toàn hơn
                requests.post(url, params=params, timeout=10)
            except Exception as e:
                log.warning(f"Lỗi gọi deleteMessage cho {msg_id} trong chat {chat_id}: {e}")

        # ✅ get_bot_messages_in_range trả về (chat_id, message_id, sent_at)
        for chat_id, msg_id, _sent_at in records:
            try:
                # Logic xử lý timezone của bạn (đã đúng)
                sent_at_vn = _sent_at.astimezone(vn_tz) if _sent_at.tzinfo else _sent_at.replace(tzinfo=pytz.UTC).astimezone(vn_tz)
                
                # Giờ 'now' đã được định nghĩa và có thể so sánh
                if (now - sent_at_vn).total_seconds() > 48*3600:
                    skipped_old += 1
                    continue

                await asyncio.to_thread(_delete_message, chat_id, msg_id)
                deleted += 1
                await asyncio.sleep(0.1) # Tránh rate limit của Telegram
            except Exception as e:
                log.warning(f"Lỗi xoá message {msg_id} trong chat {chat_id}: {e}")

        # Xoá log trong DB
        await asyncio.to_thread(delete_bot_messages_in_range, start_time, end_time)
        
        # Tạo thông báo kết quả
        summary = f"✅ Đã xoá {deleted} tin nhắn trong khoảng {start_str} → {end_str}."
        if skipped_old > 0:
            summary += f"\n(Đã bỏ qua {skipped_old} tin nhắn cũ hơn 48 giờ, không thể xoá)."
        
        await reply_md(update, summary)

    except Exception as e:
        await reply_md(update, f"⚠️ Lỗi xử lý: {e}")

# ============================================================
# ♻️ /restore_core – Khôi phục dữ liệu core + Clear Redis + Sync Redis từ DB
# ============================================================
@task_locked
async def cmd_restore_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Khôi phục dữ liệu core từ file JSON backup.
    (ĐÃ SỬA: Thống kê chính xác số lượng bản ghi trước và sau restore)
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if ADMIN_ID is None or user_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return
    
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except: pass

    # 1. Lấy file
    document = update.message.document or (update.message.reply_to_message.document if update.message.reply_to_message else None)
    if not document:
        await reply_md(update, "📥 Vui lòng gửi file `.json` kèm caption `/restore_core`.")
        return

    # 2. Tải file
    tmp_dir = Path(tempfile.gettempdir()) / "stockbot_restore"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", document.file_name or "backup.json")
    tmp_path = tmp_dir / f"{int(time.time())}_{safe_name}"

    try:
        tg_file = await document.get_file()
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await reply_md(update, f"⚠️ Lỗi tải file: `{e}`")
        return

    # 3. Clear Redis
    try:
        r = get_redis()
        key_count = r.dbsize()
        r.flushdb()
        await reply_md(update, f"🧹 Đã xóa {key_count} keys Redis.")
    except Exception: pass

    # 4. Đọc JSON
    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        await reply_md(update, f"⚠️ File JSON lỗi: `{e}`")
        return

    # --- HÀM ĐẾM SỐ LƯỢNG BẢN GHI (SYNC) ---
    def _count_current_rows():
        stats = {}
        # Danh sách tất cả các bảng quan trọng
        tables = [
            "users","bot_watch", "news_pref", "bot_config", "bctc_notified", # Core cũ
            "paid_users", "bot_orders", "bot_user_settings", "analysis_report_seen" # Core mới (Tiền nong)
        ]
        with get_conn() as conn:
            with conn.cursor() as cur:
                for tbl in tables:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                        res = cur.fetchone()
                        stats[tbl] = res[0] if res else 0
                    except Exception:
                        # Nếu bảng chưa tồn tại (lần đầu chạy), coi như 0
                        stats[tbl] = 0
        return stats
    # ---------------------------------------

    # 5. Lấy thống kê TRƯỚC khi restore
    try:
        before_stats = await asyncio.to_thread(_count_current_rows)
    except Exception as e:
        log.warning(f"Lỗi đếm before: {e}")
        before_stats = {}

    # 6. Thực hiện IMPORT
    try:
        # Hàm import_core_data không cần trả về gì cả, nó chỉ cần chạy xong không lỗi
        await asyncio.to_thread(import_core_data, payload, "replace")
    except Exception as e:
        await reply_md(update, f"⚠️ Lỗi Import DB: `{e}`")
        return

    # 7. Lấy thống kê SAU khi restore
    try:
        after_stats = await asyncio.to_thread(_count_current_rows)
    except Exception:
        after_stats = {}

    # 8. Đồng bộ lại Redis (Watchlist)
    sync_msg = ""
    try:
        synced_users = 0
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, watch_list FROM bot_watch")
                rows = cur.fetchall()
        
        r = get_redis()
        r.delete("watch_chat_ids")
        for cid, wl in rows:
            r.set(f"watch:{cid}", json.dumps(wl))
            r.sadd("watch_chat_ids", cid)
            synced_users += 1
        sync_msg = f"🔄 Đã đồng bộ Redis (Watchlist). Tổng user: *{synced_users}*"
    except Exception as e:
        sync_msg = f"⚠️ Lỗi đồng bộ Redis: {e}"

    # 9. Tạo báo cáo kết quả
    lines = ["✅ **Khôi phục dữ liệu core thành công!**\n"]
    lines.append("**Biến động dữ liệu (Trước → Sau):**")
    
    # Danh sách hiển thị đẹp
    display_map = {
        "users": "👤 User Info",
        "paid_users": "💰 User Pro",
        "bot_orders": "🧾 Đơn hàng",
        "bot_watch": "📋 Watchlist",
        "bot_user_settings": "⚙️ Setting",
        "bctc_notified": "🔔 Log BCTC",
        "analysis_report_seen": "📊 Log Report",
        "news_pref": "📰 News Pref",
        "bot_config": "🔧 Config"
    }

    for tbl, name in display_map.items():
        b = before_stats.get(tbl, 0)
        a = after_stats.get(tbl, 0)
        # Chỉ hiện những bảng có dữ liệu hoặc có sự thay đổi
        if b > 0 or a > 0:
            lines.append(f"- {name}: {b} → **{a}**")

    lines.append("")
    lines.append(sync_msg)

    await reply_md(update, "\n".join(lines))

    # Dọn dẹp file tạm
    try:
        if os.path.exists(tmp_path): os.remove(tmp_path)
    except: pass

@task_locked
async def cmd_backup_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin: backup dữ liệu core thành file JSON.
    (ĐÃ SỬA LỖI JSON SERIALIZABLE CHO DATETIME)
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if ADMIN_ID is None or user_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return
    
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.TYPING
        )
    except Exception:
        pass

    # Log command
    try: await asyncio.to_thread(log_command_usage, chat_id, "/backup_core", ADMIN_ID)
    except: pass

    await reply_md(update, "⏳ Đang backup dữ liệu core, vui lòng đợi...")

    # Export dữ liệu core (DB I/O chạy trong thread)
    try:
        payload = await asyncio.to_thread(export_core_data)
    except Exception as e:
        log.error(f"Lỗi export data: {e}")
        await reply_md(update, f"⚠️ Lỗi khi lấy dữ liệu từ DB: {e}")
        return

    # Tạo file tạm trong container
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    ts = now.strftime("%Y%m%d_%H%M%S")
    month_key = now.strftime("%Y-%m")
    filename = f"stockbot_core_backup_{month_key}_{ts}.json"
    tmp_path = os.path.join(TMP_DIR, filename)

    # --- HÀM CONVERTER ĐỂ XỬ LÝ DATETIME ---
    def json_datetime_converter(o):
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")
    # ---------------------------------------

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            # Thêm tham số default=json_datetime_converter
            json.dump(
                payload, 
                f, 
                ensure_ascii=False, 
                indent=2, 
                default=json_datetime_converter # <--- SỬA LỖI TẠI ĐÂY
            )

        # Gửi file cho admin
        await context.bot.send_document(
            chat_id=chat_id,
            document=open(tmp_path, "rb"),
            filename=filename,
            caption=(
                f"📦 Backup dữ liệu core lúc {ts} (tháng {month_key}).\n"
                "- Dữ liệu bao gồm: Watchlist, User Pro, Orders, Settings...\n"
                "- Dùng file này cho lệnh /restore_core sau khi tạo DB mới."
            ),
        )
        await reply_md(update, "✅ Đã backup xong và gửi file cho bạn.")

    except Exception as e:
        log.error(f"Lỗi backup core: {e}")
        await reply_md(update, f"⚠️ Lỗi khi tạo file backup: {e}")
    
    # Dọn dẹp file tạm (nếu cần thiết, nhưng để lại debug cũng được)
    # try: os.remove(tmp_path)
    # except: pass

# ==============================================
# COMMAND: /report (CÓ CACHE REDIS + RETRY, KHÔNG COOLDOWN)
# Cache nội dung report theo danh mục vào Redis (theo cache_key = danh mục chuẩn hoá)
# ==============================================

@task_locked
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (UX PRO) AI Report với thanh tiến trình (Progress Bar).
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    vn_tz = pytz.timezone(TIMEZONE)
    if not update or not update.effective_chat: return
    chat_id = update.effective_chat.id

    # 1. Xác định trạng thái Pro
    is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    watch = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
    symbols = [s.upper() for s in (watch or []) if not s.upper().startswith("VN")]
    cache_key = make_report_cache_key(symbols) if symbols else "EMPTY"
    web_app_url = f"{base_url}/report/view/{cache_key}?chat_id={chat_id}"

    # --- NHÁNH 1: FREE USER (GIỮ NGUYÊN) ---
    if not is_pro:
        # (Code cũ phần Free giữ nguyên, chỉ copy lại cho đủ)
        try: await asyncio.to_thread(log_command_usage, chat_id, "/report (Free)", ADMIN_ID)
        except: pass
        kb = [[InlineKeyboardButton("📊 Xem Báo Cáo AI", web_app=WebAppInfo(url=web_app_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        await reply_md(update, "📊 **Báo Cáo Danh Mục AI**\n\nAI sẽ phân tích chuyên sâu sức khỏe danh mục.\n👇 Nhấn nút bên dưới để xem chi tiết.", reply_markup=InlineKeyboardMarkup(kb))
        return

    # --- NHÁNH 2: PRO USER (CÓ PROGRESS BAR) ---
    await asyncio.to_thread(log_command_usage, chat_id, "/report", ADMIN_ID)
    if not symbols:
        await reply_md(update, "📭 Danh mục trống. Hãy dùng `/add` để thêm mã trước nhé!")
        return

    # A. Check Cache
    cached = get_report_from_redis(cache_key)
    if cached and not cached[2]: # Not error
        # ... (Phần Cache Hit giữ nguyên) ...
        text_json, generated_at, _, _ = cached
        time_str = "vừa xong"
        if generated_at:
             try: time_str = generated_at.astimezone(vn_tz).strftime("%H:%M %d/%m")
             except: pass
        kb = [[InlineKeyboardButton("📊 Xem Báo Cáo Chi Tiết", web_app=WebAppInfo(url=web_app_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        await reply_md(update, f"✅ Báo cáo danh mục *{', '.join(symbols)}* đã sẵn sàng (bản lưu lúc {time_str}).", reply_markup=InlineKeyboardMarkup(kb))
        return

    # B. Cache Miss -> CHẠY TIẾN TRÌNH
    
    # B1. Gửi tin nhắn khởi tạo (0%)
    progress_msg = await reply_md(
        update, 
        f"⏳ **Khởi động AI Analyst...**\n"
        f"`[{make_progress_bar(10)}] 10%`"
    )
    
    try:
        # Giả lập loading steps (Để tạo cảm giác mượt mà)
        # Bước 1: Thu thập dữ liệu (30%)
        await asyncio.sleep(0.5) 
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"📥 **Đang tải dữ liệu thị trường...**\n`[{make_progress_bar(35)}] 35%`",
            parse_mode="Markdown"
        )

        # Bước 2: Gọi AI (Thật)
        # Trước khi gọi AI, update lên 60%
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"🧠 **Gemini AI đang phân tích...**\n`[{make_progress_bar(60)}] 60%`",
            parse_mode="Markdown"
        )

        # --- GỌI HÀM NẶNG ---
        json_text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
        save_report_to_redis(cache_key, json_text, source="on_demand")
        # --------------------

        # Bước 3: Hoàn tất (100%)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"✅ **Hoàn tất! Đang tạo báo cáo...**\n`[{make_progress_bar(100)}] 100%`",
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.5) # Dừng xíu để user thấy 100%

        # Xóa tin nhắn loading
        await context.bot.delete_message(chat_id, progress_msg.message_id)
        
        # Gửi kết quả cuối cùng
        kb = [
            [InlineKeyboardButton("📊 Xem Báo Cáo Chi Tiết", web_app=WebAppInfo(url=web_app_url))],
            [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]
        ]
        
        await reply_md(
            update, 
            f"🚀 **Phân tích hoàn tất!**\n"
            f"Đã xử lý xong danh mục: *{', '.join(symbols)}*\n"
            f"Nhấn nút bên dưới để xem báo cáo.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    except Exception as e:
        log.error(f"Lỗi /report: {e}")
        # Nếu lỗi, sửa tin nhắn loading thành báo lỗi
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"⚠️ **Lỗi xử lý:** Hệ thống đang bận.\nVui lòng thử lại sau.",
                parse_mode="Markdown"
            )
        except:
            pass

async def cmd_report_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Xoá TOÀN BỘ cache báo cáo AI trong Redis cho tất cả danh mục.

    - Quét toàn bộ key `report_cache:*` trong Redis.
    - Xoá cả cache báo cáo OK lẫn cache lỗi.
    - Chỉ dùng khi muốn force tạo lại toàn bộ báo cáo (/report + Weekly Report).
    """
    if not update or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # 1. Chỉ Admin mới được dùng
    if chat_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # 2. Ghi log usage
    await asyncio.to_thread(log_command_usage, chat_id, "/report_clear", ADMIN_ID)

    await reply_md(
        update,
        "🔎 Đang quét và xoá *toàn bộ cache báo cáo AI* trong Redis...\n"
        "_(Lệnh này ảnh hưởng tới mọi danh mục /report & Weekly Report.)_",
    )

    # 3. Hàm sync: clear tất cả key report_cache:*
    def _clear_all_report_cache() -> int:
        r = get_redis()  # dùng chung Redis (đã import từ news_seen_cache)
        deleted = 0
        # SCAN để tránh block Redis nếu số key lớn
        for key in r.scan_iter(match="report_cache:*"):
            try:
                r.delete(key)
                deleted += 1
            except Exception:
                # best-effort, lỗi 1 key thì bỏ qua, tiếp tục
                continue
        return deleted

    try:
        deleted_count = await asyncio.to_thread(_clear_all_report_cache)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][REPORT_CLEAR] Lỗi khi xoá report_cache:*: {e}")
        await reply_md(
            update,
            f"⚠️ Lỗi khi xoá cache báo cáo AI trong Redis: `{e}`",
        )
        return

    # 4. Phản hồi kết quả
    if deleted_count > 0:
        await reply_md(
            update,
            (
                f"✅ Đã xoá *{deleted_count}* key cache báo cáo AI trong Redis.\n"
                "Lần tiếp theo bạn dùng `/report` hoặc khi Weekly Report chạy, "
                "bot sẽ gọi AI để tạo báo cáo *mới hoàn toàn*."
            ),
        )
    else:
        await reply_md(
            update,
            (
                "ℹ️ Không tìm thấy key nào dạng `report_cache:*` trong Redis.\n"
                "Có thể cache đã hết hạn / chưa được tạo."
            ),
        )

# ==============================================
# COMMAND: /info <MÃ> (HỒ SƠ DOANH NGHIỆP)
# ==============================================
@task_locked
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (UX PRO) Soi hồ sơ doanh nghiệp với Progress Bar.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    if not update.effective_chat: return
    chat_id = update.effective_chat.id

    if not context.args:
        # Fallback nếu user gõ sai
        kb = [[InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        await reply_md(update, "⚠️ Cách dùng: `/info <MÃ>` (VD: `/info FPT`)", reply_markup=InlineKeyboardMarkup(kb))
        return

    symbol = context.args[0].strip().upper()
    
    # 1. Xác định trạng thái Pro
    is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    web_app_url = f"{base_url}/info/{symbol}?chat_id={chat_id}"

    # --- NHÁNH 1: FREE USER (GỬI NÚT NGAY - UPSELL) ---
    if not is_pro:
        try: await asyncio.to_thread(log_command_usage, chat_id, f"/info {symbol} (Free)", ADMIN_ID)
        except: pass
        kb = [[InlineKeyboardButton(f"📄 Mở Hồ Sơ {symbol}", web_app=WebAppInfo(url=web_app_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        await reply_md(
            update,
            f"🏢 **Hồ Sơ Doanh Nghiệp: {symbol}**\n\nPhân tích mô hình kinh doanh & vị thế.\n👇 Nhấn nút bên dưới để xem.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # --- NHÁNH 2: PRO USER (PROGRESS BAR) ---
    await asyncio.to_thread(log_command_usage, chat_id, f"/info {symbol}", ADMIN_ID)
    cache_key = make_profile_cache_key(symbol)
    
    # A. Check Cache
    cached = get_profile_from_redis(cache_key)
    if cached:
        text, _, is_error, _ = cached
        if not is_error:
            kb = [[InlineKeyboardButton(f"📄 Mở Hồ Sơ {symbol}", web_app=WebAppInfo(url=web_app_url))],
                  [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
            await reply_md(update, f"✅ Hồ sơ *{symbol}* đã sẵn sàng.", reply_markup=InlineKeyboardMarkup(kb))
            return

    # B. Cache Miss -> CHẠY TIẾN TRÌNH
    # B1. Khởi tạo (10%)
    progress_msg = await reply_md(
        update, 
        f"⏳ **Đang truy xuất dữ liệu {symbol}...**\n"
        f"`[{make_progress_bar(10)}] 10%`"
    )

    try:
        # B2. Giả lập bước thu thập dữ liệu (40%)
        await asyncio.sleep(0.5)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"📥 **Đang tổng hợp dữ liệu BCTC & Vĩ mô...**\n`[{make_progress_bar(40)}] 40%`",
            parse_mode="Markdown"
        )

        # B3. Gọi AI (75%)
        await asyncio.sleep(0.3)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"🧠 **Gemini AI đang phân tích hồ sơ...**\n`[{make_progress_bar(75)}] 75%`",
            parse_mode="Markdown"
        )

        # --- GỌI HÀM NẶNG ---
        json_text = await asyncio.to_thread(call_gemini_for_profile, symbol)
        save_profile_to_redis(cache_key, json_text, source="on_demand")
        # --------------------

        # B4. Hoàn tất (100%)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"✅ **Hoàn tất! Đang tạo giao diện...**\n`[{make_progress_bar(100)}] 100%`",
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.5)

        # Xóa tin nhắn loading & Gửi kết quả
        await context.bot.delete_message(chat_id, progress_msg.message_id)
        
        kb = [[InlineKeyboardButton(f"📄 Mở Hồ Sơ {symbol}", web_app=WebAppInfo(url=web_app_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        
        await reply_md(update, f"🚀 Đã tạo xong hồ sơ *{symbol}*.", reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        log.error(f"Lỗi /info: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"⚠️ **Lỗi:** Không thể tạo hồ sơ lúc này.\nVui lòng thử lại sau.",
                parse_mode="Markdown"
            )
        except: pass

async def cmd_info_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Xoá TOÀN BỘ cache hồ sơ doanh nghiệp (/info) trong Redis.
    Quét pattern: profile_cache:*
    """
    if not update or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # 1. Chỉ Admin mới được dùng
    if chat_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # 2. Ghi log usage
    await asyncio.to_thread(log_command_usage, chat_id, "/info_clear", ADMIN_ID)

    await reply_md(
        update,
        "🔎 Đang quét và xoá *toàn bộ cache hồ sơ doanh nghiệp (/info)* trong Redis...",
    )

    # 3. Hàm sync: clear tất cả key profile_cache:*
    def _clear_all_profile_cache() -> int:
        r = get_redis()
        deleted = 0
        # SCAN pattern profile_cache:*
        for key in r.scan_iter(match="profile_cache:*"):
            try:
                r.delete(key)
                deleted += 1
            except Exception:
                continue
        return deleted

    try:
        deleted_count = await asyncio.to_thread(_clear_all_profile_cache)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][INFO_CLEAR] Lỗi khi xoá profile_cache:*: {e}")
        await reply_md(
            update,
            f"⚠️ Lỗi khi xoá cache hồ sơ trong Redis: `{e}`",
        )
        return

    # 4. Phản hồi kết quả
    if deleted_count > 0:
        await reply_md(
            update,
            (
                f"✅ Đã xoá *{deleted_count}* hồ sơ doanh nghiệp trong cache.\n"
                "Lần tới gọi `/info`, bot sẽ gọi AI để tạo mới."
            ),
        )
    else:
        await reply_md(
            update,
            (
                "ℹ️ Không tìm thấy cache hồ sơ nào (`profile_cache:*`).\n"
                "Cache có thể đã hết hạn hoặc chưa được tạo."
            ),
        )
#==========================================

async def check_pro_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Hàm kiểm tra Paywall: Kiểm tra xem user có phải là Pro (hoặc Admin) không.
    
    - Trả về True: Nếu là Pro hoặc Admin (được phép tiếp tục).
    - Trả về False: Nếu là user thường (Bot đã tự gửi tin nhắn paywall 
                     và hàm gọi bên ngoài nên 'return' ngay lập tức).
    """
    if not update or not update.effective_chat:
        return False

    chat_id = update.effective_chat.id

    # 1. Gọi DB (trong thread) để kiểm tra trạng thái Pro
    is_pro = await asyncio.to_thread(is_user_pro, chat_id)
    
    # 2. Áp dụng logic của bạn: (Không phải Pro) VÀ (Không phải Admin) -> Chặn
    if not is_pro and chat_id != ADMIN_ID:
        await reply_md(
            update, 
            "⚠️ Tính năng này chỉ dành cho Gói Pro. Vui lòng liên hệ Admin `https://t.me/KhoiTran99` để nâng cấp ạ 🙏."
        )
        return False
    
    # 3. Nếu is_pro = True, HOẶC chat_id == ADMIN_ID -> Cho phép
    return True

# PAID USERS
async def cmd_admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return # Chỉ admin được dùng

    try:
        chat_id_to_add = int(context.args[0])
        days_to_add = int(context.args[1])
        
        # Gọi hàm db_utils.add_paid_user(chat_id_to_add, days_to_add)
        await asyncio.to_thread(add_paid_user, chat_id_to_add, days_to_add)
        
        await reply_md(update, f"✅ Đã gia hạn {days_to_add} ngày cho user {chat_id_to_add}.")
        
        # Tự động gửi tin nhắn cho user kia báo là họ đã được nâng cấp
        await send_md(
            context.bot, 
            chat_id_to_add, 
            f"🚀 Chúc mừng! Tài khoản của bạn đã được nâng cấp lên Gói Pro, có hiệu lực trong {days_to_add} ngày."
        )
    except Exception as e:
        await reply_md(update, f"Lỗi: {e}. Cú pháp: `/admin_add_user <chat_id> <số_ngày>`")

async def cmd_admin_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) Ngưng hoạt động Gói Pro của user ngay lập tức."""
    if update.effective_user.id != ADMIN_ID:
        return # Chỉ admin

    try:
        chat_id_to_deactivate = int(context.args[0])
        
        # Gọi hàm db_utils
        updated_rows = await asyncio.to_thread(deactivate_paid_user, chat_id_to_deactivate)
        
        if updated_rows > 0:
            await reply_md(update, f"✅ Đã ngưng hoạt động Gói Pro của user {chat_id_to_deactivate}.")
            # Gửi thông báo cho user kia
            await send_md(
                context.bot, 
                chat_id_to_deactivate, 
                "⚠️ Gói Pro của bạn đã bị ngưng hoạt động. Vui lòng liên hệ Admin để biết thêm chi tiết."
            )
        else:
            await reply_md(update, f"ℹ️ Không tìm thấy user {chat_id_to_deactivate} trong danh sách Gói Pro.")
            
    except Exception as e:
        await reply_md(update, f"Lỗi: {e}. Cú pháp: /admin_deactivate <chat_id>")

async def cmd_admin_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) Xoá vĩnh viễn Gói Pro của user."""
    if update.effective_user.id != ADMIN_ID:
        return # Chỉ admin

    try:
        chat_id_to_remove = int(context.args[0])
        
        # (Thêm 1 bước xác nhận nhỏ cho an toàn)
        if len(context.args) < 2 or context.args[1] != 'confirm':
             await reply_md(update, f"⚠️ Đây là hành động xoá vĩnh viễn. Để xác nhận, gõ:\n`/admin_remove_user {chat_id_to_remove} confirm`")
             return

        # Gọi hàm db_utils
        deleted_rows = await asyncio.to_thread(remove_paid_user, chat_id_to_remove)
        
        if deleted_rows > 0:
            await reply_md(update, f"✅ Đã XOÁ VĨNH VIỄN Gói Pro của user {chat_id_to_remove} khỏi DB.")
            # Gửi thông báo cho user kia
            await send_md(
                context.bot, 
                chat_id_to_remove, 
                "ℹ️ Tài khoản Pro của bạn đã bị xoá khỏi hệ thống. Vui lòng liên hệ Admin."
            )
        else:
            await reply_md(update, f"ℹ️ Không tìm thấy user {chat_id_to_remove} trong danh sách Gói Pro.")
            
    except Exception as e:
        await reply_md(update, f"Lỗi: {e}. Cú pháp: /admin_remove_user <chat_id> confirm")

async def cmd_run_weekly_report_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Chạy tác vụ gửi weekly report ngay lập tức.
    """
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # Log command
    await asyncio.to_thread(log_command_usage, update.effective_chat.id, "/cmd_run_weekly_report_now", ADMIN_ID)
    
    await reply_md(update, "🏃 Bắt đầu chạy tác vụ `execute_weekly_report` trong nền. Tác vụ này rất lâu, bot sẽ thông báo khi hoàn tất...")

    # Gọi hàm lõi (truyền update vào để nhận phản hồi)
    asyncio.create_task(execute_weekly_report(admin_update=update))

@task_locked
async def cmd_admin_test_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Test trọn vẹn quy trình Daily Digest.
    CÓ CƠ CHẾ MOCK DATA: Nếu không có dữ liệu thật, tự động tạo dữ liệu giả để test UI.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    chat_id = update.effective_chat.id
    vn_tz = pytz.timezone(TIMEZONE)
    
    # 1. Thông báo bắt đầu
    status_msg = await reply_md(update, "🧪 **Bắt đầu Test Full Digest...**\nWait: Đang lấy dữ liệu thật (hoặc tạo Mock data)...")

    try:
        # 2. Khung thời gian 24h
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        since_utc = now_utc - datetime.timedelta(hours=24)

        # 3. Thu thập dữ liệu THẬT
        (
            bctc_rows,
            report_rows,
            macro_rows,
            spec_rows,
            top_value_stocks,
        ) = await asyncio.gather(
            asyncio.to_thread(get_recent_bctc_notified, since_utc),
            asyncio.to_thread(get_recent_analysis_reports, since_utc),
            asyncio.to_thread(get_recent_news_seen, "MACRO", since_utc),
            asyncio.to_thread(get_recent_news_seen, "SPECIALIZED", since_utc),
            get_top_mean_reversion_stocks(limit=5),
        )

        # --- 4. XỬ LÝ TIN TỨC & AI (MOCK NẾU THIẾU) ---
        all_news_items = []
        # Gom tin thật
        for row in macro_rows: all_news_items.append({"title": row[0], "link": row[1], "source": "Vĩ mô"})
        for row in spec_rows: all_news_items.append({"title": row[0], "link": row[1], "source": "Doanh nghiệp"})
        
        # Nếu không có tin thật, tạo tin giả để test AI (hoặc bỏ qua AI)
        using_mock_news = False
        if not all_news_items:
            using_mock_news = True
            all_news_items = [
                {"title": "FED giữ nguyên lãi suất, chứng khoán Mỹ lập đỉnh mới (Mock)", "link": "https://google.com", "source": "Vĩ mô"},
                {"title": "HPG công bố sản lượng thép tháng 10 tăng trưởng 20% (Mock)", "link": "https://google.com", "source": "Doanh nghiệp"},
                {"title": "VHM khởi công dự án 5 tỷ USD tại Long An (Mock)", "link": "https://google.com", "source": "Doanh nghiệp"}
            ]

        ai_news_data = None
        ai_telegram_text = "_Chưa có dữ liệu AI_"

        # Gọi AI (Dù tin thật hay giả)
        if all_news_items:
            try:
                ai_news_data = await summarize_daily_news_with_ai(all_news_items)
                if ai_news_data:
                    lines = []
                    if ai_news_data.get('headline'):
                        lines.append("⚡ *TIÊU ĐIỂM*")
                        for i in ai_news_data['headline']: lines.append(f"• {i['text']}")
                    if ai_news_data.get('comment'):
                        lines.append(f"\n🧠 *AI:* {ai_news_data['comment']}")
                    ai_telegram_text = "\n".join(lines)
                    if using_mock_news: ai_telegram_text += "\n_(Dữ liệu tin tức giả lập để test)_"
            except Exception as e:
                log.error(f"Lỗi AI Test: {e}")

        # --- 5. BUILD PAYLOAD WEB APP (MOCK DỮ LIỆU THIẾU) ---
        payload = {
            "is_pro": True,
            "ai_news": ai_news_data,
            "value_stocks": top_value_stocks,
            "bctc": [],
            "reports": [],
            "specialized": [],
            "macro": []
        }

        # A. Xử lý BCTC (Real + Mock)
        if bctc_rows:
            for (sym, y, q, t) in bctc_rows:
                payload["bctc"].append({"symbol": sym, "year": y, "quarter": q, "time": t.astimezone(vn_tz).strftime("%H:%M"), "is_locked": False})
        else:
            # Mock BCTC
            payload["bctc"].append({"symbol": "HPG (Test)", "year": 2024, "quarter": 3, "time": "08:30", "is_locked": False})
            payload["bctc"].append({"symbol": "VNM (Test)", "year": 2024, "quarter": 3, "time": "09:15", "is_locked": False})

        # B. Xử lý Reports (Real + Mock)
        if report_rows:
            for (sym, title, link, pub, created) in report_rows:
                payload["reports"].append({"symbol": sym, "title": title, "link": link, "time": pub.astimezone(vn_tz).strftime("%d/%m"), "is_locked": False})
        else:
            # Mock Report
            payload["reports"].append({"symbol": "FPT", "title": "Khuyến nghị MUA: Tăng trưởng bền vững (Test)", "link": "#", "time": "Hôm nay", "is_locked": False})
            payload["reports"].append({"symbol": "MWG", "title": "Báo cáo cập nhật: Phục hồi mạnh mẽ (Test)", "link": "#", "time": "Hôm qua", "is_locked": False})

        # C. Xử lý News List (Real + Mock)
        # Tin Vĩ mô
        if macro_rows:
            payload["macro"] = [{"title": r[0], "link": r[1]} for r in macro_rows[:5]]
        else:
            payload["macro"] = [
                {"title": "GDP Việt Nam dự báo tăng trưởng 6.5% năm 2025 (Test)", "link": "#"},
                {"title": "Ngân hàng Nhà nước tiếp tục hút ròng qua tín phiếu (Test)", "link": "#"}
            ]
        
        # Tin Doanh nghiệp
        if spec_rows:
            payload["specialized"] = [{"title": r[0], "link": r[1]} for r in spec_rows[:5]]
        else:
            payload["specialized"] = [
                {"title": "VHM: Doanh số bán hàng tháng 10 đạt kỷ lục (Test)", "link": "#"},
                {"title": "GAS: Chốt quyền chia cổ tức bằng tiền mặt 20% (Test)", "link": "#"}
            ]

        # 6. Lưu Redis & Tạo Link
        digest_id = uuid.uuid4().hex
        await asyncio.to_thread(save_digest_to_redis, digest_id, payload)
        
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        web_url = f"{base_url}/digest/{digest_id}"
        
        # 7. Gửi kết quả
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📰 Xem Bản Tin (Web App)", web_app=WebAppInfo(url=web_url))]])
        
        final_msg = (
            f"🌅 *BẢN TIN SÁNG (TEST MODE)*\n\n"
            f"{ai_telegram_text}\n\n"
            f"👉 Nhấn nút dưới để xem chi tiết Top Cổ Phiếu & Tin tức.\n"
            f"_(Lưu ý: Các mục có chữ 'Test' là dữ liệu giả lập do DB đang rỗng)_"
        )

        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        sent_msg = await send_md(context.bot, chat_id, final_msg, reply_markup=kb)
        
        # Ghim thử để check
        if sent_msg:
            try: await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent_msg.message_id)
            except: pass

    except Exception as e:
        log.error(f"Lỗi Test Digest: {e}")
        await reply_md(update, f"❌ Lỗi: {e}")


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý khi user muốn nâng cấp Pro.
    (ĐÃ SỬA: Gửi ảnh VietQR động)
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    await asyncio.to_thread(log_command_usage, chat_id, "/upgrade", ADMIN_ID)
    
    # 1. Kiểm tra xem admin đã cấu hình QR chưa
    if not SEPAY_QR_BANK or not SEPAY_QR_ACC:
        log.error(f"[SEPAPAY] Lỗi: Admin chưa set SEPAY_QR_BANK/SEPAY_QR_ACC trong .env")
        await reply_md(update, "⚠️ Hệ thống thanh toán đang bảo trì (thiếu cấu hình QR). Vui lòng liên hệ Admin.")
        return

    # 2. Tạo đơn hàng PENDING trong DB (Giữ nguyên)
    try:
        order_id = await asyncio.to_thread(
            create_pending_order,
            chat_id,
            PRO_PACKAGE_AMOUNT,
            PRO_PACKAGE_DAYS
        )
    except Exception as e:
        log.error(f"Lỗi khi tạo đơn hàng SePay cho {chat_id}: {e}")
        await reply_md(update, "⚠️ Đã xảy ra lỗi khi tạo đơn hàng. Vui lòng thử lại sau.")
        return

    # 3. Tạo URL ảnh VietQR (Dựa theo file order.php)
    # Dùng quote_plus để mã hóa nội dung (ví dụ: PAY1088...)
    qr_url = (
        f"https://qr.sepay.vn/img?"
        f"bank={SEPAY_QR_BANK}"
        f"&acc={SEPAY_QR_ACC}"
        f"&template=compact"
        f"&amount={PRO_PACKAGE_AMOUNT}"
        f"&des={quote_plus(order_id)}"
    )

    # 4. Gửi ảnh QR và Hướng dẫn
    amount_str = f"{PRO_PACKAGE_AMOUNT:,}".replace(",", ".")
    
    # Tạo nội dung caption
    caption_lines = [
        f"🌟 *Nâng cấp Gói Pro ({PRO_PACKAGE_DAYS} ngày)*",
        "",
        f"Giá: *{amount_str} VNĐ*",
        "",
        "**Cách 1 (Khuyến nghị):**",
        "Mở App ngân hàng và quét mã QR dưới đây. Mọi thông tin (số tiền, nội dung) sẽ được tự động điền.",
        "",
        "**Cách 2 (Thủ công):**",
        "Nếu không thể quét, vui lòng chuyển khoản thủ công:",
        f"• Số tiền: `{PRO_PACKAGE_AMOUNT}`",
        f"• Nội dung: `{order_id}`",
        "",
        "Sau khi chuyển khoản, Gói Pro sẽ được tự động kích hoạt."
    ]

    try:
        # Dùng send_photo để gửi ảnh trực tiếp
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=qr_url, # Telegram tự động tải URL ảnh này
            caption="\n".join(caption_lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Lỗi khi gửi ảnh QR cho {chat_id}: {e}")
        await reply_md(update, "⚠️ Lỗi khi tạo mã QR. Vui lòng thử lại.")

def _send_telegram_message_safe(chat_id_to_send, text):
    """
    Hàm helper (Sync) để gọi từ Flask route (Thread khác)
    gửi tin nhắn qua Main Loop của bot một cách an toàn.
    """
    try:
        if not tg_app or not MAIN_LOOP:
            log.error("[SEPAPAY] Lỗi: Không tìm thấy tg_app hoặc MAIN_LOOP.")
            return
            
        # Gửi tin nhắn từ Flask route (thread khác) qua Main Loop của bot
        future = asyncio.run_coroutine_threadsafe(
            send_md(tg_app.bot, chat_id_to_send, text),
            MAIN_LOOP
        )
        future.result(timeout=5) # Chờ 5s
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi khi gửi tin nhắn cho {chat_id_to_send}: {e}")
#------------------------------------------------------
# --- HELPER REDIS CHO DIGEST ---
def save_digest_to_redis(digest_id: str, data: dict):
    """Lưu digest data vào Redis với TTL 24h (86400s)"""
    try:
        r = get_redis()
        r.set(f"digest_web:{digest_id}", json.dumps(data, ensure_ascii=False), ex=86400)
    except Exception as e:
        log.error(f"[DIGEST] Lỗi lưu Redis: {e}")

def get_digest_from_redis(digest_id: str):
    """Đọc digest data từ Redis"""
    try:
        r = get_redis()
        raw = r.get(f"digest_web:{digest_id}")
        return json.loads(raw) if raw else None
    except Exception as e:
        log.error(f"[DIGEST] Lỗi đọc Redis: {e}")
        return None
# ==============================================
# FLASK KEEPALIVE
# ==============================================
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return f"✅ Bot is alive. Instance {INSTANCE_ID}"
#------------------------------
@flask_app.route("/health")
def health_check():
    # Phản hồi nhanh nhất có thể, chỉ để xác nhận máy chủ đang chạy
    return "", 200 # Trả về chuỗi rỗng và mã 200 OK
#--------------------------------
@flask_app.route("/webhook", methods=["POST"])
def telegram_webhook():
    global tg_app, MAIN_LOOP

    log.info(f"[{INSTANCE_ID}] 🔔 Received webhook call")

    if tg_app is None or MAIN_LOOP is None:
        return "Bot not ready", 503

    try:
        data = request.get_json(force=True)
    except Exception:
        return "Bad Request", 400

    update = Update.de_json(data, tg_app.bot)

    asyncio.run_coroutine_threadsafe(
        tg_app.process_update(update),
        MAIN_LOOP,
    )

    return "OK", 200
#--------------------------------

@flask_app.route("/sepay-webhook", methods=["POST"])
def sepay_webhook():
    """
    Endpoint nhận thông báo thanh toán (Webhook) từ SePay.
    (ĐÃ CẬP NHẬT: Báo cáo về Admin kèm link chat trực tiếp)
    """
    
    # === 1. LẤY DỮ LIỆU VÀ XÁC THỰC TOKEN ===
    try:
        auth_header = request.headers.get("Authorization")
        data = request.get_json()
        token_from_request = None
        if auth_header and auth_header.startswith("Apikey "):
            token_from_request = auth_header[7:] 

        if not SEPAY_TOKEN:
             log.warning("[SEPAPAY] Bỏ qua xác thực vì SEPAY_TOKEN chưa được set.")
        elif not hmac.compare_digest(str(token_from_request), SEPAY_TOKEN):
            log.warning(f"[SEPAPAY] WEBHOOK BỊ TỪ CHỐI - SAI TOKEN!")
            return jsonify({"message": "Invalid Token"}), 403
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi khi parse JSON hoặc xác thực Header: {e}")
        return jsonify({"message": "Invalid Request Body"}), 400

    # === 2. PHÂN TÍCH PAYLOAD ===
    try:
        raw_content = data.get("content")
        received_amount_str = data.get("transferAmount")
        transfer_type = data.get("transferType")

        if transfer_type != "in":
            return jsonify({"message": "Not an 'in' transaction"}), 200

        if not raw_content or received_amount_str is None:
            return jsonify({"message": "Missing fields"}), 400
        
        # Regex tìm mã PAY...
        match = re.search(r'(PAY\d{9,15}\w{5})', raw_content.upper())
        order_id = match.group(1) if match else None
        
        if not order_id:
            log.info(f"[SEPAPAY] Không tìm thấy Order ID trong: '{raw_content}'. Bỏ qua.")
            return jsonify({"message": "Order ID pattern not found"}), 200
        
        received_amount = int(float(received_amount_str))
            
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi phân tích payload: {e}")
        return jsonify({"message": "Invalid fields"}), 400
    
    # === 3. XỬ LÝ LOGIC THANH TOÁN ===
    try:
        order = get_order_by_id(order_id)
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi DB: {e}")
        return jsonify({"message": "Database error"}), 500

    if not order:
        return jsonify({"message": "Order not found"}), 200

    if order['status'] == 'PAID':
        return jsonify({"message": "Already processed"}), 200

    # Dữ liệu đơn hàng
    chat_id = order['chat_id']
    expected_amount = int(order['amount']) 
    days_to_add = order['days_to_add']
    
    # --- CASE 1: THANH TOÁN SAI SỐ TIỀN (THẤT BẠI) ---
    if received_amount != expected_amount:
        log.warning(f"[SEPAPAY] SAI TIỀN: User {chat_id}. Yêu cầu {expected_amount}, nhận {received_amount}.")
        
        # 1. Báo cho User
        msg_fail_user = (
            f"⚠️ **Thanh toán chưa được ghi nhận!**\n\n"
            f"Hệ thống nhận được *{received_amount:,} đ*, nhưng đơn hàng yêu cầu *{expected_amount:,} đ*.\n"
            "Vui lòng liên hệ Admin để được hỗ trợ xử lý thủ công."
        )
        _send_telegram_message_safe(chat_id, msg_fail_user)

        # 2. Báo cho Admin (Kèm link chat)
        if ADMIN_ID:
            msg_fail_admin = (
                f"🚨 **CẢNH BÁO: LỖI THANH TOÁN (SAI TIỀN)**\n\n"
                f"👤 User ID: `{chat_id}`\n"
                f"📝 Mã đơn: `{order_id}`\n"
                f"🔻 Thực nhận: `{received_amount:,} đ`\n"
                f"yêu cầu: `{expected_amount:,} đ`\n\n"
                f"👉 [Bấm vào đây để chat với khách](tg://user?id={chat_id})"
            )
            _send_telegram_message_safe(ADMIN_ID, msg_fail_admin)

        return jsonify({"message": "Incorrect amount"}), 200

    # --- CASE 2: THANH TOÁN THÀNH CÔNG ---
    try:
        log.info(f"[SEPAPAY] THÀNH CÔNG: User {chat_id}. +{days_to_add} ngày.")
        
        add_paid_user(chat_id, days_to_add)
        mark_order_as_paid(order_id)
        
        # 1. Báo cho User
        msg_success_user = (
            f"🚀 **Kích hoạt Gói Pro thành công!**\n\n"
            f"Bạn đã được cộng thêm *{days_to_add} ngày* sử dụng.\n"
            "Cảm ơn bạn đã ủng hộ! 🥰"
        )
        _send_telegram_message_safe(chat_id, msg_success_user)

        # 2. Báo cho Admin (Kèm link chat)
        if ADMIN_ID:
            msg_success_admin = (
                f"💰 **NẠP TIỀN THÀNH CÔNG**\n\n"
                f"👤 User ID: `{chat_id}`\n"
                f"💵 Số tiền: `{received_amount:,} đ`\n"
                f"📦 Gói: `{days_to_add} ngày`\n"
                f"📝 Mã: `{order_id}`\n\n"
                f"👉 [Bấm vào đây để chat/cảm ơn khách](tg://user?id={chat_id})"
            )
            _send_telegram_message_safe(ADMIN_ID, msg_success_admin)
        
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi kích hoạt Pro: {e}")
        return jsonify({"message": "Error activating Pro"}), 500

    return jsonify({"message": "Success"}), 200

# --- FLASK ROUTE CHO WEB APP ---
@flask_app.route("/digest/<digest_id>")
def view_digest(digest_id):
    """Route hiển thị Web App Digest"""
    data = get_digest_from_redis(digest_id)
    
    # Nếu không tìm thấy data (hết hạn hoặc ID sai) -> Render trang 404 đẹp
    if not data:
        return render_template_string(DIGEST_404_TEMPLATE), 404
    
    # Lấy ngày giờ hiện tại (VN Timezone) để hiển thị trên Header
    vn_tz = datetime.timezone(datetime.timedelta(hours=7))
    date_str = datetime.datetime.now(vn_tz).strftime("Ngày %d/%m/%Y")
    
    # Render trang chính
    return render_template_string(DIGEST_HTML_TEMPLATE, data=data, date_str=date_str)

@flask_app.route("/info/<symbol>")
async def view_profile(symbol: str): # <--- Đổi thành async
    """
    Route hiển thị WebApp hồ sơ (JSON Mode).
    ĐÃ VÁ LỖI: Chặn user Free.
    """
    if not symbol:
        return render_template_string(PROFILE_404_TEMPLATE, symbol=""), 404

    # Logic chặn Free User
    chat_id_str = request.args.get("chat_id")
    is_pro = False
    if chat_id_str:
        try:
            cid = int(chat_id_str)
            is_pro = await asyncio.to_thread(is_user_pro, cid) or (cid == ADMIN_ID)
        except: pass
    
    if not is_pro:
        return render_template_string(
            LOCKED_FEATURE_TEMPLATE,
            icon="🏢",
            title=f"Hồ Sơ Doanh Nghiệp {symbol}",
            desc=(
            "Hệ thống AI sẽ tổng hợp và phân tích toàn diện hồ sơ doanh nghiệp "
            "giúp bạn thấu hiểu cổ phiếu chỉ trong 30 giây.\n\n"
            "✅ Phân tích Mô hình kinh doanh & Chuỗi giá trị.\n"
            "✅ Đánh giá Lợi thế cạnh tranh (Moat) & Vị thế ngành.\n"
            "✅ Nhận diện Rủi ro tiềm ẩn & Ban lãnh đạo.\n\n"
            "Báo cáo chuyên sâu này chỉ dành cho thành viên Pro."
        )
        ), 403

    sym = symbol.upper().strip()
    cache_key = make_profile_cache_key(sym)
    cached = await asyncio.to_thread(get_profile_from_redis, cache_key)

    if not cached:
        return render_template_string(PROFILE_404_TEMPLATE, symbol=sym), 404

    text_json, generated_at, is_error, wait_sec = cached

    if is_error:
        return f"<h3>Đang xử lý hoặc lỗi: {text_json}</h3>", 500

    try:
        data = json.loads(text_json)
    except Exception as e:
        log.error(f"Lỗi parse JSON profile: {e}")
        return "Dữ liệu hồ sơ lỗi format.", 500

    # Map JSON keys sang UI Sections
    SECTION_MAPPING = [
        ("overview",        "Tổng quan",                "🧭"),
        ("products",        "Sản phẩm & Dịch vụ",       "🏭"),
        ("business_model",  "Mô hình kinh doanh",       "🔧"),
        ("market_position", "Vị thế & Thị trường",      "📍"),
        ("value_chain",     "Vị thế chuỗi giá trị",     "🔗"),
        ("moat",            "Lợi thế cạnh tranh",       "🛡️"),
        ("risks",           "Rủi ro chính",             "⚠️"),
        ("leadership",      "Ban lãnh đạo & Cổ đông",   "👔")
    ]

    sections_view = []
    for key, title, icon in SECTION_MAPPING:
        content = data.get(key, "")
        if content:
            sec_id = "sec_" + key
            sections_view.append({
                "id": sec_id,
                "title": title,
                "icon": icon,
                "body": content
            })

    vn_tz = pytz.timezone(TIMEZONE)
    generated_str = ""
    report_code = ""
    if generated_at:
        if generated_at.tzinfo is None:
             generated_at = generated_at.replace(tzinfo=datetime.timezone.utc)
        local_dt = generated_at.astimezone(vn_tz)
        generated_str = local_dt.strftime("%H:%M %d/%m/%Y")
        report_code = f"INFO-{sym}-{local_dt.strftime('%Y%m%d')}"

    # --- [TẠO BIỂU ĐỒ] ---
    chart_div = ""
    try:
        # Gọi hàm từ chart_utils (đã import)
        chart_div = await generate_chart_html(sym)
    except Exception as e:
        log.error(f"Lỗi tạo chart cho {sym}: {e}")
    # ---------------------

    return render_template_string(
        PROFILE_HTML_TEMPLATE,
        symbol=sym,
        sections=sections_view,
        generated_at=generated_str,
        report_code=report_code,
        is_pro=is_pro,
        chart_html=chart_div, # <--- Truyền biểu đồ vào đây
        is_error=False
    )

@flask_app.route("/report/view/<cache_key>")
async def view_report(cache_key):
    """
    Route hiển thị Web App Report (ĐÃ FIX ASYNC & LOG).
    """
    # 1. Check Pro (Giữ nguyên)
    chat_id_str = request.args.get("chat_id")
    is_pro = False
    if chat_id_str:
        try:
            cid = int(chat_id_str)
            is_pro = await asyncio.to_thread(is_user_pro, cid) or (cid == ADMIN_ID)
        except: pass
    
    if not is_pro:
        return render_template_string(LOCKED_FEATURE_TEMPLATE, icon="📊", title="AI Phân Tích Danh Mục", desc=(
            "Trợ lý AI (Gemini) sẽ phân tích chuyên sâu sức khỏe danh mục "
            "đầu tư của bạn dựa trên dữ liệu Real-time.\n\n"
            "✅ Chấm điểm sức khỏe danh mục (Portfolio Score).\n"
            "✅ Khuyến nghị hành động cụ thể: Mua / Bán / Giữ.\n"
            "✅ Phân tích động lực tăng giá & Rủi ro tiềm ẩn.\n\n"
            "Báo cáo tư vấn chi tiết này chỉ dành cho thành viên Pro."
        )), 403

    # 2. Lấy Cache (FIX ASYNC REDIS)
    try:
        # Chạy trong thread để không block Flask async loop
        cached = await asyncio.to_thread(get_report_from_redis, cache_key)
    except Exception as e:
        log.error(f"[{INSTANCE_ID}][VIEW] Lỗi đọc Redis: {e}")
        cached = None

    if not cached:
        # Log debug để biết tại sao 404
        log.warning(f"[{INSTANCE_ID}][VIEW] Cache MISS key: '{cache_key}' -> 404")
        return render_template_string(REPORT_404_TEMPLATE), 404

    text_json, generated_at, is_error, wait_sec = cached
    
    if is_error:
        return f"<h3>Đang gặp lỗi: {text_json}</h3>", 500

    try:
        # Clean lần nữa cho chắc
        clean_text = text_json.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_text)
    except Exception as e:
        log.error(f"[{INSTANCE_ID}][VIEW] Lỗi parse JSON: {e}")
        return "Lỗi dữ liệu báo cáo (JSON Format)", 500

    # 3. Tạo Chart (Giữ nguyên)
    if 'stocks' in data and data['stocks']:
        try:
            tasks = [generate_mini_chart(stock['symbol']) for stock in data['stocks']]
            charts = await asyncio.gather(*tasks)
            for i, stock in enumerate(data['stocks']):
                stock['chart_html'] = charts[i]
        except: pass

    # 4. Render
    vn_tz = pytz.timezone(TIMEZONE)
    time_str = generated_at.astimezone(vn_tz).strftime("%H:%M %d/%m/%Y") if generated_at else ""

    return render_template_string(
        REPORT_HTML_TEMPLATE, 
        data=data, 
        generated_at=time_str,
        is_pro=True 
    )

@flask_app.route("/eod/<eod_id>")
def view_eod(eod_id):
    """Route hiển thị Web App Tổng kết cuối phiên (EOD)"""
    data = get_digest_from_redis(f"eod_web:{eod_id}") # Lưu ý key prefix khác
    
    if not data:
        return render_template_string(EOD_404_TEMPLATE), 404

    return render_template_string(
        EOD_HTML_TEMPLATE, 
        market_data=data.get('market_data'),
        user_stocks=data.get('user_stocks'),
        generated_at=data.get('generated_at'),
        is_pro=data.get('is_pro', False) # Mặc định False, nhưng template đã bỏ badge
    )

@flask_app.route("/chart/<symbol>")
async def view_chart(symbol: str):
    """
    Route hiển thị Flash View (Chart Intraday + Dòng tiền).
    """
    # 1. Lấy dữ liệu (Logic đã gom vào chart_utils)
    data = await get_flash_view_data(symbol.upper())
    
    if not data:
        return "<h3>Chưa có dữ liệu hoặc lỗi kết nối. Vui lòng thử lại sau.</h3>", 404

    # 2. Vẽ Chart (Gọi hàm vẽ UI)
    chart_html = draw_line_chart_fixed_ui(*data['chart_data'])
    orderbook_html = draw_orderbook_fixed_ui(data['price_depth'], data['ref_price'])

    # 3. Render
    return render_template_string(
        FLASH_VIEW_HTML_TEMPLATE,
        symbol=data['symbol'],
        current_price=f"{data['current']:,.0f}",
        change_str=data['change_str'],
        bg_cls=data['bg_cls'],
        cls_color=data['cls_color'],
        
        chart_html=chart_html,
        orderbook_html=orderbook_html,
        
        buy_pct=data['buy_pct'],
        sell_pct=data['sell_pct'],
        buy_vol_str=data['buy_vol_str'],
        sell_vol_str=data['sell_vol_str'],
        
        low_price=data['low'],
        high_price=data['high'],
        range_pct=data['range_pct'],
        
        rsi_val=data['rsi_val'],
        rsi_color=data['rsi_color'],
        rsi_msg=data['rsi_msg'],
        volume_str=data['volume_str']
    )

@flask_app.route("/screener/locked")
def view_screener_locked():
    """
    Route hiển thị giao diện khóa cho Screener (Dành cho Free User).
    Sử dụng LOCKED_FEATURE_TEMPLATE.
    """
    return render_template_string(
        LOCKED_FEATURE_TEMPLATE,
        icon="💎",
        title="Bộ Lọc Giá Trị (Mean Reversion)",
        desc=(
            "Hệ thống tự động quét toàn thị trường để tìm kiếm các cổ phiếu "  # <--- Đã sửa: Thêm dấu cách, bỏ dấu chấm
            "đang bị định giá thấp hơn lịch sử 5 năm (P/E, P/B).\n\n"
            "✅ Tự động loại bỏ cổ phiếu rác.\n"                              # Rút gọn cho đỡ bị ngắt dòng xấu
            "✅ Xếp hạng cơ hội đầu tư thực chiến.\n"
            "✅ Dữ liệu Realtime trong phiên.\n\n"
            "Kết quả lọc chuyên sâu này chỉ dành cho thành viên Pro."
        )
    )

@flask_app.route("/screener_result/<id>")
async def view_screener_result(id): # <--- Chuyển thành ASYNC để gọi DB
    """
    Route hiển thị kết quả Screener Value.
    """
    # 1. Kiểm tra quyền hạn (Paywall Check)
    chat_id_str = request.args.get("chat_id")
    is_pro = False
    if chat_id_str:
        try:
            cid = int(chat_id_str)
            # Gọi hàm check DB trong thread riêng
            is_pro = await asyncio.to_thread(is_user_pro, cid) or (cid == ADMIN_ID)
        except: pass
    
    # Nếu không phải Pro -> Hiển thị giao diện Khóa
    if not is_pro:
        return render_template_string(
            LOCKED_FEATURE_TEMPLATE,
            icon="💎",
            title="Bộ Lọc Giá Trị (Mean Reversion)",
            desc="Kết quả lọc cổ phiếu định giá rẻ chuyên sâu này chỉ dành cho thành viên Pro."
        ), 403

    # 2. Lấy dữ liệu từ Redis (Logic cũ)
    try:
        r = get_redis()
        raw = r.get(f"digest_web:screener_val:{id}")
        
        if not raw:
            return "<h3>Dữ liệu đã hết hạn hoặc không tồn tại. Vui lòng tạo lại lệnh /screener_value</h3>", 404
            
        data = json.loads(raw)
        
        return render_template_string(
            SCREENER_HTML_TEMPLATE, 
            items=data['items'], 
            generated_time=data['generated_time']
        )
    except Exception as e:
        log.error(f"Lỗi render screener_result: {e}")
        return f"Lỗi server: {e}", 500
    
# ==============================================
# 👑 ADMIN DASHBOARD ROUTES
# ==============================================

@flask_app.route("/admin/dashboard")
def admin_dashboard():
    """Trang chủ Admin Dashboard (Đã cập nhật: Full dữ liệu Logs + Orders + Settings)"""
    req_admin_id = request.args.get("admin_id")
    
    # 1. Check quyền Admin
    if not req_admin_id or int(req_admin_id) != ADMIN_ID:
        return "⛔ Access Denied. Chỉ Admin mới được truy cập.", 403

    try:
        # 2. Lấy dữ liệu cơ bản từ DB
        raw_data = get_admin_dashboard_data()
        
        # --- 🔥 [PHẦN MỚI THÊM VÀO] BỔ SUNG DỮ LIỆU CHI TIẾT ---
        for row in raw_data:
            uid = row['id']
            
            # A. Lấy Nhật ký hoạt động (Command Logs)
            try:
                logs = get_user_logs(uid, limit=10)
                # Convert datetime sang ISO string để JSON hiểu được
                for l in logs:
                    if isinstance(l.get('used_at'), (datetime.datetime, datetime.date)):
                        l['used_at'] = l['used_at'].isoformat()
                row['logs'] = logs
            except Exception:
                row['logs'] = []

            # B. Lấy Lịch sử giao dịch (Orders)
            try:
                orders = get_user_orders(uid)
                for o in orders:
                    if isinstance(o.get('created_at'), (datetime.datetime, datetime.date)):
                        o['created_at'] = o['created_at'].isoformat()
                row['orders'] = orders
            except Exception:
                row['orders'] = []

            # C. Lấy Cấu hình (VN30F1M, News...)
            try:
                row['config'] = get_user_configs(uid)
            except Exception:
                row['config'] = {"vn30": False, "news": True}
            
            # D. Parse Watchlist từ JSON string (nếu cần)
            if isinstance(row.get('watchlist'), str):
                try: row['watchlist'] = json.loads(row['watchlist'])
                except: row['watchlist'] = []
        # -------------------------------------------------------

        # --- TÍNH DOANH ---
        # Gọi hàm tính tổng từ bảng orders
        real_revenue = get_total_revenue_real() # <-- Hàm mới viết ở db_utils
        # Format thành dạng: 4.500.000
        revenue_str = "{:,.0f}".format(real_revenue).replace(",", ".")
        
        # 3. Hàm xử lý dữ liệu an toàn (Date, Decimal...)
        def safe_serializer(obj):
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()
            if isinstance(obj, datetime.timedelta):
                return str(obj)
            if isinstance(obj, Decimal):
                return float(obj)
            if hasattr(obj, '__str__'): 
                return str(obj)
            return str(obj)

        # 4. Chuyển thành chuỗi JSON
        users_json = json.dumps(raw_data, default=safe_serializer, ensure_ascii=False)
        
    except Exception as e:
        log.error(f"Lỗi load dashboard data: {e}")
        users_json = "[]"
        revenue_str = "0"

    # 5. Render Template và truyền biến
    return render_template_string(
        ADMIN_MOBILE_TEMPLATE, 
        admin_id=ADMIN_ID,
        initial_data=users_json,
        total_revenue=revenue_str
    )

@flask_app.route("/api/admin/users")
def api_admin_users():
    """API trả về danh sách user JSON (Kèm Logs + Config)"""
    req_admin_id = request.args.get("admin_id")
    try:
        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"error": "Unauthorized"}), 403
    except:
        return jsonify({"error": "Invalid Admin ID"}), 403

    try:
        data = get_admin_dashboard_data()
        
        # Loop từng user để lấy thêm dữ liệu chi tiết
        for row in data:
            uid = row['id']
            
            # 1. Lấy Orders (Code cũ)
            orders = get_user_orders(uid)
            for o in orders:
                if isinstance(o.get('created_at'), (datetime.datetime, datetime.date)):
                    o['created_at'] = o['created_at'].isoformat()
            row['orders'] = orders

            # 2. [MỚI] Lấy Logs hoạt động
            logs = get_user_logs(uid, limit=10)
            for l in logs:
                if isinstance(l.get('used_at'), (datetime.datetime, datetime.date)):
                    l['used_at'] = l['used_at'].isoformat()
            row['logs'] = logs

            # 3. [MỚI] Lấy Cấu hình
            row['config'] = get_user_configs(uid)

            # 4. Xử lý datetime chung cho user object
            for key, val in row.items():
                if isinstance(val, (datetime.datetime, datetime.date)):
                    row[key] = val.isoformat()
            
            if isinstance(row.get('watchlist'), str):
                try: row['watchlist'] = json.loads(row['watchlist'])
                except: row['watchlist'] = []

        # Serialize an toàn
        def safe_serializer(obj):
            if isinstance(obj, (datetime.datetime, datetime.date)):
                return obj.isoformat()
            if isinstance(obj, Decimal):
                return float(obj)
            if hasattr(obj, '__str__'):
                return str(obj)
            return str(obj)

        json_str = json.dumps(data, default=safe_serializer, ensure_ascii=False)
        return Response(json_str, mimetype='application/json')

    except Exception as e:
        log.error(f"[ADMIN_API] Users Error: {e}")
        return jsonify({"error": "Server Error", "message": str(e)}), 500

@flask_app.route("/api/admin/user/extend", methods=["POST"])
def api_admin_extend():
    """API Gia hạn user"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = data.get("target_id")
        days = data.get("days")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        # Gọi hàm gia hạn có sẵn
        add_paid_user(int(target_id), int(days))
        
        # Gửi thông báo cho user qua Telegram
        # Lưu ý: Cần dùng run_coroutine_threadsafe vì Flask chạy khác thread với Bot
        if tg_app and MAIN_LOOP:
            msg_text = f"🎁 TÀI KHOẢN ĐƯỢC GIA HẠN!\nAdmin vừa cộng thêm *{days} ngày* Pro cho bạn. Hạn dùng mới đã được cập nhật."
            asyncio.run_coroutine_threadsafe(
                send_md(tg_app.bot, int(target_id), msg_text),
                MAIN_LOOP
            )

        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"[ADMIN_API] Extend Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/user/deactivate", methods=["POST"])
def api_admin_deactivate_user():
    """API Ngưng kích hoạt gói Pro"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = data.get("target_id")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        # Gọi hàm DB để set ngày hết hạn về quá khứ
        rows = deactivate_paid_user(int(target_id))
        
        if rows > 0:
            # Gửi thông báo cho user
            if tg_app and MAIN_LOOP:
                msg_text = "⚠️ **Thông báo:** Gói Pro của bạn đã bị ngưng kích hoạt bởi Admin.\nVui lòng liên hệ hỗ trợ nếu có thắc mắc."
                asyncio.run_coroutine_threadsafe(
                    send_md(tg_app.bot, int(target_id), msg_text),
                    MAIN_LOOP
                )
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": False, "message": "User này chưa kích hoạt Pro."}), 400

    except Exception as e:
        log.error(f"[ADMIN_API] Deactivate Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/user/message", methods=["POST"])
def api_admin_send_message():
    """API Gửi tin nhắn trực tiếp cho user"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = data.get("target_id")
        text = data.get("text")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403
        
        if not text:
            return jsonify({"ok": False, "message": "Nội dung trống"}), 400

        # Gửi qua Bot
        if tg_app and MAIN_LOOP:
            # Thêm prefix để user biết là từ Admin
            final_text = f"💌 **Tin nhắn từ Admin:**\n\n{text}"
            asyncio.run_coroutine_threadsafe(
                send_md(tg_app.bot, int(target_id), final_text),
                MAIN_LOOP
            )
            return jsonify({"ok": True})
        
        return jsonify({"ok": False, "message": "Bot not ready"}), 500

    except Exception as e:
        log.error(f"[ADMIN_API] Send Message Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/user/contact", methods=["POST"])
def api_admin_request_contact():
    """API gửi link chat (Markdown) về cho admin rồi đóng webapp"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = data.get("target_id")
        target_name = data.get("target_name")
        username = data.get("username")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        # Tạo nội dung tin nhắn
        if username:
            # Case 1: Có username -> Dùng link t.me chuẩn
            display_link = f"https://t.me/{username}"
            msg_text = (
                f"👤 **Contact:** {target_name}\n"
                f"🔗 Link: {display_link}\n\n"
                f"👉 [Bấm vào đây để chat với @{username}]({display_link})"
            )
        else:
            # Case 2: Không có username -> Dùng Markdown Link với ID
            # Cú pháp: [Text hiển thị](tg://user?id=123456) <- Đây là cách duy nhất hoạt động ổn định
            deep_link = f"tg://user?id={target_id}"
            msg_text = (
                f"👤 **Contact:** {target_name} (ID: `{target_id}`)\n"
                f"⚠️ User này chưa đặt Username.\n\n"
                f"👉 [Bấm vào đây để mở chat riêng]({deep_link})"
            )

        # Gửi tin nhắn
        if tg_app and MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(
                send_md(tg_app.bot, int(req_admin_id), msg_text),
                MAIN_LOOP
            )
            return jsonify({"ok": True})
        
        return jsonify({"ok": False, "message": "Bot not ready"}), 500

    except Exception as e:
        log.error(f"[ADMIN_API] Request Contact Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/user/note", methods=["POST"])
async def api_admin_save_note():
    """API Lưu ghi chú admin"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = data.get("target_id")
        note = data.get("note")

        # Check quyền Admin
        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        # Gọi hàm DB (chạy trong thread để không block bot)
        await asyncio.to_thread(update_user_admin_note, int(target_id), note)

        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"[ADMIN_API] Save Note Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/user/ban", methods=["POST"])
async def api_admin_ban_user():
    """API Chặn / Bỏ chặn user (Có gửi thông báo)"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        target_id = int(data.get("target_id"))
        action = data.get("action") # 'ban' hoặc 'unban'

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        should_ban = (action == 'ban')
        
        # 1. Cập nhật DB (Async)
        await asyncio.to_thread(set_user_ban_status, target_id, should_ban)
        
        # 2. Cập nhật RAM Cache ngay lập tức
        if should_ban:
            BANNED_CACHE.add(target_id)
            log.info(f"[ADMIN] ⛔ Đã BAN user {target_id}")
            
            # Nội dung tin nhắn khi BAN
            msg_text = (
                "⛔ **THÔNG BÁO: TÀI KHOẢN BỊ KHÓA**\n\n"
                "Tài khoản của bạn đã bị chặn truy cập Bot do vi phạm chính sách hoặc nghi vấn Spam.\n"
                "Vui lòng liên hệ Admin @KhoiTran99 để được hỗ trợ."
            )
        else:
            if target_id in BANNED_CACHE:
                BANNED_CACHE.remove(target_id)
            log.info(f"[ADMIN] ✅ Đã UNBAN user {target_id}")
            
            # Nội dung tin nhắn khi UNBAN
            msg_text = (
                "✅ **THÔNG BÁO: TÀI KHOẢN ĐƯỢC MỞ KHÓA**\n\n"
                "Quyền truy cập Bot của bạn đã được khôi phục.\n"
                "Chúc bạn đầu tư hiệu quả! 🚀"
            )

        # 3. Gửi thông báo tới User
        # Sử dụng run_coroutine_threadsafe để đảm bảo an toàn luồng giữa Flask và Telegram Bot
        if tg_app and MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(
                send_md(tg_app.bot, target_id, msg_text),
                MAIN_LOOP
            )

        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"[ADMIN_API] Ban Error: {e}")
        return jsonify({"ok": False, "message": str(e)}), 500


# ==============================================
# 🚀 CẤU TRÚC KHỞI ĐỘNG (Phần code mới)
# Đây là phần code đã sửa lỗi hoàn toàn
# ==============================================

# 1. Đặt các biến này ở global (ngoài main)
# để các hàm helper (lifespan, wrapper) có thể truy cập
wsgi_app = WsgiToAsgi(flask_app)

ENV_MODE = os.getenv("ENV_MODE", "production").lower()
IS_PRODUCTION = os.getenv("RENDER") == "true" or ENV_MODE == "production"

async def set_telegram_webhook():
    """
    Hàm này CHỈ thực hiện VIỆC set/update webhook.
    """
    global tg_app, log, INSTANCE_ID

    webhook_url = None
    
    # Ưu tiên 1: Production Render URL
    if IS_PRODUCTION:
        webhook_url = os.getenv("RENDER_EXTERNAL_URL")
        if webhook_url and not webhook_url.endswith("/webhook"):
            webhook_url += "/webhook"
    
    # Ưu tiên 2: Local Ngrok URL (Nếu không phải Production)
    elif os.getenv("NGROK_URL"):
        webhook_url = os.getenv("NGROK_URL")
        if webhook_url and not webhook_url.endswith("/webhook"):
            webhook_url += "/webhook"

    # --- NẾU KHÔNG CÓ URL -> KHÔNG LÀM GÌ (ĐỂ POLLING LO) ---
    if not webhook_url:
        log.info(f"[{INSTANCE_ID}] [Lifespan] Không tìm thấy URL Webhook. Sẽ chuyển sang chế độ Polling.")
        return

    # --- THỰC HIỆN SET WEBHOOK ---
    log.info(f"[{INSTANCE_ID}] [Lifespan] Đang set webhook tới: {webhook_url}")
    try:
        success = await tg_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
        if success:
            log.info(f"[{INSTANCE_ID}] ✅ [Lifespan] Webhook đã set thành công!")
        else:
            log.error(f"[{INSTANCE_ID}] ❌ [Lifespan] API set_webhook trả về 'False'.")
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] ❌ [Lifespan] Lỗi set webhook: {e}")

async def run_telegram_processing():
    """
    Quyết định chạy Polling hay chờ Webhook.
    """
    global tg_app, log, INSTANCE_ID, IS_PRODUCTION
    
    await tg_app.initialize()
    await tg_app.start()
    
    # Kiểm tra xem có URL webhook không
    has_webhook_url = False
    if IS_PRODUCTION and os.getenv("RENDER_EXTERNAL_URL"):
        has_webhook_url = True
    elif os.getenv("NGROK_URL"):
        has_webhook_url = True

    # --- TRƯỜNG HỢP 1: CHẠY POLLING (Local không Ngrok) ---
    if not has_webhook_url:
        log.info(f"[{INSTANCE_ID}] [MODE] Không có Webhook URL -> Chuyển sang chạy POLLING...")
        
        # Xóa webhook cũ để tránh xung đột
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
        
        # Bật Polling
        await tg_app.updater.start_polling(
            drop_pending_updates=True, 
            allowed_updates=Update.ALL_TYPES
        )
        log.info(f"[{INSTANCE_ID}] [MODE] Polling đã bắt đầu.")
        
        # Giữ cho task này sống mãi mãi
        await asyncio.Event().wait()

    # --- TRƯỜNG HỢP 2: CHẠY WEBHOOK (Production hoặc Local + Ngrok) ---
    else:
        log.info(f"[{INSTANCE_ID}] [MODE] Đang chạy chế độ WEBHOOK. `run_telegram_processing` sẽ ngủ để duy trì background tasks.")
        # Chỉ cần ngủ để giữ task không bị đóng
        await asyncio.Event().wait()

async def asgi_wrapper_app(scope, receive, send):
    global wsgi_app, log, tg_app, IS_PRODUCTION
    global BACKGROUND_TASKS, MAIN_LOOP
    global ADMIN_ID, initial_active, INSTANCE_ID
 
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                log.info("[Lifespan] Server startup...")
                
                # 1. Cố gắng set webhook (nếu có URL)
                await set_telegram_webhook()
                
                # 2. Báo cho Hypercorn biết là startup xong (QUAN TRỌNG)
                await send({"type": "lifespan.startup.complete"})
                
                # 3. Khởi động các tác vụ nền
                log.info("[Lifespan] Starting background tasks...")
                
                # ... (Giữ nguyên phần list BACKGROUND_TASKS của bạn) ...
                # Chú ý: Đảm bảo list BACKGROUND_TASKS của bạn đã đầy đủ như file cũ
                
                BACKGROUND_TASKS = [
                    MAIN_LOOP.create_task(alert_loop()),
                    MAIN_LOOP.create_task(stock_price_fetcher_loop()),
                    MAIN_LOOP.create_task(stock_broadcast_loop()),
                    MAIN_LOOP.create_task(vn30f1m_alert_loop()),
                    MAIN_LOOP.create_task(vn30f1m_price_fetcher_loop()),
                    MAIN_LOOP.create_task(vn30f1m_broadcast_loop()),
                    MAIN_LOOP.create_task(news_specialized_loop()),
                    MAIN_LOOP.create_task(news_macro_loop()),
                    MAIN_LOOP.create_task(news_cleanup_loop()),
                    MAIN_LOOP.create_task(session_notice_loop()),
                    MAIN_LOOP.create_task(weekly_report_loop()),
                    MAIN_LOOP.create_task(analysis_report_loop()),
                    MAIN_LOOP.create_task(financial_Statements_notice_loop()),
                    MAIN_LOOP.create_task(nightly_valuation_loop()),
                    MAIN_LOOP.create_task(daily_user_digest_loop()),
                    MAIN_LOOP.create_task(restore_reminder_loop()),
                    MAIN_LOOP.create_task(run_background_startup_tasks(ADMIN_ID, initial_active, INSTANCE_ID, tg_app)),
                    MAIN_LOOP.create_task(auto_on_after_delay(initial_active)),
                ]
                log.info(f"[Lifespan] Đã khởi động {len(BACKGROUND_TASKS)} tác vụ nền.")
            
            elif message["type"] == "lifespan.shutdown":
                log.info("[Lifespan] Server shutdown.")
                for task in BACKGROUND_TASKS:
                    task.cancel()
                await send({"type": "lifespan.shutdown.complete"})
                break
    
    elif scope["type"] == "http":
        await wsgi_app(scope, receive, send)

# Đặt hàm này bên trên hàm main()
async def run_background_startup_tasks(admin_id: int | None, initial_active: bool, instance_id: str, app: telegram.ext.Application):
    """
    Chạy các tác vụ khởi động chậm (I/O, network) trong nền
    sau khi máy chủ web đã khởi động.
    """
    try:
        # Tác vụ 1: Đăng ký lệnh bot (network call) - Thêm (admin) để phân loại commands của users và admin
        commands = [
            # --- CÁC LỆNH CỐT LÕI ---
            ("start", "🏠 Mở Dashboard chính"),
            ("admin", "(admin) Mở Dashboard Admin"),
            ("on", "(admin) Bật bot (thoát chế độ bảo trì)"),
            ("off", "(admin) Tắt bot (bảo trì tạm thời)"),
            ("status", "(admin) Kiểm tra trạng thái hoạt động của bot"),
            ("announce", "(admin) Gửi thông báo đến tất cả người dùng"),
            ("allwatch", "(admin) Thống kê toàn bộ danh sách theo dõi của user"),
            ("screener_value_clear", "(admin) Xóa dữ liệu screener cache"),
            ("report_clear", "(admin) Xóa dữ liệu AI report trên redis"),
            ("info_clear", "(admin) Xóa dữ liệu AI hồ sơ (/info)"),
            ("delete_range", "(admin) Xóa tin nhắn bot gửi trong khoảng thời gian"),
            ("news_test_macro", "(admin) Gửi thử tin tức vĩ mô mới nhất"),
            ("news_test_specialized", "(admin) Gửi thử tin tức vĩ mô mới nhất"),
            ("cmd_run_weekly_report_now", "(admin) Chạy và gửi Weekly Report ngay lập tức"),
            ("backup_core", "(admin) Backup dữ liệu core (watchlist, news_pref, BCTC)"),
            ("restore_core", "(admin) Khôi phục dữ liệu core từ file backup"),
            ("admin_add_user", "(admin) (admin) Thêm/gia hạn Gói Pro cho user"),
            ("admin_deactivate", "(admin) Ngưng hoạt động Gói Pro của user"),
            ("admin_remove_user", "(admin) Xoá vĩnh viễn Gói Pro của user"),
            ("test_digest", "(admin) Gửi bản tin test Web App ngay lập tức"),
        ]
        

        # Tách commands
        user_cmds = [(c, d) for c, d in commands if "(admin)" not in d]
        admin_cmds = commands

        # 1) Set commands cho USERS: tất cả private chats
        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in user_cmds],
            scope=BotCommandScopeAllPrivateChats(),
        )

        # 2) Set commands cho ADMIN: private chat 1088200599
        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in admin_cmds],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )

        log.info(f"[{instance_id}] ✅ Đã đăng ký danh sách lệnh Telegram thành công.")

    except Exception as e:
        log.warning(f"[{instance_id}] Lỗi khi set_my_commands: {e}")

    try:
        # Tác vụ 2: Gửi thông báo cho admin (blocking I/O + network)
        if admin_id:
            # Chạy các hàm blocking trong thread riêng
            def _build_admin_message():
                # Lấy thông tin hệ thống (dùng interval=None để không block)
                cpu_percent = psutil.cpu_percent(interval=0.1) 
                ram = psutil.virtual_memory()
                ram_used = ram.used / (1024 * 1024)
                ram_total = ram.total / (1024 * 1024)
                uptime_seconds = time.time() - psutil.boot_time()
                uptime_days = int(uptime_seconds // 86400)
                uptime_hours = int((uptime_seconds % 86400) // 3600)
                uptime_mins = int((uptime_seconds % 3600) // 60)

                def progress_bar(percent: float, length: int = 10):
                    filled = int((percent / 100) * length)
                    empty = length - filled
                    return "█" * filled + "░" * empty

                cpu_bar = progress_bar(cpu_percent)
                ram_percent = (ram_used / ram_total) * 100
                ram_bar = progress_bar(ram_percent)

                state_text = (
                    "🟢 Bot đã khởi động và đang *hoạt động bình thường.*"
                    if initial_active
                    else "🔴 Bot đã khởi động nhưng đang ở *chế độ bảo trì.*"
                )

                boot_time = datetime.datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")

                auto_on_notice = ""
                if not initial_active:
                    auto_on_notice = "✅ *Hệ thống sẽ được kích hoạt trở lại sau 2 phút (auto /on).*"

                git_info = get_git_deploy_info() # <- Đây là blocking call (subprocess)

                parts = [
                    f"🚀 *Chatbot đã khởi động lại thành công!*\n\n"
                    f"🕓 Thời gian: {boot_time}\n"
                    f"{state_text}\n\n"
                    f"🧠 CPU [{cpu_bar}] {cpu_percent:.1f}%\n"
                    f"🦾 RAM [{ram_bar}] {ram_percent:.1f}%\n"
                    f"📡 Uptime server: {uptime_days}d {uptime_hours}h {uptime_mins}m\n"
                    f"🧩 Instance ID: `{instance_id}`"
                ]

                if git_info:
                    parts.append(f"`{git_info}`")
                if auto_on_notice:
                    parts.append(auto_on_notice)

                return "\n\n".join(parts)

            # Chạy hàm build message (có I/O) trong thread
            msg = await asyncio.to_thread(_build_admin_message)
            
            # Chạy hàm gửi tin (network) trong thread
            await asyncio.to_thread(send_msg_to, admin_id, msg)
            log.info(f"[{instance_id}] Đã gửi thông báo khởi động lại tới admin ({admin_id}).")

    except Exception as e:
        log.warning(f"[{instance_id}] Lỗi khi gửi thông báo khởi động lại cho admin: {e}")

# ==============================================
# MAIN (HÀM MAIN MỚI, ĐÃ SỬA LỖI)
# ==============================================
async def main():
    log.info(f"[{INSTANCE_ID}] ✅ Starting bot main()...")
    log.info(f"[{INSTANCE_ID}] 🚀 Chế độ IS_PRODUCTION: {IS_PRODUCTION}")

    # 🗄️ Khởi tạo DB
    init_db()

    # 🏢 Load danh sách tên doanh nghiệp
    global COMPANY_KEYWORDS
    COMPANY_KEYWORDS = load_company_keywords_from_csv("ssi_master_list.csv")

    # 🔄 Load trạng thái bảo trì từ DB
    global BOT_ACTIVE, MAIN_LOOP, tg_app

    global initial_active

    MAIN_LOOP = asyncio.get_running_loop()
    BOT_ACTIVE = get_bot_active()
    initial_active = BOT_ACTIVE  # lưu trạng thái ban đầu
    log.info(f"[{INSTANCE_ID}] BOT_ACTIVE loaded from DB: {BOT_ACTIVE}")

    # Tăng timeout lên 60 giây để tránh lỗi trên Render Free
    t_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0
    )

    # Khởi tạo Application với request tùy chỉnh
    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(t_request)
        .concurrent_updates(True)
        .build()
    )

    # User commands
    tg_app.add_handler(CommandHandler("help", cmd_help))
    tg_app.add_handler(CommandHandler("setting", cmd_setting))
    tg_app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("report", cmd_report))
    tg_app.add_handler(CommandHandler("report_clear", cmd_report_clear))
    tg_app.add_handler(CommandHandler("info", cmd_info))
    tg_app.add_handler(CommandHandler("info_clear", cmd_info_clear))
    tg_app.add_handler(CommandHandler("screener_value", cmd_screener_value))
    tg_app.add_handler(CommandHandler("vn30f1m_status", cmd_vn30f1m_status))
    tg_app.add_handler(CommandHandler("vn30f1m_on", cmd_vn30f1m_on))
    tg_app.add_handler(CommandHandler("vn30f1m_off", cmd_vn30f1m_off))
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("trial", cmd_trial))

    # Admin commands
    tg_app.add_handler(CommandHandler("news_test_macro", cmd_news_test_macro))
    tg_app.add_handler(CommandHandler("news_test_specialized", cmd_news_test_specialized))
    tg_app.add_handler(CommandHandler("on", cmd_on))
    tg_app.add_handler(CommandHandler("off", cmd_off))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("announce", cmd_announce))
    tg_app.add_handler(CommandHandler("delete_range", cmd_delete_range))
    tg_app.add_handler(CommandHandler("screener_value_clear", cmd_screener_value_clear))
    tg_app.add_handler(CommandHandler("backup_core", cmd_backup_core))
    tg_app.add_handler(CommandHandler("restore_core", cmd_restore_core))
    tg_app.add_handler(CommandHandler("admin_add_user", cmd_admin_add_user))
    tg_app.add_handler(CommandHandler("admin_deactivate", cmd_admin_deactivate))
    tg_app.add_handler(CommandHandler("admin_remove_user", cmd_admin_remove_user))
    tg_app.add_handler(CommandHandler("cmd_run_weekly_report_now", cmd_run_weekly_report_now))
    tg_app.add_handler(CommandHandler("test_digest", cmd_admin_test_digest))
    tg_app.add_handler(CommandHandler("admin", cmd_admin))
    tg_app.add_handler(MessageHandler(filters.TEXT, unknown_message))
    tg_app.add_handler(CallbackQueryHandler(handle_quick_button))

    # 🆕 Bắt case gửi file JSON + caption /restore_core
    tg_app.add_handler(
        MessageHandler(
            filters.Document.ALL & filters.CaptionRegex(r"^/restore_core(@\w+)?"),
            cmd_restore_core,
        )
    )

    tg_app.add_handler(MessageHandler(filters.TEXT, unknown_message))
    
    # Cấu hình máy chủ web
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    log.info(f"[{INSTANCE_ID}] Cấu hình hoàn tất. Khởi động máy chủ và các tác vụ nền...")

    # 🔥 LOAD BLACKLIST VÀO RAM
    global BANNED_CACHE
    try:
        BANNED_CACHE = await asyncio.to_thread(get_banned_users)
        log.info(f"[{INSTANCE_ID}] ⛔ Đã load {len(BANNED_CACHE)} users vào danh sách chặn (Blacklist).")
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] ❌ Lỗi load Blacklist: {e}")

    # Chạy tất cả các tác vụ song song
    await asyncio.gather(
        # 1. ⚡ MỞ PORT (Hàm này sẽ tự động gọi logic lifespan ở trên)
        serve(asgi_wrapper_app, config),           
        
        # 2. Chạy logic xử lý của Telegram (sẽ tự động chuyển sang Polling nếu cần)
        run_telegram_processing(),
    )


if __name__ == "__main__":
    log.info("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
