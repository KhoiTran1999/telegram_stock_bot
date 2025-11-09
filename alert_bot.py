# ==============================================
# alert_bot.py - phiên bản dùng OpenRouter + MiniMax M2
# ==============================================
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
from vnstock import Trading, Quote, Listing, Finance
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
    is_news_enabled_for_chat
)
import psutil
import time
import subprocess
import re
import csv
from datetime import timedelta
from telegram.error import BadRequest
from typing import Any
import html
from telegram import BotCommand
import telegram
import feedparser
from telegram.error import TelegramError

# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================
TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PASSENGER_PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

# Cấu hình batch cho screener Value
VALUE_BATCH_SIZE = 30       # 50 mã / batch
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
ALERT_COOLDOWN_SECONDS = 15 * 60  # 15 phút

# Ngưỡng cảnh báo
STOCK_LEVELS = [2, 4, 6.9, -2, -4, -6.9]
INDEX_POINT_LEVELS = [10, 20, 30, 40, -10, -20, -30, -40]

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
    "text": "🔔 Phiên giao dịch chiều sắp kết thúc lúc 14:45. Quý nhà đầu tư vui lòng rà soát lại các vị thế trong ngày. Báo cáo tổng kết tuần sẽ được gửi vào 09:00 sáng Chủ Nhật — hứa hẹn mang đến những thông tin hữu ích cho danh mục của bạn 📊",
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
        commit_message = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%B"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip() or None
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

