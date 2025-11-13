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
load_dotenv()
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from flask import Flask, request
from hypercorn.asyncio import serve
from hypercorn.config import Config
from asgiref.wsgi import WsgiToAsgi
from vnstock import Trading, Quote, Listing, Finance, Company
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
    upsert_stock_value_batch,
    load_stock_value_cache,
    get_stock_value_cache_count,
    clear_stock_value_cache,
    has_news_seen,
    mark_news_seen,          
    get_news_seen_count,
    get_news_pref,
    set_news_pref,   
    is_news_enabled_for_chat,
    has_bctc_notified,
    mark_bctc_notified,
    add_bctc_queue,
    get_bctc_queue_by_date,
    clear_bctc_queue_entry,
    export_core_data,
    import_core_data,
    get_last_restore_month,
    mark_restore_done_now,
    get_conn,
    add_paid_user,
    is_user_pro,
    deactivate_paid_user,
    remove_paid_user,
    get_all_pro_chat_ids,
)
import psutil
import time
import subprocess
import re
import csv
from telegram.error import BadRequest
from typing import Any
import html
from telegram import BotCommand
import telegram
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
)
from pathlib import Path
from openai import OpenAI

# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PASSENGER_PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

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

# Thiết lập OpenRouter API (MiniMax M2)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    log.warning(
        "⚠️ OPENROUTER_API_KEY chưa được cấu hình – chức năng báo cáo 16:00 và /report sẽ không hoạt động."
    )

BOT_ACTIVE = None  # Sẽ được load từ DB trong main()

initial_active = None  # Trạng thái bot lúc khởi động (dùng trong lifespan)

ALERT_STATE = {}

# Thời gian giãn cách giữa 2 lần báo cùng một mốc cho cùng 1 mã (giây)
ALERT_COOLDOWN_SECONDS = 30 * 60  # 15 phút

# Ngưỡng cảnh báo
STOCK_LEVELS = [1, 2, 3, 4, 5, 6, -1, -2, -3, -4, -5, -6]

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
    "https://vietstock.vn/3355/chung-khoan/cau-chuyen-dau-tu.rss",
    "https://vietstock.vn/143/chung-khoan/chinh-sach.rss",
    "https://vietstock.vn/759/hang-hoa/vang-va-kim-loai-quy.rss",
    "https://vietstock.vn/34/hang-hoa/nhien-lieu.rss",
    "https://vietstock.vn/118/hang-hoa/nong-san-thuc-pham.rss",
    "https://vietstock.vn/757/tai-chinh/ngan-hang.rss",
    "https://vietstock.vn/3113/tai-chinh/bao-hiem.rss",
    "https://vietstock.vn/16312/tai-chinh/tai-san-so.rss",
    "https://vietstock.vn/761/kinh-te/vi-mo.rss",
    "https://vietstock.vn/768/kinh-te/kinh-te-dau-tu.rss",
    "https://vietstock.vn/773/the-gioi/chung-khoan-the-gioi.rss",
    "https://vietstock.vn/4309/the-gioi/tien-ky-thuat-so.rss",
    "https://vietstock.vn/772/the-gioi/tai-chinh-quoc-te.rss",
    "https://vietstock.vn/775/the-gioi/kinh-te-nganh.rss",


]

# Chu kỳ quét RSS (giây)
NEWS_SPECIALIZED_INTERVAL_SECONDS = 30 * 60   # 30 phút
NEWS_MACRO_INTERVAL_SECONDS = 60 * 60        # 60 phút

# Số bài tối đa gửi cho mỗi user / mỗi vòng quét tin
NEWS_MAX_ARTICLES_PER_CHAT = 3          # tin chuyên ngành
NEWS_MACRO_MAX_ARTICLES_PER_RUN = 3     # tin vĩ mô (broadcast)

# Map symbol -> list keyword (mã + tên doanh nghiệp)
COMPANY_KEYWORDS: dict[str, list[str]] = {}


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

async def send_md(bot: telegram.Bot, chat_id: int, text: str, **kwargs):
    """
    Gửi tin nhắn Markdown an toàn (async) bằng bot instance.
    """
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            **kwargs,
        )
    except BadRequest as e:
        if "can't parse entities" in str(e).lower():
            log.warning(f"[Markdown error] {e} | text={text!r}")
            safe_text = escape_markdown_v2(text) # Bạn đã có hàm này
            return await bot.send_message(
                chat_id=chat_id,
                text=safe_text,
                parse_mode="Markdown",
                **kwargs,
            )
        else:
            log.error(f"[Telegram Send Error] chat={chat_id}: {e}")
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

# Thay thế hàm này trong alert_bot.py

# (Trong file alert_bot.py)
# THAY THẾ HÀM NÀY:

async def broadcast_to_all_watchers(text: str, target_audience: str = 'pro'):
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

            tasks.append(send_md(tg_app.bot, chat_id, text))
            
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NOTICE] Lỗi chuẩn bị gửi cho {chat_key}: {e}")

    # Gửi song song
    results = await asyncio.gather(*tasks, return_exceptions=True)
    count = sum(1 for res in results if not isinstance(res, Exception))

    log.info(f"[{INSTANCE_ID}][NOTICE] Đã gửi thông báo tới {count} user (Target: {target_audience}).")

async def send_eod_summary():
    """
    Gửi tổng kết cuối phiên cho từng user dựa trên watchlist:
    - Mã cổ phiếu
    - % thay đổi (tự tính từ match_price & reference_price)
    - Tổng giá trị khớp (VND) – từ match_accumulated_value * 1_000_000
    - GT mua/bán khối ngoại (VND)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_str = now.strftime("%d/%m/%Y")

    log.info(f"[{INSTANCE_ID}][EOD] Bắt đầu tổng kết cuối phiên cho ngày {today_str}.")

    # 1️⃣ Lấy toàn bộ watchlist từ DB (chạy trong thread)
    try:
        all_watch = await asyncio.to_thread(get_all_watch)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][EOD] Lỗi get_all_watch: {e}")
        return

    if not all_watch:
        log.info(f"[{INSTANCE_ID}][EOD] Không có user nào theo dõi mã, bỏ qua.")
        return

    # Gom tất cả mã cần lấy board
    all_symbols: set[str] = set()
    for block in all_watch.values():
        for sym in (block.get("list", []) or []):
            s = str(sym).upper().strip()
            if s:
                all_symbols.add(s)

    if not all_symbols:
        log.info(f"[{INSTANCE_ID}][EOD] Watchlist rỗng, bỏ qua.")
        return

    # 2️⃣ Gọi price_board cho toàn bộ mã (chạy trong thread)
    def _fetch_price_board(symbols: list[str]):
        try:
            tr = Trading(source="VCI")
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Không khởi tạo được Trading: {e}")
            return None
        try:
            return tr.price_board(symbols)
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Lỗi price_board: {e}")
            return None

    pb_df = await asyncio.to_thread(_fetch_price_board, sorted(all_symbols))
    if pb_df is None or pb_df.empty:
        log.warning(f"[{INSTANCE_ID}][EOD] price_board trả về rỗng.")
        return

    # 3️⃣ Helpers parse dữ liệu
    def _norm(x):
        if x is None:
            return None
        try:
            if hasattr(x, "item"):
                x = x.item()
        except Exception:
            pass
        try:
            x = float(x)
        except Exception:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return x

    def _find_by_substrings(row, required_substrings: list[str]):
        """Tìm cột có chứa đầy đủ các substring yêu cầu."""
        for k in row.index:
            if isinstance(k, tuple):
                name = f"{k[0]}_{k[1]}".lower()
            else:
                name = str(k).lower()
            if all(sub in name for sub in required_substrings):
                try:
                    v = _norm(row.get(k))
                except Exception:
                    v = None
                if v is not None:
                    return v
        return None

    summary_by_symbol: dict[str, dict] = {}

    for idx, row in pb_df.iterrows():
        # Lấy symbol
        sym = None
        for ksym in [("listing", "symbol"), ("stock", "symbol"), ("listing", "ticker")]:
            try:
                if ksym in row.index:
                    val = row.get(ksym)
                else:
                    val = None
            except Exception:
                val = None
            if val:
                sym = str(val).upper().strip()
                break

        if not sym and isinstance(idx, str):
            sym = idx.upper().strip()
        if not sym:
            continue

        # --- % thay đổi: tự tính từ match_price & reference_price ---
        match_price = None
        ref_price = None
        try:
            if ("match", "match_price") in row.index:
                match_price = _norm(row[("match", "match_price")])
        except Exception:
            match_price = None

        # ưu tiên reference_price trong "match", nếu không có thì lấy ref_price trong "listing"
        try:
            if ("match", "reference_price") in row.index:
                ref_price = _norm(row[("match", "reference_price")])
            elif ("listing", "ref_price") in row.index:
                ref_price = _norm(row[("listing", "ref_price")])
        except Exception:
            ref_price = None

        pct = None
        if match_price is not None and ref_price not in (None, 0):
            pct = (match_price - ref_price) / ref_price * 100.0

        # --- Tổng GT khớp ---
        # match_accumulated_value đang là "triệu VND" => nhân 1_000_000 để ra VND
        raw_acc_value = _find_by_substrings(row, ["accumulated_value"])
        total_value_vnd = None
        if raw_acc_value is not None:
            total_value_vnd = raw_acc_value * 1_000_000.0

        # --- GT khối ngoại mua/bán (đã là VND) ---
        foreign_buy = _find_by_substrings(row, ["foreign", "buy", "value"])
        foreign_sell = _find_by_substrings(row, ["foreign", "sell", "value"])

        summary_by_symbol[sym] = {
            "pct": pct,
            "value": total_value_vnd,
            "foreign_buy": foreign_buy,
            "foreign_sell": foreign_sell,
        }

    if not summary_by_symbol:
        log.warning(f"[{INSTANCE_ID}][EOD] Không parse được dữ liệu cho mã nào.")
        return

    # 4️⃣ Hàm format cho đẹp
    def fmt_pct(p):
        if p is None:
            return "N/A"
        try:
            return f"{float(p):+,.2f}%"
        except Exception:
            return "N/A"

    def fmt_vnd(v):
        if v is None:
            return "N/A"
        try:
            v = float(v)
        except Exception:
            return "N/A"
        av = abs(v)
        if av >= 1_000_000_000_000:
            return f"{v/1_000_000_000_000:.1f} nghìn tỷ"
        if av >= 1_000_000_000:
            return f"{v/1_000_000_000:.1f} tỷ"
        if av >= 1_000_000:
            return f"{v/1_000_000:.1f} triệu"
        if av >= 1_000:
            return f"{v/1_000:.1f} nghìn"
        return f"{v:.0f}"

    # 5️⃣ Build & gửi message cho từng user
    tasks = []
    for chat_key, user_block in all_watch.items():
        try:
            chat_id = int(chat_key)
        except Exception:
            continue

        watch_list = user_block.get("list", []) or []
        if not watch_list:
            continue

        lines: list[str] = [
            f"📊 *Tổng kết cuối phiên {today_str}*",
            "",
        ]
        has_any = False

        for sym in watch_list:
            s = str(sym).upper().strip()
            info = summary_by_symbol.get(s)
            if not info:
                lines.append(f"• *{s}*: không có dữ liệu giao dịch hôm nay.")
                has_any = True
                continue

            pct_str = fmt_pct(info["pct"])
            val_str = fmt_vnd(info["value"])

            fb = info.get("foreign_buy")
            fs = info.get("foreign_sell")

            if fb is not None or fs is not None:
                fb_str = fmt_vnd(fb) if fb is not None else "0"
                fs_str = fmt_vnd(fs) if fs is not None else "0"
                line = (
                    f"• *{s}*: {pct_str}"
                    f" | GT khớp: {val_str}"
                    f" | NN Mua/Bán: {fb_str} / {fs_str}"
                )
            else:
                line = f"• *{s}*: {pct_str} | GT khớp: {val_str}"

            lines.append(line)
            has_any = True

        if not has_any:
            continue

        lines.append("")
        lines.append("🤖 Dữ liệu được tổng hợp tự động bởi *StockBot*.")

        text = "\n".join(lines)
        tasks.append(send_md(tg_app.bot, chat_id, text))

    if not tasks:
        log.info(f"[{INSTANCE_ID}][EOD] Không có user nào cần gửi tổng kết.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    log.info(f"[{INSTANCE_ID}][EOD] Đã gửi tổng kết cuối phiên cho {ok} user.")


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
                # 🔔 15:00 – Tổng kết cuối phiên theo từng danh mục
                await send_eod_summary()
            else:
                # Các mốc khác: broadcast câu text cố định
                await broadcast_to_all_watchers(spec["text"], target_audience="all")
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
    Lấy chính xác:
    - Giá hiện tại (close gần nhất)
    - % thay đổi so với:
        + 1 ngày trước (trading day gần nhất trước đó)
        + 1 tuần trước (close gần nhất trước ngày -7)
        + 1 tháng trước (close gần nhất trước ngày -30)
    Dùng dữ liệu lịch sử từ vnstock Quote.history (nguồn VCI).
    """
    try:
        vn_tz = pytz.timezone(TIMEZONE)
        today = datetime.datetime.now(vn_tz).date()

        # Lấy tầm 60 ngày gần nhất là đủ để tính 1 tháng
        start_date = (today - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        quote = Quote(symbol=symbol, source="VCI")
        df = quote.history(start=start_date, end=end_date, interval="1D")

        if df is None or len(df) == 0:
            log.warning(f"[{INSTANCE_ID}] [PERF] {symbol}: không có dữ liệu history")
            return None

        # Chuẩn hoá time -> datetime + sort
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

        last = df.iloc[-1]
        last_date = last["time"].date()
        price = float(last["close"])

        def find_price_before(target_date: datetime.date):
            """Tìm giá close gần nhất TRƯỚC hoặc BẰNG target_date."""
            sub = df[df["time"].dt.date <= target_date]
            if sub.empty:
                return None
            return float(sub.iloc[-1]["close"])

        # % NGÀY: so với trading day gần nhất trước hôm nay
        prev_price = find_price_before(last_date - datetime.timedelta(days=1))
        if prev_price is not None and prev_price != 0:
            day_pct = (price - prev_price) / prev_price * 100.0
        else:
            day_pct = None

        # % TUẦN: so với close gần nhất trước (hôm nay - 7 ngày)
        week_price = find_price_before(last_date - datetime.timedelta(days=7))
        if week_price is not None and week_price != 0:
            week_pct = (price - week_price) / week_price * 100.0
        else:
            week_pct = None

        # % THÁNG: so với close gần nhất trước (hôm nay - 30 ngày)
        month_price = find_price_before(last_date - datetime.timedelta(days=30))
        if month_price is not None and month_price != 0:
            month_pct = (price - month_price) / month_price * 100.0
        else:
            month_pct = None

        return {
            "price": price,
            "day_pct": day_pct,
            "week_pct": week_pct,
            "month_pct": month_pct,
        }

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [PERF FAIL] {symbol}: {e}")
        return None


def format_perf_line(sym: str, perf: dict) -> str:
    price = perf.get("price")
    day_pct = perf.get("day_pct")
    week_pct = perf.get("week_pct")
    month_pct = perf.get("month_pct")

    price_str = f"{float(price):,.0f} đ" if price is not None else "N/A"
    day_str = f"{day_pct:+.2f}%" if day_pct is not None else "N/A"
    week_str = f"{week_pct:+.2f}%" if week_pct is not None else "N/A"
    month_str = f"{month_pct:+.2f}%" if month_pct is not None else "N/A"

    return (
        f"- {sym}: giá hiện tại {price_str}, "
        f"ngày {day_str}, tuần {week_str}, tháng {month_str}"
    )


def build_prompt_for_symbols(symbols: list[str]) -> str:
    lines = []
    for sym in symbols:
        perf = get_perf_history(sym)
        if not perf:
            continue
        lines.append(format_perf_line(sym, perf))

    if not lines:
        return "Không có dữ liệu giá để tạo báo cáo."

    data_block = "\n".join(lines)

    prompt = f"""
Bạn là chuyên viên phân tích chứng khoán Việt Nam. 
Hãy viết *báo cáo đầu tư trung–dài hạn (3–12 tháng)* cho danh mục dưới đây, dùng giọng văn chuyên nghiệp, súc tích, dễ đọc trên Telegram.

Dữ liệu danh mục:
{data_block}

Với MỖI cổ phiếu, trình bày theo mẫu:

🔹 MÃ
- Giá hiện tại: ...
- KQKD nổi bật: (tóm tắt kết quả kinh doanh gần nhất, mảng chính đóng góp, xu hướng tăng trưởng)
- Triển vọng: (các động lực 6–12 tháng tới như dự án, mở rộng, chính sách hỗ trợ, xu hướng ngành)
- Rủi ro: (yếu tố tiêu cực như pháp lý, nợ vay, giá nguyên liệu, chu kỳ)
- Hành động: (tăng tỷ trọng / nắm giữ / giảm tỷ trọng / theo dõi thêm)

Cuối cùng viết phần:
📊 **Tổng quan danh mục:** (nhận xét cơ cấu ngành, mức rủi ro chung và gợi ý định hướng 3–12 tháng)

Yêu cầu:
- Không nói chuyện “hôm nay tăng giảm”, không nêu giá mục tiêu.
- Giọng phân tích chuyên nghiệp, tránh câu khẳng định tuyệt đối.
- Mỗi mã ngắn gọn, rõ ý, tối đa khoảng 800 ký tự.
"""
    return prompt.strip()


def call_chatgpt_for_report(symbols: list[str]) -> str:
    """Gọi OpenRouter (MiniMax M2) để sinh bản tin báo cáo danh mục."""
    if not OPENROUTER_API_KEY:
        return (
            "⚠️ Hệ thống chưa cấu hình OPENROUTER_API_KEY nên chưa tạo được báo cáo tự động."
        )

    prompt = build_prompt_for_symbols(symbols)

    # Tạo client OpenRouter (sử dụng SDK openai)
    try:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OPENROUTER_API_KEY,
        )
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][LLM INIT ERROR] {e}")
        return "⚠️ Không khởi tạo được OpenRouter client."
    
    models = [
        "minimax/minimax-m2",      # hoạt động ổn định nhất hiện tại
    ]

     # Prompt messages
    messages = [
        {
            "role": "user",
            "content": f"Bạn là chuyên gia chứng khoán Việt Nam, trả lời ngắn gọn, rõ ràng, phù hợp gửi qua Telegram. {prompt}",
        },
    ]
    
     # Gọi OpenRouter có retry + fallback
    last_error = None
    for model in models:
        for attempt in range(3):
            try:
                completion = client.chat.completions.create(
                    model=model,
                    messages=messages,
                )
                text = completion.choices[0].message.content.strip()
                log.info(f"[{INSTANCE_ID}][LLM OK] Model={model} round={attempt+1}")
                return text

            except Exception as e:
                last_error = e
                log.warning(
                    f"[{INSTANCE_ID}][LLM ERROR] Model={model} round={attempt+1}: {e}"
                )
                log.info(f"[{INSTANCE_ID}][LLM ERROR] OPENROUTER_API_KEY: {OPENROUTER_API_KEY}")
                time.sleep(20)  # exponential backoff
                continue

    return (
        f"⚠️ Hiện tại không tạo được báo cáo danh mục (LLM lỗi: {type(last_error).__name__}). "
        "Bạn thử lại sau nhé."
    )


