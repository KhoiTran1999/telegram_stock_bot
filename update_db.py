import os
import psycopg
from dotenv import load_dotenv

# Load biến môi trường (để lấy DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Bắt buộc thêm sslmode=require khi kết nối tới DB external (trừ localhost)
if DATABASE_URL and "localhost" not in DATABASE_URL and "127.0.0.1" not in DATABASE_URL:
    if "?sslmode=" not in DATABASE_URL and "&sslmode=" not in DATABASE_URL:
        if "?" in DATABASE_URL:
            DATABASE_URL += "&sslmode=require"
        else:
            DATABASE_URL += "?sslmode=require"

def migrate_admin_note():
    print("🚀 Đang kết nối đến Database...")
    
    if not DATABASE_URL:
        print("❌ Lỗi: Không tìm thấy DATABASE_URL trong file .env hoặc môi trường.")
        return

    try:
        # Kết nối trực tiếp (không qua Pool để chạy nhanh 1 lần rồi ngắt)
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                print("⚙️ Đang thực thi lệnh thêm cột 'admin_note'...")
                
                # Lệnh SQL thêm cột
                sql = "ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_note TEXT;"
                
                cur.execute(sql)
                
            # Commit thay đổi
            conn.commit()
            print("✅ THÀNH CÔNG! Đã thêm cột 'admin_note' vào bảng 'users'.")
            
    except Exception as e:
        print(f"❌ CÓ LỖI XẢY RA: {e}")

def add_ban_column():
    print("🚀 Đang kết nối Database để thêm cột Blacklist...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Thêm cột is_banned, mặc định là FALSE (chưa bị chặn)
                sql = "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE;"
                cur.execute(sql)
            conn.commit()
            print("✅ THÀNH CÔNG! Đã thêm cột 'is_banned'.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

def migrate_trial():
    print("🚀 Đang bắt đầu thêm cột 'has_used_trial' vào bảng 'users'...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # Thêm cột is_banned, mặc định là FALSE (chưa bị chặn)
                sql = "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_used_trial BOOLEAN DEFAULT FALSE;"
                cur.execute(sql)
            conn.commit()
            print("✅ THÀNH CÔNG! Đã thêm cột 'has_used_trial'.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

def migrate_paid_users():
    print("🚀 Đang kiểm tra và thêm cột 'plan_name' vào bảng 'paid_users'...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = "ALTER TABLE paid_users ADD COLUMN IF NOT EXISTS plan_name TEXT DEFAULT 'pro';"
                cur.execute(sql)
            conn.commit()
            print("✅ THÀNH CÔNG! Đã cập nhật bảng 'paid_users'.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

def create_new_tables():
    print("🚀 Đang tạo các bảng mới (bot_orders, analysis_report_seen, v.v.)...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                # 1. bot_orders
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_orders (
                        order_id    TEXT PRIMARY KEY,
                        chat_id     BIGINT NOT NULL,
                        amount      INTEGER NOT NULL,
                        days_to_add INTEGER NOT NULL,
                        status      TEXT NOT NULL DEFAULT 'PENDING',
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                
                # 2. analysis_report_seen
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS analysis_report_seen (
                        id           SERIAL PRIMARY KEY,
                        symbol       TEXT,
                        link         TEXT NOT NULL,
                        title        TEXT,
                        published_at TIMESTAMPTZ,
                        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE(link, published_at) 
                    )
                """)
                
                # 3. bctc_notify_queue
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
                
                # 4. bot_user_settings
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_user_settings (
                        chat_id     BIGINT PRIMARY KEY,
                        settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
                        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                
            conn.commit()
            print("✅ THÀNH CÔNG! Đã tạo các bảng mới.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

def create_stock_personalization_table():
    print("🚀 Đang tạo bảng stock_personalization (lưu ghi chú từng mã)...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stock_personalization (
                        id         BIGSERIAL PRIMARY KEY,
                        symbol     TEXT NOT NULL,
                        note       TEXT NOT NULL,
                        expires_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

                # Bổ sung cột còn thiếu nếu bảng đã tồn tại trước đó
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS id BIGSERIAL")
                cur.execute("ALTER TABLE stock_personalization ALTER COLUMN symbol SET NOT NULL")
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")

                # Đảm bảo khoá chính là cột id
                cur.execute("ALTER TABLE stock_personalization DROP CONSTRAINT IF EXISTS stock_personalization_pkey")
                cur.execute("ALTER TABLE stock_personalization ADD CONSTRAINT stock_personalization_pkey PRIMARY KEY (id)")

                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_stock_personalization_expiry
                    ON stock_personalization (expires_at)
                    """
                )

                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_stock_personalization_symbol
                    ON stock_personalization (symbol)
                    """
                )
                #---------------- Thêm các cột mới cho quản lý ghi chú từ user/admin ----------------#
                # 1. Thêm cột status (Mặc định là APPROVED cho các note cũ của Admin)
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'APPROVED';")
                
                # 2. Thêm cột submitted_by (Lưu chat_id người gửi)
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS submitted_by BIGINT;")
                
                # 3. Thêm cột admin_comment (Lý do từ chối/ghi chú admin)
                cur.execute("ALTER TABLE stock_personalization ADD COLUMN IF NOT EXISTS admin_comment TEXT;")
                
                # 4. Tạo Index cho status để Worker query nhanh
                cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_personalization_status ON stock_personalization (status);")
                
                # 5. Tạo Index cho submitted_by để User load danh sách nhanh
                cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_personalization_user ON stock_personalization (submitted_by);")

                #---------------------------------------------------------------------------------------#


            conn.commit()
            print("✅ THÀNH CÔNG! Đã đảm bảo bảng stock_personalization tồn tại.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

def add_command_log_note_column():
    """
    Thêm cột 'note' vào bảng command_log để lưu nội dung chat với AI hoặc tham số lệnh.
    """
    print("🚀 Đang thêm cột 'note' vào bảng 'command_log'...")
    if not DATABASE_URL:
        print("❌ Lỗi: Thiếu DATABASE_URL.")
        return

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                sql = "ALTER TABLE command_log ADD COLUMN IF NOT EXISTS note TEXT;"
                cur.execute(sql)
            conn.commit()
            print("✅ THÀNH CÔNG! Đã thêm cột 'note' vào bảng 'command_log'.")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

from db_utils import init_db

if __name__ == "__main__":
    print("🚀 Đang khởi tạo Database core tables...")
    try:
        init_db()
        print("✅ Khởi tạo Database core tables thành công.")
    except Exception as e:
        print(f"❌ Lỗi khởi tạo DB: {e}")

    migrate_admin_note()
    add_ban_column()
    migrate_trial()
    migrate_paid_users()
    create_new_tables()
    create_stock_personalization_table()
    add_command_log_note_column()