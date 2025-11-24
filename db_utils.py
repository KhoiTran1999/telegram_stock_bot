# db_utils.py
import os
import json
import datetime
import time
import pytz

from psycopg import rows
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv
load_dotenv()

from news_seen_cache import (
    has_news_seen_redis,
    mark_news_seen_redis,
    get_news_seen_count_redis,
    canonicalize_link,
)
from redis_client import get_redis
import hashlib
import uuid

REDIS_DEBUG = os.getenv("REDIS_DEBUG", "False").lower() in ("1", "true", "yes")

def redis_debug_log(message: str):
    """In log nhẹ khi REDIS_DEBUG bật."""
    if REDIS_DEBUG:
        print(f"[CACHE] {message}")


# Lấy DATABASE_URL từ biến môi trường Render
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# Tạo connection pool dùng chung cho toàn bộ service
# min_size: số connection tối thiểu
# max_size: số connection tối đa (tùy gói, Render free nên để 5–10 là ổn)
POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=5,
    timeout=30,      # tối đa 30s chờ lấy connection trong pool
)

def get_conn():
    # Lấy connection từ pool (tái sử dụng, không mở/đóng liên tục nữa)
    return POOL.connection()

def init_db():
    """Tạo bảng nếu chưa có."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Bảng lưu thông tin chi tiết user (để hiển thị trên Admin Dashboard)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    joined_at TIMESTAMPTZ DEFAULT NOW(),
                    last_active_at TIMESTAMPTZ DEFAULT NOW(),
                    admin_note TEXT,
                    is_banned BOOLEAN DEFAULT FALSE,
                    has_used_trial BOOLEAN DEFAULT FALSE
                )
            """)

            # Bảng lưu danh sách mã theo dõi của từng user
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_watch (
                    chat_id BIGINT PRIMARY KEY,
                    watch_list JSONB NOT NULL DEFAULT '[]'
                )
            """)

            # Bảng lưu cấu hình chung (ví dụ: trạng thái bảo trì)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value JSONB
                )
            """)

            # Log lệnh user (dùng cho thống kê /status)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS command_log (
                    id      SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    command TEXT   NOT NULL,
                    used_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)

            # Cache screener Value
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_value_cache (
                    symbol   TEXT PRIMARY KEY,
                    exchange TEXT,
                    industry TEXT,
                    pe       DOUBLE PRECISION,
                    pb       DOUBLE PRECISION,
                    roe      DOUBLE PRECISION,
                    floor    TEXT,
                    asset_proxy     DOUBLE PRECISION,
                    liquidity_proxy DOUBLE PRECISION,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Log tin nhắn bot đã gửi (để xóa theo range)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_msg_log (
                    id         SERIAL PRIMARY KEY,
                    chat_id    BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    sent_at    TIMESTAMP NOT NULL DEFAULT NOW(),
                    msg_type   TEXT DEFAULT 'GENERAL'  
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_msg_log_cleanup 
                ON bot_msg_log(msg_type, sent_at);
            """)

            # Tin tức đã xử lý (RSS) – tránh gửi trùng
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_seen (
                    id        SERIAL PRIMARY KEY,
                    feed_type TEXT NOT NULL,           -- 'MACRO' hoặc 'SPECIALIZED'
                    guid      TEXT,
                    link      TEXT NOT NULL,
                    title     TEXT,
                    published TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(feed_type, link)
                )
            """)

            # Tùy chọn nhận tin tức theo từng user
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_pref (
                    chat_id            BIGINT PRIMARY KEY,
                    enable_specialized BOOLEAN NOT NULL DEFAULT TRUE,
                    enable_macro       BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)

            # Bảng log mã đã thông báo BCTC (đảm bảo mỗi mã/quý chỉ báo 1 lần)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bctc_notified (
                    id          SERIAL PRIMARY KEY,
                    symbol      TEXT NOT NULL,
                    year        INTEGER NOT NULL,
                    quarter     INTEGER NOT NULL,
                    notified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(symbol, year, quarter)
                )
            """)

            # Hàng đợi gửi thông báo BCTC (crawl lúc 2h, gửi lúc 8h)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bctc_notify_queue (
                    id          SERIAL PRIMARY KEY,
                    symbol      TEXT NOT NULL,
                    year        INTEGER NOT NULL,
                    quarter     INTEGER NOT NULL,
                    notify_date DATE   NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(symbol, year, quarter, notify_date)
                )
            """)

            # Tổng hợp những paid_users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paid_users (
                chat_id     BIGINT PRIMARY KEY,
                expiry_date TIMESTAMPTZ NOT NULL, -- Ngày hết hạn gói Pro
                plan_name   TEXT DEFAULT 'pro'
                )
            """)
            
            # Bảng lưu báo cáo phân tích đã gửi (tránh trùng)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS analysis_report_seen (
                    id           SERIAL PRIMARY KEY,
                    symbol       TEXT, -- Chỉ để tham khảo
                    link         TEXT NOT NULL,
                    title        TEXT,
                    published_at TIMESTAMPTZ, -- Lấy từ cột 'date' của vnstock
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    
                    -- Dùng (link, published_at) làm khóa duy nhất như bạn yêu cầu
                    UNIQUE(link, published_at) 
                )
            """)

            # Bảng lưu các đơn hàng thanh toán Pro (SePay)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_orders (
                    order_id    TEXT PRIMARY KEY,         -- Nội dung chuyển khoản (VD: PAY_12345_ABC)
                    chat_id     BIGINT NOT NULL,          -- User nào đã yêu cầu
                    amount      INTEGER NOT NULL,         -- Số tiền (VND)
                    days_to_add INTEGER NOT NULL,         -- Số ngày Pro sẽ được cộng
                    status      TEXT NOT NULL DEFAULT 'PENDING', -- PENDING | PAID | FAILED
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Cài đặt riêng cho từng user (VD: VN30F1M)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_user_settings (
                    chat_id     BIGINT PRIMARY KEY,
                    settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

        conn.commit()

# --- ADMIN DASHBOARD HELPERS ---

def upsert_user_info(chat_id: int, username: str | None, full_name: str | None):
    """Cập nhật thông tin user mỗi khi họ tương tác"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (chat_id, username, full_name, last_active_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE 
                SET username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_active_at = NOW()
            """, (chat_id, username, full_name))
        conn.commit()

# db_utils.py

def get_admin_dashboard_data():
    """
    Lấy dữ liệu tổng hợp cho Admin Dashboard.
    (ĐÃ CẬP NHẬT: Lấy thêm has_used_trial)
    """
    with get_conn() as conn:
        with conn.cursor(row_factory=rows.dict_row) as cur:
            cur.execute("""
                SELECT 
                    u.chat_id as id,
                    COALESCE(u.full_name, u.username, 'User ' || u.chat_id) as name,
                    u.username,
                    u.admin_note,
                    COALESCE(u.is_banned, FALSE) as is_banned,
                    
                    -- [MỚI] Lấy trạng thái đã dùng thử hay chưa
                    COALESCE(u.has_used_trial, FALSE) as has_used_trial,
                    
                    -- Thông tin Pro
                    p.expiry_date,
                    p.plan_name, -- Lấy thêm plan_name để biết là gói 'trial' hay 'pro'
                    CASE WHEN p.expiry_date > NOW() THEN true ELSE false END as is_pro,
                    CASE WHEN p.expiry_date <= NOW() THEN true ELSE false END as is_expired,
                    
                    -- Tính số ngày còn lại
                    EXTRACT(DAY FROM (p.expiry_date - NOW()))::int as days_left,
                    
                    -- Watchlist
                    COALESCE(w.watch_list, '[]'::jsonb) as watchlist
                    
                FROM users u
                LEFT JOIN paid_users p ON u.chat_id = p.chat_id
                LEFT JOIN bot_watch w ON u.chat_id = w.chat_id
                ORDER BY u.last_active_at DESC
            """)
            return cur.fetchall()