#--------------------------------------------------------------------
    # url = "https://openrouter.ai/api/v1/chat/completions"
    # headers = {
    #     "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    #     "Content-Type": "application/json",
    # }
    # body = {
    #     "model": "z-ai/glm-4.5-air:free",  # MiniMax M2 free trên OpenRouter
    #     "messages": [
    #         {
    #             "role": "user",
    #             "content": "Bạn là chuyên gia chứng khoán Việt Nam, trả lời ngắn gọn, rõ ràng, phù hợp gửi qua Telegram.",
    #         },
    #         {
    #             "role": "user",
    #             "content": prompt,
    #         },
    #     ],
    #     "temperature": 0.4,
    # }

    # try:
    #     resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
    #     if resp.status_code != 200:
    #         log.warning(
    #             f"[{INSTANCE_ID}][LLM ERROR] HTTP {resp.status_code}: {resp.text[:300]}"
    #         )
    #         return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM trả lỗi HTTP). Bạn thử lại sau nhé."

    #     data = resp.json()
    #     if "error" in data:
    #         log.warning(f"[{INSTANCE_ID}][LLM ERROR] {data['error']}")
    #         return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM báo lỗi nội bộ)."

    #     choices = data.get("choices")
    #     if not choices:
    #         log.warning(
    #             f"[{INSTANCE_ID}][LLM ERROR] Response không có 'choices': {list(data.keys())}"
    #         )
    #         return "⚠️ Hiện tại không tạo được báo cáo danh mục (không nhận được nội dung phù hợp)."

    #     content = choices[0]["message"]["content"]
    #     return content.strip()

    # except Exception as e:
    #     log.warning(f"[{INSTANCE_ID}][LLM EXCEPTION] {e}")
    #     return "⚠️ Hiện tại không tạo được báo cáo danh mục do lỗi kết nối LLM."

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

# Dùng HTML để bot gửi tin nhắn format đẹp và an toàn
USER_HELP_TEXT_HTML = """📊 <b>Các lệnh bạn có thể sử dụng:</b>
• <code>/add &lt;MÃ&gt;</code> – Thêm mã cổ phiếu vào danh sách theo dõi
• <code>/remove &lt;MÃ&gt;</code> – Xóa mã cổ phiếu khỏi danh sách
• <code>/list</code> – Xem danh sách cổ phiếu đang theo dõi
• <code>/report</code> – Nhận báo cáo phân tích AI về danh mục của bạn"""

async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Phản hồi khi người dùng gõ văn bản tự do hoặc lệnh không tồn tại.
    (Đã gộp logic của _collector vào đây)
    (ĐÃ SỬA LỖI BLOCKING I/O)
    """
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    
    # === LOGIC TỪ _collector ĐÃ GỘP VÀO ĐÂY ===
    # Tự động lưu chat_id vào DB nếu chưa có
    try:
        # ⭐️ SỬA: Chạy CSDL trong thread
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        if lst is None:
            # ⭐️ SỬA: Chạy CSDL trong thread
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])
    except Exception as e:
        log.warning(f"Lỗi khi auto-save chat_id {chat_id} trong unknown_message: {e}")
    # === KẾT THÚC LOGIC GỘP ===
    
    user_text = update.message.text
    
    try:
        # ⭐️ SỬA: Chạy CSDL trong thread
        # Log lại hành vi này (tận dụng hàm bạn đã có)
        await asyncio.to_thread(
            log_command_usage, chat_id, f"unknown: {user_text[:50]}", ADMIN_ID
        )
    except Exception as e:
        log.warning(f"Không thể log 'unknown' command: {e}")

    # Dùng html.escape để đảm bảo text người dùng nhập (ví dụ: <HAG>) 
    # không làm hỏng format HTML của bot
    safe_user_text = html.escape(user_text)
    
    reply_text = (
        f"🤔 Hmm, có vẻ tôi chưa được lập trình để hiểu <code>{safe_user_text}</code>.\n\n"
        f"{USER_HELP_TEXT_HTML}"
    )
    
    await update.message.reply_text(reply_text, parse_mode="HTML")

# ==============================
# 🧮 SCREENER VALUE – PRECOMPUTE
# ==============================
def precompute_value_data(full_refresh: bool = False, skip_if_fresh_today: bool = True):
    """
    Crawl P/E, P/B, ROE VÀ Thanh khoản, Tài sản (TẤT CẢ TỪ VCI)
    lưu vào stock_value_cache.
    
    (ĐÃ SỬA LỖI - 13/11/2025)
    - Dùng VCI (price_board) để lấy Vốn hoá, Thanh khoản, Sàn.
    - Dùng VCI (Company().ratio_summary()) để lấy P/E, P/B, ROE.
    - Không còn phụ thuộc vào nguồn TCBS (Finance.ratio()) bị lỗi.
    """
    
    vn_tz = pytz.timezone(TIMEZONE)
    started_at = datetime.datetime.now(vn_tz)
    log.info(
        f"[{INSTANCE_ID}][VALUE] Bắt đầu precompute_value_data (Chỉ dùng VCI) lúc "
        f"{started_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    # 1) Lấy danh sách mã từ Listing()
    try:
        listing = Listing()
        listing_df = listing.all_symbols()
    except Exception as e:
        log.exception(f"[{INSTANCE_ID}][VALUE] Lỗi gọi Listing().all_symbols(): {e}")
        return

    if listing_df is None or listing_df.empty:
        log.warning(
            f"[{INSTANCE_ID}][VALUE] listing.all_symbols() trả về rỗng, dừng."
        )
        return

    filtered_df = listing_df.copy()
    symbol_col_candidates = ["symbol", "ticker", "code"]
    symbol_col = next(
        (c for c in symbol_col_candidates if c in filtered_df.columns),
        None,
    )
    if not symbol_col:
        log.error(
            f"[{INSTANCE_ID}][VALUE] Không tìm thấy cột mã (symbol/ticker/code). "
            f"Columns: {filtered_df.columns.tolist()}"
        )
        return

    filtered_df[symbol_col] = filtered_df[symbol_col].astype(str).str.upper()
    symbols = filtered_df[symbol_col].dropna().unique().tolist()
    log.info(f"[{INSTANCE_ID}][VALUE] Tổng số mã từ Listing(): {len(symbols)}")

    # 🔹 Load industry map từ CSV (Topi)
    industry_map = load_industry_map_from_csv("ssi_master_list.csv")
    if industry_map:
        symbols = [s for s in symbols if s in industry_map]
        log.info(
            f"[{INSTANCE_ID}][VALUE] Đã load {len(industry_map)} mã có ngành từ ssi_master_list.csv. "
            f"Sau khi lọc, còn {len(symbols)} mã sẽ được crawl."
        )
        # --- Sanitize symbol list: chỉ giữ mã 3 chữ cái [A-Z] ---
        _symbols_before = len(symbols)
        symbols = [s for s in symbols if isinstance(s, str) and re.fullmatch(r"[A-Z]{3}", (s or ""))]
        dropped = _symbols_before - len(symbols)
        if dropped > 0:
            log.info(f"[{INSTANCE_ID}][VALUE] Đã lọc {dropped} mã không phải 3 chữ cái A-Z (loại mã gây lỗi batch).")
    else:
        log.info(
            f"[{INSTANCE_ID}][VALUE] Không load được ssi_master_list.csv, "
            "tất cả mã sẽ gán industry='Khác'."
        )

    exchange_col_candidates = ["exchange", "floor", "san"]
    exchange_col = next(
        (c for c in exchange_col_candidates if c in filtered_df.columns),
        None,
    )

    exchange_map = {}
    if exchange_col:
        try:
            tmp = filtered_df[[symbol_col, exchange_col]].dropna()
            for _, row in tmp.iterrows():
                exchange_map[str(row[symbol_col]).upper()] = str(row[exchange_col]).upper()
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][VALUE] Không build được exchange_map: {e}")

    try:
        trading = Trading(source="VCI")
    except Exception as e:
        trading = None
        log.warning(f"[{INSTANCE_ID}][VALUE] Không khởi tạo được Trading(source='VCI'): {e}")
        return

    # 2️⃣ Đọc các mã đã có trong DB
    existing_records = load_stock_value_cache()

    if full_refresh and skip_if_fresh_today:
        try:
            last = max(
                (r.get("updated_at") for r in existing_records if r.get("updated_at")),
                default=None
            )
            if last is not None:
                last_vn_date = last.astimezone(vn_tz).date() if last.tzinfo else last.date()
                today_vn_date = started_at.date()
                if last_vn_date == today_vn_date:
                    log.info(
                        f"[{INSTANCE_ID}][VALUE] stock_value_cache đã được refresh hôm nay ({today_vn_date}), bỏ qua."
                    )
                    return
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][VALUE] Không kiểm tra được last updated_at: {e}")

    # 🔹 Chọn tập mã cần crawl
    if full_refresh:
        todo_symbols = symbols
        already = 0
        total_symbols = len(symbols)
        total_todo = len(todo_symbols)
        log.info(
            f"[{INSTANCE_ID}][VALUE] CHẾ ĐỘ FULL REFRESH: crawl toàn bộ {total_todo}/{total_symbols} mã."
        )
    else:
        processed_symbols = {r["symbol"] for r in existing_records if r.get("symbol")}
        todo_symbols = [s for s in symbols if s not in processed_symbols]
        total_symbols = len(symbols)
        total_todo = len(todo_symbols)
        already = len(processed_symbols)
        log.info(
            f"[{INSTANCE_ID}][VALUE] Đã có trong DB: {already} | "
            f"Cần crawl thêm: {total_todo} | Tổng: {total_symbols}"
        )

    if total_todo == 0:
        log.info(
            f"[{INSTANCE_ID}][VALUE] Không còn mã cần crawl. Kết thúc precompute."
        )
        return

    total_batches = math.ceil(total_todo / VALUE_BATCH_SIZE)
    processed_count = already
    per_symbol_sleep = 1 # 🔧 DÙNG VCI: nên tăng sleep để tránh rate limit

    # --- Các hàm Helper (đã sửa _safe_float) ---
    def _safe_float(val):
        if val is None:
            return None
        try:
            if hasattr(val, "item"):
                val = val.item()
        except Exception:
            pass
        try:
            if isinstance(val, str):
                val = val.strip()
                if val == "" or val.lower() in {"nan", "none", "null"}:
                    return None
                if "," in val and val.count(",") == 1 and "." not in val:
                    val = val.replace(",", ".")
            v = float(val)
        except Exception:
            return None
        if math.isnan(v):
            return None
        return v
    
    # --- Bắt đầu vòng lặp Batch ---
    for batch_idx in range(total_batches):
        batch_syms = todo_symbols[
            batch_idx * VALUE_BATCH_SIZE : (batch_idx + 1) * VALUE_BATCH_SIZE
        ]
        log.info(
            f"[{INSTANCE_ID}][VALUE] Batch {batch_idx+1}/{total_batches} – "
            f"{len(batch_syms)} symbols: {', '.join(batch_syms[:8])}"
        )

        # 3️⃣ Lấy price_board cho batch (VCI - Lấy Giá + Proxies)
        pb_df = None
        try:
            pb_df = _fetch_price_board_with_fallback(trading, batch_syms, log, INSTANCE_ID)
            if pb_df is None or pb_df.empty:
                log.warning(f"[{INSTANCE_ID}][VALUE] price_board rỗng sau fallback cho batch {batch_idx}/{total_batches}. Bỏ qua batch này.")
                continue
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi price_board batch: {e}")

        batch_records = []

        for sym in batch_syms:
            sym = str(sym).upper()
            
            time.sleep(per_symbol_sleep) # Throttle VCI API

            # 4️⃣ Lấy thông tin từ price_board (VCI)
            exchange = exchange_map.get(sym)
            floor = None
            asset_proxy = None
            liquidity_proxy = None

            if pb_df is not None and not pb_df.empty:
                try:
                    # Tìm hàng của sym trong pb_df
                    row = None
                    try:
                        if sym in pb_df.index:
                             row = pb_df.loc[sym]
                    except Exception:
                        row = None
                        
                    if row is None:
                        try:
                            pb_df_sym_col = pb_df[('listing', 'symbol')].astype(str).str.upper()
                            row_match = pb_df[pb_df_sym_col == sym]
                            if row_match is not None and not row_match.empty:
                                row = row_match.iloc[0]
                        except KeyError:
                             row = None
                        except Exception:
                             row = None

                    if row is not None:
                        # 1. LẤY SÀN
                        if ('listing', 'exchange') in row.index:
                            floor = str(row[('listing', 'exchange')]).upper()
                        elif 'exchange' in row.index:
                            floor = str(row['exchange']).upper()

                        # 2. LẤY THANH KHOẢN (GTGD)
                        raw_liq = _safe_float(row.get(('match', 'accumulated_value')))
                        if raw_liq is not None:
                            liquidity_proxy = raw_liq * 1_000_000  # Đổi ra VND

                        # 3. TÍNH VỐN HOÁ (ASSET PROXY)
                        price_for_asset = _safe_float(row.get(('match', 'match_price')))
                        shares = _safe_float(row.get(('listing', 'listed_share')))
                        if price_for_asset is not None and shares is not None and shares > 0:
                            asset_proxy = price_for_asset * shares

                except Exception as e:
                    log.warning(f"[{INSTANCE_ID}][VALUE] Map VCI/proxies lỗi ({sym}): {e}")

            # Fallback floor nếu chưa có
            if floor is None:
                fallback_floor = exchange_map.get(sym)
                if fallback_floor:
                    floor = str(fallback_floor)

            # 5️⃣ Lấy P/E, P/B, ROE từ VCI (Company().ratio_summary())
            pe = None
            pb = None
            roe = None
            
            try:
                company = Company(symbol=sym)
                summary_df = company.ratio_summary()
                
                if summary_df is not None and not summary_df.empty:
                    row = summary_df.iloc[0]
                    pe = _safe_float(row.get('pe'))
                    pb = _safe_float(row.get('pb'))
                    roe = _safe_float(row.get('roe'))
                else:
                    log.warning(f"[{INSTANCE_ID}][VALUE] Không lấy được ratio_summary (VCI) cho {sym}.")
            
            except Exception as e:
                 log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi gọi Company.ratio_summary cho {sym}: {e}")

            # Ngành
            industry = industry_map.get(sym, "Khác") if industry_map else "Khác"

            record = {
                "symbol": sym,
                "exchange": exchange,
                "industry": industry,
                "pe": pe, # <-- P/E từ VCI
                "pb": pb, # <-- P/B từ VCI
                "roe": roe, # <-- ROE từ VCI
                "floor": floor,
                "asset_proxy": asset_proxy,
                "liquidity_proxy": liquidity_proxy,
            }
            batch_records.append(record)

        # 5️⃣ Ghi batch vào DB (upsert theo symbol)
        try:
            upsert_stock_value_batch(batch_records)
        except Exception as e:
            log.exception(
                f"[{INSTANCE_ID}][VALUE] Lỗi khi upsert batch {batch_idx+1}: {e}"
            )

        # Cập nhật tiến độ
        processed_count += len(batch_syms)
        done = processed_count
        percent = (done / total_symbols * 100) if total_symbols > 0 else 0.0
        log.info(
            f"[{INSTANCE_ID}][VALUE] Tiến độ: {done}/{total_symbols} mã "
            f"({percent:.1f}%). Batch {batch_idx+1}/{total_batches} hoàn tất."
        )

        # Nghỉ nhẹ giữa các batch
        if batch_idx < total_batches - 1:
            time.sleep(VALUE_BATCH_SLEEP)

    finished_at = datetime.datetime.now(vn_tz)
    duration = (finished_at - started_at).total_seconds()
    log.info(
        f"[{INSTANCE_ID}][VALUE] Hoàn thành precompute_value_data sau {duration:.1f}s. "
        f"Tổng mã trong DB hiện tại: {get_stock_value_cache_count()}."
    )

def compute_value_screener(
    top_per_industry: int = 3,
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

    # ❗️ BỘ LỌC NGHIÊM NGẶT (FIX 2 - Theo yêu cầu)
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
    # KẾT THÚC BỘ LỌC NGHIÊM NGẶT

    # LỌC SÀN TỪ DB (SIÊU NHANH)
    before_count = len(df)
    allowed_floors = {"HOSE", "HNX"}
    df = df[df["floor"].isin(allowed_floors)]
    log.info(
        f"[{INSTANCE_ID}][VALUE] Lọc sàn HOSE/HNX (từ DB): {before_count} -> {len(df)} mã."
    )
    
    # LỌC THANH KHOẢN TỪ DB (SIÊU NHANH)
    before_count = len(df)
    df = df[df["liquidity_proxy"] >= 50_000_000_000]  # 50 tỷ
    log.info(
        f"[{INSTANCE_ID}][VALUE] Lọc thanh khoản >=50 tỷ (từ DB): {before_count} -> {len(df)} mã."
    )
   
    # LỌC TÀI SẢN TỪ DB (SIÊU NHANH)
    before_count = len(df)
    df = df[df["asset_proxy"] >= 5_000_000_000_000]  # 5000 tỷ
    log.info(
        f"[{INSTANCE_ID}][VALUE] Lọc tổng tài sản proxy >=5000 tỷ (từ DB): {before_count} -> {len(df)} mã."
    )
    
    if df.empty:
        log.warning(
            f"[{INSTANCE_ID}][VALUE] Không còn mã nào sau khi filter 3 tiêu chí."
        )
        return None

    # Tính trung bình ngành
    industry_group = df.groupby("industry").agg(
        pe_industry=("pe", "mean"),
        pb_industry=("pb", "mean"),
        roe_industry=("roe", "mean"),
    )
    df = df.join(industry_group, on="industry")

    # (Logic này đã an toàn vì df đã được lọc roe > 0)
    df = df[df["roe_industry"] > 0]
    if df.empty:
        return None

    df["value_score"] = (
        (df["pe_industry"] / df["pe"]) * 0.3
        + (df["pb_industry"] / df["pb"]) * 0.3
        + (df["roe"] / df["roe_industry"]) * 0.4
    )

    # Ngày dữ liệu
    as_of = None
    if "updated_at" in df.columns and not df["updated_at"].isna().all():
        latest_ts = df["updated_at"].max()
        try:
            if isinstance(latest_ts, datetime.datetime):
                vn_tz = pytz.timezone(TIMEZONE)
                as_of = latest_ts.astimezone(vn_tz).strftime("%Y-%m-%d %H:%M:%S")
            else:
                as_of = str(latest_ts)
        except Exception:
            as_of = str(latest_ts)

    industries_data = []
    for industry_name, g in df.groupby("industry"):
        g_sorted = g.sort_values("value_score", ascending=False)
        top_g = g_sorted.head(top_per_industry)

        rows_list = []
        for _, r in top_g.iterrows():
            rows_list.append(
                {
                    "symbol": r["symbol"],
                    "industry": industry_name,
                    "pe": float(r["pe"]),
                    "pb": float(r["pb"]),
                    "roe": float(r["roe"]),
                    "pe_industry": float(r["pe_industry"]),
                    "pb_industry": float(r["pb_industry"]),
                    "roe_industry": float(r["roe_industry"]),
                    "value_score": float(r["value_score"]),
                }
            )

        if rows_list:
            best_score = rows_list[0]["value_score"]
            industries_data.append(
                {
                    "industry": industry_name,
                    "best_score": best_score,
                    "rows": rows_list,
                }
            )

    if not industries_data:
        return None

    industries_data.sort(key=lambda x: x["best_score"], reverse=True)
    industries_data = industries_data[:max_industries]

    for item in industries_data:
        item.pop("best_score", None)

    return {
        "as_of": as_of,
        "industries": industries_data,
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
    Từ kết quả của compute_value_screener, format ra tin nhắn Markdown.
    Trả về None nếu không có dữ liệu.
    """
    if (
        not result
        or not result.get("industries")
        or all(not ind["rows"] for ind in result["industries"])
    ):
        log.warning(f"[{INSTANCE_ID}][SCREENER] Không có dữ liệu để format báo cáo.")
        return None

    as_of = result.get("as_of")
    industries = result["industries"]

    lines: list[str] = []
    lines.append("💰 *Top 3 cổ phiếu Value theo từng ngành* (dữ liệu TCBS)")
    if as_of:
        lines.append(f"_Cập nhật đến: {as_of}_")
    lines.append("")
    lines.append("📊 *Tiêu chí chấm điểm:*")
    lines.append("• P/E & P/B thấp hơn trung bình ngành → điểm cao hơn")
    lines.append("• ROE cao hơn trung bình ngành → điểm cao hơn")
    lines.append("• Chỉ lấy các cổ phiếu sàn HOSE/HNX")
    lines.append("• Thanh khoảng trên 50 tỷ/ngày")
    lines.append("• Tổng tài sản trên 5000 tỷ")
    lines.append("")

    for industry_block in industries:
        industry_name = industry_block["industry"] or "Khác"
        display_industry = (
            industry_name[:1].upper() + industry_name[1:]
            if industry_name
            else "Khác"
        )

        first = industry_block["rows"][0]
        pe_avg = first["pe_industry"]
        pb_avg = first["pb_industry"]
        roe_avg = first["roe_industry"]

        lines.append(
            f"🏷 *Ngành: {display_industry}* "
            f"(P/E TB: {pe_avg:.1f} | P/B TB: {pb_avg:.1f} | ROE TB: {format_roe_pct(roe_avg)})"
        )

        for idx, r in enumerate(industry_block["rows"], start=1):
            lines.append(
                f"{idx}️⃣ *{r['symbol']}* – "
                f"P/E {r['pe']:.1f} | "
                f"P/B {r['pb']:.1f} | "
                f"ROE {format_roe_pct(r['roe'])}"
            )

        lines.append("")  # dòng trống ngăn cách ngành

    lines.append(
        "_Lưu ý: Đây là bảng xếp hạng định lượng, nhà đầu tư nên kết hợp phân tích cơ bản & kỹ thuật để ra quyết định._"
    )

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3800] + "\n\n_(Đã rút gọn do giới hạn độ dài tin nhắn Telegram.)_"
    
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
TICKER_INTERVAL_SECONDS = 3  # Tần suất Ticker (check cache)
FETCHER_INTERVAL_SECONDS = 15 # Tần suất Fetcher (gọi API)

