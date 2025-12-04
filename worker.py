# worker.py
import asyncio
import json
import datetime
import pytz
import logging
import os
import random
import shutil
from vnstock import Trading, Quote, Screener, Finance, Company, Vnstock
import redis
from dotenv import load_dotenv
from google import genai
import uuid
import time
import pandas as pd
import numpy as np

# pandas-ta (custom build) still expects numpy.NaN to exist; numpy>=2 drops it.
# Create the alias before importing pandas_ta so the package can import cleanly.
if not hasattr(np, "NaN"):
    np.NaN = np.nan

import pandas_ta as ta  # noqa: F401  # Registers the DataFrame .ta accessor
pd.set_option('future.no_silent_downcasting', True)
from typing import Any, Optional
import feedparser
import re
import csv
import tempfile
import urllib3
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin # Để parse REDIS_URL
from datetime import timedelta
from flask import Flask, jsonify
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi
from chart_utils import generate_mini_chart, draw_sector_performance_chart
from manual_valuation import fetch_manual_pe_pb
from db_utils import (
    get_all_watch,
    get_all_pro_chat_ids,
    get_bot_active,
    get_users_with_stock_alert_off,
    get_vn30f1m_enabled_map,
    get_vnindex_enabled_map,
    get_vn30_enabled_map,
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
    get_stock_personalization_map,
    cleanup_expired_stock_personalizations,
    get_ai_questions_by_month
)
from report_cache import (
    make_report_cache_key,
    save_report_to_redis,
    get_report_from_redis,
    delete_report_from_redis,
)
from ai_knowledge import BOT_KNOWLEDGE_BASE
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from urllib.parse import urlparse # Để parse REDIS_URL

# --- CẤU HÌNH CƠ BẢN ---
load_dotenv()
TIMEZONE = "Asia/Ho_Chi_Minh"
INSTANCE_ID = "WORKER_01" # Định danh cho Worker
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

def _resolve_web_base_url() -> str:
    """Worker phải dùng URL Gateway, không phải domain riêng của Worker."""
    candidates = [
        os.getenv("GATEWAY_BASE_URL"),
        os.getenv("WEB_APP_BASE_URL"),
        os.getenv("NGROK_URL"),
        os.getenv("RENDER_EXTERNAL_URL"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate.rstrip("/")
    return "https://google.com"

BASE_URL = _resolve_web_base_url()  # URL công khai của Gateway/WebApp
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GSO_DATA_DIR = os.path.join(BASE_DIR, "GSO_Data")
os.makedirs(GSO_DATA_DIR, exist_ok=True)

# Cấu hình Redis Output
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CHANNEL_OUTBOUND = 'telegram_outbound'
REDIS_CHANNEL_INBOUND = 'worker_inbound'
AGENT_TYPES = ("macro", "biz", "tech")
AGENT_RESULT_TTL = 24 * 60 * 60  # 24h
AGENT_BUNDLE_TTL = 7 * 24 * 60 * 60  # 7 ngày
BIZ_CACHE_KEY_PREFIX = "biz_cache"
BIZ_CACHE_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 ngày
BIZ_AGENT_MAX_DATASET_PERIODS = 20
BIZ_AGENT_BATCH_SIZE = 10
BIZ_AGENT_BATCH_PAUSE_SECONDS = 8  # Nghỉ giữa các batch để tránh rate limit
TECH_AGENT_HISTORY_DAYS = 365
TECH_AGENT_EXPORT_LOOKBACK_DAYS = 200
TECH_AGENT_BATCH_SIZE = 5
TECH_AGENT_BATCH_PAUSE_SECONDS = 5
TECH_AGENT_MAX_SYMBOLS = 30
VCI_RATE_LIMIT_BACKOFF_SECONDS = 6
TECH_INDICATOR_COLUMN_MAP = {
    "time": "Date",
    "close": "Close",
    "EMA_20": "EMA20",
    "EMA_50": "EMA50",
    "EMA_200": "EMA200",
    "RSI_14": "RSI",
    "MACD_12_26_9": "MACD",
    "MACDs_12_26_9": "MACDs",
    "MACDh_12_26_9": "MACDh",
    "BBU_20_2.0": "BBU",
    "BBL_20_2.0": "BBL",
}
TECH_ALERT_CACHE_TTL_SECONDS = 600  # Cache kết quả phân tích kỹ thuật trong 10 phút

ALERT_FUN_LINES_UP = [
    "🚀 Bảng điện xanh rì – giữ vững tinh thần!",
    "🌱 Dòng tiền đang ủng hộ, đừng vội rời trận.",
    "💹 Khối ngoại gom hàng, ta cũng không thể đứng ngoài!",
]
ALERT_FUN_LINES_DOWN = [
    "🧯 Đỏ quá thì nhấp cafe bình tĩnh đã nhé.",
    "🛑 Giữ tiền quan trọng hơn giữ cảm xúc, hít thở sâu nào.",
    "📉 Sóng gió tạm thời, quản trị rủi ro trước đã!",
]

REPORT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "general_market_comment": {"type": "string"},
        "general_portfolio_comment": {"type": "string"},
        "portfolio_health_score": {"type": "string"},
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "industry": {"type": "string"},
                    "analysis": {"type": "string"},
                    "key_metrics": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": [
                    "symbol",
                    "industry",
                    "analysis",
                    "key_metrics",
                    "action",
                ],
            },
        },
    },
    "required": [
        "general_market_comment",
        "general_portfolio_comment",
        "portfolio_health_score",
        "stocks",
    ],
}


class VCIRateLimitError(RuntimeError):
    """Bọc lỗi Rate Limit của nguồn dữ liệu VCI để xử lý thống nhất."""


def _is_vci_rate_limit_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, VCIRateLimitError):
        return True

    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True

    message = str(exc).lower()
    keywords = ["rate limit", "too many requests", "429", "try again later"]
    return any(keyword in message for keyword in keywords)

REPORT_SECTION_KEYWORDS = [
    (re.compile(r"(🚀 động lực chính|key\s*drivers?)", re.IGNORECASE), "*🚀 Động lực chính*"),
    (re.compile(r"(💡 cơ hội|opportunit)", re.IGNORECASE), "*💡 Cơ hội*"),
    (re.compile(r"(⚠️ rủi ro|risk)", re.IGNORECASE), "*⚠️ Rủi ro*"),
]
MACRO_NEWS_LOOKBACK_HOURS = 48
MACRO_GSO_MONTH_LIMIT = 3
MACRO_GSO_LOOKBACK_MONTHS = 12
MACRO_SECTOR_NAMES = [
    "Bán lẻ",
    "Bảo hiểm",
    "Bất động sản",
    "Công nghệ Thông tin",
    "Du lịch và Giải trí",
    "Dầu khí",
    "Dịch vụ tài chính",
    "Hàng & Dịch vụ Công nghiệp",
    "Hàng cá nhân & Gia dụng",
    "Hóa chất",
    "Ngân hàng",
    "Thực phẩm và đồ uống",
    "Truyền thông",
    "Tài nguyên Cơ bản",
    "Viễn thông",
    "Xây dựng và Vật liệu",
    "Y tế",
    "Ô tô và phụ tùng",
    "Điện, nước & xăng dầu khí đốt",
]
GSO_BASE_URL = "https://www.nso.gov.vn"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

PORT = int(os.getenv("PORT") or os.getenv("PASSENGER_PORT", "10001"))

flask_app = Flask(__name__)


@flask_app.get("/")
@flask_app.get("/health")
@flask_app.get("/healthz")
def worker_healthcheck():
    """Expose a minimal HTTP endpoint so Render sees an open port."""
    return jsonify({
        "status": "ok",
        "service": "worker",
        "instance": INSTANCE_ID,
        "time": datetime.datetime.now().isoformat(),
    })


wsgi_app = WsgiToAsgi(flask_app)
_worker_task: Optional[asyncio.Task] = None


async def asgi_wrapper_app(scope, receive, send):
    """ASGI wrapper to manage worker background tasks via Hypercorn lifespan."""
    global _worker_task

    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                log.info(f"[{INSTANCE_ID}] Lifespan startup → booting worker runtime.")
                if _worker_task is None:
                    _worker_task = asyncio.create_task(run_worker_runtime())
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                log.info(f"[{INSTANCE_ID}] Lifespan shutdown → stopping worker runtime.")
                if _worker_task:
                    _worker_task.cancel()
                    try:
                        await _worker_task
                    except asyncio.CancelledError:
                        pass
                    _worker_task = None
                await send({"type": "lifespan.shutdown.complete"})
                break
    elif scope["type"] == "http":
        await wsgi_app(scope, receive, send)

# Cấu hình Loop
FETCHER_INTERVAL_SECONDS = 20
TICKER_INTERVAL_SECONDS = 10

# Cache cục bộ của Worker
_stock_current_price_cache = {} 
_stock_current_watch_cache = {}
_stock_alert_disabled_cache = set()
ALERT_STATE = {}
_tech_alert_summary_cache: dict[str, dict[str, Any]] = {}

# --- CẤU HÌNH MARKET MONITOR (VN30F1M, VNINDEX, VN30) ---
MARKET_MONITORS = {
    "VN30F1M": {
        "threshold": 5,
        "get_users_func": get_vn30f1m_enabled_map,
        "msg_type": "VN30_ALERT", # Giữ nguyên legacy
        "tick_sec": 5
    },
    "VNINDEX": {
        "threshold": 5,
        "get_users_func": get_vnindex_enabled_map,
        "msg_type": "VNINDEX_ALERT",
        "tick_sec": 10
    },
    "VN30": {
        "threshold": 5,
        "get_users_func": get_vn30_enabled_map,
        "msg_type": "VN30_INDEX_ALERT",
        "tick_sec": 10
    }
}

# State Market Monitor
_market_data = {
    "VN30F1M": {"price": None, "ref": None, "anchor": None, "date": None},
    "VNINDEX": {"price": None, "ref": None, "anchor": None, "date": None},
    "VN30":    {"price": None, "ref": None, "anchor": None, "date": None},
}

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


def _reconnect_redis_client() -> redis.Redis | None:
    """Thử tạo lại Redis client dùng chung."""
    global r_client
    try:
        r_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r_client.ping()
        log.info(f"[{INSTANCE_ID}] 🔁 Redis client đã được kết nối lại thành công.")
        return r_client
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] ❌ Không thể reconnect Redis: {exc}")
        r_client = None
        return None


def ensure_redis_client() -> redis.Redis | None:
    """Đảm bảo r_client còn sống; nếu không sẽ reconnect."""
    global r_client
    if r_client is None:
        return _reconnect_redis_client()
    try:
        r_client.ping()
        return r_client
    except Exception as exc:
        log.warning(f"[{INSTANCE_ID}] ⚠️ Redis ping thất bại: {exc}. Đang reconnect...")
        return _reconnect_redis_client()

# Khởi tạo Trading object (VCI)
try:
    stock_trading = Trading(source="VCI")
except:
    stock_trading = None

# --- CẤU HÌNH GEMINI (Worker) ---
GEMINI_KEYS = []
_gemini_keys_map = {}

# 1. Lấy key chính (GEMINI_API_KEY)
if os.getenv("GEMINI_API_KEY"):
    _gemini_keys_map[1] = os.getenv("GEMINI_API_KEY")

# 2. Quét toàn bộ biến môi trường để tìm GEMINI_API_KEY_n
for key, value in os.environ.items():
    if key.startswith("GEMINI_API_KEY_") and value:
        try:
            # Lấy số thứ tự từ tên biến (ví dụ: GEMINI_API_KEY_2 -> 2)
            suffix = key.replace("GEMINI_API_KEY_", "")
            if suffix.isdigit():
                _gemini_keys_map[int(suffix)] = value
        except ValueError:
            continue

# 3. Sắp xếp theo thứ tự index và đưa vào danh sách
for idx in sorted(_gemini_keys_map.keys()):
    GEMINI_KEYS.append(_gemini_keys_map[idx])

GEMINI_KEYS = [k for k in GEMINI_KEYS if k] # Lọc key rỗng
_gemini_key_index = 0 # Biến đếm toàn cục để xoay vòng

def prepare_personalization_keys(symbols: list[str]) -> list[str]:
    """
    Từ danh sách mã cổ phiếu (VD: ['HPG', 'VCB'])
    -> Map ra ngành (VD: ['Tài nguyên Cơ bản', 'Ngân hàng'])
    -> Thêm mã Vĩ mô ('VN_MACRO')
    -> Trả về danh sách tổng hợp để query DB.
    """
    # 1. Load mapping ngành (Load file sectors.json)
    # Lưu ý: Nên load 1 lần hoặc cache, ở đây load mỗi lần cho chắc chắn dữ liệu mới
    # Nếu file lớn có thể tối ưu sau.
    sector_map = load_symbol_sector_map() # Hàm này đã có sẵn trong worker.py
    
    keys = set()
    
    # Thêm mã cổ phiếu
    for sym in symbols:
        keys.add(sym)
        # Thêm ngành tương ứng
        sector = sector_map.get(sym)
        if sector and sector != "Khác":
            keys.add(sector)
            
    # Thêm mã Vĩ mô
    keys.add("VN_MACRO")
    
    return list(keys)

def _get_rotated_gemini_keys() -> list[str]:
    """Trả về danh sách key theo cơ chế xoay vòng."""
    global _gemini_key_index
    if not GEMINI_KEYS:
        return []
    start_idx = _gemini_key_index % len(GEMINI_KEYS)
    rotated = GEMINI_KEYS[start_idx:] + GEMINI_KEYS[:start_idx]
    _gemini_key_index += 1
    return rotated

async def _generate_monthly_insight(questions: list[str], period_label: str) -> str:
    """
    Helper: Gửi danh sách câu hỏi cho Gemini để phân tích Insight.
    """
    # 1. Lấy mẫu nếu dữ liệu quá lớn (tránh lỗi context window)
    # Giới hạn khoảng 500 câu hỏi mới nhất để phân tích xu hướng
    SAMPLE_LIMIT = 500
    total_q = len(questions)
    
    if total_q > SAMPLE_LIMIT:
        input_data = "\n".join(questions[:SAMPLE_LIMIT])
        note_limit = f"(Đã lấy mẫu {SAMPLE_LIMIT}/{total_q} câu mới nhất để phân tích)"
    else:
        input_data = "\n".join(questions)
        note_limit = f"(Tổng hợp toàn bộ {total_q} câu hỏi)"

    prompt = f"""
Bạn là Chuyên gia Phân tích Trải nghiệm Khách hàng (CX Analyst) cho Bot Chứng khoán.
Dưới đây là danh sách các câu hỏi người dùng đã gửi trong tháng {period_label}:

--- DỮ LIỆU BẮT ĐẦU ---
{input_data}
--- DỮ LIỆU KẾT THÚC ---

Nhiệm vụ của bạn:
1. 📊 **Phân loại chủ đề:** Chia các câu hỏi thành 3-5 nhóm chính (Ví dụ: Hỏi mã cổ phiếu, Hỏi kiến thức cơ bản, Báo lỗi, Tán gẫu...). Tính tỷ lệ % ước lượng.
2. 🔥 **Top 5 Vấn đề quan tâm:** Liệt kê 5 nội dung cụ thể được hỏi nhiều nhất (VD: Mã HPG, Cách mở tài khoản...).
3. 💡 **Đề xuất cải thiện:** Dựa trên các câu hỏi (đặc biệt là các câu hỏi về lỗi hoặc hướng dẫn), hãy đề xuất 3 tính năng hoặc nội dung cần bổ sung cho Bot.
4. 😊 **Cảm xúc dòng tiền:** Đánh giá sơ bộ tâm lý nhà đầu tư qua các câu hỏi (Lo lắng, Hưng phấn, hay Thận trọng?).

Hãy trình bày kết quả dưới dạng báo cáo Markdown ngắn gọn, chuyên nghiệp, dùng emoji phù hợp.
{note_limit}
"""
    try:
        # Gọi Gemini (Dùng Flash cho nhanh và rẻ, context window lớn)
        report = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-pro", 
            contents=prompt
        )
        return report.strip() if report else "⚠️ AI không trả về kết quả."
    except Exception as e:
        log.error(f"[MONTHLY_INSIGHT] Lỗi gọi AI: {e}")
        return f"⚠️ Lỗi khi phân tích AI: {e}"

async def job_monthly_cskh_report():
    """
    [JOB APSCHEDULER] Chạy vào 08:00 sáng ngày 1 hàng tháng.
    Tổng hợp câu hỏi user tháng trước và báo cáo cho Admin.
    """
    if not ADMIN_ID:
        log.warning("[MONTHLY_INSIGHT] Chưa cấu hình ADMIN_ID. Bỏ qua job.")
        return

    log.info("[MONTHLY_INSIGHT] 📅 Bắt đầu tổng hợp báo cáo CSKH tháng...")
    
    # 1. Xác định tháng trước
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    
    # Lấy ngày đầu tháng hiện tại, trừ đi 1 ngày để về tháng trước
    first_day_this_month = now.replace(day=1)
    
    target_month = first_day_this_month.month
    target_year = first_day_this_month.year
    period_label = f"{target_month}/{target_year}"

    try:
        # 2. Lấy dữ liệu từ DB
        questions = await asyncio.to_thread(get_ai_questions_by_month, target_year, target_month)
        
        if not questions:
            msg = f"📉 **BÁO CÁO CSKH THÁNG {period_label}**\n\nKhông có câu hỏi nào được ghi nhận trong tháng qua."
            push_telegram_msg(ADMIN_ID, msg, msg_type="SYSTEM_MSG")
            return

        # 3. Gửi lời nhắn chờ (vì AI có thể chạy lâu)
        push_telegram_msg(ADMIN_ID, f"⏳ Đang tổng hợp {len(questions)} câu hỏi tháng {period_label} để phân tích...", msg_type="SYSTEM_MSG")

        # 4. Phân tích AI
        ai_analysis = await _generate_monthly_insight(questions, period_label)
        
        # 5. Gửi báo cáo hoàn chỉnh
        final_msg = (
            f"📈 **TỔNG HỢP CSKH THÁNG {period_label}**\n\n"
            f"{ai_analysis}\n\n"
            f"--------------------\n"
            f"🤖 *Báo cáo tự động bởi StockBot AI Worker*"
        )
        
        push_telegram_msg(ADMIN_ID, final_msg, msg_type="SYSTEM_MSG")
        log.info(f"[MONTHLY_INSIGHT] ✅ Đã gửi báo cáo tháng {period_label}.")

    except Exception as e:
        log.error(f"[MONTHLY_INSIGHT] ❌ Lỗi job: {e}")
        push_telegram_msg(ADMIN_ID, f"⚠️ Lỗi tạo báo cáo tháng {period_label}: {e}", msg_type="SYSTEM_MSG")

def call_gemini_safe(model_id, contents, config=None, return_usage=False):
    """Hàm gọi Gemini an toàn (Failover) với cơ chế Round Robin (Xoay vòng từng request)"""
    global _gemini_key_index
    last_error = None
    
    # [UPDATED] Cơ chế Round Robin: Chia bài đều lần lượt cho từng Key
    # Đảm bảo Key A vừa dùng xong sẽ không bị gọi lại ngay ở request tiếp theo (trừ khi chỉ có 1 key).
    rotated_keys = _get_rotated_gemini_keys()

    for api_key in rotated_keys:
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_id, contents=contents, config=config
            )
            text = getattr(resp, "text", "").strip()
            
            if return_usage:
                usage = getattr(resp, "usage_metadata", None)
                return text, usage
                
            return text
        except Exception as e:
            last_error = e
            continue
    log.error(f"All Gemini keys failed: {last_error}")
    return (None, None) if return_usage else None

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

        edit_id = payload.get("edit_id")
        if edit_id:
            log.info(f"[{INSTANCE_ID}] 📤 Push edit payload chat={chat_id} msg={edit_id} type={msg_type}")

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


def push_task_unlock_signal(chat_id: int):
    """Gửi tín hiệu yêu cầu Gateway mở khóa tác vụ cho chat_id."""
    if not chat_id:
        return
    try:
        push_telegram_msg(chat_id=chat_id, text="", msg_type="TASK_UNLOCK")
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] ❌ Lỗi gửi tín hiệu unlock {chat_id}: {exc}")


def default_ai_reply_markup():
    """Nút mặc định quay lại Dashboard/Hướng dẫn."""
    return {
        "inline_keyboard": [[
            {"text": "🏠 Dashboard", "callback_data": "back_to_start"},
            {"text": "❓ Hướng dẫn", "callback_data": "menu_help"}
        ]]
    }