def update_user_admin_note(chat_id: int, note: str):
    """Cập nhật ghi chú của admin cho user"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET admin_note = %s
                WHERE chat_id = %s
            """, (note, chat_id))
        conn.commit()

# ==========================================
# WATCHLIST
# ==========================================
def get_all_watch():
    """
    (ĐÃ SỬA) Trả về dict {str(chat_id): {'list': [...]}}.
    Ưu tiên đọc từ Redis, nếu miss cache của user nào thì
    gọi get_watch_list_for_chat() để fallback DB cho user đó.
    """
    data: dict[str, dict] = {}
    chat_ids_from_redis = set()

    # 1) Thử đọc từ Redis trước
    try:
        r = get_redis()
        chat_ids_from_redis = r.smembers("watch_chat_ids") or set()
        
        if chat_ids_from_redis:
            for cid_str in chat_ids_from_redis:
                try:
                    cid = int(cid_str)
                except Exception:
                    continue
                
                # Cố gắng lấy cache của user này
                raw = r.get(f"watch:{cid}")
                
                if raw is not None:
                    # === CACHE HIT ===
                    try:
                        wl = json.loads(raw)
                    except Exception:
                        wl = [] # Cache lỗi thì coi như rỗng
                    data[str(cid)] = {"list": wl}
                else:
                    # === CACHE MISS (Do bị Invalidate hoặc hết hạn) ===
                    # Gọi hàm "get" đơn lẻ (hàm này đã có logic fallback DB
                    # và tự động warm-up lại cache)
                    redis_debug_log(f"get_all_watch: Cache miss cho {cid}, gọi fallback...")
                    wl_fallback = get_watch_list_for_chat(cid) # Đã bao gồm DB + warm-up
                    if wl_fallback is not None:
                         data[str(cid)] = {"list": wl_fallback}
                    # (Nếu wl_fallback là None, tức là user không có trong DB)
            
            # Nếu data có nội dung thì trả về, không cần fallback toàn bộ
            if data:
                return data

    except Exception as e:
        redis_debug_log(f"Redis error in get_all_watch(): {e}")
        # Bỏ qua và fallback DB toàn bộ bên dưới

    # 2) Fallback: đọc toàn bộ từ DB (giữ nguyên logic của bạn)
    # (Trường hợp Redis sập, hoặc set "watch_chat_ids" rỗng)
    
    redis_debug_log("Redis empty/error → fallback DB (get_all_watch)")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, watch_list FROM bot_watch")
            rows = cur.fetchall()

    for chat_id, watch_list in rows:
        data[str(chat_id)] = {"list": watch_list or []}

    # 3) Warm lại Redis từ DB (best-effort, giữ nguyên)
    try:
        r = get_redis()
        r.delete("watch_chat_ids")
        for chat_id, watch_list in rows:
            key = f"watch:{chat_id}"
            wl = watch_list or []
            r.set(key, json.dumps(wl))
            r.sadd("watch_chat_ids", chat_id)
    except Exception:
        pass

    return data

def get_watch_list_for_chat(chat_id: int):
    """
    Lấy watchlist của 1 chat_id.
    Ưu tiên đọc từ Redis, nếu miss thì fallback DB và warm lại cache.
    """
    # 1) Thử lấy từ Redis trước
    try:
        r = get_redis()
        raw = r.get(f"watch:{chat_id}")
        if raw is not None:
            try:
                return json.loads(raw)
            except Exception:
                # Nếu parse lỗi thì bỏ qua, fallback DB
                pass
        else:
            redis_debug_log(f"Redis miss watch:{chat_id}")           
    except Exception as e:
        # Có lỗi Redis thì fallback DB
        redis_debug_log(f"Redis error watch:{chat_id}: {e}")
        pass

    # 2) Fallback: đọc từ DB như cũ
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT watch_list FROM bot_watch WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
    watch_list = row[0] if row else None

    # 3) Warm lại Redis nếu có dữ liệu
    if watch_list is not None:
        try:
            r = get_redis()
            key = f"watch:{chat_id}"
            
            # SỬA LỖI: List rỗng [] vẫn là một giá trị hợp lệ,
            # vẫn phải set key và add vào set toàn cục.
            
            # (watch_list có thể là [] hoặc ['HPG'])
            r.set(key, json.dumps(watch_list))
            r.sadd("watch_chat_ids", chat_id)
            
        except Exception:
            pass
    return watch_list

# TRONG DB_UTILS.PY