def broadcast_to_all_watchers(text: str):
    """Gửi 1 thông báo tới tất cả user đã từng lưu danh sách theo dõi."""
    all_watch = get_all_watch()
    count = 0
    for chat_key in all_watch.keys():
        try:
            chat_id = int(chat_key)
            send_msg_to(chat_id, text)
            count += 1
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NOTICE] Lỗi gửi cho {chat_key}: {e}")
    log.info(f"[{INSTANCE_ID}][NOTICE] Đã gửi thông báo tới {count} user.")


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
            broadcast_to_all_watchers(spec["text"])
        except Exception as e:
            log.error(f"[{INSTANCE_ID}][SESSION {loop_id}] Lỗi khi broadcast: {e}")

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

        log.info(
            f"[{INSTANCE_ID}] [QUOTE OK] {symbol} -> "
            f"price={price_display}, pct={pct_display}, change_abs={change_abs_display}"
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
    """
    Lấy GIÁ + % NGÀY / TUẦN / THÁNG thật từ lịch sử giá,
    rồi build prompt gửi vào LLM (MiniMax M2 qua OpenRouter).
    """
    lines = []
    for sym in symbols:
        perf = get_perf_history(sym)
        if not perf:
            continue
        lines.append(format_perf_line(sym, perf))

    if not lines:
        return (
            "Danh mục hiện tại không có dữ liệu giá, hãy trả lời gọn: "
            "Chưa có dữ liệu để tổng hợp báo cáo hôm nay."
        )

    data_block = "\n".join(lines)

    prompt = f"""
Bạn là chuyên gia phân tích chứng khoán Việt Nam.

Hãy viết một bản tin Telegram gửi cho khách hàng sau phiên giao dịch hôm nay.

Dữ liệu danh mục của khách (giá + % thay đổi):
{data_block}

YÊU CẦU:
1. Với từng mã, hãy:
   - Nhắc lại giá hiện tại và % thay đổi trong ngày, tuần, tháng (dựa đúng vào dữ liệu ở trên). Định dạng giá cổ phiếu ví dụ sẽ là 10.000đ
   - Hãy cập nhật thông tin mới nhất về mã cổ phiếu, nếu không có gì mới, hãy đưa ra nhận định & phân tích về giao dịch trong ngày (cho thêm tí emoji cho sinh động):
     điểm mạnh, rủi ro, và gợi ý chiến lược nắm giữ / chốt lời 
     (nhưng KHÔNG dùng giọng ép buộc kiểu 'phải mua/bán').
2. Viết bằng tiếng Việt, giọng điệu dễ gần thậm chí là cà khịa nhưng dễ hiểu, phù hợp gửi qua Telegram.
3. Có thể dùng emoji cho sinh động, nhưng không lạm dụng.
4. Không đặt câu hỏi ở cuối bản tin, chỉ tóm tắt / kết luận nhẹ.
5. Quan trọng là không được đưa ra khuyến nghị mua bán cụ thể, chỉ phân tích và nhận định chung.

FORMAT:
- Mỗi mã theo block:

🔹 MÃ
- Giá hiện tại: ...
- Biến động: Ngày ..., Tuần ..., Tháng ...
- Thông tin mới: ...
- Nhận định: ...

- Cuối cùng thêm 1–2 câu tổng kết chung về danh mục (thiên về ngành nào, rủi ro / cơ hội tổng thể).

Hãy trả về đúng nội dung tin nhắn Telegram, KHÔNG giải thích thêm.
"""
    return prompt.strip()


def call_chatgpt_for_report(symbols: list[str]) -> str:
    """Gọi OpenRouter (MiniMax M2) để sinh bản tin báo cáo danh mục."""
    if not OPENROUTER_API_KEY:
        return (
            "⚠️ Hệ thống chưa cấu hình OPENROUTER_API_KEY nên chưa tạo được báo cáo tự động."
        )

    prompt = build_prompt_for_symbols(symbols)

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # 2 header này OpenRouter khuyến khích gửi thêm
        "HTTP-Referer": "https://github.com/Thinpad/telegram_stock_bot",
        "X-Title": "Telegram Stock Bot",
    }
    body = {
        "model": "minimax/minimax-m2:free",  # MiniMax M2 free trên OpenRouter
        "messages": [
            {
                "role": "system",
                "content": "Bạn là chuyên gia chứng khoán Việt Nam, trả lời ngắn gọn, rõ ràng, phù hợp gửi qua Telegram.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.4,
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
        if resp.status_code != 200:
            log.warning(
                f"[{INSTANCE_ID}][LLM ERROR] HTTP {resp.status_code}: {resp.text[:300]}"
            )
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM trả lỗi HTTP). Bạn thử lại sau nhé."

        data = resp.json()
        if "error" in data:
            log.warning(f"[{INSTANCE_ID}][LLM ERROR] {data['error']}")
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (LLM báo lỗi nội bộ)."

        choices = data.get("choices")
        if not choices:
            log.warning(
                f"[{INSTANCE_ID}][LLM ERROR] Response không có 'choices': {list(data.keys())}"
            )
            return "⚠️ Hiện tại không tạo được báo cáo danh mục (không nhận được nội dung phù hợp)."

        content = choices[0]["message"]["content"]
        return content.strip()

    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][LLM EXCEPTION] {e}")
        return "⚠️ Hiện tại không tạo được báo cáo danh mục do lỗi kết nối LLM."


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

def load_floor_map_from_csv(path: str = "ssi_master_list.csv") -> dict[str, str]:
    """
    Đọc file CSV mapping sàn niêm yết, trả về:
        {symbol: floor}
    CSV kỳ vọng có cột: symbol, floor
    Ví dụ:
        symbol,floor
        AAA,hose
        AAM,hnx
    """
    mapping: dict[str, str] = {}
    try:
        # dùng utf-8-sig để auto bỏ BOM nếu có
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return {}

            raw_cols = reader.fieldnames
            cols = [
                (c or "").strip().lstrip("\ufeff").lower()
                for c in raw_cols
            ]

            try:
                symbol_idx = cols.index("symbol")
            except ValueError:
                log.warning(
                    f"[{INSTANCE_ID}][VALUE] CSV {path} không có cột 'symbol'. "
                    f"fieldnames raw = {raw_cols}"
                )
                return {}

            try:
                floor_idx = cols.index("floor")
            except ValueError:
                log.warning(
                    f"[{INSTANCE_ID}][VALUE] CSV {path} không có cột 'floor'. "
                    f"fieldnames raw = {raw_cols}"
                )
                return {}

            for row in reader:
                values = list(row.values())
                sym = (values[symbol_idx] or "").strip().upper()
                flr = (values[floor_idx] or "").strip().upper()
                if not sym or not flr:
                    continue
                mapping[sym] = flr

        log.info(
            f"[{INSTANCE_ID}][VALUE] Đã load {len(mapping)} mã từ {path} (floor HOSE/HNX/...)."
        )

    except FileNotFoundError:
        log.warning(f"[{INSTANCE_ID}][VALUE] Không tìm thấy file {path}.")
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi đọc CSV {path}: {e}")

    return mapping

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
    """
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    
    # === LOGIC TỪ _collector ĐÃ GỘP VÀO ĐÂY ===
    # Tự động lưu chat_id vào DB nếu chưa có
    try:
        lst = get_watch_list_for_chat(chat_id)
        if lst is None:
            save_watch_list_for_chat(chat_id, [])
    except Exception as e:
        log.warning(f"Lỗi khi auto-save chat_id {chat_id} trong unknown_message: {e}")
    # === KẾT THÚC LOGIC GỘP ===
    
    user_text = update.message.text
    
    try:
        # Log lại hành vi này (tận dụng hàm bạn đã có)
        log_command_usage(chat_id, f"unknown: {user_text[:50]}") # Cắt ngắn text để an toàn
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
async def precompute_value_data():
    """
    Crawl P/E, P/B, ROE (TCBS) VÀ Thanh khoản, Tài sản (VCI Price Board)
    lưu vào stock_value_cache.
    (Phiên bản hoàn chỉnh, đã fix _safe_float)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    started_at = datetime.datetime.now(vn_tz)
    log.info(
        f"[{INSTANCE_ID}][VALUE] Bắt đầu precompute_value_data (có TK/TS) lúc "
        f"{started_at.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    # 1️⃣ Lấy danh sách tất cả mã từ Listing
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

    log.info(
        f"[{INSTANCE_ID}][VALUE] listing_df columns: {listing_df.columns.tolist()}"
    )

    filtered_df = listing_df.copy()

    # Cột mã
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

    # 🔹 Load industry_map từ CSV (Topi)
    industry_map = load_industry_map_from_csv("ssi_master_list.csv")
    if industry_map:
        symbols = [s for s in symbols if s in industry_map]
        log.info(
            f"[{INSTANCE_ID}][VALUE] Đã load {len(industry_map)} mã có ngành từ ssi_master_list.csv. "
            f"Sau khi lọc, còn {len(symbols)} mã sẽ được crawl."
        )
    else:
        log.info(
            f"[{INSTANCE_ID}][VALUE] Không load được ssi_master_list.csv, "
            "tất cả mã sẽ gán industry='Khác'."
        )

    # ❗️ SỬA LỖI LẤY SÀN (FIX 1)
    exchange_col_candidates = ["exchange", "floor", "san"]
    exchange_col = next(
        (c for c in exchange_col_candidates if c in filtered_df.columns),
        None,
    )
    
    exchange_map = {}
    if exchange_col:
        exchange_map = pd.Series(
            filtered_df[exchange_col].values, 
            index=filtered_df[symbol_col].str.upper()
        ).to_dict()
        log.info(f"[{INSTANCE_ID}][VALUE] Đã map được {len(exchange_map)} mã với sàn (exchange).")
    else:
        log.warning(f"[{INSTANCE_ID}][VALUE] Không tìm thấy cột sàn (exchange/floor/san).")

    # ❗️ TẢI FILE SÀN 1 LẦN (FIX 2)
    floor_map = load_floor_map_from_csv("ssi_master_list.csv")
    if not floor_map:
        log.warning(f"[{INSTANCE_ID}][VALUE] Không load được ssi_master_list.csv, "
                    "dữ liệu 'floor' sẽ bị NULL.")
    
    # ❗️ Copy 2 hàm helper này vào đây để dùng
    def _norm(x):
        if x is None: return None
        try:
            if hasattr(x, "item"): x = x.item()
        except Exception: pass
        try: x = float(x)
        except Exception: return None
        if isinstance(x, float) and math.isnan(x): return None
        return x

    def _pick_from_candidates(row, candidates, substrings):
        for key in candidates:
            try:
                if key in row:
                    v = _norm(row.get(key, None))
                    if v is not None: return v
            except Exception: continue
        for k in row.index:
            name = ""
            if isinstance(k, tuple):
                name = str(k[1]).lower()
            else:
                name = str(k).lower()
            if any(sub in name for sub in substrings):
                try: v = _norm(row.get(k, None))
                except Exception: v = None
                if v is not None: return v
        return None

    # Khởi tạo trading 1 lần
    try:
        trading = Trading(source="VCI")
    except Exception as e:
        trading = None
        log.warning(f"[{INSTANCE_ID}][VALUE] Không khởi tạo được Trading(source='VCI'): {e}")
        return # Bắt buộc phải có trading để tính TK/TS

    # 2️⃣ Đọc các mã đã có trong DB để auto-resume
    existing_records = load_stock_value_cache()
    processed_symbols = {r["symbol"] for r in existing_records if r.get("symbol")}
    todo_symbols = [s for s in symbols if s not in processed_symbols]

    total_symbols = len(symbols)
    total_todo = len(todo_symbols)
    already = len(processed_symbols)

    log.info(
        f"[{INSTANCE_ID}][VALUE] Đã có trong DB: {already} | "
        f"Cần crawl thêm: {total_todo} | Tổng: {total_symbols}"
    )

    if not todo_symbols:
        log.info(
            f"[{INSTANCE_ID}][VALUE] Không còn mã cần crawl, kết thúc precompute."
        )
        return

    total_batches = math.ceil(total_todo / VALUE_BATCH_SIZE)
    processed_count = already
    rate_limit_sleep = 60
    per_symbol_sleep = 0.7

    # ❗️ HÀM _safe_float ĐÃ SỬA LỖI TypeError
    def _safe_float(val):
        if val is None:
            return None
        try:
            if hasattr(val, "item"):
                val = val.item()
        except Exception:
            pass
        
        # FIX: Ép kiểu sang float TRƯỚC khi gọi isnan
        try:
            val = float(val)
        except Exception:
            return None # Nếu không thể convert sang float, trả về None
        
        if math.isnan(val):
            return None
            
        return val


    for batch_idx in range(total_batches):
        batch_syms = todo_symbols[
            batch_idx * VALUE_BATCH_SIZE : (batch_idx + 1) * VALUE_BATCH_SIZE
        ]
        log.info(
            f"[{INSTANCE_ID}][VALUE] Batch {batch_idx+1}/{total_batches} – "
            f"{len(batch_syms)} mã."
        )

        # ❗️ TỐI ƯU: LẤY TK/TS THEO BATCH
        liquidity_map: dict[str, float] = {}
        asset_map: dict[str, float] = {}
        try:
            pb_df = trading.price_board(batch_syms)
            if pb_df is not None and not pb_df.empty:
                for idx, row in pb_df.iterrows():
                    sym = None
                    for ksym in [("listing", "symbol"), ("stock", "symbol"), ("listing", "ticker")]:
                        try: val = row.get(ksym, None)
                        except Exception: val = None
                        if val: sym = str(val).upper(); break
                    if not sym: sym = str(idx).upper()
                    
                    price = _pick_from_candidates(row, [("match", "match_price"), ("match", "reference_price"), ("listing", "ref_price")], ["price"])
                    volume = _pick_from_candidates(row, [("match", "accumulated_vol"), ("match", "match_volume")], ["vol"])
                    shares = _pick_from_candidates(row, [("listing", "listed_share"), ("listing", "outstanding_share"), ("listing", "listed_shares"), ("listing", "outstanding_shares")],["share"])

                    if price is not None and volume is not None:
                        liquidity_map[sym] = float(price) * float(volume)
                    if price is not None and shares is not None:
                        asset_map[sym] = float(price) * float(shares)
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][VALUE] Lỗi price_board batch: {e}")
        
        batch_records = []

        for sym in batch_syms:
            sym = str(sym).upper()
            await asyncio.sleep(per_symbol_sleep) # Vẫn sleep để throttle fin.ratio

            ratio_df = None
            last_err = None

            # 🔁 Thử gọi TCBS, nếu bị rate limit thì nghỉ rồi thử lại (tối đa 3 lần)
            for attempt in range(3):
                try:
                    fin = Finance(symbol=sym, source="TCBS")
                    ratio_df = fin.ratio(period="year", lang="vi", dropna=False)
                    break  # OK
                except SystemExit as e:
                    msg = str(e)
                    last_err = e
                    log.warning(
                        f"[{INSTANCE_ID}][VALUE] SystemExit từ Finance.ratio(TCBS) cho {sym}: {msg}"
                    )
                    if "Rate limit exceeded" in msg:
                        log.warning(
                            f"[{INSTANCE_ID}][VALUE] Bị rate limit từ TCBS, nghỉ {rate_limit_sleep}s rồi thử lại (lần {attempt+1}/3)..."
                        )
                        await asyncio.sleep(rate_limit_sleep)
                        continue
                    else:
                        break
                except Exception as e:
                    last_err = e
                    log.warning(
                        f"[{INSTANCE_ID}][VALUE] Lỗi Finance.ratio(TCBS) cho {sym}: {e}"
                    )
                    break

            if ratio_df is None or ratio_df.empty:
                if last_err is not None:
                    log.debug(
                        f"[{INSTANCE_ID}][VALUE] {sym}: không lấy được ratio_df TCBS, lỗi cuối: {last_err}"
                    )
                else:
                    log.debug(
                        f"[{INSTANCE_ID}][VALUE] {sym}: ratio_df TCBS rỗng, bỏ qua."
                    )
                continue

            df = ratio_df.copy()

            # Sắp xếp để lấy kỳ mới nhất
            if "year" in df.columns:
                df = df.sort_values("year", ascending=False)
            elif "report_period" in df.columns:
                df = df.sort_values("report_period", ascending=False)

            latest = df.iloc[0]

            # 🧮 Lấy các chỉ số chính từ TCBS
            pe = _safe_float(latest.get("price_to_earning"))
            pb = _safe_float(latest.get("price_to_book"))
            roe = _safe_float(latest.get("roe"))
            eps = _safe_float(latest.get("earning_per_share"))

            # Chỉ chấp nhận > 0 cho pe/pb/roe
            if pe is not None and pe <= 0: pe = None
            if pb is not None and pb <= 0: pb = None
            if roe is not None and roe <= 0: roe = None

            # Ước tính giá để lọc penny: price_est ≈ P/E * EPS
            price_est = None
            if pe is not None and eps is not None and eps > 0:
                price_est = pe * eps

            if price_est is not None and price_est < MIN_PENNY_PRICE:
                log.debug(
                    f"[{INSTANCE_ID}][VALUE] {sym}: price_est≈{price_est:.0f} < {MIN_PENNY_PRICE}, coi là penny, bỏ qua."
                )
                continue

            if pe is None or pb is None or roe is None:
                log.debug(
                    f"[{INSTANCE_ID}][VALUE] {sym}: thiếu P/E/P/B/ROE hợp lệ, bỏ qua."
                )
                continue

            industry = industry_map.get(sym, "Khác")
            exchange = exchange_map.get(sym, None)
            floor = floor_map.get(sym, None)
            
            # ❗️ Lấy TK/TS từ map đã tính toán
            asset_proxy = asset_map.get(sym)
            liquidity_proxy = liquidity_map.get(sym)

            record = {
                "symbol": sym,
                "exchange": exchange,
                "industry": industry,
                "pe": pe,
                "pb": pb,
                "roe": roe,
                "floor": floor,
                "asset_proxy": asset_proxy,         # ⬅️ THÊM VÀO RECORD
                "liquidity_proxy": liquidity_proxy, # ⬅️ THÊM VÀO RECORD
            }
            batch_records.append(record)

        # Ghi batch vào DB (upsert theo symbol)
        try:
            # ❗️ Dùng hàm upsert mới, nó sẽ tự động chạy executemany
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
            f"({percent:.1f}%). Batch {batch_idx+1}/{total_batches} xong, "
            f"upsert {len(batch_records)} mã vào DB."
        )

        # Nghỉ nhẹ giữa các batch
        if batch_idx < total_batches - 1:
            await asyncio.sleep(VALUE_BATCH_SLEEP)

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
    (Đã fix lỗi UnboundLocalError)
    """
    rows = load_stock_value_cache()
    if not rows:
        return None

    df = pd.DataFrame(rows)
    
    # ❗️ Cần 4 cột mới: floor, asset_proxy, liquidity_proxy
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

    # Lọc theo industry map (TOPI) nếu có
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

    # Loại bỏ giá trị xấu
    df = df.dropna(
        subset=[
            "pe", "pb", "roe", "industry", "floor", 
            "asset_proxy", "liquidity_proxy" # ⬅️ Thêm vào dropna
        ]
    )
    df = df[(df["pe"] > 0) & (df["pb"] > 0) & (df["roe"] > 0)]
    if df.empty:
        return None

    # ❗️ LỌC SÀN TỪ DB (SIÊU NHANH)
    before_count = len(df)
    allowed_floors = {"HOSE", "HNX"}
    df = df[df["floor"].isin(allowed_floors)]
    log.info(
        f"[{INSTANCE_ID}][VALUE] Lọc sàn HOSE/HNX (từ DB): {before_count} -> {len(df)} mã."
    )
    
    # ❗️ LỌC THANH KHOẢN TỪ DB (SIÊU NHANH)
    before_count = len(df)
    df = df[df["liquidity_proxy"] >= 50_000_000_000]  # 50 tỷ
    log.info(
        f"[{INSTANCE_ID}][VALUE] Lọc thanh khoản >=50 tỷ (từ DB): {before_count} -> {len(df)} mã."
    )
   
    # ❗️ LỌC TÀI SẢN TỪ DB (SIÊU NHANH)
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
    # ❗️ ĐÃ XOÁ KHỐI ELSE GÂY LỖI TẠI ĐÂY

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
# BÁO CÁO TUẦN 09:00 CHỦ NHẬT (CÓ CACHE + RETRY)
# ==============================================
async def daily_report_loop():
    """
    Gửi báo cáo danh mục cho từng user vào 09:00 sáng Chủ Nhật hằng tuần
    (dùng chung cache với /report, có retry nếu lỗi LLM).
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1

        # ❗️ KIỂM TRA TRẠNG THÁI BOT (kiểm tra trước khi ngủ dài)
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][WEEKLY {loop_id}] [gửi báo cáo tự động 09:00 Chủ Nhật hằng tuần] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue # Quay lại vòng lặp, kiểm tra BOT_ACTIVE tiếp

        if not OPENROUTER_API_KEY:
            log.warning(
                f"[{INSTANCE_ID}][WEEKLY {loop_id}] Chưa có OPENROUTER_API_KEY, "
                "bỏ qua gửi báo cáo tuần, sleep 3600s."
            )
            await asyncio.sleep(3600)
            continue

        wait_sec = seconds_until_next_weekly_report()
        log.info(
            f"[{INSTANCE_ID}][WEEKLY {loop_id}] Ngủ tới 09:00 Chủ Nhật, còn {wait_sec:.0f}s"
        )
        await asyncio.sleep(wait_sec)

        # ❗️ KIỂM TRA LẦN NỮA SAU KHI THỨC DẬY
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][WEEKLY {loop_id}] Thức dậy nhưng bot đang TẮT, bỏ qua.")
            continue

        now = datetime.datetime.now(vn_tz)
        if now.weekday() != 6:  # không phải Chủ Nhật thì bỏ qua (phòng trường hợp lệch giờ)
            log.info(f"[{INSTANCE_ID}][WEEKLY {loop_id}] Không phải Chủ Nhật, bỏ qua.")
            continue

        try:
            log.info(f"[{INSTANCE_ID}][WEEKLY {loop_id}] Bắt đầu gửi báo cáo tuần")
            all_watch = get_all_watch()

            if not all_watch:
                log.info(
                    f"[{INSTANCE_ID}][WEEKLY {loop_id}] Không có user nào theo dõi, bỏ qua."
                )
                continue

            sent_count = 0
            skipped_count = 0

            for chat_key, user_block in all_watch.items():
                # ❗️ KIỂM TRA BOT TRƯỚC KHI GỬI CHO TỪNG USER
                if not BOT_ACTIVE:
                    log.info(f"[{INSTANCE_ID}][WEEKLY] Bot TẮT giữa chừng, dừng gửi.")
                    break

                chat_id = int(chat_key)
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

                cache_key = "-".join(sorted(symbols))
                now = datetime.datetime.now(pytz.timezone(TIMEZONE))

                # 🧠 Dùng cache nếu còn hạn (12h) để tiết kiệm token
                if cache_key in REPORT_CACHE:
                    cached_text, cached_time = REPORT_CACHE[cache_key]
                    if (now - cached_time).total_seconds() < 12 * 3600:
                        send_msg_to(chat_id, cached_text)
                        log.info(
                            f"[{INSTANCE_ID}][WEEKLY] Cache hit cho {chat_id} ({cache_key})"
                        )
                        await asyncio.sleep(1.5)
                        sent_count += 1
                        continue

                # 🧩 Gọi AI (có retry)
                async def fetch_report_with_retry():
                    retry = 0
                    while retry < 3:
                        start = time.time()
                        text = await asyncio.to_thread(
                            call_chatgpt_for_report, symbols
                        )
                        duration = time.time() - start
                        log.info(
                            f"[{INSTANCE_ID}][WEEKLY] Round {retry+1} cho {chat_id} ({duration:.1f}s)"
                        )

                        if "⚠️" not in text and "429" not in text:
                            return text
                        retry += 1
                        await asyncio.sleep(10 * retry)
                    return text

                try:
                    text = await fetch_report_with_retry()
                    REPORT_CACHE[cache_key] = (text, now)
                    send_msg_to(chat_id, text)
                    log.info(
                        f"[{INSTANCE_ID}][WEEKLY] Đã gửi báo cáo tuần cho {chat_id}"
                    )
                    sent_count += 1
                except Exception as e:
                    log.warning(
                        f"[{INSTANCE_ID}][WEEKLY] Lỗi gửi cho {chat_id}: {e}"
                    )

                # Giãn nhịp gửi để tránh spam Telegram (3s/user)
                await asyncio.sleep(3)

            log.info(
                f"[{INSTANCE_ID}][WEEKLY {loop_id}] Hoàn tất — gửi {sent_count}, bỏ qua {skipped_count} user."
            )

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][WEEKLY {loop_id}] Lỗi tổng quát: {e}")
            await asyncio.sleep(300)  # 5 phút retry nếu lỗi tổng

