import os
import json
import random
import datetime
import asyncio
import pytz
import requests
import pandas as pd
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
from flask import Flask, request, jsonify, render_template_string
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

# --- HÀM HELPER VẼ THANH TIẾN TRÌNH ---
def make_progress_bar(percent: int, width: int = 8) -> str:
    """Tạo thanh loading dạng text: ▰▰▰▱▱"""
    filled = int(width * percent / 100)
    empty = width - filled
    return "▰" * filled + "▱" * empty


# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PASSENGER_PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

# === VALUE SCREENER (API) CONFIG ===
VALUE_SCREENER_REDIS_KEY_PREFIX = "value_screener_api"
VALUE_SCREENER_MIN_LIQUIDITY = 50_000_000_000      # 50 tỷ
VALUE_SCREENER_MIN_ASSET = 5_000_000_000_000       # 5000 tỷ

# 🗂 Thư mục tạm dùng chung cho backup/restore (tự động phù hợp Windows / Linux)
TMP_DIR = tempfile.gettempdir()

# Cấu hình batch cho screener Value
VALUE_BATCH_SIZE = 20       # 20 mã / batch
VALUE_BATCH_SLEEP = 2       # nghỉ 2 giây giữa các batch
MIN_PENNY_PRICE = 15000      # Giá tối thiểu (VND) để KHÔNG bị coi là penny

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

# Thiết lập Gemini API cho báo cáo danh mục
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    log.warning(
        "⚠️ GEMINI_API_KEY chưa được cấu hình – chức năng báo cáo tuần và /report sẽ không hoạt động."
    )

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

# ==============================================
# EOD SUMMARY MỚI (WEB APP + AI)
# ==============================================

async def call_gemini_eod_insight(market_data: dict) -> str:
    """
    Gọi Gemini nhận định thị trường cuối ngày (1 lần duy nhất).
    Trả về JSON string chứa field 'ai_comment'.
    """
    if not GEMINI_API_KEY:
        return "AI chưa được cấu hình."

    prompt = f"""
    Đóng vai chuyên gia chứng khoán, nhận định thị trường cuối phiên hôm nay dựa trên dữ liệu:
    {json.dumps(market_data, ensure_ascii=False)}
    
    Yêu cầu:
    - Ngắn gọn (<100 từ), xúc tích.
    - Có nhận xét Xu hướng, Dòng tiền (Khối ngoại/Thanh khoản).
    - Đưa ra 1 lời khuyên hành động cho ngày mai.
    - Dùng emoji phù hợp.
    
    OUTPUT JSON FORMAT:
    {{ "ai_comment": "Nội dung nhận định..." }}
    """
    
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        text = getattr(resp, "text", "")
        if text:
            data = json.loads(text)
            return data.get("ai_comment", "")
    except Exception as e:
        log.error(f"[EOD] Lỗi Gemini: {e}")
        
    return "Thị trường biến động. Hãy quan sát kỹ dòng tiền."

async def send_eod_summary():
    """
    Gửi Tổng kết cuối phiên (EOD) dạng Web App.
    Chạy lúc 15:15.
    (ĐÃ SỬA: Dùng fetch_data_smart để tự động chuyển nguồn VCI -> TCBS)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_str = now.strftime("%d/%m/%Y")
    
    log.info(f"[{INSTANCE_ID}][EOD] 🚀 Bắt đầu quy trình EOD Summary {today_str}...")

    # 1. Lấy danh sách User
    try:
        all_watch = await asyncio.to_thread(get_all_watch)
        if not all_watch:
            log.info(f"[{INSTANCE_ID}][EOD] Không có user nào, bỏ qua.")
            return
    except Exception as e:
        log.error(f"[{INSTANCE_ID}][EOD] Lỗi get_all_watch: {e}")
        return

    # 2. Gom toàn bộ mã để lấy giá 1 lần (Batching)
    all_symbols = set()
    for block in all_watch.values():
        for s in block.get("list", []):
            all_symbols.add(str(s).upper())
    
    # Thêm các chỉ số thị trường
    MARKET_IDXS = ["VNINDEX", "VN30"]
    for idx in MARKET_IDXS: all_symbols.add(idx)
    
    if not all_symbols: return

    # 3. GỌI SMART FETCHER (Thay vì gọi trực tiếp Trading VCI)
    # Hàm này sẽ tự lo việc thử VCI, nếu lỗi thì qua TCBS, và trả về dict chuẩn
    try:
        # Chia batch nếu quá nhiều (ví dụ > 50 mã) để tránh timeout 20s của Smart Fetcher
        # Ở đây mình gọi 1 lần cho đơn giản, nếu danh sách > 50 mã nên chia nhỏ
        data_source = await fetch_data_smart(list(all_symbols))
    except Exception as e:
        log.error(f"[{INSTANCE_ID}][EOD] Lỗi Smart Fetcher: {e}")
        return

    if not data_source:
        log.warning(f"[{INSTANCE_ID}][EOD] Không lấy được dữ liệu thị trường.")
        return

    # 4. Chuẩn bị dữ liệu Thị trường chung (Market Data)
    # Hàm helper lấy từ dict đã chuẩn hóa
    def _get_market_info(sym):
        if sym not in data_source:
            return {"price": "---", "change": 0, "pct": 0}
        
        item = data_source[sym]
        p = item['price']
        pct = item['pct']
        # Tính change (tương đối)
        ref = item.get('ref', p)
        change = p - ref
        
        return {
            "price": f"{p:,.2f}", # Index thường có số lẻ
            "change": round(change, 2),
            "pct": round(pct, 2)
        }

    vnindex = _get_market_info("VNINDEX")
    vn30 = _get_market_info("VN30")
    
    # Lưu ý: Smart Fetcher hiện tại chưa trả về f_net (Khối ngoại) khi fallback TCBS
    # Nên ta tạm để 0 hoặc update fetch_data_smart sau nếu cần thiết
    total_f_net = 0 

    market_data_input = {
        "vnindex": vnindex,
        "vn30": vn30,
        "foreign_net_val": round(total_f_net, 1)
    }

    # 5. Gọi AI (1 lần duy nhất)
    ai_comment = await call_gemini_eod_insight(market_data_input)
    market_data_input["ai_comment"] = ai_comment 

    # 6. Tạo và gửi Web App cho từng user
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    tasks = []

    for chat_key, block in all_watch.items():
        try:
            chat_id = int(chat_key)
            watch_list = block.get("list", [])
            if not watch_list: continue

            # Build danh sách cổ phiếu riêng của user từ data_source
            user_stocks = []
            for sym in watch_list:
                sym_u = str(sym).upper()
                if sym_u in data_source:
                    info = data_source[sym_u]
                    price_fmt = f"{info['price']:,.0f}".replace(",", ".") # Cổ phiếu thường không có số lẻ
                    user_stocks.append({
                        "symbol": sym_u,
                        "price": price_fmt,
                        "pct": round(info['pct'], 2)
                    })
            
            if not user_stocks: continue

            # Tạo Payload
            payload = {
                "market_data": market_data_input,
                "user_stocks": user_stocks,
                "generated_at": datetime.datetime.now(vn_tz).strftime("%H:%M %d/%m"),
                "is_pro": False 
            }

            # Lưu Redis
            digest_id = uuid.uuid4().hex
            r = get_redis()
            r.set(f"digest_web:eod_web:{digest_id}", json.dumps(payload), ex=86400)
            
            web_app_url = f"{base_url}/eod/{digest_id}"
            
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📊 Xem Tổng Kết Phiên", web_app=WebAppInfo(url=web_app_url))
            ]])
            
            msg = (
                f"🇻🇳 *Tổng kết phiên {today_str}*\n"
                f"VN-INDEX: {vnindex['price']} ({vnindex['pct']}%)\n"
                f"👉 Nhấn nút bên dưới để xem chi tiết."
            )
            
            tasks.append(send_md(tg_app.bot, chat_id, msg, reply_markup=kb, msg_type='EOD_SUMMARY'))

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Lỗi gửi cho {chat_key}: {e}")

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        
    log.info(f"[{INSTANCE_ID}][EOD] ✅ Đã gửi EOD cho {len(tasks)} users.")
    
    # Kích hoạt dọn dẹp sau 10s
    await asyncio.sleep(10)
    from alert_bot import cleanup_after_eod
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


# Trong file alert_bot.py

def call_chatgpt_for_report(symbols: list[str]) -> str:
    """
    (PHIÊN BẢN JSON - CÓ XUỐNG DÒNG)
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY chưa được cấu hình.")

    if len(symbols) > 6:
        symbols = symbols[:6]

    symbols_str = ", ".join(symbols)
    vn_tz = pytz.timezone(TIMEZONE)
    date_str = datetime.datetime.now(vn_tz).strftime('%d/%m/%Y')

    prompt = f"""
Bạn là chuyên gia phân tích chứng khoán Việt Nam theo chiến lược đầu tư tăng trưởng.
Hãy phân tích danh mục đầu tư trung–dài hạn (3–12 tháng) cho các mã sau: {symbols_str} (Ngày báo cáo: {date_str}).

YÊU CẦU FORMAT OUTPUT:
Trả về kết quả dưới định dạng **JSON thuần**.
Cấu trúc JSON bắt buộc:
{{
  "general_market_comment": "Đoạn văn (khoảng 3-4 câu) tổng quan về thị trường và định hướng danh mục.",
  "portfolio_health_score": 8.5,
  "stocks": [
    {{
      "symbol": "MÃ",
      "industry": "Tên ngành",
      "action": "Mua / Nắm giữ / Bán / Theo dõi",
      "analysis": "Phân tích chi tiết (500-700 ký tự). BẮT BUỘC trình bày thành các ý gạch đầu dòng (•), mỗi ý MỘT DÒNG RIÊNG BIỆT. Bao gồm: KQKD, Lợi thế, Động lực, Rủi ro.\\nVí dụ format:\\n• Vị thế: Dẫn đầu ngành...\\n• KQKD: Tăng trưởng 20%...\\n• Rủi ro: Tỷ giá...",
      "key_metrics": "P/E: 10.x, LNST tăng 20%..."
    }}
  ]
}}

LƯU Ý:
- Trường `analysis` phải chứa các ký tự xuống dòng (\\n) để tách ý.
- Giọng văn chuyên nghiệp, khách quan.
"""

    log.info(f"[{INSTANCE_ID}] Gọi Gemini (JSON Mode) cho báo cáo: {symbols_str}")

    client = genai.Client(api_key=GEMINI_API_KEY)
    model_id = "gemini-2.5-flash-lite"

    try:
        resp = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        text = getattr(resp, "text", "")
        if not text:
            raise RuntimeError("Gemini trả về rỗng.")
        
        return text.strip()

    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi Gemini Report JSON: {e}")
        raise e

# ==============================================
# HÀM GỌI GEMINI CHO HỒ SƠ DOANH NGHIỆP (/info)
# ==============================================