def save_watch_list_for_chat(chat_id: int, watch_list):
    """
    Lưu watchlist vào DB và đồng thời LÀM MẤT HIỆU LỰC (Invalidate) cache Redis.
    """
    # 1) Ghi vào DB (nguồn sự thật)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_watch (chat_id, watch_list)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (chat_id)
                DO UPDATE SET watch_list = EXCLUDED.watch_list
                """,
                (chat_id, json.dumps(watch_list)),
            )
        conn.commit()

    # 2) Xóa cache của user này khỏi Redis (Invalidate)
    try:
        r = get_redis()
        key = f"watch:{chat_id}"
        
        # Xóa cache chi tiết của user
        r.delete(key) 
        
        # Vẫn quản lý set "watch_chat_ids"
        if watch_list is not None and len(watch_list) > 0:
            # Nếu user có watchlist, đảm bảo họ có trong set
            r.sadd("watch_chat_ids", chat_id)
        else:
            # Nếu user xóa hết (list rỗng), xóa họ khỏi set (tùy chọn)
            # Hoặc cứ để trong set cũng không sao, vì get_all_watch sẽ xử lý
            pass 
            # Nếu muốn chặt chẽ:
            # if not watch_list:
            #     r.srem("watch_chat_ids", chat_id)
            
    except Exception as e:
        # Ghi log lỗi Redis nhưng không làm sập tiến trình
        redis_debug_log(f"Redis invalidate error watch:{chat_id}: {e}")
        pass

# ==========================================
# BOT ACTIVE (BẢO TRÌ)
# ==========================================
def get_bot_active() -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = 'bot_active'")
            row = cur.fetchone()
    if not row or row[0] is None:
        return True

    value = row[0]
    if isinstance(value, dict) and "active" in value:
        return bool(value["active"])
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict) and "active" in parsed:
                return bool(parsed["active"])
            if isinstance(parsed, bool):
                return parsed
        except Exception:
            pass
    return True


def set_bot_active(active: bool):
    payload = {"active": bool(active)}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value)
                VALUES ('bot_active', %s::jsonb)
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(payload),))
        conn.commit()

# ==========================================
# LOG COMMAND
# ==========================================
def log_command_usage(chat_id: int, command: str, admin_id: int | None = None):
    """
    Ghi log sử dụng lệnh.
    Nếu admin_id được truyền vào và chat_id == admin_id -> bỏ qua (không tính vào thống kê).
    """
    if admin_id is not None and chat_id == admin_id:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO command_log (chat_id, command) VALUES (%s, %s)",
                (chat_id, command),
            )
        conn.commit()


def get_command_stats():
    """
    Trả về list[dict]:
        {"command": "...", "day": "x", "month": "y", "total": "z"}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    command,
                    SUM(CASE WHEN used_at::date = CURRENT_DATE THEN 1 ELSE 0 END) AS day,
                    SUM(
                        CASE
                            WHEN date_trunc('month', used_at) = date_trunc('month', CURRENT_DATE)
                            THEN 1 ELSE 0
                        END
                    ) AS month,
                    COUNT(*) AS total
                FROM command_log
                GROUP BY command
                ORDER BY total DESC
            """)
            rows = cur.fetchall()

    stats = []
    for command, day, month, total in rows:
        stats.append(
            {
                "command": command,
                "day": str(day),
                "month": str(month),
                "total": str(total),
            }
        )
    return stats

def get_user_logs(chat_id: int, limit: int = 10):
    """Lấy nhật ký lệnh gần nhất của user"""
    with get_conn() as conn:
        with conn.cursor(row_factory=rows.dict_row) as cur:
            cur.execute("""
                SELECT command, used_at
                FROM command_log
                WHERE chat_id = %s
                ORDER BY used_at DESC
                LIMIT %s
            """, (chat_id, limit))
            return cur.fetchall()

def get_user_configs(chat_id: int):
    """Lấy cài đặt cá nhân (VN30, Stock Alert, News...)"""
    with get_conn() as conn:
        with conn.cursor(row_factory=rows.dict_row) as cur:
            # 1. Lấy cấu hình Bot Settings (VN30 & Stock)
            cur.execute("SELECT settings FROM bot_user_settings WHERE chat_id = %s", (chat_id,))
            s_row = cur.fetchone()
            
            vn30_enabled = False
            stock_enabled = True # Mặc định là BẬT
            
            if s_row and s_row.get('settings'):
                settings = s_row['settings']
                vn30_enabled = settings.get('vn30f1m_enabled', False)
                stock_enabled = settings.get('stock_alert_enabled', True)

            # 2. Lấy cấu hình Tin tức (News Pref)
            cur.execute("SELECT enable_specialized, enable_macro FROM news_pref WHERE chat_id = %s", (chat_id,))
            n_row = cur.fetchone()
            
            news_enabled = True
            if n_row:
                if not n_row['enable_specialized'] and not n_row['enable_macro']:
                    news_enabled = False

            return {
                "vn30": vn30_enabled,
                "stock": stock_enabled, # <--- Key mới thêm vào
                "news": news_enabled
            }

# ==========================================
# LOG TIN NHẮN BOT ĐÃ GỬI
# ==========================================
def save_bot_message(chat_id: int, message_id: int, msg_type: str = 'GENERAL'):
    """
    Lưu lại message bot đã gửi kèm theo LOẠI TIN NHẮN.
    msg_type: 'STOCK_ALERT', 'VN30_ALERT', 'SESSION_NOTICE', 'EOD_SUMMARY', 'GENERAL'
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_msg_log (chat_id, message_id, msg_type)
                VALUES (%s, %s, %s)
            """, (chat_id, message_id, msg_type))
        conn.commit()


def get_bot_messages_in_range(start_time, end_time):
    """Lấy danh sách (chat_id, message_id, sent_at) trong khoảng thời gian."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id, message_id, sent_at
                FROM bot_msg_log
                WHERE sent_at BETWEEN %s AND %s
                ORDER BY sent_at ASC
            """, (start_time, end_time))
            return cur.fetchall()


def delete_bot_messages_in_range(start_time, end_time):
    """Xoá record log khỏi DB sau khi xoá trên Telegram."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM bot_msg_log
                WHERE sent_at BETWEEN %s AND %s
            """, (start_time, end_time))
        conn.commit()

# ==========================================
# CACHE SCREENER VALUE
# ==========================================
def upsert_stock_value_batch(records):
    """
    Upsert nhiều dòng vào stock_value_cache.
    records: list[dict] với key:
        symbol, exchange, industry, pe, pb, roe, floor,
        asset_proxy, liquidity_proxy
    """
    if not records:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            params = []
            for r in records:
                params.append(
                    (
                        r.get("symbol"),
                        r.get("exchange"),
                        r.get("industry"),
                        r.get("pe"),
                        r.get("pb"),
                        r.get("roe"),
                        r.get("floor"),
                        r.get("asset_proxy"),
                        r.get("liquidity_proxy"),
                    )
                )
            cur.executemany("""
                INSERT INTO stock_value_cache (
                    symbol, exchange, industry, pe, pb, roe, floor,
                    asset_proxy, liquidity_proxy
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE
                SET
                    exchange = EXCLUDED.exchange,
                    industry = EXCLUDED.industry,
                    pe       = EXCLUDED.pe,
                    pb       = EXCLUDED.pb,
                    roe      = EXCLUDED.roe,
                    floor    = EXCLUDED.floor,
                    asset_proxy     = EXCLUDED.asset_proxy,
                    liquidity_proxy = EXCLUDED.liquidity_proxy,
                    updated_at      = NOW()
            """, params)
        conn.commit()


def load_stock_value_cache():
    """Trả về list[dict] từ stock_value_cache."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol, exchange, industry, pe, pb, roe, floor,
                    asset_proxy, liquidity_proxy, updated_at
                FROM stock_value_cache
            """)
            rows = cur.fetchall()

    data = []
    for (
        sym, exchange, industry, pe, pb, roe, floor,
        asset_proxy, liquidity_proxy, updated_at
    ) in rows:
        data.append(
            {
                "symbol": sym,
                "exchange": exchange,
                "industry": industry,
                "pe": float(pe) if pe is not None else None,
                "pb": float(pb) if pb is not None else None,
                "roe": float(roe) if roe is not None else None,
                "floor": floor,
                "asset_proxy": float(asset_proxy) if asset_proxy is not None else None,
                "liquidity_proxy": float(liquidity_proxy) if liquidity_proxy is not None else None,
                "updated_at": updated_at,
            }
        )
    return data


def get_stock_value_cache_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_value_cache")
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def clear_stock_value_cache():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE stock_value_cache")
        conn.commit()

# ==========================================
# NEWS: RSS SEEN + PREFERENCES + REDIS + POSTGRES
# ==========================================
def has_news_seen(feed_type: str, link: str) -> bool:
    """
    Kiểm tra xem bài RSS (feed_type, link) đã được xử lý chưa.

    Ưu tiên:
    1) Kiểm tra Redis (nhanh).
    2) Nếu miss -> kiểm tra Postgres (bảng news_seen).
       - Nếu tìm thấy -> warm lại Redis rồi trả True.
       - Nếu không -> False.

    Lưu ý:
    - Không log "Redis HIT" nữa để tránh spam log trong các loop RSS.
    - Chỉ log khi có lỗi Redis/DB.
    """
    ft = (feed_type or "").strip().upper()
    if not ft or not link:
        return False

    # Chuẩn hoá link để giảm trùng lặp do query tracking
    canonical_link = canonicalize_link(link)

    # 1) Hỏi Redis trước
    try:
        if has_news_seen_redis(ft, canonical_link):
            return True
    except Exception as e:
        redis_debug_log(f"[news_seen] Redis error in has_news_seen: {e}")

    # 2) Nếu Redis miss -> hỏi Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM news_seen
                    WHERE feed_type = %s
                      AND link = %s
                    LIMIT 1
                    """,
                    (ft, canonical_link),
                )
                row = cur.fetchone()
    except Exception as e:
        # Nếu DB có vấn đề, fallback: coi như chưa seen để không chặn tin
        redis_debug_log(f"[news_seen] DB error in has_news_seen: {e}")
        return False

    if row:
        # Warm lại Redis để lần sau nhanh hơn
        try:
            mark_news_seen_redis(ft, canonical_link)
        except Exception as e:
            redis_debug_log(f"[news_seen] Redis warm error: {e}")
        return True

    return False