# (Các hàm same_sign, get_quote... của bạn nằm ở đây)
def same_sign(a: float, b: float) -> bool:
    """Hai số cùng dấu (cùng dương hoặc cùng âm) hay không."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)

async def stock_price_fetcher_loop():
    """
    (TÁC VỤ 1 - FETCHER - MỚI)
    - Loop này chạy 15 GIÂY/LẦN (FETCHER_INTERVAL_SECONDS).
    - Chỉ làm 2 việc nặng: Lấy DB (get_all_watch) và Lấy API (price_board).
    - Cập nhật kết quả vào 2 biến cache toàn cục.
    """
    global _stock_current_price_cache, _stock_current_watch_cache
    
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    if not stock_trading:
        log.error(f"[{INSTANCE_ID}][FETCHER_STOCK] 'stock_trading' (VCI) bị lỗi, không thể chạy.")
        return # Thoát

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Bot TẮT, ngủ 60s.")
            await asyncio.sleep(60)
            continue

        if not in_session_vietnam():
            next_start = next_session_start(now)
            delay = max((next_start - now).total_seconds(), 60.0)
            log.info(
                f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Ngoài giờ... "
                f"sleep {delay:.0f}s tới {next_start.strftime('%Y-%m-%d %H:%M')}"
            )
            await asyncio.sleep(delay)
            continue

        loop_start = now
        log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Bắt đầu lấy DB (watch) và API (price)...")
        try:
            # ==================================================
            # 1. GATHER (GOM) - (I/O Nặng 1: DB)
            # ==================================================
            all_watch = await asyncio.to_thread(get_all_watch)

            all_symbols: set[str] = set()
            for block in all_watch.values():
                for sym in (block.get("list", []) or []):
                    if len(sym) == 3 and sym.isalpha():
                         all_symbols.add(sym.upper())

            if not all_symbols:
                log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Không có symbol nào, sleep 60s.")
                _stock_current_watch_cache = {} # Xóa cache
                _stock_current_price_cache = {}
                await asyncio.sleep(60)
                continue
            
            symbols_list = sorted(list(all_symbols))
            log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Cần lấy giá cho {len(symbols_list)} mã.")

            # ==================================================
            # 2. FETCH (Lấy dữ liệu - I/O Nặng 2: API)
            # ==================================================       
            df = None
            try:
                df = await asyncio.to_thread(stock_trading.price_board, symbols_list)
            except (Exception, SystemExit) as e: # <-- SỬA Ở ĐÂY
                log.error(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Lỗi price_board (hoặc rate limit VCI): {e}")
                # Thêm sleep 60s khi lỗi để chờ VCI
                await asyncio.sleep(60)

            if df is None or df.empty:
                log.warning(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] price_board rỗng.")
                await asyncio.sleep(FETCHER_INTERVAL_SECONDS)
                continue

            # ==================================================
            # 3. PROCESS (Xử lý & Build Cache)
            # ==================================================
            quote_cache: dict[str, dict] = {}
            def norm(x):
                if x is None: return None
                try:
                    if hasattr(x, "item"): x = x.item()
                except Exception: pass
                try: x = float(x)
                except Exception: return None
                if isinstance(x, float) and math.isnan(x): return None
                return x

            for _, row in df.iterrows():
                try:
                    sym = row[('listing', 'symbol')]
                    sym_u = sym.upper()
                    match_price = norm(row.get(("match", "match_price")))
                    ref_price = norm(
                        row.get(("match", "reference_price"))
                        if ("match", "reference_price") in row.index 
                        else row.get(("listing", "ref_price"))
                    )
                    price = match_price if match_price is not None else ref_price
                    pct_change = (
                        ((float(match_price) - float(ref_price)) / float(ref_price)) * 100.0
                        if match_price is not None and ref_price is not None and ref_price != 0
                        else None
                    )
                    if price is None or pct_change is None:
                        continue
                    quote_cache[sym_u] = {"price": price, "pct": pct_change}
                except Exception as e:
                    sym_debug = row.get(('listing', 'symbol'), 'Unknown')
                    log.warning(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Lỗi xử lý hàng {sym_debug}: {e}")
            
            # ==================================================
            # 4. CẬP NHẬT CACHE TOÀN CỤC
            # ==================================================
            _stock_current_watch_cache = all_watch
            _stock_current_price_cache = quote_cache
            log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Cập nhật cache: {len(all_watch)} users, {len(quote_cache)} mã.")

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Lỗi nghiêm trọng: {e}")

        # Giữ nhịp quét 15 giây
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(FETCHER_INTERVAL_SECONDS - elapsed, 1)
        log.info(f"[{INSTANCE_ID}][FETCHER_STOCK {loop_id}] Sleep {delay:.1f}s")
        await asyncio.sleep(delay)

# (Hàm alert_loop() gốc của bạn sẽ nằm ở dưới)
async def alert_loop():
    """
    (TÁC VỤ 2 - TICKER - ĐÃ TỐI ƯU)
    - Loop này chạy 3 GIÂY/LẦN (TICKER_INTERVAL_SECONDS).
    - Chỉ đọc cache (từ Fetcher) và RAM (state).
    - KHÔNG gọi API, KHÔNG gọi DB.
    - Nếu có alert -> Đẩy tin nhắn vào _stock_broadcast_queue.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    
    log.info(f"[{INSTANCE_ID}][TICKER_STOCK] Bắt đầu.")

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        if not BOT_ACTIVE:
            await asyncio.sleep(30) # Ngủ ngắn, vì Fetcher sẽ ngủ dài
            continue

        if not in_session_vietnam():
            await asyncio.sleep(60) # Ngủ ngắn, vì Fetcher sẽ ngủ dài
            continue

        loop_start = now
        try:
            # ==================================================
            # 1. GATHER (GOM) - (Đọc từ RAM, siêu nhanh)
            # ==================================================
            all_watch = _stock_current_watch_cache
            quote_cache = _stock_current_price_cache
            all_state = get_state_for_all() # Cái này từ RAM

            if not all_watch or not quote_cache:
                log.info(f"[{INSTANCE_ID}][TICKER_STOCK {loop_id}] Cache rỗng, chờ Fetcher...")
                await asyncio.sleep(TICKER_INTERVAL_SECONDS)
                continue
            
            # log.info(f"[{INSTANCE_ID}][TICKER_STOCK {loop_id}] Bắt đầu quét {len(all_watch)} users...")

            # ==================================================
            # 2. DISTRIBUTE (Phân phối & Đẩy vào Queue)
            # ==================================================
            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", []) or []
                if not watch_list:
                    continue

                if chat_key not in all_state:
                    all_state[chat_key] = {}
                personal_state = all_state[chat_key]
                messages: list[str] = []

                for sym in watch_list:
                    sym_u = sym.upper()
                    
                    quote = quote_cache.get(sym_u)
                    if not quote:
                        continue 

                    price = quote["price"]
                    pct = quote["pct"]
                    metric = pct
                    new_lvl = pick_new_level(metric, STOCK_LEVELS)

                    state_entry = personal_state.get(sym_u, {})
                    prev_lvl = state_entry.get("last_level", 0)
                    last_alert_at_str = state_entry.get("last_alert_at")

                    last_alert_at = None
                    if last_alert_at_str:
                        try:
                            last_alert_at = datetime.datetime.fromisoformat(last_alert_at_str)
                        except Exception:
                            pass

                    should_alert = False
                    if new_lvl is not None:
                        if prev_lvl == 0:
                            should_alert = True
                        else:
                            if same_sign(new_lvl, prev_lvl):
                                if abs(new_lvl) > abs(prev_lvl):
                                    should_alert = True
                                elif (
                                    last_alert_at is None
                                    or (now - last_alert_at).total_seconds() >= ALERT_COOLDOWN_SECONDS
                                ):
                                    should_alert = True
                            else:
                                should_alert = True

                    if should_alert:
                        icon = "🟢" if new_lvl > 0 else "🔴"
                        fun_line = random.choice(FUN_UP if new_lvl > 0 else FUN_DOWN)
                        price_str = f"{float(price):,.0f}" if price is not None else "N/A"
                        pct_str = f"{float(pct):+.2f}%" if pct is not None else "N/A"

                        messages.append(
                            f"{icon} *{sym_u} {pct_str}* tại {price_str}\n_{fun_line}_"
                        )

                        personal_state[sym_u] = {
                            "last_level": new_lvl,
                            "last_alert_at": now.isoformat(),
                        }
                    else:
                        if sym_u not in personal_state:
                            personal_state[sym_u] = {
                                "last_level": 0,
                                "last_alert_at": None,
                            }

                # [TỐI ƯU] Đẩy vào Queue, KHÔNG gửi tin
                if messages:
                    header = (
                        "--------------------------------\n"
                        f"⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*"
                    )
                    messages_text = "\n".join(messages)
                    body = messages_text + "\n" + header
                    
                    try:
                        _stock_broadcast_queue.put_nowait({"chat_id": chat_id, "body": body})
                    except asyncio.QueueFull:
                        log.warning(f"[{INSTANCE_ID}][TICKER_STOCK {loop_id}] Queue cổ phiếu bị đầy!")

                all_state[chat_key] = personal_state

            # 🧩 3️⃣ Lưu state
            save_state_for_all(all_state) # Nhanh, từ RAM

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][TICKER_STOCK {loop_id}] Lỗi nghiêm trọng: {e}")

        # 🕒 Giữ nhịp quét 3 giây
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(TICKER_INTERVAL_SECONDS - elapsed, 0.1)
        # log.info(f"[{INSTANCE_ID}][TICKER_STOCK {loop_id}] Sleep {delay:.1f}s")
        await asyncio.sleep(delay)

