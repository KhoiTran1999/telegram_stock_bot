# test_slugify.py
import re
import pandas as pd

# 1. Đảm bảo import hàm unidecode ở ĐẦU file
try:
    from unidecode import unidecode
except ImportError:
    print("⛔ LỖI: Vui lòng cài thư viện 'unidecode' trước khi chạy.")
    print("Chạy lệnh: pip install unidecode")
    exit(1)

from vnstock import Company    # Dùng cú pháp v1.x của bạn

# ==========================================================
# CÁC HÀM SLUGIFY (Không thay đổi)
# ==========================================================

def slugify(text: str) -> str:
    """
    Chuẩn hóa text thành dạng slug (viết thường, bỏ dấu, gạch ngang).
    """
    if not text:
        return ""
    
    # 1. Bỏ dấu tiếng Việt (ví dụ: "Mảng" -> "Mang")
    text = unidecode(text) # Dòng này giờ sẽ gọi đúng HÀM
    
    # 2. Viết thường
    text = text.lower()
    
    # 3. Thay thế tất cả ký tự không phải chữ/số bằng gạch ngang
    text = re.sub(r'[^a-z0-9]+', '-', text)
    
    # 4. Xóa gạch ngang thừa ở đầu/cuối
    text = text.strip('-')
    
    # 5. Gộp nhiều gạch ngang thành 1
    text = re.sub(r'-+', '-', text)
    
    return text

def generate_trading_link(report_title: str) -> str:
    """
    Tạo link trading.vietcap.com.vn từ tiêu đề báo cáo.
    """
    BASE_URL = "https://trading.vietcap.com.vn/iq/report-detail/vi/"
    
    slug = slugify(report_title)
    if not slug:
        return "https://trading.vietcap.com.vn/iq/analysis-report/vi"
        
    return BASE_URL + slug

# ==========================================================
# HÀM TEST VỚI MÃ HPG (Không thay đổi)
# ==========================================================
def test_hpg_link_generation():
    
    symbol_to_test = "fpt"
    print(f"\n--- 🧪 Bắt đầu test: Lấy báo cáo cho {symbol_to_test} ---")

    try:
        # 1. Khởi tạo đối tượng v1.x
        company = Company(symbol=symbol_to_test)
        
        # 2. Lấy báo cáo
        df = company.reports()
        
        if df is None or df.empty:
            print(f"❌ Không tìm thấy báo cáo nào cho {symbol_to_test}.")
            return

        print(f"✅ Tìm thấy {len(df)} báo cáo. Đang test báo cáo mới nhất...")
        
        # 3. Lấy báo cáo mới nhất (dòng đầu tiên)
        latest_report = df.iloc[0]
        
        title = latest_report["name"]
        original_link = latest_report["link"]
        
        # 4. Tạo link mới từ title (name)
        generated_link = generate_trading_link(title)
        
        print("\n--- KẾT QUẢ TEST ---")
        print(f"📝 Title (Name):")
        print(f"   {title}")
        
        print(f"\n🔗 Link gốc (từ API):")
        print(f"   {original_link}")
        
        print(f"\n✨ Link tự tạo (Slugified):")
        print(f"   {generated_link}")
        
        print("\n--- ⏹️ Kết thúc test ---")
        print("\n👉 Bạn hãy copy link 'Slugified' dán vào trình duyệt xem có chạy đúng không nhé.")

    except Exception as e:
        print(f"❌ Đã xảy ra lỗi nghiêm trọng khi test {symbol_to_test}:")
        print(f"Lỗi: {e}")

# ==========================================================
# CHẠY TEST
# ==========================================================
if __name__ == "__main__":
    # 2. XÓA BỎ đoạn code `import unidecode` bị lỗi ở đây
    #    (Vì chúng ta đã kiểm tra ở đầu file rồi)
    test_hpg_link_generation()