async def screener_value_update_loop():
    """
    Chạy precompute_value_data() vào 00:00 (giờ VN) các ngày Thứ 2–Thứ 6.
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
            await precompute_value_data()
        except Exception:
            log.exception(f"[{INSTANCE_ID}][VALUE {loop_id}] Lỗi khi chạy precompute_value_data() theo lịch.")
            # tránh spam lỗi, nghỉ 1h rồi tính lịch mới
            await asyncio.sleep(3600)

async def initial_value_precompute_loop():
    """
    Chạy 1 lần sau khi bot khởi động:
    - Đợi vài giây cho service & webhook mở port xong
    - Kiểm tra DB, nếu chưa có dữ liệu screener Value thì crawl lần đầu
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 1

    # Đợi 20s cho Hypercorn/Flask & Telegram webhook ổn định
    await asyncio.sleep(20)

    try:
        current_count = get_stock_value_cache_count()
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][VALUE {loop_id}] Lỗi khi kiểm tra stock_value_cache: {e}")
        current_count = 0

    if current_count == 0:
        now = datetime.datetime.now(vn_tz)
        log.info(
            f"[{INSTANCE_ID}][VALUE {loop_id}] DB chưa có dữ liệu screener Value, "
            f"bắt đầu precompute_value_data() lần đầu (background) lúc {now.strftime('%Y-%m-%d %H:%M:%S')}."
        )
        try:
            await precompute_value_data()
        except Exception:
            log.exception(
                f"[{INSTANCE_ID}][VALUE {loop_id}] Lỗi khi chạy precompute_value_data() lần đầu (background)."
            )
    else:
        log.info(
            f"[{INSTANCE_ID}][VALUE {loop_id}] stock_value_cache đã có {current_count} dòng, "
            "không cần precompute lần đầu."
        )

    # Kết thúc loop một lần, không lặp lại
    log.info(f"[{INSTANCE_ID}][VALUE {loop_id}] initial_value_precompute_loop() kết thúc.")

