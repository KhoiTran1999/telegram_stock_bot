# worker.py
import asyncio
import json
import datetime
import pytz
import logging
import os
import random
import shutil
from vnstock import Trading, Quote, Finance, Company, Vnstock
import redis
from dotenv import load_dotenv
from agent_tools import TOOL_MAPPING, AGENT_TOOLS_SCHEMA
from ai_knowledge import get_dynamic_system_prompt
import uuid
import time
import pandas as pd
import numpy as np
import inspect

def serialize_gemini_contents_to_openai(contents):
    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]
    
    if not isinstance(contents, list):
        return []
        
    messages = []
    for item in contents:
        # If it's already a dict (OpenAI format)
        if isinstance(item, dict):
            messages.append(item)
            continue
            
        # If it's a Content object
        role = getattr(item, 'role', 'user')
        if role == 'model':
            role = 'assistant'
            
        parts = getattr(item, 'parts', [])
        content_text = ""
        tool_calls = []
        
        # If there's a function_response part, map it to tool role message
        is_tool_response = False
        tool_response_text = ""
        tool_call_id = ""
        
        for part in parts:
            if getattr(part, 'text', None):
                content_text += part.text
            if getattr(part, 'function_call', None) and part.function_call:
                fc = part.function_call
                # Convert args dict to JSON string for OpenAI format
                import json
                try:
                    args_str = json.dumps(fc.args) if isinstance(fc.args, dict) else str(fc.args)
                except:
                    args_str = "{}"
                tool_calls.append({
                    "id": getattr(fc, 'id', 'call_dummy'),
                    "type": "function",
                    "function": {
                        "name": fc.name,
                        "arguments": args_str
                    }
                })
            if getattr(part, 'function_response', None) and part.function_response:
                is_tool_response = True
                fr = part.function_response
                import json
                tool_response_text = json.dumps(fr.response) if isinstance(fr.response, dict) else str(fr.response)
                # Extract call id if we tracked it, otherwise dummy
                tool_call_id = getattr(fr, 'id', 'call_dummy')
                
        if is_tool_response:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": tool_response_text
            })
        else:
            msg = {"role": role}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if content_text or not tool_calls:
                msg["content"] = content_text
            messages.append(msg)
            
    return messages


class Part:
    def __init__(self, text="", function_call=None, function_response=None):
        self.text = text
        self.function_call = function_call
        self.function_response = function_response

class Content:
    def __init__(self, role="user", parts=None):
        self.role = role
        self.parts = parts or []

class FunctionResponse:
    def __init__(self, name, response):
        self.name = name
        self.response = response


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
    get_stock_personalization_map,
    get_all_watch,
    get_all_pro_chat_ids,
    get_bot_active,
    save_bot_message,
    get_users_with_stock_alert_off,
    get_vn30f1m_enabled_map,
    get_vnindex_enabled_map,
    get_vn30_enabled_map,
    get_recent_bctc_notified,
    get_recent_analysis_reports,
    get_user_logs,
    get_user_alert_settings,
    get_all_user_alert_settings,
)
from report_cache import (
    make_report_cache_key,
    save_report_to_redis,
    get_report_from_redis,
    delete_report_from_redis,
)
from ai_knowledge import STATIC_KNOWLEDGE_BASE
from agent_tools import TOOL_MAPPING, AGENT_TOOLS_SCHEMA
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from redis_client import get_redis

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

# Cấu hình Redis Output
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

if REDIS_URL.startswith("redis://") and "?" not in REDIS_URL and "localhost" not in REDIS_URL and "127.0.0.1" not in REDIS_URL:
    # We need to make sure we parse REDIS URL to rediss to fix Render/Aiven redis error
    REDIS_URL = "rediss://" + REDIS_URL[8:]

REDIS_CHANNEL_OUTBOUND = 'telegram_outbound'
REDIS_CHANNEL_INBOUND = 'worker_inbound'
AGENT_TYPES = ("macro", "biz", "tech")
AGENT_RESULT_TTL = 24 * 60 * 60  # 24h
AGENT_BUNDLE_TTL = 7 * 24 * 60 * 60  # 7 ngày
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

# --- Redis helper cho Alert State ---
def get_stock_alert_state(chat_id: int, symbol: str) -> dict:
    """Lấy state cảnh báo của cổ phiếu từ Redis."""
    try:
        r = get_redis()
        key = f"alert_state:stock:{chat_id}:{symbol}"
        raw = r.hgetall(key)
        if raw:
            return {
                "last_pct": float(raw.get("last_pct", 0.0)),
                "last_alert_at": raw.get("last_alert_at", "")
            }
    except Exception as e:
        log.error(f"Redis get stock alert state error: {e}")
    return {"last_pct": 0.0, "last_alert_at": ""}

def save_stock_alert_state(chat_id: int, symbol: str, pct: float, timestamp: str):
    """Lưu state cảnh báo của cổ phiếu vào Redis (hết ngày tự xóa)."""
    try:
        r = get_redis()
        key = f"alert_state:stock:{chat_id}:{symbol}"
        r.hset(key, mapping={"last_pct": str(pct), "last_alert_at": timestamp})
        r.expire(key, 86400)
    except Exception as e:
        log.error(f"Redis save stock alert state error: {e}")

def get_market_alert_state(symbol: str) -> dict:
    """Lấy state chỉ số thị trường (Market Monitor) từ Redis."""
    try:
        r = get_redis()
        key = f"alert_state:index:{symbol}"
        raw = r.hgetall(key)
        if raw and raw.get("anchor") and raw.get("date"):
            return {
                "anchor": float(raw["anchor"]),
                "date": raw["date"]
            }
    except Exception as e:
        log.error(f"Redis get market alert state error: {e}")
    return {"anchor": None, "date": None}

def save_market_alert_state(symbol: str, anchor: float, current_date_str: str):
    """Lưu state chỉ số thị trường vào Redis."""
    try:
        r = get_redis()
        key = f"alert_state:index:{symbol}"
        r.hset(key, mapping={"anchor": str(anchor), "date": current_date_str})
        r.expire(key, 86400)
    except Exception as e:
        log.error(f"Redis save market alert state error: {e}")

def clear_market_alert_state(symbol: str):
    try:
        r = get_redis()
        key = f"alert_state:index:{symbol}"
        r.delete(key)
    except Exception:
        pass

# State Market Monitor (RAM fallback)
_market_data = {
    "VN30F1M": {"price": None, "ref": None, "date": None},
    "VNINDEX": {"price": None, "ref": None, "date": None},
    "VN30":    {"price": None, "ref": None, "date": None},
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
    kwargs = {"decode_responses": True}
    if REDIS_URL.startswith("rediss://"):
        kwargs["ssl_cert_reqs"] = "none"
    r_client = redis.Redis.from_url(REDIS_URL, **kwargs)
    log.info(f"[{INSTANCE_ID}] ✅ Kết nối Redis thành công.")
except Exception as e:
    log.error(f"[{INSTANCE_ID}] ❌ Lỗi kết nối Redis: {e}")
    r_client = None


def _reconnect_redis_client() -> redis.Redis | None:
    """Thử tạo lại Redis client dùng chung."""
    global r_client
    try:
        kwargs = {"decode_responses": True}
        if REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = "none"
        r_client = redis.Redis.from_url(REDIS_URL, **kwargs)
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
    stock_trading = Trading(symbol='VNINDEX', source='VCI')
except:
    stock_trading = None

# --- CẤU HÌNH ROUTER9 (OPENAI COMPATIBLE) ---
ROUTER9_API_KEY = os.getenv("ROUTER9_API_KEY")

from openai import OpenAI, AsyncOpenAI

api_key_safe = ROUTER9_API_KEY or "dummy-key-to-pass-validation"

openai_client = OpenAI(
    api_key=api_key_safe,
    base_url="https://khoitran1999-claude-server.hf.space/v1"
)
openai_async_client = AsyncOpenAI(
    api_key=api_key_safe,
    base_url="https://khoitran1999-claude-server.hf.space/v1"
)

MODEL_BRAIN = os.getenv("AI_MODEL_BRAIN") or os.getenv("MODEL_BRAIN") or "gemini-3.1-pro"
MODEL_WORKER = os.getenv("AI_MODEL_WORKER") or os.getenv("MODEL_WORKER") or "gemini-3.5-Flash"
log.info(f"[{INSTANCE_ID}] Loaded models - Brain: {MODEL_BRAIN}, Worker: {MODEL_WORKER}")

def get_tool_descriptions_for_brain():
    """
    Tạo danh sách mô tả ngắn gọn các công cụ cho Brain (Manager).
    Chỉ lấy Tên và Mô tả, bỏ qua chi tiết tham số kỹ thuật.
    """
    lines = ["Dưới đây là danh sách các CÔNG CỤ (Tools) mà Worker có thể sử dụng:"]
    for tool in AGENT_TOOLS_SCHEMA:
        name = tool.get("name")
        desc = tool.get("description", "")
        lines.append(f"- {name}: {desc}")

    return "\n".join(lines)

def clean_data_robust(data):
    """
    Làm sạch dữ liệu triệt để bằng cách encode/decode JSON.
    Biến đổi mọi kiểu dữ liệu lạ (numpy, pandas timestamp...) thành string hoặc chuẩn Python.
    """
    def default_converter(o):
        return str(o)

    try:
        json_str = json.dumps(data, default=default_converter, ensure_ascii=False)
        return json.loads(json_str)
    except Exception:
        return str(data)

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
            model_id="gemini-2.5-flash", 
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
    import json
    last_error = None
    MAX_RETRIES = 3
    BASE_SLEEP_SEC = 5
    
    messages = []
    if config and "system_instruction" in config:
        messages.append({"role": "system", "content": config["system_instruction"]})
        
    if isinstance(contents, str):
        messages.append({"role": "user", "content": contents})
    elif isinstance(contents, list):
        messages.extend(contents)
        
    kwargs = {
        "model": model_id,
        "messages": messages,
    }
    
    if config:
        if "temperature" in config:
            kwargs["temperature"] = config["temperature"]
            
        if "response_schema" in config:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response_schema",
                    "schema": config["response_schema"],
                    "strict": True
                }
            }
        elif config.get("response_mime_type") == "application/json":
            kwargs["response_format"] = {"type": "json_object"}
            
        if "tools" in config:
            openai_tools = []
            for tool in config["tools"][0]["function_declarations"]:
                openai_tools.append({
                    "type": "function",
                    "function": tool
                })
            kwargs["tools"] = openai_tools

    for attempt in range(MAX_RETRIES):
        try:
            resp = openai_client.chat.completions.create(**kwargs)
            text_result = resp.choices[0].message.content
            if text_result is None: text_result = ""
            if return_usage:
                return text_result, resp.usage
            return text_result
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate_limit" in err_str.lower():
                log.warning(f"[{INSTANCE_ID}] Router9 429. Thử lại...")
            else:
                log.warning(f"[{INSTANCE_ID}] Router9 lỗi: {e}")
            last_error = e
        
        if attempt < MAX_RETRIES - 1:
            import time
            sleep_time = BASE_SLEEP_SEC * (attempt + 1)
            time.sleep(sleep_time)
            
    log.error(f"GỌI ROUTER9 THẤT BẠI. Lỗi: {last_error}")
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