def _agent_result_key(agent_type: str) -> str:
    return f"agent:{agent_type}:current"


def _agent_bundle_key(chat_id: int) -> str:
    return f"agent:bundle:{chat_id}:current"


def save_agent_result(agent_type: str, payload: dict):
    if not r_client:
        return
    try:
        r_client.setex(
            _agent_result_key(agent_type),
            AGENT_RESULT_TTL,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        log.error(f"Save agent {agent_type} result error: {exc}")


def save_agent_bundle(chat_id: int, payload: dict):
    if not r_client:
        return
    try:
        r_client.setex(
            _agent_bundle_key(chat_id),
            AGENT_BUNDLE_TTL,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        log.error(f"Save agent bundle error: {exc}")


def _build_agent_stub(agent_type: str, request_id: str, ts_iso: str) -> dict:
    return {
        "agent": agent_type,
        "request_id": request_id,
        "generated_at": ts_iso,
        "insights": [],
        "raw_data": {},
        "notes": "TODO: thêm logic crawl+LLM cho agent này.",
    }


def write_agent_payload_to_file(agent_type: str, request_id: str, payload: dict) -> str:
    tmp_dir = tempfile.gettempdir()
    filename = f"{agent_type}_agent_{request_id}.json"
    path = os.path.join(tmp_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.error(f"Write agent file error: {exc}")
    return path


def write_temp_json_file(filename: str, payload: dict | list | None) -> str | None:
    tmp_dir = tempfile.gettempdir()
    path = os.path.join(tmp_dir, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception as exc:
        log.error(f"Write temp JSON error ({filename}): {exc}")
        return None


def _get_cached_tech_summary(symbol: str) -> str | None:
    entry = _tech_alert_summary_cache.get(symbol)
    if not entry:
        return None

    ts: datetime.datetime | None = entry.get("ts")
    if not ts:
        return None

    age = datetime.datetime.now(datetime.timezone.utc) - ts
    if age.total_seconds() > TECH_ALERT_CACHE_TTL_SECONDS:
        _tech_alert_summary_cache.pop(symbol, None)
        return None

    return entry.get("text")


def _set_cached_tech_summary(symbol: str, text: str) -> None:
    if not symbol or not text:
        return
    _tech_alert_summary_cache[symbol] = {
        "text": text,
        "ts": datetime.datetime.now(datetime.timezone.utc),
    }


def _biz_cache_key(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    return f"{BIZ_CACHE_KEY_PREFIX}:{symbol}" if symbol else f"{BIZ_CACHE_KEY_PREFIX}:UNKNOWN"


def get_biz_cache(symbol: str) -> dict | None:
    client = ensure_redis_client()
    if not client:
        return None
    try:
        raw = client.get(_biz_cache_key(symbol))
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] Biz cache read error {symbol}: {exc}")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        log.warning(f"[{INSTANCE_ID}] Biz cache parse error {symbol}: {exc}")
        return None


def save_biz_cache(symbol: str, datasets: dict) -> dict | None:
    client = ensure_redis_client()
    payload = {
        "symbol": (symbol or "").strip().upper(),
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "Finance-VCI",
        "dataset_limit": BIZ_AGENT_MAX_DATASET_PERIODS,
        "datasets": datasets or {},
    }
    if not client:
        return payload
    try:
        client.setex(
            _biz_cache_key(symbol),
            BIZ_CACHE_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] Biz cache write error {symbol}: {exc}")
    return payload


def _flatten_finance_df(df: pd.DataFrame | None, limit: int = BIZ_AGENT_MAX_DATASET_PERIODS) -> list[dict]:
    if df is None or df.empty:
        return []
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        flat_cols = []
        for col in df.columns.values:
            if isinstance(col, tuple):
                flat_cols.append("_".join(str(part).strip() for part in col if part))
            else:
                flat_cols.append(str(col).strip())
        df.columns = flat_cols
    df.columns = [str(col).strip() for col in df.columns]

    # Giữ nguyên thứ tự gốc (API trả mới nhất trước), chỉ cắt 20 kỳ đầu tiên
    limited_df = df.head(limit)
    try:
        records = json.loads(limited_df.to_json(orient="records", date_format="iso"))
    except Exception:
        records = limited_df.replace({pd.NA: None}).to_dict(orient="records")
    return records


def _collect_finance_datasets(symbol: str) -> dict:
    try:
        stock = Finance(symbol=symbol, source="VCI")
    except BaseException as exc:
        if _is_vci_rate_limit_error(exc):
            raise VCIRateLimitError(f"VCI rate limit khi khởi tạo Finance cho {symbol}") from exc
        raise RuntimeError(f"Finance init lỗi cho {symbol}: {exc}") from exc

    datasets = {}
    fetch_plan = [
        ("ratio_quarter", lambda: stock.ratio(period="quarter", lang="vi")),
        ("balance_sheet_quarter", lambda: stock.balance_sheet(period="quarter", lang="vi")),
        ("income_statement_quarter", lambda: stock.income_statement(period="quarter", lang="vi")),
        ("cash_flow_quarter", lambda: stock.cash_flow(period="quarter", lang="vi")),
    ]

    for name, getter in fetch_plan:
        try:
            df = getter()
        except BaseException as exc:
            if _is_vci_rate_limit_error(exc):
                raise VCIRateLimitError(
                    f"VCI rate limit khi tải {name} của {symbol}"
                ) from exc
            log.warning(f"[{INSTANCE_ID}] Finance fetch error {symbol}-{name}: {exc}")
            continue
        records = _flatten_finance_df(df)
        if records:
            datasets[name] = records
        time.sleep(1)

    return datasets


def _build_finance_preview(datasets: dict) -> dict:
    preview: dict[str, Any] = {}
    ratio_rows = (datasets or {}).get("ratio_quarter") or []
    if ratio_rows:
        latest = ratio_rows[0]
        preview["pe"] = latest.get("pe") or latest.get("pe_ttm")
        preview["pb"] = latest.get("pb") or latest.get("pb_ttm")
        preview["roe"] = latest.get("roe")
    income_rows = (datasets or {}).get("income_statement_quarter") or []
    if income_rows:
        latest_income = income_rows[0]
        preview["revenue"] = latest_income.get("revenue") or latest_income.get("net_revenue")
        preview["profit_after_tax"] = latest_income.get("profit_after_tax") or latest_income.get("pat")
    cash_rows = (datasets or {}).get("cash_flow_quarter") or []
    if cash_rows:
        cf = cash_rows[0]
        preview["operating_cf"] = cf.get("cash_from_operating_activities")
    return {k: v for k, v in preview.items() if v is not None}


def _tech_agent_date_bounds(
    now: datetime.datetime | None = None,
) -> tuple[str, str, datetime.datetime]:
    now = now or datetime.datetime.now()
    start_calc = now - datetime.timedelta(days=TECH_AGENT_HISTORY_DAYS)
    export_cutoff = now - datetime.timedelta(days=TECH_AGENT_EXPORT_LOOKBACK_DAYS)
    return (
        start_calc.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m-%d"),
        export_cutoff,
    )


def _tech_agent_fetch_history(
    symbol: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame | None:
    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
    except BaseException as exc:
        if _is_vci_rate_limit_error(exc):
            raise VCIRateLimitError(f"VCI rate limit khi khởi tạo stock {symbol}") from exc
        log.warning(f"[{INSTANCE_ID}] [TECH] Lỗi khởi tạo stock {symbol}: {exc}")
        return None

    try:
        df = stock.quote.history(start=start_date, end=end_date, interval="1D")
    except BaseException as exc:
        if _is_vci_rate_limit_error(exc):
            raise VCIRateLimitError(f"VCI rate limit khi tải lịch sử {symbol}") from exc
        log.warning(f"[{INSTANCE_ID}] [TECH] Lỗi lấy dữ liệu {symbol}: {exc}")
        return None

    if df is None or df.empty:
        return None

    if "time" not in df.columns and "tradingDate" in df.columns:
        df = df.rename(columns={"tradingDate": "time"})

    if "time" not in df.columns:
        log.warning(f"[{INSTANCE_ID}] [TECH] Không có cột thời gian cho {symbol}")
        return None

    df["time"] = pd.to_datetime(df["time"])
    df.sort_values("time", inplace=True)
    df.set_index("time", inplace=True)
    return df


def _tech_agent_compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    working = df.copy()
    working["EMA_20"] = working.ta.ema(length=20)
    working["EMA_50"] = working.ta.ema(length=50)
    ema_200 = working.ta.ema(length=200)
    working["EMA_200"] = ema_200.iloc[:, 0] if isinstance(ema_200, pd.DataFrame) else ema_200
    working["RSI_14"] = working.ta.rsi(length=14)
    working.ta.macd(fast=12, slow=26, signal=9, append=True)
    working.ta.bbands(length=20, std=2, append=True)
    return working


def _tech_agent_build_records(
    df: pd.DataFrame,
    export_cutoff: datetime.datetime,
) -> list[dict]:
    df_export = df[df.index >= export_cutoff].copy()
    if df_export.empty:
        return []

    df_export.reset_index(inplace=True)
    df_export["time"] = df_export["time"].dt.strftime("%Y-%m-%d")
    available_cols = [col for col in TECH_INDICATOR_COLUMN_MAP if col in df_export.columns]
    if not available_cols:
        return []

    df_final = df_export[available_cols].rename(columns=TECH_INDICATOR_COLUMN_MAP)
    if "EMA200" in df_final.columns:
        df_final = df_final[df_final["EMA200"].notna()]
    try:
        records = df_final.to_dict(orient="records")
    except Exception:
        records = df_final.replace({pd.NA: None}).to_dict(orient="records")
    return records


def _collect_tech_indicator_dataset(
    symbol: str,
    start_date: str,
    end_date: str,
    export_cutoff: datetime.datetime,
) -> dict:
    payload = {
        "symbol": symbol,
        "records": [],
        "error": None,
        "stats": {},
    }

    df = _tech_agent_fetch_history(symbol, start_date, end_date)
    if df is None:
        payload["error"] = "Không lấy được dữ liệu lịch sử."
        return payload

    if len(df) < TECH_AGENT_HISTORY_DAYS:
        log.warning(
            f"[{INSTANCE_ID}] [TECH] {symbol} chỉ có {len(df)} phiên, vẫn tiếp tục tính chỉ báo."
        )

    enriched = _tech_agent_compute_indicators(df)
    records = _tech_agent_build_records(enriched, export_cutoff)
    if not records:
        payload["error"] = "Không tạo được bản ghi chỉ báo."
        return payload

    payload["records"] = records
    payload["stats"] = {
        "total_rows": len(df),
        "export_rows": len(records),
        "start_date": records[0]["Date"] if records else None,
        "end_date": records[-1]["Date"] if records else None,
    }
    return payload


def get_index_snapshot(symbol: str, now: datetime.datetime | None = None) -> dict | None:
    now = now or datetime.datetime.now()
    try:
        q = Quote(symbol=symbol, source='VCI')
        start_dt = (now - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
        end_dt = now.strftime('%Y-%m-%d')
        df = q.history(start=start_dt, end=end_dt, interval='1D')
        if df is None or len(df) < 2:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last['close'])
        prev_close = float(prev['close'])
        change = price - prev_close
        pct = (change / prev_close * 100) if prev_close else 0.0

        cls = "t-ref"
        if change > 0:
            cls = "t-up"
        elif change < 0:
            cls = "t-down"

        sign = "+" if change > 0 else ""

        volume_val = float(last.get('volume') or 0)

        return {
            "price": f"{price:,.2f}",
            "change_str": f"{sign}{change:,.2f} ({sign}{pct:.2f}%)",
            "cls": cls,
            "raw_price": price,
            "raw_change": change,
            "raw_pct": pct,
            "raw_volume": volume_val,
            "raw_prev_close": prev_close,
            "raw": {
                "price": price,
                "prev_close": prev_close,
                "change": change,
                "pct": pct,
                "volume": volume_val,
            }
        }
    except Exception as exc:
        log.warning(f"Index snapshot error for {symbol}: {exc}")
        return None


def get_macro_target_periods(now: datetime.datetime | None = None, max_months: int = MACRO_GSO_MONTH_LIMIT) -> list[tuple[int, int]]:
    now = now or datetime.datetime.now()
    current_month = now.month
    current_year = now.year

    if current_month >= 10:
        anchor_month = 10
    elif current_month >= 7:
        anchor_month = 7
    elif current_month >= 4:
        anchor_month = 4
    else:
        anchor_month = 1

    anchor_year = current_year
    periods = []
    temp_m, temp_y = anchor_month, anchor_year

    while True:
        periods.append((temp_m, temp_y))
        if temp_m == current_month and temp_y == current_year:
            break
        temp_m += 1
        if temp_m > 12:
            temp_m = 1
            temp_y += 1

    if max_months and len(periods) > max_months:
        periods = periods[-max_months:]

    return periods


def fetch_html(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=20, verify=False)
        if resp.status_code == 200:
            return BeautifulSoup(resp.content, 'html.parser')
    except Exception as exc:
        log.warning(f"GSO fetch error {url}: {exc}")
    return None


def _extract_gso_paragraphs(content_div) -> list[str]:
    if not content_div:
        return []

    collected = []
    seen = set()
    target_tags = ("p", "li", "h2", "h3", "h4", "h5", "blockquote")

    for node in content_div.find_all(target_tags):
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if text.lower().startswith("chia sẻ bài viết"):
            continue
        if text in seen:
            continue
        seen.add(text)
        collected.append(text)

    return collected


def crawl_gso_month(month: int, year: int) -> dict | None:
    list_url = f"{GSO_BASE_URL}/bai-top/{year}/{month:02d}/"
    soup = fetch_html(list_url)
    if not soup:
        return None

    target_link = None
    for link in soup.find_all('a', href=True):
        title = (link.get_text(strip=True) or '').lower()
        href = link['href']
        if "kinh tế" in title and "xã hội" in title:
            target_link = urljoin(GSO_BASE_URL, href)
            break

    if not target_link:
        return {
            "month": month,
            "year": year,
            "article_url": None,
            "paragraphs": [],
            "attachments": [],
            "status": "not_found",
        }

    article_soup = fetch_html(target_link)
    if not article_soup:
        return {
            "month": month,
            "year": year,
            "article_url": target_link,
            "paragraphs": [],
            "attachments": [],
            "status": "fetch_failed",
        }

    content_div = article_soup.find('div', class_=re.compile(r'(entry-content|post-content|article-body)'))
    paragraphs = _extract_gso_paragraphs(content_div)

    attachments = []
    for link in article_soup.find_all('a', href=True):
        href = link['href']
        if re.search(r'\.(xls|xlsx|pdf|doc|docx)$', href, re.IGNORECASE):
            abs_url = urljoin(GSO_BASE_URL, href)
            attachments.append({
                "name": os.path.basename(abs_url),
                "url": abs_url,
            })

    return {
        "month": month,
        "year": year,
        "article_url": target_link,
        "paragraphs": paragraphs,
        "attachments": attachments,
        "status": "ok" if paragraphs or attachments else "empty",
    }


def collect_gso_reports_sync(max_months: int = MACRO_GSO_MONTH_LIMIT) -> list[dict]:
    reports = []
    for month, year in get_macro_target_periods(max_months=max_months):
        report = crawl_gso_month(month, year)
        if report:
            reports.append(report)
    return reports


def _download_gso_attachment(url: str, folder: str | None = None) -> str | None:
    folder = folder or os.path.join(tempfile.gettempdir(), "gso_assets")
    os.makedirs(folder, exist_ok=True)

    parsed = urlparse(url)
    filename = os.path.basename(parsed.path) or f"gso_{uuid.uuid4().hex}"
    if "?" in filename:
        filename = filename.split("?")[0]
    local_path = os.path.join(folder, filename)
    if os.path.exists(local_path):
        base, ext = os.path.splitext(filename)
        local_path = os.path.join(folder, f"{base}_{uuid.uuid4().hex[:8]}{ext}")

    try:
        with requests.get(url, headers=HTTP_HEADERS, timeout=40, verify=False, stream=True) as resp:
            resp.raise_for_status()
            with open(local_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        out.write(chunk)
        return local_path
    except Exception as exc:
        log.warning(f"[GSO] Lỗi tải file {url}: {exc}")
        return None


def _convert_excel_to_multi_sheet_csv(xlsx_path: str) -> str | None:
    if not xlsx_path or not os.path.exists(xlsx_path):
        return None

    base = os.path.splitext(os.path.basename(xlsx_path))[0]
    csv_path = os.path.join(os.path.dirname(xlsx_path), f"{base}_ALL_SHEETS.csv")

    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None)
    except Exception as exc:
        log.error(f"[GSO] Không đọc được Excel {xlsx_path}: {exc}")
        return None

    try:
        with open(csv_path, "w", encoding="utf-8") as csv_file:
            for sheet_name, df in sheets.items():
                header = f"\n\n{'=' * 20} SHEET: {sheet_name} {'=' * 20}\n"
                csv_file.write(header)
                df.to_csv(csv_file, index=False)
        return csv_path
    except Exception as exc:
        log.error(f"[GSO] Không thể chuyển đổi CSV {xlsx_path}: {exc}")
        return None


def _prepare_gso_csv_asset(report: dict) -> dict | None:
    attachments = (report or {}).get("attachments") or []
    for attachment in attachments:
        url = attachment.get("url")
        name = (attachment.get("name") or "").lower()
        if not url:
            continue
        url_lower = url.lower()
        if not (name.endswith((".xlsx", ".xls")) or url_lower.endswith((".xlsx", ".xls"))):
            continue

        xlsx_path = _download_gso_attachment(url)
        if not xlsx_path:
            continue
        csv_path = _convert_excel_to_multi_sheet_csv(xlsx_path)
        if not csv_path:
            continue

        return {
            "attachment_url": url,
            "attachment_name": attachment.get("name"),
            "xlsx_path": xlsx_path,
            "csv_path": csv_path,
        }

    return None


def _derive_gso_period_tokens(report: dict | None, fallback_label: str | None = None) -> tuple[str | None, str | None]:
    """Return (slug, display) for naming & messaging."""
    month = report.get("month") if report else None
    year = report.get("year") if report else None
    display = fallback_label
    slug = None

    if isinstance(month, int) and isinstance(year, int):
        slug = f"{int(year):04d}{int(month):02d}"
        display = display or f"{int(month):02d}/{int(year)}"
    elif fallback_label:
        cleaned = fallback_label.replace("/", "")
        if cleaned.isdigit() and len(cleaned) == 6:
            slug = cleaned
        display = fallback_label

    return slug, display


def _persist_gso_csv_asset(report: dict, preferred_label: str | None = None) -> dict | None:
    csv_path = (report or {}).get("csv_path")
    if not csv_path or not os.path.exists(csv_path):
        return None

    slug, display = _derive_gso_period_tokens(report, preferred_label)
    if not slug:
        slug = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"MACRO_{slug}.csv"
    dest_path = os.path.join(GSO_DATA_DIR, filename)

    shutil.copy2(csv_path, dest_path)

    # Không lưu file tạm sau khi đã chuyển sang CSV
    try:
        os.remove(csv_path)
    except OSError:
        pass

    xlsx_path = (report or {}).get("xlsx_path")
    if xlsx_path and os.path.exists(xlsx_path):
        try:
            os.remove(xlsx_path)
        except OSError:
            pass

    metadata = {
        "csv_path": dest_path,
        "filename": filename,
        "period_label": display or slug,
        "attachment_url": (report or {}).get("attachment_url"),
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "file_size_bytes": os.path.getsize(dest_path),
    }
    return metadata


def collect_latest_gso_report(
    max_months: int = MACRO_GSO_LOOKBACK_MONTHS,
) -> tuple[dict | None, str | None, list[str]]:
    """Get the latest available GSO report by walking backward month by month."""
    now = datetime.datetime.now()
    month = now.month
    year = now.year
    tried_labels: list[str] = []

    for _ in range(max_months):
        label = f"{int(month):02d}/{int(year)}"
        tried_labels.append(label)
        report = crawl_gso_month(month, year)
        if report:
            asset = _prepare_gso_csv_asset(report)
            if asset:
                report.update(asset)
                return report, label, tried_labels
            else:
                log.info(f"[GSO] Không tìm thấy file Excel cho kỳ {label}.")

        month -= 1
        if month < 1:
            month = 12
            year -= 1

    return None, None, tried_labels


async def run_macro_agent(chat_id: int, request_id: str, ts_iso: str) -> dict:
    latest_report, latest_label, tried_labels = await asyncio.to_thread(
        collect_latest_gso_report
    )

    csv_path = (latest_report or {}).get("csv_path")
    if not latest_report or not csv_path or not os.path.exists(csv_path):
        target_label = latest_label or (tried_labels[0] if tried_labels else "N/A")
        attempts = ", ".join(tried_labels) if tried_labels else "không xác định"
        note = (
            "Chưa tìm thấy báo cáo GSO có file Excel phù hợp. "
            f"Các kỳ đã thử: {attempts}."
        )
        payload = _build_agent_stub("macro", request_id, ts_iso)
        empty_stats = {
            "gso_period": target_label,
            "attempts": tried_labels,
            "csv_path": None,
            "gso_data_dir": GSO_DATA_DIR,
            "errors": [note],
        }
        payload.update({
            "notes": note,
            "raw_data": empty_stats,
            "redis_json": {
                "generated_at": ts_iso,
                "status": "CSV_MISSING",
                "gso_data_dir": GSO_DATA_DIR,
                "attempts": tried_labels,
                "message": note,
            },
        })
        return payload

    if tried_labels and latest_label and latest_label != tried_labels[0]:
        log.info(
            f"[{INSTANCE_ID}] Macro agent fallback dùng báo cáo {latest_label} sau khi thử {tried_labels}"
        )

    persisted_meta = await asyncio.to_thread(
        _persist_gso_csv_asset,
        latest_report,
        latest_label,
    )

    if not persisted_meta:
        note = "Không thể lưu file CSV GSO vào thư mục dự án."
        payload = _build_agent_stub("macro", request_id, ts_iso)
        error_stats = {
            "gso_period": latest_label,
            "attempts": tried_labels,
            "csv_path": None,
            "gso_data_dir": GSO_DATA_DIR,
            "errors": [note],
        }
        payload.update({
            "notes": note,
            "raw_data": error_stats,
            "redis_json": {
                "generated_at": ts_iso,
                "status": "CSV_PERSIST_FAILED",
                "attempts": tried_labels,
                "message": note,
            },
        })
        return payload

    note = (
        f"Đã tải báo cáo GSO kỳ {persisted_meta['period_label']} và lưu CSV tại "
        f"{persisted_meta['csv_path']}."
    )

    stats = {
        "gso_period": persisted_meta["period_label"],
        "csv_path": persisted_meta["csv_path"],
        "filename": persisted_meta["filename"],
        "attachment_url": persisted_meta.get("attachment_url"),
        "saved_at": persisted_meta["saved_at"],
        "file_size_bytes": persisted_meta["file_size_bytes"],
        "attempts": tried_labels,
    }

    payload = _build_agent_stub("macro", request_id, ts_iso)
    payload.update({
        "notes": note,
        "raw_data": stats,
        "redis_json": {
            "generated_at": ts_iso,
            "status": "CSV_SAVED",
            "period": stats["gso_period"],
            "csv_path": stats["csv_path"],
            "attempts": tried_labels,
        },
    })
    return payload


async def run_tech_agent(chat_id: int, request_id: str, ts_iso: str, symbols: list[str]) -> dict:
    normalized = []
    for sym in symbols or []:
        if not sym:
            continue
        cleaned = str(sym).strip().upper()
        if not cleaned:
            continue
        if cleaned not in normalized:
            normalized.append(cleaned)

    payload = _build_agent_stub("tech", request_id, ts_iso)
    if not normalized:
        payload.update({
            "notes": "Danh mục rỗng hoặc không hợp lệ.",
            "raw_data": {"processed_symbols": 0},
            "redis_json": {
                "generated_at": ts_iso,
                "symbols": [],
                "processed_symbols": [],
                "errors": ["Không tìm thấy mã nào để xử lý."],
            },
        })
        return payload

    if len(normalized) > TECH_AGENT_MAX_SYMBOLS:
        trimmed = normalized[:TECH_AGENT_MAX_SYMBOLS]
        skipped = normalized[TECH_AGENT_MAX_SYMBOLS:]
        log.info(
            f"[{INSTANCE_ID}] [TECH] Giới hạn {TECH_AGENT_MAX_SYMBOLS} mã, bỏ qua: {skipped}"
        )
        normalized = trimmed
    else:
        skipped = []

    start_str, end_str, export_cutoff = _tech_agent_date_bounds()
    entries: dict[str, list[dict]] = {}
    stats_by_symbol: dict[str, dict] = {}
    errors: list[str] = []

    total = len(normalized)
    log.info(f"[{INSTANCE_ID}] [TECH] Chat {chat_id} request {request_id} xử lý {total} mã.")

    for batch_start in range(0, total, TECH_AGENT_BATCH_SIZE):
        batch = normalized[batch_start: batch_start + TECH_AGENT_BATCH_SIZE]
        log.info(
            f"[{INSTANCE_ID}] [TECH] Batch {batch_start // TECH_AGENT_BATCH_SIZE + 1}: {batch}"
        )
        for sym in batch:
            try:
                dataset = await asyncio.to_thread(
                    _collect_tech_indicator_dataset,
                    sym,
                    start_str,
                    end_str,
                    export_cutoff,
                )
                if dataset.get("error"):
                    raise RuntimeError(dataset["error"])
                entries[sym] = dataset.get("records", [])
                stats_by_symbol[sym] = dataset.get("stats", {})
            except VCIRateLimitError as exc:
                log.error(
                    f"[{INSTANCE_ID}] [TECH] VCI rate limit khi xử lý {sym}: {exc}. Dừng agent."
                )
                await asyncio.sleep(VCI_RATE_LIMIT_BACKOFF_SECONDS)
                raise
            except Exception as exc:
                err_msg = f"{sym}: {exc}"
                errors.append(err_msg[:200])
                log.error(f"[{INSTANCE_ID}] [TECH] {err_msg}")

        if batch_start + TECH_AGENT_BATCH_SIZE < total:
            await asyncio.sleep(TECH_AGENT_BATCH_PAUSE_SECONDS)

    date_context = {
        "history_days": TECH_AGENT_HISTORY_DAYS,
        "export_lookback_days": TECH_AGENT_EXPORT_LOOKBACK_DAYS,
        "start_date": start_str,
        "end_date": end_str,
    }

    redis_payload = {
        "generated_at": ts_iso,
        "symbols": normalized,
        "processed_symbols": list(entries.keys()),
        "errors": errors,
        "entries": entries,
        "stats": stats_by_symbol,
        "date_context": date_context,
        "skipped_symbols": skipped,
    }

    success_count = len(entries)
    note = (
        f"Đã tính chỉ báo cho {success_count}/{len(normalized)} mã. "
        f"Khoảng dữ liệu xuất: {TECH_AGENT_EXPORT_LOOKBACK_DAYS} ngày gần nhất."
    )
    if skipped:
        note += f" Bỏ qua {len(skipped)} mã do vượt giới hạn."
    if errors:
        note += f" Có {len(errors)} lỗi trong quá trình xử lý."

    payload.update({
        "notes": note,
        "raw_data": {
            "entries": entries,
            "stats": stats_by_symbol,
            "errors": errors,
            "date_context": date_context,
            "skipped_symbols": skipped,
        },
        "redis_json": redis_payload,
    })
    return payload


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _collect_indicator_metrics(record: dict | None) -> dict[str, float | None]:
    if not isinstance(record, dict):
        return {}

    aliases = {
        "close": ("Close", "close"),
        "ema20": ("EMA20", "ema20", "EMA_20"),
        "ema50": ("EMA50", "ema50", "EMA_50"),
        "ema200": ("EMA200", "ema200", "EMA_200"),
        "rsi": ("RSI", "rsi", "RSI_14"),
        "macd_line": ("MACD_Line", "MACD", "macd_line", "MACD_12_26_9"),
        "macd_signal": ("MACD_Signal", "MACDs", "macd_signal", "MACDs_12_26_9"),
        "bb_upper": ("BB_Upper", "BBU", "BBU_20_2.0"),
        "bb_lower": ("BB_Lower", "BBL", "BBL_20_2.0"),
    }

    metrics: dict[str, float | None] = {}
    for key, candidates in aliases.items():
        val = None
        for candidate in candidates:
            if candidate in record:
                val = _safe_float(record.get(candidate))
                if val is not None:
                    break
        metrics[key] = val

    metrics["date"] = record.get("Date") or record.get("time")
    return metrics


def _build_deterministic_tech_summary(
    symbol: str,
    metrics: dict[str, float | None],
    price: float | None,
    pct: float | None,
) -> str:
    segments: list[str] = []
    current_price = _safe_float(price) or metrics.get("close")

    if pct is not None:
        segments.append(f"Biến động hiện tại {pct:+.2f}%.")

    ema20 = metrics.get("ema20")
    ema50 = metrics.get("ema50")
    ema200 = metrics.get("ema200")
    rsi = metrics.get("rsi")
    bb_upper = metrics.get("bb_upper")
    bb_lower = metrics.get("bb_lower")
    macd_line = metrics.get("macd_line")
    macd_signal = metrics.get("macd_signal")

    if current_price is not None and ema20 is not None:
        diff = current_price - ema20
        relation = "trên" if diff >= 0 else "dưới"
        segments.append(f"Giá đang {relation} EMA20 {abs(diff):.2f}đ.")

    if ema20 and ema50 and ema200:
        if ema20 > ema50 > ema200:
            segments.append("Các EMA xếp tăng (20>50>200).")
        elif ema20 < ema50 < ema200:
            segments.append("Các EMA xếp giảm (20<50<200).")

    if rsi is not None:
        if rsi >= 70:
            segments.append(f"RSI {rsi:.0f} → vùng quá mua.")
        elif rsi <= 30:
            segments.append(f"RSI {rsi:.0f} → vùng quá bán.")
        else:
            segments.append(f"RSI {rsi:.0f}, trạng thái trung tính.")

    if current_price is not None and bb_upper and bb_lower:
        range_width = bb_upper - bb_lower
        if range_width > 0:
            pos = (current_price - bb_lower) / range_width
            if pos >= 0.9:
                segments.append("Giá bám dải trên Bollinger.")
            elif pos <= 0.1:
                segments.append("Giá chạm dải dưới Bollinger.")

    if macd_line is not None and macd_signal is not None:
        if macd_line > macd_signal:
            segments.append("MACD nằm trên đường tín hiệu.")
        elif macd_line < macd_signal:
            segments.append("MACD nằm dưới đường tín hiệu.")

    if not segments:
        return f"Chưa đủ dữ liệu kỹ thuật cho {symbol}."

    trimmed = segments[:3]
    return " ".join(trimmed)


def _build_tech_prompt(symbol: str, metrics: dict[str, float | None], price: float | None, pct: float | None) -> str | None:
    base_price = _safe_float(price) or metrics.get("close")
    if base_price is None:
        return None

    lines = [f"Mã: {symbol}", f"Giá hiện tại: {base_price:.2f}"]
    if pct is not None:
        lines.append(f"Biến động trong phiên: {pct:+.2f}%")

    for label, key in [
        ("EMA20", "ema20"),
        ("EMA50", "ema50"),
        ("EMA200", "ema200"),
        ("RSI", "rsi"),
        ("MACD", "macd_line"),
        ("MACD_signal", "macd_signal"),
    ]:
        val = metrics.get(key)
        if val is not None:
            lines.append(f"{label}: {val:.2f}")

    bb_upper = metrics.get("bb_upper")
    bb_lower = metrics.get("bb_lower")
    if bb_upper is not None and bb_lower is not None:
        lines.append(f"Bollinger: [{bb_lower:.2f}, {bb_upper:.2f}]")

    bullet = "\n".join(lines)
    prompt = (
        "Bạn là trợ lý phân tích kỹ thuật. Hãy nhận xét nhanh cho mã "
        f"{symbol} dựa trên dữ liệu sau:\n{bullet}\n"
        "Viết tối đa 2 câu, giọng trung lập, nêu xu hướng ngắn hạn và nhận định rủi ro hay cơ hội."
        "Không ghi tiêu hoặc giới thiệu, chỉ tập trung vào nội dung")
    return prompt


async def _generate_ai_tech_summary(
    symbol: str,
    metrics: dict[str, float | None],
    price: float | None,
    pct: float | None,
) -> str | None:
    prompt = _build_tech_prompt(symbol, metrics, price, pct)
    if not prompt:
        return None

    text = await asyncio.to_thread(
        call_gemini_safe,
        "gemini-2.5-flash-lite",
        prompt,
    )
    if not text:
        return None
    return text.strip()


async def run_biz_agent(chat_id: int, request_id: str, ts_iso: str, symbols: list[str]) -> dict:
    normalized = []
    for sym in symbols or []:
        if not sym:
            continue
        cleaned = str(sym).strip().upper()
        if not cleaned:
            continue
        if cleaned not in normalized:
            normalized.append(cleaned)

    payload = _build_agent_stub("biz", request_id, ts_iso)
    if not normalized:
        payload.update({
            "notes": "Danh mục rỗng hoặc không hợp lệ.",
            "raw_data": {"processed_symbols": 0},
            "redis_json": {
                "generated_at": ts_iso,
                "symbols": [],
                "processed_symbols": [],
                "fresh_symbols": [],
                "cached_symbols": [],
                "errors": ["Không tìm thấy mã nào để xử lý."],
            },
        })
        return payload

    fresh_symbols: list[str] = []
    errors: list[str] = []
    meta_by_symbol: dict[str, dict] = {}
    datasets_for_debug: dict[str, dict] = {}

    total = len(normalized)
    log.info(f"[{INSTANCE_ID}] [BIZ] Chat {chat_id} request {request_id} xử lý {total} mã.")

    for batch_start in range(0, total, BIZ_AGENT_BATCH_SIZE):
        batch = normalized[batch_start: batch_start + BIZ_AGENT_BATCH_SIZE]
        log.info(f"[{INSTANCE_ID}] [BIZ] Batch {batch_start // BIZ_AGENT_BATCH_SIZE + 1}: {batch}")
        for sym in batch:
            try:
                datasets = await asyncio.to_thread(_collect_finance_datasets, sym)
                if not datasets:
                    err_msg = f"{sym}: Không thu thập được dữ liệu tài chính."
                    errors.append(err_msg)
                    log.warning(f"[{INSTANCE_ID}] [BIZ] {err_msg}")
                    continue

                fresh_symbols.append(sym)
                datasets_for_debug[sym] = datasets
                preview = _build_finance_preview(datasets)
                meta_by_symbol[sym] = {
                    "status": "fresh",
                    "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "dataset_count": len(datasets),
                    "datasets": list(datasets.keys()),
                    "preview": preview,
                }
            except VCIRateLimitError as exc:
                log.error(
                    f"[{INSTANCE_ID}] [BIZ] VCI rate limit khi xử lý {sym}: {exc}. Dừng agent."
                )
                await asyncio.sleep(VCI_RATE_LIMIT_BACKOFF_SECONDS)
                raise
            except Exception as exc:
                err_msg = f"{sym}: {exc}"
                errors.append(err_msg[:200])
                log.error(f"[{INSTANCE_ID}] [BIZ] {err_msg}")

        if batch_start + BIZ_AGENT_BATCH_SIZE < total:
            await asyncio.sleep(BIZ_AGENT_BATCH_PAUSE_SECONDS)

    redis_payload = {
        "generated_at": ts_iso,
        "symbols": normalized,
        "processed_symbols": list(meta_by_symbol.keys()),
        "fresh_symbols": fresh_symbols,
        "errors": errors,
        "entries": meta_by_symbol,
        "dataset_limit": BIZ_AGENT_MAX_DATASET_PERIODS,
        "batch_size": BIZ_AGENT_BATCH_SIZE,
        "rest_seconds": BIZ_AGENT_BATCH_PAUSE_SECONDS,
    }

    payload.update({
        "notes": (
            f"Đã xử lý {len(meta_by_symbol)} mã (fresh {len(fresh_symbols)}). "
            f"Mỗi lần chạy đều thu thập trực tiếp từ Finance-VCI, giới hạn {BIZ_AGENT_MAX_DATASET_PERIODS} kỳ/bảng."
        ),
        "raw_data": {
            "fresh_symbols": fresh_symbols,
            "errors": errors,
            "entries": meta_by_symbol,
            "datasets": datasets_for_debug,
        },
        "redis_json": redis_payload,
    })
    return payload


def format_agent_bundle_message(chat_id: int, request_id: str, scope: str, bundle: dict) -> str:
    lines = [
        "🤖 *Multi-Agent Pipeline*",
        f"👤 Chat: `{chat_id}`",
        f"🆔 Request: `{request_id}`",
        f"🎯 Scope: `{scope}`",
        f"🕒 Thời gian: {bundle.get('generated_at', '—')}",
    ]

    agents = bundle.get("agents", {})
    for agent_type in AGENT_TYPES:
        if agent_type not in agents:
            continue
        agent_payload = agents[agent_type]
        redis_key = _agent_result_key(agent_type)
        lines.append("")
        lines.append(f"*{agent_type.upper()} Agent*")
        lines.append(f"• Redis key: `{redis_key}`")
        notes = agent_payload.get("notes") or "—"
        lines.append(f"• Notes: {notes}")
        insight_count = len(agent_payload.get("insights", []))
        lines.append(f"• Insights: {insight_count} items")
        debug_file = agent_payload.get("debug_file_path")
        if debug_file:
            lines.append(f"• File: `{debug_file}`")

    lines.append("")
    lines.append("🧠 *AI Summary (stub)*")
    lines.append(bundle.get("ai_summary", "TODO: Bổ sung prompt tổng hợp."))

    return "\n".join(lines)


async def handle_agent_run(payload: dict):
    chat_id = payload.get("chat_id")
    scope = (payload.get("scope") or "all").lower()
    request_id = payload.get("request_id") or str(uuid.uuid4())

    if not chat_id:
        log.warning("CMD_AGENT_RUN thiếu chat_id")
        return

    if scope not in AGENT_TYPES and scope != "all":
        scope = "all"

    target_agents: tuple[str, ...]
    if scope == "all":
        target_agents = AGENT_TYPES
    else:
        target_agents = (scope,)

    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    ts_iso = now.isoformat()

    user_watch_symbols: list[str] = []
    if any(agent in target_agents for agent in ("biz", "tech")):
        try:
            all_watch = await asyncio.to_thread(get_all_watch)
            watch_block = (all_watch or {}).get(str(chat_id)) or {}
            user_watch_symbols = watch_block.get("list") or []
        except Exception as exc:
            log.error(f"[{INSTANCE_ID}] [WATCHLIST] Lỗi lấy danh mục user {chat_id}: {exc}")
            user_watch_symbols = []

    agent_outputs = {}
    for agent_type in target_agents:
        try:
            if agent_type == "macro":
                result = await run_macro_agent(chat_id, request_id, ts_iso)
            elif agent_type == "biz":
                result = await run_biz_agent(chat_id, request_id, ts_iso, user_watch_symbols or [])
            elif agent_type == "tech":
                result = await run_tech_agent(chat_id, request_id, ts_iso, user_watch_symbols or [])
            else:
                result = _build_agent_stub(agent_type, request_id, ts_iso)
        except Exception as exc:
            log.error(f"Agent {agent_type} run error: {exc}")
            result = _build_agent_stub(agent_type, request_id, ts_iso)
            result["notes"] = f"Lỗi thực thi: {exc}"[:200]

        agent_outputs[agent_type] = result

    bundle = {
        "chat_id": chat_id,
        "request_id": request_id,
        "generated_at": ts_iso,
        "scope": scope,
        "agents": agent_outputs,
        "ai_summary": "AI summary chưa bật. Dữ liệu đang được kiểm tra thủ công.",
    }

    log.info(
        f"[{INSTANCE_ID}] Agent run hoàn tất cho chat {chat_id} scope '{scope}'."
    )

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

_JSON_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)

def _parse_ai_digest_payload(raw: Any) -> dict | None:
    """Best-effort parse to ensure AI digest payload is a dict."""
    if not raw:
        return None

    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                escaped = escape_control_chars_in_json_strings(raw)
                return json.loads(escaped)
            except json.JSONDecodeError as exc:
                log.warning("[DIGEST] JSON decode failed: %s", str(exc))
                return None

    return None


def _normalize_ai_digest_payload(payload: dict | None) -> dict | None:
    """Normalize AI digest payload to ensure required fields exist."""
    if not isinstance(payload, dict):
        return None

    def _ensure_item_list(items: Any) -> list[dict]:
        normalized = []
        if isinstance(items, list):
            iterable = items
        elif items:
            iterable = [items]
        else:
            iterable = []

        for entry in iterable:
            if isinstance(entry, dict):
                text = str(entry.get("text") or "").strip()
                link = str(entry.get("link") or "").strip()
                ticker = str(entry.get("ticker") or "").strip()
            else:
                text = str(entry).strip()
                link = ""
                ticker = ""
            if not text:
                continue
            normalized.append({
                "text": text,
                "link": link,
                "ticker": ticker,
            })
        return normalized

    comment = str(payload.get("comment") or "").strip()
    try:
        sentiment = int(payload.get("sentiment_score", 5))
    except Exception:
        sentiment = 5
    sentiment = max(1, min(10, sentiment))

    normalized_payload = {
        "headline": _ensure_item_list(payload.get("headline")),
        "macro": _ensure_item_list(payload.get("macro")),
        "corporate": _ensure_item_list(payload.get("corporate")),
        "comment": comment or "Thị trường khá yên ắng, chưa có thông tin mới.",
        "sentiment_score": sentiment,
    }
    return normalized_payload


def _build_empty_ai_digest(reason: str | None = None) -> dict:
    message = reason or "Chưa có bài viết mới nào trong 24 giờ qua."
    payload = {
        "headline": [{"text": message, "link": ""}],
        "macro": [],
        "corporate": [],
        "comment": "Thị trường tương đối bình lặng. Hãy tập trung theo dõi danh mục của bạn và các tín hiệu kỹ thuật.",
        "sentiment_score": 5,
    }
    return payload

def escape_control_chars_in_json_strings(text: str) -> str:
    """Escape newline/tab chars that Gemini đôi khi trả về trần trong chuỗi JSON."""
    if not text:
        return text

    def _escape_fragment(match: re.Match) -> str:
        fragment = match.group(0)
        if not any(ord(ch) < 0x20 for ch in fragment):
            return fragment

        chars = [fragment[0]]  # opening quote
        i = 1
        end = len(fragment) - 1
        while i < end:
            ch = fragment[i]
            if ch == '\\':
                # Giữ nguyên escape sequence hiện hữu
                chars.append(ch)
                i += 1
                if i < end:
                    chars.append(fragment[i])
                i += 1
                continue

            code_point = ord(ch)
            if ch == '\n':
                chars.append('\\n')
            elif ch == '\r':
                chars.append('\\r')
            elif ch == '\t':
                chars.append('\\t')
            elif code_point < 0x20:
                chars.append(f"\\u{code_point:04x}")
            else:
                chars.append(ch)
            i += 1

        chars.append(fragment[-1])  # closing quote
        return ''.join(chars)

    return _JSON_STRING_RE.sub(_escape_fragment, text)

def remove_markdown(text):
    """
    Xóa các ký tự Markdown (**...**) để văn bản sạch hơn khi lưu lịch sử.
    """
    if not text: return ""
    # Xóa **, __, `
    return text.replace("**", "").replace("__", "").replace("`", "").strip()

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

async def process_ask_ai(chat_id, question, loading_msg_id=None):
    """Xử lý câu hỏi CSKH bằng Gemini Flash Lite với bộ nhớ hội thoại."""
    log.info(f"[{INSTANCE_ID}] 🤖 AI CSKH: {chat_id} - '{question}'")

    try:
        kb = default_ai_reply_markup()

        # 1. Lấy lịch sử từ Redis (Context Memory)
        history_key = f"ai_history:{chat_id}"
        history_context = ""
        if r_client:
            items = r_client.lrange(history_key, -10, -1)
            if items:
                history_context = "\n".join(items)

        # 2. Tạo Prompt (System + History + User)
        full_prompt = f"""{BOT_KNOWLEDGE_BASE}

---
LỊCH SỬ HỘI THOẠI (Context):
{history_context}

User: {question}
Bot:"""

        # 3. Gọi Gemini
        answer, usage = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-flash-lite",
            contents=full_prompt,
            return_usage=True,
        )

        if not answer:
            answer = "😅 Xin lỗi, hiện tại mình đang bị quá tải. Bạn vui lòng thử lại sau nhé."

        # [LOG ADMIN]
        if usage and (chat_id == ADMIN_ID):
            try:
                in_tok = usage.prompt_token_count
                out_tok = usage.candidates_token_count
                total = usage.total_token_count
                answer += f"\n\n`[DEBUG] In: {in_tok} | Out: {out_tok} | Total: {total}`"
            except Exception:
                pass

        # 4. Lưu hội thoại mới vào Redis (để AI nhớ cho lần sau)
        if r_client:
            r_client.rpush(history_key, f"User: {question}")
            clean_answer = answer.split("\n\n`[DEBUG]")[0]
            history_text = remove_markdown(clean_answer)
            r_client.rpush(history_key, f"Bot: {history_text}")
            r_client.ltrim(history_key, -20, -1)
            r_client.expire(history_key, 86400)

        # 5. Gửi kết quả về Gateway
        push_telegram_msg(
            chat_id=chat_id,
            text=answer,
            reply_markup=kb,
            edit_id=loading_msg_id,
        )

    except Exception as e:
        log.error(f"AI CSKH Error: {e}")
        push_telegram_msg(
            chat_id=chat_id,
            text="⚠️ Lỗi hệ thống AI. Vui lòng thử lại sau.",
            edit_id=loading_msg_id,
            reply_markup=default_ai_reply_markup(),
        )
async def worker_inbound_loop():
    """[WORKER] Lắng nghe lệnh từ Gateway (ví dụ: User gõ /report)."""
    global r_client
    log.info(f"[{INSTANCE_ID}] 🎧 Worker lắng nghe lệnh từ '{REDIS_CHANNEL_INBOUND}'...")

    while True:
        pubsub = None
        try:
            client = ensure_redis_client()
            if not client:
                log.error(f"[{INSTANCE_ID}] ❌ Không thể kết nối Redis inbound. Chờ 5s rồi thử lại...")
                await asyncio.sleep(5)
                continue

            pubsub = client.pubsub()
            pubsub.subscribe(REDIS_CHANNEL_INBOUND)

            while True:
                try:
                    message = pubsub.get_message(ignore_subscribe_messages=True)
                except Exception as err:

                    break

                if message:
                    try:
                        payload = json.loads(message['data'])
                        cmd = payload.get('cmd')

                        if cmd == "GEN_REPORT":
                            chat_id = payload.get('chat_id')
                            symbols = payload.get('symbols')
                            loading_id = payload.get('loading_msg_id')
                            async def _run_report_task():
                                try:
                                    await process_report_for_user(chat_id, symbols, loading_msg_id=loading_id)
                                finally:
                                    push_task_unlock_signal(chat_id)

                            asyncio.create_task(_run_report_task())

                        elif cmd == "RUN_WEEKLY_NOW":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Weekly từ {admin_id}")
                            asyncio.create_task(execute_weekly_batch(requester_id=admin_id))

                        elif cmd == "RUN_NIGHTLY_VALUATION":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Nightly Valuation từ {admin_id}")
                            asyncio.create_task(job_nightly_valuation())

                        elif cmd == "RUN_DAILY_DIGEST":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Daily Digest từ {admin_id}")
                            asyncio.create_task(job_daily_digest())

                        elif cmd == "RUN_EOD_SUMMARY":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run EOD Summary từ {admin_id}")
                            asyncio.create_task(job_eod_summary())

                        elif cmd == "GEN_INFO":
                            chat_id = payload.get('chat_id')
                            symbol = payload.get('symbol')
                            loading_id = payload.get('loading_msg_id')
                            async def _run_info_task():
                                try:
                                    await process_profile_for_user(chat_id, symbol, loading_msg_id=loading_id)
                                finally:
                                    push_task_unlock_signal(chat_id)

                            asyncio.create_task(_run_info_task())

                        elif cmd == "GEN_SCREENER":
                            chat_id = payload.get('chat_id')
                            loading_id = payload.get('loading_msg_id')
                            asyncio.create_task(process_screener_view(chat_id, loading_id))

                        elif cmd == "FORCE_SCREENER":
                            admin_id = payload.get('admin_id')
                            asyncio.create_task(process_force_update_screener(admin_id))

                        elif cmd == "RUN_MONTHLY_INSIGHT":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Monthly Insight")
                            asyncio.create_task(job_monthly_cskh_report())

                        elif cmd == "CMD_AGENT_RUN":
                            asyncio.create_task(handle_agent_run(payload))

                        elif cmd == "CMD_MANUAL_ALERT":
                            asyncio.create_task(handle_manual_alert(payload))

                        elif cmd == "CMD_ASK_AI":
                            chat_id = payload.get('chat_id')
                            question = payload.get('question')
                            loading_id = payload.get('loading_msg_id')
                            asyncio.create_task(process_ask_ai(chat_id, question, loading_id))

                    except Exception as e:
                        log.error(f"Inbound Error: {e}")

                await asyncio.sleep(0.1)

        except Exception as e:
            log.error(f"Worker Inbound Crash: {e}")
            r_client = None
            await asyncio.sleep(5)
        finally:
            if pubsub:
                try:
                    pubsub.close()
                except Exception:
                    pass


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
def load_company_keywords_from_json(path: str = "sectors.json") -> dict[str, list[str]]:
    """
    Đọc danh sách công ty từ file JSON (format: {SYM: {sector: ..., name: ...}}),
    trả về dict[symbol] = [symbol, tên đầy đủ, tên rút gọn].
    """
    mapping: dict[str, list[str]] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        for sym, info in data.items():
            sym = sym.strip().upper()
            if isinstance(info, dict):
                name = info.get("name", "")
            else:
                name = "" 
            
            if not sym: continue
            
            keywords = {sym}
            if name:
                name = name.strip()
                keywords.add(name)
                
                # Tạo tên rút gọn
                short = re.sub(
                    r"\b(Công ty|Cổ phần|Tập đoàn|TNHH|Ngân hàng|Thương mại|Đầu tư|Phát triển|Kỹ thuật|Tài chính)\b",
                    "",
                    name,
                    flags=re.IGNORECASE,
                )
                short = re.sub(r"\s+", " ", short).strip()
                if len(short) > 2:
                    keywords.add(short)

            mapping[sym] = [k for k in keywords if len(k) > 2]

        log.info(f"[{INSTANCE_ID}][COMPANY] Đã load {len(mapping)} công ty từ {path}.")
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][COMPANY] Lỗi đọc JSON {path}: {e}")
    return mapping

# Map symbol -> list keyword (mã + tên doanh nghiệp)
COMPANY_KEYWORDS = load_company_keywords_from_json("sectors.json")

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
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                raise e
            # Bắt cả SystemExit nếu VCI rate limit
            _vci_blocked_date = today_date
            log.warning(f"[{INSTANCE_ID}] VCI Fetch Error (Rate Limit? {type(e).__name__}). Switch to Fallback.")

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

async def _dispatch_symbol_tech_followup(chat_id: int, symbol_contexts: list[dict[str, Any]]):
    unique_symbols: list[str] = []
    context_map: dict[str, dict[str, Any]] = {}
    for ctx in symbol_contexts or []:
        symbol = str(ctx.get("symbol") or "").strip().upper()
        if not symbol or symbol in context_map:
            continue
        context_map[symbol] = {
            "price": ctx.get("price"),
            "pct": ctx.get("pct"),
        }
        unique_symbols.append(symbol)

    if not unique_symbols:
        return

    vn_tz = pytz.timezone(TIMEZONE)
    ts_iso = datetime.datetime.now(vn_tz).isoformat()
    request_id = str(uuid.uuid4())

    cached_texts: dict[str, str] = {}
    missing_symbols: list[str] = []
    for symbol in unique_symbols:
        cached = _get_cached_tech_summary(symbol)
        if cached:
            cached_texts[symbol] = cached
        else:
            missing_symbols.append(symbol)

    entries: dict[str, list[dict]] = {}
    if missing_symbols:
        try:
            agent_payload = await run_tech_agent(chat_id, request_id, ts_iso, missing_symbols)
            raw_block = (agent_payload.get("raw_data") or {}) if agent_payload else {}
            entries = raw_block.get("entries") or {}
        except Exception as exc:
            log.error(f"[{INSTANCE_ID}] ⚠️ Tech follow-up error: {exc}")

    summaries: list[str] = []
    for symbol in unique_symbols:
        cache_hit = cached_texts.get(symbol)
        if cache_hit:
            summaries.append(f"*{symbol}*: {cache_hit}")
            continue

        record_list = entries.get(symbol) or []
        latest_record = record_list[-1] if record_list else None
        metrics = _collect_indicator_metrics(latest_record)
        price = context_map[symbol]["price"]
        pct = context_map[symbol]["pct"]
        deterministic = _build_deterministic_tech_summary(symbol, metrics, price, pct)

        ai_summary = None
        try:
            ai_summary = await _generate_ai_tech_summary(symbol, metrics, price, pct)
        except Exception as exc:
            log.warning(f"[{INSTANCE_ID}] ⚠️ AI tech summary failed for {symbol}: {exc}")

        final_line = ai_summary or deterministic
        if final_line:
            _set_cached_tech_summary(symbol, final_line)
        summaries.append(f"*{symbol}*: {final_line}")

    if not summaries:
        return
    body_lines = [f"🤖 *Soi Chart nhanh {symbol}*", *summaries]
    push_telegram_msg(
        chat_id=chat_id,
        text="\n".join(body_lines),
        msg_type="STOCK_ALERT_TECH",
    )


async def handle_manual_alert(payload: dict):
    chat_id = payload.get("chat_id")
    symbols = payload.get("symbols") or []

    if not chat_id:
        log.warning("CMD_MANUAL_ALERT thiếu chat_id")
        return

    normalized: list[str] = []
    for sym in symbols:
        cleaned = str(sym or "").strip().upper()
        if not cleaned:
            continue
        if cleaned not in normalized:
            normalized.append(cleaned)

    if not normalized:
        push_telegram_msg(chat_id, "⚠️ Vui lòng cung cấp mã hợp lệ (VD: /alert HPG).", msg_type="STOCK_ALERT")
        return

    try:
        data = await fetch_data_smart(normalized)
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] Manual alert fetch error: {exc}")
        push_telegram_msg(chat_id, "⚠️ Không lấy được dữ liệu giá. Vui lòng thử lại sau.", msg_type="STOCK_ALERT")
        return

    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    messages: list[str] = []
    buttons = {"inline_keyboard": []}
    tech_followups: list[dict[str, Any]] = []

    for sym in normalized:
        quote = (data or {}).get(sym)
        if not quote:
            continue

        try:
            price = float(quote.get("price"))
            pct = float(quote.get("pct"))
        except (TypeError, ValueError):
            continue

        icon = "🟢" if pct >= 0 else "🔴"
        fun_line = random.choice(ALERT_FUN_LINES_UP if pct >= 0 else ALERT_FUN_LINES_DOWN)
        msg = (
            f"{icon} *{sym} {'tăng' if pct >= 0 else 'giảm'} {pct:+.2f}%*\n"
            f"Giá: {price:,.0f}\n_{fun_line}_"
        )
        messages.append(msg)

        chart_url = f"{BASE_URL}/chart/{sym}"
        buttons["inline_keyboard"].append([
            {"text": f"📊 Soi Chart {sym}", "web_app": {"url": chart_url}}
        ])

        tech_followups.append({"symbol": sym, "price": price, "pct": pct})

    if not messages:
        push_telegram_msg(chat_id, "⚠️ Không tìm thấy dữ liệu hợp lệ cho các mã vừa nhập.", msg_type="STOCK_ALERT")
        return

    body = "\n".join(messages)
    push_telegram_msg(
        chat_id=chat_id,
        text=body,
        reply_markup=buttons if buttons["inline_keyboard"] else None,
        msg_type="STOCK_ALERT",
    )

    if tech_followups:
        asyncio.create_task(_dispatch_symbol_tech_followup(chat_id, tech_followups))


async def alert_loop():
    global ALERT_STATE, _stock_alert_disabled_cache
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Alert Loop...")
    
    # Load danh sách chặn alert lần đầu
    _stock_alert_disabled_cache = await asyncio.to_thread(get_users_with_stock_alert_off)

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
                tech_followups: list[dict[str, Any]] = []
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
                        fun_line = random.choice(ALERT_FUN_LINES_UP if pct >= 0 else ALERT_FUN_LINES_DOWN)
                        
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

                        tech_followups.append({
                            "symbol": sym_u,
                            "price": price,
                            "pct": pct,
                        })

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

                    if tech_followups:
                        asyncio.create_task(_dispatch_symbol_tech_followup(chat_id, tech_followups))

        except Exception as e:
            log.error(f"Alert Loop Error: {e}")

        await asyncio.sleep(TICKER_INTERVAL_SECONDS)

# =================================
# MARKET MONITOR LOOP (Unified)
# =================================

def _market_reset_if_new_day(now):
    """Reset anchor đầu ngày cho tất cả Market Monitors"""
    global _market_data
    today = now.date()
    
    for sym, data in _market_data.items():
        if data["date"] != today:
            data["date"] = today
            data["anchor"] = None
            data["ref"] = None
            log.info(f"[MARKET] New day: {today}. Reset anchor for {sym}.")

def _market_clear_after_close():
    global _market_data
    cleared = False
    for sym, data in _market_data.items():
        if data["anchor"] is not None:
            data["anchor"] = None
            cleared = True
    if cleared:
        log.info("[MARKET] Close session. Clear all anchors.")

async def _market_process_tick(symbol: str, price: float):
    """
    Xử lý logic so sánh giá chung cho VN30F1M, VNINDEX, VN30.
    """
    global _market_data
    
    config = MARKET_MONITORS.get(symbol)
    if not config: return None

    state = _market_data.get(symbol)
    if not state: return None

    anchor = state["anchor"]
    ref_price = state["ref"]

    if anchor is None or ref_price is None:
        return None

    delta_trigger = float(price) - float(anchor)
    threshold = config["threshold"]
    
    # Trigger nếu biến động >= threshold
    if abs(delta_trigger) >= threshold:
        delta_display = float(price) - float(ref_price)
        direction = "tăng" if delta_display > 0 else "giảm"
        icon = "🟢" if delta_display > 0 else "🔴"
        trend_icon = "🚀" if delta_display > 0 else "📉"
        now_str = datetime.datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")

        text = (
            f"{icon} *{symbol} {direction} {abs(delta_display):.1f} điểm*\n"
            f"Giá hiện tại: *{float(price):.1f}*\n"
            f"(So với TC: {ref_price:.1f})\n"
            f"{trend_icon} _Cập nhật lúc {now_str}_"
        )
        
        # Cập nhật mốc anchor mới
        state["anchor"] = float(price)
        log.info(f"[{symbol}] 🔔 Trigger! {price} (Delta: {delta_trigger})")
        return text
    
    return None

async def market_monitor_fetcher_loop():
    """
    Loop lấy giá chung cho VN30F1M, VNINDEX, VN30.
    Chạy chu kỳ 5s (nhanh nhất). Các mã 10s sẽ được check time nếu cần, 
    nhưng để đơn giản và responsive, ta fetch tất cả mỗi 5s.
    """
    global _market_data
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Market Monitor Fetcher (Unified)...")

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
            
            async def _fetch_one(symbol):
                # 1. Giá khớp (Live 1m) - Giữ nguyên
                q = Quote(symbol=symbol, source='VCI')
                df_now = await asyncio.to_thread(q.history, start=today_str, end=today_str, interval='1m')
                p_now = float(df_now.iloc[-1]['close']) if df_now is not None and not df_now.empty else None
                
                # 2. Giá tham chiếu (Ref) - LOGIC MỚI [FIX]
                p_ref = None
                state = _market_data.get(symbol)
                
                # Chỉ lấy lại Ref nếu trong state chưa có
                if state and state["ref"] is None:
                    # A. Thử lấy từ Board (VCI thường có ref chuẩn cho Phái sinh)
                    if symbol == "VN30F1M" and stock_trading:
                        try:
                            row = stock_trading.price_board([symbol]).iloc[0]
                            val = row.get(('listing', 'ref_price')) or row.get('ref_price')
                            if val: p_ref = float(val)
                        except: pass
                    
                    # B. Nếu chưa có, lấy từ History nhưng check ngày thông minh
                    if p_ref is None:
                        # Dùng TCBS cho Index vì ổn định hơn VCI, các mã khác dùng VCI
                        src = 'TCBS' if symbol in ['VNINDEX', 'VN30'] else 'VCI'
                        q_hist = Quote(symbol=symbol, source=src)
                        
                        start_prev = (now - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
                        df_daily = await asyncio.to_thread(q_hist.history, start=start_prev, end=today_str, interval='1D')
                        
                        if df_daily is not None and not df_daily.empty:
                            # Chuẩn hóa cột ngày tháng
                            if 'time' in df_daily.columns:
                                df_daily['dt'] = pd.to_datetime(df_daily['time']).dt.date
                            elif 'tradingDate' in df_daily.columns:
                                df_daily['dt'] = pd.to_datetime(df_daily['tradingDate']).dt.date
                            else:
                                # Fallback nếu không tìm thấy cột ngày
                                df_daily['dt'] = None

                            last_row = df_daily.iloc[-1]
                            last_close = float(last_row['close'])
                            last_date = last_row['dt']
                            today_date = now.date()
                            
                            # LOGIC QUAN TRỌNG:
                            if last_date == today_date:
                                # Nếu dòng cuối là hôm nay -> Ref là dòng áp chót (Hôm qua)
                                if len(df_daily) >= 2:
                                    p_ref = float(df_daily.iloc[-2]['close'])
                                else:
                                    # Trường hợp dị: Mới lên sàn hoặc dữ liệu lỗi chỉ có 1 dòng hôm nay
                                    # Fallback tạm bằng giá open hoặc close hôm nay
                                    p_ref = float(last_row.get('open', last_close))
                            else:
                                # Nếu dòng cuối KHÔNG PHẢI hôm nay (tức là dữ liệu mới nhất là hôm qua)
                                # -> Ref chính là dòng cuối
                                p_ref = last_close

                return symbol, p_now, p_ref
            # Chạy song song tất cả monitors
            tasks = [_fetch_one(sym) for sym in MARKET_MONITORS.keys()]
            results = await asyncio.gather(*tasks)

            for sym, p_now, p_ref in results:
                state = _market_data.get(sym)
                if not state: continue

                if p_now:
                    state["price"] = p_now
                    
                    if state["ref"] is None and p_ref:
                        state["ref"] = p_ref
                        # Init anchor bằng Ref nếu chưa có
                        if state["anchor"] is None: state["anchor"] = p_ref

        except Exception as e:
            log.error(f"Market Fetch Error: {e}")

        await asyncio.sleep(5) # Chu kỳ chung 5s

async def market_monitor_alert_loop():
    """
    Loop kiểm tra và bắn tin cảnh báo chung.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Market Monitor Alert Loop...")

    while True:
        now = datetime.datetime.now(vn_tz)
        _market_reset_if_new_day(now)

        if not get_bot_active() or not in_session_vietnam():
            _market_clear_after_close()
            await asyncio.sleep(60)
            continue

        try:
            # Duyệt qua từng mã trong config
            for symbol, config in MARKET_MONITORS.items():
                state = _market_data.get(symbol)
                if not state or state["price"] is None: continue

                # 1. Xử lý logic
                alert_text = await _market_process_tick(symbol, float(state["price"]))

                # 2. Nếu có biến động -> Gửi tin (Broadcast)
                if alert_text:
                    # Lấy danh sách user bật setting tương ứng
                    get_users = config["get_users_func"]
                    msg_type = config["msg_type"]
                    
                    user_map = await asyncio.to_thread(get_users)
                    count = 0
                    
                    for chat_id, enabled in user_map.items():
                        if enabled:
                            push_telegram_msg(chat_id, alert_text, msg_type=msg_type)
                            count += 1
                    
                    log.info(f"[{symbol}] Pushed alert to {count} users.")

        except Exception as e:
            log.error(f"Market Alert Error: {e}")

        await asyncio.sleep(5) # Chu kỳ chung 5s

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

# ===============================================================
# COMPREHENSIVE MARKET DATA TASK (ĐỊNH GIÁ + HIỆU SUẤT NGÀNH)
# ===============================================================

def load_symbol_sector_map(path="sectors.json") -> dict[str, str]:
    """
    Đọc file sectors.json để lấy mapping {Mã: Ngành}.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = {}
        for sym, info in data.items():
            if isinstance(info, dict) and "sector" in info:
                mapping[sym.upper()] = info["sector"]
        return mapping
    except Exception as e:
        log.error(f"[{INSTANCE_ID}] Lỗi load sectors.json: {e}")
        return {}

async def summarize_daily_news_with_ai(news_list):
    """
    Tóm tắt tin tức bằng AI (Gemini).
    Input: List of dict {title, link, source}
    Output: Dict {headline: [{text, link}], comment: str}
    """
    if not news_list: return None

    # Chuẩn bị prompt
    news_text = ""
    for i, item in enumerate(news_list[:40]): # Limit 40 tin để không quá dài
        news_text += f"- [{item['source']}] {item['title']}\n"

    prompt = f"""
Bạn là trợ lý tài chính thông minh. Hãy đọc danh sách tin tức chứng khoán Việt Nam dưới đây và thực hiện 2 nhiệm vụ:

1. **TIÊU ĐIỂM**: Chọn ra 3-5 tin quan trọng nhất, có tác động lớn đến thị trường hoặc các mã cổ phiếu lớn. Viết lại ngắn gọn (dưới 15 từ/tin).
2. **NHẬN ĐỊNH**: Viết một đoạn bình luận ngắn (dưới 50 từ) tổng hợp tâm lý thị trường dựa trên các tin này (Tích cực/Tiêu cực/Thận trọng...).

DANH SÁCH TIN:
{news_text}

YÊU CẦU OUTPUT (JSON Thuần):
{{
  "headline": [
    {{"text": "Nội dung tin 1...", "link": ""}}, 
    {{"text": "Nội dung tin 2...", "link": ""}}
  ],
  "comment": "Nhận định thị trường..."
}}
Lưu ý: Field "link" để trống cũng được vì khó map lại chính xác, hoặc nếu bạn tự tin thì điền link gốc.
"""
    try:
        # Gọi Gemini (Dùng Flash cho nhanh và rẻ)
        json_text = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-pro",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        if not json_text:
            return None

        cleaned = extract_json_from_text(json_text)
        parsed = _parse_ai_digest_payload(cleaned)
        if parsed is None:
            log.warning("[DIGEST] AI summary JSON parse failed. Raw: %s", str(cleaned)[:200])
            return None

        normalized = _normalize_ai_digest_payload(parsed)
        if not normalized:
            log.warning("[DIGEST] AI summary missing required fields. Payload: %s", str(parsed)[:200])
        return normalized
    except Exception as e:
        log.error(f"Summarize News Error: {e}")
        return None

def _normalize_price_value(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 500:
        value *= 1000.0
    return value


def _extract_price_from_screener_row(row: pd.Series) -> float | None:
    candidates = ("close", "price", "price_near_realtime")
    for key in candidates:
        if key not in row:
            continue
        price = _normalize_price_value(row.get(key))
        if price:
            return price
    return None


async def calculate_market_comprehensive_data():
    """
    TÁC VỤ NẶNG (Nightly):
    1. Tính P/E, P/B trung bình 5 năm (Mean Reversion).
    2. Tính hiệu suất giá (12 tuần, 6 tháng).
    3. Tổng hợp chỉ số ngành (Sector Performance).
    4. Lưu tất cả vào Redis để WebApp/Bot dùng chung.
    """
    log.info(f"[{INSTANCE_ID}] 🏗️ Bắt đầu Job tổng hợp dữ liệu thị trường (Valuation + Performance)...")
    
    try:
        # 1. Chuẩn bị dữ liệu đầu vào
        sector_map = await asyncio.to_thread(load_symbol_sector_map)
        
        # Lấy danh sách mã từ Screener (Lọc thanh khoản & Vốn hóa)
        screener = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
        MIN_MARKET_CAP = 5000 
        MIN_TRADING_VAL = 50 # [UPDATED] Tăng điều kiện thanh khoản lên 50 tỷ
        screener['market_cap'] = pd.to_numeric(screener['market_cap'], errors='coerce').fillna(0)
        liq_col = 'total_trading_value' if 'total_trading_value' in screener.columns else 'avg_trading_value_20d'
        screener[liq_col] = pd.to_numeric(screener[liq_col], errors='coerce').fillna(0)

        valid_df = screener[
            (screener['market_cap'] >= MIN_MARKET_CAP) & 
            (screener[liq_col] >= MIN_TRADING_VAL)
        ]
        valid_tickers = valid_df['ticker'].tolist()
        log.info(f"[{INSTANCE_ID}] Danh sách cần xử lý: {len(valid_tickers)} mã.")

        price_hints: dict[str, float] = {}
        for _, row in valid_df.iterrows():
            sym = str(row['ticker']).strip().upper()
            hint = _extract_price_from_screener_row(row)
            if sym and hint:
                price_hints[sym] = hint

        # Cấu trúc dữ liệu lưu Redis
        # {
        #    "stocks": { "HPG": { "pe_avg":..., "change_6m":..., "sector":... }, ... },
        #    "sectors": { "Thép": { "change_6m":..., "count":... }, ... },
        #    "updated_at": "..."
        # }
        stocks_data = {}
        sector_accumulators = {} # { "Thép": {"sum_12w": 0, "sum_6m": 0, "count": 0} }

        consecutive_errors = 0
        manual_alerts: list[str] = []
        
        # 2. Loop xử lý từng mã (Batching)
        BATCH_SIZE = 5
        BATCH_SLEEP = 60

        for i, sym in enumerate(valid_tickers):
            # Log progress
            log.info(f"[{INSTANCE_ID}] Processing {i+1}/{len(valid_tickers)}: {sym}")

            # Rate Limit (Batching)
            if i > 0 and i % BATCH_SIZE == 0:
                log.info(f"[{INSTANCE_ID}] 💤 Đã xong batch {BATCH_SIZE} mã. Nghỉ {BATCH_SLEEP}s để hồi API...")
                await asyncio.sleep(BATCH_SLEEP)

            if consecutive_errors > 5:
                log.warning(f"[{INSTANCE_ID}] ⚠️ Bị chặn liên tục. Ngủ 120s...")
                await asyncio.sleep(120)
                consecutive_errors = 0

            try:
                # --- A. Fetch Dữ liệu (Chạy song song Ratio & History) ---
                async def _fetch_ratio():
                    return await asyncio.to_thread(lambda: Finance(symbol=sym, source='VCI').ratio(period='year', lang='vi'))
                
                async def _fetch_history():
                    # Lấy 190 ngày để đảm bảo đủ 6 tháng (khoảng 180 ngày)
                    end_d = datetime.datetime.now()
                    start_d = end_d - datetime.timedelta(days=190)
                    q = Quote(symbol=sym, source='VCI')
                    return await asyncio.to_thread(q.history, start=start_d.strftime('%Y-%m-%d'), end=end_d.strftime('%Y-%m-%d'), interval='1D')

                # Chạy song song 2 request để tiết kiệm thời gian
                fin_df, hist_df = await asyncio.gather(_fetch_ratio(), _fetch_history())
                
                # --- B. Xử lý Valuation (P/E, P/B Avg) ---
                val_stats = {}
                if fin_df is not None and not fin_df.empty:
                    fin_df = _clean_vnstock_columns(fin_df)
                    df_5y = fin_df.head(5)
                    pe_s = pd.to_numeric(df_5y.get('pe', []), errors='coerce')
                    pb_s = pd.to_numeric(df_5y.get('pb', []), errors='coerce')
                    pe_s = pe_s[pe_s > 0]
                    pb_s = pb_s[pb_s > 0]
                    
                    if len(pe_s) >= 3: val_stats['pe_avg'] = pe_s.mean()
                    if len(pb_s) >= 3: val_stats['pb_avg'] = pb_s.mean()

                # --- C. Xử lý Performance (12W, 6M) ---
                perf_stats = {}
                if hist_df is not None and not hist_df.empty:
                    # Chuẩn hóa cột
                    hist_df.columns = hist_df.columns.str.lower().str.strip()
                    if 'time' in hist_df.columns: hist_df['time'] = pd.to_datetime(hist_df['time'])
                    hist_df = hist_df.sort_values('time')
                    
                    closes = pd.to_numeric(hist_df['close'], errors='coerce')
                    dates = hist_df['time'].tolist()
                    
                    if len(closes) > 0:
                        price_now = closes.iloc[-1]
                        date_now = dates[-1]
                        
                        # Hàm tìm giá tại thời điểm T - days
                        def _get_change(days_back):
                            target_date = date_now - datetime.timedelta(days=days_back)
                            # Tìm ngày gần nhất trong quá khứ (<= target_date)
                            # Vì list đã sort, ta tìm ngược từ dưới lên
                            idx = -1
                            for k in range(len(dates)-1, -1, -1):
                                if dates[k] <= target_date:
                                    idx = k
                                    break
                            
                            if idx != -1 and closes.iloc[idx] > 0:
                                p_old = closes.iloc[idx]
                                return ((price_now - p_old) / p_old) * 100
                            return None

                        perf_stats['change_12w'] = _get_change(84)  # 12 tuần ~ 84 ngày
                        perf_stats['change_6m'] = _get_change(180) # 6 tháng ~ 180 ngày

                manual_payload: dict[str, Any] = {}
                try:
                    manual_result = await asyncio.to_thread(
                        fetch_manual_pe_pb,
                        sym,
                        use_cache=True,
                        price=price_hints.get(sym),
                    )
                except Exception as exc:
                    log.warning(f"[{INSTANCE_ID}] Manual valuation fatal for {sym}: {exc}")
                    manual_result = None
                if manual_result:
                    manual_fields = {
                        "pe_manual": manual_result.pe,
                        "pb_manual": manual_result.pb,
                        "manual_price": manual_result.price,
                        "manual_eps_ttm": manual_result.eps_ttm,
                        "manual_bvps": manual_result.bvps,
                        "manual_updated_at": manual_result.computed_at,
                    }
                    for key, value in manual_fields.items():
                        if value is not None:
                            manual_payload[key] = value
                    if manual_result.error:
                        manual_payload["manual_error"] = manual_result.error
                    if manual_result.needs_admin_alert and manual_result.error:
                        manual_alerts.append(f"{sym}: {manual_result.error}")

                # --- D. Tổng hợp ---
                if val_stats or perf_stats or manual_payload:
                    sector_name = sector_map.get(sym, "Khác")
                    
                    item_data = {
                        "sector": sector_name,
                        **val_stats,
                        **perf_stats,
                        **manual_payload,
                    }
                    stocks_data[sym] = item_data
                    
                    # Cộng dồn cho Sector (Chỉ tính nếu có dữ liệu)
                    if sector_name != "Khác":
                        if sector_name not in sector_accumulators:
                            sector_accumulators[sector_name] = {"sum_12w": 0.0, "cnt_12w": 0, "sum_6m": 0.0, "cnt_6m": 0}
                        
                        acc = sector_accumulators[sector_name]
                        
                        if perf_stats.get('change_12w') is not None:
                            acc['sum_12w'] += perf_stats['change_12w']
                            acc['cnt_12w'] += 1
                            
                        if perf_stats.get('change_6m') is not None:
                            acc['sum_6m'] += perf_stats['change_6m']
                            acc['cnt_6m'] += 1

                    consecutive_errors = 0
                
                # Delay nhẹ
                await asyncio.sleep(2.0)

            except BaseException as e:
                # [FIX] Check cancellation first
                if isinstance(e, asyncio.CancelledError):
                    raise e

                # Bắt cả SystemExit do vnstock raise khi bị Rate Limit
                consecutive_errors += 1
                err_str = str(e)
                
                # Check SystemExit explicitly
                is_system_exit = isinstance(e, SystemExit) or type(e).__name__ == 'SystemExit'
                
                if "Rate limit exceeded" in err_str or is_system_exit:
                    log.warning(f"[{INSTANCE_ID}] ⚠️ Rate Limit Hit ({sym}) - {type(e).__name__}. Ngủ 60s...")
                    await asyncio.sleep(60.0)
                else:
                    log.warning(f"Lỗi xử lý {sym}: {type(e).__name__} - {e}")
                    await asyncio.sleep(2.0)

        # 3. Tính chỉ số ngành (Trung bình cộng)
        sectors_final = {}
        for sec_name, acc in sector_accumulators.items():
            avg_12w = (acc['sum_12w'] / acc['cnt_12w']) if acc['cnt_12w'] > 0 else None
            avg_6m = (acc['sum_6m'] / acc['cnt_6m']) if acc['cnt_6m'] > 0 else None
            
            sectors_final[sec_name] = {
                "change_12w": avg_12w,
                "change_6m": avg_6m,
                "count": max(acc['cnt_12w'], acc['cnt_6m'])
            }

        # --- [NEW] Thêm VNINDEX vào danh sách Sector ---
        try:
            log.info(f"[{INSTANCE_ID}] Đang lấy dữ liệu VNINDEX...")
            end_d = datetime.datetime.now()
            start_d = end_d - datetime.timedelta(days=190)
            
            # Hàm lấy history VNINDEX
            def _get_vnindex_hist():
                q = Quote(symbol='VNINDEX', source='VCI')
                return q.history(start=start_d.strftime('%Y-%m-%d'), end=end_d.strftime('%Y-%m-%d'), interval='1D')

            vnindex_df = await asyncio.to_thread(_get_vnindex_hist)
            
            if vnindex_df is not None and not vnindex_df.empty:
                vnindex_df.columns = vnindex_df.columns.str.lower().str.strip()
                if 'time' in vnindex_df.columns: vnindex_df['time'] = pd.to_datetime(vnindex_df['time'])
                vnindex_df = vnindex_df.sort_values('time')
                
                closes = pd.to_numeric(vnindex_df['close'], errors='coerce')
                dates = vnindex_df['time'].tolist()
                
                if len(closes) > 0:
                    price_now = closes.iloc[-1]
                    date_now = dates[-1]
                    
                    def _get_change_idx(days_back):
                        target_date = date_now - datetime.timedelta(days=days_back)
                        idx = -1
                        for k in range(len(dates)-1, -1, -1):
                            if dates[k] <= target_date:
                                idx = k
                                break
                        if idx != -1 and closes.iloc[idx] > 0:
                            p_old = closes.iloc[idx]
                            return ((price_now - p_old) / p_old) * 100
                        return None

                    vn_12w = _get_change_idx(84)
                    vn_6m = _get_change_idx(180)
                    
                    sectors_final['VNINDEX'] = {
                        "change_12w": vn_12w,
                        "change_6m": vn_6m,
                        "count": 1
                    }
                    log.info(f"[{INSTANCE_ID}] ✅ Đã thêm VNINDEX: 12W={vn_12w:.1f}%, 6M={vn_6m:.1f}%")
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}] ⚠️ Lỗi lấy VNINDEX: {e}")

        # 4. Lưu Redis
        final_payload = {
            "updated_at": datetime.datetime.now().isoformat(),
            "stocks": stocks_data,
            "sectors": sectors_final
        }
        
        await asyncio.to_thread(save_historical_valuation_to_redis, final_payload)
        log.info(f"[{INSTANCE_ID}] ✅ Hoàn tất Comprehensive Data. Đã lưu {len(stocks_data)} mã và {len(sectors_final)} ngành.")

        if manual_alerts and ADMIN_ID:
            deduped = list(dict.fromkeys(manual_alerts))
            preview = "\n".join(deduped[:10])
            remainder = ""
            if len(deduped) > 10:
                remainder = f"\n... và {len(deduped) - 10} mã khác."
            push_telegram_msg(
                ADMIN_ID,
                "⚠️ Manual PE/PB thiếu dữ liệu:\n" + preview + remainder,
                msg_type="SYSTEM_MSG",
            )

    except BaseException as e:
        if isinstance(e, asyncio.CancelledError):
            raise e
        log.error(f"[{INSTANCE_ID}] ❌ LỖI NGHIÊM TRỌNG (Comprehensive Task): {type(e).__name__} - {e}")
        await asyncio.sleep(60)

async def get_top_mean_reversion_stocks(limit=5):
    """
    Lấy Top cổ phiếu rẻ nhất (Mean Reversion) từ Redis (Cấu trúc mới).
    """
    try:
        # 1. Lấy dữ liệu từ Redis
        full_data = await asyncio.to_thread(get_historical_valuation_from_redis)
        
        # Nếu chưa có hoặc format cũ -> chạy lại task đồng bộ để có dữ liệu
        if not full_data or "stocks" not in full_data:
            lock = await _get_comprehensive_lock()
            async with lock:
                # Re-check after acquiring lock to avoid duplicate jobs
                full_data = await asyncio.to_thread(get_historical_valuation_from_redis)
                if full_data and "stocks" in full_data:
                    log.info(f"[{INSTANCE_ID}] Dữ liệu Comprehensive đã có sau khi chờ lock.")
                else:
                    log.warning(f"[{INSTANCE_ID}] Redis chưa có dữ liệu Comprehensive. Đang chạy tính toán đồng bộ...")
                    try:
                        await calculate_market_comprehensive_data()
                    except Exception as exc:
                        log.error(f"[{INSTANCE_ID}] Lỗi tính toán Comprehensive tức thời: {exc}")
                        return []
                    full_data = await asyncio.to_thread(get_historical_valuation_from_redis)

            if not full_data or "stocks" not in full_data:
                log.error(f"[{INSTANCE_ID}] Không lấy được dữ liệu Comprehensive sau khi tính toán.")
                return []

        hist_data = full_data["stocks"] # Lấy phần stocks

        processed_items = []
        
        for sym, stock_info in hist_data.items():
            pe_avg = stock_info.get('pe_avg')
            pb_avg = stock_info.get('pb_avg')
            if not pe_avg or not pb_avg:
                continue

            pe_cur = stock_info.get('pe_manual')
            pb_cur = stock_info.get('pb_manual')
            if pe_cur is None or pb_cur is None:
                manual = await asyncio.to_thread(fetch_manual_pe_pb, sym)
                pe_cur = manual.pe
                pb_cur = manual.pb
                if manual.needs_admin_alert and manual.error:
                    log.warning(f"[{INSTANCE_ID}] Manual valuation missing for {sym}: {manual.error}")

            if not pe_cur or not pb_cur:
                continue
            if pe_cur <= 0 or pb_cur <= 0:
                continue
            if pe_avg <= 0 or pb_avg <= 0:
                continue

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

# --- NEW HELPERS FOR PERSONALIZED DIGEST ---
AI_SEMAPHORE = asyncio.Semaphore(3)
_comprehensive_lock: asyncio.Lock | None = None


async def _get_comprehensive_lock() -> asyncio.Lock:
    global _comprehensive_lock
    if _comprehensive_lock is None:
        _comprehensive_lock = asyncio.Lock()
    return _comprehensive_lock

def tag_news_items(items):
    for item in items:
        text = item['title'] + " " + item.get('summary', '')
        text_upper = text.upper()
        found = set()
        # Fast check for symbols (3 chars)
        potential_syms = set(re.findall(r"\b[A-Z]{3}\b", text_upper))
        for sym in potential_syms:
            if sym in COMPANY_KEYWORDS:
                found.add(sym)
        item['tags'] = found
    return items

async def generate_user_ai_digest(chat_id, watchlist, all_spec_news, all_macro_news):
    async with AI_SEMAPHORE:
        # 1. Filter Specialized News
        my_spec = []
        other_spec = []
        w_set = set(str(s).upper() for s in watchlist)
        
        for item in all_spec_news:
            if not item['tags'].isdisjoint(w_set):
                my_spec.append(item)
            else:
                other_spec.append(item)
        
        # 2. Fill up (Limit total spec news to 60)
        final_spec = my_spec[:]
        needed = 60 - len(final_spec)
        if needed > 0:
            final_spec.extend(other_spec[:needed])
            
        # 3. Combine with Macro
        input_news = all_macro_news + final_spec
        
        # 4. Call AI hoặc fallback
        if not input_news:
            return _build_empty_ai_digest("Chưa có tin tức mới trong 24 giờ qua." )

        result = await summarize_daily_news_with_ai(input_news)
        return result or _build_empty_ai_digest("AI không thể tổng hợp dữ liệu. Hiển thị ghi chú mặc định.")

    
async def job_daily_digest():
    """
    [JOB APSCHEDULER] Gửi bản tin sáng (Digest) lúc 07:00.
    Không còn while True, không còn sleep.
    """
    # 1. Kiểm tra trạng thái Bot
    if not get_bot_active():
        log.info("[DIGEST] Bot đang TẮT (Maintenance). Bỏ qua Job sáng nay.")
        return

    log.info("[DIGEST] 🌅 07:00! Bắt đầu Job tạo bản tin...")
    
    vn_tz = pytz.timezone(TIMEZONE)
    now_local = datetime.datetime.now(vn_tz)

    try:
        # 2. Thu thập dữ liệu (Song song)
        # Lưu ý: Nightly Loop (02:00) đã tính toán Screener và lưu Redis rồi.
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
            get_top_mean_reversion_stocks(limit=5),
        )

        # 3. Chuẩn bị dữ liệu tin tức
        macro_news_list = [{"title": r[0], "link": r[1], "source": "Vĩ mô"} for r in macro_rows]
        spec_news_list = [{"title": r[0], "link": r[1], "source": "DN"} for r in spec_rows]
        
        # Tagging
        spec_news_list = tag_news_items(spec_news_list)

        # 4. Chạy AI cho từng User (Parallel)
        user_ai_tasks = []
        user_ids_map = [] # Keep track of order
        
        for chat_key, user_block in all_watch.items():
            try: chat_id = int(chat_key)
            except: continue
            
            watchlist = user_block.get("list", [])
            user_ids_map.append(chat_id)
            user_ai_tasks.append(generate_user_ai_digest(chat_id, watchlist, spec_news_list, macro_news_list))
            
        # Run gather
        ai_results = await asyncio.gather(*user_ai_tasks)
        user_ai_map = dict(zip(user_ids_map, ai_results))

        # 5. Chuẩn bị dữ liệu Mapping
        bctc_by_sym = {str(sym).upper(): (y, q, t) for (sym, y, q, t) in bctc_rows}
        
        # Group Reports theo mã
        reports_by_sym = {}
        for (s, title, link, pub, created) in report_rows: # Lưu ý unpack đúng số lượng cột từ DB trả về
            reports_by_sym.setdefault(str(s).upper(), []).append((title, link, pub))
        
        # Map User -> Watchlist
        watch_to_chats = {}
        for chat_key, user_block in all_watch.items():
            try: 
                chat_id = int(chat_key)
            except: 
                continue
            for sym in user_block.get("list", []) or []:
                watch_to_chats.setdefault(str(sym).upper().strip(), []).append(chat_id)

        # 6. Khởi tạo Payload cho từng User
        digest_payloads = {}
        
        def _get_payload(cid):
            if cid not in digest_payloads:
                # Lấy AI Data riêng của user và đảm bảo dạng dict
                my_ai_data = _parse_ai_digest_payload(user_ai_map.get(cid))
                
                digest_payloads[cid] = {
                    "is_pro": (cid in pro_chat_ids or cid == ADMIN_ID),
                    "ai_news": my_ai_data, 
                    "value_stocks": [], 
                    "bctc": [], 
                    "reports": []
                }
            return digest_payloads[cid]

        # Đảm bảo mọi user có watchlist đều nhận được tin (dù chỉ là tin AI)
        if all_watch:
            for chat_key in all_watch.keys():
                try:
                    _get_payload(int(chat_key))
                except: continue

        # 7. Fill dữ liệu chi tiết vào Payload
        
        # A. Value Stocks (Chỉ cho Pro)
        if top_value_stocks:
            for cid in digest_payloads.keys():
                pl = digest_payloads[cid]
                if pl["is_pro"]: 
                    pl["value_stocks"] = top_value_stocks

        # B. BCTC
        if bctc_rows:
            for sym, (y, q, t) in bctc_by_sym.items():
                t_str = t.astimezone(vn_tz).strftime("%H:%M %d/%m")
                for cid in watch_to_chats.get(sym, []):
                    pl = _get_payload(cid)
                    is_locked = not pl["is_pro"]
                    # Tránh add trùng
                    if not any(x['symbol'] == sym for x in pl["bctc"]):
                        pl["bctc"].append({
                            "symbol": sym, "year": y, "quarter": q, 
                            "time": t_str, "is_locked": is_locked
                        })

        # C. Analysis Reports (ĐÃ BỔ SUNG PHẦN THIẾU)
        if report_rows:
            for sym, r_list in reports_by_sym.items():
                for cid in watch_to_chats.get(sym, []):
                    pl = _get_payload(cid)
                    
                    # Lấy tin mới nhất để hiển thị thời gian nếu bị lock
                    last_pub = r_list[0][2]
                    t_str = last_pub.astimezone(vn_tz).strftime("%H:%M %d/%m") if last_pub else ""
                    
                    if pl["is_pro"]:
                        # Pro: Xem hết các báo cáo
                        for (title, link, pub) in r_list:
                            ts = pub.astimezone(vn_tz).strftime("%H:%M %d/%m") if pub else ""
                            # Tránh trùng link
                            if not any(x['link'] == link for x in pl["reports"]):
                                pl["reports"].append({
                                    "symbol": sym, "title": title, "link": link, 
                                    "time": ts, "is_locked": False
                                })
                    else:
                        # Free: Chỉ hiện 1 dòng bị lock
                        if not any(x['symbol'] == sym for x in pl["reports"]):
                            pl["reports"].append({
                                "symbol": sym, "title": "Báo cáo phân tích (Pro)", 
                                "link": "#", "time": t_str, "is_locked": True
                            })

        # 8. Gửi tin nhắn (Push Redis)
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        count = 0
        
        for chat_id, data in digest_payloads.items():
            try:
                digest_id = uuid.uuid4().hex
                
                # Lưu Web App Data vào Redis (TTL 24h)
                r_client.set(f"digest_web:{digest_id}", json.dumps(data), ex=86400)
                
                web_url = f"{BASE_URL}/digest/{digest_id}"
                
                kb = {
                    "inline_keyboard": [[
                        {"text": "📰 Xem Chi Tiết (Web App) 🚀", "web_app": {"url": web_url}}
                    ]]
                }
                
                # Format AI Text riêng cho từng user
                ai_data = data.get("ai_news")
                if ai_data and not isinstance(ai_data, dict):
                    ai_data = _parse_ai_digest_payload(ai_data)
                ai_text = "_Không có tin nổi bật._"
                if isinstance(ai_data, dict):
                    lines = []
                    headlines = ai_data.get('headline') or []
                    if isinstance(headlines, dict):
                        headlines = [headlines]
                    if headlines:
                        lines.append("⚡ *TIÊU ĐIỂM*")
                        for item in headlines:
                            if isinstance(item, dict):
                                text = item.get('text') or ''
                            else:
                                text = str(item)
                            if text:
                                lines.append(f"• {text}")
                    comment = ai_data.get('comment')
                    if comment:
                        lines.append(f"\n🧠 *AI:* {comment}")
                    if lines:
                        ai_text = "\n".join(lines)
                
                msg_text = (
                    f"🌅 *BẢN TIN SÁNG {now_local.strftime('%d/%m')}* 🤖\n\n"
                    f"👉 *Chúc bạn một ngày năng suất nhé!*"
                )

                push_telegram_msg(
                    chat_id=chat_id,
                    text=msg_text,
                    reply_markup=kb,
                    msg_type="DIGEST" 
                )
                count += 1
                
                # Rate limit nhẹ khi push redis (để Gateway không bị ngộp)
                if count % 20 == 0:
                    await asyncio.sleep(0.5)

            except Exception as e:
                log.warning(f"[DIGEST] Lỗi tạo msg cho {chat_id}: {e}")
        
        log.info(f"[DIGEST] ✅ Đã đẩy {count} bản tin sang Gateway.")

    except Exception as e:
        log.error(f"[DIGEST] ❌ Lỗi Job: {e}")

