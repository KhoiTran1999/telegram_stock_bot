# agent_tools.py
import logging
import json
import datetime
import asyncio
import pandas as pd
import numpy as np
import inspect
from functools import wraps
from typing import Optional, Literal

from vnstock import Quote, Finance, Company
from manual_valuation import fetch_manual_pe_pb
from db_utils import get_historical_valuation_from_redis
from profile_cache import make_profile_cache_key, get_profile_from_redis

# Cấu hình Logger riêng cho Tool
log = logging.getLogger("AgentTools")

# ==============================================================================
# 1. TOOL REGISTRY (Core Logic)
# ==============================================================================

class ToolRegistry:
    def __init__(self):
        self.tools = {}       # Thay thế TOOL_MAPPING cũ
        self.schema = []      # Thay thế AGENT_TOOLS_SCHEMA cũ

    def register(self, name=None):
        """
        Decorator để đăng ký tool. 
        Tự động trích xuất tên, mô tả (docstring) và tham số (signature) để tạo Schema.
        """
        def decorator(func):
            # 1. Tên Tool
            tool_name = name or func.__name__
            
            # 2. Mô tả (Lấy dòng đầu tiên của Docstring)
            docstring = inspect.getdoc(func) or ""
            description = docstring.strip().split("\n")[0]
            
            # 3. Phân tích tham số (Signature)
            sig = inspect.signature(func)
            params_schema = {
                "type": "object",
                "properties": {},
                "required": []
            }
            
            for param_name, param in sig.parameters.items():
                # Bỏ qua tham số self/cls nếu có
                if param_name in ('self', 'cls'):
                    continue
                    
                # Mapping kiểu dữ liệu Python -> JSON Schema
                param_type = "string"
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == float:
                    param_type = "number"
                elif param.annotation == bool:
                    param_type = "boolean"
                
                # Xử lý Enum (Literal)
                enum_values = None
                if hasattr(param.annotation, "__origin__") and param.annotation.__origin__ is Literal:
                    enum_values = list(param.annotation.__args__)
                
                prop_config = {
                    "type": param_type,
                    "description": f"Tham số {param_name}" # Có thể cải thiện nếu dùng docstring parser xịn hơn
                }
                if enum_values:
                    prop_config["enum"] = enum_values
                    
                params_schema["properties"][param_name] = prop_config
                
                # Nếu không có default value -> Bắt buộc
                if param.default == inspect.Parameter.empty:
                    params_schema["required"].append(param_name)

            # 4. Lưu vào Registry
            self.tools[tool_name] = func
            self.schema.append({
                "name": tool_name,
                "description": description,
                "parameters": params_schema
            })

            # 5. Wrapper để bắt lỗi chung (Optional)
            @wraps(func)
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    error_msg = f"Lỗi thực thi tool {tool_name}: {str(e)}"
                    log.error(error_msg)
                    return json.dumps({"error": error_msg}, ensure_ascii=False)
            
            return wrapper
        return decorator

# Khởi tạo Registry
registry = ToolRegistry()

# --- CÁC HÀM HELPER ---

# ==============================================================================
# 2. HELPER FUNCTIONS
# ==============================================================================

def _df_to_markdown(df: pd.DataFrame, limit: int = 5) -> str:
    """
    Chuyển DataFrame sang Markdown Table để tiết kiệm token và AI dễ đọc hơn JSON raw.
    """
    if df is None or df.empty:
        return "Không có dữ liệu."
    
    # Giới hạn số dòng
    df_limited = df.head(limit).copy()
    
    # Xử lý NaN/Inf
    df_limited = df_limited.replace([np.inf, -np.inf, np.nan], "-")
    
    # Format ngày tháng nếu có (ví dụ cột 'time', 'date')
    for col in df_limited.columns:
        if 'time' in col.lower() or 'date' in col.lower():
            try:
                df_limited[col] = pd.to_datetime(df_limited[col]).dt.strftime('%d/%m/%Y')
            except: pass
            
    try:
        return df_limited.to_markdown(index=False)
    except ImportError:
        # Fallback nếu thiếu thư viện tabulate
        return df_limited.to_string(index=False)

# ==============================================================================
# 3. CÁC TOOL (Định nghĩa & Đăng ký)
# ==============================================================================

