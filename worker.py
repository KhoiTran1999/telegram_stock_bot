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
import tempfile
import urllib3
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin # Để parse REDIS_URL
from datetime import timedelta
from chart_utils import generate_mini_chart, draw_sector_performance_chart
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
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "https://google.com") # URL của Gateway

# Cấu hình Redis Output
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CHANNEL_OUTBOUND = 'telegram_outbound'
REDIS_CHANNEL_INBOUND = 'worker_inbound'
AGENT_TYPES = ("macro", "biz", "tech")
AGENT_RESULT_TTL = 24 * 60 * 60  # 24h
AGENT_BUNDLE_TTL = 7 * 24 * 60 * 60  # 7 ngày
MACRO_NEWS_LOOKBACK_HOURS = 48
MACRO_GSO_MONTH_LIMIT = 3
GSO_BASE_URL = "https://www.nso.gov.vn"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Cấu hình Loop
FETCHER_INTERVAL_SECONDS = 20
TICKER_INTERVAL_SECONDS = 10

# Cache cục bộ của Worker
_stock_current_price_cache = {} 
_stock_current_watch_cache = {}
_stock_alert_disabled_cache = set()
ALERT_STATE = {}

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

def call_gemini_safe(model_id, contents, config=None, return_usage=False):
    """Hàm gọi Gemini an toàn (Failover) với cơ chế Round Robin (Xoay vòng từng request)"""
    global _gemini_key_index
    last_error = None
    
    # [UPDATED] Cơ chế Round Robin: Chia bài đều lần lượt cho từng Key
    # Đảm bảo Key A vừa dùng xong sẽ không bị gọi lại ngay ở request tiếp theo (trừ khi chỉ có 1 key).
    if GEMINI_KEYS:
        # Lấy vị trí bắt đầu dựa trên biến đếm
        start_idx = _gemini_key_index % len(GEMINI_KEYS)
        
        # Tạo danh sách ưu tiên bắt đầu từ key đó: [Key_2, Key_3, ..., Key_1]
        rotated_keys = GEMINI_KEYS[start_idx:] + GEMINI_KEYS[:start_idx]
        
        # Tăng biến đếm cho lần gọi sau
        _gemini_key_index += 1
    else:
        rotated_keys = []

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


async def collect_index_pair() -> dict:
    now = datetime.datetime.now()
    vni_data, v30_data = await asyncio.gather(
        asyncio.to_thread(get_index_snapshot, 'VNINDEX', now),
        asyncio.to_thread(get_index_snapshot, 'VN30', now)
    )
    return {"vnindex": vni_data, "vn30": v30_data}


async def collect_macro_news(limit: int = 20) -> list[dict]:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    since = now_utc - datetime.timedelta(hours=MACRO_NEWS_LOOKBACK_HOURS)
    rows = await asyncio.to_thread(get_recent_news_seen, "MACRO", since)
    news = []
    for title, link, published, created_at in rows:
        news.append({
            "title": title,
            "link": link,
            "published": published.isoformat() if published else None,
            "created_at": created_at.isoformat() if created_at else None,
        })
        if len(news) >= limit:
            break
    return news


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


