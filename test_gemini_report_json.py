import os
import json
import re
from dotenv import load_dotenv
from google import genai

# Load biến môi trường (GEMINI_API_KEY)
load_dotenv(override=True)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("❌ Lỗi: Chưa cấu hình GEMINI_API_KEY trong file .env")
    exit(1)

# Danh sách giả lập (Portfolio > 5 mã để test xem prompt có xử lý tốt không)
# Trong code thật ta sẽ slice list[:5] trước khi gửi, nhưng ở đây ta gửi 5 mã tiêu biểu.
TEST_SYMBOLS = ["HPG", "SSI", "FPT"]

def clean_json_text(text: str) -> str:
    """
    Làm sạch response từ Gemini để lấy chuỗi JSON thuần.
    Thường AI sẽ trả về dạng:
    ```json
    { ... }
    ```
    Hàm này sẽ gỡ bỏ markdown đó.
    """
    # 1. Xóa markdown code block ```json ... ```
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    
    # 2. Trim khoảng trắng thừa
    text = text.strip()
    return text

def build_json_prompt(symbols: list[str]) -> str:
    symbols_str = ", ".join(symbols)
    
    prompt = f"""
Bạn là chuyên gia phân tích đầu tư chứng khoán Việt Nam theo trường phái Tăng trưởng (Growth Investing).
Hãy phân tích danh mục sau: {symbols_str}

YÊU CẦU OUTPUT:
Trả về kết quả dưới định dạng **JSON thuần** (không có text dẫn dắt, không markdown).
Cấu trúc JSON bắt buộc như sau:

{{
  "general_market_comment": "Nhận định ngắn gọn (2-3 câu) về thị trường chung và tác động đến danh mục này.",
  "portfolio_health_score": 7.5,
  "stocks": [
    {{
      "symbol": "MÃ",
      "industry": "Tên ngành",
      "action": "Nắm giữ / Mua thêm / Hạ tỷ trọng / Quan sát",
      "analysis": "Đánh giá ngắn gọn (dưới 200 ký tự) về triển vọng tăng trưởng, catalyst hoặc rủi ro chính.",
      "key_metrics": "VD: P/E: 10.x, KQKD Q3 tăng 20%"
    }}
  ]
}}

LƯU Ý QUAN TRỌNG:
1. Điểm số `portfolio_health_score` là số thực (float) từ 0 đến 10.
2. Trường `analysis` phải súc tích, tập trung vào *câu chuyện tăng trưởng*.
3. Chỉ trả về JSON, không thêm lời chào.
"""
    return prompt

def test_gemini_json():
    print(f"🚀 Đang gửi yêu cầu tới Gemini cho danh mục: {TEST_SYMBOLS}...")
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    model_id = "gemini-2.5-flash" # Hoặc "gemini-1.5-flash" tùy bạn, flash cho nhanh và rẻ
    
    prompt = build_json_prompt(TEST_SYMBOLS)
    
    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config={
                'response_mime_type': 'application/json' # Ép kiểu JSON từ phía API (tính năng mới của Gemini)
            }
        )
        
        raw_text = response.text
        print("\n--- RAW RESPONSE TỪ GEMINI ---")
        print(raw_text[:500] + "...\n(đã cắt bớt)")
        
        # Làm sạch và parse
        cleaned_json = clean_json_text(raw_text)
        data = json.loads(cleaned_json)
        
        print("\n✅ PARSE JSON THÀNH CÔNG!")
        print("-" * 30)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("-" * 30)
        
        # Kiểm tra cấu trúc
        if "stocks" in data and isinstance(data["stocks"], list):
            print(f"🔍 Đã nhận diện được {len(data['stocks'])} mã trong JSON.")
        else:
            print("⚠️ Cảnh báo: Không tìm thấy key 'stocks' hoặc sai định dạng.")

    except json.JSONDecodeError as e:
        print(f"\n❌ Lỗi Parse JSON: {e}")
        print("Nội dung nhận được không phải JSON hợp lệ.")
    except Exception as e:
        print(f"\n❌ Lỗi gọi API: {e}")

if __name__ == "__main__":
    test_gemini_json()