# ==============================
# BÁO CÁO TÀI CHÍNH (BCTC) LOOP
# ==============================

BCTC_MONTHS = [1, 4, 5, 10] # Tháng có thể ra BCTC


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

async def job_scan_bctc():
    """
    [JOB APSCHEDULER] Quét BCTC mới từ Vnstock và đánh dấu vào DB.
    Chạy định kỳ vào các tháng cao điểm (1, 4, 5, 10).
    """
    log.info("[BCTC] 🔍 Bắt đầu Job quét Báo cáo tài chính...")
    
    if not get_bot_active():
        return

    try:
        vn_tz = pytz.timezone(TIMEZONE)
        now = datetime.datetime.now(vn_tz)
        
        # 1. Xác định Kỳ Báo Cáo (Quý/Năm) dựa trên ngày hiện tại
        period = get_bctc_period_for_date(now)
        if not period:
            log.warning(f"[BCTC] Không xác định được kỳ BCTC cho ngày {now.date()}. Bỏ qua.")
            return
            
        year, quarter = period
        period_label = f"Quý {quarter}/{year}"
        log.info(f"[BCTC] Đang quét dữ liệu cho: {period_label}")

        # 2. Lấy danh sách mã cần quét (Chỉ quét cho Pro User & Admin để tiết kiệm resource)
        all_watch = await asyncio.to_thread(get_all_watch)
        pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
        
        symbol_set = set()
        for chat_key, info in all_watch.items():
            try:
                chat_id = int(chat_key)
                # Chỉ lấy watchlist của Pro hoặc Admin
                if chat_id in pro_chat_ids or chat_id == ADMIN_ID:
                    syms = info.get("list", [])
                    for s in syms:
                        if s: symbol_set.add(str(s).upper().strip())
            except: continue

        if not symbol_set:
            log.info("[BCTC] Không có mã nào trong watchlist của Pro User.")
            return

        # 3. Duyệt từng mã
        count_new = 0
        for sym in sorted(symbol_set):
            # Check Active giữa chừng
            if not get_bot_active(): break

            try:
                # Check DB: Đã thông báo chưa?
                already = await asyncio.to_thread(has_bctc_notified, sym, year, quarter)
                if already:
                    continue # Đã có rồi thì bỏ qua nhanh

                # Check API: Có BCTC trên mạng chưa?
                # (Hàm check_bctc_available đã có sẵn trong worker.py, chạy trong thread)
                available = await asyncio.to_thread(check_bctc_available, sym, year, quarter)
                
                if available:
                    # Đánh dấu vào DB (để Digest sáng mai sẽ lấy ra gửi)
                    await asyncio.to_thread(mark_bctc_notified, sym, year, quarter)
                    count_new += 1
                    log.info(f"[BCTC] 🆕 Phát hiện BCTC mới: {sym} ({period_label})")
                
                # Rate Limit nhẹ
                await asyncio.sleep(0.5)

            except Exception as e:
                log.warning(f"[BCTC] Lỗi check {sym}: {e}")

        log.info(f"[BCTC] ✅ Hoàn tất quét. Phát hiện {count_new} báo cáo mới.")

    except Exception as e:
        log.error(f"[BCTC] ❌ Lỗi Job: {e}")