async def run_macro_agent(chat_id: int, request_id: str, ts_iso: str) -> dict:
    gso_reports = await asyncio.to_thread(collect_gso_reports_sync)
    redis_payload = {
        "generated_at": ts_iso,
        "gso_reports": gso_reports,
    }

    payload = _build_agent_stub("macro", request_id, ts_iso)
    payload.update({
        "notes": f"GSO raw data snapshot lúc {ts_iso} (không AI summary)",
        "raw_data": {
            "gso_count": len(gso_reports),
        },
        "redis_json": redis_payload,
    })

    file_path = write_agent_payload_to_file("macro", request_id, payload)
    payload["debug_file_path"] = file_path
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

    target_agents = AGENT_TYPES if scope == "all" else (scope,)

    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    ts_iso = now.isoformat()

    agent_outputs = {}
    for agent_type in target_agents:
        try:
            if agent_type == "macro":
                result = await run_macro_agent(chat_id, request_id, ts_iso)
            else:
                result = _build_agent_stub(agent_type, request_id, ts_iso)
        except Exception as exc:
            log.error(f"Agent {agent_type} run error: {exc}")
            result = _build_agent_stub(agent_type, request_id, ts_iso)
            result["notes"] = f"Lỗi thực thi: {exc}"[:200]

        agent_outputs[agent_type] = result
        save_agent_result(agent_type, result)

    bundle = {
        "chat_id": chat_id,
        "request_id": request_id,
        "generated_at": ts_iso,
        "scope": scope,
        "agents": agent_outputs,
        "ai_summary": "AI summary chưa bật. Dữ liệu đang được kiểm tra thủ công.",
    }

    save_agent_bundle(chat_id, bundle)

    message = format_agent_bundle_message(chat_id, request_id, scope, bundle)
    push_telegram_msg(chat_id, message, msg_type="ADMIN_AGENT")

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
    """
    Xử lý câu hỏi CSKH bằng Gemini Flash Lite.
    [UPDATED] Có bộ nhớ hội thoại (Context Aware) lưu trong Redis.
    """
    log.info(f"[{INSTANCE_ID}] 🤖 AI CSKH: {chat_id} - '{question}'")
    
    try:
        # 1. Lấy lịch sử từ Redis (Context Memory)
        history_key = f"ai_history:{chat_id}"
        history_context = ""
        
        if r_client:
            # Lấy 10 dòng gần nhất (tương đương 5 cặp hỏi-đáp)
            # Redis List: [User: A, Bot: B, User: C, Bot: D...]
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
            return_usage=True
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
            except: pass

        # 4. Lưu hội thoại mới vào Redis (để AI nhớ cho lần sau)
        if r_client:
            # Lưu câu hỏi của User
            r_client.rpush(history_key, f"User: {question}")
            
            # Lưu câu trả lời của Bot (Cần xóa phần debug token nếu có)
            clean_answer = answer.split("\n\n`[DEBUG]")[0]
            
            # [MỚI] Xóa Markdown khi lưu vào lịch sử để tiết kiệm token & sạch context
            history_text = remove_markdown(clean_answer)
            r_client.rpush(history_key, f"Bot: {history_text}")
            
            # Giới hạn lịch sử: Chỉ giữ 20 tin gần nhất (10 cặp) để tiết kiệm Token
            r_client.ltrim(history_key, -20, -1)
            
            # Set thời gian hết hạn cho bộ nhớ (24 giờ)
            r_client.expire(history_key, 86400)

        # 5. Gửi kết quả về Gateway
        kb = {
            "inline_keyboard": [[
                {"text": "🏠 Dashboard", "callback_data": "back_to_start"},
                {"text": "❓ Hướng dẫn", "callback_data": "menu_help"}
            ]]
        }
        
        push_telegram_msg(
            chat_id=chat_id,
            text=answer,
            reply_markup=kb,
            edit_id=loading_msg_id 
        )

    except Exception as e:
        log.error(f"AI CSKH Error: {e}")
        push_telegram_msg(
            chat_id=chat_id,
            text="⚠️ Lỗi hệ thống AI. Vui lòng thử lại sau.",
            edit_id=loading_msg_id
        )

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

                    # [MỚI] Xử lý lệnh chạy Nightly Valuation ngay lập tức
                    elif cmd == "RUN_NIGHTLY_VALUATION":
                        admin_id = payload.get('admin_id')
                        log.info(f"[{INSTANCE_ID}] 📥 Nhận lệnh Force Run Nightly Valuation từ {admin_id}")
                        asyncio.create_task(job_nightly_valuation())

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

                    elif cmd == "CMD_AGENT_RUN":
                        asyncio.create_task(handle_agent_run(payload))

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
                # 1. Giá khớp (Live 1m)
                q = Quote(symbol=symbol, source='VCI')
                df = await asyncio.to_thread(q.history, start=today_str, end=today_str, interval='1m')
                p_now = float(df.iloc[-1]['close']) if df is not None and not df.empty else None
                
                # 2. Giá tham chiếu (Ref)
                # Logic: Nếu chưa có Ref, thử tìm trong history hôm qua hoặc board
                p_ref = None
                state = _market_data.get(symbol)
                
                if state and state["ref"] is None:
                    # A. Thử lấy từ Board (Ưu tiên cho VN30F1M)
                    if symbol == "VN30F1M" and stock_trading:
                        try:
                            row = stock_trading.price_board([symbol]).iloc[0]
                            val = row.get(('listing', 'ref_price')) or row.get('ref_price')
                            if val: p_ref = float(val)
                        except: pass
                    
                    # B. Nếu chưa có, thử lấy Close hôm qua từ History
                    if p_ref is None:
                        start_prev = (now - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
                        df_daily = await asyncio.to_thread(q.history, start=start_prev, end=today_str, interval='1D')
                        if df_daily is not None and len(df_daily) >= 2:
                            # Lấy close của phiên trước đó (iloc[-2])
                            p_ref = float(df_daily.iloc[-2]['close'])
                
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
            model_id="gemini-2.5-flash-lite",
            contents=prompt,
            config={'response_mime_type': 'application/json'}
        )
        return extract_json_from_text(json_text)
    except Exception as e:
        log.error(f"Summarize News Error: {e}")
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

        # Cấu trúc dữ liệu lưu Redis
        # {
        #    "stocks": { "HPG": { "pe_avg":..., "change_6m":..., "sector":... }, ... },
        #    "sectors": { "Thép": { "change_6m":..., "count":... }, ... },
        #    "updated_at": "..."
        # }
        stocks_data = {}
        sector_accumulators = {} # { "Thép": {"sum_12w": 0, "sum_6m": 0, "count": 0} }

        consecutive_errors = 0
        
        # 2. Loop xử lý từng mã (Batching)
        BATCH_SIZE = 10
        BATCH_SLEEP = 30

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

                # --- D. Tổng hợp ---
                if val_stats or perf_stats:
                    sector_name = sector_map.get(sym, "Khác")
                    
                    item_data = {
                        "sector": sector_name,
                        **val_stats,
                        **perf_stats
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
                await asyncio.sleep(1.0)

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
        
        # Nếu chưa có hoặc format cũ -> chạy lại task
        if not full_data or "stocks" not in full_data:
            log.warning(f"[{INSTANCE_ID}] Redis chưa có dữ liệu Comprehensive. Đang kích hoạt tính toán...")
            asyncio.create_task(calculate_market_comprehensive_data())
            return []

        hist_data = full_data["stocks"] # Lấy phần stocks

        # 2. Lấy dữ liệu hiện tại từ Screener API
        screener_df = await asyncio.to_thread(lambda: Screener().stock(params={"exchangeName": "HOSE,HNX,UPCOM"}, limit=1700))
        
        processed_items = []
        
        for index, row in screener_df.iterrows():
            sym = row['ticker']
            if sym not in hist_data: continue
            
            stock_info = hist_data[sym]
            
            # Check có đủ dữ liệu PE/PB avg không
            if 'pe_avg' not in stock_info or 'pb_avg' not in stock_info:
                continue

            try:
                pe_cur = float(row['pe'])
                pb_cur = float(row['pb'])
            except: continue

            pe_avg = stock_info['pe_avg']
            pb_avg = stock_info['pb_avg']
            
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

# --- NEW HELPERS FOR PERSONALIZED DIGEST ---
AI_SEMAPHORE = asyncio.Semaphore(3)

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
        
        # 4. Call AI
        if not input_news: return None
        return await summarize_daily_news_with_ai(input_news)

    
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
            top_value_stocks
        ) = await asyncio.gather(
            asyncio.to_thread(get_recent_bctc_notified, since_utc),
            asyncio.to_thread(get_recent_analysis_reports, since_utc),
            asyncio.to_thread(get_recent_news_seen, "MACRO", since_utc),
            asyncio.to_thread(get_recent_news_seen, "SPECIALIZED", since_utc),
            asyncio.to_thread(get_all_watch),
            asyncio.to_thread(get_all_pro_chat_ids),
            get_top_mean_reversion_stocks(limit=5)
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
                # Lấy AI Data riêng của user
                my_ai_data = user_ai_map.get(cid)
                
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
                ai_text = "_Không có tin nổi bật._"
                if ai_data:
                    lines = []
                    if ai_data.get('headline'):
                        lines.append("⚡ *TIÊU ĐIỂM*")
                        for i in ai_data['headline']: lines.append(f"• {i['text']}")
                    if ai_data.get('comment'):
                        lines.append(f"\n🧠 *AI:* {ai_data['comment']}")
                    ai_text = "\n".join(lines)
                
                msg_text = (
                    f"🌅 *BẢN TIN SÁNG {now_local.strftime('%d/%m')}* 🤖\n\n"
                    f"{ai_text}\n\n"
                    f"👉 *Nhấn nút dưới để xem chi tiết danh mục của bạn!*"
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
        
        if deleted_news > 0 or deleted_orders > 0:
            log.info(f"[MAINTENANCE] ✅ Đã xóa: {deleted_news} news cũ, {deleted_orders} đơn hàng treo.")
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
            model_id="gemini-2.5-flash-lite", # Dùng Flash cho nhanh
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
async def main():
    log.info(f"[{INSTANCE_ID}] Worker starting (Advanced APScheduler Mode)...")

    # --- 1. CẤU HÌNH REDIS JOB STORE ---
    # Parse URL Redis từ biến môi trường để lấy host, port, password
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r_parse = urlparse(redis_url)
    
    # Cấu hình lưu trữ Job vào Redis (Persistence)
    jobstores = {
        'default': RedisJobStore(
            jobs_key='stockbot_jobs', 
            run_times_key='stockbot_running', 
            host=r_parse.hostname, 
            port=r_parse.port, 
            password=r_parse.password,
            db=0 # Hoặc lấy từ r_parse.path nếu cần
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

    # Bắt đầu Scheduler
    scheduler.start()
    log.info("✅ APScheduler đã kích hoạt các tác vụ định kỳ.")
    
    # Chạy song song 2 loop
    await asyncio.gather(
        stock_price_fetcher_loop(),
        alert_loop(),
        #----------------------------
        market_monitor_fetcher_loop(),
        market_monitor_alert_loop(),
        #-------------------------
        worker_inbound_loop(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Worker stopped.")