async def daily_screener_loop():
    """
    Gửi báo cáo screener value cho TẤT CẢ user vào 09:00 sáng T2-T6.
    """
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0

    while True:
        loop_id += 1

        # ❗️ KIỂM TRA TRẠNG THÁI BOT
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] [gửi báo cáo screener 09:00 T2-T6] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue

        wait_sec = seconds_until_next_weekday_screener()
        log.info(
            f"[{INSTANCE_ID}][SCREENER {loop_id}] Ngủ tới 09:00 ngày làm việc tiếp theo, còn {wait_sec:.0f}s"
        )
        await asyncio.sleep(wait_sec)

        # ❗️ KIỂM TRA LẦN NỮA SAU KHI THỨC DẬY
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] Thức dậy nhưng bot đang TẮT, bỏ qua.")
            continue

        now = datetime.datetime.now(vn_tz)
        if now.weekday() > 4: # 5=T7, 6=CN
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] Thức dậy nhưng không phải T2-T6, bỏ qua.")
            continue
        
        try:
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] Bắt đầu chạy compute_value_screener() lúc 09:00.")
            
            # 1. Tính toán (chạy trong thread để không block bot)
            result = await asyncio.to_thread(compute_value_screener)
            
            # 2. Format
            text = format_screener_report_text(result)
            
            if not text:
                log.warning(f"[{INSTANCE_ID}][SCREENER {loop_id}] Không có dữ liệu screener để gửi.")
                continue

            # 3. Gửi cho tất cả user
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] Bắt đầu broadcast báo cáo screener...")
            broadcast_to_all_watchers(text)
            log.info(f"[{INSTANCE_ID}][SCREENER {loop_id}] Hoàn tất broadcast.")

        except Exception as e:
            log.exception(f"[{INSTANCE_ID}][SCREENER {loop_id}] Lỗi tổng quát: {e}")
            await asyncio.sleep(300) # 5 phút retry nếu lỗi

async def news_specialized_loop():
    """
    Quét RSS chuyên ngành, tìm bài có chứa mã cổ phiếu HOẶC tên doanh nghiệp
    trong danh mục của user. Gửi tin nhắn riêng cho từng user có bài liên quan.
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
            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, all_specialized_urls
            )

            # 🌱 Warm-up lần đầu: DB chưa có gì thì chỉ lưu dấu, không gửi
            if not warmed_up:
                count_in_db = get_news_seen_count(NEWS_FEED_TYPE_SPECIALIZED)
                if count_in_db == 0 and entries:
                    for it in entries:
                        mark_news_seen(
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

            # Lọc bài mới chưa xử lý
            new_entries = [
                it
                for it in entries
                if not has_news_seen(NEWS_FEED_TYPE_SPECIALIZED, it["link"])
            ]

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

            # Map symbol -> list chat_id (chỉ những user bật tin chuyên ngành)
            all_watch = get_all_watch()
            symbol_to_chats: dict[str, list[int]] = {}
            for chat_key, block in all_watch.items():
                try:
                    cid = int(chat_key)
                except Exception:
                    continue

                # User đã tắt nhận tin chuyên ngành -> bỏ qua
                if not is_news_enabled_for_chat(cid, NEWS_FEED_TYPE_SPECIALIZED):
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
                    mark_news_seen(
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=it["link"],
                        guid=None,
                        title=it["title"],
                        published=it["published"],
                    )
                await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)
                continue

            # 🔎 Compile pattern cho từng mã với keyword từ CSV (symbol + name)
            patterns: dict[str, re.Pattern] = {}
            for sym in symbol_to_chats.keys():
                keywords = COMPANY_KEYWORDS.get(sym, [sym])
                combined = "|".join(re.escape(k) for k in keywords if k)
                if not combined:
                    continue
                patterns[sym] = re.compile(rf"\b({combined})\b", re.IGNORECASE)

            # Xử lý từng bài mới
            for it in new_entries:
                title = it["title"] or ""
                raw_summary = it.get("summary") or ""
                link = it["link"] or ""
                source = it.get("source") or ""
                pub_dt = it.get("published")

                # Dùng hàm clean_html_text đã dùng cho macro
                decoded_summary = clean_html_text(raw_summary)

                text_for_match = (title + " " + decoded_summary)

                # Nếu rỗng thì coi như bài lỗi, chỉ đánh dấu đã xem
                if not text_for_match.strip():
                    mark_news_seen(
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=link,
                        guid=None,
                        title=title,
                        published=pub_dt,
                    )
                    continue

                # Tìm xem bài này liên quan tới mã nào, và gửi cho user nào
                affected: dict[int, list[str]] = {}

                for sym, pat in patterns.items():
                    if pat.search(text_for_match):
                        for cid in symbol_to_chats.get(sym, []):
                            affected.setdefault(cid, []).append(sym)

                if not affected:
                    # Không trùng mã nào trong danh mục user -> vẫn đánh dấu đã xem
                    mark_news_seen(
                        NEWS_FEED_TYPE_SPECIALIZED,
                        link=link,
                        guid=None,
                        title=title,
                        published=pub_dt,
                    )
                    continue

                if isinstance(pub_dt, datetime.datetime):
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    pub_str = ""

                # (Tuỳ chọn) rút gọn summary nếu muốn gửi kèm
                short_sum = decoded_summary
                if len(short_sum) > 300:
                    short_sum = short_sum[:280].rstrip() + "..."

                for chat_id, syms in affected.items():
                    uniq_syms = sorted(set(syms))

                    lines = [
                        "📰 *Tin tức mới liên quan tới danh mục của bạn:*",
                        title,
                        "",
                        "*Liên quan tới:* " + ", ".join(uniq_syms),
                    ]

                    if short_sum:
                        lines.extend(["", short_sum])

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
                    # Gửi plain text để tránh lỗi Markdown
                    send_msg_to(chat_id, text)
                    await asyncio.sleep(0.2)

                # Đánh dấu bài đã xử lý
                mark_news_seen(
                    NEWS_FEED_TYPE_SPECIALIZED,
                    link=link,
                    guid=None,
                    title=title,
                    published=pub_dt,
                )

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_SPEC {loop_id}] Lỗi tổng quát: {e}")

        await asyncio.sleep(NEWS_SPECIALIZED_INTERVAL_SECONDS)


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

async def news_macro_loop():
    """
    Quét RSS vĩ mô, nếu có bài mới thì broadcast cho tất cả user
    (nhưng CHỈ những user chưa tắt tin vĩ mô).
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
            entries = await asyncio.to_thread(
                fetch_rss_entries_for_urls, RSS_FEEDS_MACRO
            )

            # Warm-up lần đầu
            if not warmed_up:
                count_in_db = get_news_seen_count(NEWS_FEED_TYPE_MACRO)
                if count_in_db == 0 and entries:
                    for it in entries:
                        mark_news_seen(
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

            new_entries = [
                it
                for it in entries
                if not has_news_seen(NEWS_FEED_TYPE_MACRO, it["link"])
            ]

            if not new_entries:
                log.info(
                    f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Không có bài vĩ mô mới."
                )
                await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS)
                continue

            all_watch = get_all_watch()

            for it in new_entries:
                title = it["title"] or ""
                link = it["link"] or ""
                source = it.get("source") or ""
                pub_dt = it.get("published")

                # ==== Xử lý ngày giờ ====
                if isinstance(pub_dt, datetime.datetime):
                    pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    pub_str = ""

                # ==== Làm sạch phần summary ====
                raw_summary = it.get("summary") or ""
                clean_summary = clean_html_text(raw_summary)

                # Rút gọn nếu quá dài
                short_sum = clean_summary
                if len(short_sum) > 400:
                    short_sum = short_sum[:380].rstrip() + "..."

                # ==== Ghép nội dung gửi ====
                lines = [
                    f"🌏 *Tin vĩ mô mới:*",
                    title,
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

                # ==== Gửi tới user ====
                sent = 0
                for chat_key in all_watch.keys():
                    try:
                        chat_id = int(chat_key)
                    except Exception:
                        continue

                    if not is_news_enabled_for_chat(chat_id, NEWS_FEED_TYPE_MACRO):
                        continue

                    try:
                        send_msg_to(chat_id, text)
                        sent += 1
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        log.warning(
                            f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Lỗi gửi cho {chat_id}: {e}"
                        )

                log.info(
                    f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Đã gửi tin vĩ mô tới {sent} user."
                )

                # Đánh dấu bài đã xử lý
                mark_news_seen(
                    NEWS_FEED_TYPE_MACRO,
                    link=link,
                    guid=None,
                    title=title,
                    published=pub_dt,
                )
                await asyncio.sleep(1.0)

        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][NEWS_MACRO {loop_id}] Lỗi tổng quát: {e}")

        await asyncio.sleep(NEWS_MACRO_INTERVAL_SECONDS)


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

