import os
import time
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

# Load biến môi trường (để lấy DATABASE_URL)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ LỖI: Không tìm thấy biến DATABASE_URL trong file .env")
    print("👉 Hãy chắc chắn bạn đã tạo file .env chứa link kết nối đến DB.")
    exit()

def fix_database():
    print(f"🚀 Đang kết nối tới Database...")
    
    try:
        # Tạo connection pool tạm thời
        pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=1)
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                print("⚙️ Đang thêm cột 'msg_type' vào bảng bot_msg_log...")
                
                # 1. Thêm cột msg_type (nếu chưa có)
                cur.execute("""
                    ALTER TABLE bot_msg_log 
                    ADD COLUMN IF NOT EXISTS msg_type TEXT DEFAULT 'GENERAL';
                """)
                
                # 2. Tạo Index để sau này xóa tin rác cho nhanh
                print("⚙️ Đang tạo Index tối ưu tốc độ...")
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_msg_log_cleanup 
                    ON bot_msg_log(msg_type, sent_at);
                """)
                
            conn.commit()
            
        print("\n✅ THÀNH CÔNG! Database đã được nâng cấp.")
        print("👉 Bây giờ bạn có thể chạy lại Bot, lỗi sẽ biến mất.")
        
    except Exception as e:
        print(f"\n❌ THẤT BẠI: {e}")
    finally:
        try:
            pool.close()
        except:
            pass

if __name__ == "__main__":
    fix_database()