#-------------------------------------------

async def job_scan_analysis_reports():
    """
    [JOB APSCHEDULER] Quét báo cáo phân tích (Chạy lúc 07:00).
    """
    log.info("[REPORT_SCAN] 📑 Bắt đầu job quét báo cáo phân tích...")
    
    if not get_bot_active():
        return

    try:
        # 1. Lấy danh sách mã quan tâm (Của TẤT CẢ User - Đã gỡ Paywall theo logic cũ)
        all_watch = await asyncio.to_thread(get_all_watch)
        
        all_symbol_set = set()
        for user_block in all_watch.values():
            watch_list = user_block.get("list", []) or []
            for sym in watch_list:
                s = str(sym).upper().strip()
                if s and len(s) == 3:
                    all_symbol_set.add(s)

        if not all_symbol_set:
            log.info("[REPORT_SCAN] Không có mã nào trong watchlist. Kết thúc.")
            return

        log.info(f"[REPORT_SCAN] Đang quét báo cáo cho {len(all_symbol_set)} mã...")
        
        count_new = 0
        
        # 2. Duyệt từng mã
        for symbol in sorted(all_symbol_set):
            # Kiểm tra Bot Active giữa chừng (vì loop này chạy lâu)
            if not get_bot_active():
                log.warning("[REPORT_SCAN] Bot TẮT giữa chừng. Dừng job.")
                break

            try:
                # Fetch dữ liệu từ Vnstock (chạy trong thread)
                company = Company(symbol=symbol)
                df = await asyncio.to_thread(company.reports)
                
                if df is None or df.empty:
                    await asyncio.sleep(2) # Rate limit nhẹ
                    continue

                # 3. Kiểm tra và Lưu
                for row in df.itertuples():
                    link = getattr(row, "link", "")
                    date_str = getattr(row, "date", "")
                    title = getattr(row, "name", "")
                    
                    if not link or not date_str: continue

                    # Check Redis xem đã lưu chưa
                    is_seen = await asyncio.to_thread(has_report_seen, link, date_str)
                    
                    if not is_seen:
                        await asyncio.to_thread(
                            mark_report_seen, symbol, link, title, date_str
                        )
                        count_new += 1
                        # log.info(f"[REPORT_SCAN] 🆕 Tìm thấy: {symbol} - {title}")

            except Exception as e:
                log.warning(f"[REPORT_SCAN] Lỗi mã {symbol}: {e}")

            # Rate Limit quan trọng: Nghỉ 3s giữa các mã để tránh bị chặn IP
            await asyncio.sleep(3)

        log.info(f"[REPORT_SCAN] ✅ Hoàn tất. Đã lưu {count_new} báo cáo mới.")

    except Exception as e:
        log.error(f"[REPORT_SCAN] ❌ Lỗi Job: {e}")