def send_msg_to(chat_id: int, text: str, parse_mode: str | None = "Markdown"):
    """Gửi tin nhắn Telegram, mặc định dùng Markdown (v1) với fallback an toàn."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    def _do_send(t: str, mode: str | None):
        params = {
            "chat_id": chat_id,
            "text": t,
        }
        if mode:
            params["parse_mode"] = mode
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        return data

    try:
        # Lần 1: gửi nguyên văn
        data = _do_send(text, parse_mode)

        # Nếu lỗi do Markdown -> escape và gửi lại
        if (not data.get("ok")
            and parse_mode == "Markdown"
            and "description" in data
            and "can't parse entities" in data["description"].lower()):
            log.warning(f"[WARN] Markdown parse error, retry with escaped text: {data}")
            safe_text = escape_markdown_v2(text)
            data = _do_send(safe_text, parse_mode)

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
                send_msg_to(
                    ADMIN_ID,
                    "✅ *Hệ thống đã được kích hoạt trở lại (auto /on sau 2 phút).* \n\n"
                    "Bot hiện đang ở trạng thái *hoạt động bình thường* và sẵn sàng phục vụ người dùng. 🚀"
                )
            except Exception as e:
                log.warning(f"[{INSTANCE_ID}] Lỗi khi gửi thông báo auto /on cho admin: {e}")


# ==============================================
# COMMAND HANDLERS
# ==============================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return
    
    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/start", ADMIN_ID)   # 🆕 ghi log

    # ✅ Chỉ tạo mới nếu user chưa có record
    lst = get_watch_list_for_chat(chat_id)
    if lst is None:
        save_watch_list_for_chat(chat_id, [])

    await reply_md(update,
    "╔════════════════════════════════╗\n"
    "🎯 *Chào mừng Quý Nhà Đầu Tư đến với StockBot!* 🤖💹\n"
    "╚════════════════════════════════╝\n\n"
    "StockBot là trợ lý cảnh báo chứng khoán realtime, giúp bạn theo dõi biến động giá một cách nhanh chóng và chính xác.\n\n"
    "📈 *Cách hoạt động:*\n"
    "• Khi giá cổ phiếu trong danh sách của bạn *tăng hoặc giảm 2%, 4%, 6%* so với giá tham chiếu, hệ thống sẽ tự động gửi cảnh báo ngay lập tức.\n"
    "• Mỗi cảnh báo đều hiển thị phần trăm thay đổi, xu hướng và thông tin liên quan để bạn dễ dàng nắm bắt tình hình thị trường.\n\n"
    "📊 *Các lệnh bạn có thể sử dụng:*\n"
    "• `/add <MÃ>` – Thêm mã cổ phiếu vào danh sách theo dõi\n"
    "• `/remove <MÃ>` – Xóa mã cổ phiếu khỏi danh sách\n"
    "• `/list` – Xem danh sách cổ phiếu đang theo dõi\n"
    "• `/report` – Nhận báo cáo phân tích AI về danh mục của bạn 🧠\n"
    "• `/news_on` – Bật nhận tin tức (vĩ mô + chuyên ngành)\n"
    "• `/news_off` – Tắt nhận tin tức\n"
    "• `/news_status` – Xem trạng thái nhận tin tức\n\n"
    "🕓 *Báo cáo tự động:* Mỗi Chủ Nhật lúc 09:00 sáng, StockBot sẽ tổng hợp dữ liệu trong tuần và gửi đến bạn một bản *báo cáo AI chi tiết*, giúp bạn đánh giá hiệu quả đầu tư và xu hướng sắp tới.\n\n"
    "💬 Với StockBot, mọi biến động đều được cập nhật tức thì – để bạn không bỏ lỡ bất kỳ cơ hội nào.\n\n"
    "🚀 Bắt đầu theo dõi bằng lệnh `/add` ngay hôm nay!"
    )


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bật bot (chỉ admin)."""
    global BOT_ACTIVE

    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    BOT_ACTIVE = True
    set_bot_active(True)   # 🔄 Lưu trạng thái vào DB

    msg = (
    "✅ *Hệ thống đã được kích hoạt trở lại.*\n\n"
    "Bot hiện đang ở trạng thái *hoạt động bình thường* và sẵn sàng phục vụ người dùng. 🚀"
    )


    log.info("[ADMIN] Bot đã bật (BOT_ACTIVE=True, lưu vào DB).")
    await reply_md(update, msg)


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tắt bot (chỉ admin)."""
    global BOT_ACTIVE

    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    BOT_ACTIVE = False
    set_bot_active(False)  # 🔄 Lưu trạng thái vào DB

    msg = (
        "🛠️ *Hệ thống đã chuyển sang chế độ bảo trì.*\n\n"
        "Tất cả lệnh người dùng sẽ bị tạm ngưng. "
        "Trạng thái này đã được lưu trong cơ sở dữ liệu và sẽ giữ nguyên sau khi deploy. 🔒"
    )

    log.info("[ADMIN] Bot đã tắt (BOT_ACTIVE=False, lưu vào DB).")
    await reply_md(update, msg)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hiển thị trạng thái bot hiện tại (admin only)."""
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return
    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    current_state = get_bot_active()
    status = "🟢 Đang *hoạt động bình thường*" if current_state else "🔴 Đang *bảo trì*"
    await reply_md(update,
        f"{status}\n(Dữ liệu lấy trực tiếp từ cơ sở dữ liệu.)"
    )

async def cmd_news_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /news_on – bật nhận tất cả tin tức (vĩ mô + chuyên ngành) cho user hiện tại.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/news_on", ADMIN_ID)

    set_news_pref(chat_id, enable_specialized=True, enable_macro=True)
    await reply_md(
        update,
        "🔔 Bạn đã BẬT nhận tin tức:\n"
        "👉 Tin vĩ mô: Bật\n"
        "👉 Tin chuyên ngành theo danh mục: Bật\n\n"
        "💡 Có thể dùng `/news_off` nếu sau này muốn tạm tắt.",
    )


async def cmd_news_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /news_off – tắt toàn bộ tin tức (vĩ mô + chuyên ngành) cho user hiện tại.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/news_off", ADMIN_ID)

    set_news_pref(chat_id, enable_specialized=False, enable_macro=False)
    await reply_md(
        update,
        "🔕 Bạn đã TẮT nhận mọi loại tin tức (vĩ mô & chuyên ngành).\n\n"
        "Có thể bật lại bất cứ lúc nào bằng lệnh `/news_on.`",
    )


