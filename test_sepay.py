import requests
import json
import time

# ================= CẤU HÌNH TEST =================
BOT_PORT = 10000 
BASE_URL = f"http://127.0.0.1:{BOT_PORT}/sepay-webhook"

YOUR_SEPAY_TOKEN = "734QUF5CDMGE1AS5ODB2WFN6T9RQIKM1XPM0VTDWCX7NH3ZOIRLENR2KGW49XHKJ"

# === 🌟 VIỆC CẦN LÀM ===
# 1. Chạy 'alert_bot.py' (với code webhook ĐÃ SỬA)
# 2. Gõ /upgrade 2 LẦN với bot để lấy 2 Order ID MỚI.
# 3. Dán 2 Order ID đó vào đây:

# Dùng cho Kịch bản 1 (Thành công) và 4 (Lặp)
YOUR_ORDER_ID_FOR_SUCCESS = "PAY1088200599898E0"  # <--- SỬA LẠI (ID 1)

# Dùng cho Kịch bản 3 (Thiếu tiền/Sai tiền)
YOUR_ORDER_ID_FOR_FAIL_AMOUNT = "PAY1088200599BB803" # <--- SỬA LẠI (ID 2)
# =================================================

# Số tiền gói Pro (phải khớp với alert_bot.py, ví dụ 99000)
PRO_AMOUNT = 99000  # <--- SỬA LẠI NẾU CẦN

# Header mặc định (SỬA LỖI: Bỏ HEADERS global cũ)
# Chúng ta sẽ tạo header trong hàm post_to_webhook

def post_to_webhook(description: str, payload: dict, use_token: str | None = None):
    """
    Hàm helper để gửi POST request (ĐÃ SỬA: gửi token trong Header)
    """
    print(f"--- 🚀 ĐANG TEST: {description} ---")
    
    # Tạo header động cho mỗi request
    test_headers = {"Content-Type": "application/json"}
    if use_token:
        # Gửi theo đúng định dạng SePay yêu cầu
        test_headers["Authorization"] = f"Apikey {use_token}"
            
    try:
        response = requests.post(
            BASE_URL, 
            data=json.dumps(payload), 
            headers=test_headers, # <--- Dùng header động
            timeout=5
        )
        
        print(f"Status Code: {response.status_code}")
        try:
            print(f"Response JSON: {response.json()}")
        except requests.exceptions.JSONDecodeError:
            print(f"Response Text: {response.text}")
            
    except requests.exceptions.ConnectionError:
        print(f"LỖI: Không thể kết nối đến {BASE_URL}.")
        print("Bạn đã chạy file 'alert_bot.py' chưa?")
    except Exception as e:
        print(f"LỖI TEST: {e}")
    print("-" * (len(description) + 20) + "\n")


# === CÁC KỊCH BẢN TEST (ĐÃ SỬA: Bỏ 'api_key' khỏi payload) ===

def test_1_success(order_id: str):
    """KỊCH BẢN 1: Thành công"""
    payload = {
        # KHÔNG CÓ "api_key" ở đây
        "content": order_id,
        "transferAmount": PRO_AMOUNT,
        "transferType": "in",
        "gateway": "VCB",
        "accountNumber": "0071000123456"
    }
    # Gửi token đúng trong header
    post_to_webhook(f"Thành công (Order: {order_id}, Amount: {PRO_AMOUNT})", payload, use_token=YOUR_SEPAY_TOKEN)

def test_2_fail_invalid_token():
    """KỊCH BẢN 2: Sai Token (Bảo mật)"""
    payload = {
        "content": "TEST_123",
        "transferAmount": PRO_AMOUNT,
        "transferType": "in",
    }
    # Gửi token SAI trong header
    post_to_webhook("Sai Token (Bảo mật)", payload, use_token="INVALID_TOKEN_123")

def test_3_fail_incorrect_amount(order_id: str):
    """KỊCH BẢN 3: Sai tiền (Thiếu 1000đ)"""
    payload = {
        "content": order_id,
        "transferAmount": PRO_AMOUNT - 1000, 
        "transferType": "in",
    }
    # Gửi token đúng trong header
    post_to_webhook(f"Sai tiền (Order: {order_id}, Amount: {PRO_AMOUNT - 1000})", payload, use_token=YOUR_SEPAY_TOKEN)

def test_4_fail_duplicate(order_id: str):
    """KỊCH BẢN 4: Giao dịch lặp"""
    payload = {
        "content": order_id,
        "transferAmount": PRO_AMOUNT,
        "transferType": "in",
    }
    # Gửi token đúng trong header
    post_to_webhook(f"Giao dịch lặp (Order: {order_id})", payload, use_token=YOUR_SEPAY_TOKEN)

def test_5_fail_order_not_found():
    """KỊCH BẢN 5: Sai nội dung (Order ID)"""
    payload = {
        "content": "SAI_NOI_DUNG_123",
        "transferAmount": PRO_AMOUNT,
        "transferType": "in",
    }
    # Gửi token đúng trong header
    post_to_webhook("Sai nội dung (Order ID không tồn tại)", payload, use_token=YOUR_SEPAY_TOKEN)

def test_6_fail_not_in_transaction(order_id: str):
    """KỊCH BẢN 6: Giao dịch tiền ra (không phải 'in')"""
    payload = {
        "content": order_id,
        "transferAmount": PRO_AMOUNT,
        "transferType": "out", # Tiền ra
    }
    # Gửi token đúng trong header
    post_to_webhook("Giao dịch tiền ra (Type 'out')", payload, use_token=YOUR_SEPAY_TOKEN)

# === HÀM CHẠY CHÍNH (Đã sửa tên test 3) ===
if __name__ == "__main__":
    if "PAY_..." in YOUR_ORDER_ID_FOR_SUCCESS or \
       "PAY_..." in YOUR_ORDER_ID_FOR_FAIL_AMOUNT:
        print("=" * 60)
        print("⚠️ VUI LÒNG SỬA 2 BIẾN 'YOUR_ORDER_ID...' Ở ĐẦU FILE!")
        print("1. Chạy 'alert_bot.py' (bản đã sửa Header)")
        print("2. Gõ /upgrade 2 LẦN với bot để lấy 2 Order ID mới")
        print("3. Dán 2 Order ID đó vào dòng 19 và 22 của file test.")
        print("=" * 60)
    else:
        # Chạy các kịch bản
        
        test_1_success(YOUR_ORDER_ID_FOR_SUCCESS)
        time.sleep(1)
        
        test_2_fail_invalid_token()
        time.sleep(1)

        test_3_fail_incorrect_amount(YOUR_ORDER_ID_FOR_FAIL_AMOUNT)
        time.sleep(1)
        
        test_4_fail_duplicate(YOUR_ORDER_ID_FOR_SUCCESS)
        time.sleep(1)
        
        test_5_fail_order_not_found()
        time.sleep(1)
        
        test_6_fail_not_in_transaction(YOUR_ORDER_ID_FOR_SUCCESS)