async def stock_broadcast_loop():
    """
    (TÁC VỤ 3 - BROADCASTER - MỚI)
    - Loop này CHỈ GỬI TIN NHẮN (cho cổ phiếu thường).
    - Chờ tin nhắn trong _stock_broadcast_queue.
    - Gọi (blocking) send_msg_to trong thread.
    """
    log.info("[BCASTER_STOCK] Bắt đầu. Chờ tin nhắn trong queue...")
    
    while True:
        try:
            # Chờ Ticker đẩy tin nhắn vào
            item = await _stock_broadcast_queue.get()
            
            chat_id = item.get("chat_id")
            body = item.get("body")
            
            if not chat_id or not body:
                _stock_broadcast_queue.task_done()
                continue
            
            # log.info(f"[BCASTER_STOCK] Nhận được tin cho {chat_id}, đang gửi...")

            # [TỐI ƯU] Gọi hàm blocking send_msg_to trong thread
            await asyncio.to_thread(send_msg_to, chat_id, body, "Markdown")

            _stock_broadcast_queue.task_done()
            # log.info(f"[BCASTER_STOCK] Gửi xong cho {chat_id}. Quay lại chờ...")
            
            await asyncio.sleep(0.1) # Thêm 1 sleep nhỏ 100ms để tránh dồn dập

        except asyncio.CancelledError:
            log.info("[BCASTER_STOCK] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[BCASTER_STOCK] Lỗi: {e}")
            if '_stock_broadcast_queue' in locals():
                try:
                    _stock_broadcast_queue.task_done()
                except ValueError:
                    pass
#-------------------------------------------

# ==============================================
# BÁO CÁO TUẦN 09:00 CHỦ NHẬT (CÓ CACHE + RETRY)
# ==============================================
async def execute_weekly_report(admin_update: Update | None = None):
    """
    Hàm lõi: Chạy, tính toán và gửi báo cáo tuần (Pro + Admin).
    Nếu có admin_update, sẽ gửi phản hồi cho admin.
    """
    global INSTANCE_ID, log, tg_app, BOT_ACTIVE, OPENROUTER_API_KEY, ADMIN_ID

    instance_label = f"[{INSTANCE_ID}][EXEC_WEEKLY]"
    admin_chat_id = admin_update.effective_chat.id if admin_update else None
    vn_tz = pytz.timezone(TIMEZONE)
    
    try:
        log.info(f"{instance_label} Bắt đầu chạy (trigger by: {'Admin' if admin_chat_id else 'Scheduler'}).")
        if admin_chat_id:
            await tg_app.bot.send_message(admin_chat_id, "⏳ Bắt đầu chạy tác vụ gửi Weekly Report thủ công...")

        if not BOT_ACTIVE:
            log.info(f"{instance_label} Bot TẮT, huỷ tác vụ.")
            if admin_chat_id:
                await tg_app.bot.send_message(admin_chat_id, "⚠️ Bot đang TẮT, đã huỷ tác vụ.")
            return

        if not OPENROUTER_API_KEY:
            log.warning(f"{instance_label} Chưa có OPENROUTER_API_KEY, bỏ qua.")
            if admin_chat_id:
                await tg_app.bot.send_message(admin_chat_id, "⚠️ Chưa có OPENROUTER_API_KEY, đã huỷ tác vụ.")
            return

        # === BÊ NGUYÊN LOGIC TỪ weekly_report_loop VÀO ĐÂY ===

        # 1. Lấy TẤT CẢ user
        all_watch = await asyncio.to_thread(get_all_watch)
        
        # 2. Lấy user Pro (1 lần gọi DB)
        pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)

        if not all_watch:
            log.info(f"{instance_label} Không có user nào theo dõi, bỏ qua.")
            if admin_chat_id:
                await tg_app.bot.send_message(admin_chat_id, "ℹ️ Không có user nào theo dõi, bỏ qua.")
            return

        sent_count = 0
        skipped_count = 0

        for chat_key, user_block in all_watch.items():
            if not BOT_ACTIVE:
                log.info(f"{instance_label} Bot TẮT giữa chừng, dừng gửi.")
                if admin_chat_id:
                    await tg_app.bot.send_message(admin_chat_id, "⚠️ Bot TẮT giữa chừng, dừng gửi.")
                break

            chat_id = int(chat_key)
            
            # === LOGIC PAYWALL ===
            if chat_id not in pro_chat_ids and chat_id != ADMIN_ID:
                skipped_count += 1
                continue # Bỏ qua user thường
            # =====================

            watch_list = user_block.get("list", []) or []
            if not watch_list:
                skipped_count += 1
                continue

            symbols = [
                s.upper()
                for s in watch_list
                if not s.upper().startswith("VN")
            ]
            if not symbols:
                skipped_count += 1
                continue

            cache_key = make_report_cache_key(symbols)
            log.info(
                f"{instance_label} Xử lý Pro user: chat_id={chat_id}, cache_key={cache_key}"
            )

            cached = get_report_from_redis(cache_key)
            if cached is not None:
                cached_text, generated_at = cached
                log.info(
                    f"{instance_label} Cache hit (Redis) cho {chat_id} ({cache_key}), generated_at={generated_at}"
                )
                await asyncio.to_thread(send_msg_to, chat_id, cached_text)
                sent_count += 1
                await asyncio.sleep(3)
                continue

            # (Hàm lồng bên trong được giữ nguyên)
            async def fetch_report_with_retry():
                retry = 0
                last_text = "⚠️ Hiện tại không tạo được báo cáo, vui lòng thử lại sau."
                while retry < 3:
                    start = time.time()
                    text = await asyncio.to_thread(
                        call_chatgpt_for_report, symbols
                    )
                    duration = time.time() - start
                    log.info(
                        f"{instance_label} Round {retry+1} cho {chat_id} ({duration:.1f}s)"
                    )
                    last_text = text

                    if "⚠️" not in text and "429" not in text:
                        return text
                    retry += 1
                    await asyncio.sleep(10 * retry)
                return last_text
            
            try:
                text = await fetch_report_with_retry()
                save_report_to_redis(cache_key, text, source="weekly_loop" if not admin_chat_id else "admin_trigger")
                now_footer = datetime.datetime.now(vn_tz)
                footer = f"\n\n🕓 _Báo cáo được tạo vào {now_footer.strftime('%d/%m/%Y %H:%M')} — dữ liệu có thể thay đổi theo thời gian._"
                final_text = text.strip() + footer
                await asyncio.to_thread(send_msg_to, chat_id, final_text)
                log.info(
                    f"{instance_label} Đã gửi báo cáo tuần cho {chat_id}"
                )
                sent_count += 1
            except Exception as e:
                log.warning(
                    f"{instance_label} Lỗi gửi cho {chat_id}: {e}"
                )
            
            await asyncio.sleep(3) # Giữ nguyên sleep để tránh spam

        # === KẾT THÚC LOGIC CŨ ===

        final_msg = f"Hoàn tất — gửi {sent_count} (Pro), bỏ qua {skipped_count} (Free)."
        log.info(f"{instance_label} {final_msg}")
        if admin_chat_id:
            await tg_app.bot.send_message(admin_chat_id, f"✅ {final_msg}")

    except Exception as e:
        log.error(f"{instance_label} Lỗi tổng quát: {e}")
        if admin_chat_id:
            try:
                await tg_app.bot.send_message(admin_chat_id, f"❌ Lỗi tổng quát khi chạy Weekly Report: {e}")
            except Exception as e2:
                log.error(f"{instance_label} Lỗi gửi tin nhắn lỗi cho admin: {e2}")

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

        if not OPENROUTER_API_KEY:
            log.warning(
                f"{instance_label} Chưa có OPENROUTER_API_KEY, bỏ qua báo cáo tuần."
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

async def screener_value_update_loop():
    """
    Chạy precompute_value_data() vào 00:00 (giờ VN) các ngày Thứ 2–Thứ 6.
    
    (ĐÃ SỬA LỖI BLOCKING I/O)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # ❗️ KIỂM TRA TRẠNG THÁI BOT (kiểm tra trước khi ngủ dài)
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][VALUE {loop_id}] [precompute screener Value 00:00 T2–T6] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue # Quay lại vòng lặp, kiểm tra BOT_ACTIVE tiếp

        # Tính thời điểm 00:00 tiếp theo
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(days=1)

        # Nhảy qua cuối tuần (T7=5, CN=6)
        while next_run.weekday() > 4:
            next_run += datetime.timedelta(days=1)

        wait_sec = (next_run - now).total_seconds()
        log.info(
            f"[{INSTANCE_ID}][VALUE {loop_id}] Ngủ {wait_sec:.0f}s tới 00:00 ngày {next_run.date()} "
            f"(weekday={next_run.weekday()})."
        )
        await asyncio.sleep(max(wait_sec, 0))

        # ❗️ KIỂM TRA LẦN NỮA SAU KHI THỨC DẬY
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][VALUE {loop_id}] Thức dậy nhưng bot đang TẮT, bỏ qua.")
            continue

        # Đảm bảo đúng ngày T2–T6
        now = datetime.datetime.now(vn_tz)
        if now.weekday() > 4:
            log.info(f"[{INSTANCE_ID}][VALUE {loop_id}] Thức dậy nhưng không phải T2–T6, bỏ qua.")
            continue

        try:
            log.info(f"[{INSTANCE_ID}][VALUE {loop_id}] Bắt đầu chạy precompute_value_data() theo lịch.")
            
            # ⭐️ SỬA LỖI BLOCKING: Chạy hàm precompute (blocking) trong thread
            await asyncio.to_thread(precompute_value_data, True, True)
            
        except Exception:
            log.exception(f"[{INSTANCE_ID}][VALUE {loop_id}] Lỗi khi chạy precompute_value_data() theo lịch.")
            # tránh spam lỗi, nghỉ 1h rồi tính lịch mới
            await asyncio.sleep(3600)

#================= DAILY_SCREENER_LOOP ===============
async def execute_daily_screener(admin_update: Update | None = None):
    """
    Hàm lõi: Chạy, tính toán và gửi báo cáo screener.
    Nếu có admin_update, sẽ gửi phản hồi cho admin.
    """
    global INSTANCE_ID, log, tg_app # Cần tg_app để gửi tin

    instance_label = f"[{INSTANCE_ID}][EXEC_SCREENER]"
    admin_chat_id = admin_update.effective_chat.id if admin_update else None

    try:
        log.info(f"{instance_label} Bắt đầu chạy (trigger by: {'Admin' if admin_chat_id else 'Scheduler'}).")
        if admin_chat_id:
            # Gửi tin nhắn bằng bot instance toàn cục
            await tg_app.bot.send_message(admin_chat_id, "⏳ Bắt đầu chạy tác vụ gửi Daily Screener thủ công...")

        # === BÊ NGUYÊN LOGIC TỪ daily_screener_loop VÀO ĐÂY ===
        
        # 1. Tính toán
        result = await asyncio.to_thread(compute_value_screener)
        
        # 2. Format
        text = format_screener_report_text(result)
        
        if not text:
            log.warning(f"{instance_label} Không có dữ liệu screener để gửi (text=None).")
            if admin_chat_id:
                await tg_app.bot.send_message(admin_chat_id, "⚠️ Không có dữ liệu screener để gửi (text=None).")
            return # Dừng hàm

        # 3. Gửi cho tất cả user (Pro)
        log.info(f"{instance_label} Bắt đầu broadcast báo cáo screener...")
        
        await broadcast_to_all_watchers(text, target_audience="pro")
        
        # === KẾT THÚC LOGIC CŨ ===

        log.info(f"{instance_label} Hoàn tất broadcast.")
        if admin_chat_id:
            await tg_app.bot.send_message(admin_chat_id, "✅ Đã gửi Daily Screener thành công.")

    except Exception as e:
        log.exception(f"{instance_label} Lỗi nghiêm trọng: {e}")
        if admin_chat_id:
            try:
                await tg_app.bot.send_message(admin_chat_id, f"❌ Lỗi khi chạy Daily Screener: {e}")
            except Exception as e2:
                log.error(f"{instance_label} Lỗi gửi tin nhắn lỗi cho admin: {e2}")

async def daily_screener_loop():
    """
    (Đã sửa) Gửi báo cáo screener value (CHỈ LÊN LỊCH)
    vào 09:00 sáng T2-T6.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1
        instance_label = f"[{INSTANCE_ID}][SCREENER {loop_id}]" # Giữ log label

        # ❗️ KIỂM TRA TRẠNG THÁI BOT
        if not BOT_ACTIVE:
            log.info(f"{instance_label} [gửi báo cáo screener 09:00 T2-T6] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        wait_sec = seconds_until_next_weekday_screener()
        log.info(
            f"{instance_label} Ngủ tới 09:00 ngày làm việc tiếp theo, còn {wait_sec:.0f}s"
        )
        await asyncio.sleep(wait_sec)

        # ❗️ KIỂM TRA LẦN NỮA SAU KHI THỨC DẬY
        if not BOT_ACTIVE:
            log.info(f"{instance_label} Thức dậy nhưng bot đang TẮT, bỏ qua.")
            continue

        now = datetime.datetime.now(vn_tz)
        if now.weekday() > 4: # 5=T7, 6=CN
            log.info(f"{instance_label} Thức dậy nhưng không phải T2-T6, bỏ qua.")
            continue
        
        try:
            # Chỉ cần gọi hàm lõi (không truyền admin_update)
            log.info(f"{instance_label} 09:00, bắt đầu chạy execute_daily_screener() theo lịch.")
            await execute_daily_screener(admin_update=None)
            
        except Exception as e:
            # Lỗi này chỉ là lỗi khi *gọi* hàm, chứ ko phải lỗi *trong* hàm
            log.exception(f"{instance_label} Lỗi nghiêm trọng khi gọi execute_daily_screener: {e}")
            await asyncio.sleep(300) # 5 phút retry nếu lỗi
#-----------------------------------------

async def news_specialized_loop():
    """
    Quét RSS chuyên ngành, tìm bài có chứa mã cổ phiếu HOẶC tên doanh nghiệp
    trong danh mục của user. Gửi tin nhắn riêng cho từng user có bài liên quan.
    
    (ĐÃ SỬA LỖI BLOCKING I/O)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    warmed_up = False

    # Gộp toàn bộ URL chuyên ngành
    all_specialized_urls: list[str] = []
    for urls in RSS_FEEDS_SPECIALIZED.values():
        all_specialized_urls.extend(urls)

    while True:
        loop_id += 1

        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        try:
            # 1. Fetch RSS (OK, đã chạy non-blocking)
            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, all_specialized_urls
            )

            # 2. Warm-up
            if not warmed_up:
                # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                count_in_db = await asyncio.to_thread(
                    get_news_seen_count, NEWS_FEED_TYPE_SPECIALIZED
                )
                
                if count_in_db == 0 and entries:
                    for it in entries:
                        # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                        await asyncio.to_thread(
                            mark_news_seen,
                            NEWS_FEED_TYPE_SPECIALIZED,
                            link=it["link"],
                            guid=None,
                            title=it["title"],
                            published=it["published"],
                        )
                    warmed_up = True
                    log.info(
                        f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Warm-up lần đầu, lưu {len(entries)} bài, KHÔNG gửi tin."
                    )
                    await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)
                    continue
                else:
                    warmed_up = True

            if not entries:
                log.info(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Không có bài RSS.")
                await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)
                continue

            # 3. Lọc bài mới (Sửa list comprehension thành for loop)
            # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
            new_entries = []
            for it in entries:
                # (Sửa lỗi: dùng đúng hằng số TYPE)
                is_seen = await asyncio.to_thread(
                    has_news_seen, NEWS_FEED_TYPE_SPECIALIZED, it["link"]
                )
                if not is_seen:
                    new_entries.append(it)
            
            if not new_entries:
                log.info(
                    f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Không có bài chuyên ngành mới."
                )
                await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)
                continue

            # Lọc trùng theo link trong chính mẻ new_entries (phòng xa)
            unique_by_link: dict[str, dict[str, Any]] = {}
            for it in new_entries:
                link = (it.get("link") or "").strip()
                if not link:
                    continue
                if link in unique_by_link:
                    continue
                unique_by_link[link] = it

            new_entries = list(unique_by_link.values())

            # 4. Lấy danh sách user
            # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
            all_watch = await asyncio.to_thread(get_all_watch)
            
            symbol_to_chats: dict[str, list[int]] = {}
            for chat_key, block in all_watch.items():
                try:
                    cid = int(chat_key)
                except Exception:
                    continue

                # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                is_enabled = await asyncio.to_thread(
                    is_news_enabled_for_chat, cid, NEWS_FEED_TYPE_SPECIALIZED
                )
                if not is_enabled:
                    continue

                lst = block.get("list", []) or []
                if not lst:
                    continue

                for sym in lst:
                    s = sym.upper()
                    symbol_to_chats.setdefault(s, []).append(cid)

            if not symbol_to_chats:
                # Không có ai quan tâm, chỉ đánh dấu đã xem
                for it in new_entries:
                    # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                    await asyncio.to_thread(
                        mark_news_seen,
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=it["link"],
                        guid=None,
                        title=it["title"],
                        published=it["published"],
                    )
                await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)
                continue

            # 5. Compile pattern (OK, non-blocking)
            patterns: dict[str, re.Pattern] = {}
            for sym in symbol_to_chats.keys():
                keywords = COMPANY_KEYWORDS.get(sym, [sym])
                combined = "|".join(re.escape(k) for k in keywords if k)
                if not combined:
                    continue
                patterns[sym] = re.compile(rf"\b({combined})\b", re.IGNORECASE)

            # Chuẩn bị map: mỗi chat_id -> list các message bài báo
            news_by_chat: dict[int, list[str]] = {}

            # 6. Xử lý từng bài mới
            for it in new_entries:
                title = it["title"] or ""
                raw_summary = it.get("summary") or ""
                link = it["link"] or ""
                source = it.get("source") or ""
                pub_dt = it.get("published")

                # (Xử lý text - OK, non-blocking)
                decoded_summary = clean_html_text(raw_summary)
                text_for_match = (title + " " + decoded_summary)

                if not text_for_match.strip():
                    # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                    await asyncio.to_thread(
                        mark_news_seen,
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=link,
                        guid=None,
                        title=title,
                        published=pub_dt,
                    )
                    continue

                # (Regex match - OK, non-blocking)
                affected: dict[int, list[str]] = {}
                for sym, pat in patterns.items():
                    if pat.search(text_for_match):
                        for cid in symbol_to_chats.get(sym, []):
                            affected.setdefault(cid, []).append(sym)

                if not affected:
                    # Không liên quan mã nào trong watchlist -> chỉ đánh dấu đã seen
                    await asyncio.to_thread(
                        mark_news_seen,
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=link,
                        guid=None,
                        title=title,
                        published=pub_dt,
                    )
                    continue
                
                # (Xử lý thời gian & tóm tắt - OK, non-blocking)
                if isinstance(pub_dt, datetime.datetime):
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    pub_str = ""
                short_sum = decoded_summary
                if len(short_sum) > 300:
                    short_sum = short_sum[:280].rstrip() + "..."

                # Build message cho từng chat, nhưng CHƯA gửi vội
                for chat_id, syms in affected.items():
                    uniq_syms = sorted(set(syms))
                    lines = [
                        "📰 *Tin tức mới liên quan tới danh mục của bạn:*",
                        title,
                        "",
                        "*Liên quan tới:* " + ", ".join(uniq_syms),
                    ]
                    if short_sum:
                        lines.extend(["", f"{short_sum}"])
                    if source or pub_str:
                        lines.append("")
                        meta = []
                        if source:
                            meta.append(f"_Nguồn: {source}_")
                        if pub_str:
                            meta.append(f"_Thời gian: {pub_str}_")
                        lines.append(" | ".join(meta))
                    if link:
                        lines.append("")
                        lines.append(f"🔗 {link}")
                    text = "\n".join(lines)

                    news_by_chat.setdefault(chat_id, []).append(text)

                # ⭐️ Đánh dấu bài này đã xử lý (cho dù gửi cho bao nhiêu chat)
                await asyncio.to_thread(
                    mark_news_seen,
                    NEWS_FEED_TYPE_SPECIALIZED,
                    link=link,
                    guid=None,
                    title=title,
                    published=pub_dt,
                )

            # 7. Gửi bài cho từng user: 1 header có chuông + các bài silent (tối đa 3)
            for chat_id, articles in news_by_chat.items():
                total = len(articles)
                max_per_chat = NEWS_MAX_ARTICLES_PER_CHAT
                to_send = articles[:max_per_chat]

                # Nếu chỉ có 1 bài -> gửi thẳng, không header, không silent
                if total == 1:
                    await asyncio.to_thread(send_msg_to, chat_id, to_send[0])
                    await asyncio.sleep(0.2)
                    continue

                # Nhiều hơn 1 bài -> 1 header + các bài silent
                header_lines = [
                    f"📰 Hiện tại có {total} bài báo *chuyên ngành* mới liên quan tới danh mục của bạn.",
                ]
                if total > max_per_chat:
                    header_lines.append(
                        f"Mình sẽ gửi *{max_per_chat} bài tiêu biểu* ở chế độ im lặng để tránh làm phiền bạn."
                    )
                else:
                    header_lines.append(
                        "Mình sẽ gửi từng bài ngay sau đây ở chế độ *im lặng* để tránh làm phiền bạn."
                    )
                header_text = "\n".join(header_lines)

                # Header: gửi bình thường (có noti)
                await asyncio.to_thread(send_msg_to, chat_id, header_text)
                await asyncio.sleep(0.2)

                # Các bài: gửi silent để user vẫn thấy nhưng không rung máy
                for text in to_send:
                    await asyncio.to_thread(
                        send_msg_to, chat_id, text, "Markdown", True  # silent=True
                    )
                    await asyncio.sleep(0.2)  # (OK, non-blocking)


        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Lỗi tổng quát: {e}")

        await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS) # (OK, non-blocking)

async def news_macro_loop():
    """
    Quét RSS vĩ mô, nếu có bài mới thì broadcast cho tất cả user
    (nhưng CHỈ những user chưa tắt tin vĩ mô).
    
    (ĐÃ SỬA LỖI BLOCKING I/O)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    warmed_up = False

    while True:
        loop_id += 1

        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        try:
            # 1. Fetch RSS (OK, đã chạy non-blocking)
            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, RSS_FEEDS_MACRO
            )

            # 2. Warm-up
            if not warmed_up:
                # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                count_in_db = await asyncio.to_thread(
                    get_news_seen_count, NEWS_FEED_TYPE_MACRO
                )
                
                if count_in_db == 0 and entries:
                    for it in entries:
                        # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                        await asyncio.to_thread(
                            mark_news_seen,
                            NEWS_FEED_TYPE_MACRO,
                            link=it["link"],
                            guid=None,
                            title=it["title"],
                            published=it["published"],
                        )
                    warmed_up = True
                    log.info(
                        f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Warm-up lần đầu, lưu {len(entries)} bài, KHÔNG gửi tin."
                    )
                    await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS)
                    continue
                else:
                    warmed_up = True

            if not entries:
                log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Không có bài RSS.")
                await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS)
                continue

            # 3. Lọc bài mới (Sửa list comprehension thành for loop)
            # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
            new_entries = []
            for it in entries:
                is_seen = await asyncio.to_thread(
                    has_news_seen, NEWS_FEED_TYPE_MACRO, it["link"]
                )
                if not is_seen:
                    new_entries.append(it)
            
            if not new_entries:
                log.info(
                    f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Không có bài vĩ mô mới."
                )
                await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS)
                continue

            # 4. Lấy danh sách user
            # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
            all_watch = await asyncio.to_thread(get_all_watch)

            # 5. Format toàn bộ bài vĩ mô mới vào list (chưa gửi vội)
            macro_texts: list[str] = []
            total_articles = 0

            for it in new_entries:
                title = it["title"] or ""
                link = it["link"] or ""
                source = it.get("source") or ""
                pub_dt = it.get("published")

                # (Xử lý ngày giờ, làm sạch summary - OK, không blocking)
                if isinstance(pub_dt, datetime.datetime):
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    pub_str = ""
                raw_summary = it.get("summary") or ""
                clean_summary = clean_html_text(raw_summary)
                short_sum = clean_summary
                if len(short_sum) > 400:
                    short_sum = short_sum[:380].rstrip() + "..."
                
                # (Ghép nội dung - OK, không blocking)
                lines = [
                    f"🌏 *Tin vĩ mô mới:*\n",
                    f"*{title}*",
                ]
                if short_sum:
                    lines.extend(["", short_sum])
                meta = []
                if source:
                    meta.append(f"_Nguồn:_ {source}")
                if pub_str:
                    meta.append(f"_Thời gian: {pub_str}_")
                if meta:
                    lines.append("")
                    lines.append(" | ".join(meta))
                if link:
                    lines.append("")
                    lines.append(f"🔗 {link}")
                text = "\n".join(lines)

                macro_texts.append(text)
                total_articles += 1

                # 6. Đánh dấu đã xử lý bài này (dù gửi cho bao nhiêu user)
                await asyncio.to_thread(
                    mark_news_seen,
                    NEWS_FEED_TYPE_MACRO,
                    link=link,
                    guid=None,
                    title=title,
                    published=pub_dt,
                )

            if not macro_texts:
                log.info(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Không có bài vĩ mô mới sau khi lọc.")
                # quay lại vòng while, sẽ sleep ở cuối hàm
            else:
                max_macro = NEWS_MACRO_MAX_ARTICLES_PER_RUN
                to_send_global = macro_texts[:max_macro]

                # 7. Gửi tới từng user (header + silent)
                total_sent_users = 0
                for chat_key in all_watch.keys():
                    try:
                        chat_id = int(chat_key)
                    except Exception:
                        continue

                    # ⭐️ SỬA LỖI DB: Chạy CSDL trong thread
                    is_enabled = await asyncio.to_thread(
                        is_news_enabled_for_chat, chat_id, NEWS_FEED_TYPE_MACRO
                    )
                    if not is_enabled:
                        continue

                    # Chỉ 1 bài -> gửi thẳng, không header, không silent
                    if total_articles == 1:
                        await asyncio.to_thread(send_msg_to, chat_id, macro_texts[0])
                        await asyncio.sleep(0.2)
                        total_sent_users += 1
                        continue

                    # >= 2 bài -> header + các bài silent (tối đa 3)
                    header_lines = [
                        f"🌏 Hiện tại có {total_articles} bài báo *vĩ mô* mới đáng chú ý.",
                    ]
                    if total_articles > max_macro:
                        header_lines.append(
                            f"Mình sẽ gửi *{max_macro} bài tiêu biểu* ở chế độ im lặng để tránh làm phiền bạn."
                        )
                    else:
                        header_lines.append(
                            "Mình sẽ gửi từng bài ngay sau đây ở chế độ *im lặng* để tránh làm phiền bạn."
                        )
                    header_text = "\n".join(header_lines)

                    # Header: gửi bình thường, có noti
                    await asyncio.to_thread(send_msg_to, chat_id, header_text)
                    await asyncio.sleep(0.2)

                    # Các bài: silent để không spam noti
                    for text in to_send_global:
                        await asyncio.to_thread(
                            send_msg_to, chat_id, text, "Markdown", True  # silent=True
                        )
                        await asyncio.sleep(0.2)

                    total_sent_users += 1

                log.info(
                    f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Đã gửi tin vĩ mô tới {total_sent_users} user "
                    f"(tối đa {max_macro} bài / user, tổng {total_articles} bài trong vòng quét này)."
                )


        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Lỗi tổng quát: {e}")

        await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS) # (OK, non-blocking)

