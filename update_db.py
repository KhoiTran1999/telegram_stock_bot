import os
import psycopg
from dotenv import load_dotenv

# Load biến môi trường (để lấy DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def migrate_db():
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

if __name__ == "__main__":
    migrate_db()