def mark_news_seen(
    feed_type: str,
    link: str,
    guid: str | None = None,
    title: str | None = None,
    published=None,
) -> None:
    """
    Đánh dấu 1 bài RSS đã được xử lý.

    - Ghi vào Postgres (bảng news_seen) với canonical_link.
    - Set key tương ứng trong Redis (write-through cache).

    published có thể là:
    - datetime (tz-aware hoặc naive)
    - string (Postgres tự cast nếu hợp lệ)
    - None
    """
    ft = (feed_type or "").strip().upper()
    if not ft or not link:
        return

    canonical_link = canonicalize_link(link)

    # 1) Ghi vào Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO news_seen (feed_type, guid, link, title, published)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (feed_type, link) DO NOTHING
                    """,
                    (ft, guid, canonical_link, title, published),
                )
            conn.commit()
    except Exception as e:
        # Không raise để tránh làm hỏng luồng chính nếu DB lỗi
        redis_debug_log(f"[news_seen] DB error in mark_news_seen: {e}")

    # 2) Đánh dấu trong Redis
    try:
        mark_news_seen_redis(
            ft,
            canonical_link,
            title=title,
            published=published,
        )
    except Exception as e:
        redis_debug_log(f"[news_seen] Redis error in mark_news_seen: {e}")


def get_news_seen_count(feed_type: str) -> int:
    """
    Đếm số bài đã seen cho 1 feed_type dựa trên Redis.
    (chỉ dùng cho logic warm-up / debug).
    """
    return get_news_seen_count_redis(feed_type)


def cleanup_old_news_seen(retention_days: int = 180) -> int:
    """
    Xoá các bản ghi news_seen cũ hơn retention_days (mặc định ~ 6 tháng).

    - Dùng COALESCE(published, created_at) để xử lý cả case thiếu published.
    - Trả về số dòng đã xoá (rowcount).
    """
    try:
        retention_days = int(retention_days)
    except Exception:
        retention_days = 180

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Ví dụ: retention_days = 180 -> '180 days'
                interval_str = f"{retention_days} days"
                cur.execute(
                    """
                    DELETE FROM news_seen
                    WHERE COALESCE(published, created_at)
                          < NOW() - (%s)::INTERVAL
                    """,
                    (interval_str,),
                )
                deleted = cur.rowcount or 0
            conn.commit()
    except Exception as e:
        redis_debug_log(f"[news_seen] DB error in cleanup_old_news_seen: {e}")
        return 0

    return deleted


def get_news_pref(chat_id: int) -> dict:
    """
    Lấy preference nhận tin tức của 1 user.
    Ưu tiên đọc từ Redis, nếu miss thì fallback DB và warm lại cache.
    Mặc định: cả hai đều True nếu chưa có record.
    """
    # 1) Thử lấy từ Redis
    try:
        r = get_redis()
        raw = r.get(f"news_pref:{chat_id}")
        if raw is not None:
            try:
                data = json.loads(raw)
                return {
                    "enable_specialized": bool(
                        data.get("enable_specialized", True)
                    ),
                    "enable_macro": bool(data.get("enable_macro", True)),
                }
            except Exception as e:
                # Nếu JSON lỗi thì bỏ qua, fallback DB
                redis_debug_log(f"Redis error news_pref:{chat_id}: {e}")
                pass
        else:
            redis_debug_log(f"Redis miss news_pref:{chat_id}")
    except Exception as e:
        # Redis lỗi -> fallback DB
        redis_debug_log(f"Redis error news_pref:{chat_id}: {e}")
        pass

    # 2) Fallback: đọc từ DB
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT enable_specialized, enable_macro FROM news_pref WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()

    if not row:
        pref = {"enable_specialized": True, "enable_macro": True}
    else:
        enable_specialized, enable_macro = row
        pref = {
            "enable_specialized": bool(enable_specialized),
            "enable_macro": bool(enable_macro),
        }

    # 3) Warm lại Redis (best-effort)
    try:
        r = get_redis()
        r.set(f"news_pref:{chat_id}", json.dumps(pref))
    except Exception:
        pass

    return pref

def set_news_pref(
    chat_id: int,
    enable_specialized: bool | None = None,
    enable_macro: bool | None = None,
):
    """
    Cập nhật preference nhận tin tức cho 1 user.
    Ghi vào DB và sync sang Redis.
    """
    current = get_news_pref(chat_id)

    if enable_specialized is None:
        enable_specialized = current["enable_specialized"]
    if enable_macro is None:
        enable_macro = current["enable_macro"]

    # 1) Ghi vào DB
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO news_pref (chat_id, enable_specialized, enable_macro)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET enable_specialized = EXCLUDED.enable_specialized,
                    enable_macro       = EXCLUDED.enable_macro
                """,
                (chat_id, enable_specialized, enable_macro),
            )
        conn.commit()

    # 2) Cập nhật Redis (best-effort)
    pref = {
        "enable_specialized": bool(enable_specialized),
        "enable_macro": bool(enable_macro),
    }
    try:
        r = get_redis()
        r.set(f"news_pref:{chat_id}", json.dumps(pref))
    except Exception:
        pass

def is_news_enabled_for_chat(chat_id: int, feed_type: str) -> bool:
    """Kiểm tra user có bật nhận loại tin feed_type hay không."""
    pref = get_news_pref(chat_id)
    if feed_type.upper() == "SPECIALIZED":
        return pref["enable_specialized"]
    if feed_type.upper() == "MACRO":
        return pref["enable_macro"]
    return True

# Cache RAM (in-memory) cho các setting không đổi thường xuyên
_news_pref_cache: dict[int, dict] = {}
_news_pref_cache_time: float = 0.0

