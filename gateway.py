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
    SCREENER_WEBAPP_TEMPLATE,
    LOCKED_FEATURE_TEMPLATE,
    EOD_HTML_TEMPLATE, 
    EOD_404_TEMPLATE,
    FLASH_VIEW_HTML_TEMPLATE,
    ADMIN_MOBILE_TEMPLATE,
)
# --- GLOBAL VARIABLES ---
_vci_blocked_date = None

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Update, WebAppInfo, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
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
from chart_utils import draw_sector_performance_chart, generate_sector_table_html
from db_utils import (
    init_db,
    get_all_watch,
    get_watch_list_for_chat,
    save_watch_list_for_chat,
    get_bot_active,
    set_bot_active,
    log_command_usage,
    save_bot_message,
    get_bot_messages_in_range,
    delete_bot_messages_in_range,
    export_core_data,
    import_core_data,
    get_conn,
    add_paid_user,
    is_user_pro,
    deactivate_paid_user,
    remove_paid_user,
    get_all_pro_chat_ids,
    get_user_pro_expiry,
    create_pending_order,
    get_order_by_id,
    mark_order_as_paid,
    save_bot_message,
    get_messages_to_cleanup,
    delete_bot_log_record,
    save_historical_valuation_to_redis,
    upsert_user_info,
    get_admin_dashboard_data,
    get_user_orders,
    get_user_logs,
    get_user_configs,
    get_vn30f1m_enabled_map,
    set_vn30f1m_enabled,
    get_stock_alert_enabled_map,
    set_stock_alert_enabled,
    get_vnindex_enabled_map,
    set_vnindex_enabled,
    get_vn30_enabled_map,
    set_vn30_enabled,
    get_total_revenue_real,
    update_user_admin_note,
    get_banned_users,
    set_user_ban_status,
    check_trial_eligibility,
    activate_trial_package,
    get_digest_from_redis,
    get_historical_valuation_from_redis,
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
import uuid
import hmac
from chart_utils import (
    get_flash_view_data,
    draw_line_chart_fixed_ui,
    draw_orderbook_fixed_ui,
    generate_chart_html,
    generate_mini_chart,
)
from decimal import Decimal
from functools import wraps
import redis

# --- HÀM HELPER VẼ THANH TIẾN TRÌNH ---
def make_progress_bar(percent: int, width: int = 8) -> str:
    """Tạo thanh loading dạng text: ▰▰▰▱▱"""
    filled = int(width * percent / 100)
    empty = width - filled
    return "▰" * filled + "▱" * empty

# ANTI-SPAM & LOCKING
_user_last_action_time = {}
SPAM_COOLDOWN = 1  # Giây

TASK_LOCK_TTL_SECONDS = 180
TASK_LOCK_PREFIX = "lock:user_task"


def _task_lock_key(chat_id: int) -> str:
    return f"{TASK_LOCK_PREFIX}:{chat_id}"


def acquire_task_lock(chat_id: int) -> bool:
    r = get_redis()
    return bool(r.set(_task_lock_key(chat_id), "1", nx=True, ex=TASK_LOCK_TTL_SECONDS))


def release_task_lock(chat_id: int):
    try:
        r = get_redis()
        r.delete(_task_lock_key(chat_id))
    except Exception as exc:
        log.error(f"Redis release lock error {chat_id}: {exc}")


def is_task_locked(chat_id: int) -> bool:
    try:
        r = get_redis()
        return bool(r.exists(_task_lock_key(chat_id)))
    except Exception:
        return False

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
                    await update.callback_query.answer("⏳ Bình tĩnh, bấm chậm lại...", show_alert=False)
                except: pass
            return 
            
        return await func(update, context, *args, **kwargs)
    return wrapper


def task_locked(func=None, *, manual_release: bool = False):
    """Decorator khóa tác vụ dùng Redis với tuỳ chọn tự mở khóa."""

    def decorator(inner_func):
        @wraps(inner_func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            if not update.effective_chat:
                return await inner_func(update, context, *args, **kwargs)

            chat_id = update.effective_chat.id

            try:
                acquired = acquire_task_lock(chat_id)
            except Exception as exc:
                log.error(f"Redis Lock Error: {exc}")
                return await inner_func(update, context, *args, **kwargs)

            if not acquired:
                return

            try:
                return await inner_func(update, context, *args, **kwargs)
            finally:
                if not manual_release:
                    release_task_lock(chat_id)

        return wrapper

    if callable(func):
        return decorator(func)

    return decorator
# ==============================================
# CẤU HÌNH CƠ BẢN
# ==============================================

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.getenv("PASSENGER_PORT", "10000"))
TIMEZONE = "Asia/Ho_Chi_Minh"
ADMIN_ID_STR = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_STR) if ADMIN_ID_STR else None

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_CHANNEL_OUTBOUND = 'telegram_outbound'
REDIS_CHANNEL_INBOUND = 'worker_inbound'
VALID_AGENT_SCOPES = {"macro", "biz", "tech", "all"}


def _agent_result_key(agent_type: str) -> str:
    return f"agent:{agent_type}:current"


def _agent_bundle_key(chat_id: int) -> str:
    return f"agent:bundle:{chat_id}:current"

def push_to_worker(payload):
    """Gửi lệnh sang Worker"""
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.publish(REDIS_CHANNEL_INBOUND, json.dumps(payload))
    except Exception as e:
        log.error(f"Push Worker Error: {e}")

# 🔥 CACHE DANH SÁCH ĐEN (Lưu trong RAM để check siêu nhanh)
BANNED_CACHE = set()

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

# ==============================================
# HÀM TIỆN ÍCH
# ==============================================

async def send_md(bot: telegram.Bot, chat_id: int, text: str, msg_type: str = 'GENERAL', reply_markup=None, **kwargs):
    """
    Gửi tin nhắn Markdown an toàn (async) + Ghi log msg_type.
    [UPDATED] Hỗ trợ nhận reply_markup từ tham số.
    """
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup, # <--- Điểm thay đổi quan trọng
            **kwargs,
        )
        # 🔥 LƯU DB ASYNC (Chạy trong thread để không chặn)
        await asyncio.to_thread(save_bot_message, chat_id, msg.message_id, msg_type)
        return msg
    except BadRequest as e:
        # (Giữ nguyên phần xử lý lỗi cũ của bạn nếu muốn, hoặc dùng code mặc định này)
        log.warning(f"[Send Error] {e}")
        pass
    except Exception as e:
        log.error(f"[Telegram Send Error] chat={chat_id}: {e}")

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


async def delete_message_safely(chat_id: int, message_id: int):
    """Delete message without breaking the loop when Telegram rejects it."""
    try:
        await tg_app.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        if "message to delete not found" in str(exc).lower():
            return
        log.warning(f"[GATEWAY] Delete message error: {exc}")
    except Exception as exc:
        log.warning(f"[GATEWAY] Unexpected delete error: {exc}")



