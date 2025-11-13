# (Gần các dòng import vnstock khác)
from vnstock import Trading, Quote, Listing, Finance, derivatives

async def _vn30f1m_get_current_price() -> float | None:
    """
    (V4) Lấy giá real-time của VN30F1M bằng module derivatives (chữ thường).
    Yêu cầu vnstock >= 3.3.0.
    """
    try:
        # 1. Dùng module 'derivatives' và hàm derivatives_price_board()
        # Chạy blocking I/O (mạng) trong một thread riêng
        df = await asyncio.to_thread(derivatives.derivatives_price_board, source='dnse') 
        
        if df is None or df.empty:
            log.warning("[VN30F1M] derivatives.derivatives_price_board() trả về rỗng.")
            return None

        # 2. Tìm dòng của 'VN30F1M'
        row = None
        if 'symbol' in df.columns:
            row = df[df['symbol'] == VN30F1M_SYMBOL]
        
        if row is None or row.empty:
            if 'name' in df.columns:
                 row = df[df['name'] == VN30F1M_SYMBOL]

        if row is None or row.empty:
            log.warning(f"[VN30F1M] Không tìm thấy {VN30F1M_SYMBOL} trong bảng giá Derivatives.")
            return None
            
        # 3. Lấy giá từ cột 'lastPrice' (hoặc 'price')
        price = row.iloc[0].get('lastPrice')
        
        if price is None:
            price = row.iloc[0].get('price') # Fallback

        if price is None:
            log.warning(f"[VN30F1M] Hàng VN30F1M không có cột 'lastPrice' hoặc 'price'.")
            return None

        return float(price)

    except Exception as e:
        log.warning(f"[VN30F1M] Lỗi khi lấy giá phái sinh (derivatives module): {e}")
        return None