def get_all_news_pref(max_cache_age_sec: int = 60) -> dict[int, dict]:
    """
    (HÀM MỚI - TỐI ƯU N+1)
    Lấy toàn bộ setting tin tức của user, cache 60 giây trong RAM.
    Trả về dict: {chat_id: {"enable_specialized": bool, "enable_macro": bool}}
    """
    global _news_pref_cache, _news_pref_cache_time
    now = time.time()

    # 1. Dùng cache RAM (siêu nhanh) nếu còn hạn
    if (now - _news_pref_cache_time) < max_cache_age_sec and _news_pref_cache:
        return _news_pref_cache

    # 2. Cache cũ/rỗng -> Lấy từ DB (CHỈ 1 QUERY)
    redis_debug_log("[get_all_news_pref] Cache RAM rỗng/hết hạn. Đang lấy từ DB...")
    data = {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, enable_specialized, enable_macro FROM news_pref")
                rows = cur.fetchall()
        
        for chat_id, enable_specialized, enable_macro in rows:
            data[int(chat_id)] = {
                "enable_specialized": bool(enable_specialized),
                "enable_macro": bool(enable_macro),
            }
    except Exception as e:
        redis_debug_log(f"Lỗi get_all_news_pref: {e}")
        # Nếu lỗi, trả về cache cũ (nếu có) để bot tiếp tục chạy
        if _news_pref_cache:
            return _news_pref_cache
        return {} # Hoặc trả rỗng nếu chưa có cache

    # 3. Cập nhật cache RAM
    _news_pref_cache = data
    _news_pref_cache_time = now
    redis_debug_log(f"[get_all_news_pref] Đã cache {len(data)} user vào RAM.")
    return data
# ==========================================
# BÁO CÁO TÀI CHÍNH (BCTC)
# ==========================================

def has_bctc_notified(symbol: str, year: int, quarter: int) -> bool:
    """True nếu mã này đã được thông báo BCTC cho kỳ (year, quarter)."""
    sym = str(symbol).upper().strip()
    if not sym:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM bctc_notified
                WHERE symbol = %s AND year = %s AND quarter = %s
                LIMIT 1
                """,
                (sym, year, quarter),
            )
            row = cur.fetchone()
    return row is not None


def mark_bctc_notified(symbol: str, year: int, quarter: int):
    """Đánh dấu đã gửi thông báo BCTC cho mã / kỳ này (chỉ 1 lần / quý)."""
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bctc_notified (symbol, year, quarter)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol, year, quarter)
                DO UPDATE SET notified_at = NOW()
                """,
                (sym, year, quarter),
            )
        conn.commit()


def add_bctc_queue(symbol: str, year: int, quarter: int, notify_date):
    """
    Thêm mã vào hàng đợi BCTC để gửi thông báo vào ngày notify_date (08:00).
    """
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bctc_notify_queue (symbol, year, quarter, notify_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, year, quarter, notify_date)
                DO NOTHING
                """,
                (sym, year, quarter, notify_date),
            )
        conn.commit()


def get_bctc_queue_by_date(notify_date):
    """Lấy danh sách (symbol, year, quarter) cần gửi vào ngày notify_date."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, year, quarter
                FROM bctc_notify_queue
                WHERE notify_date = %s
                """,
                (notify_date,),
            )
            return cur.fetchall()


def clear_bctc_queue_entry(symbol: str, year: int, quarter: int, notify_date):
    """Xóa 1 entry khỏi hàng đợi sau khi gửi xong."""
    sym = str(symbol).upper().strip()
    if not sym:
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM bctc_notify_queue
                WHERE symbol = %s AND year = %s AND quarter = %s AND notify_date = %s
                """,
                (sym, year, quarter, notify_date),
            )
        conn.commit()

# ----------------------------------------------
# BACKUP / RESTORE CORE DATA
# ----------------------------------------------

# Các bảng "cốt lõi" cần backup/restore khi đổi DB
CORE_TABLES = [
    "users",
    "bot_watch",       # watchlist mỗi user
    "news_pref",       # bật/tắt tin tức
    "bot_config",      # cấu hình chung (BOT_ACTIVE, v.v.)
    "bctc_notified",   # đã notify BCTC quý nào
    "paid_users",          # 💰 Dữ liệu Gói Pro (QUAN TRỌNG NHẤT)
    "bot_orders",          # 🧾 Lịch sử đơn hàng
    "bot_user_settings",   # ⚙️ Cài đặt VN30F1M
    "analysis_report_seen" # 📊 Lịch sử báo cáo (tránh spam lại)
]


def export_core_data():
    """
    Export dữ liệu core ra dict Python, sau đó có thể json.dump ra file.
    Cấu trúc:
    {
        "version": 1,
        "exported_at": "...",
        "tables": {
            "bot_watch": [...],
            "news_pref": [...],
            "bot_config": [...],
            "bctc_notified": [...]
        }
    }
    """
    payload = {
        "version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "tables": {},
    }

    with get_conn() as conn:
        # row_factory=dict_row => mỗi dòng là 1 dict
        with conn.cursor(row_factory=rows.dict_row) as cur:
            for tbl in CORE_TABLES:
                cur.execute(f"SELECT * FROM {tbl}")
                rows_data = cur.fetchall()
                payload["tables"][tbl] = rows_data

    return payload

def import_core_data(payload: dict, mode: str = "replace"):
    """
    (ĐÃ CẬP NHẬT FULL) Restore dữ liệu core từ dict.
    Hỗ trợ đầy đủ: Watchlist, News, Config, BCTC, Paid Users, Orders, Settings, Reports.
    """
    tables = payload.get("tables", {}) or {}

    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) TRUNCATE (Xóa sạch cũ) nếu mode là replace
            if mode == "replace":
                # Tắt check ngoại khóa tạm thời nếu cần (Postgres thường ko cần nếu ko có FK cứng)
                for tbl in CORE_TABLES:
                    # Chỉ truncate nếu trong file backup CÓ dữ liệu của bảng đó
                    if tbl in tables: 
                        try:
                            cur.execute(f"TRUNCATE {tbl}")
                        except Exception as e:
                            print(f"Lỗi truncate {tbl}: {e}")

            # --- NHÓM 1: CORE CŨ ---

            # 2) bot_watch
            for row in tables.get("bot_watch", []):
                chat_id = row["chat_id"]
                watch_list = row.get("watch_list", [])
                raw_json = watch_list if isinstance(watch_list, str) else json.dumps(watch_list)
                cur.execute(
                    "INSERT INTO bot_watch (chat_id, watch_list) VALUES (%s, %s::jsonb) ON CONFLICT (chat_id) DO UPDATE SET watch_list = EXCLUDED.watch_list",
                    (chat_id, raw_json),
                )

            # 3) news_pref
            for row in tables.get("news_pref", []):
                cur.execute(
                    "INSERT INTO news_pref (chat_id, enable_specialized, enable_macro) VALUES (%s, %s, %s) ON CONFLICT (chat_id) DO UPDATE SET enable_specialized = EXCLUDED.enable_specialized, enable_macro = EXCLUDED.enable_macro",
                    (row["chat_id"], row["enable_specialized"], row["enable_macro"]),
                )

            # 4) bot_config
            for row in tables.get("bot_config", []):
                val = row.get("value")
                json_val = val if isinstance(val, str) or val is None else json.dumps(val)
                cur.execute(
                    "INSERT INTO bot_config (key, value) VALUES (%s, %s::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (row["key"], json_val),
                )

            # 5) bctc_notified
            for row in tables.get("bctc_notified", []):
                cur.execute(
                    "INSERT INTO bctc_notified (symbol, year, quarter, notified_at) VALUES (%s, %s, %s, %s) ON CONFLICT (symbol, year, quarter) DO NOTHING",
                    (row["symbol"], row["year"], row["quarter"], row.get("notified_at")),
                )

            # --- NHÓM 2: CORE MỚI (TIỀN NONG & SETTINGS) ---

            # 6) paid_users (QUAN TRỌNG)
            for row in tables.get("paid_users", []):
                cur.execute(
                    """
                    INSERT INTO paid_users (chat_id, expiry_date, plan_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chat_id) DO UPDATE 
                    SET expiry_date = EXCLUDED.expiry_date, plan_name = EXCLUDED.plan_name
                    """,
                    (row["chat_id"], row["expiry_date"], row.get("plan_name", "pro")),
                )

            # 7) bot_orders (Lịch sử đơn hàng)
            for row in tables.get("bot_orders", []):
                cur.execute(
                    """
                    INSERT INTO bot_orders (order_id, chat_id, amount, days_to_add, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id) DO NOTHING
                    """,
                    (
                        row["order_id"], row["chat_id"], row["amount"], row["days_to_add"], 
                        row["status"], row.get("created_at"), row.get("updated_at")
                    ),
                )

            # 8) bot_user_settings (Cài đặt VN30F1M)
            for row in tables.get("bot_user_settings", []):
                s_val = row.get("settings")
                s_json = s_val if isinstance(s_val, str) else json.dumps(s_val)
                cur.execute(
                    """
                    INSERT INTO bot_user_settings (chat_id, settings, updated_at)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (chat_id) DO UPDATE SET settings = EXCLUDED.settings
                    """,
                    (row["chat_id"], s_json, row.get("updated_at")),
                )

            # 9) analysis_report_seen (Lịch sử báo cáo)
            for row in tables.get("analysis_report_seen", []):
                cur.execute(
                    """
                    INSERT INTO analysis_report_seen (symbol, link, title, published_at, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (link, published_at) DO NOTHING
                    """,
                    (
                        row.get("symbol"), row["link"], row.get("title"), 
                        row.get("published_at"), row.get("created_at")
                    ),
                )

            # 10) users (Thông tin user, ban status, admin notes)
            for row in tables.get("users", []):
                # Chỉ restore nếu có dữ liệu
                if not row: continue
                
                cur.execute(
                    """
                    INSERT INTO users (chat_id, username, full_name, joined_at, last_active_at, admin_note, is_banned)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id) DO UPDATE
                    SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        full_name = COALESCE(EXCLUDED.full_name, users.full_name),
                        admin_note = COALESCE(EXCLUDED.admin_note, users.admin_note),
                        is_banned = EXCLUDED.is_banned,
                        last_active_at = GREATEST(users.last_active_at, EXCLUDED.last_active_at)
                    """,
                    (
                        row.get("chat_id"), 
                        row.get("username"), 
                        row.get("full_name"),
                        row.get("joined_at"), 
                        row.get("last_active_at"),
                        row.get("admin_note"), 
                        row.get("is_banned", False)
                    ),
                )

        conn.commit()