#-------------------------------------------

async def job_nightly_valuation():
    """
    [JOB APSCHEDULER] Tính toán định giá Mean Reversion (Chạy lúc 02:00).
    """
    log.info("[NIGHTLY] 🌙 Job tính toán định giá lịch sử bắt đầu...")

    if not get_bot_active():
        log.info("[NIGHTLY] Bot đang TẮT. Bỏ qua job.")
        return

    try:
        # Gọi task tính toán nặng (đã có sẵn trong worker.py)
        # Task này đã bao gồm logic try/except và rate limit bên trong
        await calculate_market_comprehensive_data()
        
        log.info("[NIGHTLY] ✅ Job tính toán hoàn tất.")
        
    except Exception as e:
        log.error(f"[NIGHTLY] ❌ Lỗi Job: {e}")

#-------------------------------------------
async def job_restore_reminder():
    """
    [JOB APSCHEDULER] Nhắc Admin backup dữ liệu core vào 08:00 sáng ngày 7 hàng tháng.
    """
    log.info("[REMINDER] ⏰ Bắt đầu job nhắc nhở backup...")

    if not get_bot_active():
        return

    if not ADMIN_ID:
        log.warning("[REMINDER] Chưa cấu hình ADMIN_ID, không thể gửi nhắc nhở.")
        return

    try:
        vn_tz = pytz.timezone(TIMEZONE)
        now = datetime.datetime.now(vn_tz)

        msg = (
            f"⏰ **NHẮC NHỞ BẢO TRÌ ĐỊNH KỲ (Tháng {now.month})**\n\n"
            f"Hôm nay là ngày 7. Đã đến lúc sao lưu dữ liệu Core.\n"
            f"👉 Vui lòng vào trang admin để tải bản backup về.\n"
            f"👉 Sau đó kiểm tra đính kèm file restore và chạy `/restore_core` nếu cần chuyển Database."
        )

        # Bắn tin nhắc nhở qua Redis Gateway
        push_telegram_msg(ADMIN_ID, msg, msg_type="SYSTEM_MSG")
        
        log.info(f"[REMINDER] ✅ Đã gửi nhắc nhở bảo trì tháng {now.month} cho Admin.")

    except Exception as e:
        log.error(f"[REMINDER] ❌ Lỗi Job: {e}")

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
# LOOP TIN CHUYÊN NGÀNH và VĨ MÔ (Sửa đổi: Quét 06:00 & 18:00)
# ===============================================================