class GeminiPart:
    def __init__(self, text="", function_call=None):
        self.text = text
        self.function_call = function_call

class GeminiContent:
    def __init__(self, role="assistant", parts=None):
        self.role = role
        self.parts = parts or []

class GeminiCandidate:
    def __init__(self, content):
        self.content = content

class GeminiResponseAdapter:
    def __init__(self, openai_resp):
        self._resp = openai_resp
        self.usage_metadata = self._get_usage(openai_resp)
        self.candidates = self._get_candidates(openai_resp)

    @property
    def choices(self):
        return getattr(self._resp, 'choices', [])

    @property
    def usage(self):
        return getattr(self._resp, 'usage', None)

    def _get_usage(self, r):
        class Usage:
            def __init__(self, u):
                self.prompt_token_count = getattr(u, 'prompt_tokens', 0) if u else 0
                self.candidates_token_count = getattr(u, 'completion_tokens', 0) if u else 0
        return Usage(getattr(r, 'usage', None))

    def _get_candidates(self, r):
        if not getattr(r, 'choices', None): return []
        choice = r.choices[0]
        parts = []
        
        # Text content
        if getattr(choice.message, 'content', None):
            parts.append(GeminiPart(text=choice.message.content))
            
        # Tool calls
        if getattr(choice.message, 'tool_calls', None):
            for tc in choice.message.tool_calls:
                import json
                try:
                    args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                except:
                    args = {}
                
                class GeminiFunctionCall:
                    def __init__(self, name, arguments, id):
                        self.name = name
                        self.args = arguments
                        self.id = id
                        
                        class MockFunction:
                            def __init__(self, n, a):
                                self.name = n
                                self.arguments = a
                        self.function = MockFunction(name, arguments)
                        
                parts.append(GeminiPart(function_call=GeminiFunctionCall(tc.function.name, args, tc.id)))
                
        return [GeminiCandidate(GeminiContent(role="assistant", parts=parts))]