# ----------------------------------------------
# THEO DÕI THÁNG ĐÃ RESTORE CORE
# ----------------------------------------------

def _normalize_bot_config_value(value):
    """
    Chuẩn hóa value lấy từ bot_config:
    - Nếu là dict: trả lại nguyên dict.
    - Nếu là str: cố parse json -> dict.
    - Ngược lại: trả về {}.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def get_last_restore_month() -> str | None:
    """
    Lấy tháng gần nhất đã chạy /restore_core, dạng 'YYYY-MM'.
    Nếu chưa từng restore -> trả về None.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = 'last_restore_core'")
            row = cur.fetchone()

    if not row or row[0] is None:
        return None

    data = _normalize_bot_config_value(row[0])
    month = data.get("month")
    if isinstance(month, str):
        return month
    return None


def mark_restore_done_now():
    """
    Ghi nhận rằng /restore_core vừa được chạy thành công,
    lưu lại tháng hiện tại vào bot_config.
    """
    now = datetime.datetime.utcnow()
    month_key = now.strftime("%Y-%m")
    payload = {
        "month": month_key,
        "restored_at": now.isoformat() + "Z",
    }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_config (key, value)
                VALUES ('last_restore_core', %s::jsonb)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value
                """,
                (json.dumps(payload),),
            )
        conn.commit()

# ==========================================
# QUẢN LÝ USER TRẢ PHÍ (GÓI PRO)
# ==========================================
def add_paid_user(chat_id: int, days_to_add: int):
    """
    Thêm hoặc gia hạn Gói Pro cho user.
    - Nếu user đã hết hạn: Gia hạn `days_to_add` ngày kể từ HÔM NAY.
    - Nếu user vẫn còn hạn: Cộng dồn `days_to_add` ngày vào ngày hết hạn HIỆN TẠI.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Logic:
            # 1. INSERT... ON CONFLICT: Thử thêm mới
            # 2. DO UPDATE: Nếu đã tồn tại
            # 3. GREATEST(paid_users.expiry_date, NOW()): Lấy mốc thời gian
            #    là ngày hết hạn hiện tại, hoặc HÔM NAY (nếu đã hết hạn).
            # 4. + interval '1 day' * %s: Cộng thêm số ngày gia hạn.
            cur.execute(
                """
                INSERT INTO paid_users (chat_id, expiry_date)
                VALUES (%s, NOW() + interval '1 day' * %s)
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    expiry_date = GREATEST(paid_users.expiry_date, NOW()) + interval '1 day' * %s;
                """,
                (chat_id, days_to_add, days_to_add), # `days_to_add` được dùng 2 lần
            )
        conn.commit()

def is_user_pro(chat_id: int) -> bool:
    """
    Trả về True nếu user có trong bảng paid_users VÀ ngày hết hạn > NOW()
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 
                FROM paid_users
                WHERE chat_id = %s AND expiry_date > NOW()
                LIMIT 1;
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    
    # Nếu row không phải là None (tức là tìm thấy 1 dòng) -> True
    # Nếu row là None (không tìm thấy) -> False
    return bool(row)

def get_user_pro_expiry(chat_id: int) -> datetime.datetime | None:
    """
    Lấy ngày hết hạn Pro (expiry_date) của user.
    Trả về datetime object (TIMESTAMPTZ) nếu có, ngược lại trả về None.
    Hàm này lấy cả user đã hết hạn.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT expiry_date 
                FROM paid_users
                WHERE chat_id = %s
                LIMIT 1;
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    
    # row[0] sẽ là expiry_date (datetime object) hoặc None
    return row[0] if row else None

def deactivate_paid_user(chat_id: int) -> int:
    """
    Ngưng hoạt động Gói Pro của user ngay lập tức.
    Hàm này set ngày hết hạn về quá khứ (NOW() - 1 giây).
    Trả về số dòng đã được cập nhật (0 hoặc 1).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE paid_users
                SET expiry_date = NOW() - interval '1 second'
                WHERE chat_id = %s;
                """,
                (chat_id,),
            )
            # cur.rowcount là số dòng bị ảnh hưởng bởi lệnh UPDATE
            updated_rows = cur.rowcount
        conn.commit()
    return updated_rows

def remove_paid_user(chat_id: int) -> int:
    """
    Xoá vĩnh viễn Gói Pro của user khỏi bảng.
    Trả về số dòng đã bị xoá (0 hoặc 1).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM paid_users
                WHERE chat_id = %s;
                """,
                (chat_id,),
            )
            # cur.rowcount là số dòng bị ảnh hưởng bởi lệnh DELETE
            deleted_rows = cur.rowcount
        conn.commit()
    return deleted_rows

def get_all_pro_chat_ids() -> set[int]:
    """
    Lấy MỘT TẬP (set) chứa chat_id của tất cả user
    đang có Gói Pro (còn hạn).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id 
                FROM paid_users
                WHERE expiry_date > NOW();
                """,
            )
            rows = cur.fetchall()
    
    # Trả về set (tập hợp) để tra cứu O(1) (siêu nhanh)
    return {int(row[0]) for row in rows}

