import os
import psycopg
from dotenv import load_dotenv

# Load biến môi trường (để lấy DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

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

if __name__ == "__main__":
    migrate_admin_note()
    add_ban_column()
    migrate_trial()