@registry.register(name="get_market_price")
async def tool_get_market_price(symbol: str):
    """Lấy giá khớp lệnh, khối lượng và biến động hiện tại của một mã cổ phiếu (VD: HPG)."""
    symbol = symbol.upper().strip()
    try:
        quote = Quote(symbol=symbol, source='VCI')
        # Lấy data nhỏ gọn nhất
        df = await asyncio.to_thread(quote.price_board, [symbol])
        
        if df is None or df.empty:
            return f"Không tìm thấy dữ liệu giá cho {symbol}"
        
        row = df.iloc[0]
        # Mapping các trường quan trọng từ vnstock
        price = row.get(('match', 'match_price')) or row.get('match_price')
        change = row.get(('match', 'match_change')) or row.get('change')
        pct = row.get(('match', 'match_change_percent')) or row.get('change_percent')
        vol = row.get(('match', 'match_vol')) or row.get('volume')
        
        # Xử lý đơn vị giá (x1000 nếu < 500) - logic cũ của bạn
        if price and price < 500: price *= 1000
        if change and abs(change) < 50: change *= 1000 # Giả định change cũng bị lệch
        
        return json.dumps({
            "symbol": symbol,
            "price": price,
            "change_val": change,
            "change_pct": pct,
            "volume": vol
        }, ensure_ascii=False)
    except Exception as e:
        return f"Lỗi dữ liệu giá: {str(e)}"

@registry.register(name="get_fundamentals")
async def tool_get_fundamentals(symbol: str):
    """Lấy các chỉ số định giá P/E, P/B, EPS để đánh giá đắt rẻ."""
    symbol = symbol.upper().strip()
    manual_val = await asyncio.to_thread(fetch_manual_pe_pb, symbol)
    
    if manual_val:
        # Trả về format text dễ đọc cho AI
        return (
            f"**Chỉ số cơ bản {symbol}**:\n"
            f"- P/E: {manual_val.pe:.2f}x\n"
            f"- P/B: {manual_val.pb:.2f}x\n"
            f"- EPS (TTM): {manual_val.eps_ttm:,.0f} VND\n"
            f"- Book Value: {manual_val.bvps:,.0f} VND\n"
            f"(Dữ liệu cập nhật: {manual_val.computed_at})"
        )
    return "Không tính được định giá (thiếu dữ liệu)."

@registry.register(name="get_company_profile")
async def tool_get_company_profile(symbol: str) -> str:
    """
    Lấy thông tin tổng quan doanh nghiệp.
    CHIẾN LƯỢC: Kết hợp (Merge) dữ liệu từ 2 nguồn:
    1. Redis Cache (AI Profile): Chứa phân tích sâu (Moat, Risks, Business Model...).
    2. API Vnstock (TCBS): Chứa thông tin hành chính chuẩn xác (Tên, Năm lập, Sàn, Website...).
    """
    try:
        symbol = symbol.upper().strip()
        
        # --- ĐỊNH NGHĨA 2 TASKS CHẠY SONG SONG ---
        
        async def fetch_from_redis():
            """Lấy dữ liệu phân tích từ AI Cache"""
            try:
                cache_key = make_profile_cache_key(symbol)
                cached = await asyncio.to_thread(get_profile_from_redis, cache_key)
                if cached:
                    text_json, _, is_error, _ = cached
                    if not is_error and text_json:
                        return json.loads(text_json)
            except Exception as e:
                log.warning(f"Redis profile fetch error: {e}")
            return {}

        async def fetch_from_api():
            """Lấy dữ liệu hành chính từ API"""
            try:
                company = Company(symbol=symbol, source='TCBS')
                df = await asyncio.to_thread(company.overview)
                if df is not None and not df.empty:
                    data = df.iloc[0].to_dict()
                    # Chuẩn hóa key cho dễ đọc
                    return {
                        "tên_đầy_đủ": data.get("short_name") or data.get("organ_name"),
                        "ngành_nghề": data.get("industry") or data.get("icb_name2"),
                        "loại_hình": data.get("business_type"),
                        "năm_thành_lập": data.get("established_year"),
                        "sàn_giao_dịch": data.get("exchange"),
                        "số_lượng_nhân_viên": data.get("no_employees"),
                        "website": data.get("website")
                    }
            except Exception as e:
                log.warning(f"API profile fetch error: {e}")
            return {}

        # --- CHẠY SONG SONG (CONCURRENT) ---
        # Dùng asyncio.gather để tổng thời gian = max(thời gian redis, thời gian api)
        # thay vì cộng dồn.
        ai_data, api_data = await asyncio.gather(fetch_from_redis(), fetch_from_api())

        # --- KẾT HỢP DỮ LIỆU (MERGE) ---
        if not ai_data and not api_data:
            return json.dumps({"error": f"Không tìm thấy thông tin cho {symbol}"})

        # Gộp 2 dict lại. Ưu tiên dữ liệu AI (nếu trùng key) hoặc API tùy bạn chọn.
        # Ở đây ta gộp chung vì các key thường không trùng nhau.
        # API: tên, ngành, năm...
        # AI: overview, moat, risks...
        combined_data = {**api_data, **ai_data}
        
        # Thêm metadata để debug
        combined_data["nguồn_dữ_liệu"] = []
        if api_data: combined_data["nguồn_dữ_liệu"].append("API Realtime")
        if ai_data: combined_data["nguồn_dữ_liệu"].append("AI Analysis Cache")
        
        return json.dumps(combined_data, ensure_ascii=False)

    except Exception as e:
        log.error(f"Error tool_get_company_profile: {e}")
        return json.dumps({"error": "Lỗi hệ thống khi lấy hồ sơ"})