async def news_cleanup_loop():
    """
    Loop dọn news_seen (giữ lại để tương thích, nhưng Redis đã tự xoá theo TTL).
    """
    while True:
        log.info("[NEWS_CLEANUP] Redis tự xoá news_seen, không cần dọn thủ công.")
        await asyncio.sleep(3600)  # chạy mỗi 1h chỉ để log nhắc nhẹ

# ===== VN30F1M realtime alert (no Redis) =====================================
# Yêu cầu: mốc di động ±5 điểm, 15s/lần, gửi có thông báo, chiều dùng mốc sáng,
# reset theo ngày mới; user có thể bật/tắt, admin quản qua BOT_ACTIVE.

# --- Tham số riêng
VN30F1M_SYMBOL = "VN30F1M"
VN30F1M_DELTA_THRESHOLD = 5.0    # ±5 điểm
VN30F1M_TICK_SECONDS = 3        # chu kỳ quét

# --- State trong RAM
_vn30f1m_anchor: float | None = None      # mốc di động trong ngày
_vn30f1m_date: datetime.date | None = None
_vn30f1m_enabled_cache: set[int] = set()  # tập chat_id đang bật

# --- [TỐI ƯU] Thêm các biến dùng chung cho 3 tác vụ ---
_vn30f1m_broadcast_queue = asyncio.Queue()
_vn30f1m_current_price_cache: float | None = None
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
    """Đầu ngày mới: reset anchor (phiên chiều dùng mốc đã hình thành từ sáng)."""
    global _vn30f1m_date, _vn30f1m_anchor
    if (_vn30f1m_date is None) or (now.date() != _vn30f1m_date):
        _vn30f1m_date = now.date()
        _vn30f1m_anchor = None
        log.info(f"[VN30F1M] New trading day: {_vn30f1m_date}. Reset anchor.")

def _vn30f1m_clear_after_close():
    """Cuối ngày: xóa sạch state trong RAM (không dùng Redis)."""
    global _vn30f1m_anchor
    if _vn30f1m_anchor is not None:
        log.info("[VN30F1M] End of day → clear in-memory anchor.")
    _vn30f1m_anchor = None

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
    (V7 - Đã tối ưu) Lấy giá 1m.
    Sử dụng object 'quote_vn30f1m' toàn cục, không khởi tạo lại.
    """
    try:
        # 1. Lấy ngày hôm nay (giờ VN)
        vn_tz = pytz.timezone(TIMEZONE)
        today = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")

        # 2. Dùng object 'quote_vn30f1m' (đã khởi tạo 1 lần)
        df = await asyncio.to_thread(quote_vn30f1m.history, start=today, end=today, interval="1m")
        
        if df is None or df.empty:
            log.warning(f"[VN30F1M] quote.history(interval='1m') trả về rỗng cho hôm nay.")
            # (Fallback)
            try:
                df_1d = await asyncio.to_thread(quote_vn30f1m.history, start=today, end=today, interval="1D")
                if df_1d is not None and not df_1d.empty:
                    return float(df_1d.iloc[-1]["close"])
            except Exception:
                pass 
            return None

        # 3. Lấy giá 'close' của dòng CUỐI CÙNG
        price = float(df.iloc[-1]["close"])
        return price

    except (Exception, SystemExit) as e:
        log.warning(f"[VN30F1M] Lỗi khi lấy giá 1m (quote.history): {e}")
        return None

async def _vn30f1m_process_tick(price: float):
    """
    (Đã tối ưu + log)
    - KHÔNG gửi tin nhắn (để tránh block).
    - Chỉ so sánh mốc anchor.
    - Nếu có biến động -> ĐẨY (put) tin nhắn vào Queue.
    """
    global _vn30f1m_anchor
    if _vn30f1m_anchor is None:
        _vn30f1m_anchor = float(price)
        log.info(f"[VN30F1M][PROCESS]     >>> ⛳ Anchor set = {_vn30f1m_anchor:.2f}")
        return

    delta = float(price) - _vn30f1m_anchor
    
    # Log so sánh
    log.info(f"[VN30F1M][PROCESS]     >>> Delta: {delta:.2f} (Hiện tại: {price:.2f} | Mốc: {_vn30f1m_anchor:.2f})")
    
    if abs(delta) >= VN30F1M_DELTA_THRESHOLD:
        log.info(f"[VN30F1M][PROCESS]     >>> VƯỢT MỐC! ({abs(delta):.2f} >= {VN30F1M_DELTA_THRESHOLD})")
        
        direction = "tăng" if delta > 0 else "giảm"
        pct = (delta / _vn30f1m_anchor) * 100 if _vn30f1m_anchor else 0.0
        now_str = datetime.datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")
        text = (
            f"🔔 *VN30F1M {direction} {abs(delta):.2f} điểm* so với mốc gần nhất {_vn30f1m_anchor:.2f}\n"
            f"Giá hiện tại: *{float(price):.2f}* ({pct:+.2f}%) — {now_str}"
        )
        
        try:
            _vn30f1m_broadcast_queue.put_nowait(text)
            log.info("[VN30F1M][PROCESS]     >>> Đã đẩy tin nhắn vào Queue.")
        except asyncio.QueueFull:
            log.warning("[VN30F1M][PROCESS]     >>> LỖI: Broadcast queue bị đầy, bỏ lỡ 1 tin nhắn.")

        # Cập nhật mốc
        _vn30f1m_anchor = float(price)
        log.info(f"[VN30F1M][PROCESS]     >>> 🔁 Anchor moved to {_vn30f1m_anchor:.2f}")
    
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

    while True:
        loop_start = datetime.datetime.now(vn_tz)
        try:
            now = loop_start
            _vn30f1m_reset_if_new_day(now)

            if not BOT_ACTIVE:
                await asyncio.sleep(30)
                continue

            if not in_session_vietnam():
                _vn30f1m_clear_after_close()
                await asyncio.sleep(60)
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
    (Tác vụ 1: Fetcher - Yêu cầu 5 giây)
    - Loop này CHỈ LẤY GIÁ từ API.
    - Cập nhật _vn30f1m_current_price_cache.
    """
    global _vn30f1m_current_price_cache
    vn_tz = pytz.timezone(TIMEZONE)
    FETCH_INTERVAL = 10 # Theo yêu cầu của bạn
    last_healthcheck_date: datetime.date | None = None
    
    while True:
        loop_start = datetime.datetime.now(vn_tz)
        try:
            now = loop_start
            
            if last_healthcheck_date != now.date():
                log.info("[VN30F1M][FETCHER] Đầu ngày, chạy health-check 1D...")
                vn30f1m_day_healthcheck()
                last_healthcheck_date = now.date()

            if not BOT_ACTIVE:
                log.info("[VN30F1M][FETCHER] Bot OFF, ngủ 30s.")
                await asyncio.sleep(30)
                continue
                
            if not in_session_vietnam():
                log.info("[VN30F1M][FETCHER] Ngoài giờ, ngủ 60s.")
                await asyncio.sleep(60)
                continue
                
            price = await _vn30f1m_get_current_price()

            if price is not None:
                log.info(f"[VN30F1M][FETCHER] _vn30f1m_anchor = {_vn30f1m_anchor}")
                _vn30f1m_current_price_cache = float(price)
            else:
                log.warning("[VN30F1M][FETCHER] API trả về None.")

        except asyncio.CancelledError:
            log.info("[VN30F1M][FETCHER] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[VN30F1M][FETCHER] Lỗi: {e}")
        
        # Giữ nhịp 5 giây
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(0.1, FETCH_INTERVAL - elapsed)
        await asyncio.sleep(delay)