# --- HELPER CHO SCREENER MEAN REVERSION ---

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

    # [MỚI] Xử lý nút Dashboard từ Reply Keyboard
    if "DASHBOARD" in user_text:
        await cmd_start(update, context)
        return

    # --- LOGIC MỚI: SMART INPUT HANDLING ---
    # Kiểm tra: Đúng 3 ký tự VÀ là chữ cái (A-Z)
    if len(user_text) == 3 and user_text.isalpha():
        # [MỚI] Validate mã có tồn tại không trước khi hiện menu
        try:
            # Gửi action typing để user biết bot đang check
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            
            # Gọi hàm fetch giá để check tồn tại (nhanh)
            data = await fetch_data_smart([user_text])
            
            # Chỉ hiện menu nếu lấy được dữ liệu giá (tức là mã tồn tại)
            if data and user_text in data:
                # Kiểm tra xem mã đã có trong watchlist chưa
                watchlist = await asyncio.to_thread(get_watch_list_for_chat, chat_id) or []
                is_watched = user_text in watchlist

                base_url = os.getenv("RENDER_EXTERNAL_URL", "https://google.com")
                chart_url = f"{base_url}/chart/{user_text}"

                # Xác định nút hành động (Thêm hoặc Xóa)
                if is_watched:
                    action_btn = InlineKeyboardButton(f"🗑️ Bỏ theo dõi", callback_data=f"btn_del_{user_text}")
                else:
                    action_btn = InlineKeyboardButton(f"➕ Theo dõi", callback_data=f"btn_add_{user_text}")

                # Tạo nút bấm Inline
                kb = [
                    [InlineKeyboardButton(f"📊 Soi Chart {user_text}", web_app=WebAppInfo(url=chart_url))],
                    [
                        action_btn,
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
            else:
                # [MỚI] Báo lỗi nếu mã không tồn tại
                await reply_md(update, f"⚠️ Mã **{user_text}** không tồn tại trên sàn chứng khoán.\nVui lòng kiểm tra lại.")
                return
        except Exception as e:
            log.warning(f"Check symbol {user_text} error: {e}")
            # Nếu lỗi check thì bỏ qua, xuống phần AI xử lý
    # ---------------------------------------

    # Logic cũ (Xử lý user mới + Báo lỗi)
    try:
        # Tự động lưu chat_id vào DB nếu chưa có (giữ nguyên logic cũ của bạn)
        lst = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        if lst is None:
            await asyncio.to_thread(save_watch_list_for_chat, chat_id, [])
    except Exception as e:
        log.warning(f"Lỗi khi auto-save chat_id {chat_id}: {e}")

    # --- 2. FALLBACK (GÕ BẬY BẠ / KHÔNG HIỂU) -> CHUYỂN SANG AI ---
    # Thay vì báo lỗi, gửi sang Worker để AI trả lời
    
    # Gửi tin nhắn chờ (để user biết bot đang nghĩ)
    sent_msg = await reply_md(update, "🤖 **Đang suy nghĩ...**")
    
    # Push sang Worker
    payload = {
        "cmd": "CMD_ASK_AI",
        "chat_id": chat_id,
        "question": update.message.text, # Lấy text gốc (có thể có dấu)
        "loading_msg_id": sent_msg.message_id
    }
    await asyncio.to_thread(push_to_worker, payload)


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
        # --- LOGIC VẼ LẠI DASHBOARD (EDIT MODE) ---
        # 1. Check Trial/Admin status
        trial_status = await asyncio.to_thread(check_trial_eligibility, chat_id)
        is_admin = (chat_id == ADMIN_ID)
        show_trial = (trial_status == 'OK') and not is_admin

        # 2. Build Menu
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

        if show_trial:
            kb.append([InlineKeyboardButton("🎁 Kích hoạt Dùng thử (Free)", callback_data="btn_trial_click")])
        
        if is_admin:
            base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
            admin_url = f"{base_url}/admin/dashboard?admin_id={ADMIN_ID}"
            kb.append([InlineKeyboardButton("👑 Admin Dashboard", web_app=WebAppInfo(url=admin_url))])

        kb.append([InlineKeyboardButton("❓ Hướng dẫn", callback_data="menu_help")])

        # 3. Build Text (Chỉ lấy phần Body)
        if show_trial:
             msg_text = (
                "🚀 **Tôi giúp gì cho bạn?**\n"
                "• **Báo tín hiệu:** Cảnh báo giá cổ phiếu và chỉ số realtime.\n"
                "• **Soi danh mục & Định giá:** Phân tích doanh nghiệp trong 5s.\n"
                "• **Sàng lọc:** Tìm cổ phiếu Rẻ/Đắt tự động.\n"
                "• **Báo cáo Tự động:** Gửi bản tin Sáng (7h), Chiều (15h) & Tuần (CN).\n\n"
                "🎁 **Tặng bạn 10 ngày dùng thử Full tính năng Pro!**\n"
                "Bấm nút **'🎁 Kích hoạt Dùng thử'** bên dưới để nhận ngay."
            )
        else:
            msg_text = "👇 *Chọn nhanh tính năng bên dưới:*"

        # 4. Edit Message (Thay vì gửi mới)
        await safe_edit_message(query, msg_text, InlineKeyboardMarkup(kb))

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
        
        # Hiển thị thông báo nhỏ (Toast)
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} VN30F1M!")
        
        # 🔥 GỌI HÀM cmd_setting ĐỂ UPDATE GIAO DIỆN TẠI CHỖ 🔥
        # (Vì update có callback_query nên cmd_setting sẽ tự biết là cần edit message)
        await cmd_setting(update, context) 

    # 3.2. STOCK ALERT (MỚI)
    elif data in ("set_stock_on", "set_stock_off"):
        want_on = (data == "set_stock_on")
        
        await asyncio.to_thread(set_stock_alert_enabled, chat_id, want_on)
        
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} Cảnh báo Stock!")
        
        # 🔥 GỌI HÀM cmd_setting ĐỂ UPDATE GIAO DIỆN TẠI CHỖ 🔥
        await cmd_setting(update, context)

    # 3.3. VNINDEX
    elif data in ("set_vnindex_on", "set_vnindex_off"):
        want_on = (data == "set_vnindex_on")
        
        # Check Pro
        is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
        if want_on and not is_pro:
             await query.answer("⚠️ Chỉ dành cho Gói Pro!", show_alert=True)
             return

        await asyncio.to_thread(set_vnindex_enabled, chat_id, want_on)
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} VNINDEX!")
        await cmd_setting(update, context)

    # 3.4. VN30 Index
    elif data in ("set_vn30_index_on", "set_vn30_index_off"):
        want_on = (data == "set_vn30_index_on")
        
        # Check Pro
        is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
        if want_on and not is_pro:
             await query.answer("⚠️ Chỉ dành cho Gói Pro!", show_alert=True)
             return

        await asyncio.to_thread(set_vn30_enabled, chat_id, want_on)
        await query.answer(f"{'✅ Đã BẬT' if want_on else '🚫 Đã TẮT'} VN30!")
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
        # 1. Báo cho Telegram biết đã nhận lệnh (Tắt vòng quay loading trên nút)
        await query.answer("⏳ Đang lấy dữ liệu...")
        
        # 2. Lấy mã cổ phiếu từ callback_data (vd: btn_info_HPG -> HPG)
        symbol = data.split("_")[2]
        
        # 3. Giả lập tham số context.args để gọi hàm cmd_info
        context.args = [symbol]
        
        # 4. Gọi hàm xử lý chính
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