@registry.register(name="get_financial_report")
async def tool_get_financial_report(
    symbol: str, 
    report_type: Literal['income', 'balance', 'cash', 'ratio'], 
    period: Literal['year', 'quarter'] = 'quarter'
):
    """Lấy dữ liệu báo cáo tài chính chi tiết (Doanh thu, Lợi nhuận, Tài sản...)."""
    symbol = symbol.upper().strip()
    finance = Finance(symbol=symbol, source='VCI')
    
    # Mapping tên hàm
    func_map = {
        'income': finance.income_statement,
        'balance': finance.balance_sheet,
        'cash': finance.cash_flow,
        'ratio': finance.ratio
    }
    
    if report_type not in func_map:
        return "Loại báo cáo không hợp lệ (chọn: income, balance, cash, ratio)."
    
    # Lấy dữ liệu
    df = await asyncio.to_thread(func_map[report_type], period=period, lang='vi')
    
    # Trả về bảng Markdown (lấy 4 kỳ gần nhất)
    return _df_to_markdown(df, limit=4)

@registry.register(name="get_stock_events")
async def tool_get_stock_events(symbol: str):
    """Lấy lịch sự kiện: Cổ tức, phát hành thêm, họp ĐHCĐ."""
    symbol = symbol.upper().strip()
    company = Company(symbol=symbol, source='TCBS')
    df = await asyncio.to_thread(company.events)
    
    # Chỉ lấy các cột quan trọng
    if df is not None and not df.empty:
        cols = ['exerDate', 'type', 'price', 'ratio', 'content'] # Tên cột có thể thay đổi tùy version vnstock
        # Lọc các cột tồn tại
        valid_cols = [c for c in cols if c in df.columns]
        if not valid_cols and len(df.columns) > 0:
             # Fallback nếu tên cột khác
             valid_cols = df.columns[:4]
        
        return _df_to_markdown(df[valid_cols], limit=5)
        
    return "Không có sự kiện sắp tới."

@registry.register(name="get_stock_news")
async def tool_get_stock_news(symbol: str):
    """Tìm kiếm tin tức báo chí mới nhất liên quan trực tiếp đến mã cổ phiếu."""
    symbol = symbol.upper().strip()
    company = Company(symbol=symbol, source='TCBS')
    df = await asyncio.to_thread(company.news)
    
    if df is not None and not df.empty:
        # Lấy tiêu đề và ngày
        df_clean = df[['title', 'publish_date']].copy() if 'title' in df.columns else df.iloc[:, :2]
        return _df_to_markdown(df_clean, limit=5)
        
    return "Không tìm thấy tin tức liên quan."

@registry.register(name="get_industry_peers")
async def tool_get_industry_peers(symbol: str):
    """Tìm danh sách các mã cổ phiếu khác trong cùng nhóm ngành (Dữ liệu từ Screener)."""
    symbol = symbol.upper().strip()
    
    # 1. Lấy dữ liệu Screener từ Redis
    hist_data = await asyncio.to_thread(get_historical_valuation_from_redis)
    stocks_map = hist_data.get("stocks", {}) if hist_data else {}
    
    # 2. Tìm ngành
    my_sector = None
    if symbol in stocks_map:
        my_sector = stocks_map[symbol].get("sector")
    
    # Fallback nếu không có trong Redis
    if not my_sector:
        # Logic đọc file sectors.json (bạn có thể giữ lại hoặc import từ utils)
        import os
        try:
            with open("sectors.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                if symbol in data:
                    val = data[symbol]
                    my_sector = val.get("sector") if isinstance(val, dict) else val
        except: pass
            
    if not my_sector:
        return f"Không xác định được ngành của mã {symbol}."

    # 3. Lọc Peers từ Redis (Chất lượng cao)
    peers = []
    for s, info in stocks_map.items():
        if s != symbol and info.get("sector") == my_sector:
            peers.append(s)
    
    peers.sort()
    
    if not peers:
        return f"Ngành '{my_sector}' chưa có mã tương tự nào đạt chuẩn thanh khoản."
        
    # Trả về danh sách text đơn giản
    top_peers = peers[:15]
    return json.dumps({
        "symbol": symbol,
        "sector": my_sector,
        "peers_count": len(peers),
        "top_peers": top_peers
    }, ensure_ascii=False)


# ==============================================================================
# 4. EXPORT (Cho worker.py import)
# ==============================================================================

# Schema tự động sinh ra từ decorator
AGENT_TOOLS_SCHEMA = registry.schema

# Mapping tên hàm -> function object
TOOL_MAPPING = registry.tools

if __name__ == "__main__":
    # Test Schema Generation
    print(json.dumps(AGENT_TOOLS_SCHEMA, indent=2, ensure_ascii=False))