async def cmd_news_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /news_status – xem trạng thái nhận tin tức hiện tại.
    """
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    pref = get_news_pref(chat_id)

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
    """
    /news_test_macro – test nhanh việc đọc RSS macro & hiển thị.
    Nên dùng cho admin để debug.
    """
    chat_id = update.effective_chat.id

    # Giới hạn chỉ admin (nếu muốn)
    if chat_id != ADMIN_ID:
        await reply_md(update, "⚠️ Lệnh này chỉ dành cho admin.")
        return

    await reply_md(update, "⏱ Đang đọc RSS macro, đợi xíu nhé...")

    try:
        entries = await asyncio.to_thread(
            fetch_rss_entries_for_urls, RSS_FEEDS_MACRO
        )
    except Exception as e:
        await reply_md(update, f"❌ Lỗi đọc RSS macro: `{e}`")
        return

    if not entries:
        await reply_md(update, "❌ Không đọc được bài nào từ RSS macro.")
        return

    # Lấy 2 bài mới nhất để test
    for it in entries[:2]:
        title = it["title"] or ""
        raw_summary = it.get("summary") or ""
        link = it["link"] or ""
        source = it.get("source") or ""
        pub_dt = it.get("published")

        cleaned = clean_html_text(raw_summary)

        if isinstance(pub_dt, datetime.datetime):
            pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
        else:
            pub_str = ""

        lines = [
            "🧪 *Test tin vĩ mô:*",
            title,
            "",
            cleaned[:500] + ("..." if len(cleaned) > 500 else ""),
        ]

        meta = []
        if source:
            meta.append(f"Nguồn: {source}")
        if pub_str:
            meta.append(f"Thời gian: {pub_str}")
        if meta:
            lines.append("")
            lines.append(" | ".join(meta))

        if link:
            lines.append("")
            lines.append(f"🔗 {link}")

        await reply_md(update, "\n".join(lines))
        await asyncio.sleep(0.3)

async def cmd_news_test_specialized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /news_test_specialized – test nhanh việc quét tin chuyên ngành theo symbol.
    - Nếu truyền argument: /news_test_specialized HPG SSI
        → test với các mã đó.
    - Nếu không truyền argument:
        → lấy danh mục (watchlist) của chính user (thường là admin) để test.
    Chỉ nên dùng cho admin để debug.
    """
    chat_id = update.effective_chat.id

    # Giới hạn cho admin (nếu bạn muốn mở cho ai cũng xài thì bỏ if này)
    if chat_id != ADMIN_ID:
        await reply_md(update, "⚠️ Lệnh này chỉ dành cho admin.")
        return

    log_command_usage(chat_id, "/news_test_specialized", ADMIN_ID)

    # 1) Lấy danh sách symbol để test
    args = context.args or []
    symbols_raw: list[str] = []

    if args:
        # Dùng symbol user truyền vào
        symbols_raw = args
    else:
        # Không truyền arg: dùng danh mục của chính user
        watch = get_watch_list_for_chat(chat_id) or []
        symbols_raw = watch

    symbols = sorted({s.strip().upper() for s in symbols_raw if s.strip()})
    if not symbols:
        await reply_md(
            update,
            "❌ Không có mã nào để test.\n\n"
            "Hãy dùng:\n"
            "- `/news_test_specialized HPG SSI`\n"
            "hoặc\n"
            "- Thêm mã vào danh mục rồi chạy `/news_test_specialized` không tham số.",
        )
        return

    await reply_md(
        update,
        "⏱ Đang đọc RSS *chuyên ngành* và quét theo các mã:\n"
        f"`{', '.join(symbols)}`\n"
        "_(chỉ lấy khoảng vài bài match đầu tiên để hiển thị)_",
    )

    # 2) Gộp URL RSS chuyên ngành
    all_specialized_urls: list[str] = []
    for urls in RSS_FEEDS_SPECIALIZED.values():
        all_specialized_urls.extend(urls)

    try:
        entries = await asyncio.to_thread(
            fetch_rss_entries_for_urls, all_specialized_urls
        )
    except Exception as e:
        await reply_md(update, f"❌ Lỗi đọc RSS chuyên ngành: `{e}`")
        return

    if not entries:
        await reply_md(update, "❌ Không đọc được bài nào từ RSS chuyên ngành.")
        return

    # 3) Chuẩn bị pattern cho từng symbol dùng COMPANY_KEYWORDS
    patterns: dict[str, re.Pattern] = {}
    for sym in symbols:
        keywords = COMPANY_KEYWORDS.get(sym, [sym])
        combined = "|".join(re.escape(k) for k in keywords if k)
        if not combined:
            continue
        patterns[sym] = re.compile(rf"\b({combined})\b", re.IGNORECASE)

    if not patterns:
        await reply_md(
            update,
            "❌ Không tạo được pattern cho bất kỳ mã nào.\n"
            "Kiểm tra lại COMPAN​Y_KEYWORDS hoặc file `ssi_master_list.csv`.",
        )
        return

    # 4) Quét từng bài, tìm xem match symbol nào
    matched_items: list[tuple[dict, list[str]]] = []  # (entry, [symbols])
    for it in entries:
        title = it["title"] or ""
        raw_summary = it.get("summary") or ""
        link = it["link"] or ""
        source = it.get("source") or ""
        pub_dt = it.get("published")

        decoded_summary = clean_html_text(raw_summary)
        text_for_match = (title + " " + decoded_summary)

        if not text_for_match.strip():
            continue

        hit_syms: list[str] = []
        for sym, pat in patterns.items():
            if pat.search(text_for_match):
                hit_syms.append(sym)

        if hit_syms:
            matched_items.append((it, sorted(set(hit_syms))))

        # Giới hạn, tránh spam – lấy tối đa 5 bài match
        if len(matched_items) >= 5:
            break

    if not matched_items:
        await reply_md(
            update,
            "ℹ️ Không tìm thấy bài chuyên ngành nào match với các mã:\n"
            f"`{', '.join(symbols)}`\n"
            "Thử lại sau hoặc thử với mã khác.",
        )
        return

    # 5) Gửi kết quả test cho admin
    for it, hit_syms in matched_items:
        title = it["title"] or ""
        raw_summary = it.get("summary") or ""
        link = it["link"] or ""
        source = it.get("source") or ""
        pub_dt = it.get("published")

        decoded_summary = clean_html_text(raw_summary)

        if isinstance(pub_dt, datetime.datetime):
            pub_str = pub_dt.strftime("%Y-%m-%d %H:%M")
        else:
            pub_str = ""

        short_sum = decoded_summary
        if len(short_sum) > 400:
            short_sum = short_sum[:380].rstrip() + "..."

        lines = [
            "🧪 *Test tin chuyên ngành:*",
            title,
            "",
            f"Match với mã: `{', '.join(hit_syms)}`",
        ]
        if short_sum:
            lines.extend(["", short_sum])

        meta = []
        if source:
            meta.append(f"Nguồn: {source}")
        if pub_str:
            meta.append(f"Thời gian: {pub_str}")
        if meta:
            lines.append("")
            lines.append(" | ".join(meta))

        if link:
            lines.append("")
            lines.append(f"🔗 {link}")

        await reply_md(update, "\n".join(lines))
        await asyncio.sleep(0.3)

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add <MÃ>

    ✅ Yêu cầu:
    - Mã phải là 3 chữ cái (A–Z)
    - Không cho add chỉ số như VNINDEX, VN30,...
    - Chỉ add nếu có dữ liệu thực
    - Sau khi add: tóm tắt thông tin + hiển thị danh sách hiện tại
    """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/add", ADMIN_ID)  # ghi log (bỏ qua admin trong hàm log)

    # 1️⃣ Kiểm tra tham số
    if not context.args:
        await reply_md(update,
            "⚠️ Cách dùng: /add <MÃ>\n"
            "Ví dụ: /add HPG, /add SSI, /add VNM\n"
            "(*Chỉ hỗ trợ mã cổ phiếu gồm 3 chữ cái.*)"
        )
        return

    symbol = context.args[0].strip().upper()

    # 2️⃣ Kiểm tra định dạng mã: đúng 3 chữ cái A–Z
    if len(symbol) != 3 or not symbol.isalpha():
        await reply_md(update,
            "⚠️ Mã không hợp lệ.\n"
            "Hiện bot chỉ cho phép thêm *mã cổ phiếu* gồm đúng 3 chữ cái, "
            "ví dụ: HPG, SSI, VNM."
        )
        return

    # 3️⃣ Gọi dữ liệu realtime duy nhất 1 lần
    try:
        trading = Trading(source="VCI")
        df = trading.price_board([symbol])
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] [ADD] Lỗi khi gọi price_board cho {symbol}: {e}")
        await reply_md(update,
            f"⚠️ Không lấy được dữ liệu cho mã *{symbol}*. Vui lòng thử lại sau."
        )
        return

    # Không có dữ liệu -> xem như mã không hợp lệ / không giao dịch
    if df is None or len(df) == 0:
        await reply_md(update,
            f"⚠️ Không tìm thấy dữ liệu giao dịch cho mã *{symbol}*.\n"
            "Vui lòng kiểm tra lại mã hoặc thử mã khác.\n"
            "(*Chỉ hỗ trợ cổ phiếu đang giao dịch trên HOSE/HNX/UPCOM.*)"
        )
        return

    row = df.iloc[0]

    # Hàm chuẩn hoá giá trị về float / int nếu được
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

    # Lấy các field chính từ price_board
    price = None
    pct = None
    change_abs = None
    volume = None
    exchange = None

    # Tùy cấu trúc MultiIndex của vnstock, ta cố lấy các cột cần thiết
    try:
        price = norm(row.get(("match", "match_price"), None))
    except Exception:
        pass
    try:
        pct = norm(row.get(("match", "price_change_rate"), None))
    except Exception:
        pass
    try:
        change_abs = norm(row.get(("match", "price_change"), None))
    except Exception:
        pass
    try:
        volume = norm(row.get(("match", "accumulated_vol"), None))
    except Exception:
        pass
    try:
        exchange = row.get(("listing", "exchange"), None)
    except Exception:
        exchange = None

    # 4️⃣ Trường hợp giá = 0 (thường là trước giờ giao dịch)
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

    # 5️⃣ Lấy danh sách hiện tại
    lst = get_watch_list_for_chat(chat_id) or []

    # Nếu đã tồn tại: báo + show luôn list
    if symbol in lst:
        symbols_text = ", ".join(lst) if lst else "—"
        msg = (
            f"ℹ️ *{symbol}* đã có trong danh sách theo dõi rồi.\n\n"
            "📋 *Danh sách mã bạn đang theo dõi hiện tại:*\n"
            f"{symbols_text}"
        )
        await reply_md(update, msg)
        return

    # 6️⃣ Thêm mã mới vào danh sách
    lst.append(symbol)
    save_watch_list_for_chat(chat_id, lst)

    # Chuẩn bị đoạn danh sách hiện tại để ghép thêm vào message
    symbols_text = ", ".join(lst)
    watchlist_section = (
        "\n\n📋 *Danh sách mã bạn đang theo dõi hiện tại:*\n"
        f"{symbols_text}"
    )

    # 7️⃣ Tóm tắt thông tin cổ phiếu + danh sách cuối cùng
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
    """
    /remove <MÃ>

    - Nếu mã có trong danh sách: xoá và hiển thị lại danh sách còn lại.
    - Nếu mã không có: báo lỗi nhẹ + gợi ý dùng /list.
    """
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/remove", ADMIN_ID)   # bỏ qua admin trong thống kê

    # Không truyền mã -> hướng dẫn
    if not context.args:
        await reply_md(update,"⚠️ Cách dùng: /remove <MÃ>\nVí dụ: /remove SSI")
        return

    symbol = context.args[0].upper().strip()
    lst = get_watch_list_for_chat(chat_id) or []

    if symbol in lst:
        lst.remove(symbol)
        save_watch_list_for_chat(chat_id, lst)

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
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    log_command_usage(chat_id, "/list", ADMIN_ID)   # ghi log nhưng bỏ qua admin

    lst = get_watch_list_for_chat(chat_id) or []

    if not lst:
        await reply_md(update,
            "📭 Danh sách theo dõi hiện đang trống.\n"
            "Bạn có thể dùng lệnh /add <MÃ> để thêm cổ phiếu vào danh sách.",
        )
        return

    # Format danh sách cho đẹp
    symbols_text = ", ".join(lst)

    msg = (
        "📋 *Danh sách mã bạn đang theo dõi:*\n"
        f"{symbols_text}\n\n"
        "Bạn có thể dùng /remove <MÃ> để xoá một mã khỏi danh sách."
    )

    await reply_md(update, msg)

# Dùng dict lưu tạm xác nhận theo admin_id
pending_clear_confirmations = {}

async def cmd_screener_value_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /screener_value_clear – Yêu cầu xác nhận trước khi XOÁ BẢNG cache Value.
    """
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await reply_md(update,"⛔ Chỉ admin mới có quyền dùng lệnh này.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)

    if user_id in pending_clear_confirmations:
        confirm_time = pending_clear_confirmations[user_id]
        if now - confirm_time < timedelta(seconds=30):
            del pending_clear_confirmations[user_id]

            before_count = 0
            try:
                before_count = get_stock_value_cache_count()
            except Exception:
                pass # Bảng có thể đã bị lỗi, cứ cho qua

            clear_stock_value_cache() # ⬅️ Sẽ gọi hàm DROP TABLE mới
            
            # Sau khi DROP, count chắc chắn là 0
            after = get_stock_value_cache_count()

            msg = (
                f"🧹 Đã **XOÁ HOÀN TOÀN BẢNG** (DROP TABLE) `stock_value_cache`.\n"
                f"Trước khi xoá: **{before_count}** dòng.\n"
                f"Sau khi xoá: **{after}** dòng.\n\n"
                "✅ Bot sẽ tự động *tạo lại bảng với cấu trúc mới nhất* trong lần crawl tiếp theo (khi khởi động lại hoặc vào 00:00)."
            )
            await reply_md(update, msg)
            return
        else:
            del pending_clear_confirmations[user_id]

    pending_clear_confirmations[user_id] = now
    await reply_md(update,
        "⚠️ *XÁC NHẬN XOÁ BẢNG (DROP TABLE)*\n\n"
        "Thao tác này sẽ **XOÁ HOÀN TOÀN** bảng `stock_value_cache` để cập nhật cấu trúc mới.\n"
        "Gõ lệnh */screener_value_clear* lần nữa trong vòng *30 giây* để xác nhận."
    )

async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await reply_md(update,"⛔ Chỉ admin mới có quyền dùng lệnh này.")
        return

    if not context.args:
        await reply_md(update,"❗ Vui lòng nhập nội dung thông báo sau lệnh /announce.")
        return

    # Lấy nội dung announce từ admin
    text = " ".join(context.args)
    text = text.replace("\\n", "\n")  # chỉ giữ lại xuống dòng thôi
    # Escape các ký tự đặc biệt MarkdownV2 ngoại trừ * và \n
    text = re.sub(r'([_`\[\]()~>#+\-=|{}.!])', r'\\\1', text)

    # Gửi cho tất cả user
    all_watch = get_all_watch()
    sent = 0

    for chat_key in all_watch.keys():
        try:
            chat_id = int(chat_key)
            send_msg_to(chat_id, text)  # không escape nữa
            sent += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            log.warning(f"Lỗi gửi announce tới {chat_key}: {e}")

    await reply_md(update, f"✅ Đã gửi thông báo tới *{sent}* người dùng.")

async def cmd_allwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chỉ cho admin dùng
    if ADMIN_ID is None:
        await reply_md(update,"⚠️ Bot chưa cấu hình ADMIN_ID.")
        return

    if update.effective_user.id != ADMIN_ID:
        await reply_md(update,"⛔ Không có quyền.")
        return

    all_watch = get_all_watch()
    if not all_watch:
        await reply_md(update,"📭 Chưa có user nào lưu danh sách theo dõi.")
        return

    # Thống kê symbol -> số user đang theo dõi
    symbol_counts = {}
    detail_lines = []

    for chat_key, block in all_watch.items():
        lst = block.get("list", []) or []
        # thống kê theo mã
        for sym in lst:
            symbol_counts[sym] = symbol_counts.get(sym, 0) + 1

        # chi tiết theo từng user
        if lst:
            detail_lines.append(f"- {chat_key}: {', '.join(lst)}")
        else:
            detail_lines.append(f"- {chat_key}: (trống)")

    # Phần thống kê theo mã
    stats_lines = []
    for sym, cnt in sorted(symbol_counts.items()):
        stats_lines.append(f"{sym}: {cnt} user")

    cmd_stats = get_command_stats()

    # 👇 DÒNG NÀY ĐỂ LỌC BỎ "unknown:"
    cmd_stats = [s for s in cmd_stats if not s["command"].startswith("unknown:")]

    if cmd_stats:
        cmd_summary = "📊 *Thống kê lệnh được sử dụng:*\n"
        for row in cmd_stats:
            cmd_name = escape_markdown_v2(row["command"])  # /screener_value_clear, /start, ...
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

    # Ghép text và tự chia nhỏ nếu quá dài
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
    """Xoá các tin nhắn do bot gửi trong khoảng thời gian chỉ định."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await reply_md(update,"⛔ Chỉ admin mới có quyền xoá tin nhắn.")
        return

    args = context.args
    if len(args) < 4:
        await reply_md(update,
            "❗ Cú pháp: /delete_range <từ ngày> <giờ> <đến ngày> <giờ>\n"
            "Ví dụ: /delete_range 2025-03-01 09:00 2025-03-01 10:30"
        )

        return

    try:
        vn_tz = pytz.timezone(TIMEZONE)
        start_str = f"{args[0]} {args[1]}"
        end_str = f"{args[2]} {args[3]}"
        start_time = vn_tz.localize(datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M"))
        end_time = vn_tz.localize(datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M"))

        records = get_bot_messages_in_range(start_time, end_time)
        if not records:
            await reply_md(update,"📭 Không có tin nhắn nào trong khoảng thời gian này.")
            return

        deleted = 0
        for chat_id, msg_id in records:
            try:
                url = f"https://api.telegram.org/bot{TOKEN}/deleteMessage"
                params = {"chat_id": chat_id, "message_id": msg_id}
                requests.get(url, params=params, timeout=10)
                deleted += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                log.warning(f"Lỗi xoá message {msg_id} trong chat {chat_id}: {e}")

        delete_bot_messages_in_range(start_time, end_time)
        await reply_md(update,f"✅ Đã xoá {deleted} tin nhắn trong khoảng {start_str} → {end_str}.")

    except Exception as e:
        await reply_md(update,f"⚠️ Lỗi xử lý: {e}")



async def _collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tự động lưu chat_id vào DB nếu chưa có."""
    if update and update.effective_chat:
        chat_id = update.effective_chat.id
        lst = get_watch_list_for_chat(chat_id)
        if lst is None:
            save_watch_list_for_chat(chat_id, [])

# ==============================================
# COMMAND: /report (CÓ CACHE + COOLDOWN + RETRY)
# Cache nội dung report theo danh mục
REPORT_CACHE = {}
REPORT_COOLDOWN = {}  # {chat_id: last_time}

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi báo cáo danh mục ngay lập tức cho user (có cache & cooldown)."""
    if not BOT_ACTIVE:
        await reply_md(update,"⚙️ Bot đang bảo trì.")
        return

    if not update or not update.effective_chat:
        return

    chat_id = update.effective_chat.id

    # Cooldown chống spam: mỗi user chỉ được dùng /report 1 lần / ngày
    now = datetime.datetime.now(pytz.timezone(TIMEZONE))
    COOLDOWN_SECONDS = 24 * 3600  # 24 giờ

    last_time = REPORT_COOLDOWN.get(chat_id)
    if last_time and (now - last_time).total_seconds() < COOLDOWN_SECONDS:
        remaining = int(COOLDOWN_SECONDS - (now - last_time).total_seconds())
        hours = remaining // 3600
        mins = (remaining % 3600) // 60
        await reply_md(update,
            f"⏳ /report chỉ được dùng 1 lần mỗi ngày. "
            f"Vui lòng thử lại sau {hours} giờ {mins} phút."
        )
        return


    REPORT_COOLDOWN[chat_id] = now
    log_command_usage(chat_id, "/report", ADMIN_ID)   # Ghi log

    # Lấy danh mục
    watch = get_watch_list_for_chat(chat_id)
    symbols = [s.upper() for s in (watch or []) if not s.upper().startswith("VN")]

    if not symbols:
        await reply_md(update,"📭 Danh mục của bạn trống. Hãy /add vài mã trước nhé!")
        return

    # Tạo key cache
    cache_key = "-".join(sorted(symbols))

    await reply_md(update,"⏳ Đang tổng hợp báo cáo danh mục, vui lòng đợi vài giây...")

    # Dùng cache nếu có
    if cache_key in REPORT_CACHE:
        log.info(f"[{INSTANCE_ID}] /report cache hit for {chat_id} ({cache_key})")
        cached_text, cached_time = REPORT_CACHE[cache_key]
        # Nếu cache dưới 12 tiếng thì dùng lại
        if (now - cached_time).total_seconds() < 12 * 3600:
            await reply_md(update,cached_text)
            return

    # Gọi OpenRouter (có retry)
    async def fetch_report_with_retry():
        retry = 0
        while retry < 3:
            start = time.time()
            text = await asyncio.to_thread(call_chatgpt_for_report, symbols)
            duration = time.time() - start
            log.info(f"[{INSTANCE_ID}] /report round {retry+1} done in {duration:.2f}s")

            if "⚠️ Hiện tại không tạo được" not in text and "429" not in text:
                return text
            retry += 1
            await asyncio.sleep(10 * retry)
        return text

    text = await fetch_report_with_retry()

    # Lưu cache
    REPORT_CACHE[cache_key] = (text, now)

    # Gửi báo cáo
    try:
        await reply_md(update,text)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}] Lỗi gửi báo cáo /report cho {chat_id}: {e}")
        # Gửi fallback rút gọn nếu lỗi parse Markdown
        await reply_md(update,"📋 Báo cáo đã được tạo xong nhưng gặp lỗi định dạng. Vui lòng thử lại sau nhé.")


# ==============================================
# VÒNG LẶP CẢNH BÁO (CÓ CACHE SYMBOL)
# ==============================================
async def alert_loop():
    vn_tz = pytz.timezone(TIMEZONE)
    loop_id = 0
    TARGET_INTERVAL = 15  # giãn cách giữa 2 vòng quét (giây)

    while True:
        loop_id += 1
        now = datetime.datetime.now(vn_tz)

        # ❗️ KIỂM TRA TRẠNG THÁI BOT
        if not BOT_ACTIVE:
            log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] [cảnh báo realtime trong giờ giao dịch] Bot đang TẮT, sleep 60s.")
            await asyncio.sleep(60)
            continue # Quay lại vòng lặp, kiểm tra BOT_ACTIVE tiếp

        # Nếu ngoài giờ giao dịch -> ngủ tới phiên tiếp theo
        if not in_session_vietnam():
            next_start = next_session_start(now)
            delay = max((next_start - now).total_seconds(), 60.0)
            log.info(
                f"[{INSTANCE_ID}][LOOP {loop_id}] Ngoài giờ giao dịch, sleep {delay:.0f}s tới {next_start.strftime('%Y-%m-%d %H:%M')}"
            )
            await asyncio.sleep(delay)
            continue

        loop_start = now
        try:
            log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Bắt đầu vòng alert (có cache)")

            all_watch = get_all_watch()
            all_state = get_state_for_all()

            # 🧩 1️⃣ Gom tất cả symbol cần theo dõi
            all_symbols = set()
            for block in all_watch.values():
                for sym in (block.get("list", []) or []):
                    all_symbols.add(sym.upper())

            if not all_symbols:
                log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Không có symbol nào, sleep 60s.")
                await asyncio.sleep(60)
                continue

            # 🧩 2️⃣ Cache dữ liệu quote cho từng symbol
            quote_cache = {}
            for sym in all_symbols:
                data = get_quote(sym)
                if data:
                    quote_cache[sym] = data
            log.info(f"[{INSTANCE_ID}][LOOP {loop_id}] Đã lấy dữ liệu cho {len(quote_cache)}/{len(all_symbols)} symbol.")

            # 🧩 3️⃣ Duyệt từng user & xử lý cảnh báo
            for chat_key, user_block in all_watch.items():
                chat_id = int(chat_key)
                watch_list = user_block.get("list", []) or []
                if not watch_list:
                    continue

                if chat_key not in all_state:
                    all_state[chat_key] = {}
                personal_state = all_state[chat_key]
                messages = []

                for sym in watch_list:
                    sym_u = sym.upper()
                    quote = quote_cache.get(sym_u)
                    if not quote:
                        continue

                    price, pct, change_abs = (
                        quote["price"],
                        quote["pct"],
                        quote["change_abs"],
                    )

                    is_index = sym_u.startswith("VN")
                    metric = change_abs if is_index else pct
                    new_lvl = pick_new_level(
                        metric, INDEX_POINT_LEVELS if is_index else STOCK_LEVELS
                    )

                    state_entry = personal_state.get(sym_u, {})
                    if isinstance(state_entry, dict):
                        prev_lvl = state_entry.get("last_level", 0)
                        last_alert_at_str = state_entry.get("last_alert_at")
                    else:
                        prev_lvl = state_entry or 0
                        last_alert_at_str = None

                    last_alert_at = None
                    if last_alert_at_str:
                        try:
                            last_alert_at = datetime.datetime.fromisoformat(last_alert_at_str)
                        except Exception:
                            last_alert_at = None

                    should_alert = False
                    if new_lvl is not None:
                        if new_lvl != prev_lvl:
                            should_alert = True
                        elif (
                            last_alert_at is None
                            or (now - last_alert_at).total_seconds() >= ALERT_COOLDOWN_SECONDS
                        ):
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
                            personal_state[sym_u] = {"last_level": 0, "last_alert_at": None}

                # Gửi nếu có thông báo
                if messages:
                    header = f"--------------------------------\n⏰ *Cảnh báo {now.strftime('%H:%M:%S')}*"
                    # Ghép các thông điệp trong messages (list[str]) lại thành 1 chuỗi
                    messages_text = "\n\n".join(messages)
                    # Đặt messages trước, header sau
                    body = messages_text + "\n\n" + header  
                    send_msg_to(chat_id, body)



                all_state[chat_key] = personal_state

            # 🧩 4️⃣ Lưu lại state
            save_state_for_all(all_state)

        except Exception as e:
            log.error(f"[{INSTANCE_ID}][LOOP {loop_id}] ERROR: {e}")

        # 🕒 Giữ nhịp cố định
        elapsed = (datetime.datetime.now(vn_tz) - loop_start).total_seconds()
        delay = max(TARGET_INTERVAL - elapsed, 1)
        log.info(f"[{INSTANCE_ID}] Sleep {delay:.1f}s\n")
        await asyncio.sleep(delay)

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
                
                # ==========================================================
                # 🚀 YÊU CẦU MỚI: Dừng 1 phút
                # Tạo một khoảng đệm an toàn để Render/UptimeRobot 
                # health check thành công TRƯỚC KHI các loop nặng chạy.
                log.info("[Lifespan] Đang chờ 60s để Render ổn định health check...")
                await asyncio.sleep(60) 
                # ==========================================================
 
                # (Lấy toàn bộ các loop từ hàm main() chuyển lên đây)
                BACKGROUND_TASKS = [
                    MAIN_LOOP.create_task(alert_loop()),
                    MAIN_LOOP.create_task(session_notice_loop()),
                    MAIN_LOOP.create_task(daily_report_loop()),
                    MAIN_LOOP.create_task(screener_value_update_loop()),
                    MAIN_LOOP.create_task(daily_screener_loop()),
                    MAIN_LOOP.create_task(initial_value_precompute_loop()),
                    MAIN_LOOP.create_task(news_specialized_loop()),
                    MAIN_LOOP.create_task(news_macro_loop()),
                    MAIN_LOOP.create_task(run_background_startup_tasks(ADMIN_ID, initial_active, INSTANCE_ID, tg_app)),
                    MAIN_LOOP.create_task(auto_on_after_delay(initial_active)),
                ]
                log.info(f"[Lifespan] Đã khởi động {len(BACKGROUND_TASKS)} tác vụ nền.")
            
            elif message["type"] == "lifespan.shutdown":
                log.info("[Lifespan] Server shutdown. Cancelling background tasks...")
                
                # 3. Dọn dẹp các tác vụ nền khi tắt
                for task in BACKGROUND_TASKS:
                    task.cancel()
                    
                log.info("[Lifespan] Đang xóa webhook...")
                try:
                    if IS_PRODUCTION: 
                        await tg_app.bot.delete_webhook(drop_pending_updates=True)
                        log.info("[Lifespan] Đã xóa webhook.")
                except Exception as e:
                    log.warning(f"[Lifespan] Lỗi khi xóa webhook: {e}")
                
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
            ("on", "(admin) Bật bot (thoát chế độ bảo trì)"),
            ("off", "(admin) Tắt bot (bảo trì tạm thời)"),
            ("status", "(admin) Kiểm tra trạng thái hoạt động của bot"),
            ("announce", "(admin) Gửi thông báo đến tất cả người dùng"),
            ("allwatch", "(admin) Thống kê toàn bộ danh sách theo dõi của user"),
            ("screener_value_clear", "(admin) Xóa dữ liệu screener cache (làm mới)"),
            ("delete_range", "(admin) Xóa tin nhắn bot gửi trong khoảng thời gian"),
            ("news_test_macro", "Gửi thử tin tức vĩ mô mới nhất"),
            ("news_test_specialized", "Gửi thử tin tức vĩ mô mới nhất"),
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
                    parts.append(git_info)
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

    # Đăng ký các command handlers (giữ nguyên code của bạn)
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("news_on", cmd_news_on))
    tg_app.add_handler(CommandHandler("news_off", cmd_news_off))
    tg_app.add_handler(CommandHandler("news_status", cmd_news_status))
    tg_app.add_handler(CommandHandler("news_test_macro", cmd_news_test_macro))
    tg_app.add_handler(CommandHandler("news_test_specialized", cmd_news_test_specialized))
    tg_app.add_handler(CommandHandler("on", cmd_on))
    tg_app.add_handler(CommandHandler("off", cmd_off))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("announce", cmd_announce))
    tg_app.add_handler(CommandHandler("allwatch", cmd_allwatch))
    tg_app.add_handler(CommandHandler("delete_range", cmd_delete_range))
    tg_app.add_handler(CommandHandler("report", cmd_report))
    tg_app.add_handler(CommandHandler("screener_value_clear", cmd_screener_value_clear))
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