async def redis_gateway_loop():
    """[GATEWAY] Lắng nghe Redis -> Gửi/Sửa Telegram (có cơ chế tự reconnect)."""
    log.info(f"[{INSTANCE_ID}][GATEWAY] 🎧 Khởi chạy Redis outbound loop trên kênh '{REDIS_CHANNEL_OUTBOUND}'")

    while True:
        pubsub = None
        try:
            r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
            pubsub = r.pubsub()
            pubsub.subscribe(REDIS_CHANNEL_OUTBOUND)

            while True:
                try:
                    message = pubsub.get_message(ignore_subscribe_messages=True)
                except Exception as err:
                    break

                if message:
                    try:
                        raw_data = message['data']
                        payload = json.loads(raw_data)

                        chat_id = payload.get('chat_id')
                        text = payload.get('text')
                        markup_data = payload.get('reply_markup')
                        msg_type = payload.get('msg_type', 'GENERAL')

                        if msg_type == "TASK_UNLOCK":
                            if chat_id:
                                release_task_lock(chat_id)
                            continue

                        if text:
                            text = text.replace("**", "*")
                            text = text.replace("### ", "*").replace("## ", "*")

                        edit_id = payload.get('edit_id')

                        if msg_type == "TRIGGER_CLEANUP":
                            asyncio.create_task(cleanup_after_eod())
                            continue

                        if chat_id and text:
                            if edit_id:
                                log.info(f"[{INSTANCE_ID}][GATEWAY] ✏️ Đang sửa tin {edit_id} cho chat {chat_id}")
                                try:
                                    await tg_app.bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=edit_id,
                                        text=text,
                                        parse_mode="Markdown",
                                        reply_markup=markup_data
                                    )
                                    log.info(f"[{INSTANCE_ID}][GATEWAY] ✅ Đã sửa tin {edit_id}")
                                    continue

                                except Exception as e:
                                    log.warning(f"[{INSTANCE_ID}][GATEWAY] ⚠️ Edit Markdown lỗi: {e}. Thử Plain Text...")
                                    try:
                                        await tg_app.bot.edit_message_text(
                                            chat_id=chat_id,
                                            message_id=edit_id,
                                            text=text.replace("*", "").replace("_", ""),
                                            parse_mode=None,
                                            reply_markup=markup_data
                                        )
                                        log.info(f"[{INSTANCE_ID}][GATEWAY] ✅ Đã sửa tin {edit_id} (Plain Text)")
                                        continue

                                    except Exception as e2:
                                        log.error(f"[{INSTANCE_ID}][GATEWAY] ❌ Edit thất bại hoàn toàn (chat={chat_id}, msg={edit_id}): {e2}")
                                        await delete_message_safely(chat_id, edit_id)

                            if msg_type in ["DIGEST", "EOD_SUMMARY"]:
                                await send_digest_with_pin(tg_app.bot, chat_id, text, reply_markup=markup_data)
                            else:
                                await send_md(
                                    tg_app.bot,
                                    chat_id=chat_id,
                                    text=text,
                                    msg_type=msg_type,
                                    reply_markup=markup_data
                                )

                    except json.JSONDecodeError:
                        log.warning(f"⚠️ [GATEWAY] Lỗi JSON: {message['data']}")
                    except Exception as e:
                        log.error(f"❌ [GATEWAY] Lỗi xử lý: {e}")

                    continue

                await asyncio.sleep(0.05)

        except Exception as e:
            log.error(f"💀 [GATEWAY] Lỗi Redis outbound: {e}. Sẽ thử reconnect sau 5s")
            await asyncio.sleep(5)

        finally:
            if pubsub:
                try:
                    pubsub.close()
                except Exception:
                    pass

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