def call_gemini_for_profile(symbol: str) -> str:
    """
    (PHIÊN BẢN JSON) Gọi Gemini tạo hồ sơ doanh nghiệp.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY chưa được cấu hình.")

    sym = symbol.upper().strip()
    log.info(f"[{INSTANCE_ID}] Gọi Gemini (JSON) cho hồ sơ: {sym}")

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

    client = genai.Client(api_key=GEMINI_API_KEY)
    model_id = "gemini-2.5-flash-lite"

    try:
        resp = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        text = getattr(resp, "text", "")
        if not text:
            raise RuntimeError("Gemini trả về rỗng.")
        return text.strip()

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

async def handle_quick_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý sự kiện khi user bấm vào nút gợi ý (Add / Info).
    """

    query = update.callback_query

    # 🔥 CHẶN TẠI ĐÂY: Nếu bot đang bảo trì, không cho bấm nút gì cả
    if not BOT_ACTIVE:
        await query.answer("⚙️ Hệ thống đang bảo trì.", show_alert=True)
        return
    
    # Báo cho Telegram biết đã nhận click (để tắt vòng xoay loading trên nút)
    try:
        await query.answer()
    except BadRequest as e:
        # Nếu lỗi là "Query is too old" -> Bỏ qua (chuyện bình thường)
        if "Query is too old" in str(e):
            pass
        else:
            # Nếu là lỗi khác -> Ghi log cảnh báo để Admin biết
            log.warning(f"⚠️ Lỗi nút bấm (BadRequest): {e}")
    except Exception as e:
        # Lỗi không mong muốn khác -> Ghi log
        log.warning(f"⚠️ Lỗi lạ khi answer callback: {e}")
    
    data = query.data

    # --- XỬ LÝ CHUNG CHO NÚT ĐÓNG ---
    if data == "close_msg":
        # Xóa tin nhắn hiện tại
        await query.delete_message()
        return

    # --- XỬ LÝ MENU DASHBOARD ---
    elif data == "menu_list":
        await cmd_list(update, context)
    
    elif data == "menu_add":
        # Tạo nút Đóng
        kb = [[InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]

        # Hướng dẫn user cách thêm vì /add cần tham số
        await query.message.reply_text(
            "➕ Để thêm mã, bạn hãy gõ trực tiếp mã 3 chữ cái (VD: `HPG`, `FPT`) vào ô chat.\n"
            "Hoặc gõ lệnh: `/add <MÃ>`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    
    elif data == "menu_screener":
        # Gọi hàm screener mặc định (hiện menu chọn loại)
        context.args = [] # Xóa args cũ để nó hiện menu chọn
        await cmd_screener_value(update, context)
        
    elif data == "menu_report":
        await cmd_report(update, context)
        
    elif data == "menu_setting":
        await cmd_setting(update, context)
        
    elif data == "menu_help":
        await cmd_help(update, context)
    
    # Xử lý nút ADD
    elif data.startswith("btn_add_"):
        symbol = data.split("_")[2]
        chat_id = update.effective_chat.id
        
        # --- LOGIC THÊM MÃ (SILENT) ---
        
        # 1. Lấy dữ liệu hiện tại
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        
        # 2. Kiểm tra đã tồn tại chưa
        if symbol in lst:
            await query.answer(f"⚠️ {symbol} đã có trong danh sách!", show_alert=True)
            # Vẫn chuyển sang màn hình danh sách để user thấy
        
        else:
            # 3. Kiểm tra Paywall (Free max 1 mã)
            is_pro = await asyncio.to_thread(is_user_pro, chat_id)
            is_admin = (chat_id == ADMIN_ID)
            
            if not is_pro and not is_admin and len(lst) >= 1:
                await query.answer(
                    "⚠️ Bản Free chỉ theo dõi được 1 mã.\nVui lòng nâng cấp Pro!", 
                    show_alert=True
                )
                return # Dừng lại, không chuyển trang

            # 4. Kiểm tra mã có hợp lệ không (Gọi Smart Fetcher check nhanh)
            # (Optional: Nếu muốn nhanh tuyệt đối có thể bỏ qua bước này, nhưng check thì an toàn hơn)
            data_check = await fetch_data_smart([symbol])
            if not data_check:
                await query.answer("⚠️ Mã không tồn tại hoặc lỗi dữ liệu.", show_alert=True)
                return

            # 5. Lưu vào DB
            lst.append(symbol)
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)
            await query.answer(f"✅ Đã thêm {symbol} thành công!")

        # --- CHUYỂN HƯỚNG VỀ WATCHLIST DASHBOARD (Re-render) ---
        
        # Xây dựng bàn phím danh sách (Giống hệt back_to_list)
        keyboard = []
        row = []
        for sym in lst:
            row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        # Nút đóng/quay lại
        keyboard.append([InlineKeyboardButton("❌ Đóng danh sách", callback_data="close_list")])

        # Biến hình tin nhắn hiện tại
        await query.edit_message_text(
            text=f"📋 **Quản lý danh mục**\n(Vừa thêm: **{symbol}**)\n\n👇 Bấm vào mã để xem tùy chọn:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    # Xử lý nút INFO
    elif data.startswith("btn_info_"):
        symbol = data.split("_")[2]
        
        # Giả lập tham số context.args
        context.args = [symbol]
        
        # Gọi hàm cmd_info
        await cmd_info(update, context)

    elif data.startswith("mgr_"):
        # Khi user bấm vào 1 mã trong danh sách (VD: HPG)
        symbol = data.split("_")[1]
        
        # Hiện submenu cho mã đó (Sửa lại tin nhắn hiện tại thay vì gửi tin mới)
        kb = [
            [
                InlineKeyboardButton("📄 Soi hồ sơ", callback_data=f"btn_info_{symbol}"),
                InlineKeyboardButton("🗑️ Xóa mã này", callback_data=f"btn_del_{symbol}")
            ],
            [InlineKeyboardButton("🔙 Quay lại danh sách", callback_data="back_to_list")]
        ]
        
        await query.edit_message_text(
            f"⚙️ **Tùy chọn cho {symbol}**:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("btn_del_"):
        # Xử lý xóa nhanh và quay lại danh sách (Dashboard)
        symbol = data.split("_")[2]
        chat_id = update.effective_chat.id
        
        # 1. Thực hiện xóa trong DB
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
        
        if symbol in lst:
            lst.remove(symbol)
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)
            # Hiện thông báo nhỏ (Toast) phía trên màn hình
            await query.answer(f"🗑️ Đã xóa {symbol} khỏi danh mục!")
        else:
            await query.answer(f"⚠️ {symbol} không còn trong danh mục.", show_alert=False)
            # Vẫn load lại danh sách để đồng bộ
            
        # 2. Vẽ lại giao diện danh sách (Giống logic back_to_list)
        if not lst:
             await query.edit_message_text(
                 "📭 Danh mục của bạn đã trống.\n\nDùng `/add <MÃ>` để thêm mã mới.",
                 parse_mode="Markdown"
             )
             return

        # Xây dựng bàn phím danh sách (3 mã/hàng)
        keyboard = []
        row = []
        for sym in lst:
            row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("❌ Đóng danh sách", callback_data="close_list")])

        # 3. Cập nhật lại tin nhắn hiện tại (Thay vì gửi tin mới)
        try:
            await query.edit_message_text(
                text=f"📋 **Quản lý danh mục**\n(Đã xóa: **{symbol}**)\n\n👇 Bấm vào mã để xem tùy chọn:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception:
            # Trường hợp danh sách không đổi (hiếm), bỏ qua lỗi MessageNotModified
            pass
        
    elif data == "back_to_list":
        # 1. Lấy lại danh sách từ DB
        chat_id = update.effective_chat.id
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []

        if not lst:
             await query.edit_message_text("📭 Danh mục trống. Dùng `/add <MÃ>` để thêm.")
             return

        # 2. Xây dựng lại bàn phím danh sách (Giống hệt logic trong cmd_list)
        keyboard = []
        row = []
        for sym in lst:
            row.append(InlineKeyboardButton(f"{sym}", callback_data=f"mgr_{sym}"))
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("❌ Đóng danh sách", callback_data="close_list")])

        # 3. [QUAN TRỌNG] Dùng edit_message_text để ghi đè lên tin nhắn cũ
        # Thay vì gọi await cmd_list(update, context)
        await query.edit_message_text(
            text="📋 **Quản lý danh mục**\nBấm vào mã để xem tùy chọn:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        
    elif data == "close_list":
        await query.delete_message()
    
    elif data == "v30_on":
        # Gọi logic bật
        await cmd_vn30f1m_on(update, context)
        # Sau khi bật xong, cập nhật lại giao diện nút thành "Tắt ngay" (UX cao cấp)
        # (Bạn có thể gọi lại cmd_vn30f1m_status để refresh cái message đó)
        
    elif data == "v30_off":
        await cmd_vn30f1m_off(update, context)

    # [MỚI] XỬ LÝ CÀI ĐẶT
    elif data == "btn_upgrade":
        # Gọi lệnh upgrade để hiện mã QR
        await cmd_upgrade(update, context)

    # ✅ DÁN ĐOẠN NÀY VÀO
    # --- XỬ LÝ BẬT/TẮT VN30 (IN-PLACE UPDATE) ---
    elif data in ("set_vn30_on", "set_vn30_off"):
        chat_id = update.effective_chat.id
        want_turn_on = (data == "set_vn30_on")

        # 1. Kiểm tra Paywall (Nếu muốn BẬT)
        if want_turn_on:
            is_pro = await asyncio.to_thread(is_user_pro, chat_id)
            is_admin = (chat_id == ADMIN_ID)
            if not is_pro and not is_admin:
                # Hiện Popup cảnh báo giữa màn hình
                await query.answer("⚠️ Tính năng này chỉ dành cho Gói Pro.\nVui lòng chọn 'Nâng cấp' ở trên!", show_alert=True)
                return

        # 2. Thực hiện lưu DB
        await asyncio.to_thread(set_vn30f1m_enabled, chat_id, want_turn_on)
        reload_vn30f1m_enabled_cache() # Refresh cache RAM ngay lập tức

        # 3. Hiện thông báo nhỏ (Toast)
        status_toast = "✅ Đã BẬT" if want_turn_on else "🚫 Đã TẮT"
        await query.answer(f"{status_toast} cảnh báo phái sinh!")

        # 4. VẼ LẠI MENU SETTING (Re-render)
        vn_tz = pytz.timezone(TIMEZONE)
        now = datetime.datetime.now(vn_tz)
        
        # Lấy lại thông tin user để vẽ lại text
        expiry_date = await asyncio.to_thread(get_user_pro_expiry, chat_id)
        
        lines = ["⚙️ *CÀI ĐẶT & TRẠNG THÁI TÀI KHOẢN* ⚙️\n"]
        
        if chat_id == ADMIN_ID:
            lines.append("👤 *Gói cước:* 😎 *ADMIN* (Full quyền)")
        elif expiry_date and expiry_date.astimezone(vn_tz) > now:
            exp_str = expiry_date.astimezone(vn_tz).strftime("%H:%M %d/%m/%Y")
            lines.append(f"👤 *Gói cước:* 👑 *PRO*")
            lines.append(f"⏳ *Hết hạn:* {exp_str}")
        else:
            lines.append("👤 *Gói cước:* 🆓 *FREE*")
            lines.append("_Giới hạn: 1 mã theo dõi, không có AI Report & Screener._")

        lines.append("\n📰 *Bản tin sáng (Digest)*")
        lines.append("✅ Trạng thái: *TỰ ĐỘNG (07:00)*")

        lines.append("\n📈 *Cảnh báo VN30F1M*")
        # Dùng biến want_turn_on để hiển thị trạng thái mới nhất
        if want_turn_on:
            lines.append("✅ Trạng thái: *ĐANG BẬT*")
            btn_vn30 = InlineKeyboardButton("🔴 Tắt VN30F1M", callback_data="set_vn30_off")
        else:
            lines.append("❌ Trạng thái: *ĐANG TẮT*")
            btn_vn30 = InlineKeyboardButton("🟢 Bật VN30F1M", callback_data="set_vn30_on")

        # Build lại Keyboard
        kb = [
            [InlineKeyboardButton("💎 Nâng cấp / Gia hạn Pro", callback_data="btn_upgrade")],
            [btn_vn30], # Nút này đã được đảo chiều
            [InlineKeyboardButton("❌ Đóng", callback_data="close_setting")]
        ]

        # Cập nhật tin nhắn cũ
        try:
            await query.edit_message_text(
                text="\n".join(lines),
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        except Exception:
            pass
        
    elif data == "close_setting":
        # Xóa tin nhắn cài đặt cho gọn màn hình
        await query.delete_message()

    # [MỚI] XỬ LÝ NÚT TỪ MENU HELP
    elif data == "back_to_start":
        # Gọi lại lệnh start để hiện Dashboard
        await cmd_start(update, context)
        # Tùy chọn: Xóa tin nhắn Help cũ cho gọn
        # await query.delete_message()

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

# =====================================================================
# =============== VALUE SCREENER (VNSTOCK API VERSION) ================
# =====================================================================

def build_value_df_from_screener(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Chuẩn hoá DataFrame từ Screener().stock(...) về dạng:
    symbol, floor, industry, pe, pb, roe, liquidity_proxy, asset_proxy
    """
    df = df_raw.copy()

    df["symbol"] = df["ticker"].astype(str).str.upper().str.strip()

    def map_floor(exchange: str) -> str:
        if not isinstance(exchange, str):
            return "UNKNOWN"
        ex = exchange.upper()
        if ex in ("HOSE", "HSX"):
            return "HOSE"
        if ex == "HNX":
            return "HNX"
        if ex == "UPCOM":
            return "UPCOM"
        return "UNKNOWN"

    df["floor"] = df["exchange"].apply(map_floor)
    df["industry"] = df["industry"].fillna("Khác").astype(str)

    # PE / PB / ROE
    for col in ["pe", "pb", "roe"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Thanh khoản (tỷ → VND)
    if "avg_trading_value_20d" in df.columns:
        df["liquidity_proxy"] = (
            pd.to_numeric(df["avg_trading_value_20d"], errors="coerce").fillna(0) * 1e9
        )
    else:
        df["liquidity_proxy"] = (
            pd.to_numeric(df["total_trading_value"], errors="coerce").fillna(0) * 1e9
        )

    # Tài sản (market_cap tỷ → VND)
    df["asset_proxy"] = (
        pd.to_numeric(df["market_cap"], errors="coerce").fillna(0) * 1e9
    )

    return df[[
        "symbol",
        "floor",
        "industry",
        "pe",
        "pb",
        "roe",
        "liquidity_proxy",
        "asset_proxy",
    ]]


def run_value_screener_on_value_df(df: pd.DataFrame, screener_type: str) -> Optional[dict]:
    """
    Hàm logic cốt lõi (đã test).
    Chạy logic screener (pe, pb, roe, all) từ DataFrame đã chuẩn hoá.
    """
    df_clean = df.copy()

    # --- BƯỚC 1: BỘ LỌC CƠ SỞ (Áp dụng cho TẤT CẢ) ---
    
    # Lọc sàn
    df_clean = df_clean[df_clean['floor'].isin(['HOSE', 'HNX'])]
    
    # Lọc thanh khoản và vốn hóa
    df_clean = df_clean[df_clean['liquidity_proxy'] >= VALUE_SCREENER_MIN_LIQUIDITY]
    df_clean = df_clean[df_clean['asset_proxy'] >= VALUE_SCREENER_MIN_ASSET]
    
    # Lọc logic (Thống nhất 1: Loại bỏ NaN và các giá trị <= 0)
    df_clean = df_clean.dropna(subset=['pe', 'pb', 'roe'])
    df_clean = df_clean[(df_clean['pe'] > 0) & (df_clean['pb'] > 0) & (df_clean['roe'] > 0)]

    total_all = len(df) # Tổng số mã nhận vào (trước khi lọc)
    after_base_filter = len(df_clean) # Số mã còn lại sau lọc cơ sở

    if df_clean.empty:
        log.warning("[VALUE_SCREENER_API] Không còn mã nào sau khi qua lọc cơ sở.")
        return None # Trả về None nếu không có mã nào pass

    # Chuẩn hoá ROE về decimal (chỉ dùng cho 'all' và format)
    df_clean['roe_decimal'] = df_clean['roe'] / 100.0

    # --- BƯỚC 2: LOGIC XẾP HẠNG (Tùy theo loại) ---
    
    industries_data = [] # List cuối cùng chứa kết quả
    sort_column = ""
    sort_ascending = True

    if screener_type == 'all':
        # Logic 'all' cũ: Cần tính value_score
        industry_stats = df_clean.groupby("industry").agg(
            pe_industry=("pe", "mean"),
            pb_industry=("pb", "mean"),
            roe_industry=("roe_decimal", "mean"),
        ).reset_index()

        df_clean = df_clean.merge(industry_stats, on="industry", how="left")
        
        # Thống nhất 2: Nếu ngành không có ROE > 0 thì cũng bỏ
        df_clean = df_clean[df_clean["roe_industry"] > 0]

        if not df_clean.empty:
            df_clean["pe_rel"] = df_clean["pe_industry"] / df_clean["pe"]
            df_clean["pb_rel"] = df_clean["pb_industry"] / df_clean["pb"]
            df_clean["roe_rel"] = df_clean["roe_decimal"] / df_clean["roe_industry"]

            df_clean["value_score"] = (
                df_clean["pe_rel"] * 0.4 +
                df_clean["pb_rel"] * 0.3 +
                df_clean["roe_rel"] * 0.3
            ).round(2)
        
        sort_column = "value_score"
        sort_ascending = False # Điểm cao tốt hơn

    elif screener_type == 'pe':
        sort_column = "pe"
        sort_ascending = True # P/E thấp tốt hơn

    elif screener_type == 'pb':
        sort_column = "pb"
        sort_ascending = True # P/B thấp tốt hơn

    elif screener_type == 'roe':
        sort_column = "roe" # Dùng cột % (22.0) thay vì decimal (0.22)
        sort_ascending = False # ROE cao tốt hơn
    
    # --- BƯỚC 3: GROUP BY NGÀNH VÀ LẤY TOP 5 ---
    
    if sort_column not in df_clean.columns:
        # Xảy ra khi 'all' không tính được value_score (ví dụ df_clean rỗng)
        log.warning(f"[VALUE_SCREENER_API] Lỗi: Cột sắp xếp '{sort_column}' không tồn tại.")
        return None

    # Sắp xếp toàn bộ df trước
    df_clean = df_clean.sort_values(by=sort_column, ascending=sort_ascending)

    # Lặp qua các ngành
    for industry, group in df_clean.groupby('industry'):
        top_20_rows = group.head(20) # Lấy top 20 để có nhiều lựa chọn hơn
        
        if top_20_rows.empty:
            continue
            
        rows_list = []
        for _, r in top_20_rows.iterrows():
            # Thêm tất cả data cần thiết cho 4 chế độ format
            rows_list.append({
                "symbol": r["symbol"],
                "pe": float(r["pe"]),
                "pb": float(r["pb"]),
                "roe": float(r["roe_decimal"]), # Luôn dùng decimal cho thống nhất
                "value_score": float(r.get("value_score", 0.0)),
                # 3 field cho format 'all'
                "pe_industry": float(r.get("pe_industry", 0.0)),
                "pb_industry": float(r.get("pb_industry", 0.0)),
                "roe_industry": float(r.get("roe_industry", 0.0)),
            })

        industries_data.append({
            "industry": industry,
            "rows": rows_list,
            # Dùng best_score để sort các ngành (cho 'all')
            "best_score": rows_list[0].get(sort_column, 0.0)
        })

    # Chỉ sort các ngành theo điểm khi là 'all' hoặc 'roe' (cao -> thấp)
    if screener_type in ['all', 'roe']:
        industries_data.sort(key=lambda x: x["best_score"], reverse=True)
    else:
        # PE, PB (thấp -> cao)
        industries_data.sort(key=lambda x: x["best_score"], reverse=False)

    # Giới hạn số ngành (giữ nguyên)
    industries_data = industries_data[:19]

    # (Phần Top 5 toàn thị trường của 'all' không còn cần thiết,
    # vì 'all' giờ là một loại screener riêng)

    vn_tz = pytz.timezone(TIMEZONE)
    as_of = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d %H:%M")

    return {
        "as_of": as_of,
        "screener_type": screener_type, # Quan trọng: Thêm loại vào kết quả
        "stats": {
            "total_all": total_all,
            "after_base_filter": after_base_filter,
        },
        "industries": industries_data,
        "top_all": [] # Tạm thời không dùng
    }


def run_value_screener_from_api(screener_type: str) -> Optional[dict]:
    """
    Hàm pipeline đầy đủ: Gọi API -> Chuẩn hóa -> Chạy Logic.
    """
    log.info(f"[VALUE_SCREENER_API] Gọi vnstock Screener() (loại: {screener_type})...")
    try:
        df_raw = Screener().stock(
            params={"exchangeName": "HOSE,HNX,UPCOM"},
            limit=1700,
        )
    except Exception as e:
        log.exception(f"[VALUE_SCREENER_API] Lỗi gọi API: {e}")
        return None

    if df_raw is None or len(df_raw) == 0:
        return None

    # Chạy chuẩn hóa
    value_df = build_value_df_from_screener(df_raw)
    
    # Chạy logic
    return run_value_screener_on_value_df(value_df, screener_type)

# -------------------------- Redis Helper --------------------------

def _value_screener_key_today(screener_type: str) -> str:
    """
    Tạo key Redis theo ngày VÀ theo loại.
    Ví dụ: value_screener_api:2025-11-16:pe
           value_screener_api:2025-11-16:all
    """
    vn_tz = pytz.timezone(TIMEZONE)
    today = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")
    stype = screener_type.strip().lower()
    return f"{VALUE_SCREENER_REDIS_KEY_PREFIX}:{today}:{stype}"


def _ttl_until_midnight() -> int:
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=3, microsecond=0
    )
    return max(60, int((midnight - now).total_seconds()))


def load_value_screener_from_redis(screener_type: str) -> Optional[dict]:
    """Lấy cache từ Redis (đã nhận screener_type)."""
    try:
        r = get_redis()
        # Key mới đã bao gồm screener_type
        key = _value_screener_key_today(screener_type)
        raw = r.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def save_value_screener_to_redis(result: dict, screener_type: str):
    """Lưu cache vào Redis (đã nhận screener_type)."""
    try:
        r = get_redis()
        # Key mới đã bao gồm screener_type
        key = _value_screener_key_today(screener_type)
        
        # Thêm screener_type vào payload để hàm format tự nhận diện
        result_to_save = result.copy()
        result_to_save['screener_type'] = screener_type
        
        r.set(
            key, 
            json.dumps(result_to_save, ensure_ascii=False), 
            ex=_ttl_until_midnight()
        )
        log.info(f"[VALUE_SCREENER_API] Đã lưu cache (loại: {screener_type}) trong ngày.")
    except Exception as e:
        log.warning(f"[VALUE_SCREENER_API] Lỗi lưu Redis (loại: {screener_type}): {e}")


def compute_value_screener(
    top_per_industry: int = 5,
    max_industries: int = 19,
):
    """
    So sánh cổ phiếu (đã tính toán trước) và tính điểm value_score.
    (ĐÃ SỬA: Dùng bộ lọc nghiêm ngặt, loại bỏ None/NaN/<=0)
    """
    rows = load_stock_value_cache()
    if not rows:
        return None

    df = pd.DataFrame(rows)
    
    required_cols = {
        "symbol", "industry", "pe", "pb", "roe", "floor",
        "asset_proxy", "liquidity_proxy"
    }
    if not required_cols.issubset(df.columns):
        log.error(
            f"[{INSTANCE_ID}][VALUE] Dữ liệu cache thiếu cột. Columns: {df.columns.tolist()}"
        )
        return None

    df["symbol"] = df["symbol"].astype(str).str.upper()

    try:
        industry_map = load_industry_map_from_csv("ssi_master_list.csv")
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi load ssi_master_list.csv: {e}")
        industry_map = {}

    if industry_map:
        before = len(df)
        df = df[df["symbol"].isin(industry_map.keys())]
        log.info(
            f"[{INSTANCE_ID}][VALUE] Lọc theo industry_map: {before} -> {len(df)} mã."
        )
        if df.empty:
            log.warning(f"[{INSTANCE_ID}][VALUE] Không còn mã sau khi lọc industry_map.")
            return None
        df["industry"] = df["symbol"].map(industry_map).fillna(df["industry"])

    # ❗️ BỘ LỌC NGHIÊM NGẶT
    # 1. Loại bỏ BẤT KỲ hàng nào có None (NaN)
    df = df.dropna(
        subset=[
            "pe", "pb", "roe", "industry", "floor",
            "asset_proxy", "liquidity_proxy"
        ]
    )

    # 2. Loại bỏ BẤT KỲ hàng nào có chỉ số <= 0
    df = df[(df["pe"] > 0) & (df["pb"] > 0) & (df["roe"] > 0)]

    if df.empty:
        log.warning(
            f"[{INSTANCE_ID}][VALUE] Không còn mã nào sau khi lọc nghiêm ngặt (dropna và > 0)."
        )
        return None

    # Chuẩn hoá ROE về decimal
    df["roe_decimal"] = df["roe"] / 100.0

    # Tính trung bình ngành
    industry_stats = df.groupby("industry").agg(
        pe_industry=("pe", "mean"),
        pb_industry=("pb", "mean"),
        roe_industry=("roe_decimal", "mean"),
    ).reset_index()

    df = df.merge(industry_stats, on="industry", how="left")

    # Loại ngành ROE ngành <= 0
    df = df[df["roe_industry"] > 0]
    if df.empty:
        log.warning(f"[{INSTANCE_ID}][VALUE] Không còn mã sau khi lọc theo ROE ngành > 0.")
        return None

    # Tính tương đối so với ngành
    df["pe_rel"] = df["pe_industry"] / df["pe"]
    df["pb_rel"] = df["pb_industry"] / df["pb"]
    df["roe_rel"] = df["roe_decimal"] / df["roe_industry"]

    df["value_score"] = (
        df["pe_rel"] * 0.4 +
        df["pb_rel"] * 0.3 +
        df["roe_rel"] * 0.3
    ).round(2)

    # Top từng ngành
    industries_data = []
    for industry, g in df.groupby("industry"):
        rows = g.sort_values("value_score", ascending=False).head(top_per_industry)
        if rows.empty:
            continue

        rows_list = []
        for _, r in rows.iterrows():
            rows_list.append({
                "symbol": r["symbol"],
                "pe": float(r["pe"]),
                "pb": float(r["pb"]),
                "roe": float(r["roe_decimal"]),     # decimal
                "value_score": float(r["value_score"]),

                # 3 FIELD BẮT BUỘC CHO FORMATTER CŨ
                "pe_industry": float(r["pe_industry"]) if pd.notna(r["pe_industry"]) else None,
                "pb_industry": float(r["pb_industry"]) if pd.notna(r["pb_industry"]) else None,
                "roe_industry": float(r["roe_industry"]) if pd.notna(r["roe_industry"]) else None,
            })

        industries_data.append({
            "industry": industry,
            "rows": rows_list,
            "best_score": rows_list[0]["value_score"],
        })

    industries_data.sort(key=lambda x: x["best_score"], reverse=True)
    industries_data = industries_data[:max_industries]

    # Top toàn thị trường
    top_all = df.sort_values("value_score", ascending=False).head(5)
    top_all_list = []
    for _, r in top_all.iterrows():
        top_all_list.append({
            "symbol": r["symbol"],
            "industry": r["industry"],
            "pe": float(r["pe"]),
            "pb": float(r["pb"]),
            "roe": float(r["roe_decimal"]),
            "value_score": float(r["value_score"]),
        })

    vn_tz = pytz.timezone(TIMEZONE)
    as_of = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d %H:%M")

    return {
        "as_of": as_of,
        "stats": {
            "total_all": len(df),
        },
        "industries": industries_data,
        "top_all": top_all_list,
    }


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

def format_screener_report_text(result: dict) -> str | None:
    """
    Từ kết quả (đã bao gồm screener_type), format ra tin nhắn Markdown.
    (ĐÃ GỠ BỎ logic truncating/rút gọn. Sẽ trả về text đầy đủ)
    """
    if not result or not result.get("industries"):
        log.warning(f"[{INSTANCE_ID}][SCREENER] Không có dữ liệu để format báo cáo.")
        return None

    as_of = result.get("as_of")
    industries = result.get("industries", [])
    
    screener_type = result.get("screener_type", "all")

    lines: list[str] = []

    # --- 1. Tùy chỉnh Tiêu đề và Mô tả ---
    if screener_type == 'all':
        lines.append("💰 *Top 5 cổ phiếu Value (Tổng hợp) theo ngành*")
        lines.append("_Dựa trên điểm Value Score (P/E, P/B, ROE so với ngành)_")
    elif screener_type == 'pe':
        lines.append("💰 *Top 5 cổ phiếu P/E Thấp nhất (theo ngành)*")
        lines.append("_Đã lọc P/E > 0_")
    elif screener_type == 'pb':
        lines.append("💰 *Top 5 cổ phiếu P/B Thấp nhất (theo ngành)*")
        lines.append("_Đã lọc P/B > 0_")
    elif screener_type == 'roe':
        lines.append("💰 *Top 5 cổ phiếu ROE Cao nhất (theo ngành)*")
        lines.append("_Đã lọc ROE > 0_")

    if as_of:
        lines.append(f"_Cập nhật đến: {as_of}_")
    
    lines.append("")
    lines.append("📊 *Tiêu chí lọc cơ sở (áp dụng cho mọi loại):*")
    lines.append("• Chỉ lấy các cổ phiếu sàn HOSE/HNX")
    lines.append("• Thanh khoản TB > 50 tỷ/ngày")
    lines.append("• Vốn hóa > 5,000 tỷ")
    lines.append("• P/E > 0, P/B > 0, và ROE > 0")
    lines.append("")

    # --- 2. Tùy chỉnh vòng lặp hiển thị ---
    for industry_block in industries:
        industry_name = industry_block["industry"] or "Khác"
        display_industry = industry_name
        
        first_row = industry_block["rows"][0]

        # A. Dòng tiêu đề Ngành
        if screener_type == 'all':
            pe_avg = first_row["pe_industry"]
            pb_avg = first_row["pb_industry"]
            roe_avg = first_row["roe_industry"]
            lines.append(
                f"🏷 *Ngành: {display_industry}* "
                f"(P/E TB: {pe_avg:.1f} | P/B TB: {pb_avg:.1f} | ROE TB: {format_roe_pct(roe_avg)})"
            )
        else:
            lines.append(f"🏷 *Ngành: {display_industry}*")

        # B. Dòng chi tiết Cổ phiếu
        for idx, r in enumerate(industry_block["rows"], start=1):
            if screener_type == 'all':
                lines.append(
                    f"{idx}️⃣ *{r['symbol']}* – "
                    f"Score {r['value_score']:.2f} | "
                    f"P/E {r['pe']:.1f} | "
                    f"P/B {r['pb']:.1f} | "
                    f"ROE {format_roe_pct(r['roe'])}"
                )
            elif screener_type == 'pe':
                lines.append(
                    f"{idx}️⃣ *{r['symbol']}* – "
                    f"P/E *{r['pe']:.1f}* | "
                    f"P/B {r['pb']:.1f} | "
                    f"ROE {format_roe_pct(r['roe'])}"
                )
            elif screener_type == 'pb':
                lines.append(
                    f"{idx}️⃣ *{r['symbol']}* – "
                    f"P/B *{r['pb']:.1f}* | "
                    f"P/E {r['pe']:.1f} | "
                    f"ROE {format_roe_pct(r['roe'])}"
                )
            elif screener_type == 'roe':
                 lines.append(
                    f"{idx}️⃣ *{r['symbol']}* – "
                    f"ROE *{format_roe_pct(r['roe'])}* | "
                    f"P/E {r['pe']:.1f} | "
                    f"P/B {r['pb']:.1f}"
                )

        lines.append("")

    lines.append(
        "_Lưu ý: Đây là bảng xếp hạng định lượng, nhà đầu tư nên kết hợp phân tích cơ bản & kỹ thuật để ra quyết định._"
    )

    text = "\n".join(lines)
    
    # === GỠ BỎ KHỐI TRUNCATE (if len(text) > 3900) TẠI ĐÂY ===
    
    return text

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
# SMART DATA FETCHER (Circuit Breaker Mode)
# ==============================================
async def fetch_data_smart(symbols: list[str]) -> dict[str, dict]:
    """
    Hàm Async lấy dữ liệu thông minh (VCI Snapshot -> Fallback TCBS 1 phút).
    Cập nhật: 
    - Xử lý VN30F1M = 0 từ VCI.
    - Dùng nến 1m của TCBS để có giá sát thực tế nhất.
    - Tự động fix lỗi đơn vị giá (x1000) của TCBS.
    """
    global _vci_blocked_date
    
    results = {}
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_date = now.date()

    # 1. KIỂM TRA TRẠNG THÁI VCI
    skip_vci = (_vci_blocked_date == today_date)

    if not skip_vci:
        # --- THỬ NGUỒN VCI (Ưu tiên vì nhanh) ---
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
                        # Parse MultiIndex an toàn
                        sym = str(row.get(('listing', 'symbol'))).upper().strip()
                        match_p = float(row.get(('match', 'match_price'), 0))
                        ref_p = float(row.get(('listing', 'ref_price'), 0))
                        
                        # [FIX QUAN TRỌNG] Nếu VCI trả về 0 (thường gặp ở Phái sinh), dùng tham chiếu
                        # Hoặc nếu muốn chính xác tuyệt đối: bỏ qua để Fallback sang TCBS
                        if match_p == 0:
                            if ref_p > 0:
                                match_p = ref_p 
                            else:
                                # Nếu cả khớp và tham chiếu đều 0/lỗi -> Bỏ qua để TCBS xử lý
                                continue

                        pct = 0.0
                        if ref_p > 0:
                            pct = ((match_p - ref_p) / ref_p) * 100.0

                        results[sym] = {"price": match_p, "pct": pct, "ref": ref_p}
                    except: continue
                
                # Nếu lấy đủ số lượng mã yêu cầu thì trả về luôn
                # Nếu thiếu (do VCI lỗi 1 vài mã), code sẽ chạy tiếp xuống TCBS để bù vào
                if len(results) == len(symbols):
                    return results
                
        except asyncio.TimeoutError:
            log.warning(f"[SMART] ⏳ VCI timeout. Chuyển sang TCBS.")
            _vci_blocked_date = today_date
        except Exception as e:
            log.warning(f"[SMART] ❌ VCI lỗi: {e}. Chuyển sang TCBS.")
            _vci_blocked_date = today_date

    # --- NGUỒN DỰ PHÒNG: TCBS (Dùng Nến 1 Phút) ---
    # Chạy khi VCI lỗi, timeout, bị chặn, hoặc thiếu mã
    missing_symbols = [s for s in symbols if s not in results]
    if not missing_symbols:
        return results

    try:
        # Lấy data 2 ngày gần nhất để đảm bảo có nến (phòng trường hợp đầu phiên sáng sớm)
        today_str = today_date.strftime("%Y-%m-%d")
        start_str = (today_date - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        
        def _run_tcbs_1m_fallback(syms_to_run):
            tcbs_results = {}
            for sym in syms_to_run:
                try:
                    quote = Quote(symbol=sym, source="TCBS")
                    # [UPDATE] Dùng interval='1m' thay vì '1D'
                    df = quote.history(start=start_str, end=today_str, interval="1m")
                    
                    if df is not None and not df.empty:
                        # Lấy cây nến cuối cùng (mới nhất)
                        last_row = df.iloc[-1]
                        
                        # Giá hiện tại (Close của nến phút cuối)
                        current_price = float(last_row['close'])
                        
                        # [FIX] Xử lý đơn vị giá (TCBS trả về 27.25 -> 27250)
                        if current_price < 500: 
                            current_price *= 1000

                        # Để tính % thay đổi, ta cần giá tham chiếu.
                        # Với nến 1m, khó lấy tham chiếu chính xác. 
                        # Cách tạm thời: Lấy giá open của ngày hoặc nến đầu tiên trong ngày.
                        # Tuy nhiên, để đơn giản và nhanh: ta tính pct dựa trên biến động nến cuối
                        # Hoặc chấp nhận pct = 0 nếu không có ref chuẩn.
                        # Ở đây mình giả lập ref_price bằng giá đóng cửa cây nến liền trước đó (nếu có)
                        
                        ref_price = current_price # Default
                        if len(df) >= 2:
                            prev_close = float(df.iloc[-2]['close'])
                            if prev_close < 500: prev_close *= 1000
                            ref_price = prev_close
                        
                        # Tính % (So với nến phút trước - Biến động tức thời)
                        # Lưu ý: Đây không phải % so với tham chiếu ngày, nhưng đủ để bot alert biến động
                        pct = 0.0
                        if ref_price > 0:
                            pct = ((current_price - ref_price) / ref_price) * 100.0

                        tcbs_results[sym] = {"price": current_price, "pct": pct, "ref": ref_price}
                except: continue
            return tcbs_results

        # Chạy TCBS trong thread
        tcbs_data = await asyncio.to_thread(_run_tcbs_1m_fallback, missing_symbols)
        results.update(tcbs_data)
                
    except Exception as e:
        log.error(f"[SMART] ❌ TCBS 1m Fatal Error: {e}")
        
    return results

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
FETCHER_INTERVAL_SECONDS = 15 # Tần suất Fetcher (gọi API)

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
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    log.info(f"[{INSTANCE_ID}][TICKER_STOCK] Bắt đầu. Mốc khởi tạo = GIÁ THAM CHIẾU (0%).")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # --- HEARTBEAT LOG (MỚI): Log mỗi 20 vòng (khoảng 60s) ---
        if loop_id % 20 == 0:
            log.info(f"[{INSTANCE_ID}][TICKER_STOCK] 💓 Heartbeat: Đang theo dõi {len(_stock_current_watch_cache)} user. Bot vẫn sống.")

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

            for chat_key, user_block in all_watch.items():
                try:
                    chat_id = int(chat_key)
                except Exception:
                    continue

                watch_list = user_block.get("list", []) or []
                if not watch_list:
                    continue

                if chat_key not in all_state:
                    all_state[chat_key] = {}
                personal_state = all_state[chat_key]

                messages: list[str] = []

                for sym in watch_list:
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

                    # ⚡ LOGIC QUAN TRỌNG NHẤT ĐÂY ⚡
                    # 1. Nếu chưa có last_pct (mới thêm mã) -> Gán = 0.0 (Tham chiếu)
                    # 2. Nếu qua ngày mới -> Reset về 0.0 (Tham chiếu)
                    if last_pct is None or (last_alert_at and last_alert_at.date() != now.date()):
                        last_pct = 0.0 
                        last_alert_at = None # Coi như chưa báo hôm nay

                    # === TÍNH TOÁN BIẾN ĐỘNG ===
                    # Delta = % Hiện tại - % Mốc cũ
                    # Ví dụ: Mốc cũ (Tham chiếu) = 0.0. Hiện tại = -2.1%. Delta = 2.1 -> BÁO
                    delta_pct = float(pct) - float(last_pct)
                    should_alert = abs(delta_pct) >= 2.0 # Ngưỡng 2%

                    if should_alert:
                        # 1. Chuẩn bị dữ liệu hiển thị
                        icon = "🟢" if pct >= 0 else "🔴"
                        direction = "tăng" if pct >= 0 else "giảm"
                        
                        # Format giá: 20.000
                        price_str = f"{float(price):,.0f}".replace(",", ".")
                        # Format %: +2.10%
                        pct_str = f"{float(pct):+.2f}%"
                        
                        # Câu thoại vui
                        fun_line = random.choice(FUN_UP if pct >= 0 else FUN_DOWN)

                        # 2. Tạo nội dung tin nhắn (GỌN GÀNG)
                        # Mẫu: 🟢 HPG tăng +2.10% Giá hiện tại: 25.000
                        msg = (
                            f"{icon} * {sym_u} {direction} {pct_str} Giá hiện tại: {price_str}*\n"
                            f"_{fun_line}_"
                        )
                        
                        # TẠO NÚT KÈM THEO ALERT
                        kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("Soi hồ sơ", callback_data=f"btn_info_{sym_u}")]
                        ])

                        messages.append(msg)
                        
                        # 3. Cập nhật mốc mới vào State
                        personal_state[sym_u] = {
                            "last_pct": float(pct),
                            "last_alert_at": now.isoformat(),
                        }
                    
                    # Nếu chưa đủ 2% để báo, nhưng là ngày mới/mã mới, ta vẫn phải lưu mốc 0.0 vào state
                    # để vòng lặp sau có cái mà so sánh (tránh trường hợp nó cứ None mãi)
                    elif sym_u not in personal_state or (last_alert_at is None and state_entry.get("last_pct") != 0.0):
                         personal_state[sym_u] = {
                            "last_pct": 0.0,
                            "last_alert_at": state_entry.get("last_alert_at") # Giữ nguyên time cũ hoặc None
                        }

                if messages:
                    header = (
                        "--------------------------------\n"
                        f"⏰ *Cảnh báo {now.strftime('%H:%M')}*"
                    )
                    messages_text = "\n".join(messages)
                    body = messages_text + "\n" + header

                    try:
                        _stock_broadcast_queue.put_nowait({"chat_id": chat_id, "body": body})
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
    - Đã thêm check BOT_ACTIVE để chặn tin tồn đọng khi tắt bot.
    """
    log.info("[BCASTER_STOCK] Bắt đầu. Chờ tin nhắn trong queue...")
    
    while True:
        try:
            # Chờ Ticker đẩy tin nhắn vào
            item = await _stock_broadcast_queue.get()
            
            # 🔥 CHẶN TẠI ĐÂY: Nếu bot tắt, hủy bỏ tin nhắn này luôn
            if not BOT_ACTIVE:
                _stock_broadcast_queue.task_done()
                continue

            chat_id = item.get("chat_id")
            body = item.get("body")
            
            if not chat_id or not body:
                _stock_broadcast_queue.task_done()
                continue
            
            # Gửi tin nhắn (đã gán msg_type='STOCK_ALERT')
            await asyncio.to_thread(send_msg_to, chat_id, body, "Markdown", False, "STOCK_ALERT")

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
                    web_app_url = f"{base_url}/report/view/{cache_key}"
                    
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
    (Đã sửa) Gửi báo cáo danh mục (CHỈ LÊN LỊCH)
    vào 09:00 sáng Chủ Nhật hằng tuần.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1
        instance_label = f"[{INSTANCE_ID}][WEEKLY {loop_id}]"

        if not BOT_ACTIVE:
            log.info(f"{instance_label} Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        if not GEMINI_API_KEY:
            log.warning(
                f"{instance_label} Chưa có GEMINI_API_KEY, bỏ qua báo cáo tuần."
            )
            await asyncio.sleep(3600)
            continue

        wait_sec = seconds_until_next_weekly_report()
        log.info(
            f"{instance_label} Ngủ tới 09:00 Chủ Nhật, còn {wait_sec:.0f}s"
        )
        await asyncio.sleep(wait_sec)

        if not BOT_ACTIVE:
            log.info(f"{instance_label} Thức dậy nhưng bot TẮT, bỏ qua.")
            continue

        now = datetime.datetime.now(vn_tz)
        if now.weekday() != 6:
            log.info(f"{instance_label} Không phải Chủ Nhật, bỏ qua.")
            continue

        try:
            # Chỉ cần gọi hàm lõi (không truyền admin_update)
            log.info(f"{instance_label} 09:00 Chủ Nhật, bắt đầu chạy execute_weekly_report() theo lịch.")
            await execute_weekly_report(admin_update=None)
            
        except Exception as e:
            log.error(f"{instance_label} Lỗi nghiêm trọng khi gọi execute_weekly_report: {e}")
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
    Loop dọn bảng news_seen trong Postgres:

    - Mỗi 24h xoá các bản ghi cũ hơn ~6 tháng (180 ngày).
    - Redis vẫn tự xoá theo TTL riêng, không cần dọn ở đây.
    """
    RETENTION_DAYS = 180
    INTERVAL_SECONDS = 24 * 60 * 60  # 24 giờ

    while True:
        try:
            # Chạy cleanup trong thread riêng để không block event loop
            deleted = await asyncio.to_thread(
                cleanup_old_news_seen,
                RETENTION_DAYS,
            )
            log.info(
                f"[NEWS_CLEANUP] Đã xoá {deleted} bản ghi news_seen cũ hơn {RETENTION_DAYS} ngày."
            )
        except Exception as e:
            log.warning(f"[NEWS_CLEANUP] Lỗi khi dọn news_seen: {e}")

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

def get_vn30f1m_enabled_map() -> dict[int, bool]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id,
                       COALESCE((settings ->> 'vn30f1m_enabled')::boolean, FALSE) AS enabled
                FROM bot_user_settings
            """)
            rows = cur.fetchall()
    return {int(r[0]): bool(r[1]) for r in rows}

def set_vn30f1m_enabled(chat_id: int, enabled: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_user_settings (chat_id, settings)
                VALUES (%s, jsonb_build_object('vn30f1m_enabled', %s))
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    settings = COALESCE(bot_user_settings.settings, '{}'::jsonb)
                               || jsonb_build_object('vn30f1m_enabled', EXCLUDED.settings->'vn30f1m_enabled'),
                    updated_at = NOW()
            """, (chat_id, enabled))
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

async def _vn30f1m_get_current_price() -> float | None:
    """
    (V8 - Fix) Lấy giá realtime dùng price_board (nhẹ & ổn định hơn history).
    Tránh lỗi ConnectionError/RetryError từ endpoint history.
    """
    # Sử dụng biến global stock_trading (đã được cơ chế Re-init quản lý bên fetcher loop)
    global stock_trading
    
    try:
        if stock_trading is None:
            return None

        # Gọi API lấy bảng giá (Snapshot realtime) - Nhanh và ít bị chặn
        df = await asyncio.to_thread(stock_trading.price_board, [VN30F1M_SYMBOL])
        
        if df is None or df.empty:
            return None

        # Lấy giá khớp lệnh (Match Price) từ DataFrame
        # Cấu trúc price_board trả về thường là MultiIndex: ('match', 'match_price')
        try:
            row = df.iloc[0]
            # Ưu tiên lấy giá khớp lệnh
            val = row.get(('match', 'match_price'))
            
            # Nếu không có giá khớp (ví dụ đầu phiên chưa khớp), thử lấy giá tham chiếu
            if val is None or val == 0:
                 val = row.get(('listing', 'ref_price'))

            if val is not None:
                return float(val)
                
        except Exception:
            pass
            
        return None

    except Exception as e:
        # Log warning nhẹ để debug nếu cần
        log.warning(f"[VN30F1M] Lỗi lấy giá Live (price_board): {e}")
        return None

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
                # Clear anchor, price cache... mỗi khi ra ngoài giờ
                _vn30f1m_clear_after_close()
                now = datetime.datetime.now(vn_tz)

                next_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
                if now >= next_open:
                    next_open += datetime.timedelta(days=1)

                while next_open.weekday() >= 5:
                    next_open += datetime.timedelta(days=1)

                sleep_seconds = max(5, (next_open - now).total_seconds())

                log.info(f"[VN30F1M][TICKER] Ngoài giờ. Ngủ tới {next_open.strftime('%Y-%m-%d %H:%M:%S')} ({int(sleep_seconds)}s)")
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
    (Tác vụ 1: Fetcher VN30F1M - Smart Async)
    """
    global _vn30f1m_current_price_cache, _vn30f1m_anchor, _vn30f1m_ref_price
    vn_tz = pytz.timezone(TIMEZONE)
    FETCH_INTERVAL = 15
    
    log.info(f"[{INSTANCE_ID}][VN30F1M] Bắt đầu (Smart Mode Async)...")

    while True:
        loop_start = datetime.datetime.now(vn_tz)
        try:
            if not BOT_ACTIVE:
                await asyncio.sleep(30)
                continue
                
            if not in_session_vietnam():
                now = datetime.datetime.now(vn_tz)
                next_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
                if now >= next_open: next_open += datetime.timedelta(days=1)
                while next_open.weekday() >= 5: next_open += datetime.timedelta(days=1)
                sleep_seconds = max(5, (next_open - now).total_seconds())
                log.info(f"[VN30F1M] Ngoài giờ. Ngủ tới {next_open.strftime('%H:%M')}")
                await asyncio.sleep(sleep_seconds)
                continue

            # 🔥 SỬA ĐỔI: Gọi trực tiếp await
            data = await fetch_data_smart([VN30F1M_SYMBOL])
            
            if data and VN30F1M_SYMBOL in data:
                info = data[VN30F1M_SYMBOL]
                current_price = info['price']
                ref_price = info['ref']
                
                _vn30f1m_current_price_cache = current_price
                
                if _vn30f1m_ref_price is None:
                    _vn30f1m_ref_price = ref_price
                    log.info(f"[VN30F1M] ⛳ Ref Price Set: {ref_price}")
                
                if _vn30f1m_anchor is None:
                    _vn30f1m_anchor = ref_price

            else:
                # Log nhẹ để biết đang retry
                pass

        except Exception as e:
            log.error(f"[VN30F1M] Lỗi Loop: {e}")
            await asyncio.sleep(10)
        
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
async def daily_user_digest_loop():
    """
    Gửi bản tin tổng hợp (Digest) 7:00 sáng.
    Đã tích hợp: Value Screener (Pro), BCTC (Pro), Báo cáo (Pro), Tin tức (All).
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

        if not BOT_ACTIVE:
            continue
        
        log.info(f"{loop_label} 07:00! Bắt đầu build digest...")

        # 2️⃣ Thu thập dữ liệu (Song song)
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            since_utc = now_utc - datetime.timedelta(hours=24)

            # Gọi song song tất cả các nguồn dữ liệu
            (
                bctc_rows,
                report_rows,
                macro_rows,
                spec_rows,
                all_watch,
                pro_chat_ids,
                screener_data,  # <-- Dữ liệu Value Screener mới
            ) = await asyncio.gather(
                asyncio.to_thread(get_recent_bctc_notified, since_utc),
                asyncio.to_thread(get_recent_analysis_reports, since_utc),
                asyncio.to_thread(get_recent_news_seen, "MACRO", since_utc),
                asyncio.to_thread(get_recent_news_seen, "SPECIALIZED", since_utc),
                asyncio.to_thread(get_all_watch),
                asyncio.to_thread(get_all_pro_chat_ids),
                # Lấy Top Value (All) cho ngày hôm nay
                asyncio.to_thread(run_value_screener_from_api, 'all'), 
            )
            
            all_keywords = COMPANY_KEYWORDS

        except Exception as e:
            log.error(f"{loop_label} Lỗi nghiêm trọng khi gather data: {e}")
            await asyncio.sleep(600) 
            continue

        # 3️⃣ Xử lý dữ liệu Value Screener (Lấy Top 5-7 mã điểm cao nhất)
        top_value_stocks = []
        if screener_data and "industries" in screener_data:
            # Flatten: Gom tất cả mã từ tất cả ngành vào 1 list
            all_candidates = []
            for ind in screener_data["industries"]:
                for row in ind.get("rows", []):
                    # Thêm tên ngành vào row để hiển thị
                    row_copy = row.copy()
                    row_copy["industry"] = ind["industry"]
                    all_candidates.append(row_copy)
            
            # Sắp xếp theo Value Score giảm dần
            all_candidates.sort(key=lambda x: x.get("value_score", 0), reverse=True)
            
            # Lấy Top 5
            for item in all_candidates[:5]:
                top_value_stocks.append({
                    "symbol": item["symbol"],
                    "industry": item["industry"],
                    "pe": f"{item.get('pe', 0):.1f}",
                    "roe": f"{item.get('roe', 0) * 100:.1f}", # roe decimal -> %
                    "score": f"{item.get('value_score', 0):.2f}"
                })

        # 4️⃣ Xử lý các dữ liệu khác (Giữ nguyên logic cũ)
        bctc_by_sym: dict[str, tuple] = {}
        for (symbol, year, quarter, notified_at) in bctc_rows:
            bctc_by_sym[str(symbol).upper()] = (year, quarter, notified_at)

        reports_by_sym: dict[str, list] = {}
        for (symbol, title, link, published_at, created_at) in report_rows:
            sym_u = str(symbol).upper()
            reports_by_sym.setdefault(sym_u, []).append((title, link, published_at))

        watch_to_chats: dict[str, list[int]] = {}
        users_map: dict[int, set[str]] = {}

        for chat_key, user_block in all_watch.items():
            try:
                chat_id = int(chat_key)
            except: continue
            watch_list = user_block.get("list", []) or []
            users_map[chat_id] = set()
            for sym in watch_list:
                s = str(sym).upper().strip()
                if s:
                    users_map[chat_id].add(s)
                    watch_to_chats.setdefault(s, []).append(chat_id)

        spec_patterns: dict[str, re.Pattern] = {}
        for sym in watch_to_chats.keys():
            keywords = all_keywords.get(sym, [sym])
            combined = "|".join(re.escape(k) for k in keywords if k)
            if combined:
                spec_patterns[sym] = re.compile(rf"\b({combined})\b", re.IGNORECASE)

        # 5️⃣ Tạo Payload riêng cho từng User
        digest_payloads: dict[int, dict] = {}

        def _get_payload(cid):
            if cid not in digest_payloads:
                is_pro = (cid in pro_chat_ids or cid == ADMIN_ID)
                digest_payloads[cid] = {
                    "is_pro": is_pro, # Flag quan trọng để hiển thị Badge Pro / Upsell Card
                    "value_stocks": [], 
                    "bctc": [],
                    "reports": [],
                    "specialized": [],
                    "macro": []
                }
            return digest_payloads[cid]

        # --- A. Value Screener (CHỈ CHO PRO & ADMIN) ---
        if top_value_stocks:
            for cid in users_map.keys():
                if cid in pro_chat_ids or cid == ADMIN_ID:
                    _get_payload(cid)["value_stocks"] = top_value_stocks

        # --- B. BCTC (Pro) ---
        if bctc_rows:
            for sym, (year, quarter, notified_at) in bctc_by_sym.items():
                chat_ids_impacted = watch_to_chats.get(sym, [])
                for cid in chat_ids_impacted:
                    payload = _get_payload(cid)
                    
                    # Lấy thời gian
                    t_str = notified_at.astimezone(vn_tz).strftime("%H:%M %d/%m")
                    
                    if payload["is_pro"]:
                        # PRO: Thấy full
                        payload["bctc"].append({
                            "symbol": sym, "year": year, "quarter": quarter, "time": t_str,
                            "is_locked": False
                        })
                    else:
                        # FREE: Thấy bị khóa (Upsell)
                        # Chỉ thêm 1 lần cho mỗi mã để tránh spam nếu có nhiều quý
                        if not any(x['symbol'] == sym for x in payload["bctc"]):
                            payload["bctc"].append({
                                "symbol": sym, 
                                "year": year, "quarter": quarter, 
                                "time": t_str,
                                "is_locked": True # <--- CỜ KHÓA
                            })

        # --- C. Reports (Pro) ---
        if report_rows:
            for sym, reports_list in reports_by_sym.items():
                chat_ids_impacted = watch_to_chats.get(sym, [])
                for cid in chat_ids_impacted:
                    payload = _get_payload(cid)
                    
                    # Lấy báo cáo mới nhất làm đại diện
                    last_rep = reports_list[0]
                    title = last_rep[0]
                    link = last_rep[1]
                    pub_at = last_rep[2]
                    
                    time_str = ""
                    if pub_at:
                         if getattr(pub_at, 'tzinfo', None) is None:
                             pub_at = pub_at.replace(tzinfo=datetime.timezone.utc)
                         time_str = pub_at.astimezone(vn_tz).strftime("%H:%M %d/%m")

                    if payload["is_pro"]:
                        # PRO: Thêm hết
                        for (t, l, p) in reports_list:
                             # ... (xử lý time cho từng item như cũ)
                             # Ở đây mình viết gọn lại logic loop:
                             t_str_item = "" 
                             if p: 
                                 if getattr(p, 'tzinfo', None) is None: p = p.replace(tzinfo=datetime.timezone.utc)
                                 t_str_item = p.astimezone(vn_tz).strftime("%H:%M %d/%m")
                                 
                             payload["reports"].append({
                                "symbol": sym, "title": t, "link": l, "time": t_str_item,
                                "is_locked": False
                            })
                    else:
                        # FREE: Chỉ hiện 1 dòng teaser bị khóa
                        if not any(x['symbol'] == sym and x.get('is_locked') for x in payload["reports"]):
                            payload["reports"].append({
                                "symbol": sym, 
                                "title": "Báo cáo phân tích chuyên sâu", # Ẩn tiêu đề thật
                                "link": "#",
                                "time": time_str,
                                "is_locked": True # <--- CỜ KHÓA
                            })

        # --- D. Tin Chuyên Ngành (All) ---
        if spec_rows and spec_patterns:
            for (title, link, pub, created_at) in spec_rows:
                text_for_match = title or ""
                matched_symbols = set()
                for sym, pat in spec_patterns.items():
                    if pat.search(text_for_match): matched_symbols.add(sym)
                
                if matched_symbols:
                    item = {"title": title, "link": link} # Đã bỏ thời gian
                    chat_ids_impacted = set()
                    for sym in matched_symbols:
                        chat_ids_impacted.update(watch_to_chats.get(sym, []))
                    for cid in chat_ids_impacted:
                        _get_payload(cid)["specialized"].append(item)

        # --- E. Tin Vĩ Mô (All) ---
        if macro_rows:
            for (title, link, pub, created_at) in macro_rows:
                item = {"title": title, "link": link}
                for cid in users_map.keys():
                    _get_payload(cid)["macro"].append(item)

        # 6️⃣ Gửi tin
        if not digest_payloads:
            log.info(f"{loop_label} Không có dữ liệu.")
            continue

        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        tasks = []
        sent_count = 0

        for chat_id, data in digest_payloads.items():
            # Dù rỗng cũng gửi nếu là Pro, hoặc ít nhất có Macro
            # Nhưng để an toàn, chỉ gửi nếu có ít nhất 1 mục
            has_content = (
                data["value_stocks"] or data["bctc"] or data["reports"] or 
                data["specialized"] or data["macro"]
            )
            
            if not has_content: continue

            digest_id = uuid.uuid4().hex
            await asyncio.to_thread(save_digest_to_redis, digest_id, data)
            web_app_url = f"{base_url}/digest/{digest_id}"
            
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(text="📰 Xem Bản Tin Sáng 🌅", web_app=WebAppInfo(url=web_app_url))]])
            text = f"🌅 *Bản tin sáng {now_local.strftime('%d/%m')}*\nTổng hợp thị trường và danh mục của bạn.\n👉 Nhấn nút bên dưới để xem chi tiết."
            
            tasks.append(send_md(tg_app.bot, chat_id, text, reply_markup=kb))
            sent_count += 1
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            
        log.info(f"{loop_label} Hoàn tất gửi cho {sent_count} user.")

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


def send_msg_to(chat_id: int, text: str, parse_mode: str | None = "Markdown", silent: bool = False, msg_type: str = 'GENERAL'):
    """
    Gửi tin nhắn Telegram, có hỗ trợ msg_type để phân loại rác.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    def _do_send(t: str, mode: str | None, silent_flag: bool):
        params = {
            "chat_id": chat_id,
            "text": t,
        }
        if mode:
            params["parse_mode"] = mode
        if silent_flag:
            params["disable_notification"] = True

        res = requests.get(url, params=params, timeout=10)
        return res.json()

    try:
        # Lần 1: gửi nguyên văn
        data = _do_send(text, parse_mode, silent)

        # Nếu lỗi do Markdown -> escape và gửi lại
        if (
            not data.get("ok")
            and parse_mode == "Markdown"
            and "description" in data
            and "can't parse entities" in data["description"].lower()
        ):
            safe_text = escape_markdown_v2(text)
            data = _do_send(safe_text, parse_mode, silent)

        if data.get("ok") and "result" in data:
            msg_id = data["result"]["message_id"]
            # 🔥 LƯU LOẠI TIN NHẮN VÀO DB 🔥
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

def _fetch_price_board_with_fallback(trading, batch_syms, log, INSTANCE_ID):
    """
    Cố gắng gọi price_board theo batch; nếu lỗi (RetryError/TypeError/...), fallback gọi từng mã.
    Trả về DataFrame đã concat hoặc None nếu rỗng.
    """
    import time
    import pandas as pd

    pb_df = None
    try:
        pb_df = trading.price_board(batch_syms)
        if pb_df is not None and not pb_df.empty:
            return pb_df
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi price_board batch (sẽ fallback từng mã): {type(e).__name__}: {e}")

    # Fallback từng mã để không mất dữ liệu
    rows = []
    for s in batch_syms:
        try:
            _df = trading.price_board([s])
            if _df is not None and not _df.empty:
                rows.append(_df)
            else:
                log.debug(f"[{INSTANCE_ID}][VALUE] price_board rỗng cho {s}")
        except Exception as e1:
            log.debug(f"[{INSTANCE_ID}][VALUE] Lỗi price_board({s}): {type(e1).__name__}: {e1}")
        time.sleep(0.15)  # throttle nhẹ để đỡ bị chặn

    if rows:
        try:
            return pd.concat(rows, axis=0, ignore_index=True)
        except Exception as e2:
            log.warning(f"[{INSTANCE_ID}][VALUE] Không ghép được pb_df fallback: {type(e2).__name__}: {e2}")

    return None


# ==============================================
# COMMAND HANDLERS
# ==============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Hiển thị Dashboard chính với các nút bấm tiện lợi.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return
    
    chat_id = update.effective_chat.id
    
    # Log và khởi tạo DB nếu user mới (giữ nguyên logic cũ)
    try:
        await asyncio.to_thread(log_command_usage, chat_id, "/start", ADMIN_ID)
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        if lst is None:
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])
    except Exception:
        pass

    # Lấy Base URL
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    
    # Tạo Link WebApp kèm chat_id
    screener_url = f"{base_url}/screener?chat_id={chat_id}"

    # --- TẠO DASHBOARD MENU ---
    # Layout:
    # [ 📋 Danh mục ] [ ➕ Thêm mã ]
    # [ 💎 Lọc Cổ Phiếu ] [ 📊 AI Report ]
    # [ ⚙️ Cài đặt / Pro ] [ ❓ Hướng dẫn ]
    
    kb = [
        [
            InlineKeyboardButton("📋 Danh mục", callback_data="menu_list"),
            InlineKeyboardButton("➕ Thêm mã", callback_data="menu_add")
        ],
        [
            InlineKeyboardButton("📄 Soi hồ sơ", callback_data="menu_info"),
            InlineKeyboardButton("💎 Lọc Cổ Phiếu", web_app=WebAppInfo(url=screener_url))
        ],
        [
            InlineKeyboardButton("📊 AI Report", callback_data="menu_report"),
            InlineKeyboardButton("⚙️ Tài khoản", callback_data="menu_setting")
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help")
        ]
    ]

    welcome_msg = (
        "👋 *Chào mừng bạn đến với Người Canh Bảng 🧑‍💻*\n"
        "Trợ lý đầu tư chứng khoán thông minh tích hợp AI.\n\n"
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
        "📘 **HƯỚNG DẪN SỬ DỤNG NHANH**\n\n"
        
        "1️⃣ **Quản lý Danh mục (Cực nhanh)**\n"
        "• **Thêm mã:** Chỉ cần gõ mã 3 chữ cái (VD: `HPG`, `FPT`) vào chat -> Bot sẽ hiện nút thêm.\n"
        "• **Xóa/Xem:** Bấm nút **[📋 Danh mục]** trên Dashboard (/start).\n\n"

        "2️⃣ **Phân tích & Dữ liệu AI**\n"
        "• 📊 `/report`: AI phân tích sức khỏe toàn bộ danh mục.\n"
        "• 📄 `/info <MÃ>`: Soi hồ sơ doanh nghiệp (Lợi thế, Rủi ro).\n\n"

        "3️⃣ **Công cụ Pro**\n"
        "• 💎 `/screener_value`: Lọc cổ phiếu giá trị (Realtime).\n"
        "• 📉 `/vn30f1m_on`: Bật cảnh báo phái sinh (biến động ±5 điểm).\n\n"

        "4️⃣ **Hệ thống**\n"
        "• `/setting`: Kiểm tra hạn sử dụng Pro & Cài đặt thông báo.\n"
        "• `/start`: Mở lại Bảng điều khiển chính (Dashboard)."
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

# (Hãy chắc chắn rằng bạn đã import 'is_user_pro' từ db_utils.py ở đầu file)
# from db_utils import (
#     ...
#     is_user_pro,
#     ...
# )

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ 
    (ĐÃ SỬA - SMART MODE) Thêm mã vào watchlist.
    Sử dụng fetch_data_smart để tránh lỗi VCI.
    """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

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

    # --- GỌI SMART FETCHER (Thay vì gọi trực tiếp Trading VCI) ---
    try:
        # Hàm này đã bao gồm logic: Thử VCI -> Lỗi -> Qua TCBS -> Lỗi -> None
        data = await fetch_data_smart([symbol])
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [ADD] Lỗi fetch_data_smart {symbol}: {e}")
        await reply_md(update, f"⚠️ Lỗi hệ thống khi lấy dữ liệu *{symbol}*. Vui lòng thử lại sau.")
        return

    # Kiểm tra dữ liệu trả về
    if not data or symbol not in data:
        await reply_md(update,
            f"⚠️ Không tìm thấy dữ liệu cho mã *{symbol}*.\n"
            "Vui lòng kiểm tra lại mã hoặc thử lại sau (có thể do lỗi nguồn dữ liệu)."
        )
        return

    # Parse dữ liệu từ Smart Fetcher
    info = data[symbol]
    price = info.get('price')
    pct = info.get('pct')
    
    # Kiểm tra giá
    if price is None or price == 0:
         await reply_md(update, f"⚠️ Dữ liệu giá của *{symbol}* đang bị lỗi (0 hoặc None).")
         return

    # Lấy watchlist cũ
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
    
    # Kiểm tra đã tồn tại chưa
    if symbol in lst:
        symbols_text = ", ".join(lst) if lst else "—"
        await reply_md(update, f"ℹ️ *{symbol}* đã có trong danh sách theo dõi rồi.\n\n📋 Danh mục hiện tại: {symbols_text}")
        return

    # Kiểm tra Paywall (User Free chỉ 1 mã)
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
    
    # Format số liệu
    price_str = f"{price:,.0f}".replace(",", ".")
    
    pct_sign = "+" if pct > 0 else ""
    pct_str = f"{pct_sign}{pct:.2f}%" if pct is not None else "—"
    
    # Tính thay đổi tuyệt đối (ước lượng từ % và giá hiện tại vì Smart Fetcher không trả về change_abs)
    # Công thức: ref = price / (1 + pct/100) -> change = price - ref
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
    """ (ĐÃ CẬP NHẬT UX) Xem trạng thái & Cài đặt bằng nút bấm """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # Log command (dùng try-except cho an toàn)
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
            return_exceptions=True
        )
        
        expiry_date = results[0] if not isinstance(results[0], Exception) else None
        vn30f1m_map = results[1] if not isinstance(results[1], Exception) else {}
        
        vn30f1m_enabled = bool(vn30f1m_map.get(chat_id, False))

    except Exception as e:
        log.error(f"Setting error: {e}")
        await reply_md(update, "⚠️ Lỗi lấy dữ liệu cài đặt.")
        return

    # --- 2. BUILD NỘI DUNG TEXT ---
    lines = ["⚙️ *CÀI ĐẶT & TRẠNG THÁI TÀI KHOẢN* ⚙️\n"]

    # Trạng thái Pro
    is_pro = False
    if chat_id == ADMIN_ID:
        lines.append("👤 *Gói cước:* 😎 *ADMIN* (Full quyền)")
        is_pro = True
    elif expiry_date and expiry_date.astimezone(vn_tz) > now:
        is_pro = True
        exp_str = expiry_date.astimezone(vn_tz).strftime("%H:%M %d/%m/%Y")
        lines.append(f"👤 *Gói cước:* 👑 *PRO*")
        lines.append(f"⏳ *Hết hạn:* {exp_str}")
    else:
        lines.append("👤 *Gói cước:* 🆓 *FREE*")
        lines.append("_Giới hạn: 1 mã theo dõi, không có AI Report & Screener._")

    # Morning Digest
    lines.append("\n📰 *Bản tin sáng (Digest)*")
    lines.append("✅ Trạng thái: *TỰ ĐỘNG (07:00)*")

    # Phái sinh
    lines.append("\n📈 *Cảnh báo VN30F1M*")
    if vn30f1m_enabled:
        lines.append("✅ Trạng thái: *ĐANG BẬT*")
    else:
        lines.append("❌ Trạng thái: *ĐANG TẮT*")

    # --- 3. TẠO BÀN PHÍM ĐIỀU KHIỂN ---
    
    # Logic nút VN30: Nếu đang Bật thì hiện nút Tắt, và ngược lại
    if vn30f1m_enabled:
        vn30_btn_text = "🔴 Tắt VN30F1M"
        vn30_callback = "set_vn30_off"
    else:
        vn30_btn_text = "🟢 Bật VN30F1M"
        vn30_callback = "set_vn30_on"

    kb = [
        # Hàng 1: Nâng cấp (Luôn hiện, vì Pro cũng cần gia hạn)
        [InlineKeyboardButton("💎 Nâng cấp / Gia hạn Pro", callback_data="btn_upgrade")],
        
        # Hàng 2: Bật/Tắt VN30
        [InlineKeyboardButton(vn30_btn_text, callback_data=vn30_callback)],
        
        # Hàng 3: Đóng
        [InlineKeyboardButton("❌ Đóng", callback_data="close_setting")]
    ]

    await reply_md(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

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

# (Đảm bảo bạn đã import pytz và datetime ở đầu file)
import pytz
import datetime
# ...
from db_utils import (
    # ...
    get_all_paid_users_expiry
)

# ...

# (Dán vào file alert_bot.py, thay thế hàm cmd_allwatch cũ)

async def cmd_allwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O + THÊM STATUS PRO + SORT 5 NHÓM + RELATIVE TIME) """
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    try:
        await context.bot.send_chat_action(
            chat_id=ADMIN_ID, action=ChatAction.TYPING
        )
    except Exception:
        pass

    # 0. Lấy ADMIN_ID dưới dạng string để so sánh
    admin_id_str = str(ADMIN_ID) if ADMIN_ID else None

    # 1. Lấy dữ liệu (Watchlist & Pro Expiry)
    all_watch = await asyncio.to_thread(get_all_watch)
    expiry_map_int_key = await asyncio.to_thread(get_all_paid_users_expiry)
    
    # Chuyển key sang string để tra cứu nhanh
    expiry_map_str_key = {str(k): v for k, v in expiry_map_int_key.items()}

    if not all_watch:
        await reply_md(update,"📭 Chưa có user nào lưu danh sách theo dõi.")
        return

    await reply_md(update, f"🔎 Đang tổng hợp, vui lòng đợi...")

    # 2. Chuẩn bị (Biến đếm, Thời gian, và 5 "Xô" phân loại)
    symbol_counts = {}
    
    # Năm "xô" để chứa các dòng text đã định dạng (theo thứ tự ưu tiên MỚI)
    admin_lines = []
    active_pro_safe_lines = []      # (còn > 24h)
    active_pro_expiring_lines = []  # (còn <= 24h)
    expired_pro_lines = []          # (đã hết hạn)
    free_user_lines = []            # (free)
    
    # Biến đếm (5 nhóm)
    total_admin_count = 0
    total_active_safe_count = 0
    total_active_expiring_count = 0
    total_expired_pro_count = 0
    total_free_count = 0
    
    vn_tz = pytz.timezone(TIMEZONE)
    now_aware = datetime.datetime.now(pytz.utc) # Giả định DB lưu UTC
    SECONDS_IN_A_DAY = 86400

    # 3. Vòng lặp Phân loại & Xử lý
    for chat_key, block in all_watch.items():
        lst = block.get("list", []) or []
        
        # Đếm symbol (luôn chạy)
        for sym in lst:
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
            
        list_str = ", ".join(lst) if lst else "(trống)"

        # === Logic Phân loại (5 nhóm) ===

        # NHÓM 1: ADMIN
        if chat_key == admin_id_str:
            total_admin_count += 1
            line_text = f"😎 {chat_key} Admin : {list_str}"
            admin_lines.append(line_text)
            continue # Xong, đi user tiếp theo

        # NHÓM 2, 3, 4: Pro / Free
        if chat_key in expiry_map_str_key:
            # Đây là user Pro
            expiry_date_utc = expiry_map_str_key[chat_key]
            delta_seconds = (expiry_date_utc - now_aware).total_seconds()

            if delta_seconds > SECONDS_IN_A_DAY:
                # 👑 NHÓM 2: Pro (An toàn, còn > 24h)
                total_active_safe_count += 1
                days_remaining = int(delta_seconds // SECONDS_IN_A_DAY)
                date_str = f"còn {days_remaining} ngày"
                
                line_text = f"👑 {chat_key} ({date_str}): {list_str}"
                active_pro_safe_lines.append(line_text) # Bỏ vào xô 2

            elif delta_seconds > 0:
                # ⚠️ NHÓM 3: Pro (Sắp hết hạn, còn <= 24h)
                total_active_expiring_count += 1
                hours_remaining = int(delta_seconds // 3600)
                if hours_remaining <= 0:
                    date_str = "còn <1 giờ"
                else:
                    date_str = f"còn {hours_remaining} giờ"
                
                line_text = f"⚠️ {chat_key} ({date_str}): {list_str}" # Icon ⚠️ như bạn yêu cầu
                active_pro_expiring_lines.append(line_text) # Bỏ vào xô 3
                
            else:
                # 🆓 NHÓM 4: Pro (Đã hết hạn)
                total_expired_pro_count += 1
                days_past = int(abs(delta_seconds) // SECONDS_IN_A_DAY)
                
                if days_past == 0:
                    date_str = "hết <1 ngày"
                else:
                    date_str = f"hết {days_past} ngày"
                line_text = f"🆓 {chat_key} ({date_str}): {list_str}" 
                expired_pro_lines.append(line_text) # Bỏ vào xô 4
        else:
            # 🆓 NHÓM 5: User Free
            total_free_count += 1
            line_text = f"🆓 {chat_key}: {list_str}"
            free_user_lines.append(line_text) # Bỏ vào xô 5

    # 4. Thống kê mã (giữ nguyên)
    stats_lines = []
    for sym, cnt in sorted(symbol_counts.items()):
        stats_lines.append(f"{sym}: {cnt} user")

    # 5. Thống kê lệnh (giữ nguyên)
    cmd_stats = await asyncio.to_thread(get_command_stats)
    cmd_stats = [s for s in cmd_stats if not s["command"].startswith("unknown:")]
    
    if cmd_stats:
        cmd_summary = "📊 *Thống kê lệnh được sử dụng:*\n"
        for row in cmd_stats:
            cmd_name = escape_markdown_v2(row["command"])
            day = escape_markdown_v2(row["day"])
            month = escape_markdown_v2(row["month"])
            total = escape_markdown_v2(row["total"])
            cmd_summary += (
                f"{cmd_name}: {day} hôm nay | {month} tháng này | {total} tổng cộng\n"
            )
        cmd_summary += "\n"
    else:
        cmd_summary = "📊 *Chưa có dữ liệu lệnh được sử dụng.*\n\n"


    # 6. Cập nhật Header (thêm 2 nhóm Pro)
    header = (
        cmd_summary +
        "📌 *Thống kê theo mã:*\n"
        + "\n".join(stats_lines) +
        f"\n🏷️ Tổng số mã khác nhau: {len(symbol_counts)}\n\n"
        +"📋 *Tổng hợp danh sách users*\n"
        f"👥 Tổng số user: {len(all_watch)}\n"
        f"😎 Admin: {total_admin_count}\n"
        f"👑 Pro (còn hạn): {total_active_safe_count}\n"
        f"⚠️ Pro (sắp hết hạn): {total_active_expiring_count}\n" # <-- MỚI
        f"🆓 Pro (đã hết hạn): {total_expired_pro_count}\n"
        f"🆓 Free Users: {total_free_count}\n"
        + "\n📌 *Chi tiết theo từng user (chatId):*"
    )

    # 7. Gửi tin nhắn (Nối 5 "xô" theo thứ tự)
    
    # Nối 5 danh sách theo thứ tự ưu tiên MỚI của bạn
    all_detail_lines = (
        admin_lines + 
        active_pro_safe_lines + 
        active_pro_expiring_lines + 
        expired_pro_lines + 
        free_user_lines
    )

    max_len = 3500
    parts = []
    current = header
    
    for line in all_detail_lines: # Dùng danh sách đã được sắp xếp
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += "\n" + line
    parts.append(current)

    for part in parts:
        await reply_md(update, part)

async def cmd_screener_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (UX PRO) Bộ lọc Value với Progress Bar.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # 1. Kiểm tra Paywall (Dùng hàm check có sẵn)
    if not await check_pro_access(update, context): return

    # Log
    try: await asyncio.to_thread(log_command_usage, chat_id, "/screener_value", ADMIN_ID)
    except: pass

    # Mặc định dùng 'all' vì WebApp sẽ lo phần chuyển tab
    screener_type = 'all' 
    base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
    screener_url = f"{base_url}/screener?type=all&chat_id={chat_id}"
    
    # A. Check Cache (Nhanh)
    cached = await asyncio.to_thread(load_value_screener_from_redis, screener_type)
    if cached is not None:
        # Cache Hit -> Gửi nút ngay (không cần progress bar vì quá nhanh)
        kb = [[InlineKeyboardButton("🚀 Mở Bộ Lọc (WebApp)", web_app=WebAppInfo(url=screener_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
        await reply_md(
            update,
            "💎 **Bộ Lọc Cổ Phiếu Giá Trị**\n\n"
            "Nhấn nút bên dưới để mở giao diện lọc cổ phiếu realtime (P/E, P/B, ROE...)",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return
    
    # B. Cache Miss -> CHẠY TIẾN TRÌNH (Vì gọi API mất ~10s)
    
    # B1. Khởi tạo
    progress_msg = await reply_md(
        update, 
        f"⏳ **Khởi động Market Scanner...**\n"
        f"`[{make_progress_bar(10)}] 10%`"
    )

    try:
        # B2. Đang quét (50%)
        await asyncio.sleep(0.5)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"📡 **Đang quét dữ liệu toàn thị trường...**\n`[{make_progress_bar(50)}] 50%`",
            parse_mode="Markdown"
        )
        
        # --- GỌI HÀM NẶNG ---
        result = await asyncio.to_thread(run_value_screener_from_api, screener_type)
        if result:
            await asyncio.to_thread(save_value_screener_to_redis, result, screener_type)
        # --------------------

        if not result or not result.get("industries"):
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text="⚠️ Không lấy được dữ liệu thị trường. Vui lòng thử lại sau.",
                parse_mode="Markdown"
            )
            return

        # B3. Hoàn tất
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            text=f"✅ **Xử lý hoàn tất!**\n`[{make_progress_bar(100)}] 100%`",
            parse_mode="Markdown"
        )
        await asyncio.sleep(0.5)

        # Xóa loading & Gửi kết quả
        await context.bot.delete_message(chat_id, progress_msg.message_id)
        
        kb = [[InlineKeyboardButton("🚀 Mở Bộ Lọc (WebApp)", web_app=WebAppInfo(url=screener_url))],
              [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]

        await reply_md(
            update,
            "💎 **Bộ Lọc Cổ Phiếu Giá Trị**\n\n"
            "Dữ liệu đã được cập nhật mới nhất từ thị trường.\n"
            "Nhấn nút bên dưới để mở giao diện lọc.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    except Exception as e:
        log.error(f"Lỗi /screener: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"⚠️ Lỗi kỹ thuật. Vui lòng thử lại.",
                parse_mode="Markdown"
            )
        except: pass

async def cmd_screener_value_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    [NEW] Force refresh snapshot screener value từ vnstock và ghi đè cache Redis hôm nay.
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    log_id = uuid.uuid4().hex[:8]
    log_prefix = f"[{log_id}][/screener_value_clear]"

    if chat_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    log.info("%s Admin %s gọi /screener_value_clear (refetch vnstock Screener)", log_prefix, chat_id)

    # Thông báo cho admin
    await reply_md(
        update,
        "⏳ *Đang làm mới dữ liệu Value Screener từ vnstock...*\n"
        "_Lệnh này sẽ gọi lại API Screener, tính toán lại và ghi đè cache Redis trong ngày._"
    )

    # Gọi API + tính toán lại (Mặc định làm mới loại 'all')
    try:
        result = await asyncio.to_thread(run_value_screener_from_api, 'all')
    except Exception as e:
        log.exception("%s Lỗi khi gọi run_value_screener_from_api: %s", log_prefix, e)
        await reply_md(update, f"⚠️ Lỗi khi gọi API Screener: `{e}`")
        return

    if result is None:
        await reply_md(
            update,
            "⚠️ Không thể làm mới dữ liệu Value Screener.\n"
            "_Có thể do API vnstock trả về rỗng hoặc lỗi._"
        )
        return

    # [ĐÃ SỬA] Ghi đè cache Redis (Thêm tham số 'all')
    await asyncio.to_thread(save_value_screener_to_redis, result, 'all')
    
    stats = result.get("stats", {})
    total_all = stats.get("total_all", "N/A")
    after_base = stats.get("after_base_filter", "N/A")

    await reply_md(
        update,
        "✅ *Đã làm mới dữ liệu Value Screener (ALL) từ vnstock và ghi đè cache Redis.*\n\n"
        f"📔 Tổng mã ban đầu: *{total_all}*\n"
        f"📘 Sau lọc cơ sở: *{after_base}*\n\n"
        f"Bạn có thể dùng `/screener_value` để xem báo cáo mới nhất."
    )

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
            "bot_watch", "news_pref", "bot_config", "bctc_notified", # Core cũ
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

async def cmd_admin_test_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Test nhanh tính năng Web App Digest.
    Cách dùng: 
    - /test_digest (mặc định xem bản Pro)
    - /test_digest free (xem bản Free)
    """
    
    # 1. Check Admin
    if update.effective_user.id != ADMIN_ID:
        return

    # 2. Xác định chế độ test (Pro hay Free)
    mode = "pro"
    if context.args and context.args[0].lower() == "free":
        mode = "free"

    is_pro_flag = (mode == "pro")

    # 3. Tạo dữ liệu giả (Mock Data)
    mock_data = {
        "is_pro": is_pro_flag,  # <--- QUAN TRỌNG: Cờ này quyết định giao diện
        
        # Dữ liệu Value Stocks (Chỉ hiện nếu là Pro)
        "value_stocks": [
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
            {"symbol": "TCB", "industry": "Ngân hàng", "pe": "6.5", "roe": "22.0", "score": "9.8"},
     
        ] if is_pro_flag else [],

        # Dữ liệu BCTC (Chỉ hiện nếu là Pro)
        "bctc": [
            {"symbol": "HPG", "year": 2025, "quarter": 1, "time": "Vừa xong"},
            {"symbol": "HPG", "year": 2025, "quarter": 1, "time": "Vừa xong"},
            {"symbol": "HPG", "year": 2025, "quarter": 1, "time": "Vừa xong"},
            {"symbol": "HPG", "year": 2025, "quarter": 1, "time": "Vừa xong"},
            {"symbol": "HPG", "year": 2025, "quarter": 1, "time": "Vừa xong"},
    
        ] if is_pro_flag else [],

        # Dữ liệu Reports (Chỉ hiện nếu là Pro)
        "reports": [
            {"symbol": "SSI", "title": "Báo cáo test: Triển vọng ngành 2025", "link": "https://google.com", "time": "08:30 19/11"},
            {"symbol": "SSI", "title": "Báo cáo test: Triển vọng ngành 2025", "link": "https://google.com", "time": "08:30 19/11"},
            {"symbol": "SSI", "title": "Báo cáo test: Triển vọng ngành 2025", "link": "https://google.com", "time": "08:30 19/11"},
            {"symbol": "SSI", "title": "Báo cáo test: Triển vọng ngành 2025", "link": "https://google.com", "time": "08:30 19/11"},
            {"symbol": "SSI", "title": "Báo cáo test: Triển vọng ngành 2025", "link": "https://google.com", "time": "08:30 19/11"},
        ] if is_pro_flag else [],

        # Tin tức (Hiện cho cả 2)
        "specialized": [
            {"title": "Tin test: Doanh nghiệp X đạt lợi nhuận kỷ lục", "link": "https://google.com", "time": "09:00"},
             {"title": "Tin test: Cổ phiếu Y tăng trần", "link": "https://google.com", "time": "10:00"},
             {"title": "Tin test: Cổ phiếu Y tăng trần", "link": "https://google.com", "time": "10:00"},
             {"title": "Tin test: Cổ phiếu Y tăng trần", "link": "https://google.com", "time": "10:00"},
             {"title": "Tin test: Cổ phiếu Y tăng trần", "link": "https://google.com", "time": "10:00"},
             {"title": "Tin test: Cổ phiếu Y tăng trần", "link": "https://google.com", "time": "10:00"},

        ],
        "macro": [
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
            {"title": "Tin test: GDP tăng trưởng vượt bậc", "link": "https://google.com"},
        ]
    }

    # 4. Lưu vào Redis & Tạo Link
    try:
        digest_id = uuid.uuid4().hex
        
        # Lưu Redis (chạy trong thread để không block)
        await asyncio.to_thread(save_digest_to_redis, digest_id, mock_data)
        
        # Lấy URL (Render hoặc fallback google)
        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://google.com")
        web_app_url = f"{base_url}/digest/{digest_id}"

        # 5. Gửi nút Web App
        btn_text = "👑 Xem Bản Tin Pro (Test)" if is_pro_flag else "🆓 Xem Bản Tin Free (Test)"
        
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=btn_text, 
                web_app=WebAppInfo(url=web_app_url)
            )]
        ])
        
        msg_text = (
            f"👇 Đây là bản tin test chế độ: *{mode.upper()}*\n"
            f"_Gõ `/test_digest free` để xem giao diện người dùng miễn phí._"
        )
        
        await reply_md(update, msg_text, reply_markup=kb)
        
    except Exception as e:
        await reply_md(update, f"❌ Lỗi test digest: {e}")

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
    (ĐÃ SỬA: Đọc Token từ Header 'Authorization: Apikey ...')
    (ĐÃ SỬA: Dùng Regex để tìm Order ID)
    """
    
    # === 1. LẤY DỮ LIỆU VÀ XÁC THỰC TOKEN (Giữ nguyên) ===
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
            log.warning(f"[SEPAPAY] Header 'Authorization' nhận được: {auth_header}")
            return jsonify({"message": "Invalid Token"}), 403
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi khi parse JSON hoặc xác thực Header: {e}")
        return jsonify({"message": "Invalid Request Body"}), 400

    # === 2. PHÂN TÍCH PAYLOAD (SỬA LỖI REGEX) ===
    try:
        # 1. Lấy nội dung gốc
        raw_content = data.get("content")
        received_amount_str = data.get("transferAmount")
        transfer_type = data.get("transferType")

        if transfer_type != "in":
            log.info(f"[SEPAPAY] Bỏ qua giao dịch (type: {transfer_type}).")
            return jsonify({"message": "Not an 'in' transaction"}), 200

        if not raw_content or received_amount_str is None:
            log.warning("[SEPAPAY] Webhook thiếu 'content' hoặc 'transferAmount'.")
            return jsonify({"message": "Missing fields"}), 400
        
        # 2. Dùng REGEX để tìm mã PAY... bên trong nội dung
        # (Regex: Tìm chữ "PAY", theo sau là 9-15 chữ số, và 5 chữ/số)
        # (Bạn đã import 're' ở dòng 98 rồi)
        match = re.search(r'(PAY\d{9,15}\w{5})', raw_content.upper())
        
        order_id = None
        if match:
            order_id = match.group(1) # Lấy mã PAY... đã tìm được
        
        # 3. Nếu không tìm thấy mã PAY... -> Bỏ qua
        if not order_id:
            log.info(f"[SEPAPAY] Không tìm thấy Order ID (PAY...) trong nội dung: '{raw_content}'. Bỏ qua.")
            return jsonify({"message": "Order ID pattern not found"}), 200
        
        received_amount = int(float(received_amount_str))
            
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi khi đọc các trường hoặc Regex: {e}")
        return jsonify({"message": "Invalid fields"}), 400
    
    # === 3. XỬ LÝ LOGIC THANH TOÁN (Giữ nguyên) ===
    # (Từ đây trở đi, 'order_id' đã là mã PAY... chuẩn, code sẽ chạy đúng)
    try:
        order = get_order_by_id(order_id)
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi DB khi gọi get_order_by_id({order_id}): {e}")
        return jsonify({"message": "Database error"}), 500

    # 3.1. Không tìm thấy đơn hàng
    if not order:
        log.warning(f"[SEPAPAY] Không tìm thấy đơn hàng cho order_id: {order_id}")
        return jsonify({"message": "Order not found"}), 200

    # 3.2. Đơn hàng đã được xử lý
    if order['status'] == 'PAID':
        log.info(f"[SEPAPAY] Đơn hàng {order_id} đã được xử lý trước đó. Bỏ qua.")
        return jsonify({"message": "Already processed"}), 200

    # 3.3. Đơn hàng PENDING -> Kiểm tra tiền
    chat_id = order['chat_id']
    expected_amount = int(order['amount']) 
    days_to_add = order['days_to_add']
    
    # 3.4. XỬ LÝ SAI TIỀN
    if received_amount != expected_amount:
        log.warning(f"[SEPAPAY] THANH TOÁN SAI SỐ TIỀN: User {chat_id} | Order {order_id}. "
                    f"Yêu cầu {expected_amount}, nhận {received_amount}.")
        
        msg_fail = (
            f"⚠️ **Thanh toán thất bại!**\n\n"
            f"Đơn hàng `{order_id}` của bạn yêu cầu *{expected_amount:,} VNĐ*, "
            f"*nhưng hệ thống ghi nhận bạn đã chuyển *"
            f"*{received_amount:,} VNĐ*.\n\n"
            "Giao dịch này **không** được ghi nhận. "
            "Vui lòng liên hệ Admin để xử lý hoặc tạo đơn hàng mới."
        )
        
        _send_telegram_message_safe(chat_id, msg_fail)
        return jsonify({"message": "Incorrect amount"}), 200

    # 3.5. XỬ LÝ THÀNH CÔNG
    try:
        log.info(f"[SEPAPAY] THÀNH CÔNG: User {chat_id} | Order {order_id}. "
                 f"Kích hoạt {days_to_add} ngày Pro.")
        
        add_paid_user(chat_id, days_to_add)
        mark_order_as_paid(order_id)
        
        msg_success = (
            f"🚀 **Kích hoạt Gói Pro thành công!**\n\n"
            f"Tài khoản của bạn đã được cộng thêm *{days_to_add} ngày* sử dụng Gói Pro.\n\n"
            "Cảm ơn bạn đã sử dụng dịch vụ!"
        )
        _send_telegram_message_safe(chat_id, msg_success)
        
    except Exception as e:
        log.error(f"[SEPAPAY] Lỗi nghiêm trọng khi kích hoạt Pro cho {chat_id}: {e}")
        return jsonify({"message": "Error activating Pro"}), 500

    # Trả 200 OK cuối cùng
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
def view_profile(symbol: str):
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
            is_pro = is_user_pro(cid) or (cid == ADMIN_ID)
        except: pass
    
    if not is_pro:
        # [NỘI DUNG RIÊNG CHO INFO]
        return render_template_string(
            LOCKED_FEATURE_TEMPLATE,
            icon="🏢",
            title=f"Hồ Sơ Doanh Nghiệp {symbol}",
            desc="Phân tích chuyên sâu về Mô hình kinh doanh, Lợi thế cạnh tranh (Moat) và Vị thế trong ngành."
        ), 403
    # -----------------------------

    sym = symbol.upper().strip()
    cache_key = make_profile_cache_key(sym)
    cached = get_profile_from_redis(cache_key)

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

    return render_template_string(
        PROFILE_HTML_TEMPLATE,
        symbol=sym,
        sections=sections_view,
        generated_at=generated_str,
        report_code=report_code,
        is_pro=is_pro, # Luôn là True nếu chạy tới đây
        is_error=False
    )

@flask_app.route("/report/view/<cache_key>")
def view_report(cache_key):
    """
    Route hiển thị Web App Report.
    ĐÃ VÁ LỖI: Chặn user Free.
    """
    # Logic chặn Free User
    chat_id_str = request.args.get("chat_id")
    is_pro = False
    if chat_id_str:
        try:
            cid = int(chat_id_str)
            is_pro = is_user_pro(cid) or (cid == ADMIN_ID)
        except: pass
    
    if not is_pro:
        # [NỘI DUNG RIÊNG CHO AI REPORT]
        return render_template_string(
            LOCKED_FEATURE_TEMPLATE,
            icon="📊",
            title="AI Phân Tích Danh Mục",
            desc="Trí tuệ nhân tạo đánh giá sức khỏe danh mục, cảnh báo rủi ro và nhận định xu hướng thị trường."
        ), 403
    # -----------------------------

    cached = get_report_from_redis(cache_key)
    
    if not cached:
        return render_template_string(REPORT_404_TEMPLATE), 404

    text_json, generated_at, is_error, wait_sec = cached

    if is_error:
        return f"<h3>Đang gặp lỗi hoặc quá tải: {text_json}</h3>", 500

    try:
        data = json.loads(text_json)
    except Exception as e:
        log.error(f"Lỗi parse JSON report route: {e}")
        return "Lỗi dữ liệu báo cáo (Invalid JSON)", 500

    vn_tz = pytz.timezone(TIMEZONE)
    time_str = "Vừa xong"
    if generated_at:
        if generated_at.tzinfo is None:
             generated_at = generated_at.replace(tzinfo=datetime.timezone.utc)
        time_str = generated_at.astimezone(vn_tz).strftime("%H:%M %d/%m/%Y")

    return render_template_string(
        REPORT_HTML_TEMPLATE, 
        data=data, 
        generated_at=time_str,
        is_pro=True 
    )

@flask_app.route("/screener")
def view_screener():
    """
    Route hiển thị Web App Screener.
    Query params: ?type=all&chat_id=123
    """
    screener_type = request.args.get("type", "all").lower()

    # Logic chặn Free User
    chat_id_str = request.args.get("chat_id")
    is_pro = False
    if chat_id_str:
        try:
            cid = int(chat_id_str)
            is_pro = is_user_pro(cid) or (cid == ADMIN_ID)
        except: pass
    
    if not is_pro:
        # [NỘI DUNG RIÊNG CHO SCREENER]
        return render_template_string(
            LOCKED_FEATURE_TEMPLATE,
            icon="💎",
            title="Bộ Lọc Giá Trị Realtime",
            desc="Lọc cổ phiếu định giá rẻ (P/E, P/B) và hiệu quả cao (ROE) ngay trong phiên giao dịch."
        ), 403
    
    # A. Check Redis
    cached = load_value_screener_from_redis(screener_type)
    data = None
    
    if cached:
        data = cached
    else:
        # B. Gọi API (nếu miss cache)
        try:
            data = run_value_screener_from_api(screener_type)
            if data:
                save_value_screener_to_redis(data, screener_type)
        except Exception as e:
            log.error(f"[WEBAPP SCREENER] Error fetching data: {e}")

    error_msg = None
    if not data or not data.get("industries"):
        error_msg = f"Không lấy được dữ liệu cho tiêu chí {screener_type.upper()}."

    return render_template_string(
        SCREENER_HTML_TEMPLATE,
        data=data,
        current_type=screener_type,
        chat_id=chat_id_str,
        error=error_msg
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
    Nó sẽ được gọi bởi 'lifespan.startup' SAU KHI server đã mở port.
    """
    global tg_app, log, INSTANCE_ID

    webhook_url = None
    
    if IS_PRODUCTION:
        # Chế độ Production: Lấy URL từ Render
        log.info(f"[{INSTANCE_ID}] [Lifespan] Chế độ PRODUCTION, đang lấy URL từ Render...")
        webhook_url = os.getenv("RENDER_EXTERNAL_URL") + "/webhook"
        log.info(f"[{INSTANCE_ID}] [Khôi Trần] check xem đúng được link có /webhook chưa: {webhook_url}")
        if not webhook_url:
            host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
            if host:
                webhook_url = f"https://{host}/webhook"
    else:
        # # Chế độ Local: Lấy URL từ .env (biến NGROK_URL)
        # log.info(f"[{INSTANCE_ID}] [Lifespan] Chế độ LOCAL, đang lấy URL từ .env (NGROK_URL)...")
        # # Thay vì hardcode URL ngrok, hãy đọc từ file .env
        # webhook_url = os.getenv("NGROK_URL") 
        # if webhook_url and not webhook_url.endswith("/webhook"):
        #     webhook_url += "/webhook"

        # 🛠️ LOCAL: Dùng polling thủ công để chia sẻ event loop với các loop khác
        log.info(f"[{INSTANCE_ID}] [LOCAL] Bắt đầu chạy Polling (updater.start_polling)...")

        # Đảm bảo không còn webhook nào đang set
        await tg_app.bot.delete_webhook(drop_pending_updates=True)

        # 🔄 Bật polling (không block event loop, chỉ start fetcher)
        await tg_app.updater.start_polling(drop_pending_updates=True)

        log.info(f"[{INSTANCE_ID}] [LOCAL] Polling đã start, chờ update từ Telegram...")

        return  # Không cần set webhook trong local polling

            # -----------------------------------------------------------------

    if not webhook_url:
        log.error(
            f"[{INSTANCE_ID}] ⚠️ [Lifespan] KHÔNG THỂ SET WEBHOOK. "
            "Không tìm thấy RENDER_EXTERNAL_URL (production) hoặc NGROK_URL (local)."
        )
        return

    # Thực hiện gọi API set webhook (lấy từ code cũ của bạn)
    try:
        success = await tg_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
        )
        if success:
            log.info(f"[{INSTANCE_ID}] ✅ [Lifespan] Webhook đã set thành công: {webhook_url}")
        else:
            log.error(f"[{INSTANCE_ID}] ❌ [Lifespan] API set_webhook trả về 'False'.")
    except TelegramError as e:
        log.error(
            f"[{INSTANCE_ID}] ❌ [Lifespan] SET WEBHOOK THẤT BẠI (TelegramError)!"
            f" URL: {webhook_url} | Lỗi: {e}"
        )
    except Exception as e:
        log.error(
            f"[{INSTANCE_ID}] ❌ [Lifespan] SET WEBHOOK THẤT BẠI (Lỗi chung)!"
            f" URL: {webhook_url} | Lỗi: {e}"
        )

async def run_telegram_processing():
    """
    Khởi chạy và duy trì các tiến trình nội bộ của thư viện python-telegram-bot.
    (Đây là phần tg_app.start() và logic Polling)
    """
    global tg_app, log, INSTANCE_ID, IS_PRODUCTION
    
    await tg_app.initialize()
    await tg_app.start()
    
    # Nếu chạy local mà KHÔNG CÓ NGROK_URL -> Chuyển sang POLLING
    # (Code Polling của bạn đã bị comment, tôi kích hoạt lại nó ở đây)
    if not IS_PRODUCTION and not os.getenv("NGROK_URL"):
        log.info(f"[{INSTANCE_ID}] [LOCAL] Không tìm thấy NGROK_URL, "
                 "chuyển sang chạy Polling (xóa webhook)...")
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
        await tg_app.updater.start_polling(drop_pending_updates=True)
        log.info(f"[{INSTANCE_ID}] [LOCAL] Polling đã start.")
    else:
        log.info(f"[{INSTANCE_ID}] Bot đang chạy ở chế độ Webhook. "
                 "`run_telegram_processing` đang duy trì tiến trình...")
        
    # Giữ cho các task của tg_app (như job_queue) được chạy
    # Đây là nơi đúng để dùng Event().wait()
    await asyncio.Event().wait() 

async def asgi_wrapper_app(scope, receive, send):
    """
    Ứng dụng ASGI "vỏ bọc" chính.
    - Xử lý 'lifespan' (startup/shutdown).
    - Chuyển tiếp 'http' cho Flask.
    """
    global wsgi_app, log, tg_app, IS_PRODUCTION

    # 🚀 BIẾN MỚI: Khai báo để sử dụng
    global BACKGROUND_TASKS, MAIN_LOOP
    global ADMIN_ID, initial_active, INSTANCE_ID
 
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                log.info("[Lifespan] Server startup. Đang chuẩn bị set webhook...")
                
                if IS_PRODUCTION or os.getenv("NGROK_URL"):
                    await set_telegram_webhook() 
                
                # 1. Báo cho Hypercorn biết là đã startup xong
                await send({"type": "lifespan.startup.complete"})
                
                # 2. 🚦 MÁY CHỦ ĐÃ SỐNG! BÂY GIỜ MỚI CHẠY TÁC VỤ NỀN
                log.info("[Lifespan] Startup complete. Server is live. Starting background tasks...")
   
                # (Lấy toàn bộ các loop từ hàm main() chuyển lên đây)
                BACKGROUND_TASKS = [
                    #----------------alert_loop---------------
                    MAIN_LOOP.create_task(alert_loop()),
                    MAIN_LOOP.create_task(stock_price_fetcher_loop()), # Fetcher (15s)
                    MAIN_LOOP.create_task(stock_broadcast_loop()),   # Broadcaster (chờ queue)
                    #-------------- vn30f1m_alert_loop ------------
                    MAIN_LOOP.create_task(vn30f1m_alert_loop()),      # Ticker (5s)
                    MAIN_LOOP.create_task(vn30f1m_price_fetcher_loop()), # Fetcher (61s)
                    MAIN_LOOP.create_task(vn30f1m_broadcast_loop()),   # Broadcaster (chờ queue)
                    #-------------- News_loop ------------
                    MAIN_LOOP.create_task(news_specialized_loop()),
                    MAIN_LOOP.create_task(news_macro_loop()),
                    MAIN_LOOP.create_task(news_cleanup_loop()),
                    #--------------------------------------
                    MAIN_LOOP.create_task(session_notice_loop()),
                    MAIN_LOOP.create_task(weekly_report_loop()),
                    MAIN_LOOP.create_task(analysis_report_loop()),
                    MAIN_LOOP.create_task(financial_Statements_notice_loop()),
                    MAIN_LOOP.create_task(daily_user_digest_loop()),
                    MAIN_LOOP.create_task(restore_reminder_loop()),
                    MAIN_LOOP.create_task(run_background_startup_tasks(ADMIN_ID, initial_active, INSTANCE_ID, tg_app)),
                    MAIN_LOOP.create_task(auto_on_after_delay(initial_active)),
                ]
                log.info(f"[Lifespan] Đã khởi động {len(BACKGROUND_TASKS)} tác vụ nền.")
            
            elif message["type"] == "lifespan.shutdown":
                log.info("[Lifespan] Server shutdown. Cancelling background tasks...")
                
                # 3. Dọn dẹp các tác vụ nền khi tắt
                for task in BACKGROUND_TASKS:
                    task.cancel()
                    
                # log.info("[Lifespan] Đang xóa webhook...")
                # try:
                #     if IS_PRODUCTION: 
                #         await tg_app.bot.delete_webhook(drop_pending_updates=True)
                #         log.info("[Lifespan] Đã xóa webhook.")
                # except Exception as e:
                #     log.warning(f"[Lifespan] Lỗi khi xóa webhook: {e}")
                
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
            ("list", "📋 Xem danh mục theo dõi"),
            ("add", "➕ Thêm mã (VD: /add HPG)"),
            ("report", "📊 Phân tích danh mục AI"),
            
            # --- CÔNG CỤ ---
            ("screener_value", "💎 Bộ lọc cổ phiếu giá trị"),
            ("info", "📄 Tra cứu doanh nghiệp"),
            ("setting", "⚙️ Cài đặt & Gói Pro"),
            ("help", "❓ Hướng dẫn sử dụng"),
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

    # Khởi tạo Application
    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
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

    # Admin commands
    tg_app.add_handler(CommandHandler("news_test_macro", cmd_news_test_macro))
    tg_app.add_handler(CommandHandler("news_test_specialized", cmd_news_test_specialized))
    tg_app.add_handler(CommandHandler("on", cmd_on))
    tg_app.add_handler(CommandHandler("off", cmd_off))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("announce", cmd_announce))
    tg_app.add_handler(CommandHandler("allwatch", cmd_allwatch))
    tg_app.add_handler(CommandHandler("delete_range", cmd_delete_range))
    tg_app.add_handler(CommandHandler("screener_value_clear", cmd_screener_value_clear))
    tg_app.add_handler(CommandHandler("backup_core", cmd_backup_core))
    tg_app.add_handler(CommandHandler("restore_core", cmd_restore_core))
    tg_app.add_handler(CommandHandler("admin_add_user", cmd_admin_add_user))
    tg_app.add_handler(CommandHandler("admin_deactivate", cmd_admin_deactivate))
    tg_app.add_handler(CommandHandler("admin_remove_user", cmd_admin_remove_user))
    tg_app.add_handler(CommandHandler("cmd_run_weekly_report_now", cmd_run_weekly_report_now))
    tg_app.add_handler(CommandHandler("test_digest", cmd_admin_test_digest))
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
if __name__ == "__main__":
    log.info("🚀 Khởi động bot đa người dùng + Flask keepalive (Render Web Service)...")
    asyncio.run(main())