async def job_scan_news(feed_type: str):
    """
    [JOB APSCHEDULER] Quét tin tức RSS (Chạy lúc 06:00 và 18:00).
    feed_type: "MACRO" hoặc "SPECIALIZED"
    """
    log.info(f"[NEWS] 📰 Bắt đầu job quét tin {feed_type}...")
    
    if not get_bot_active():
        return

    try:
        # 1. Xác định danh sách URL dựa trên loại tin
        urls = []
        if feed_type == "MACRO":
            urls = RSS_FEEDS_MACRO
        elif feed_type == "SPECIALIZED":
            # Gom tất cả URL từ các nhóm ngành vào 1 list duy nhất
            for group_urls in RSS_FEEDS_SPECIALIZED.values():
                urls.extend(group_urls)
        
        if not urls:
            log.warning(f"[NEWS] Không tìm thấy URL nào cho loại {feed_type}")
            return

        # 2. Gọi hàm Fetch (Chạy trong thread vì feedparser là sync)
        # Lưu ý: Fetcher cũ của bạn đã trả về list dict chuẩn rồi
        entries = await asyncio.to_thread(fetch_rss_entries_for_urls, urls)
        
        # Giới hạn số lượng xử lý để tránh quá tải DB
        if len(entries) > NEWS_MAX_RSS_ENTRIES_PER_RUN:
            entries = entries[:NEWS_MAX_RSS_ENTRIES_PER_RUN]

        vn_tz = pytz.timezone(TIMEZONE)
        scan_now = datetime.datetime.now(vn_tz)
        new_count = 0

        # 3. Lọc và Lưu
        for it in entries:
            link = (it.get("link") or "").strip()
            if not link: continue
            
            # Check 1: Bài viết có quá cũ không?
            if not is_fresh_news(it.get("published"), scan_now): 
                continue
            
            # Check 2: Đã từng lưu vào DB chưa?
            is_seen = await asyncio.to_thread(has_news_seen, feed_type, link)
            
            if not is_seen:
                # Lưu vào DB (Mark seen)
                await asyncio.to_thread(
                    mark_news_seen, 
                    feed_type, 
                    link=it["link"], 
                    guid=None, # RSS thường ít dùng guid chuẩn, dùng link làm key là ổn
                    title=it["title"], 
                    published=it["published"]
                )
                new_count += 1
        
        if new_count > 0:
            log.info(f"[NEWS] ✅ {feed_type}: Đã lưu {new_count} tin mới.")
        else:
            log.info(f"[NEWS] {feed_type}: Không có tin mới.")

    except Exception as e:
        log.error(f"[NEWS] ❌ Lỗi quét {feed_type}: {e}")