async def vn30f1m_broadcast_loop():
    """
    (Tác vụ 3: Broadcaster)
    - Loop này CHỈ GỬI TIN NHẮN.
    - Chờ tin nhắn trong Queue.
    """
    log.info("[VN30F1M][BCASTER] Bắt đầu. Chờ tin nhắn trong queue...")
    
    while True:
        try:
            # Chờ Ticker đẩy tin nhắn vào
            text = await _vn30f1m_broadcast_queue.get()
            
            if not text:
                _vn30f1m_broadcast_queue.task_done()
                continue

            user_count = len(_vn30f1m_enabled_cache)
            log.info(f"[VN30F1M][BCASTER] NHẬN ĐƯỢC TIN! Bắt đầu gửi cho {user_count} users...")
            log.info(f"[VN30F1M][BCASTER] Nội dung: {text.splitlines()[0]}") # Log dòng đầu của tin nhắn

            tasks = []
            for cid in list(_vn30f1m_enabled_cache):
                tasks.append(send_md(tg_app.bot, cid, text))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            _vn30f1m_broadcast_queue.task_done()
            log.info("[VN30F1M][BCASTER] Gửi tin xong. Quay lại chờ queue...")

        except asyncio.CancelledError:
            log.info("[VN30F1M][BCASTER] Bị huỷ (Cancelled).")
            break
        except Exception as e:
            log.warning(f"[VN30F1M][BCASTER] Lỗi: {e}")
            # Đảm bảo task_done được gọi ngay cả khi gather lỗi, 
            # để queue không bị kẹt
            if '_vn30f1m_broadcast_queue' in locals():
                try:
                    _vn30f1m_broadcast_queue.task_done()
                except ValueError:
                    pass # nếu task_done đã được gọi
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

# Thay thế hàm này trong alert_bot.py

async def financial_Statements_notice_loop():
    """
    (ĐÃ SỬA) Quét BCTC và chỉ thông báo cho user Pro + Admin.
    """

    vn_tz = pytz.timezone(TIMEZONE)

    while True:

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
        eight_am_today = vn_tz.localize(
            datetime.datetime(today.year, today.month, today.day, 8, 0, 0)
        )

        now = datetime.datetime.now(vn_tz)
        if now < two_am_today:
            log.info(
                f"[{INSTANCE_ID}][BCTC] Hôm nay {today} chưa tới 02:00, ngủ tới {two_am_today}."
            )
            await sleep_until(two_am_today, vn_tz)

        # 1.1️⃣ 02:00 -> CRAWL BCTC
        log.info(f"[{INSTANCE_ID}][BCTC] 02:00 – bắt đầu crawl BCTC {period_label} cho hôm nay.")

        try:
            all_watch = await asyncio.to_thread(get_all_watch)
            
            # === LOGIC PAYWALL (1) ===
            # Lấy danh sách Pro user để lọc
            pro_chat_ids = await asyncio.to_thread(get_all_pro_chat_ids)
            # ========================

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
            
            # === LOGIC PAYWALL (2) ===
            # Chỉ quét BCTC cho các mã nằm trong watchlist của Pro user (hoặc Admin)
            try:
                chat_id = int(chat_key)
                if chat_id not in pro_chat_ids and chat_id != ADMIN_ID:
                    continue # Bỏ qua user thường
            except Exception:
                continue
            # ========================

            syms = info.get("list") if isinstance(info, dict) else info
            if not syms:
                continue
            for sym in syms:
                s = str(sym).upper().strip()
                if s:
                    symbol_set.add(s)

        pending_after = 0

        # (Phần code crawl BCTC bên dưới giữ nguyên y hệt)
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
                await asyncio.to_thread(mark_bctc_notified, sym, year, quarter)
                await asyncio.to_thread(add_bctc_queue, sym, year, quarter, today)
            except Exception as e:
                log.warning(
                    f"[{INSTANCE_ID}][BCTC] Lỗi mark/add_queue({sym}, Q{quarter}/{year}): {e}"
                )

            await asyncio.sleep(0.2)

        still_pending = pending_after > 0
        log.info(
            f"[{INSTANCE_ID}][BCTC] Crawl xong BCTC (chỉ cho Pro user) {period_label} hôm nay. "
            f"still_pending = {still_pending}."
        )

        # 2️⃣ Đợi tới 08:00 để GỬI THÔNG BÁO
        now = datetime.datetime.now(vn_tz)
        if now < eight_am_today:
            log.info(
                f"[{INSTANCE_ID}][BCTC] Đợi tới 08:00 ({eight_am_today}) để gửi thông báo BCTC."
            )
            await sleep_until(eight_am_today, vn_tz)

        # 2.1️⃣ 08:00 -> GỬI THÔNG BÁO
        log.info(
            f"[{INSTANCE_ID}][BCTC] 08:00 – bắt đầu gửi thông báo BCTC {period_label}."
        )

        try:
            queue_rows = await asyncio.to_thread(get_bctc_queue_by_date, today)
        except Exception as e:
            log.warning(
                f"[{INSTANCE_ID}][BCTC] Lỗi get_bctc_queue_by_date({today}): {e}"
            )
            queue_rows = []

        if queue_rows:
            # (Phần code gửi BCTC này đã tự động đúng,
            # vì ở bước crawl (2h sáng) chúng ta đã CHỈ crawl
            # các mã của Pro user.
            # Giờ chúng ta chỉ cần gửi cho những ai theo dõi các mã này)
            
            try:
                all_watch_for_send = await asyncio.to_thread(get_all_watch)
            except Exception as e:
                log.warning(
                    f"[{INSTANCE_ID}][BCTC] Lỗi get_all_watch (notify): {e}"
                )
                all_watch_for_send = {}

            symbol_to_chats: dict[str, list[int]] = {}
            for chat_key, info in all_watch_for_send.items():
                syms = info.get("list") if isinstance(info, dict) else info
                if not syms:
                    continue
                try:
                    chat_id = int(chat_key)
                except Exception:
                    continue
                for sym in syms:
                    s = str(sym).upper().strip()
                    if not s:
                        continue
                    symbol_to_chats.setdefault(s, []).append(chat_id)

            for sym, y, q in queue_rows:
                chats = symbol_to_chats.get(sym, [])
                if not chats:
                    await asyncio.to_thread(
                        clear_bctc_queue_entry, sym, y, q, today
                    )
                    continue

                query_text = f"{sym} báo cáo tài chính"
                google_url = f"https://www.google.com/search?q={quote_plus(query_text)}"

                lines = [
                    "📑 *Báo cáo tài chính mới*",
                    "",
                    f"• Mã: *{sym}*",
                    f"• Kỳ: *Quý {q}/{y}*",
                    "",
                    "👉 Mã cổ phiếu trong danh sách theo dõi của bạn đã có báo cáo tài chính.",
                    "",
                    "Bạn có thể tra cứu nhanh trên Google với từ khóa:",
                    f"{query_text}",
                    google_url,
                ]
                text = "\n".join(lines)

                for chat_id in chats:
                    # (Không cần check Pro ở đây nữa, vì đã check lúc crawl)
                    try:
                        await send_md(tg_app.bot, chat_id, text)
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        log.warning(
                            f"[{INSTANCE_ID}][BCTC] Lỗi gửi BCTC cho {chat_id} – {sym}: {e}"
                        )

                await asyncio.to_thread(
                    clear_bctc_queue_entry, sym, y, q, today
                )

        else:
            log.info(
                f"[{INSTANCE_ID}][BCTC] Hôm nay không có mã nào trong hàng đợi BCTC."
            )

        # 3️⃣ Quyết định ngủ tới khi nào
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
    Gửi tin nhắn Markdown an toàn:
    - Lần 1: gửi nguyên văn (giữ format bạn viết).
    - Nếu lỗi 'Can't parse entities': escape toàn bộ rồi gửi lại.
    """
    try:
        return await update.message.reply_text(
            text,
            parse_mode="Markdown",
            **kwargs,
        )
    except BadRequest as e:
        logging.warning(f"[Markdown error] {e} | text={text!r}")
        safe_text = escape_markdown_v2(text)
        return await update.message.reply_text(
            safe_text,
            parse_mode="Markdown",
            **kwargs,
        )

def send_msg_to(chat_id: int, text: str, parse_mode: str | None = "Markdown", silent: bool = False):
    """Gửi tin nhắn Telegram, mặc định dùng Markdown (v1) với fallback an toàn.
    
    silent=True -> gửi disable_notification (tin nhắn im lặng, không bật noti). 
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
            # Gửi tin im lặng, không rung / popup trên điện thoại
            params["disable_notification"] = True

        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        return data

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
            log.warning(f"[WARN] Markdown parse error, retry with escaped text: {data}")
            safe_text = escape_markdown_v2(text)
            data = _do_send(safe_text, parse_mode, silent)

        if data.get("ok") and "result" in data:
            msg_id = data["result"]["message_id"]
            save_bot_message(chat_id, msg_id)
        else:
            log.warning(f"[WARN] Telegram send failed: {data}")

    except Exception as e:
        log.warning(f"[WARN] Telegram send error: {e}")


async def auto_on_after_delay(initial_active: bool):
    """
    Tự động bật lại bot sau 2 phút kể từ khi khởi động,
    *chỉ khi* trạng thái ban đầu là OFF và sau 2 phút vẫn còn OFF.
    """
    global BOT_ACTIVE

    # Nếu lúc start bot đang ON thì khỏi cần auto /on
    if initial_active:
        return

    await asyncio.sleep(120)  # 2 phút

    # Chỉ auto /on nếu tới lúc này bot vẫn đang OFF
    if BOT_ACTIVE is False:
        BOT_ACTIVE = True
        set_bot_active(True)
        log.info(f"[{INSTANCE_ID}] BOT auto switched ON after 2 minutes (initial OFF).")

        if ADMIN_ID:
            try:
                await send_md(
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
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return
    
    chat_id = update.effective_chat.id
    
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/start", ADMIN_ID)

    # ⭐️ SỬA: Chạy CSDL trong thread
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
    if lst is None:
        # ⭐️ SỬA: Chạy CSDL trong thread
        await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])

    await reply_md(
    update,
    "🎯 *Chào mừng Quý Nhà Đầu Tư đến với StockBot!* 🤖💹\n\n"
    "StockBot là *trợ lý chứng khoán realtime* – giúp bạn theo dõi biến động giá, tin tức và dữ liệu doanh nghiệp một cách nhanh chóng, chính xác,... 💩💩💩\n\n"
    "*--- (Gói Dùng Thử Miễn Phí 😏) ---*\n\n"
    "🎁 *Gói Miễn Phí (Free Plan)*\n"
    "Bot cung cấp các tính năng cơ bản để bạn trải nghiệm:\n"
    "• Theo dõi **tối đa 1 mã cổ phiếu** (dùng `/add <MÃ>`).\n"
    "• Xem danh sách (`/list`) và xoá (`/remove <MÃ>`).\n"
    "• Nhận thông báo tự động 4 mốc giờ giao dịch (sắp mở/đóng phiên sáng/chiều).\n"
    "• 📰 *Tin tức Theo Watchlist:* Bật/tắt nhận tin tức vĩ mô và tin chuyên ngành được lọc tự động theo danh mục của bạn (`/news_on`). "
    " Nhận diện bài viết liên quan đến mã trong danh mục và gửi những tin đáng chú ý nhất. 📰\n"
    "• ⚡ *Cảnh báo Giá Realtime:* Nhận cảnh báo tức thì (kèm nhận xét vui vẻ) khi mã trong watchlist của bạn biến động ±1%...±6% (quét 15 giây/lần).\n\n"
    "*--- (Gói Chuyên Nghiệp 😎) ---*\n\n"
    "👑 *Gói Nâng Cấp (Pro Plan) - 99k/tháng*\n"
    "Mở khoá toàn bộ sức mạnh của bot trợ lý AI:\n\n"
    "• 📈 *Theo dõi Không giới hạn:* Thêm không giới hạn số mã cổ phiếu vào watchlist.\n"
    "• 🧠 *Báo cáo AI Chuyên sâu:* Nhận báo cáo AI hàng tuần (`weekly_report_loop`) và phân tích chuyên sâu bất kỳ mã nào (`/info <MÃ>`) hoặc toàn bộ danh mục (`/report`).\n"
    "• 💰 *Bộ lọc Cổ phiếu Value:* Nhận báo cáo Top cổ phiếu Value (P/E, P/B, ROE, Vốn hoá > 5000 tỷ, TK > 50 tỷ) vào 09:00 mỗi sáng T2-T6 (`daily_screener_loop`).\n"
    "• 🧾 *Cảnh báo Báo cáo Tài chính:* Tự động báo cho bạn ngay khi công ty bạn theo dõi ra BCTC mới trong các tháng 1, 4, 5, 10 (`financial_Statements_notice_loop`).\n"
    "• 📊 *Cảnh báo Phái sinh:* Nhận alert realtime cho VN30F1M (`/vn30f1m_on`).\n\n"
    "--- (Cách Nâng Cấp) ---\n\n"
    f"🚀 *Cách Nâng Cấp Gói Pro:*\n"
    f"1. Liên hệ Admin qua Telegram: `https://t.me/KhoiTran99`\n"
    f"2. Chuyển khoản 99.000 VNĐ với nội dung: `PRO {chat_id}`\n"
    f"   *(Chat ID của bạn là: `{chat_id}`)*\n"
    f"Bot sẽ được kích hoạt tự động sau khi Admin xác nhận.\n\n"
    "📊 *Các lệnh dành cho nhà đầu tư:*\n"
    "• `/start` – Xem lại phần giới thiệu và danh sách tính năng\n"
    "• `/add <MÃ>` – Thêm mã cổ phiếu vào danh sách theo dõi\n"
    "• `/remove <MÃ>` – Xóa mã cổ phiếu khỏi danh sách\n"
    "• `/list` – Xem danh sách cổ phiếu bạn đang theo dõi\n"
    "• `/report` – Nhận báo cáo phân tích AI về danh mục của bạn 🧠\n"
    "• `/news_on` – Bật nhận tin tức (vĩ mô + chuyên ngành)\n"
    "• `/news_off` – Tắt nhận tin tức\n"
    "• `/news_status` – Xem trạng thái nhận tin tức hiện tại\n"
    "• `/vn30f1m_off` – Tắt nhận cập nhật VN30F1M\n"
    "• `/vn30f1m_on` – Bật nhận cập nhật VN30F1M\n"
    "• `/vn30f1m_status` – Xem trạng thái nhận cập nhật VN30F1M\n\n"
    "🛠 *Khu vực quản trị (admin – người dùng bình thường có thể bỏ qua):*\n"
    "• `/on` – Bật bot, thoát chế độ bảo trì\n"
    "• `/off` – Tắt bot, bật chế độ bảo trì tạm thời\n"
    "• `/status` – Kiểm tra trạng thái hoạt động của bot\n"
    "• `/announce` – Gửi thông báo đến tất cả người dùng\n"
    "• `/allwatch` – Xem & thống kê toàn bộ danh sách mã được user theo dõi\n"
    "• `/screener_value_clear` – Xóa dữ liệu screener cache\n"
    "• `/cmd_value_refresh_now` – Làm mới dữ liệu screener cache\n"
    "• `/delete_range` – Xóa tin nhắn bot gửi trong một khoảng thời gian\n"
    "• `/backup_core` – Backup dữ liệu core (watchlist, newsPref, BCTC)\n"
    "• `/restore_core` – Khôi phục dữ liệu core từ file backup\n"
    "• `/news_test_macro` – Gửi thử tin tức vĩ mô mới nhất\n"
    "• `/news_test_specialized` – Gửi thử tin tức chuyên ngành/doanh nghiệp\n\n"
    "• `/admin_add_user` – Thêm/gia hạn Gói Pro cho user\n"
    "• `/admin_deactivate` – Ngưng hoạt động Gói Pro của user\n"
    "• `/admin_remove_user` – Xoá vĩnh viễn Gói Pro của user\n\n"
    "• `/cmd_run_screener_now` – (Test) Chạy và gửi Daily Screener ngay lập tức\n"
    "• `/cmd_run_weekly_report_now` – (Test) Chạy và gửi Weekly Report ngay lập tức\n\n"
    "💬 Với StockBot, mọi biến động đều được cập nhật tức thì – để bạn không bỏ lỡ bất kỳ cơ hội nào.\n\n"
    "🚀 Bắt đầu theo dõi ngay hôm nay bằng lệnh `/add <MÃ>`!"
)
    
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
    mp = get_vn30f1m_enabled_map()
    enabled = bool(mp.get(chat_id, False))
    lines = [
        f"Trạng thái VN30F1M của bạn: *{'Đang bật' if enabled else 'Đang tắt'}*",
        "Ngưỡng: *±5 điểm* · Chu kỳ: *15s* · Mốc: *Di động*",
    ]
    if not BOT_ACTIVE:
        lines.append("_Lưu ý: Hệ thống đang tắt bởi admin (BOT_ACTIVE=False)._")
    lines.append("Bật: `/vn30f1m_on` · Tắt: `/vn30f1m_off`")
    await reply_md(update, "\n".join(lines))