def get_all_paid_users_expiry() -> dict[int, datetime.datetime]:
    """
    Lấy MỘT DICT (dictionary) chứa {chat_id: expiry_date}
    của TẤT CẢ user trong bảng paid_users (bao gồm cả user đã hết hạn).
    """
    mapping = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chat_id, expiry_date 
                FROM paid_users;
                """
            )
            rows = cur.fetchall()
    
    # Trả về dict để tra cứu O(1)
    # expiry_date là kiểu TIMESTAMPTZ, nên nó sẽ là đối tượng datetime
    for row in rows:
        try:
            mapping[int(row[0])] = row[1] 
        except Exception:
            pass # Bỏ qua nếu parse lỗi
            
    return mapping
#-----------------------------------
# ==========================================
# ANALYSIS REPORT SEEN (TÍNH NĂNG MỚI)
# ==========================================

# TTL 180 ngày, giống news_seen
REPORT_SEEN_TTL_SECONDS = 180 * 24 * 60 * 60

def _make_report_seen_key(link: str, pub_date_str: str) -> str:
    """
    Tạo key cache Redis.
    Dùng canonical_link (từ news_seen_cache) và pub_date_str 
    (theo yêu cầu của bạn) để đảm bảo tính duy nhất.
    """
    canonical_link = canonicalize_link(link)
    h = hashlib.sha256(f"{canonical_link}::{pub_date_str}".encode("utf-8")).hexdigest()[:32]
    return f"report_seen:{h}"

def has_report_seen(link: str, pub_date_str: str) -> bool:
    """
    Kiểm tra xem báo cáo (link + date) đã được xử lý chưa.
    1. Kiểm tra Redis
    2. Nếu miss -> Kiểm tra Postgres
    3. Nếu có -> Warm lại Redis
    """
    key = _make_report_seen_key(link, pub_date_str)
    
    # 1. Hỏi Redis trước
    try:
        r = get_redis()
        if r.exists(key):
            return True
    except Exception as e:
        redis_debug_log(f"[report_seen] Redis error in has_report_seen: {e}")

    # 2. Nếu Redis miss -> hỏi Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM analysis_report_seen
                    WHERE link = %s
                      AND published_at = %s
                    LIMIT 1
                    """,
                    (link, pub_date_str), # pub_date_str là 'YYYY-MM-DDTHH:MM:SSZ'
                )
                row = cur.fetchone()
    except Exception as e:
        redis_debug_log(f"[report_seen] DB error in has_report_seen: {e}")
        return False

    if row:
        # 3. Warm lại Redis
        try:
            r = get_redis()
            r.set(key, "1", ex=REPORT_SEEN_TTL_SECONDS)
        except Exception as e:
            redis_debug_log(f"[report_seen] Redis warm error: {e}")
        return True

    return False

def mark_report_seen(symbol: str, link: str, title: str, pub_date_str: str):
    """
    Đánh dấu 1 báo cáo đã được xử lý (Ghi vào DB và Redis).
    """
    key = _make_report_seen_key(link, pub_date_str)

    # 1. Ghi vào Postgres
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO analysis_report_seen (symbol, link, title, published_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (link, published_at) DO NOTHING
                    """,
                    (symbol, link, title, pub_date_str),
                )
            conn.commit()
    except Exception as e:
        redis_debug_log(f"[report_seen] DB error in mark_report_seen: {e}")

    # 2. Đánh dấu trong Redis
    try:
        r = get_redis()
        r.set(key, "1", ex=REPORT_SEEN_TTL_SECONDS)
    except Exception as e:
        redis_debug_log(f"[report_seen] Redis error in mark_report_seen: {e}")

#----------------------------------------------------------
# ==========================================
# MORNING DIGEST HELPERS (cho ADMIN)
# ==========================================

def get_recent_bctc_notified(since_dt):
    """
    Lấy danh sách BCTC đã thông báo từ since_dt tới nay
    (dựa trên bảng bctc_notified).
    Trả về list[ (symbol, year, quarter, notified_at) ].
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, year, quarter, notified_at
                FROM bctc_notified
                WHERE notified_at >= %s
                ORDER BY notified_at DESC
                """,
                (since_dt,),
            )
            rows = cur.fetchall()
    return rows


def get_recent_analysis_reports(since_dt):
    """
    Lấy các báo cáo phân tích đã được đánh dấu (analysis_report_seen)
    trong khoảng thời gian gần đây.
    Trả về list[ (symbol, title, link, published_at, created_at) ].
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, title, link, published_at, created_at
                FROM analysis_report_seen
                WHERE created_at >= %s
                ORDER BY published_at DESC NULLS LAST, created_at DESC
                """,
                (since_dt,),
            )
            rows = cur.fetchall()
    return rows


def get_recent_news_seen(feed_type, since_dt):
    """
    Lấy tin tức đã ghi trong bảng news_seen cho feed_type
    ('MACRO' hoặc 'SPECIALIZED') từ since_dt đến nay.
    Trả về list[ (title, link, published, created_at) ].
    """
    ft = (feed_type or "").upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT title, link, published, created_at
                FROM news_seen
                WHERE feed_type = %s
                  AND created_at >= %s
                ORDER BY published DESC NULLS LAST, created_at DESC
                """,
                (ft, since_dt),
            )
            rows = cur.fetchall()
    return rows

# ==========================================
# SEPAPAY ORDERS (GÓI PRO)
# ==========================================

def create_pending_order(chat_id: int, amount: int, days_to_add: int) -> str:
    """
    Tạo một đơn hàng mới ở trạng thái PENDING.
    Trả về order_id (nội dung chuyển khoản) duy nhất.
    """
    # Tạo Order ID: PAY_[chat_id]_[random_5_chars]
    random_part = str(uuid.uuid4()).split('-')[0][:5].upper()
    order_id = f"PAY{chat_id}{random_part}"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_orders (order_id, chat_id, amount, days_to_add, status)
                VALUES (%s, %s, %s, %s, 'PENDING')
                RETURNING order_id;
                """,
                (order_id, chat_id, amount, days_to_add),
            )
        conn.commit()
    
    return order_id

def get_order_by_id(order_id: str) -> dict | None:
    """
    Tìm một đơn hàng. Trả về dict chứa (chat_id, amount, status, days_to_add)
    """
    with get_conn() as conn:
        # Dùng row_factory để trả về dict (bạn đã import 'rows' rồi)
        with conn.cursor(row_factory=rows.dict_row) as cur:
            cur.execute(
                """
                SELECT chat_id, amount, status, days_to_add
                FROM bot_orders
                WHERE order_id = %s
                LIMIT 1;
                """,
                (order_id,),
            )
            row = cur.fetchone()
    
    return row # Sẽ là dict hoặc None

def mark_order_as_paid(order_id: str):
    """
    Cập nhật trạng thái đơn hàng sang PAID và thời gian updated_at.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bot_orders
                SET status = 'PAID', updated_at = NOW()
                WHERE order_id = %s;
                """,
                (order_id,),
            )
        conn.commit()

def cleanup_old_pending_orders(days_old: int = 3):
    """
    Xóa các đơn hàng PENDING cũ hơn 'days_old' ngày.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM bot_orders
                    WHERE status = 'PENDING'
                      AND created_at < NOW() - (%s || ' days')::INTERVAL;
                    """,
                    (str(days_old),)
                )
                deleted_count = cur.rowcount
            conn.commit()
        return deleted_count
    except Exception as e:
        print(f"[DB_UTILS] Lỗi khi dọn dẹp bot_orders: {e}")
        return 0
    
def get_user_orders(chat_id: int):
    """Lấy 5 đơn hàng gần nhất của user"""
    with get_conn() as conn:
        with conn.cursor(row_factory=rows.dict_row) as cur:
            cur.execute("""
                SELECT order_id, amount, status, created_at
                FROM bot_orders
                WHERE chat_id = %s
                ORDER BY created_at DESC
                LIMIT 5
            """, (chat_id,))
            return cur.fetchall()

def get_total_revenue_real():
    """Tính tổng tiền từ các đơn hàng đã PAID"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT SUM(amount) 
                FROM bot_orders 
                WHERE status = 'PAID'
            """)
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else 0

#-------------------------------------------------
def get_messages_to_cleanup(target_types: list[str], older_than_minutes: int = 0):
    """
    Lấy danh sách các tin nhắn cần xóa dựa trên loại tin.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Lấy id, chat_id, message_id của các tin thuộc loại target_types
            # Có thể thêm điều kiện thời gian nếu muốn (ở đây ta xóa hết các loại này trước EOD)
            cur.execute("""
                SELECT id, chat_id, message_id
                FROM bot_msg_log
                WHERE msg_type = ANY(%s)
                ORDER BY sent_at ASC
            """, (target_types,))
            return cur.fetchall()