async def job_maintenance():
    """
    [JOB APSCHEDULER] Dọn dẹp DB định kỳ (Chạy 1 lần/ngày).
    """
    log.info("[MAINTENANCE] 🧹 Bắt đầu dọn dẹp dữ liệu cũ...")
    
    try:
        # 1. Xóa tin tức đã seen quá cũ (> 180 ngày)
        deleted_news = await asyncio.to_thread(cleanup_old_news_seen, 180)
        
        # 2. Xóa đơn hàng treo quá lâu (> 3 ngày)
        deleted_orders = await asyncio.to_thread(cleanup_old_pending_orders, 3)

        # 3. Xóa ghi chú cá nhân hóa đã hết hạn
        deleted_notes = await asyncio.to_thread(cleanup_expired_stock_personalizations)
        
        if deleted_news > 0 or deleted_orders > 0 or deleted_notes > 0:
            log.info(
                f"[MAINTENANCE] ✅ Đã xóa: {deleted_news} news cũ, {deleted_orders} đơn hàng treo, {deleted_notes} ghi chú hết hạn."
            )
        else:
            log.info("[MAINTENANCE] Hệ thống sạch sẽ, không có gì để xóa.")
            
    except Exception as e:
        log.error(f"[MAINTENANCE] ❌ Lỗi dọn dẹp: {e}")

# ===============================================================
# SESSION NOTICE LOOP
# ===============================================================

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

async def job_session_notice(text_msg: str):
    """
    [JOB APSCHEDULER] Gửi thông báo phiên (Sáng/Chiều) cho user.
    """
    log.info(f"[SESSION] 🔔 Job thông báo phiên: '{text_msg[:15]}...'")
    
    if not get_bot_active():
        return

    try:
        # Lấy tất cả user (không phân biệt Pro/Free)
        all_watch = await asyncio.to_thread(get_all_watch)
        count = 0
        
        for chat_key in all_watch.keys():
            try:
                chat_id = int(chat_key)
                # Gửi qua Redis Gateway với loại tin SESSION_NOTICE
                push_telegram_msg(chat_id, text_msg, msg_type="SESSION_NOTICE")
                count += 1
            except: pass
            
        log.info(f"[SESSION] Đã đẩy thông báo tới {count} user.")

    except Exception as e:
        log.error(f"[SESSION] ❌ Lỗi Job: {e}")

async def job_eod_summary():
    """
    [JOB APSCHEDULER] Tổng kết cuối phiên (EOD) lúc 15:00.
    """
    log.info("[EOD] 📊 Bắt đầu Job EOD Summary...")
    
    if not get_bot_active():
        return

    try:
        # Gọi hàm worker cũ đã có sẵn logic lấy giá, vẽ chart và gửi tin
        await send_eod_summary_worker()
        
    except Exception as e:
        log.error(f"[EOD] ❌ Lỗi Job: {e}")

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


async def generate_market_comment_ai(vni_data, v30_data):
    """
    Tạo nhận định thị trường ngắn gọn từ dữ liệu Index.
    """
    if not vni_data or not v30_data:
        return "Thị trường hôm nay biến động..."

    try:
        prompt = f"""
Bạn là chuyên gia tài chính. Hãy viết một nhận định ngắn (dưới 200 từ) về thị trường chứng khoán Việt Nam hôm nay dựa trên số liệu sau:

VN-INDEX: {vni_data['price']} ({vni_data['change_str']})
VN30: {v30_data['price']} ({v30_data['change_str']})

Yêu cầu:
- Vào luôn vấn đề chính. Không chào hỏi dài dòng và không cần dẫn dắt.
- Giọng văn chuyên nghiệp, súc tích, tập trung vào xu hướng chính.
- Không lặp lại số liệu chi tiết (vì user đã thấy rồi).
- Nhận xét về tâm lý thị trường (Hưng phấn/Thận trọng/Hoảng loạn).
- Có thể dùng emoji phù hợp để tăng tính sinh động.
"""
        comment = await asyncio.to_thread(
            call_gemini_safe,
            model_id="gemini-2.5-pro",
            contents=prompt
        )
        return comment.strip() if comment else "Thị trường hôm nay..."
    except Exception as e:
        log.error(f"Lỗi AI Market Comment: {e}")
        return "Thị trường hôm nay..."

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
    vni_data, v30_data = await asyncio.gather(
        asyncio.to_thread(get_index_snapshot, 'VNINDEX', now),
        asyncio.to_thread(get_index_snapshot, 'VN30', now)
    )

    # Chờ chart xong
    chart_vni, chart_v30 = await asyncio.gather(task_vni, task_v30)
    
    # [NEW] Gọi AI nhận định
    ai_comment = await generate_market_comment_ai(vni_data, v30_data)

    market_data = {
        "vnindex": {**vni_data, "chart_html": chart_vni},
        "vn30": {**v30_data, "chart_html": chart_v30},
        "ai_comment": ai_comment
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
    
    lines = ["\n🔍 **DỮ LIỆU TÀI CHÍNH (REALTIME vs TRUNG BÌNH 5 NĂM):**"]
    lines.append("(Sử dụng số liệu dưới đây để đánh giá Đắt/Rẻ theo Mean Reversion)")
    lines.append("-" * 60)

    for sym in symbols:
        sym_u = sym.upper()
        lines.append(f"📌 **{sym_u}**:")
        
        hist = hist_data.get(sym_u, {})
        
        # Chỉ số
        pe_avg = float(hist.get('pe_avg', 0) or 0)
        pb_avg = float(hist.get('pb_avg', 0) or 0)

        pe_cur = hist.get('pe_manual')
        pb_cur = hist.get('pb_manual')
        if pe_cur is None or pb_cur is None:
            manual = await asyncio.to_thread(fetch_manual_pe_pb, sym_u)
            pe_cur = manual.pe
            pb_cur = manual.pb
            if manual.needs_admin_alert and manual.error:
                log.warning(f"[{INSTANCE_ID}] Manual valuation missing for {sym_u}: {manual.error}")

        info = []
        if pe_cur > 0 and pe_avg > 0:
            diff = (pe_cur - pe_avg) / pe_avg * 100
            state = "RẺ HƠN" if diff < 0 else "ĐẮT HƠN"
            info.append(f"   - P/E: {pe_cur:.1f}x (TB 5 năm: {pe_avg:.1f}x) -> {state} {abs(diff):.1f}%")
        
        pe_cur_val = float(pe_cur or 0)
        pb_cur_val = float(pb_cur or 0)

        if pe_cur_val > 0 and pe_avg > 0:
            info.append(f"P/E: hiện {pe_cur_val:.1f} vs TB5 {pe_avg:.1f} ({pe_cur_val/pe_avg:.2f}x)")
        else:
            info.append("P/E: Không khả dụng")
        if pb_cur_val > 0 and pb_avg > 0:
            info.append(f"P/B: hiện {pb_cur_val:.1f} vs TB5 {pb_avg:.1f} ({pb_cur_val/pb_avg:.2f}x)")
        else:
            info.append("P/B: Không khả dụng")
            lines.extend(info)

    lines.append("-" * 60)
    return "\n".join(lines)

def call_chatgpt_for_report(
    symbols: list[str],
    agent_payload: dict[str, dict],
    personalization_notes: dict[str, list[dict]] | None = None,
) -> str:
    """Gọi Gemini tạo báo cáo dựa trên output của macro/biz/tech agents."""
    if not GEMINI_KEYS:
        raise RuntimeError("GEMINI_API_KEY chưa được cấu hình.")

    normalized: list[str] = []
    for sym in symbols or []:
        cleaned = str(sym).strip().upper()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)

    if not normalized:
        raise ValueError("Danh sách mã trống khi tạo báo cáo.")

    if len(normalized) > 6:
        normalized = normalized[:6]

    def _fallback(obj):
        return str(obj)

    def _format_agent_block(data: dict | None) -> str:
        return json.dumps(data or {}, ensure_ascii=False, indent=2, default=_fallback)

    def _read_macro_csv(agent_payload: dict | None) -> str:
        macro_data = (agent_payload or {}).get("macro") or {}
        raw_meta = macro_data.get("raw_data") or {}
        csv_path = raw_meta.get("csv_path") or ((macro_data.get("redis_json") or {}).get("csv_path"))

        meta_text = _format_agent_block(raw_meta)
        if not csv_path:
            return (
                "Thông tin GSO:\n" + meta_text +
                "\n\nDữ liệu CSV: Không tìm thấy đường dẫn csv_path trong macro agent."
            )

        if not os.path.exists(csv_path):
            return (
                "Thông tin GSO:\n" + meta_text +
                f"\n\nDữ liệu CSV: File không tồn tại tại đường dẫn {csv_path}."
            )

        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                csv_text = f.read()
        except Exception as exc:
            return (
                "Thông tin GSO:\n" + meta_text +
                f"\n\nDữ liệu CSV: Lỗi đọc file ({exc})."
            )

        if not csv_text.strip():
            return (
                "Thông tin GSO:\n" + meta_text +
                "\n\nDữ liệu CSV: File trống hoặc không có nội dung."
            )

        return (
            "Thông tin GSO:\n" + meta_text +
            f"\n\nDữ liệu CSV (tất cả sheet) từ {csv_path}:\n" + csv_text
        )

    macro_block = _read_macro_csv(agent_payload)
    biz_block = _format_agent_block((agent_payload or {}).get("biz"))
    tech_block = _format_agent_block((agent_payload or {}).get("tech"))

    def _format_personalization_block() -> str:
        notes_map = personalization_notes or {}
        if not notes_map:
            return "Không có ghi chú cá nhân hoá nào."

        vn_tz = pytz.timezone(TIMEZONE)

        # --- Helper Con: Xử lý hiển thị nội dung + ngày hết hạn (Code cũ của bạn) ---
        def _format_note_entry(entry):
            note_text = (entry or {}).get("note")
            if not note_text:
                return ""

            expiry_info = ""
            expires_at = (entry or {}).get("expires_at")
            expiry_dt: datetime.datetime | None = None
            
            # Parse ngày tháng
            if isinstance(expires_at, datetime.datetime):
                expiry_dt = expires_at
            elif isinstance(expires_at, str):
                try:
                    expiry_dt = datetime.datetime.fromisoformat(expires_at)
                except ValueError:
                    expiry_dt = None

            # Format chuỗi hiển thị
            if expiry_dt is not None:
                try:
                    if expiry_dt.tzinfo is None:
                        expiry_dt = expiry_dt.replace(tzinfo=datetime.timezone.utc)
                    local = expiry_dt.astimezone(vn_tz)
                    expiry_info = f" (Hết hạn: {local.strftime('%d/%m %H:%M')})"
                except Exception:
                    expiry_info = ""
            
            return f"{note_text}{expiry_info}"
        # --------------------------------------------------------------------------

        lines = []
        sector_map_local = load_symbol_sector_map() # Helper đã có trong worker

        # 1. Ghi chú Vĩ mô (VN_MACRO) - Đưa lên đầu
        macro_notes = notes_map.get("VN_MACRO") or []
        if macro_notes:
            lines.append("--- GÓC NHÌN VĨ MÔ (CỘNG ĐỒNG) ---")
            for entry in macro_notes:
                formatted_text = _format_note_entry(entry)
                if formatted_text:
                    lines.append(f"• {formatted_text}")
            lines.append("")

        # 2. Ghi chú Ngành & Cổ phiếu (Loop qua danh sách mã đang request)
        for sym in normalized:
            # Lấy note của chính cổ phiếu đó
            s_notes = notes_map.get(sym) or []
            
            # Lấy note của ngành tương ứng
            sector_name = sector_map_local.get(sym)
            sec_notes = notes_map.get(sector_name) if sector_name else []
            
            if s_notes or sec_notes:
                # Tiêu đề nhóm
                lines.append(f"📌 MÃ: {sym} (Ngành: {sector_name or 'N/A'})")
                
                # A. Note ngành (Dùng helper để format)
                if sec_notes:
                    for entry in sec_notes:
                        text = _format_note_entry(entry)
                        if text: lines.append(f"   - [Ngành] {text}")
                
                # B. Note cổ phiếu (Dùng helper để format + index)
                if s_notes:
                    for idx, entry in enumerate(s_notes, 1):
                        text = _format_note_entry(entry)
                        if text:
                            # Nếu có nhiều note thì đánh số, 1 note thì khỏi
                            label = f"[Cổ phiếu #{idx}]" if len(s_notes) > 1 else "[Cổ phiếu]"
                            lines.append(f"   - {label} {text}")
                
                lines.append("") # Dòng trống ngăn cách các mã

        if not lines:
            return "Không có ghi chú phù hợp cho danh mục này."
            
        return "\n".join(lines)

    personalization_block = _format_personalization_block()

    symbols_str = ", ".join(normalized)
    vn_tz = pytz.timezone(TIMEZONE)
    date_str = datetime.datetime.now(vn_tz).strftime('%d/%m/%Y')

    base_prompt = f"""
Bạn là một Chuyên gia Quản lý Quỹ (Fund Manager) hàng đầu tại Việt Nam. 
Nhiệm vụ của bạn là phân tích danh mục: {symbols_str} (Ngày báo cáo: {date_str}).

DỮ LIỆU ĐẦU VÀO (SỐ LIỆU THỰC TẾ):

1. BỐI CẢNH VĨ MÔ (GSO):
{macro_block}

2. DỮ LIỆU TÀI CHÍNH DOANH NGHIỆP:
{biz_block}

3. CHỈ BÁO KỸ THUẬT:
{tech_block}

4. GHI CHÚ CÁ NHÂN HÓA (ƯU TIÊN TUÂN THỦ KHI PHÂN TÍCH):
{personalization_block}

YÊU CẦU ĐẦU RA (JSON FORMAT):
{{
  "general_market_comment": "Cho nhận về tình hình vĩ mô hiện tại",
  "general_portfolio_comment": "Đánh giá tổng quan về danh mục hiện tại",
  "portfolio_health_score": "dựa vào triển vọng từ 3-6 tháng, hãy đánh giá danh mục trên thang điểm 10",
  "stocks": [
    {{
      "symbol": "MÃ",
      "industry": "Tên ngành",
        "analysis": "Trình bày xuống dòng theo thứ tự: (1) 🚀 Động lực chính: mô tả yếu tố tăng trưởng 3-6 tháng dựa trên BỐI CẢNH VĨ MÔ và DỮ LIỆU TÀI CHÍNH; (2) 💡 Cơ hội: ghi rõ cơ hội cụ thể; (3) ⚠️ Rủi ro: nêu rõ rủi ro cần lưu ý.",
        "key_metrics": "...",
        "action": "hãy dựa vào file CHỈ BÁO KỸ THUẬT cộng với phần analysis ở trên để đưa ra quyết định mua, bán hay nắm giữ và giải thích lý do"
    }}
  ] 
}}


LƯU Ý:
1. **QUAN TRỌNG:** Nếu health_score từ 7 trở lên, hãy coi đó là tín hiệu tích cực (Rẻ). Ngược lại là rủi ro (Đắt).
2. Giọng văn: Khách quan, sắc sảo, dựa trên số liệu.
3. Tuyệt đối trung thực với số liệu đã cung cấp trong phần 'DỮ LIỆU ĐẦU VÀO (SỐ LIỆU THỰC TẾ)'.
4. Hãy đưa ra số liệu dẫn chứng cụ thể.
5. Nếu có ghi chú cá nhân hoá cho từng mã, hãy cân nhắc kỹ và phản ánh rõ trong phần Analysis/Action.

LUẬT NGHIÊM NGẶT VỀ JSON (STRICT RULE):
1. Trả về đúng định dạng JSON chuẩn (RFC 8259).
2. KHÔNG được có dấu phẩy (,) ở phần tử cuối cùng của danh sách hoặc object (Trailing comma prohibited).
3. Xuống dòng bằng ký tự \n để tách rõ ràng còn lại thì không sử dụng dấu markdown hay định dạng đặc biệt nào khác.
4. KHÔNG thêm bất kỳ lời dẫn hay giải thích nào ngoài khối JSON.
"""
    prompt = base_prompt
    log.info(f"[{INSTANCE_ID}] Gọi Gemini (Report Multi-Agent): {symbols_str}")

    last_error: Exception | None = None
    for attempt in range(1, 3):  # Tối đa 2 lần gọi
        try:
            generation_config = {
                "response_mime_type": "application/json",
            }
            if REPORT_RESPONSE_SCHEMA:
                generation_config["response_schema"] = REPORT_RESPONSE_SCHEMA

            raw_text = call_gemini_safe(
                model_id="gemini-2.5-pro",
                contents=prompt,
                config=generation_config,
            )
            if not raw_text:
                raise RuntimeError("Gemini trả về rỗng hoặc None")

            clean_text = extract_json_from_text(raw_text)
            try:
                json.loads(clean_text)
                return clean_text
            except json.JSONDecodeError as json_err:
                log.error(
                    f"❌ JSON LỖI CÚ PHÁP (attempt {attempt}): {json_err}\n{clean_text[:5000]}"
                )
                last_error = json_err
                prompt = (
                    f"{base_prompt}\n\n⚠️ Lần thử #{attempt} JSON không hợp lệ vì: {json_err}. "
                    "Hãy trả về duy nhất một khối JSON hợp lệ chuẩn RFC 8259, không kèm chú thích."
                )
                continue
        except Exception as e:
            last_error = e
            log.error(f"[{INSTANCE_ID}] Lỗi Gemini Report (attempt {attempt}): {e}")
            prompt = (
                f"{base_prompt}\n\n⚠️ Lần thử #{attempt} gặp lỗi hệ thống: {e}. "
                "Hãy xuất lại toàn bộ JSON hợp lệ ngay lập tức."
            )
            time.sleep(1)

    raise last_error if last_error else RuntimeError("Gemini không phản hồi JSON hợp lệ")


def _clean_inline_markdown(text: str | None) -> str:
    if not text:
        return ""
    cleaned = str(text).replace("\r", "\n")
    cleaned = cleaned.replace("**", "")
    cleaned = cleaned.replace("__", "")
    cleaned = cleaned.replace("*", "")
    return cleaned.strip()


def _highlight_report_keyword(segment: str) -> str:
    base = segment.strip().strip("*")
    if not base:
        return base
    for pattern, label in REPORT_SECTION_KEYWORDS:
        match = pattern.search(base)
        if match:
            remainder = base[match.end():].lstrip(" -:\u2013")
            if remainder:
                colon_idx = remainder.find(":")
                if 0 <= colon_idx <= 40:
                    remainder = remainder[colon_idx + 1 :].lstrip()
            if not remainder:
                return f"*{label}*"
            return f"*{label}*: {remainder}"
    return base