# (Hãy chắc chắn rằng bạn đã import 'is_user_pro' từ db_utils.py ở đầu file)
# from db_utils import (
#     ...
#     is_user_pro,
#     ...
# )

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
    # 💎 LOGIC PAYWALL
    # ===============================================
    
    # 1. Kiểm tra Pro (CSDL trong thread)
    is_pro = await asyncio.to_thread(is_user_pro, chat_id) #
    
    # 2. Kiểm tra Admin
    is_admin = (chat_id == ADMIN_ID)
    
    # 3. Áp dụng luật
    if not is_pro and not is_admin: # Nếu là user thường
        log.warning(f"[PAYWALL] User {chat_id} (Free) bị chặn bật /vn30f1m_on.")
        await reply_md(
            update, 
            "⚠️ Tính năng cảnh báo phái sinh VN30F1M chỉ dành cho Gói Pro.\n\n"
            f"Vui lòng liên hệ Admin `https://t.me/KhoiTran99` để nâng cấp. 😏"
        )
        return # Dừng hàm, không cho bật
    
    # ===============================================
    # (Nếu vượt qua Paywall, tiếp tục bật)
    # ===============================================

    set_vn30f1m_enabled(chat_id, True)
    reload_vn30f1m_enabled_cache()
    
    await reply_md(update, "✅ (Pro) Đã *bật* nhận cảnh báo VN30F1M cho bạn.")

async def cmd_vn30f1m_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    set_vn30f1m_enabled(chat_id, False)
    reload_vn30f1m_enabled_cache()
    await reply_md(update, "Đã *tắt* nhận cảnh báo VN30F1M cho bạn.")

async def cmd_news_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/news_on", ADMIN_ID)
    await asyncio.to_thread(set_news_pref, chat_id, enable_specialized=True, enable_macro=True)
    
    await reply_md(
        update,
        "🔔 Bạn đã BẬT nhận tin tức:\n"
        "👉 Tin vĩ mô: Bật\n"
        "👉 Tin chuyên ngành theo danh mục: Bật\n\n"
        "💡 Có thể dùng `/news_off` nếu sau này muốn tạm tắt.",
    )


async def cmd_news_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/news_off", ADMIN_ID)
    await asyncio.to_thread(set_news_pref, chat_id, enable_specialized=False, enable_macro=False)

    await reply_md(
        update,
        "🔕 Bạn đã TẮT nhận mọi loại tin tức (vĩ mô & chuyên ngành).\n\n"
        "Có thể bật lại bất cứ lúc nào bằng lệnh `/news_on.`",
    )


async def cmd_news_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    # ⭐️ SỬA: Chạy CSDL trong thread
    pref = await asyncio.to_thread(get_news_pref, chat_id)

    macro = "Bật" if pref["enable_macro"] else "Tắt"
    spec = "Bật" if pref["enable_specialized"] else "Tắt"

    await reply_md(
        update,
        "⚙️ *Trạng thái nhận tin tức của bạn:*\n"
        f"- Tin vĩ mô: *{macro}*\n"
        f"- Tin chuyên ngành: *{spec}*\n\n"
        "Dùng `/news_on` hoặc `/news_off` để thay đổi.",
    )

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
    (ĐÃ REFACTOR) Thêm mã vào watchlist.
    - User thường: Giới hạn 1 mã.
    - Pro User / Admin: Không giới hạn.
    """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    # ⭐️ Log CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/add", ADMIN_ID)

    if not context.args:
        await reply_md(update,
            "⚠️ Cách dùng: /add <MÃ>\n"
            "Ví dụ: /add HPG, /add SSI, /add VNM\n"
            "(*Chỉ hỗ trợ mã cổ phiếu gồm 3 chữ cái.*)"
        )
        return

    symbol = context.args[0].strip().upper()

    await reply_md(update, f"🔎 Đang kiểm tra mã *{symbol}*, vui lòng đợi...")

    if len(symbol) != 3 or not symbol.isalpha():
        await reply_md(update,
            "⚠️ Mã không hợp lệ.\n"
            "Hiện bot chỉ cho phép thêm *mã cổ phiếu* gồm đúng 3 chữ cái, "
            "ví dụ: HPG, SSI, VNM."
        )
        return

    # ⭐️ Bọc các lệnh network blocking vào hàm riêng
    def _fetch_price_board(sym):
        # Hàm này là blocking
        trading = Trading(source="VCI")
        return trading.price_board([sym])

    try:
        # ⭐️ Chạy Network I/O trong thread
        df = await asyncio.to_thread(_fetch_price_board, symbol)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [ADD] Lỗi khi gọi price_board cho {symbol}: {e}")
        await reply_md(update,
            f"⚠️ Không lấy được dữ liệu cho mã *{symbol}*. Vui lòng thử lại sau."
        )
        return

    if df is None or len(df) == 0:
        await reply_md(update,
            f"⚠️ Không tìm thấy dữ liệu giao dịch cho mã *{symbol}*.\n"
            "Vui lòng kiểm tra lại mã hoặc thử mã khác.\n"
            "(*Chỉ hỗ trợ cổ phiếu đang giao dịch trên HOSE/HNX/UPCOM.*)"
        )
        return

    row = df.iloc[0]

    # (Hàm norm và logic lấy giá an toàn, không I/O)
    def norm(x):
        if x is None:
            return None
        try:
            if hasattr(x, "item"):
                x = x.item()
        except Exception:
            pass
        if isinstance(x, (int, float)):
            return x
        try:
            return float(x)
        except Exception:
            return None
            
    price = None
    pct = None
    change_abs = None
    volume = None
    exchange = None
    try:
        # (Sửa lỗi logic đọc giá dựa trên output API bạn đã test)
        price = norm(row.get(("match", "match_price"), None))
        ref_price = norm(
            row.get(("match", "reference_price"))
            if ("match", "reference_price") in row.index 
            else row.get(("listing", "ref_price"))
        )
        
        if price is not None and ref_price is not None and ref_price != 0:
            change_abs = price - ref_price
            pct = (change_abs / ref_price) * 100.0
            
    except Exception:
        pass # Bỏ qua lỗi nhỏ, các biến sẽ là None
        
    try:
        volume = norm(row.get(("match", "accumulated_vol"), None))
    except Exception:
        pass
    try:
        exchange = row.get(("listing", "exchange"), None)
    except Exception:
        exchange = None

    if price is None:
        await reply_md(update,
            f"⚠️ Không tìm thấy dữ liệu giao dịch cho mã *{symbol}*.\n"
            "Vui lòng kiểm tra lại mã hoặc thử mã khác.\n"
            "(*Chỉ hỗ trợ cổ phiếu đang giao dịch trên HOSE/HNX/UPCOM.*)"
        )
        return

    if price == 0:
        await reply_md(update,
            f"⚠️ Hiện chưa có dữ liệu giao dịch cho mã *{symbol}*.\n\n"
            "🕒 Trong vòng *2 tiếng trước khi phiên giao dịch bắt đầu*, hệ thống có thể "
            "tạm thời không thêm được mã mới do sàn chưa cập nhật dữ liệu.\n\n"
            "👉 Vui lòng thử lại sau khi thị trường mở cửa để đảm bảo dữ liệu chính xác."
        )
        return

    # ⭐️ Lấy watchlist hiện tại (CSDL trong thread)
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []

    if symbol in lst:
        symbols_text = ", ".join(lst) if lst else "—"
        msg = (
            f"ℹ️ *{symbol}* đã có trong danh sách theo dõi rồi.\n\n"
            "📋 *Danh sách mã bạn đang theo dõi hiện tại:*\n"
            f"{symbols_text}"
        )
        await reply_md(update, msg)
        return

    # ===============================================
    # 💎 LOGIC PAYWALL / GIỚI HẠN MÃ MỚI
    # ===============================================
    
    # 1. Kiểm tra Pro (CSDL trong thread)
    is_pro = await asyncio.to_thread(is_user_pro, chat_id)
    
    # 2. Kiểm tra Admin
    is_admin = (chat_id == ADMIN_ID)
    
    # 3. Lấy số lượng mã hiện tại
    current_stock_count = len(lst)
    
    # 4. Áp dụng luật
    if not is_pro and not is_admin: # Nếu là user thường
        if current_stock_count >= 1:
            log.warning(f"[PAYWALL] User {chat_id} (Free) bị chặn thêm mã {symbol}. Đã đạt giới hạn 1 mã.")
            await reply_md(update,
                f"⚠️ Tài khoản miễn phí chỉ được theo dõi tối đa **1 mã**.\n"
                f"Bạn đang theo dõi: {lst[0]}\n\n"
                f"Vui lòng `/remove {lst[0]}` trước khi thêm mã mới, hoặc nâng cấp lên Gói Pro để theo dõi không giới hạn.\n\n"
                f"Liên hệ Admin: `https://t.me/KhoiTran99`"
            )
            return # Dừng hàm, không cho thêm

    # ===============================================
    # (Nếu vượt qua Paywall, tiếp tục thêm mã)
    # ===============================================

    lst.append(symbol)
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(save_watch_list_for_chat, chat_id, lst)

    symbols_text = ", ".join(lst)
    watchlist_section = (
        "\n\n📋 *Danh sách mã bạn đang theo dõi hiện tại:*\n"
        f"{symbols_text}"
    )

    try:
        change_sign = "+" if (pct is not None and pct >= 0) else ""
        pct_text = f"{change_sign}{pct:.2f}%" if pct is not None else "—"
        abs_text = (
            f"{change_sign}{int(change_abs):,}".replace(",", ".")
            if change_abs is not None else "—"
        )

        summary = (
            f"📈 *{symbol}* đã được thêm vào danh sách theo dõi.\n\n"
            f"💰 Giá hiện tại: *{price:,.0f}*\n"
            f"📊 Thay đổi: *{pct_text}* ({abs_text})\n"
        )

        if volume is not None:
            summary += f"📦 Khối lượng: *{int(volume):,}* cp\n"
        if exchange:
            summary += f"🏛️ Sàn: *{exchange}*\n"

        summary += watchlist_section

        await reply_md(update,summary)

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [ADD] Lỗi khi format summary cho {symbol}: {e}")
        fallback_msg = (
            f"✅ Đã thêm *{symbol}* vào danh sách theo dõi.\n"
            f"{watchlist_section}"
        )
        await reply_md(update,fallback_msg)

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
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return
    
    await reply_md(update, f"🔎 vui lòng đợi...")

    chat_id = update.effective_chat.id
    # ⭐️ SỬA: Chạy CSDL trong thread
    await asyncio.to_thread(log_command_usage, chat_id, "/list", ADMIN_ID)

    # ⭐️ SỬA: Chạy CSDL trong thread
    lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []

    if not lst:
        await reply_md(update,
            "📭 Danh sách theo dõi hiện đang trống.\n"
            "Bạn có thể dùng lệnh /add <MÃ> để thêm cổ phiếu vào danh sách.",
        )
        return

    symbols_text = ", ".join(lst)
    msg = (
        "📋 *Danh sách mã bạn đang theo dõi:*\n"
        f"{symbols_text}\n\n"
        "Bạn có thể dùng /remove <MÃ> để xoá một mã khỏi danh sách."
    )
    await reply_md(update, msg)

# Dùng dict lưu tạm xác nhận theo admin_id
pending_clear_confirmations = {}

# Dùng dict lưu tạm xác nhận theo admin_id
pending_clear_confirmations = {}

async def cmd_screener_value_clear(update: Update, context):
    """
    [ĐÃ SỬA] Xoá toàn bộ cache screener value trong PostgreSQL (bảng stock_value_cache).
    Đây là hành động nguy hiểm, yêu cầu xác nhận.
    
    Cách dùng: /screener_value_clear confirm
    """
    chat_id = update.effective_chat.id
    if chat_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    args = context.args if context.args else []
    
    # Bước 1: Yêu cầu xác nhận
    if not args or args[0].lower() != "confirm":
        # ⭐️ Chạy CSDL trong thread
        count = await asyncio.to_thread(get_stock_value_cache_count)
        
        await reply_md(
            update,
            f"⚠️ *Xác nhận xoá toàn bộ cache Value?*\n\n"
            f"Hành động này sẽ xoá *toàn bộ {count} dòng* trong bảng `stock_value_cache` của PostgreSQL.\n"
            f"Bot sẽ cần chạy lại `/value_refresh_now` (tốn nhiều thời gian) để có lại dữ liệu.\n\n"
            f"Để xác nhận, gõ chính xác lệnh:\n"
            f"`/screener_value_clear confirm`"
        )
        return

    # Bước 2: Đã xác nhận -> Thực hiện xoá
    try:
        await reply_md(update, "⏳ *[ADMIN]* Đang thực hiện TRUNCATE bảng `stock_value_cache` trong PostgreSQL...")
        
        # ⭐️ Chạy CSDL trong thread
        await asyncio.to_thread(clear_stock_value_cache)
        
        await reply_md(update, "✅ *[ADMIN]* Đã xoá thành công toàn bộ dữ liệu trong `stock_value_cache`.")
        
    except Exception as e:
        log.exception("[ADMIN] Lỗi xoá stock_value_cache (PostgreSQL): %s", e)
        await reply_md(update, f"*[ADMIN]* Lỗi khi xoá bảng `stock_value_cache`: `{e}`")

async def cmd_value_refresh_now(update: Update, context):
    """
    Chạy precompute_value_data(full_refresh=True, skip_if_fresh_today=False)
    -> Quét toàn bộ mã và upsert vào stock_value_cache.
    -> KHÔNG xoá Redis, KHÔNG xoá bảng.
    """
    await reply_md(update, "*[ADMIN]* Đang full-refresh dữ liệu Value trong nền…")
    try:
        await asyncio.to_thread(precompute_value_data, True, False)
        await reply_md(update, "*[ADMIN]* Hoàn tất full-refresh Value ✅")
    except Exception as e:
        log.exception("[ADMIN] Lỗi full-refresh Value: %s", e)
        await reply_md(update, f"*[ADMIN]* Lỗi full-refresh: `{e}`")

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

async def cmd_allwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ (ĐÃ SỬA LỖI BLOCKING I/O) """
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    # ⭐️ SỬA: Chạy CSDL trong thread
    all_watch = await asyncio.to_thread(get_all_watch)
    if not all_watch:
        await reply_md(update,"📭 Chưa có user nào lưu danh sách theo dõi.")
        return

    await reply_md(update, f"🔎 vui lòng đợi...")

    # (Phần xử lý dict/list bên dưới là an toàn, không I/O)
    symbol_counts = {}
    detail_lines = []
    for chat_key, block in all_watch.items():
        lst = block.get("list", []) or []
        for sym in lst:
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
        if lst:
            detail_lines.append(f"- {chat_key}: {', '.join(lst)}")
        else:
            detail_lines.append(f"- {chat_key}: (trống)")

    stats_lines = []
    for sym, cnt in sorted(symbol_counts.items()):
        stats_lines.append(f"{sym}: {cnt} user")

    # ⭐️ SỬA: Chạy CSDL trong thread
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

    header = (
        cmd_summary +
        "📋 *Tổng hợp danh sách mã đang được theo dõi*\n"
        f"👥 Tổng số user: {len(all_watch)}\n"
        f"🏷️ Tổng số mã khác nhau: {len(symbol_counts)}\n\n"
        "📌 *Thống kê theo mã:*\n"
        + "\n".join(stats_lines)
        + "\n\n📌 *Chi tiết theo từng user (chat-id):*"
    )

    # (Phần xử lý text an toàn, không I/O)
    max_len = 3500
    parts = []
    current = header
    for line in detail_lines:
        if len(current) + len(line) + 1 > max_len:
            parts.append(current)
            current = line
        else:
            current += "\n" + line
    parts.append(current)

    for part in parts:
        await reply_md(update, part)

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