async def run_autonomous_agent(chat_id, user_query, loading_msg_id=None):
    """
    Hierarchical Agent Architecture (Manager-Worker Pattern).
    - Manager (Brain): gemini-2.5-pro -> Lập kế hoạch & Trả lời cuối cùng.
    - Worker (Doer): gemini-2.0-flash-lite -> Thực thi Tools & Tổng hợp dữ liệu thô.
    - Có cơ chế Fast-Track (Trả lời nhanh).
    """
    log.info(f"[{INSTANCE_ID}] 🤖 Hierarchical Agent Start: {chat_id}")
    
    # Khởi tạo thống kê Token
    stats = {"requests": 0, "input_tokens": 0, "output_tokens": 0}

    def update_stats(response):
        if response and hasattr(response, "usage") and response.usage:
            stats["requests"] += 1
            stats["input_tokens"] += getattr(response.usage, "prompt_tokens", 0)
            stats["output_tokens"] += getattr(response.usage, "completion_tokens", 0)

    # Hàm helper để tạo footer thống kê (Chỉ Admin mới thấy)
    def get_admin_stats_footer():
        if chat_id == ADMIN_ID:
            return (
                f"\n\n`⚙️ Specs: {stats['requests']} calls | "
                f"In: {stats['input_tokens']} | Out: {stats['output_tokens']}`"
            )
        return ""

    # --- HELPER: GỌI GEMINI AN TOÀN (RETRY & ROTATE KEY & LOGGING) ---
    async def safe_generate_content(model_id, contents, config=None):
        import json
        last_error = None
        MAX_RETRIES = 3
        
        messages = []
        if config and "system_instruction" in config:
            messages.append({"role": "system", "content": config["system_instruction"]})
            
        messages.extend(serialize_gemini_contents_to_openai(contents))
            
        kwargs = {
            "model": model_id,
            "messages": messages,
        }
        
        if config:
            if "temperature" in config:
                kwargs["temperature"] = config["temperature"]
                
            if "response_schema" in config:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response_schema",
                        "schema": config["response_schema"],
                        "strict": True
                    }
                }
            elif config.get("response_mime_type") == "application/json":
                kwargs["response_format"] = {"type": "json_object"}
                
            if "tools" in config:
                openai_tools = []
                for tool in config["tools"][0]["function_declarations"]:
                    openai_tools.append({
                        "type": "function",
                        "function": tool
                    })
                kwargs["tools"] = openai_tools

        for attempt in range(MAX_RETRIES):
            try:
                response = await openai_async_client.chat.completions.create(**kwargs)
                return GeminiResponseAdapter(response)
            except Exception as e:
                last_error = e
                log.warning(f"[{INSTANCE_ID}] Router9 Error (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
                import asyncio
                await asyncio.sleep(2)
                
        log.error(f"[{INSTANCE_ID}] Router9 Max Retries Reached.")
        return None

    # ==========================================================================
    # GIAI ĐOẠN 1: MANAGER LẬP KẾ HOẠCH (BRAIN - PRO)
    # ==========================================================================
    try:
        # Context thời gian thực
        sys_instruction = get_dynamic_system_prompt()

        # Lấy danh sách tool dạng tóm tắt
        tool_menu = get_tool_descriptions_for_brain()
        
        # Prompt cho Manager: Chỉ giao việc, không làm
        
        # [NEW] Lấy lịch sử chat gần đây (3 tin nhắn gần nhất)
        history_context = ""
        try:
            # Lấy 3 log gần nhất. Note: get_user_logs trả về DESC (mới nhất trước)
            # Ta cần lấy cả message hiện tại cũng không sao, AI sẽ tự hiểu.
            # Tuy nhiên, nếu message hiện tại chưa kịp log xuống DB thì user_query sẽ là mới nhất.
            logs_data = get_user_logs(chat_id, limit=5)
            rows = logs_data.get('rows', [])
            
            # Đảo ngược lại để đúng thứ tự thời gian: Cũ -> Mới
            rows.reverse()
            
            history_msgs = []
            for r in rows:
                if r.get('note'):
                    history_msgs.append(f"- User: {r.get('note')}")
            
            if history_msgs:
                history_str = "\n".join(history_msgs)
                history_context = f"\nLỊCH SỬ CHAT GẦN ĐÂY:\n{history_str}\n"
            
            log.info(f"[{INSTANCE_ID}] 📜 Loaded History ({len(history_msgs)} items): {history_msgs}")  # <--- DEBUG LOG

        except Exception as e:
            log.warning(f"Failed to load chat history: {e}")

        manager_prompt = f"""
        Bạn là Manager Agent thông minh của Bot Chứng khoán.
        {history_context}
        User Query (Hiện tại): "{user_query}"

        ⚠️ [QUAN TRỌNG - BỘ NHỚ CONTEXT]:
        Nếu câu hỏi của User không có chủ ngữ hoặc thiếu Mã Cổ Phiếu (ví dụ: "giá sao rồi?", "lợi nhuận thế nào?", "vẽ biểu đồ đi", "con này ổn không?"), bạn BẮT BUỘC phải tìm lại trong "LỊCH SỬ CHAT GẦN ĐÂY" để xác định mã cổ phiếu gần nhất mà user đang đề cập.
        > Ví dụ:
        > - History: "Xem giúp HPG"
        > - Current: "Giá bao nhiêu?"
        > -> Hiểu là: "Giá HPG bao nhiêu?" -> ACTION: "Lấy giá HPG..."

        DƯỚI ĐÂY LÀ KIẾN THỨC NỀN TẢNG CỦA BẠN (FAQ/Help):
        {STATIC_KNOWLEDGE_BASE}

        DANH SÁCH CÔNG CỤ (TOOLS) CÓ THỂ DÙNG (Chỉ dùng khi cần dữ liệu realtime):
        {tool_menu}

        NHIỆM VỤ: Phân tích yêu cầu và chọn 1 trong 2 hành động:

        TRƯỜNG HỢP 1: TRẢ LỜI NGAY (Fast Track)
        Nếu câu hỏi là:
        - Chào hỏi xã giao (Hello, Hi...).
        - Hỏi về tính năng bot, cách dùng lệnh (/add, /start...), giá gói cước.
        - Các câu hỏi mở về tài chính, kinh tế vĩ mô, lý thuyết phân tích kỹ thuật/cơ bản, định nghĩa các chỉ số P/E, P/B,...
        - Các câu hỏi thảo luận, nhận định hoặc so sánh chung mà KHÔNG yêu cầu tra cứu số liệu giá thời gian thực (real-time price) hôm nay.
        => Hãy tự trả lời trực tiếp bằng kiến thức tài chính sâu rộng của bạn.
        => OUTPUT FORMAT: Bắt đầu bằng từ khóa "ANSWER:" theo sau là câu trả lời.
        Lưu ý: Trình bày chuyên nghiệp, súc tích và có chiều sâu.

        TRƯỜNG HỢP 2: GỌI WORKER (Data Fetch)
        Nếu câu hỏi yêu cầu dữ liệu thực tế:
        - Lấy giá cổ phiếu realtime hoặc thông tin khớp lệnh, khối lượng giao dịch hiện tại của cổ phiếu hôm nay.
        Nhiệm vụ: Chỉ đạo nhân viên Worker lấy dữ liệu giá cụ thể.
        => OUTPUT FORMAT: Bắt đầu bằng từ khóa "ACTION:" theo sau là lệnh chi tiết.
        Ví dụ: "Hãy lấy giá khớp lệnh hiện tại và khối lượng giao dịch của HPG."
        """
        
        resp_manager = await safe_generate_content(
            model_id=MODEL_BRAIN, # gemini-2.5-pro
            contents=manager_prompt,
            config={"system_instruction": sys_instruction}
        )
        update_stats(resp_manager)
        
        if not resp_manager or not getattr(resp_manager, "candidates", None):
            log.error(f"[{INSTANCE_ID}] ⚠️ Gemini trả về response không có candidates: {resp_manager}")
            push_telegram_msg(chat_id, "⚠️ AI không phản hồi (có thể do bộ lọc an toàn). Vui lòng hỏi lại.", edit_id=loading_msg_id)
            return

        manager_output = resp_manager.candidates[0].content.parts[0].text.strip()
        
        log.info(f"[{INSTANCE_ID}] 📝 Instruction: {manager_output}")
        # --- [SỬA ĐỔI] LOGIC RẼ NHÁNH ---
        
        # NHÁNH 1: TRẢ LỜI NGAY (FAST TRACK)
        if manager_output.startswith("ANSWER:") or "ACTION:" not in manager_output:
            final_answer = manager_output.replace("ANSWER:", "").strip()
            
            # Gửi ngay lập tức và KẾT THÚC
            kb = default_ai_reply_markup()
            push_telegram_msg(chat_id, final_answer + get_admin_stats_footer(), reply_markup=kb, edit_id=loading_msg_id)
            return  # <--- THOÁT HÀM NGAY TẠI ĐÂY

        # NHÁNH 2: CẦN DỮ LIỆU -> TIẾP TỤC GỌI WORKER
        worker_instruction = manager_output.replace("ACTION:", "").strip()
        push_telegram_msg(chat_id, f"🔍 **Người Canh Bảng 🧑‍💻 đang tra cứu dữ liệu...**", edit_id=loading_msg_id)

    except Exception as e:
        log.error(f"Manager Error: {e}")
        push_telegram_msg(chat_id, "⚠️ Lỗi khi lập kế hoạch.", edit_id=loading_msg_id)
        return

    # ==========================================================================
    # GIAI ĐOẠN 2: WORKER THỰC THI & TỔNG HỢP (DOER - LITE)
    # ==========================================================================
    
    # Worker có context riêng, không lẫn với Manager
    worker_history = [
        Content(role="user", parts=[Part(text=f"NHIỆM VỤ CỦA BẠN: {worker_instruction}\nHãy sử dụng các công cụ được cung cấp để thu thập dữ liệu chính xác.")])
    ]
    
    worker_config = {
        "tools": [{"function_declarations": AGENT_TOOLS_SCHEMA}], # Import từ agent_tools.py
        "system_instruction": sys_instruction + "\n\nVAI TRÒ CỦA BẠN: Bạn là Worker (Researcher) chăm chỉ. Nhiệm vụ: Gọi tool lấy dữ liệu thô."
    }

    MAX_LOOPS = 10  # Giới hạn số vòng lặp của Worker
    refined_data = "Không thu thập được dữ liệu."
    
    for i in range(MAX_LOOPS):
        try:
            # 2.1 Worker suy nghĩ & gọi tool
            response = await safe_generate_content(
                model_id=MODEL_WORKER, # gemini-2.0-flash-lite
                contents=worker_history,
                config=worker_config
            )
            update_stats(response)
            
            ai_content = response.candidates[0].content
            worker_history.append(ai_content)
            
            # Check function call
            current_calls = []

            # --- [FIX 1] Kiểm tra parts tồn tại trước khi lặp ---
            if ai_content and ai_content.parts:
                for part in ai_content.parts:
                    if part.function_call:
                        current_calls.append(part.function_call)
            
            # Nếu không gọi tool nữa -> Chuyển sang bước tổng hợp
            if not current_calls:
                break
                
            # Log cho user biết đang gọi tool gì (UX)
            tool_names = ", ".join([fc.function.name.replace("get_", "").replace("_", " ").title() for fc in current_calls])
            push_telegram_msg(chat_id, f"🛠 **Người Canh Bảng 🧑‍💻:** Đang tra cứu {tool_names}...", edit_id=loading_msg_id)
            
            # 2.2 Thực thi Tool
            for idx, fc in enumerate(current_calls):
                # Thêm khoảng nghỉ ngắn (1.5s) giữa các lệnh gọi API để tránh Rate Limit của VCI
                if idx > 0:
                    await asyncio.sleep(1.5)

                tool_name = fc.function.name
                tool_args = fc.function.arguments
                
                tool_result = "Error: Tool not found"
                if tool_name in TOOL_MAPPING: 
                    try:
                        import json
                        args_dict = json.loads(tool_args) if isinstance(tool_args, str) else {k: v for k, v in tool_args.items()}
                        func = TOOL_MAPPING[tool_name]
                        
                        if inspect.iscoroutinefunction(func):
                            raw_result = await func(**args_dict)
                        else:
                            raw_result = func(**args_dict)
                        
                        tool_result = clean_data_robust(raw_result)
        
                    except Exception as e:
                        log.error(f"[{INSTANCE_ID}] ❌ Lỗi khi xử lý Tool {tool_name}: {e}")
                        tool_result = f"Tool Error: {str(e)}"
                
                import json
                worker_history.append({
                    "role": "tool",
                    "tool_call_id": fc.id,
                    "content": json.dumps({"result": tool_result}, ensure_ascii=False)
                })
            
        except Exception as e:
            log.error(f"Worker Loop Error ({i}): {e}")
            break

    # ==========================================================================
    # GIAI ĐOẠN 2.5: WORKER TỔNG HỢP (SYNTHESIS)
    # ==========================================================================
    try:
        push_telegram_msg(chat_id, "📝 **Người Canh Bảng 🧑‍💻:** Đang tổng hợp & làm sạch dữ liệu...", edit_id=loading_msg_id)
        
        # Yêu cầu Worker tóm tắt lại toàn bộ quá trình tìm kiếm
        synthesis_prompt = f"""
        Bạn đã hoàn thành việc thu thập dữ liệu.
        Dựa trên NHIỆM VỤ BAN ĐẦU: "{worker_instruction}" và các DỮ LIỆU THÔ (JSON) ở trên.
        
        Hãy viết một bản "BÁO CÁO TỔNG HỢP" ngắn gọn, súc tích gửi cho Manager.
        
        QUY TẮC BẮT BUỘC:
        1. TRUNG THỰC TUYỆT ĐỐI: Chỉ báo cáo dựa trên dữ liệu tool trả về. 
           - Nếu tool chỉ trả về Quý 3, bạn PHẢI nói rõ là "Chỉ có dữ liệu Quý 3", KHÔNG ĐƯỢC tự bịa là đã có Quý 4.
           - Nếu không tìm thấy thông tin, hãy báo là "Không tìm thấy".
        2. Loại bỏ các trường null, thông tin rác.
        3. Trình bày dạng bullet points.
        """
        
        worker_history.append(Content(role="user", parts=[Part(text=synthesis_prompt)]))
        
        # Tắt tool ở bước này để Worker tập trung viết text
        synthesis_config = {"temperature": 0.2} # Giảm nhiệt độ để tăng tính chính xác (factual)
        
        resp_syn = await safe_generate_content(
            model_id=MODEL_WORKER, # Vẫn là Flash Lite (Rẻ)
            contents=worker_history,
            config=synthesis_config
        )
        update_stats(resp_syn)
        
        refined_data = resp_syn.choices[0].message.content

    except Exception as e:
        log.error(f"Synthesis Error: {e}")
        refined_data = "Lỗi khi tổng hợp dữ liệu. Manager hãy tự xem xét nếu có thể."

    # ==========================================================================
    # GIAI ĐOẠN 3: MANAGER TRẢ LỜI (BRAIN - PRO)
    # ==========================================================================
    try:
        push_telegram_msg(chat_id, "💡 *Người Canh Bảng 🧑‍💻:* Đang soạn câu trả lời cuối cùng...", edit_id=loading_msg_id)
        
        final_system_prompt = get_dynamic_system_prompt()
        
        # Context mới hoàn toàn cho Manager (sạch sẽ, không chứa raw json)
        manager_final_prompt = f"""
        User Query: "{user_query}"
        
        Dưới đây là BÁO CÁO DỮ LIỆU đã được chuyên viên nghiên cứu (Worker) tổng hợp:
        --- BẮT ĐẦU BÁO CÁO ---
        {refined_data}
        --- KẾT THÚC BÁO CÁO ---
        
        Dựa vào báo cáo trên và kiến thức của bạn:
        1. Trả lời trực tiếp câu hỏi của User.
           - QUAN TRỌNG: Nếu Báo cáo chỉ có Quý 3, bạn PHẢI trả lời là "Hiện chỉ có dữ liệu đến Quý 3". TUYỆT ĐỐI KHÔNG được bịa số liệu Quý 4.
           - Nếu thông tin trong báo cáo là "Không tìm thấy", hãy trả lời trung thực và khuyên user kiểm tra lại sau.
        2. Đưa ra nhận định, lời khuyên (nếu phù hợp và CÓ dữ liệu chứng minh).
        3. Giọng văn chuyên nghiệp, thân thiện.
        """
        resp_final = await safe_generate_content(
            model_id=MODEL_BRAIN, # gemini-2.5-pro
            contents=manager_final_prompt,
            config={"system_instruction": final_system_prompt, "temperature": 0.5} # Giảm nhiệt độ
        )
        update_stats(resp_final)
        
        if not resp_final or not getattr(resp_final, "candidates", None):
            log.error(f"[{INSTANCE_ID}] ⚠️ Gemini Final Answer trả về không có candidates: {resp_final}")
            push_telegram_msg(chat_id, "⚠️ AI không thể tổng hợp câu trả lời cuối cùng. Vui lòng thử lại.", edit_id=loading_msg_id)
            return

        final_answer = resp_final.choices[0].message.content
        
        # Gửi kết quả cuối cùng
        kb = default_ai_reply_markup()
        push_telegram_msg(chat_id, final_answer + get_admin_stats_footer(), reply_markup=kb, edit_id=loading_msg_id)

    except Exception as e:
        log.error(f"Final Answer Error: {e}")
        push_telegram_msg(chat_id, "⚠️ Lỗi khi tạo câu trả lời cuối cùng.", edit_id=loading_msg_id)

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
    # Vô hiệu hoá hoàn toàn việc cào GSO thô để tránh tạo file rác trên disk và tiết kiệm tài nguyên
    note = "Tính năng phân tích vĩ mô GSO thô tạm thời ngưng hoạt động."
    payload = _build_agent_stub("macro", request_id, ts_iso)
    empty_stats = {
        "gso_period": "N/A",
        "attempts": [],
        "csv_path": None,
        "gso_data_dir": GSO_DATA_DIR,
        "errors": [note],
    }
    payload.update({
        "notes": note,
        "raw_data": empty_stats,
        "redis_json": {
            "generated_at": ts_iso,
            "status": "DISABLED",
            "gso_data_dir": GSO_DATA_DIR,
            "attempts": [],
            "message": note,
        },
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
                        log.info(f"[{INSTANCE_ID}] 📥 Redis Message Received: {message}")
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

                        # Xóa CMD_RUN_WEEKLY_NOW

                        # elif cmd == "RUN_NIGHTLY_VALUATION":
                        #     admin_id = payload.get('admin_id')
                        #     log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Nightly Valuation từ {admin_id}")
                        #     asyncio.create_task(job_nightly_valuation())

                        # Xóa CMD_RUN_DAILY_DIGEST

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

                            asyncio.create_task(process_screener_view(chat_id, loading_id))

                        # elif cmd == "FORCE_SCREENER":
                        #     admin_id = payload.get('admin_id')
                        #     asyncio.create_task(process_force_update_screener(admin_id))

                        elif cmd == "RUN_MONTHLY_INSIGHT":
                            admin_id = payload.get('admin_id')
                            log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Monthly Insight")
                            asyncio.create_task(job_monthly_cskh_report())

                        elif cmd == "CMD_AGENT_RUN":
                            asyncio.create_task(handle_agent_run(payload))

                        elif cmd == "CMD_ASK_AI":
                            chat_id = payload.get('chat_id')
                            question = payload.get('question')
                            loading_id = payload.get('loading_msg_id')
                            # Chuyển sang dùng Agent Autonomous
                            asyncio.create_task(run_autonomous_agent(chat_id, question, loading_id))

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

def sync_sectors_to_redis():
    """
    Đọc sectors.json và đồng bộ vào Redis ZSET (profile_crawl_queue).
    - Mã mới: Thêm vào với Score = 0 (Ưu tiên làm ngay).
    - Mã cũ: Giữ nguyên Score (Để bảo toàn lịch sử crawl).
    """
    try:
        # 1. Load sectors.json
        sector_map = load_symbol_sector_map() # Hàm này đã có trong worker.py
        if not sector_map:
            log.warning(f"[{INSTANCE_ID}] [SYNC] sectors.json rỗng hoặc lỗi.")
            return

        r = ensure_redis_client()
        if not r: return

        pipeline = r.pipeline()
        count_new = 0
        
        # 2. Thêm vào ZSET với chế độ NX (Only if Not Exist)
        for symbol in sector_map.keys():
            # ZADD key nx=True score=0 member=symbol
            # nx=True nghĩa là chỉ thêm nếu chưa có, nếu có rồi thì không sửa Score
            pipeline.zadd("profile_crawl_queue", {symbol: 0}, nx=True)
            count_new += 1
            
        results = pipeline.execute()
        added = sum(1 for res in results if res) # Đếm số mã thực sự được thêm mới
        
        log.info(f"[{INSTANCE_ID}] [SYNC] Đã đồng bộ {len(sector_map)} mã vào Queue. Thêm mới: {added} mã.")

    except Exception as e:
        log.error(f"[{INSTANCE_ID}] [SYNC] Lỗi đồng bộ sectors: {e}")



# ==============================================
# TIN TỨC (RSS)
# ==============================================

#===============================================
# --- [NEW] Helper trích xuất ảnh từ RSS ---
def extract_image_from_rss_entry(entry) -> str | None:
    """
    Trích xuất URL ảnh từ entry RSS.
    Ưu tiên: media_content -> description (img tag) -> content_encoded
    """
    # 1. Thử lấy từ media_content (VnEconomy hay dùng cái này)
    if hasattr(entry, 'media_content'):
        for media in entry.media_content:
            if media.get('medium') == 'image' and media.get('url'):
                return media.get('url')

    # 2. Parse HTML trong description hoặc content (Vietstock hay dùng cái này)
    html_content = getattr(entry, 'description', '') or getattr(entry, 'content', [{'value': ''}])[0].get('value', '')
    
    if html_content:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            img_tag = soup.find('img')
            if img_tag and img_tag.get('src'):
                return img_tag.get('src')
        except Exception:
            pass
            
    return None

def fetch_rss_entries_for_urls(urls: list[str]) -> list[dict[str, Any]]:
    """
    Đọc danh sách RSS, trả về list các dict kèm ẢNH MINH HỌA.
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

                # [NEW] Trích xuất ảnh
                image_url = extract_image_from_rss_entry(entry)

                published_dt = None
                published_parsed = getattr(entry, "published_parsed", None)
                if published_parsed:
                    try:
                        ts = time.mktime(published_parsed)
                        published_dt = datetime.datetime.fromtimestamp(ts, vn_tz)
                    except Exception:
                        published_dt = None

                if link in by_link:
                    # Gộp source nếu trùng link
                    old_source = by_link[link].get("source") or ""
                    if source_title and source_title not in old_source:
                        by_link[link]["source"] = (
                            old_source + " | " + source_title if old_source else source_title
                        )
                    continue

                by_link[link] = {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "image": image_url, # <--- Trường mới
                    "published": published_dt,
                    "source": source_title,
                }

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][RSS] Lỗi đọc RSS {url}: {e}")

    items = list(by_link.values())

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
    [FIXED] Chỉ sử dụng nguồn VCI. 
    Tự động xử lý giá cho Cổ phiếu (price_board) và Index (history 1m).
    """
    global _vci_blocked_date
    results = {}
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_date = now.date()
    today_str = now.strftime('%Y-%m-%d')

    def _norm_price(p):
        if p is None or pd.isna(p): return 0.0
        p = float(p)
        if 0 < p < 500: return p * 1000.0
        return p

    # Nếu VCI bị chặn IP trong ngày hôm nay, tạm dừng để tránh spam
    if _vci_blocked_date == today_date:
        return results

    try:
        # 1. LẤY GIÁ QUA PRICE BOARD (Nhanh, phù hợp cho cổ phiếu)
        def _run_vci_board():
            t = Trading(symbol='VNINDEX', source='VCI')
            return t.price_board(symbols)
        
        df_board = await asyncio.wait_for(asyncio.to_thread(_run_vci_board), timeout=15.0)
        
        if df_board is not None and not df_board.empty:
            for _, row in df_board.iterrows():
                try:
                    # Xử lý lấy Symbol linh hoạt
                    sym = str(row.get(('listing', 'symbol'), row.get('symbol', ''))).upper().strip()
                    if not sym: continue
                    
                    # Ưu tiên lấy giá khớp, nếu bằng 0 lấy giá tham chiếu
                    match_p = _norm_price(row.get('close_price'))
                    ref_p = _norm_price(row.get(('listing', 'ref_price')))
                    
                    if match_p == 0 and ref_p > 0: 
                        match_p = ref_p
                    
                    pct = ((match_p - ref_p) / ref_p * 100) if ref_p > 0 else 0.0
                    results[sym] = {"price": match_p, "pct": pct}
                except: continue

        # 2. XỬ LÝ RIÊNG CHO INDEX HOẶC MÃ BỊ THIẾU (Dùng History 1m)
        # Các mã Index (VNINDEX, VN30...) thường trả về 0 ở price_board của VCI
        missing_or_index = [s for s in symbols if s in ['VNINDEX', 'VN30', 'VN30F1M'] or s not in results or results[s]["price"] == 0]
        
        if missing_or_index:
            for sym in missing_or_index:
                try:
                    q = Quote(symbol=sym, source='VCI')
                    # Lấy nến 1 phút mới nhất
                    df_1m = await asyncio.to_thread(q.history, start=today_str, end=today_str, interval='1m')
                    
                    if df_1m is not None and not df_1m.empty:
                        curr_p = float(df_1m.iloc[-1]['close'])
                        if sym not in ['VNINDEX', 'VN30', 'VN30F1M'] and curr_p < 500:
                            curr_p *= 1000
                            
                        # Nếu đã có ref_p từ board thì giữ, không thì coi như pct = 0 hoặc lấy từ nến ngày (nếu cần)
                        old_ref = results.get(sym, {}).get("pct", 0) # Tạm giữ pct cũ nếu có
                        results[sym] = {"price": curr_p, "pct": old_ref}
                except: continue

    except Exception as e:
        # Nếu bị lỗi Rate Limit (thường là 429), đánh dấu để switch sang nghỉ ngơi
        if "429" in str(e) or "Rate limit" in str(e):
            _vci_blocked_date = today_date
            log.warning(f"[{INSTANCE_ID}] VCI Rate Limit. Tạm dừng fetcher trong ngày hôm nay.")
        log.error(f"[{INSTANCE_ID}] fetch_data_smart Error: {e}")
        
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
    global _stock_alert_disabled_cache
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Alert Loop...")

    # Load danh sách chặn alert lần đầu
    _stock_alert_disabled_cache = await asyncio.to_thread(get_users_with_stock_alert_off)

    market_just_opened_delay_done = False

    while True:
        now = datetime.datetime.now(vn_tz)

        if not get_bot_active() or not in_session_vietnam():
            market_just_opened_delay_done = False
            await asyncio.sleep(60)
            continue

        # [MARKET OPEN DELAY]
        if now.hour == 9 and now.minute == 15 and not market_just_opened_delay_done:
            log.info(f"[{INSTANCE_ID}] ⏳ Thị trường vừa mở cửa, đợi 60s trước khi tính toán alert...")
            await asyncio.sleep(60)
            market_just_opened_delay_done = True
            continue

        try:
            quote_cache = _stock_current_price_cache
            all_watch = _stock_current_watch_cache

            if not quote_cache:
                await asyncio.sleep(5)
                continue

            # Lấy danh sách Pro (để lọc limit user thường)
            pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
            # Lấy toàn bộ settings của users để áp dụng Threshold và Silent Alerts
            all_alert_settings = await asyncio.to_thread(get_all_user_alert_settings)
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

                user_settings = all_alert_settings.get(chat_id, {"stock_alert_threshold": 2.0, "silent_alerts": False})
                threshold = user_settings["stock_alert_threshold"]
                is_silent = user_settings["silent_alerts"]

                messages = []
                tech_followups: list[dict[str, Any]] = []
                buttons = { "inline_keyboard": [] } # Cấu trúc nút bấm Telegram

                for sym in processing_list:
                    sym_u = str(sym).upper()
                    quote = quote_cache.get(sym_u)
                    if not quote: continue

                    price = quote.get('price')
                    pct = quote.get('pct')

                    # Lấy state cảnh báo từ Redis
                    state = await asyncio.to_thread(get_stock_alert_state, chat_id, sym_u)
                    last_pct = state.get('last_pct', 0.0)

                    # [FIX RESTART STORM] Khởi tạo anchor ban đầu = pct hiện tại (thay vì 0) nếu đây là lần đầu chạy trong phiên
                    if state.get("last_alert_at") == "" and pct != 0:
                        # Đã trong phiên mà state chưa có -> gắn anchor hiện tại luôn, không báo động
                        await asyncio.to_thread(save_stock_alert_state, chat_id, sym_u, pct, now.isoformat())
                        continue

                    delta = float(pct) - float(last_pct)

                    if abs(delta) >= threshold:
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
                        await asyncio.to_thread(save_stock_alert_state, chat_id, sym_u, pct, now.isoformat())

                # Bắn tin sang Redis nếu có biến động
                if messages:
                    header = f"⏰ *Cảnh báo {now.strftime('%H:%M')}*"
                    body = "\n".join(messages) + "\n" + header

                    # GỌI HÀM PUSH THAY VÌ GỬI TRỰC TIẾP
                    push_telegram_msg(
                        chat_id=chat_id,
                        text=body,
                        reply_markup=buttons if buttons["inline_keyboard"] else None,
                        msg_type="STOCK_ALERT",
                        silent=is_silent
                    )
                    log.info(f"🔔 Pushed alert for {chat_id} (Threshold: {threshold}%)")

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
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)

    config = MARKET_MONITORS.get(symbol)
    if not config: return None

    state = _market_data.get(symbol)
    if not state: return None

    # [PERSIST STATE] Đọc anchor từ Redis thay vì RAM
    redis_state = await asyncio.to_thread(get_market_alert_state, symbol)
    anchor = redis_state.get("anchor")
    date_str = redis_state.get("date")

    ref_price = state["ref"]

    if ref_price is None:
        return None

    # Khởi tạo anchor từ Redis
    if anchor is None or date_str != now.strftime('%Y-%m-%d'):
        # [FIX RESTART STORM] Gán anchor bằng giá hiện tại nếu đang trong phiên (sau 9h15)
        # Ngược lại, nếu chưa giao dịch thì gán bằng giá tham chiếu
        if now.hour > 9 or (now.hour == 9 and now.minute >= 15):
            anchor = float(price)
            log.info(f"[{symbol}] Market already open, set initial anchor to CURRENT price = {anchor}")
        else:
            anchor = float(ref_price)
            log.info(f"[{symbol}] Pre-market, set initial anchor to REF price = {anchor}")

        await asyncio.to_thread(save_market_alert_state, symbol, anchor, now.strftime('%Y-%m-%d'))
        return None

    delta_trigger = float(price) - float(anchor)
    threshold = config["threshold"]

    # Trigger nếu biến động >= threshold
    if abs(delta_trigger) >= threshold:
        delta_display = float(price) - float(ref_price)
        direction = "tăng" if delta_display > 0 else "giảm"
        icon = "🟢" if delta_display > 0 else "🔴"
        trend_icon = "🚀" if delta_display > 0 else "📉"
        now_str = now.strftime("%H:%M:%S")

        text = (
            f"{icon} *{symbol} {direction} {abs(delta_display):.1f} điểm*\n"
            f"Giá hiện tại: *{float(price):.1f}*\n"
            f"(So với TC: {ref_price:.1f})\n"
            f"{trend_icon} _Cập nhật lúc {now_str}_"
        )

        # Cập nhật mốc anchor mới vào Redis
        await asyncio.to_thread(save_market_alert_state, symbol, float(price), now.strftime('%Y-%m-%d'))
        log.info(f"[{symbol}] 🔔 Trigger! {price} (Delta: {delta_trigger})")
        return text

    return None

async def market_monitor_fetcher_loop():
    """
    [WORKER] Loop lấy giá VN30F1M, VNINDEX, VN30 sử dụng hoàn toàn nguồn VCI.
    - p_now: Lấy từ nến 1 phút mới nhất (interval='1m').
    - p_ref: Lấy từ nến ngày của phiên giao dịch trước đó (interval='1D').
    """
    global _market_data
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Market Monitor Fetcher (VCI Optimized)...")

    while True:
        now = datetime.datetime.now(vn_tz)
        
        if not get_bot_active():
            await asyncio.sleep(30)
            continue
        
        if not in_session_vietnam():
            next_start = next_session_start(now)
            await asyncio.sleep((next_start - now).total_seconds())
            continue

        try:
            today_str = now.strftime('%Y-%m-%d')
            
            async def _fetch_one(symbol):
                p_now = None
                p_ref = None
                
                try:
                    # Khởi tạo Quote với nguồn VCI
                    quote = Quote(symbol=symbol, source='VCI')
                    
                    # 1. LẤY GIÁ HIỆN TẠI (p_now) - Dùng nến 1 phút
                    # VNINDEX/VN30 thường không có giá trong price_board nên dùng cách này là chuẩn nhất
                    df_1m = await asyncio.to_thread(quote.history, start=today_str, end=today_str, interval='1m')
                    if df_1m is not None and not df_1m.empty:
                        p_now = float(df_1m.iloc[-1]['close'])
                    
                    # 2. LẤY GIÁ THAM CHIẾU (p_ref) - Dùng nến ngày phiên trước
                    state = _market_data.get(symbol)
                    # Chỉ lấy lại Ref nếu trong state chưa có hoặc sang ngày mới
                    if state and (state["ref"] is None or state["date"] != now.date()):
                        # Lấy lịch sử 7 ngày để chắc chắn cover được ngày nghỉ/lễ
                        start_hist = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
                        df_day = await asyncio.to_thread(quote.history, start=start_hist, end=today_str, interval='1D')
                        
                        if df_day is not None and len(df_day) >= 2:
                            # Nếu dòng cuối cùng là nến ngày hôm nay, p_ref là dòng liền trước (hôm qua)
                            last_date_in_df = str(df_day.iloc[-1]['time']).split()[0]
                            if last_date_in_df == today_str:
                                p_ref = float(df_day.iloc[-2]['close'])
                            else:
                                # Nếu hôm nay chưa có nến ngày, p_ref là dòng cuối cùng (hôm qua)
                                p_ref = float(df_day.iloc[-1]['close'])
                                
                except Exception as e:
                    log.warning(f"[{INSTANCE_ID}] Lỗi fetch {symbol} từ VCI: {e}")
                
                return symbol, p_now, p_ref

            # Chạy song song cho tất cả các chỉ số (VN30F1M, VNINDEX, VN30)
            tasks = [_fetch_one(sym) for sym in MARKET_MONITORS.keys()]
            results = await asyncio.gather(*tasks)

            for sym, p_now, p_ref in results:
                state = _market_data.get(sym)
                if not state: continue

                if p_now:
                    state["price"] = p_now
                    
                    if p_ref:
                        state["ref"] = p_ref
                        # Khởi tạo anchor bằng Ref nếu đây là lần đầu chạy trong ngày
                        if state["anchor"] is None: 
                            state["anchor"] = p_ref
                            log.info(f"[{sym}] Set anchor initial: {p_ref}")

        except Exception as e:
            log.error(f"Market Fetch Error: {e}")

        # Chu kỳ fetch 10 giây để đảm bảo không bị VCI chặn IP (Rate limit)
        await asyncio.sleep(10)
async def market_monitor_alert_loop():
    """
    Loop kiểm tra và bắn tin cảnh báo chung.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    log.info(f"[{INSTANCE_ID}] 🚀 Bắt đầu Market Monitor Alert Loop...")

    market_just_opened_delay_done = False

    while True:
        now = datetime.datetime.now(vn_tz)
        _market_reset_if_new_day(now)

        if not get_bot_active() or not in_session_vietnam():
            _market_clear_after_close()
            market_just_opened_delay_done = False
            await asyncio.sleep(60)
            continue

        # [MARKET OPEN DELAY]
        if now.hour == 9 and now.minute == 15 and not market_just_opened_delay_done:
            log.info(f"[{INSTANCE_ID}] ⏳ Thị trường vừa mở cửa, đợi 60s trước khi tính toán market monitor...")
            await asyncio.sleep(60)
            market_just_opened_delay_done = True
            continue

        try:
            # Lấy toàn bộ settings của users để áp dụng Silent Alerts
            all_alert_settings = await asyncio.to_thread(get_all_user_alert_settings)

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
                            # [SILENT ALERT]
                            user_settings = all_alert_settings.get(chat_id, {"silent_alerts": False})
                            is_silent = user_settings["silent_alerts"]

                            push_telegram_msg(
                                chat_id=chat_id,
                                text=alert_text,
                                msg_type=msg_type,
                                silent=is_silent
                            )
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

# async def calculate_market_comprehensive_data():
#     """
#     TÁC VỤ NẶNG (Nightly):
#     1. Tính P/E, P/B trung bình 5 năm (Mean Reversion).
#     2. Tính hiệu suất giá (12 tuần, 6 tháng).
#     3. Tổng hợp chỉ số ngành (Sector Performance).
#     4. Lưu tất cả vào Redis để WebApp/Bot dùng chung.
#     """
#     log.info(f"[{INSTANCE_ID}] 🏗️ Bắt đầu Job tổng hợp dữ liệu thị trường (Valuation + Performance)...")
    
#     try:
#         # 1. Chuẩn bị dữ liệu đầu vào
#         sector_map = await asyncio.to_thread(load_symbol_sector_map)
        
#         # Lấy danh sách mã từ Screener (Lọc thanh khoản & Vốn hóa)
#         screener = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700))
        
#         MIN_MARKET_CAP = 5000 
#         MIN_TRADING_VAL = 50 # [UPDATED] Tăng điều kiện thanh khoản lên 50 tỷ
#         screener['market_cap'] = pd.to_numeric(screener['market_cap'], errors='coerce').fillna(0)
#         liq_col = 'total_trading_value' if 'total_trading_value' in screener.columns else 'avg_trading_value_20d'
#         screener[liq_col] = pd.to_numeric(screener[liq_col], errors='coerce').fillna(0)

#         valid_df = screener[
#             (screener['market_cap'] >= MIN_MARKET_CAP) & 
#             (screener[liq_col] >= MIN_TRADING_VAL)
#         ]
#         valid_tickers = valid_df['ticker'].tolist()
#         log.info(f"[{INSTANCE_ID}] Danh sách cần xử lý: {len(valid_tickers)} mã.")

#         price_hints: dict[str, float] = {}
#         for _, row in valid_df.iterrows():
#             sym = str(row['ticker']).strip().upper()
#             hint = _extract_price_from_screener_row(row)
#             if sym and hint:
#                 price_hints[sym] = hint

#         # Cấu trúc dữ liệu lưu Redis
#         # {
#         #    "stocks": { "HPG": { "pe_avg":..., "change_6m":..., "sector":... }, ... },
#         #    "sectors": { "Thép": { "change_6m":..., "count":... }, ... },
#         #    "updated_at": "..."
#         # }
#         stocks_data = {}
#         sector_accumulators = {} # { "Thép": {"sum_12w": 0, "sum_6m": 0, "count": 0} }

#         consecutive_errors = 0
#         manual_alerts: list[str] = []
        
#         # 2. Loop xử lý từng mã (Batching)
#         BATCH_SIZE = 5
#         BATCH_SLEEP = 60

#         for i, sym in enumerate(valid_tickers):
#             # Log progress
#             log.info(f"[{INSTANCE_ID}] Processing {i+1}/{len(valid_tickers)}: {sym}")

#             # Rate Limit (Batching)
#             if i > 0 and i % BATCH_SIZE == 0:
#                 log.info(f"[{INSTANCE_ID}] 💤 Đã xong batch {BATCH_SIZE} mã. Nghỉ {BATCH_SLEEP}s để hồi API...")
#                 await asyncio.sleep(BATCH_SLEEP)

#             if consecutive_errors > 5:
#                 log.warning(f"[{INSTANCE_ID}] ⚠️ Bị chặn liên tục. Ngủ 120s...")
#                 await asyncio.sleep(120)
#                 consecutive_errors = 0

#             try:
#                 # --- A. Fetch Dữ liệu (Chạy song song Ratio & History) ---
#                 async def _fetch_ratio():
#                     return await asyncio.to_thread(lambda: Finance(symbol=sym, source='VCI').ratio(period='year', lang='vi'))
                
#                 async def _fetch_history():
#                     # Lấy 190 ngày để đảm bảo đủ 6 tháng (khoảng 180 ngày)
#                     end_d = datetime.datetime.now()
#                     start_d = end_d - datetime.timedelta(days=190)
#                     q = Quote(symbol=sym, source='VCI')
#                     return await asyncio.to_thread(q.history, start=start_d.strftime('%Y-%m-%d'), end=end_d.strftime('%Y-%m-%d'), interval='1D')

#                 # Chạy song song 2 request để tiết kiệm thời gian
#                 fin_df, hist_df = await asyncio.gather(_fetch_ratio(), _fetch_history())
                
#                 # --- B. Xử lý Valuation (P/E, P/B Avg) ---
#                 val_stats = {}
#                 if fin_df is not None and not fin_df.empty:
#                     fin_df = _clean_vnstock_columns(fin_df)
#                     df_5y = fin_df.head(5)
#                     pe_s = pd.to_numeric(df_5y.get('pe', []), errors='coerce')
#                     pb_s = pd.to_numeric(df_5y.get('pb', []), errors='coerce')
#                     pe_s = pe_s[pe_s > 0]
#                     pb_s = pb_s[pb_s > 0]
                    
#                     if len(pe_s) >= 3: val_stats['pe_avg'] = pe_s.mean()
#                     if len(pb_s) >= 3: val_stats['pb_avg'] = pb_s.mean()

#                 # --- C. Xử lý Performance (12W, 6M) ---
#                 perf_stats = {}
#                 if hist_df is not None and not hist_df.empty:
#                     # Chuẩn hóa cột
#                     hist_df.columns = hist_df.columns.str.lower().str.strip()
#                     if 'time' in hist_df.columns: hist_df['time'] = pd.to_datetime(hist_df['time'])
#                     hist_df = hist_df.sort_values('time')
                    
#                     closes = pd.to_numeric(hist_df['close'], errors='coerce')
#                     dates = hist_df['time'].tolist()
                    
#                     if len(closes) > 0:
#                         price_now = closes.iloc[-1]
#                         date_now = dates[-1]
                        
#                         # Hàm tìm giá tại thời điểm T - days
#                         def _get_change(days_back):
#                             target_date = date_now - datetime.timedelta(days=days_back)
#                             # Tìm ngày gần nhất trong quá khứ (<= target_date)
#                             # Vì list đã sort, ta tìm ngược từ dưới lên
#                             idx = -1
#                             for k in range(len(dates)-1, -1, -1):
#                                 if dates[k] <= target_date:
#                                     idx = k
#                                     break
                            
#                             if idx != -1 and closes.iloc[idx] > 0:
#                                 p_old = closes.iloc[idx]
#                                 return ((price_now - p_old) / p_old) * 100
#                             return None

#                         perf_stats['change_12w'] = _get_change(84)  # 12 tuần ~ 84 ngày
#                         perf_stats['change_6m'] = _get_change(180) # 6 tháng ~ 180 ngày

#                 manual_payload: dict[str, Any] = {}
#                 try:
#                     manual_result = await asyncio.to_thread(
#                         fetch_manual_pe_pb,
#                         sym,
#                         use_cache=True,
#                         price=price_hints.get(sym),
#                     )
#                 except Exception as exc:
#                     log.warning(f"[{INSTANCE_ID}] Manual valuation fatal for {sym}: {exc}")
#                     manual_result = None
#                 if manual_result:
#                     manual_fields = {
#                         "pe_manual": manual_result.pe,
#                         "pb_manual": manual_result.pb,
#                         "manual_price": manual_result.price,
#                         "manual_eps_ttm": manual_result.eps_ttm,
#                         "manual_bvps": manual_result.bvps,
#                         "manual_updated_at": manual_result.computed_at,
#                     }
#                     for key, value in manual_fields.items():
#                         if value is not None:
#                             manual_payload[key] = value
#                     if manual_result.error:
#                         manual_payload["manual_error"] = manual_result.error
#                     if manual_result.needs_admin_alert and manual_result.error:
#                         manual_alerts.append(f"{sym}: {manual_result.error}")

#                 # --- D. Tổng hợp ---
#                 if val_stats or perf_stats or manual_payload:
#                     sector_name = sector_map.get(sym, "Khác")
                    
#                     item_data = {
#                         "sector": sector_name,
#                         **val_stats,
#                         **perf_stats,
#                         **manual_payload,
#                     }
#                     stocks_data[sym] = item_data
                    
#                     # Cộng dồn cho Sector (Chỉ tính nếu có dữ liệu)
#                     if sector_name != "Khác":
#                         if sector_name not in sector_accumulators:
#                             sector_accumulators[sector_name] = {"sum_12w": 0.0, "cnt_12w": 0, "sum_6m": 0.0, "cnt_6m": 0}
                        
#                         acc = sector_accumulators[sector_name]
                        
#                         if perf_stats.get('change_12w') is not None:
#                             acc['sum_12w'] += perf_stats['change_12w']
#                             acc['cnt_12w'] += 1
                            
#                         if perf_stats.get('change_6m') is not None:
#                             acc['sum_6m'] += perf_stats['change_6m']
#                             acc['cnt_6m'] += 1

#                     consecutive_errors = 0
                
#                 # Delay nhẹ
#                 await asyncio.sleep(2.0)

#             except BaseException as e:
#                 # [FIX] Check cancellation first
#                 if isinstance(e, asyncio.CancelledError):
#                     raise e

#                 # Bắt cả SystemExit do vnstock raise khi bị Rate Limit
#                 consecutive_errors += 1
#                 err_str = str(e)
                
#                 # Check SystemExit explicitly
#                 is_system_exit = isinstance(e, SystemExit) or type(e).__name__ == 'SystemExit'
                
#                 if "Rate limit exceeded" in err_str or is_system_exit:
#                     log.warning(f"[{INSTANCE_ID}] ⚠️ Rate Limit Hit ({sym}) - {type(e).__name__}. Ngủ 60s...")
#                     await asyncio.sleep(60.0)
#                 else:
#                     log.warning(f"Lỗi xử lý {sym}: {type(e).__name__} - {e}")
#                     await asyncio.sleep(2.0)

#         # 3. Tính chỉ số ngành (Trung bình cộng)
#         sectors_final = {}
#         for sec_name, acc in sector_accumulators.items():
#             avg_12w = (acc['sum_12w'] / acc['cnt_12w']) if acc['cnt_12w'] > 0 else None
#             avg_6m = (acc['sum_6m'] / acc['cnt_6m']) if acc['cnt_6m'] > 0 else None
            
#             sectors_final[sec_name] = {
#                 "change_12w": avg_12w,
#                 "change_6m": avg_6m,
#                 "count": max(acc['cnt_12w'], acc['cnt_6m'])
#             }

#         # --- [NEW] Thêm VNINDEX vào danh sách Sector ---
#         try:
#             log.info(f"[{INSTANCE_ID}] Đang lấy dữ liệu VNINDEX...")
#             end_d = datetime.datetime.now()
#             start_d = end_d - datetime.timedelta(days=190)
            
#             # Hàm lấy history VNINDEX
#             def _get_vnindex_hist():
#                 q = Quote(symbol='VNINDEX', source='VCI')
#                 return q.history(start=start_d.strftime('%Y-%m-%d'), end=end_d.strftime('%Y-%m-%d'), interval='1D')

#             vnindex_df = await asyncio.to_thread(_get_vnindex_hist)
            
#             if vnindex_df is not None and not vnindex_df.empty:
#                 vnindex_df.columns = vnindex_df.columns.str.lower().str.strip()
#                 if 'time' in vnindex_df.columns: vnindex_df['time'] = pd.to_datetime(vnindex_df['time'])
#                 vnindex_df = vnindex_df.sort_values('time')
                
#                 closes = pd.to_numeric(vnindex_df['close'], errors='coerce')
#                 dates = vnindex_df['time'].tolist()
                
#                 if len(closes) > 0:
#                     price_now = closes.iloc[-1]
#                     date_now = dates[-1]
                    
#                     def _get_change_idx(days_back):
#                         target_date = date_now - datetime.timedelta(days=days_back)
#                         idx = -1
#                         for k in range(len(dates)-1, -1, -1):
#                             if dates[k] <= target_date:
#                                 idx = k
#                                 break
#                         if idx != -1 and closes.iloc[idx] > 0:
#                             p_old = closes.iloc[idx]
#                             return ((price_now - p_old) / p_old) * 100
#                         return None

#                     vn_12w = _get_change_idx(84)
#                     vn_6m = _get_change_idx(180)
                    
#                     sectors_final['VNINDEX'] = {
#                         "change_12w": vn_12w,
#                         "change_6m": vn_6m,
#                         "count": 1
#                     }
#                     log.info(f"[{INSTANCE_ID}] ✅ Đã thêm VNINDEX: 12W={vn_12w:.1f}%, 6M={vn_6m:.1f}%")
#         except Exception as e:
#             log.warning(f"[{INSTANCE_ID}] ⚠️ Lỗi lấy VNINDEX: {e}")

#         # 4. Lưu Redis
#         final_payload = {
#             "updated_at": datetime.datetime.now().isoformat(),
#             "stocks": stocks_data,
#             "sectors": sectors_final
#         }
        
#         await asyncio.to_thread(save_historical_valuation_to_redis, final_payload)
#         log.info(f"[{INSTANCE_ID}] ✅ Hoàn tất Comprehensive Data. Đã lưu {len(stocks_data)} mã và {len(sectors_final)} ngành.")

#         if manual_alerts and ADMIN_ID:
#             deduped = list(dict.fromkeys(manual_alerts))
#             preview = "\n".join(deduped[:10])
#             remainder = ""
#             if len(deduped) > 10:
#                 remainder = f"\n... và {len(deduped) - 10} mã khác."
#             push_telegram_msg(
#                 ADMIN_ID,
#                 "⚠️ Manual PE/PB thiếu dữ liệu:\n" + preview + remainder,
#                 msg_type="SYSTEM_MSG",
#             )

#     except BaseException as e:
#         if isinstance(e, asyncio.CancelledError):
#             raise e
#         log.error(f"[{INSTANCE_ID}] ❌ LỖI NGHIÊM TRỌNG (Comprehensive Task): {type(e).__name__} - {e}")
#         await asyncio.sleep(60)

# async def get_top_mean_reversion_stocks(limit=5):
#     """
#     Lấy Top cổ phiếu rẻ nhất (Mean Reversion) từ Redis (Cấu trúc mới).
#     """
#     try:
#         # 1. Lấy dữ liệu từ Redis
#         full_data = await asyncio.to_thread(get_historical_valuation_from_redis)
        
#         # Nếu chưa có hoặc format cũ -> chạy lại task đồng bộ để có dữ liệu
#         if not full_data or "stocks" not in full_data:
#             lock = await _get_comprehensive_lock()
#             async with lock:
#                 # Re-check after acquiring lock to avoid duplicate jobs
#                 full_data = await asyncio.to_thread(get_historical_valuation_from_redis)
#                 if full_data and "stocks" in full_data:
#                     log.info(f"[{INSTANCE_ID}] Dữ liệu Comprehensive đã có sau khi chờ lock.")
#                 else:
#                     log.warning(f"[{INSTANCE_ID}] Redis chưa có dữ liệu Comprehensive. Đang chạy tính toán đồng bộ...")
#                     try:
#                         await calculate_market_comprehensive_data()
#                     except Exception as exc:
#                         log.error(f"[{INSTANCE_ID}] Lỗi tính toán Comprehensive tức thời: {exc}")
#                         return []
#                     full_data = await asyncio.to_thread(get_historical_valuation_from_redis)

#             if not full_data or "stocks" not in full_data:
#                 log.error(f"[{INSTANCE_ID}] Không lấy được dữ liệu Comprehensive sau khi tính toán.")
#                 return []

#         hist_data = full_data["stocks"] # Lấy phần stocks

#         processed_items = []
        
#         for sym, stock_info in hist_data.items():
#             pe_avg = stock_info.get('pe_avg')
#             pb_avg = stock_info.get('pb_avg')
#             if not pe_avg or not pb_avg:
#                 continue

#             pe_cur = stock_info.get('pe_manual')
#             pb_cur = stock_info.get('pb_manual')
#             if pe_cur is None or pb_cur is None:
#                 manual = await asyncio.to_thread(fetch_manual_pe_pb, sym)
#                 pe_cur = manual.pe
#                 pb_cur = manual.pb
#                 if manual.needs_admin_alert and manual.error:
#                     log.warning(f"[{INSTANCE_ID}] Manual valuation missing for {sym}: {manual.error}")

#             if not pe_cur or not pb_cur:
#                 continue
#             if pe_cur <= 0 or pb_cur <= 0:
#                 continue
#             if pe_avg <= 0 or pb_avg <= 0:
#                 continue

#             # --- TÍNH TOÁN LOGIC ---
#             pe_discount = (pe_cur - pe_avg) / pe_avg
#             pb_discount = (pb_cur - pb_avg) / pb_avg
#             avg_discount = (pe_discount + pb_discount) / 2
            
#             # Helper định dạng UI
#             def get_ui_meta(discount):
#                 pct_val = abs(discount) * 100
#                 if discount < -0.1: return "diff-good", f"▼ {pct_val:.1f}%"
#                 elif discount > 0.1: return "diff-bad", f"▲ {pct_val:.1f}%"
#                 else: 
#                     sign = "▲" if discount > 0 else "▼"
#                     return "", f"{sign} {pct_val:.1f}%"

#             pe_class, pe_diff_str = get_ui_meta(pe_discount)
#             pb_class, pb_diff_str = get_ui_meta(pb_discount)
            
#             if avg_discount < -0.1: signal_class, signal_text = "sig-cheap", "Định giá Rẻ"
#             elif avg_discount > 0.1: signal_class, signal_text = "sig-expensive", "Đắt"
#             else: signal_class, signal_text = "sig-fair", "Hợp lý"
            
#             processed_items.append({
#                 "symbol": sym,
#                 "avg_discount_raw": avg_discount,
#                 "pe_cur": f"{pe_cur:.1f}", "pe_avg": f"{pe_avg:.1f}",
#                 "pe_class": pe_class, "pe_diff_str": pe_diff_str,
#                 "pb_cur": f"{pb_cur:.1f}", "pb_avg": f"{pb_avg:.1f}",
#                 "pb_class": pb_class, "pb_diff_str": pb_diff_str,
#                 "signal_class": signal_class, "signal_text": signal_text
#             })

#         # 3. Sắp xếp (Rẻ nhất lên đầu)
#         processed_items.sort(key=lambda x: x['avg_discount_raw'])
#         return processed_items[:limit]

#     except Exception as e:
#         log.error(f"[{INSTANCE_ID}] Lỗi get_top_mean_reversion_stocks: {e}")
#         return []

# --- NEW HELPERS FOR PERSONALIZED DIGEST ---
AI_SEMAPHORE = asyncio.Semaphore(3)
_comprehensive_lock: asyncio.Lock | None = None


async def _get_comprehensive_lock() -> asyncio.Lock:
    global _comprehensive_lock
    if _comprehensive_lock is None:
        _comprehensive_lock = asyncio.Lock()
    return _comprehensive_lock

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
        fin = Finance(symbol=sym, source="VCI")
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
    Cập nhật: Lưu metadata vào Redis để phục vụ Web App Digest.
    """
    log.info(f"[NEWS] 📰 Bắt đầu job quét tin {feed_type}...")
    
    if not get_bot_active():
        return

    try:
        # 1. Xác định URL
        urls = []
        if feed_type == "MACRO":
            urls = RSS_FEEDS_MACRO
        elif feed_type == "SPECIALIZED":
            for group_urls in RSS_FEEDS_SPECIALIZED.values():
                urls.extend(group_urls)
        
        if not urls: return

        # 2. Fetch
        entries = await asyncio.to_thread(fetch_rss_entries_for_urls, urls)
        if len(entries) > NEWS_MAX_RSS_ENTRIES_PER_RUN:
            entries = entries[:NEWS_MAX_RSS_ENTRIES_PER_RUN]

        vn_tz = pytz.timezone(TIMEZONE)
        scan_now = datetime.datetime.now(vn_tz)
        new_count = 0

        # 3. Lọc và Lưu
        for it in entries:
            link = (it.get("link") or "").strip()
            if not link: continue
            
            if not is_fresh_news(it.get("published"), scan_now): continue
            
            # Check DB (SQL) - Logic cũ để lọc tin đã gửi
            is_seen = await asyncio.to_thread(has_news_seen, feed_type, link)
            
            if not is_seen:
                # A. Lưu vào SQL (để đánh dấu đã đọc)
                await asyncio.to_thread(
                    mark_news_seen, 
                    feed_type, 
                    link=it["link"], 
                    guid=None, 
                    title=it["title"], 
                    published=it["published"]
                )
                
                # B. [NEW] Lưu Metadata vào Redis (để hiển thị Web App có ảnh)
                # Key: news_meta:<link_hash> hoặc đơn giản là news_meta:<feed_type>:<md5_link>
                # Ở đây ta dùng 1 key hash lớn hoặc set riêng lẻ. Để đơn giản, dùng key riêng với TTL 48h.
                try:
                    import hashlib
                    link_hash = hashlib.md5(link.encode()).hexdigest()
                    meta_key = f"news_meta:{link_hash}"
                    
                    # Convert datetime to string for JSON
                    pub_str = it["published"].isoformat() if it["published"] else ""
                    
                    meta_payload = {
                        "title": it["title"],
                        "link": link,
                        "image": it.get("image"),
                        "source": it.get("source"),
                        "summary": it.get("summary"),
                        "published": pub_str,
                        "type": feed_type
                    }
                    # Lưu Redis 48h
                    if r_client:
                        r_client.set(meta_key, json.dumps(meta_payload), ex=48 * 3600)
                except Exception as ex:
                    log.warning(f"Redis save meta error: {ex}")

                new_count += 1
        
        if new_count > 0:
            log.info(f"[NEWS] ✅ {feed_type}: Đã lưu {new_count} tin mới (SQL + Redis).")
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
            model_id="gemini-2.5-flash",
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


def job_listener(event):
    try:
        if event.exception:
            error_msg = f"🚨 **WORKER CRITICAL ERROR**\nJob ID: `{event.job_id}`\nLỗi: `{event.exception}`"
            log.error(error_msg)
            push_telegram_msg(ADMIN_ID, error_msg, msg_type="SYSTEM_MSG")
        elif event.code == EVENT_JOB_MISSED:
            warning_msg = f"⚠️ **MISSED JOB**: Job `{event.job_id}` đã bị bỏ qua do quá hạn."
            log.warning(warning_msg)
            push_telegram_msg(ADMIN_ID, warning_msg, msg_type="SYSTEM_MSG")
    except Exception as e:
        log.error(f"Lỗi trong job_listener: {e}")


async def run_worker_runtime():
    log.info(f"[{INSTANCE_ID}] Worker starting (Advanced APScheduler Mode)...")

    # --- 1. CẤU HÌNH REDIS JOB STORE ---
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    if redis_url.startswith("redis://") and "?" not in redis_url and "localhost" not in redis_url and "127.0.0.1" not in redis_url:
        redis_url = "rediss://" + redis_url[8:]

    connection_kwargs = {}
    if redis_url.startswith("rediss://"):
        connection_kwargs["ssl_cert_reqs"] = "none"

    pool = redis.ConnectionPool.from_url(
        redis_url,
        **connection_kwargs
    )

    jobstores = {
        'default': RedisJobStore(
            jobs_key='apscheduler.jobs',
            run_times_key='apscheduler.run_times',
            connection_pool=pool
        )
    }

    job_defaults = {
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': 600
    }

    scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        job_defaults=job_defaults,
        timezone=TIMEZONE
    )

    scheduler.add_listener(job_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    # A. Quét Tin tức (06:00, 18:00)
    scheduler.add_job(job_scan_news, 'cron', hour='6,18', args=["MACRO"], id='news_macro', replace_existing=True)
    scheduler.add_job(job_scan_news, 'cron', hour='6,18', args=["SPECIALIZED"], id='news_spec', replace_existing=True)

    # B. Dọn dẹp (03:30)
    scheduler.add_job(job_maintenance, 'cron', hour=3, minute=30, id='maintenance_daily', replace_existing=True)

    # C. Thông báo Phiên (Session Notices)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=9, minute=10, args=["⏰ Phiên sáng sắp mở lúc 09:15. Bạn tranh thủ xem lại danh mục nhé."], id='notice_open_am', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=11, minute=25, args=["🔔 Phiên sáng sắp kết thúc lúc 11:30."], id='notice_close_am', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=12, minute=55, args=["⏰ Phiên chiều sắp mở lúc 13:00. Chuẩn bị chiến đấu tiếp nhé!"], id='notice_open_pm', replace_existing=True)
    scheduler.add_job(job_session_notice, 'cron', day_of_week='mon-fri', hour=14, minute=40, args=["🔔 Phiên chiều sắp kết thúc (14:45). Kiểm tra lại các lệnh ATC nhé."], id='notice_close_pm', replace_existing=True)

    # D. EOD Summary (15:00)
    scheduler.add_job(job_eod_summary, 'cron', day_of_week='mon-fri', hour=15, minute=0, id='eod_summary', replace_existing=True)

    # E. Báo cáo CSKH AI Monthly (08:00 ngày 1 hàng tháng)
    scheduler.add_job(job_monthly_cskh_report, 'cron', day=1, hour=8, minute=0, id='monthly_insight', replace_existing=True)

    # Bắt đầu Scheduler
    scheduler.start()
    log.info("✅ APScheduler đã kích hoạt các tác vụ định kỳ.")

    # Sync 1 lần lúc khởi động
    try:
        await asyncio.to_thread(sync_sectors_to_redis)
    except Exception as e:
        log.error(f"Sync Sector Error: {e}")

    # Chạy song song các loop chính
    try:
        await asyncio.gather(
            stock_price_fetcher_loop(),
            alert_loop(),
            market_monitor_fetcher_loop(),
            market_monitor_alert_loop(),
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