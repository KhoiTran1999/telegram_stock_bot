import pandas as pd
import datetime
import pytz
from vnstock import Trading, Quote

# Cấu hình mã test
TEST_SYMBOL = "HPG" 

def test_vci():
    print(f"\n🔵 --- KIỂM TRA NGUỒN VCI ({TEST_SYMBOL}) ---")
    try:
        trading = Trading(source="VCI")
        df = trading.price_board([TEST_SYMBOL])
        
        if df is None or df.empty:
            print("⚠️ VCI trả về rỗng.")
            return

        # 1. In cấu trúc cột
        print(f"👉 Raw Columns: {df.columns.tolist()}")
        
        # 2. In dòng dữ liệu đầu tiên
        row = df.iloc[0]
        print("👉 Raw Row Data (Sample):")
        # Truy cập MultiIndex
        try:
            price = row[('match', 'match_price')]
            ref = row[('listing', 'ref_price')]
            sym = row[('listing', 'symbol')]
            print(f"   - Symbol: {sym}")
            print(f"   - Match Price (Raw): {price} (Kiểu: {type(price)})")
            print(f"   - Ref Price (Raw): {ref}")
        except Exception as e:
            print(f"❌ Lỗi parse key VCI: {e}")

    except Exception as e:
        print(f"❌ Lỗi gọi VCI: {e}")

def test_tcbs():
    print(f"\n🟠 --- KIỂM TRA NGUỒN TCBS ({TEST_SYMBOL}) ---")
    try:
        # Logic lấy history giống trong hàm fetch_data_smart
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        start_str = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        
        quote = Quote(symbol=TEST_SYMBOL, source="TCBS")
        df = quote.history(start=start_str, end=today_str, interval="1D")
        
        if df is None or df.empty:
            print("⚠️ TCBS trả về rỗng.")
            return

        # 1. In cấu trúc cột
        print(f"👉 Raw Columns: {df.columns.tolist()}")
        
        # 2. In dòng cuối cùng (Mới nhất)
        last_row = df.iloc[-1]
        print("👉 Raw Row Data (Last Row):")
        print(f"   - Time: {last_row['time']}")
        
        # QUAN TRỌNG: Kiểm tra giá trị Close
        raw_close = last_row['close']
        print(f"   - Close (Raw): {raw_close} (Kiểu: {type(raw_close)})")
        
        # 3. Mô phỏng logic xử lý
        price = float(raw_close)
        if price < 500: # Logic nhân 1000 nếu giá nhỏ (đơn vị nghìn)
            price *= 1000
            print(f"   => Sau khi nhân 1000: {price}")
        else:
            print(f"   => Giữ nguyên: {price}")

    except Exception as e:
        print(f"❌ Lỗi gọi TCBS: {e}")

if __name__ == "__main__":
    print("🔍 BẮT ĐẦU SO SÁNH CẤU TRÚC DỮ LIỆU...")
    test_vci()
    test_tcbs()
    print("\n✅ HOÀN TẤT.")