def _normalize_report_block(text: str | None, *, use_bullet: bool = True, highlight: bool = False) -> str:
    cleaned = _clean_inline_markdown(text)
    if not cleaned:
        return ""

    fragments = re.split(r"[•\n]+", cleaned)
    lines: list[str] = []
    for fragment in fragments:
        frag = fragment.strip(" -*•")
        if not frag:
            continue
        if highlight:
            frag = _highlight_report_keyword(frag)
        lines.append(frag)

    if not lines:
        return ""

    if use_bullet:
        return "\n".join(f"• {line}" for line in lines)
    return "\n\n".join(lines)


def _normalize_report_metrics(text: str | None) -> str:
    cleaned = _clean_inline_markdown(text)
    if not cleaned:
        return ""
    parts = re.split(r"[;•\n]+", cleaned)
    tokens = [part.strip() for part in parts if part and part.strip()]
    if not tokens:
        return ""
    return " | ".join(tokens)


def _normalize_report_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        return data

    data["general_market_comment"] = _normalize_report_block(
        data.get("general_market_comment"), use_bullet=False, highlight=False
    )
    data["general_portfolio_comment"] = _normalize_report_block(
        data.get("general_portfolio_comment"), use_bullet=False, highlight=False
    )

    stocks = data.get("stocks")
    if isinstance(stocks, list):
        for stock in stocks:
            if not isinstance(stock, dict):
                continue
            stock["analysis"] = _normalize_report_block(
                stock.get("analysis"), use_bullet=True, highlight=True
            )
            stock["key_metrics"] = _normalize_report_metrics(stock.get("key_metrics"))
            stock["action"] = _normalize_report_block(
                stock.get("action"), use_bullet=True, highlight=True
            )
    return data


def _normalize_watch_symbols(raw_symbols: list[str] | None) -> list[str]:
    """Loại bỏ mã trống, chuẩn hoá chữ hoa và giữ thứ tự xuất hiện đầu tiên."""
    normalized: list[str] = []
    for sym in raw_symbols or []:
        cleaned = str(sym).strip().upper()
        if not cleaned:
            continue
        if cleaned in normalized:
            continue
        normalized.append(cleaned)
    return normalized


async def _send_report_message(
    chat_id: int,
    llm_symbols: list[str],
    cache_key: str,
    json_text: str,
    loading_msg_id: int | None = None,
):
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Report JSON hỏng cho key {cache_key}") from exc

    data = _normalize_report_payload(data)

    stocks = data.get("stocks") or []
    if not isinstance(stocks, list):
        raise ValueError("Trường 'stocks' phải là list hợp lệ.")

    digest_id = uuid.uuid4().hex
    webapp_payload = {
        "portfolio_health_score": data.get("portfolio_health_score"),
        "general_market_comment": data.get("general_market_comment"),
        "general_portfolio_comment": data.get("general_portfolio_comment"),
        "stocks": stocks,
        "is_pro": True,
    }

    r_client.set(f"digest_web:report:{digest_id}", json.dumps(webapp_payload), ex=86400)

    web_slug = cache_key or digest_id
    web_url = f"{BASE_URL}/report/view/{web_slug}?chat_id={chat_id}"
    kb = {
        "inline_keyboard": [
            [{"text": "📊 Xem Báo Cáo Chi Tiết", "web_app": {"url": web_url}}],
            [{"text": "❌ Đóng", "callback_data": "close_msg"}],
        ]
    }

    push_telegram_msg(
        chat_id=chat_id,
        text=f"🚀 **Phân tích hoàn tất!**\nĐã xử lý xong danh mục: *{', '.join(llm_symbols)}*",
        reply_markup=kb,
        msg_type="REPORT_RESULT",
        edit_id=loading_msg_id,
    )


async def try_send_cached_report(
    chat_id: int,
    llm_symbols: list[str],
    cache_key: str,
    loading_msg_id: int | None = None,
) -> bool:
    """Nếu cache report còn hạn thì gửi lại luôn để tiết kiệm quota."""
    if not cache_key:
        return False

    cached_entry = await asyncio.to_thread(get_report_from_redis, cache_key)
    if not cached_entry:
        return False

    cached_text, generated_at, is_error, _wait_sec = cached_entry
    if is_error:
        return False

    try:
        await _send_report_message(chat_id, llm_symbols, cache_key, cached_text, loading_msg_id)
        log.info(
            f"[{INSTANCE_ID}] ♻️ Dùng lại cache report {cache_key} cho chat {chat_id} (gen at {generated_at})."
        )
        return True
    except Exception as exc:
        log.error(f"[{INSTANCE_ID}] ❌ Lỗi gửi cache report {cache_key}: {exc}")
        return False

async def process_report_for_user(
    chat_id,
    symbols,
    source="on_demand",
    loading_msg_id=None,
    *,
    prefer_cache: bool = False,
) -> bool:
    """Chạy pipeline báo cáo. Trả về True nếu dùng cache, False nếu gọi AI."""
    try:
        watch_symbols = _normalize_watch_symbols(symbols or [])

        if not watch_symbols:
            push_telegram_msg(
                chat_id,
                "⚠️ Danh mục trống. Vui lòng cập nhật watchlist rồi thử lại.",
                msg_type="GENERAL",
                edit_id=loading_msg_id,
            )
            return False

        llm_symbols = watch_symbols[:6]
        cache_key = make_report_cache_key(llm_symbols)

        if prefer_cache:
            cache_hit = await try_send_cached_report(chat_id, llm_symbols, cache_key, loading_msg_id)
            if cache_hit:
                return True

        request_id = str(uuid.uuid4())
        vn_tz = pytz.timezone(TIMEZONE)
        ts_iso = datetime.datetime.now(vn_tz).isoformat()

        async def _safe_agent(agent_type: str, coro):
            try:
                return await coro
            except Exception as exc:
                log.error(f"[{INSTANCE_ID}] Agent {agent_type} error: {exc}")
                stub = _build_agent_stub(agent_type, request_id, ts_iso)
                stub["notes"] = f"Lỗi thực thi: {exc}"[:200]
                return stub

        macro_result, biz_result, tech_result = await asyncio.gather(
            _safe_agent("macro", run_macro_agent(chat_id, request_id, ts_iso)),
            _safe_agent("biz", run_biz_agent(chat_id, request_id, ts_iso, watch_symbols)),
            _safe_agent("tech", run_tech_agent(chat_id, request_id, ts_iso, watch_symbols)),
        )

        save_agent_result("macro", macro_result)
        save_agent_result("biz", biz_result)

        agent_payload = {
            "macro": macro_result,
            "biz": biz_result,
            "tech": tech_result,
        }

        # 1. Chuẩn bị danh sách keys (Stock + Sector + Macro)
        personalization_keys = prepare_personalization_keys(llm_symbols)
        
        # 2. Query DB với danh sách keys mở rộng
        personalization_map = await asyncio.to_thread(
            get_stock_personalization_map,
            personalization_keys, # Dùng list mới này thay vì chỉ llm_symbols
        )

        # 3. Gọi AI tạo báo cáo
        json_text = await asyncio.to_thread(
            call_chatgpt_for_report,
            llm_symbols,
            agent_payload,
            personalization_map, # Map này giờ đã chứa cả note ngành và vĩ mô
        )

        try:
            normalized_payload = _normalize_report_payload(json.loads(json_text))
        except json.JSONDecodeError as exc:
            raise ValueError("Gemini trả về JSON báo cáo không hợp lệ") from exc
        json_text = json.dumps(normalized_payload, ensure_ascii=False)

        save_report_to_redis(cache_key, json_text, source=source)

        await _send_report_message(chat_id, llm_symbols, cache_key, json_text, loading_msg_id)
        return False

    except Exception as e:
        log.error(f"Report Process Error for {chat_id}: {e}")
        push_telegram_msg(chat_id, "⚠️ Lỗi khi tạo báo cáo. Vui lòng thử lại.", msg_type="GENERAL")
        return False

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
        last_ai_call_ts: float | None = None
        
        for chat_key, block in all_watch.items():
            try:
                chat_id = int(chat_key)
                # Chỉ gửi cho Pro hoặc Admin
                if chat_id not in pro_chat_ids and chat_id != ADMIN_ID: 
                    skipped += 1
                    continue
                
                watch_list = block.get("list", [])
                filtered = [s.upper() for s in watch_list if not s.upper().startswith("VN")]
                normalized_symbols = _normalize_watch_symbols(filtered)
                
                if normalized_symbols:
                    llm_symbols = normalized_symbols[:6]
                    cache_key = make_report_cache_key(llm_symbols)

                    cache_hit = await try_send_cached_report(chat_id, llm_symbols, cache_key)
                    if cache_hit:
                        count += 1
                    else:
                        if last_ai_call_ts is not None:
                            elapsed = time.monotonic() - last_ai_call_ts
                            wait_seconds = 60 - elapsed
                            if wait_seconds > 0:
                                log.info(
                                    f"[{INSTANCE_ID}][WEEKLY_BATCH] ⏳ Chờ {wait_seconds:.1f}s để tránh vượt quota AI."
                                )
                                await asyncio.sleep(wait_seconds)

                        await process_report_for_user(
                            chat_id,
                            normalized_symbols,
                            source="weekly_loop",
                        )
                        last_ai_call_ts = time.monotonic()
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

async def job_weekly_report():
    """
    [JOB APSCHEDULER] Gửi báo cáo tuần (Weekly Report) vào 09:00 Chủ Nhật.
    """
    log.info("[WEEKLY] 📅 Job Weekly Report được kích hoạt...")

    # 1. Kiểm tra Maintenance Mode
    if not get_bot_active():
        log.info("[WEEKLY] Bot đang TẮT. Bỏ qua báo cáo tuần.")
        return

    # 2. Kiểm tra Idempotency (Chống gửi lặp)
    # Dù Scheduler rất chuẩn, nhưng thêm Redis check là lớp bảo vệ thứ 2 
    # (phòng trường hợp server restart liên tục trong phút thứ 09:00)
    try:
        vn_tz = pytz.timezone(TIMEZONE)
        today_str = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")
        REDIS_KEY_LAST_RUN = "worker_state:weekly_report_last_run"
        
        last_run = r_client.get(REDIS_KEY_LAST_RUN)
        if last_run == today_str:
            log.warning("[WEEKLY] ⚠️ Job đã chạy hôm nay rồi (Check Redis). Bỏ qua.")
            return

        # 3. Thực thi Batch (Logic chính)
        # Hàm execute_weekly_batch đã có sẵn ở code cũ, chỉ việc gọi lại
        await execute_weekly_batch(requester_id=None)

        # 4. Đánh dấu đã chạy xong
        r_client.set(REDIS_KEY_LAST_RUN, today_str, ex=86400 * 6) # Expire sau 6 ngày
        log.info("[WEEKLY] ✅ Job hoàn tất và đã lưu trạng thái vào Redis.")

    except Exception as e:
        log.error(f"[WEEKLY] ❌ Lỗi Job: {e}")

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
        # 1. Lấy dữ liệu lịch sử (Mean Reversion + Sector)
        full_data = get_historical_valuation_from_redis()
        
        if not full_data:
            push_telegram_msg(
                chat_id=chat_id,
                text="⚠️ Dữ liệu định giá lịch sử chưa sẵn sàng. Vui lòng thử lại sau hoặc báo Admin.",
                msg_type="ERROR",
                edit_id=loading_msg_id
            )
            return

        # Handle new format vs old format
        if "stocks" in full_data:
            hist_stocks = full_data["stocks"]
            hist_sectors = full_data.get("sectors", {})
        else:
            hist_stocks = full_data
            hist_sectors = {}

        # 2. Lấy dữ liệu thị trường hiện tại (Screener)
        # Chạy trong thread để không block
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
        # 3. Tính toán so sánh (Logic Mean Reversion)
        processed_items = []
        for index, row in screener_df.iterrows():
            sym = row['ticker']
            if sym not in hist_stocks: continue
            
            try:
                pe_cur = float(row['pe'])
                pb_cur = float(row['pb'])
            except: continue

            stock_info = hist_stocks[sym]
            # Check keys
            if 'pe_avg' not in stock_info or 'pb_avg' not in stock_info: continue

            pe_avg = stock_info['pe_avg']
            pb_avg = stock_info['pb_avg']
            
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

        # [NEW] Vẽ Chart Sector
        sector_chart_html = ""
        if hist_sectors:
            sector_chart_html = await asyncio.to_thread(draw_sector_performance_chart, hist_sectors, '12w')

        digest_id = uuid.uuid4().hex
        vn_tz = pytz.timezone(TIMEZONE)
        payload = {
            "items": top_items,
            "sector_chart": sector_chart_html,
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
        await calculate_market_comprehensive_data() # Đã cập nhật sang hàm Comprehensive
        duration = time.time() - start
        
        push_telegram_msg(admin_id, f"✅ **Hoàn tất cập nhật Screener!**\n⏱ Thời gian: {duration/60:.1f} phút.", msg_type="SYSTEM_MSG")
        
    except Exception as e:
        log.error(f"Force Update Error: {e}")
        push_telegram_msg(admin_id, f"❌ Lỗi cập nhật: {e}", msg_type="ERROR")

def job_listener(event):
    """
    Hàm listener: Tự động báo cáo lỗi Scheduler về cho Admin.
    """
    if not ADMIN_ID: return

    try:
        if event.exception:
            # Trường hợp Job bị Crash (Lỗi code, lỗi mạng...)
            error_msg = f"🚨 **WORKER CRITICAL ERROR**\nJob ID: `{event.job_id}`\nLỗi: `{event.exception}`"
            log.error(error_msg)
            # Bắn tin báo Admin qua Redis Gateway
            push_telegram_msg(ADMIN_ID, error_msg, msg_type="SYSTEM_MSG")
            
        elif event.code == EVENT_JOB_MISSED:
            # Trường hợp Job bị lỡ giờ (do server sập quá lâu)
            warning_msg = f"⚠️ **MISSED JOB**: Job `{event.job_id}` đã bị bỏ qua do quá hạn."
            log.warning(warning_msg)
            push_telegram_msg(ADMIN_ID, warning_msg, msg_type="SYSTEM_MSG")
            
    except Exception as e:
        log.error(f"Lỗi trong job_listener: {e}")

# =========== MAIN ENTRY POINT ============
async def run_worker_runtime():
    log.info(f"[{INSTANCE_ID}] Worker starting (Advanced APScheduler Mode)...")

    # --- 1. CẤU HÌNH REDIS JOB STORE ---
    # Parse URL Redis từ biến môi trường để lấy host, port, password
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # 2. Tạo biến chứa tham số kết nối mặc định
    connection_kwargs = {}

    # 3. KIỂM TRA THÔNG MINH:
    # Chỉ thêm cấu hình bỏ qua SSL nếu URL bắt đầu bằng "rediss://" (Secure Redis)
    if redis_url.startswith("rediss://"):
        connection_kwargs['ssl_cert_reqs'] = None

    # 4. Tạo Pool với tham số động
    # Nếu ở Local: connection_kwargs rỗng -> Không lỗi
    # Nếu ở Cloud: connection_kwargs có ssl_cert_reqs -> Fix lỗi Connection Closed
    pool = redis.ConnectionPool.from_url(
        redis_url,
        **connection_kwargs 
    )

    # 5. Khởi tạo JobStore
    jobstores = {
        'default': RedisJobStore(
            jobs_key='stockbot_jobs',
            run_times_key='stockbot_running',
            connection_pool=pool
        )
}
    # --- 2. CẤU HÌNH MẶC ĐỊNH (DEFAULTS) ---
    job_defaults = {
        'coalesce': True,             # Nếu lỡ nhiều lần, chỉ chạy bù 1 lần cuối
        'max_instances': 1,           # Không bao giờ chạy chồng chéo (chờ job cũ xong mới chạy job mới)
        'misfire_grace_time': 600     # Ân hạn 5 phút (nếu trễ quá 10p thì bỏ qua luôn)
    }

    # Khởi tạo Scheduler với cấu hình nâng cao
    scheduler = AsyncIOScheduler(
        jobstores=jobstores, 
        job_defaults=job_defaults, 
        timezone=TIMEZONE
    )

    # --- 3. GẮN LISTENER BẮT LỖI ---
    scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    # --- 4. ĐĂNG KÝ JOB (Sử dụng replace_existing=True để update code mới nếu job đã lưu trong Redis) ---

    # A. Digest Sáng (07:00)
    scheduler.add_job(job_daily_digest, 'cron', hour=7, minute=0, id='digest_daily', replace_existing=True)

    # B. Báo cáo Tuần (09:00 CN)
    scheduler.add_job(job_weekly_report, 'cron', day_of_week='sun', hour=9, minute=0, id='report_weekly', replace_existing=True)

    # C. Định giá Đêm (02:00)
    scheduler.add_job(job_nightly_valuation, 'cron', hour=2, minute=0, id='valuation_nightly', replace_existing=True)

    # D. Quét Tin tức (06:00, 18:00)
    scheduler.add_job(job_scan_news, 'cron', hour='6,18', args=["MACRO"], id='news_macro', replace_existing=True)
    scheduler.add_job(job_scan_news, 'cron', hour='6,18', args=["SPECIALIZED"], id='news_spec', replace_existing=True)

    # E. Dọn dẹp (03:30)
    scheduler.add_job(job_maintenance, 'cron', hour=3, minute=30, id='maintenance_daily', replace_existing=True)

    # F. Nhắc Backup (Ngày 7, 08:00)
    scheduler.add_job(job_restore_reminder, 'cron', day=7, hour=8, minute=0, id='backup_reminder', replace_existing=True)
    
    # G. Quét BCTC (Tháng 1,4,5,10 - 4 lần/ngày)
    scheduler.add_job(job_scan_bctc, 'cron', month='1,4,5,10', hour='2,8,14,20', minute=0, id='bctc_scan', replace_existing=True)
    
    # H. Quét Báo cáo Phân tích (07:00)
    scheduler.add_job(job_scan_analysis_reports, 'cron', hour=7, minute=0, id='analysis_scan', replace_existing=True)

    # I. Thông báo Phiên (Session Notices)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=9, minute=10, args=["⏰ Phiên sáng sắp mở lúc 09:15. Bạn tranh thủ xem lại danh mục nhé."], id='notice_open_am', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=11, minute=25, args=["🔔 Phiên sáng sắp kết thúc lúc 11:30."], id='notice_close_am', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=12, minute=55, args=["⏰ Phiên chiều sắp mở lúc 13:00. Chuẩn bị chiến đấu tiếp nhé!"], id='notice_open_pm', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=14, minute=40, args=["🔔 Phiên chiều sắp kết thúc (14:45). Kiểm tra lại các lệnh ATC nhé."], id='notice_close_pm', replace_existing=True)

    # K. EOD Summary (15:00)
    scheduler.add_job(job_eod_summary, 'cron', day_of_week='mon-fri', hour=15, minute=0, id='eod_summary', replace_existing=True)

    # L. Báo cáo CSKH AI Monthly (08:00 ngày 1 hàng tháng)
    scheduler.add_job(
        job_monthly_cskh_report, 
        'cron', 
        day=1, 
        hour=8, 
        minute=0, 
        id='monthly_insight', 
        replace_existing=True
    )

    # Bắt đầu Scheduler
    scheduler.start()
    log.info("✅ APScheduler đã kích hoạt các tác vụ định kỳ.")
    
    # Chạy song song các loop chính
    try:
        await asyncio.gather(
            stock_price_fetcher_loop(),
            alert_loop(),
            #----------------------------
            market_monitor_fetcher_loop(),
            market_monitor_alert_loop(),
            #-------------------------
            worker_inbound_loop(),
        )
    except asyncio.CancelledError:
        log.info(f"[{INSTANCE_ID}] Worker runtime cancelled. Shutting down gracefully...")
        raise
    finally:
        try:
            scheduler.shutdown(wait=False)
            log.info(f"[{INSTANCE_ID}] Scheduler stopped.")
        except Exception as exc:
            log.warning(f"[{INSTANCE_ID}] Scheduler shutdown error: {exc}")


async def main():
    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    log.info(
        f"[{INSTANCE_ID}] Health endpoint: http://0.0.0.0:{PORT}/health"
    )
    await serve(asgi_wrapper_app, config)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped.")