async def cmd_backup_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin: backup dữ liệu core (watchlist + news_pref + bot_config + bctc_notified)
    thành file JSON và gửi cho admin.
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if ADMIN_ID is None or user_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # Log command (chạy trong thread để không block)
    await asyncio.to_thread(log_command_usage, chat_id, "/backup_core", ADMIN_ID)

    await reply_md(update, "⏳ Đang backup dữ liệu core, vui lòng đợi...")

    # Export dữ liệu core (DB I/O chạy trong thread)
    payload = await asyncio.to_thread(export_core_data)

    # Tạo file tạm trong container
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    ts = now.strftime("%Y%m%d_%H%M%S")
    month_key = now.strftime("%Y-%m")
    filename = f"stockbot_core_backup_{month_key}_{ts}.json"
    tmp_path = os.path.join(TMP_DIR, filename)

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Gửi file cho admin
    await context.bot.send_document(
        chat_id=chat_id,
        document=open(tmp_path, "rb"),
        filename=filename,
        caption=(
            f"📦 Backup dữ liệu core lúc {ts} (tháng {month_key}).\n"
            "- Bao gồm: bot_watch, news_pref, bot_config, bctc_notified.\n"
            "- Dùng file này cho lệnh /restore_core sau khi tạo DB mới trên Render."
        ),
    )

    await reply_md(update, "✅ Đã backup xong và gửi file cho bạn.")

# ============================================================
# ♻️ /restore_core – Khôi phục dữ liệu core + Clear Redis + Sync Redis từ DB
# ============================================================
async def cmd_restore_core(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Khôi phục dữ liệu core từ file JSON backup:
    - Lấy file từ message hiện tại hoặc message được reply.
    - Clear toàn bộ Redis (flushdb) để tránh lệch dữ liệu cache.
    - Import dữ liệu vào PostgreSQL (mode = 'replace').
    - Đồng bộ lại Redis từ PostgreSQL (watchlist).
    - Trả về thống kê before -> after (chịu lỗi nếu import_core_data không trả kết quả).
    """

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if ADMIN_ID is None or user_id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return
    

    # 1) Lấy file đính kèm
    document = None
    msg = update.message
    if msg and msg.document:
        document = msg.document
    elif msg and msg.reply_to_message and msg.reply_to_message.document:
        document = msg.reply_to_message.document

    if not document:
        await reply_md(
            update,
            "📥 *Cách dùng*\n"
            "*C1:* Gửi file backup `.json` rồi đặt caption là `/restore_core`\n"
            "*C2:* Reply `/restore_core` vào tin nhắn có đính kèm file backup `.json`"
        )
        return

    # 2) Tải file về thư mục tạm (cross-platform)
    try:
        tg_file = await document.get_file()
    except Exception as e:
        await reply_md(update, f"⚠️ *Không thể lấy file từ Telegram:* `{e}`")
        return

    tmp_dir = Path(tempfile.gettempdir()) / "stockbot_restore"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", document.file_name or "backup.json")
    tmp_path = tmp_dir / f"{int(time.time())}_{safe_name}"

    try:
        await tg_file.download_to_drive(tmp_path)
    except Exception as e:
        await reply_md(update, f"⚠️ *Không thể lưu file về máy:* `{e}`\nVui lòng thử lại.")
        return

    # 3) Clear Redis trước khi restore
    try:
        r = get_redis()
        key_count = r.dbsize()
        r.flushdb()
        await reply_md(
            update,
            f"🧹 *Đã xóa toàn bộ dữ liệu Redis trước khi restore.*\n"
            f"Số key đã xóa: *{key_count}*"
        )
    except Exception as e:
        await reply_md(update, f"⚠️ *Lỗi khi xóa Redis:* `{e}`\nTiếp tục restore vào PostgreSQL…")

    # 4) Đọc JSON payload
    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        await reply_md(update, f"⚠️ *File JSON không hợp lệ hoặc lỗi đọc file:* `{e}`")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # 5) Import vào PostgreSQL (mode = replace)
    try:
        # import_core_data có thể trả dict (before/after) hoặc None
        result = await asyncio.to_thread(import_core_data, payload, "replace")
    except Exception as e:
        await reply_md(update, f"⚠️ *Lỗi khi restore dữ liệu core:* `{e}`")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # 6) Lấy thống kê before/after (nếu result == None thì tự đếm)
    def _count_all():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM bot_watch"); bw = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM news_pref"); np = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM bot_config"); bc = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM bctc_notified"); bn = cur.fetchone()[0]
        return {"bot_watch": bw, "news_pref": np, "bot_config": bc, "bctc_notified": bn}

    if result and isinstance(result, dict):
        before = result.get("before", {})
        after = result.get("after", {}) or _count_all()
    else:
        # Không có thống kê from import_core_data -> hiển thị chỉ "after"
        before = {}
        after = _count_all()

    # 7) Đồng bộ lại Redis từ PostgreSQL (watchlist)
    sync_ok = True
    synced_users = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, watch_list FROM bot_watch")
                rows = cur.fetchall()

        r = get_redis()
        # rebuild set danh sách chat_id có watchlist
        r.delete("watch_chat_ids")
        for chat_id, watch_list in rows:
            r.set(f"watch:{chat_id}", json.dumps(watch_list))
            r.sadd("watch_chat_ids", chat_id)
            synced_users += 1
    except Exception as e:
        sync_ok = False
        await reply_md(update, f"⚠️ *Lỗi khi đồng bộ Redis từ DB (watchlist):* `{e}`")

    # 8) Trả summary
    summary_lines = []
    summary_lines.append("✅ *Khôi phục dữ liệu core thành công!*")
    summary_lines.append("")
    summary_lines.append("*Số lượng bản ghi (trước → sau):*")
    def fmt_row(name):
        b = before.get(name, "?")
        a = after.get(name, "?")
        return f"- {name}: {b} → {a}"
    summary_lines.append(fmt_row("botWatch"))
    summary_lines.append(fmt_row("newsPref"))
    summary_lines.append(fmt_row("botConfig"))
    summary_lines.append(fmt_row("bctcNotified"))
    summary_lines.append("")
    if sync_ok:
        summary_lines.append(f"🔄 *Đã đồng bộ Redis từ DB (watchlist).* Tổng số user: *{synced_users}*")
    else:
        summary_lines.append("⚠️ *Redis chưa được đồng bộ đầy đủ.* Bạn có thể chạy `/sync_watch_from_db` sau.")
    await reply_md(update, "\n".join(summary_lines))

    # 9) Dọn file tạm (best-effort)
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass

# ==============================================
# COMMAND: /report (CÓ CACHE REDIS + RETRY, KHÔNG COOLDOWN)
# Cache nội dung report theo danh mục vào Redis (theo cache_key = danh mục chuẩn hoá)
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Gửi báo cáo danh mục hiện tại cho user.

    - Không cooldown: user có thể gọi bất cứ lúc nào.
    - Ưu tiên đọc cache từ Redis (theo danh mục chuẩn hoá).
    - Nếu chưa có cache hoặc cache quá cũ -> gọi LLM tạo mới & lưu lại Redis.
    """

    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    vn_tz = pytz.timezone(TIMEZONE)

    if not update or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # === KIỂM TRA PAYWALL ===
    if not await check_pro_access(update, context):
        return # Dừng lại, vì check_pro_access đã gửi tin nhắn paywall rồi
    # -----------------------

    # Nhắc nhẹ trước để user biết bot đang xử lý
    await reply_md(update, "🔎 Vui lòng đợi, bot đang tổng hợp báo cáo danh mục...")

    # Ghi log sử dụng lệnh (chạy trong thread để tránh block)
    await asyncio.to_thread(log_command_usage, chat_id, "/report", ADMIN_ID)

    # Lấy danh mục từ DB (chạy trong thread)
    watch = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
    symbols = [s.upper() for s in (watch or []) if not s.upper().startswith("VN")]

    if not symbols:
        await reply_md(update, "📭 Danh mục của bạn trống. Hãy /add vài mã trước nhé!")
        return

    cache_key = make_report_cache_key(symbols)
    log.info(f"[{INSTANCE_ID}] /report cache_key={cache_key} for chat_id={chat_id}")

    # 1️⃣ Thử lấy báo cáo từ Redis trước
    cached = get_report_from_redis(cache_key)

    if cached is not None:
        text, generated_at = cached
        log.info(...)
        footer = f"\n\n🕓 _Báo cáo được tạo vào {generated_at.strftime('%d/%m/%Y %H:%M')} — dữ liệu có thể thay đổi theo thời gian._"
        final_text = text.strip() + footer
        await reply_md(update, final_text)
        return


    # 2️⃣ Nếu không có cache -> gọi LLM với cơ chế retry
    async def fetch_report_with_retry():
        retry = 0
        last_text = "⚠️ Hiện tại không tạo được báo cáo, vui lòng thử lại sau."
        while retry < 3:
            start = time.time()
            text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
            duration = time.time() - start
            log.info(
                f"[{INSTANCE_ID}] /report round {retry+1} done in {duration:.2f}s (chat_id={chat_id})"
            )
            last_text = text

            if "⚠️ Hiện tại không tạo được" not in text and "429" not in text:
                return text
            retry += 1
            await asyncio.sleep(10 * retry)
        return last_text

    text = await fetch_report_with_retry()

    # 3️⃣ Lưu vào Redis để lần sau /report hoặc weekly_report_loop dùng lại
    save_report_to_redis(cache_key, text, source="on_demand")

    now = datetime.datetime.now(vn_tz)
    footer = f"\n\n🕓 _Báo cáo được tạo vào {now.strftime('%d/%m/%Y %H:%M')} — dữ liệu có thể thay đổi theo thời gian._"
    final_text = text.strip() + footer

    try:
        await reply_md(update, text)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] Lỗi gửi báo cáo /report cho {chat_id}: {e}")
        await reply_md(
            update,
            "📋 Báo cáo đã được tạo xong nhưng gặp lỗi định dạng. Vui lòng thử lại sau nhé.",
        )

#==========================================
# Dán hàm này vào file alert_bot.py

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
            "⚠️ Tính năng này chỉ dành cho Gói Pro. Vui lòng liên hệ Admin `https://t.me/KhoiTran99` để nâng cấp. 😏"
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
        await reply_md(update, f"Lỗi: {e}. Cú pháp: /admin_add_user <chat_id> <số_ngày>")

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

async def cmd_run_screener_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Admin) Chạy tác vụ gửi daily screener ngay lập tức.
    """
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update, "⛔ Lệnh này chỉ dành cho admin.")
        return

    # Log command
    await asyncio.to_thread(log_command_usage, update.effective_chat.id, "/cmd_run_screener_now", ADMIN_ID)
    
    # Báo cho admin biết là lệnh đã được nhận
    await reply_md(update, "🏃 Bắt đầu chạy tác vụ `execute_daily_screener` trong nền. Sẽ thông báo khi hoàn tất...")

    # Gọi hàm lõi (truyền update vào để nhận phản hồi)
    # Chạy tác vụ này trong một task riêng để nó không block bot
    asyncio.create_task(execute_daily_screener(admin_update=update))

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
# ==============================================
# FLASK KEEPALIVE
# ==============================================
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return f"✅ Bot is alive. Instance {INSTANCE_ID}"
@flask_app.route("/health")
def health_check():
    # Phản hồi nhanh nhất có thể, chỉ để xác nhận máy chủ đang chạy
    return "", 200 # Trả về chuỗi rỗng và mã 200 OK
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
                    #-----------------------------------------
                    MAIN_LOOP.create_task(session_notice_loop()),
                    MAIN_LOOP.create_task(weekly_report_loop()),
                    MAIN_LOOP.create_task(screener_value_update_loop()),
                    MAIN_LOOP.create_task(daily_screener_loop()),
                    MAIN_LOOP.create_task(news_specialized_loop()),
                    MAIN_LOOP.create_task(news_macro_loop()),
                    MAIN_LOOP.create_task(news_cleanup_loop()),
                    MAIN_LOOP.create_task(financial_Statements_notice_loop()),
                    MAIN_LOOP.create_task(restore_reminder_loop()),
                    MAIN_LOOP.create_task(run_background_startup_tasks(ADMIN_ID, initial_active, INSTANCE_ID, tg_app)),
                    MAIN_LOOP.create_task(auto_on_after_delay(initial_active)),
                    #-------------- vn30f1m_alert_loop ------------
                    MAIN_LOOP.create_task(vn30f1m_alert_loop()),      # Ticker (5s)
                    MAIN_LOOP.create_task(vn30f1m_price_fetcher_loop()), # Fetcher (61s)
                    MAIN_LOOP.create_task(vn30f1m_broadcast_loop()),   # Broadcaster (chờ queue)
                    #----------------------------------------------
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
        # Tác vụ 1: Đăng ký lệnh bot (network call)
        commands = [
            ("start", "Giới thiệu bot và hướng dẫn sử dụng"),
            ("add", "Thêm mã cổ phiếu vào danh sách theo dõi"),
            ("remove", "Xóa mã cổ phiếu khỏi danh sách"),
            ("list", "Xem danh sách cổ phiếu bạn đang theo dõi"),
            ("report", "Phân tích danh mục bằng AI"),
            ("news_on", "Bật nhận tin tức (vĩ mô + chuyên ngành)"),
            ("news_off", "Tắt nhận tin tức"),
            ("news_status", "Xem trạng thái nhận tin tức"),
            ("vn30f1m_off", "Tắt nhận cập nhật VN30F1M"),
            ("vn30f1m_on", "Bật nhận cập nhật VN30F1M"),
            ("vn30f1m_status", "Xem trạng thái nhận cập nhật VN30F1M"),
            ("on", "(admin) Bật bot (thoát chế độ bảo trì)"),
            ("off", "(admin) Tắt bot (bảo trì tạm thời)"),
            ("status", "(admin) Kiểm tra trạng thái hoạt động của bot"),
            ("announce", "(admin) Gửi thông báo đến tất cả người dùng"),
            ("allwatch", "(admin) Thống kê toàn bộ danh sách theo dõi của user"),
            ("screener_value_clear", "(admin) Xóa dữ liệu screener cache"),
            ("value_refresh_now", "(admin) Làm mới liệu screener cache"),
            ("delete_range", "(admin) Xóa tin nhắn bot gửi trong khoảng thời gian"),
            ("news_test_macro", "(admin) Gửi thử tin tức vĩ mô mới nhất"),
            ("news_test_specialized", "(admin) Gửi thử tin tức vĩ mô mới nhất"),
            ("backup_core", "(admin) Backup dữ liệu core (watchlist, news_pref, BCTC)"),
            ("restore_core", "(admin) Khôi phục dữ liệu core từ file backup"),
            ("admin_add_user", "(admin) (admin) Thêm/gia hạn Gói Pro cho user"),
            ("admin_deactivate", "(admin) Ngưng hoạt động Gói Pro của user"),
            ("admin_remove_user", "(admin) Xoá vĩnh viễn Gói Pro của user"),
            ("cmd_run_screener_now", "(admin) Chạy và gửi Daily Screener ngay lập tức"),
            ("cmd_run_weekly_report_now", "(admin) Chạy và gửi Weekly Report ngay lập tức"),
        ]

        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in commands],
            scope=telegram.BotCommandScopeDefault()
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
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("report", cmd_report))
    tg_app.add_handler(CommandHandler("news_on", cmd_news_on))
    tg_app.add_handler(CommandHandler("news_off", cmd_news_off))
    tg_app.add_handler(CommandHandler("news_status", cmd_news_status))
    tg_app.add_handler(CommandHandler("vn30f1m_status", cmd_vn30f1m_status))
    tg_app.add_handler(CommandHandler("vn30f1m_on", cmd_vn30f1m_on))
    tg_app.add_handler(CommandHandler("vn30f1m_off", cmd_vn30f1m_off))

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
    tg_app.add_handler(CommandHandler("value_refresh_now", cmd_value_refresh_now))
    tg_app.add_handler(CommandHandler("backup_core", cmd_backup_core))
    tg_app.add_handler(CommandHandler("restore_core", cmd_restore_core))
    tg_app.add_handler(CommandHandler("admin_add_user", cmd_admin_add_user))
    tg_app.add_handler(CommandHandler("admin_deactivate", cmd_admin_deactivate))
    tg_app.add_handler(CommandHandler("admin_remove_user", cmd_admin_remove_user))
    tg_app.add_handler(CommandHandler("cmd_run_screener_now", cmd_run_screener_now))
    tg_app.add_handler(CommandHandler("cmd_run_weekly_report_now", cmd_run_weekly_report_now))

    # 🆕 Bắt case gửi file JSON + caption /restore_core
    tg_app.add_handler(
        MessageHandler(
            filters.Document.ALL & filters.CaptionRegex(r"^/restore_core(@\w+)?"),
            cmd_restore_core,
        )
    )

    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

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