def delete_bot_log_record(record_id: int):
    """Xóa 1 dòng log trong DB sau khi đã xóa trên Telegram"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_msg_log WHERE id = %s", (record_id,))
        conn.commit()

def get_latest_bot_message_id(chat_id: int, msg_type: str) -> int | None:
    """
    Lấy message_id của tin nhắn gần nhất theo loại (msg_type) gửi cho chat_id.
    Dùng để tìm lại tin Digest cũ để tháo ghim.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message_id
                FROM bot_msg_log
                WHERE chat_id = %s AND msg_type = %s
                ORDER BY sent_at DESC
                LIMIT 1
            """, (chat_id, msg_type))
            row = cur.fetchone()
    return row[0] if row else None

# --------------------------------------

def save_historical_valuation_to_redis(data: dict):
    """
    Lưu dữ liệu định giá lịch sử (Avg PE/PB 5 năm) vào Redis.
    TTL: 24 giờ (vì dữ liệu này chỉ tính 1 lần/ngày).
    """
    try:
        r = get_redis()
        # Key theo ngày để đảm bảo tươi mới
        vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
        today = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")
        key = f"screener:history:{today}"
        
        # Lưu JSON
        r.set(key, json.dumps(data), ex=86400)
        redis_debug_log(f"Đã lưu {len(data)} mã định giá lịch sử vào Redis: {key}")
    except Exception as e:
        redis_debug_log(f"Lỗi lưu historical valuation: {e}")

def get_historical_valuation_from_redis() -> dict | None:
    """
    Lấy dữ liệu định giá lịch sử từ Redis.
    """
    try:
        r = get_redis()
        vn_tz = pytz.timezone("Asia/Ho_Chi_Minh")
        today = datetime.datetime.now(vn_tz).strftime("%Y-%m-%d")
        key = f"screener:history:{today}"
        
        raw = r.get(key)
        if raw:
            return json.loads(raw)
        return None
    except Exception as e:
        redis_debug_log(f"Lỗi đọc historical valuation: {e}")
        return None

# ==========================================
# USER SETTINGS (VN30 & STOCK ALERT)
# ==========================================

def get_vn30f1m_enabled_map() -> dict[int, bool]:
    """Lấy danh sách user đang bật cảnh báo VN30F1M"""
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
    """Cập nhật trạng thái bật/tắt VN30F1M"""
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

def get_stock_alert_enabled_map() -> dict[int, bool]:
    """
    Lấy trạng thái nhận cảnh báo Stock của user.
    Mặc định (COALESCE) là TRUE (BẬT) nếu chưa cài đặt.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id,
                       COALESCE((settings ->> 'stock_alert_enabled')::boolean, TRUE) AS enabled
                FROM bot_user_settings
            """)
            rows = cur.fetchall()
    return {int(r[0]): bool(r[1]) for r in rows}

def set_stock_alert_enabled(chat_id: int, enabled: bool):
    """Cập nhật trạng thái bật/tắt Stock Alert"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_user_settings (chat_id, settings)
                VALUES (%s, jsonb_build_object('stock_alert_enabled', %s))
                ON CONFLICT (chat_id)
                DO UPDATE SET
                    settings = COALESCE(bot_user_settings.settings, '{}'::jsonb)
                               || jsonb_build_object('stock_alert_enabled', EXCLUDED.settings->'stock_alert_enabled'),
                    updated_at = NOW()
            """, (chat_id, enabled))
        conn.commit()

#--------------------------------------------

def get_banned_users() -> set[int]:
    """Lấy toàn bộ danh sách chat_id bị chặn để load vào RAM."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM users WHERE is_banned = TRUE")
            rows = cur.fetchall()
    return {int(r[0]) for r in rows}

def set_user_ban_status(chat_id: int, is_banned: bool):
    """Cập nhật trạng thái chặn của user."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Upsert: Nếu user chưa có trong bảng users thì tạo mới luôn
            cur.execute("""
                INSERT INTO users (chat_id, is_banned, last_active_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE 
                SET is_banned = EXCLUDED.is_banned
            """, (chat_id, is_banned))
        conn.commit()

# ==========================================
# TÍNH NĂNG DÙNG THỬ (TRIAL) - MỚI THÊM
# ==========================================

def check_trial_eligibility(chat_id: int) -> str:
    """
    Kiểm tra xem user có được dùng thử không.
    Trả về:
    - 'OK': Hợp lệ, cho dùng thử.
    - 'IS_PRO': Đang là Pro rồi (không cần trial).
    - 'USED': Đã từng dùng thử rồi (hết lượt).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Check xem đang là Pro không
            cur.execute("SELECT 1 FROM paid_users WHERE chat_id = %s AND expiry_date > NOW()", (chat_id,))
            if cur.fetchone():
                return 'IS_PRO'
            
            # 2. Check xem đã dùng trial chưa (trong bảng users)
            cur.execute("SELECT has_used_trial FROM users WHERE chat_id = %s", (chat_id,))
            row = cur.fetchone()
            # Nếu chưa có record trong users (user mới tinh), coi như chưa dùng (False)
            has_used = row[0] if row else False
            
            if has_used:
                return 'USED'
            
    return 'OK'

def activate_trial_package(chat_id: int, days: int = 3):
    """
    Kích hoạt gói dùng thử.
    1. Đánh dấu user đã dùng trial (users.has_used_trial = True).
    2. Thêm vào bảng paid_users với hạn dùng 'days' ngày.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1. Đánh dấu đã dùng
            cur.execute("""
                INSERT INTO users (chat_id, has_used_trial, last_active_at)
                VALUES (%s, TRUE, NOW())
                ON CONFLICT (chat_id) DO UPDATE 
                SET has_used_trial = TRUE, last_active_at = NOW()
            """, (chat_id,))
            
            # 2. Kích hoạt Pro (plan_name='trial')
            # Nếu user cũ hết hạn -> Ghi đè ngày hết hạn = NOW() + 3 ngày
            cur.execute("""
                INSERT INTO paid_users (chat_id, expiry_date, plan_name)
                VALUES (%s, NOW() + interval '1 day' * %s, 'trial')
                ON CONFLICT (chat_id)
                DO UPDATE SET 
                    expiry_date = NOW() + interval '1 day' * %s,
                    plan_name = 'trial'
            """, (chat_id, days, days))
            
        conn.commit()