# /agentlog <macro|biz|tech|all>
async def cmd_agentlog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command để xem nhanh dữ liệu agent lưu trên Redis."""
    if ADMIN_ID is None or not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    chat_id = update.effective_chat.id if update.effective_chat else user_id

    try:
        await asyncio.to_thread(log_command_usage, chat_id, "/agentlog", ADMIN_ID)
    except Exception as exc:
        log.warning(f"/agentlog log error: {exc}")

    scope = (context.args[0].lower() if context.args else "all")
    if scope not in VALID_AGENT_SCOPES:
        valid_text = ", ".join(sorted(VALID_AGENT_SCOPES))
        await reply_md(update, f"⚠️ Cú pháp: `/agentlog <{valid_text}>`")
        return

    # Thử lấy dữ liệu từ Redis
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:
        log.error(f"/agentlog Redis error: {exc}")
        await reply_md(update, "⚠️ Không kết nối được Redis.")
        return

    def _load_agent(agent_type: str):
        key = _agent_result_key(agent_type)
        raw = r.get(key)
        if not raw:
            return key, None
        try:
            return key, json.loads(raw)
        except json.JSONDecodeError:
            return key, {"error": "JSON lỗi", "raw": raw}

    lines = ["🗂 *Agent Cache Snapshot*"]

    if scope == "all" or scope == "bundle":
        bundle_key = _agent_bundle_key(chat_id)
        bundle_raw = r.get(bundle_key)
        if bundle_raw:
            try:
                bundle_data = json.loads(bundle_raw)
            except json.JSONDecodeError:
                bundle_data = {"error": "JSON lỗi", "raw": bundle_raw}
        else:
            bundle_data = None

        lines.append("")
        lines.append(f"*BUNDLE* key `{bundle_key}`")
        if bundle_data:
            request_id = bundle_data.get("request_id", "?")
            generated_at = bundle_data.get("generated_at", "?")
            scope_saved = bundle_data.get("scope", "?")
            lines.append(f"• Request: `{request_id}` | Scope: `{scope_saved}`")
            lines.append(f"• Generated at: {generated_at}")
            lines.append(f"• AI Summary: {bundle_data.get('ai_summary', '—')}")
        else:
            lines.append("• Không tìm thấy dữ liệu bundle cho chat này.")

    target_agents = [scope] if scope in ("macro", "biz", "tech") else ["macro", "biz", "tech"]

    for agent_type in target_agents:
        key, agent_data = _load_agent(agent_type)
        lines.append("")
        lines.append(f"*{agent_type.upper()}* key `{key}`")
        if not agent_data:
            lines.append("• Không tìm thấy dữ liệu.")
            continue
        request_id = agent_data.get("request_id", "?")
        generated_at = agent_data.get("generated_at", "?")
        notes = agent_data.get("notes", "—")
        insights = agent_data.get("insights")
        insight_count = len(insights) if isinstance(insights, list) else 0
        lines.append(f"• Request: `{request_id}` | Generated at: {generated_at}")
        lines.append(f"• Insights: {insight_count} mục")
        lines.append(f"• Notes: {notes}")

    await reply_md(update, "\n".join(lines))

# /agent <macro|biz|tech|all>
async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger multi-agent crawl via command (admin only)."""
    if ADMIN_ID is None or not update.effective_user:
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    chat_id = update.effective_chat.id if update.effective_chat else user_id

    # Log command usage for audit
    try:
        await asyncio.to_thread(log_command_usage, chat_id, "/agent", ADMIN_ID)
    except Exception as exc:
        log.warning(f"/agent log error: {exc}")

    scope = (context.args[0].lower() if context.args else "all")
    if scope not in VALID_AGENT_SCOPES:
        valid_text = ", ".join(sorted(VALID_AGENT_SCOPES))
        await reply_md(update, f"⚠️ Cú pháp: `/agent <{valid_text}>`")
        return

    request_id = str(uuid.uuid4())[:8]
    payload = {
        "cmd": "CMD_AGENT_RUN",
        "chat_id": chat_id,
        "scope": scope,
        "request_id": request_id,
    }

    await asyncio.to_thread(push_to_worker, payload)

    scope_label = scope.upper()
    await reply_md(
        update,
        f"🚀 Đang khởi chạy agent *{scope_label}*\n"
        f"🆔 Request: `{request_id}`\n"
        "⏳ Worker sẽ phản hồi khi hoàn tất.",
    )

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
    
    # Nếu là Admin -> Thêm nút Admin Dashboard
    if is_admin:
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        admin_url = f"{base_url}/admin/dashboard?admin_id={ADMIN_ID}"
        kb.append([InlineKeyboardButton("👑 Admin Dashboard", web_app=WebAppInfo(url=admin_url))])

    # Thêm nút Hướng dẫn cuối cùng
    kb.append([InlineKeyboardButton("❓ Hướng dẫn", callback_data="menu_help")])

    # [MỚI] Define Reply Keyboard (Nút Dashboard cố định)
    reply_kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🏠 Dashboard")]], 
        resize_keyboard=True, 
        is_persistent=True
    )

    # --- NỘI DUNG TIN NHẮN (Tách làm 2 phần để gửi kèm Reply Keyboard) ---
    if show_trial:
        # Dành cho User Mới (Chưa dùng thử) -> Có chào mời
        header_msg = (
            "👋 *Chào bạn! Mình là Người Canh Bảng 🧑‍💻*\n"
            "Trợ lý đầu tư chứng khoán thông minh 24/7."
        )
        body_msg = (
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
        header_msg = (
            "⚖️ *Người Canh Bảng* 🧑‍💻 _là công cụ hỗ trợ dữ liệu. Mọi thông tin chỉ mang tính tham khảo, nhà đầu tư tự chịu trách nhiệm với quyết định của mình._"
        )
        body_msg = "👇 *Chọn nhanh tính năng bên dưới:*"

    # 1. Gửi Header kèm Reply Keyboard (Nút Dashboard dưới cùng)
    await reply_md(update, header_msg, reply_markup=reply_kb)

    # 2. Gửi Body kèm Inline Keyboard (Menu chính)
    await reply_md(update, body_msg, reply_markup=InlineKeyboardMarkup(kb))

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


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not BOT_ACTIVE:
        await reply_md(update, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id if update and update.effective_chat else None
    if not chat_id:
        return

    await track_user_activity(update)
    await asyncio.to_thread(log_command_usage, chat_id, "/alert", ADMIN_ID)

    if not context.args:
        await reply_md(update, "⚠️ Cách dùng: /alert <MÃ> (ví dụ: /alert HPG)")
        return

    raw_symbol = context.args[0].strip().upper()
    cleaned = re.sub(r"[^A-Z0-9]", "", raw_symbol)
    if not cleaned:
        await reply_md(update, "⚠️ Mã không hợp lệ. Vui lòng nhập như: /alert HPG")
        return

    payload = {
        "cmd": "CMD_MANUAL_ALERT",
        "chat_id": chat_id,
        "symbols": [cleaned],
    }

    await reply_md(update, f"🔔 Đang kiểm tra *{cleaned}*. Bot sẽ báo ngay khi có dữ liệu.")
    await asyncio.to_thread(push_to_worker, payload)


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
            asyncio.to_thread(get_vnindex_enabled_map),
            asyncio.to_thread(get_vn30_enabled_map),
            return_exceptions=True
        )
        
        expiry_date = results[0] if not isinstance(results[0], Exception) else None
        vn30f1m_map = results[1] if not isinstance(results[1], Exception) else {}
        stock_map = results[2] if not isinstance(results[2], Exception) else {}
        vnindex_map = results[3] if not isinstance(results[3], Exception) else {}
        vn30_index_map = results[4] if not isinstance(results[4], Exception) else {}
        
        vn30f1m_enabled = bool(vn30f1m_map.get(chat_id, False))
        stock_enabled = bool(stock_map.get(chat_id, True))
        vnindex_enabled = bool(vnindex_map.get(chat_id, False))
        vn30_index_enabled = bool(vn30_index_map.get(chat_id, False))

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
    status_vn30f1m = "✅ *BẬT*" if vn30f1m_enabled else "❌ *TẮT*"
    lines.append(status_vn30f1m)

    # VNINDEX
    lines.append("\n📉 *Cảnh báo VNINDEX*")
    status_vnindex = "✅ *BẬT*" if vnindex_enabled else "❌ *TẮT*"
    lines.append(status_vnindex)

    # VN30
    lines.append("\n📉 *Cảnh báo VN30*")
    status_vn30_index = "✅ *BẬT*" if vn30_index_enabled else "❌ *TẮT*"
    lines.append(status_vn30_index)

    # --- 3. TẠO BÀN PHÍM ĐIỀU KHIỂN ---
    
    vn30f1m_btn = "🔴 Tắt VN30F1M" if vn30f1m_enabled else "🟢 Bật VN30F1M"
    vn30f1m_cb = "set_vn30_off" if vn30f1m_enabled else "set_vn30_on"

    stock_btn = "🔴 Tắt Stock" if stock_enabled else "🟢 Bật Stock"
    stock_cb = "set_stock_off" if stock_enabled else "set_stock_on"

    vnindex_btn = "🔴 Tắt VNINDEX" if vnindex_enabled else "🟢 Bật VNINDEX"
    vnindex_cb = "set_vnindex_off" if vnindex_enabled else "set_vnindex_on"

    vn30_index_btn = "🔴 Tắt VN30" if vn30_index_enabled else "🟢 Bật VN30"
    vn30_index_cb = "set_vn30_index_off" if vn30_index_enabled else "set_vn30_index_on"

    kb = [
        [InlineKeyboardButton("💎 Nâng cấp / Gia hạn Pro", callback_data="btn_upgrade")],
        [InlineKeyboardButton(stock_btn, callback_data=stock_cb), InlineKeyboardButton(vn30f1m_btn, callback_data=vn30f1m_cb)], 
        [InlineKeyboardButton(vnindex_btn, callback_data=vnindex_cb), InlineKeyboardButton(vn30_index_btn, callback_data=vn30_index_cb)],   
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



@task_locked
async def cmd_screener_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Mở WebApp Screener để lọc cổ phiếu theo ngành và upside.
    """
    if not BOT_ACTIVE:
        await send_md(context.bot, update.effective_chat.id, "⚙️ Bot đang bảo trì.")
        return

    chat_id = update.effective_chat.id
    
    # 1. Xác định Base URL (Ưu tiên Render -> Ngrok)
    base_url = os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        base_url = os.getenv("NGROK_URL")
    
    if not base_url:
        await send_md(context.bot, chat_id, "⚠️ Server chưa cấu hình URL (Render/Ngrok). Không thể mở WebApp.")
        return

    # Xử lý trailing slash
    if base_url.endswith("/"):
        base_url = base_url[:-1]
        
    webapp_url = f"{base_url}/screener/value"

    # 2. Tạo nút mở WebApp
    kb = [
        [InlineKeyboardButton("🚀 Mở Bộ Lọc Cổ Phiếu", web_app=WebAppInfo(url=webapp_url))]
    ]
    reply_markup = InlineKeyboardMarkup(kb)

    # 3. Gửi tin nhắn
    await send_md(
        context.bot, 
        chat_id, 
        "🔍 *Bộ Lọc Cổ Phiếu (Screener)*\n\n"
        "Bấm nút bên dưới để mở công cụ lọc cổ phiếu theo ngành và biên an toàn (Upside).",
        reply_markup=reply_markup
    )





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



# ==============================================
# COMMAND: /report (CÓ CACHE REDIS + RETRY, KHÔNG COOLDOWN)
# Cache nội dung report theo danh mục vào Redis (theo cache_key = danh mục chuẩn hoá)
# ==============================================

@task_locked(manual_release=True)
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (UX PRO) AI Report với thanh tiến trình (Progress Bar).
    """
    if not update or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    lock_should_persist = False

    try:
        if not BOT_ACTIVE:
            await reply_md(update, "⚙️ Bot đang bảo trì.")
            return

        vn_tz = pytz.timezone(TIMEZONE)

        # 1. Xác định trạng thái Pro
        is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
        base_url = os.getenv("RENDER_EXTERNAL_URL") or "https://google.com"
        watch = await asyncio.to_thread(get_watch_list_for_chat, chat_id)
        symbols = [s.upper() for s in (watch or []) if not s.upper().startswith("VN")]
        cache_key = make_report_cache_key(symbols) if symbols else "EMPTY"
        web_app_url = f"{base_url}/report/view/{cache_key}?chat_id={chat_id}"

        # --- NHÁNH 1: FREE USER (GIỮ NGUYÊN) ---
        if not is_pro:
            try:
                await asyncio.to_thread(log_command_usage, chat_id, "/report (Free)", ADMIN_ID)
            except Exception:
                pass
            kb = [[InlineKeyboardButton("📊 Xem Báo Cáo AI", web_app=WebAppInfo(url=web_app_url))],
                  [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
            await reply_md(
                update,
                "📊 **Báo Cáo Danh Mục AI**\n\nAI sẽ phân tích chuyên sâu sức khỏe danh mục.\n👇 Nhấn nút bên dưới để xem chi tiết.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        # --- NHÁNH 2: PRO USER (CÓ PROGRESS BAR) ---
        await asyncio.to_thread(log_command_usage, chat_id, "/report", ADMIN_ID)
        if not symbols:
            await reply_md(update, "📭 Danh mục trống. Hãy dùng `/add` để thêm mã trước nhé!")
            return

        # 2. Check Cache (Gateway vẫn check cache được để phản hồi nhanh)
        cache_key = make_report_cache_key(symbols)
        cached = await asyncio.to_thread(get_report_from_redis, cache_key)

        if cached and not cached[2]:  # Not error
            text_json, generated_at, _, _ = cached
            time_str = "vừa xong"
            if generated_at:
                try:
                    time_str = generated_at.astimezone(vn_tz).strftime("%H:%M %d/%m")
                except Exception:
                    pass
            kb = [[InlineKeyboardButton("📊 Xem Báo Cáo Chi Tiết", web_app=WebAppInfo(url=web_app_url))],
                  [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
            await reply_md(
                update,
                f"✅ Báo cáo danh mục *{', '.join(symbols)}* đã sẵn sàng (bản lưu lúc {time_str}).",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        # B. Cache Miss -> CHẠY TIẾN TRÌNH

        progress_msg = await reply_md(
            update,
            f"⏳ **Khởi động AI Analyst...**\n"
            f"`[{make_progress_bar(10)}] 10%`"
        )

        try:
            await asyncio.sleep(0.5)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"📥 **Đang tải dữ liệu thị trường...**\n`[{make_progress_bar(35)}] 35%`",
                parse_mode="Markdown"
            )

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"⏳ **Đang gửi yêu cầu cho AI...**\n`[{make_progress_bar(60)}] 60%`",
                parse_mode="Markdown"
            )

            sent_msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"✅ **Bot sẽ gửi báo cáo ngay khi xong (khoảng 30s)...**\n`[{make_progress_bar(70)}] 70%`",
                parse_mode="Markdown"
            )
            await asyncio.sleep(0.5)

            payload = {
                "cmd": "GEN_REPORT",
                "chat_id": chat_id,
                "symbols": symbols,
                "loading_msg_id": sent_msg.message_id
            }
            await asyncio.to_thread(push_to_worker, payload)
            lock_should_persist = True

        except Exception as e:
            log.error(f"Lỗi /report: {e}")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    text=f"⚠️ **Lỗi xử lý:** Hệ thống đang bận.\nVui lòng thử lại sau.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    finally:
        if not lock_should_persist:
            release_task_lock(chat_id)



# ==============================================
# COMMAND: /info <MÃ> (HỒ SƠ DOANH NGHIỆP)
# ==============================================
@task_locked(manual_release=True)
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (Gateway) Nhận lệnh /info -> Check Cache -> Nếu thiếu thì gọi Worker.
    """
    if not update or not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    lock_should_persist = False

    try:
        if not BOT_ACTIVE:
            await reply_md(update, "⚙️ Bot đang bảo trì.")
            return

        if not context.args:
            kb = [[InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
            await reply_md(update, "⚠️ Cách dùng: `/info <MÃ>` (VD: `/info FPT`)", reply_markup=InlineKeyboardMarkup(kb))
            return

        symbol = context.args[0].strip().upper()

        is_pro = await asyncio.to_thread(is_user_pro, chat_id) or (chat_id == ADMIN_ID)
        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://google.com")
        web_app_url = f"{base_url}/info/{symbol}?chat_id={chat_id}"

        if not is_pro:
            try:
                await asyncio.to_thread(log_command_usage, chat_id, f"/info {symbol} (Free)", ADMIN_ID)
            except Exception:
                pass
            kb = [[InlineKeyboardButton(f"📄 Mở Hồ Sơ {symbol}", web_app=WebAppInfo(url=web_app_url))],
                  [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]]
            await reply_md(
                update,
                f"🏢 **Hồ Sơ Doanh Nghiệp: {symbol}**\n\nPhân tích mô hình kinh doanh & vị thế.\n👇 Nhấn nút bên dưới để xem.",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        try:
            await asyncio.to_thread(log_command_usage, chat_id, f"/info {symbol}", ADMIN_ID)
        except Exception:
            pass

        cache_key = make_profile_cache_key(symbol)
        cached = await asyncio.to_thread(get_profile_from_redis, cache_key)

        if cached:
            text, _, is_error, _ = cached
            if not is_error:
                kb = [
                    [InlineKeyboardButton(f"📄 Mở Hồ Sơ {symbol}", web_app=WebAppInfo(url=web_app_url))],
                    [InlineKeyboardButton("❌ Đóng", callback_data="close_msg")]
                ]
                await reply_md(update, f"✅ Hồ sơ *{symbol}* đã sẵn sàng.", reply_markup=InlineKeyboardMarkup(kb))
                return

        progress_msg = await reply_md(
            update,
            f"⏳ **Đang truy xuất dữ liệu {symbol}...**\nBot đang đọc BCTC và tổng hợp tin tức.\n"
            f"`[{make_progress_bar(10)}] 10%`"
        )

        try:
            await asyncio.sleep(0.5)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"📥 **Đang tải dữ liệu thị trường...**\n`[{make_progress_bar(35)}] 35%`",
                parse_mode="Markdown"
            )

            await asyncio.sleep(0.5)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"⏳ **Đang gửi yêu cầu cho AI...**\n`[{make_progress_bar(60)}] 60%`",
                parse_mode="Markdown"
            )

            await asyncio.sleep(0.5)
            sent_msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                text=f"✅ **Bot sẽ gửi báo cáo ngay khi xong (khoảng 30s)...**\n`[{make_progress_bar(70)}] 70%`",
                parse_mode="Markdown"
            )

            payload = {
                "cmd": "GEN_INFO",
                "chat_id": chat_id,
                "symbol": symbol,
                "loading_msg_id": sent_msg.message_id
            }
            await asyncio.to_thread(push_to_worker, payload)
            lock_should_persist = True

        except Exception as e:
            log.error(f"Lỗi /report: {e}")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    text=f"⚠️ **Lỗi xử lý:** Hệ thống đang bận.\nVui lòng thử lại sau.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    finally:
        if not lock_should_persist:
            release_task_lock(chat_id)


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

# --- HELPER CHO SCREENER WEBAPP ---
# Các mã cần loại khỏi Screener (yêu cầu business)
EXCLUDED_TICKERS = {"VIC", "VRE", "VHM"}

def get_screener_data_for_webapp():
    """
    Lấy dữ liệu screener.
    Cố gắng lấy từ cache 'global_screener_snapshot' (do Worker hoặc request trước tạo).
    Nếu không có, tự tính toán (fallback) và cache lại 5 phút.
    """
    try:
        r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        
        # 1. Thử lấy từ Cache
        cached = r.get("global_screener_snapshot")
        if cached:
            return json.loads(cached)

        # 2. Nếu không có, tính toán (Fallback)
        # Lấy dữ liệu lịch sử
        hist_payload = get_historical_valuation_from_redis()
        if not hist_payload:
            return {"data": []}
            
        # [FIX] Extract 'stocks' from payload
        hist_data = hist_payload.get("stocks", {})
        if not hist_data:
            return {"data": []}


        # Lấy dữ liệu thị trường (Sync call)
        screener_df = Screener().stock(params={"exchangeName": "HOSE,HNX"}, limit=1700)
        
        # Lấy thông tin ngành
        sectors_map = {}
        try:
            with open("sectors.json", "r", encoding="utf-8") as f:
                sectors_map = json.load(f)
        except: pass

        result_data = []
        for index, row in screener_df.iterrows():
            sym = str(row['ticker']).upper()
            if sym in EXCLUDED_TICKERS: continue
            if sym not in hist_data: continue
            
            try:
                pe_cur = float(row['pe'])
                pb_cur = float(row['pb'])
                # Giá đóng cửa (đơn vị nghìn đồng)
                # Ưu tiên: close -> price -> price_near_realtime
                p_close = row.get('close', 0)
                p_price = row.get('price', 0)
                p_realtime = row.get('price_near_realtime', 0)
                
                current_price = 0.0
                
                try:
                    if p_close and not math.isnan(float(p_close)) and float(p_close) > 0:
                        current_price = float(p_close) * 1000
                    elif p_price and not math.isnan(float(p_price)) and float(p_price) > 0:
                        current_price = float(p_price) * 1000
                    elif p_realtime and not math.isnan(float(p_realtime)) and float(p_realtime) > 0:
                        current_price = float(p_realtime) * 1000
                except:
                    current_price = 0.0
            except: continue

            pe_avg = hist_data[sym].get('pe_avg', 0)
            pb_avg = hist_data[sym].get('pb_avg', 0)
            
            # Validate Data (Chặn NaN/Inf để tránh lỗi JSON trên iPhone)
            if math.isnan(pe_cur) or pe_cur <= 0: continue
            if math.isnan(pb_cur) or pb_cur <= 0: continue
            if math.isnan(pe_avg) or pe_avg <= 0: continue
            if math.isnan(pb_avg) or pb_avg <= 0: continue

            # Tính Upside (Biên an toàn)
            # Upside = (Fair - Current) / Current
            # Fair Value ước tính theo Mean Reversion
            upside_pe = (pe_avg / pe_cur) - 1
            upside_pb = (pb_avg / pb_cur) - 1
            
            avg_upside = (upside_pe + upside_pb) / 2
            
            # Fair Value (Display purpose)
            fair_value = current_price * (1 + avg_upside)

            # Discount logic for frontend (Negative = Undervalued/Green)
            discount_pct = -avg_upside * 100 

            # Signal
            if avg_upside >= 0.15:
                signal = "Undervalued"
            elif avg_upside <= -0.15:
                signal = "Overvalued"
            else:
                signal = "Fair"

            sector_info = sectors_map.get(sym, "Khác")
            if isinstance(sector_info, dict):
                sector = sector_info.get("sector", "Khác")
            else:
                sector = sector_info if isinstance(sector_info, str) else "Khác"
            
            # Helper sanitize final values
            def _s(v):
                if v is None: return 0.0
                if isinstance(v, float):
                    if math.isnan(v) or math.isinf(v): return 0.0
                return v

            result_data.append({
                "symbol": str(sym),
                "sector": str(sector) if sector else "Khác",
                "price": _s(current_price),
                "fair": _s(fair_value),
                "discount": _s(round(discount_pct, 2)),
                "signal": str(signal),
                "pe": _s(pe_cur),
                "pe_avg": _s(pe_avg),
                "pb": _s(pb_cur),
                "pb_avg": _s(pb_avg)
            })
            
        payload = {"data": result_data}
        
        # Cache 5 phút (Ensure NO NaN)
        try:
            json_str = json.dumps(payload, allow_nan=False)
            r.set("global_screener_snapshot", json_str, ex=300)
        except ValueError as e:
            log.error(f"FATAL: Generated JSON contains NaN! {e}")
            # Fallback: Clean recursively if needed, but _s should have caught it.
        
        return payload

    except Exception as e:
        log.error(f"Lỗi get_screener_data_for_webapp: {e}")
        return {"data": []}

@flask_app.route("/screener/value")
def view_screener_webapp():
    """Route hiển thị Web App Screener"""
    try:
        data_obj = get_screener_data_for_webapp()
        items = data_obj.get("data", [])
        
        # [NEW] Generate Sector Chart
        sector_chart = None
        sector_table = None
        try:
            hist_payload = get_historical_valuation_from_redis()
            if hist_payload and "sectors" in hist_payload:
                sector_chart = draw_sector_performance_chart(hist_payload["sectors"], '12w')
                sector_table = generate_sector_table_html(hist_payload["sectors"])
        except Exception as e:
            log.error(f"Chart Error: {e}")
        
        vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
        generated_time = datetime.datetime.now(vn_tz).strftime("%H:%M %d/%m/%Y")
        
        return render_template_string(
            SCREENER_WEBAPP_TEMPLATE, 
            items=items, 
            generated_time=generated_time,
            sector_chart=sector_chart,
            sector_table=sector_table
        )
    except Exception as e:
        log.error(f"View Screener Error: {e}")
        return "Lỗi tải dữ liệu.", 500

@flask_app.route("/api/screener-data")
def api_screener_data():
    """API trả về dữ liệu JSON cho Screener WebApp"""
    data = get_screener_data_for_webapp()
    # Force standard JSON dump to avoid NaN issues on Safari
    try:
        return flask_app.response_class(
            response=json.dumps(data, ensure_ascii=False, allow_nan=False),
            mimetype='application/json'
        )
    except ValueError:
        # If data still has NaN, return empty to prevent crash
        return jsonify({"data": []})

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
            generated_time=data['generated_time'],
            sector_chart=data.get('sector_chart', '')
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
# 🛠️ NEW ADMIN API ENDPOINTS (SYSTEM CONTROL)
# ==============================================

@flask_app.route("/api/admin/system/status", methods=["GET", "POST"])
async def api_admin_system_status():
    """Lấy hoặc Cập nhật trạng thái Bot (ON/OFF)"""
    global BOT_ACTIVE
    
    # GET: Lấy trạng thái hiện tại
    if request.method == "GET":
        req_admin_id = request.args.get("admin_id")
        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"error": "Unauthorized"}), 403
            
        # Lấy thêm thông tin Redis Keys
        redis_keys = 0
        try:
            r = get_redis()
            redis_keys = await asyncio.to_thread(r.dbsize)
        except: pass

        # Lấy thông tin CPU & RAM
        cpu_usage = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        ram_usage = ram.percent
            
        return jsonify({
            "active": BOT_ACTIVE,
            "redis_keys": redis_keys,
            "cpu": cpu_usage,
            "ram": ram_usage
        })

    # POST: Cập nhật trạng thái
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        want_active = data.get("active") # True/False

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        BOT_ACTIVE = want_active
        await asyncio.to_thread(set_bot_active, want_active)
        
        log.info(f"[ADMIN_API] System Status changed to: {BOT_ACTIVE}")
        return jsonify({"ok": True, "active": BOT_ACTIVE})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/system/broadcast", methods=["POST"])
async def api_admin_broadcast():
    """Gửi thông báo tới toàn bộ user"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        text = data.get("text")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403
        
        if not text:
            return jsonify({"ok": False, "message": "Nội dung trống"}), 400

        # Gọi hàm broadcast (chạy background task để không block request)
        # Lưu ý: broadcast_to_all_watchers là async, cần wrap vào task
        if tg_app and MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(
                broadcast_to_all_watchers(text, target_audience='all'),
                MAIN_LOOP
            )
            return jsonify({"ok": True, "message": "Đang gửi broadcast..."})
        
        return jsonify({"ok": False, "message": "Bot not ready"}), 500

    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/cache/clear", methods=["POST"])
async def api_admin_clear_cache():
    """Xóa Cache (Screener, Report, Info)"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        cache_type = data.get("type") # 'screener', 'report', 'info', 'all'

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        r = get_redis()
        deleted = 0
        
        if cache_type == 'all':
            r.flushdb()
            msg = "Đã xóa toàn bộ dữ liệu Redis (flushdb)."

        elif cache_type == 'screener':
            # Logic cũ: push lệnh sang worker để tính lại, hoặc xóa key
            # Ở đây ta xóa key snapshot để lần tới nó tự tính lại
            r.delete("global_screener_snapshot")
            # Gửi lệnh force update sang worker
            payload = {"cmd": "FORCE_SCREENER", "admin_id": ADMIN_ID}
            await asyncio.to_thread(push_to_worker, payload)
            msg = "Đã xóa cache Screener & Trigger Worker update."

        elif cache_type == 'report':
            # Quét xóa report_cache:*
            for key in r.scan_iter(match="report_cache:*"):
                r.delete(key)
                deleted += 1
            msg = f"Đã xóa {deleted} key Report Cache."

        elif cache_type == 'info':
            # Quét xóa profile_cache:*
            for key in r.scan_iter(match="profile_cache:*"):
                r.delete(key)
                deleted += 1
            msg = f"Đã xóa {deleted} key Profile Cache."
        
        else:
            return jsonify({"ok": False, "message": "Unknown type"}), 400

        return jsonify({"ok": True, "message": msg})

    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/worker/force", methods=["POST"])
async def api_admin_force_worker():
    """Force chạy tác vụ Worker (Weekly Report, etc.)"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        task_type = data.get("type") # 'weekly'

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        if task_type == 'weekly':
            payload = {"cmd": "RUN_WEEKLY_NOW", "admin_id": ADMIN_ID}
            await asyncio.to_thread(push_to_worker, payload)
            return jsonify({"ok": True, "message": "Đã gửi lệnh chạy Weekly Report."})
        
        elif task_type == 'nightly_valuation':
            payload = {"cmd": "RUN_NIGHTLY_VALUATION", "admin_id": ADMIN_ID}
            await asyncio.to_thread(push_to_worker, payload)
            return jsonify({"ok": True, "message": "Đã gửi lệnh chạy Nightly Valuation."})
        
        elif task_type == 'daily_digest':
            payload = {"cmd": "RUN_DAILY_DIGEST", "admin_id": ADMIN_ID}
            await asyncio.to_thread(push_to_worker, payload)
            return jsonify({"ok": True, "message": "Đã gửi lệnh chạy Daily Digest."})
        
        return jsonify({"ok": False, "message": "Unknown task"}), 400

    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/system/backup", methods=["POST"])
async def api_admin_backup():
    """Trigger Backup Core và gửi file về Telegram Admin"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        # Tái sử dụng logic của cmd_backup_core nhưng chạy background
        async def _do_backup():
            try:
                payload = await asyncio.to_thread(export_core_data)
                vn_tz = pytz.timezone(TIMEZONE)
                now = datetime.datetime.now(vn_tz)
                ts = now.strftime("%Y%m%d_%H%M%S")
                filename = f"stockbot_core_backup_{ts}.json"
                tmp_path = os.path.join(TMP_DIR, filename)

                def json_datetime_converter(o):
                    if isinstance(o, (datetime.date, datetime.datetime)):
                        return o.isoformat()
                    raise TypeError(f"Type {o} not serializable")

                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2, default=json_datetime_converter)

                await tg_app.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=open(tmp_path, "rb"),
                    filename=filename,
                    caption=f"📦 Backup System Triggered from Dashboard."
                )
                os.remove(tmp_path)
            except Exception as e:
                log.error(f"Backup Error: {e}")
                await send_md(tg_app.bot, ADMIN_ID, f"⚠️ Backup thất bại: {e}")

        if tg_app and MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(_do_backup(), MAIN_LOOP)
            return jsonify({"ok": True, "message": "Đang tạo backup và gửi về Telegram..."})
        
        return jsonify({"ok": False, "message": "Bot not ready"}), 500

    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@flask_app.route("/api/admin/system/delete_range", methods=["POST"])
async def api_admin_delete_range():
    """Xóa tin nhắn theo khoảng thời gian"""
    try:
        data = request.get_json()
        req_admin_id = data.get("admin_id")
        start_str = data.get("start") # "YYYY-MM-DD HH:MM"
        end_str = data.get("end")     # "YYYY-MM-DD HH:MM"

        if not req_admin_id or int(req_admin_id) != ADMIN_ID:
            return jsonify({"ok": False, "message": "Unauthorized"}), 403

        vn_tz = pytz.timezone(TIMEZONE)
        start_time = vn_tz.localize(datetime.datetime.strptime(start_str, "%Y-%m-%d %H:%M"))
        end_time = vn_tz.localize(datetime.datetime.strptime(end_str, "%Y-%m-%d %H:%M"))

        # Chạy background task để xóa
        async def _do_delete():
            try:
                records = await asyncio.to_thread(get_bot_messages_in_range, start_time, end_time)
                if not records:
                    await send_md(tg_app.bot, ADMIN_ID, "📭 Không có tin nhắn nào để xóa trong khoảng đã chọn.")
                    return

                deleted = 0
                skipped = 0
                now = datetime.datetime.now(vn_tz)

                for chat_id, msg_id, _sent_at in records:
                    try:
                        sent_at_vn = _sent_at.astimezone(vn_tz) if _sent_at.tzinfo else _sent_at.replace(tzinfo=pytz.UTC).astimezone(vn_tz)
                        if (now - sent_at_vn).total_seconds() > 48*3600:
                            skipped += 1
                            continue
                        
                        # Gọi API xóa
                        try:
                            await tg_app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                            deleted += 1
                        except: pass
                        
                        await asyncio.sleep(0.05) # Rate limit nhẹ
                    except: pass
                
                # Xóa DB
                await asyncio.to_thread(delete_bot_messages_in_range, start_time, end_time)
                
                await send_md(tg_app.bot, ADMIN_ID, f"✅ Đã xóa {deleted} tin nhắn.\n(Bỏ qua {skipped} tin cũ > 48h).")

            except Exception as e:
                log.error(f"Delete Range Error: {e}")

        if tg_app and MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(_do_delete(), MAIN_LOOP)
            return jsonify({"ok": True, "message": "Tiến trình xóa đang chạy ngầm..."})

        return jsonify({"ok": False, "message": "Bot not ready"}), 500

    except Exception as e:
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
                    MAIN_LOOP.create_task(redis_gateway_loop()),
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
            ("help", "📘 Hướng dẫn nhanh"),
            ("alert", "(admin) Báo nhanh biến động 1 mã /alert hpg"),
            ("admin", "(admin) Mở Dashboard Admin"),
            ("restore_core", "(admin) Khôi phục dữ liệu core từ file backup"),
            ("agent", "(admin) macro|biz|tech|all - Kích hoạt Agent chuyên dụng"),
            ("agentlog", "(admin) macro|biz|tech|all - Xem log hoạt động Agent"),
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

# --- USER COMMANDS (Giao diện chính) ---
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("help", cmd_help))
    
    # Quản lý danh mục
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("remove", cmd_remove))
    tg_app.add_handler(CommandHandler("alert", cmd_alert))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    
    # Tính năng AI & Dữ liệu (Gateway gọi Worker)
    tg_app.add_handler(CommandHandler("report", cmd_report))
    tg_app.add_handler(CommandHandler("info", cmd_info))
    tg_app.add_handler(CommandHandler("screener_value", cmd_screener_value))
    
    # Tài khoản & Cài đặt
    tg_app.add_handler(CommandHandler("setting", cmd_setting))
    tg_app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    tg_app.add_handler(CommandHandler("trial", cmd_trial))
    tg_app.add_handler(CommandHandler("agent", cmd_agent))
    tg_app.add_handler(CommandHandler("agentlog", cmd_agentlog))

    # --- ADMIN COMMANDS (Quản trị viên) ---
    # 1. Hệ thống
    tg_app.add_handler(CommandHandler("admin", cmd_admin)) # Web Dashboard
    tg_app.add_handler(CommandHandler("restore_core", cmd_restore_core))

    # Handlers khác
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
