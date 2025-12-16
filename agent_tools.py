# agent_tools.py
import logging
import json
import datetime
import asyncio
import pandas as pd
import numpy as np
import inspect
import os
import glob
from functools import wraps
from typing import Optional, Literal

from vnstock import Quote, Finance, Company, Vnstock, Trading
from manual_valuation import fetch_manual_pe_pb
from db_utils import get_historical_valuation_from_redis
from profile_cache import make_profile_cache_key, get_profile_from_redis

# --- CẤU HÌNH PANDAS TA (FIX LỖI NUMPY 2.0) ---
if not hasattr(np, "NaN"):
    np.NaN = np.nan
import pandas_ta as ta

# Cấu hình Logger riêng cho Tool
log = logging.getLogger("AgentTools")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GSO_DATA_DIR = os.path.join(BASE_DIR, "GSO_Data")

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
    
def _parse_gso_custom_csv(file_path: str, keywords: list[str]) -> str:
    """
    Đọc file CSV GSO (định dạng custom với separator 'SHEET:') và trích xuất phần liên quan.
    """
    if not os.path.exists(file_path):
        return "Chưa có dữ liệu báo cáo GSO."

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Tách file thành các sheet dựa trên separator
        # Format: ==================== SHEET: Ten_Sheet ====================
        sections = content.split("====================")
        
        relevant_sections = []
        
        for section in sections:
            # Kiểm tra xem section này có chứa từ khóa user cần không
            # Section header thường nằm ở dòng đầu sau khi split
            lines = section.strip().split('\n')
            if not lines: continue
            
            header = lines[0] # Vd: SHEET: 16.CPI
            body = "\n".join(lines[1:])
            
            # Logic khớp từ khóa: nếu từ khóa nằm trong Header hoặc Body
            is_match = False
            section_lower = section.lower()
            for kw in keywords:
                if kw.lower() in section_lower:
                    is_match = True
                    break
            
            if is_match:
                # Clean bớt các dòng trống thừa
                cleaned_body = "\n".join([line for line in body.split('\n') if line.strip()])
                relevant_sections.append(f"### {header}\n{cleaned_body}")

        if not relevant_sections:
            return "Không tìm thấy dữ liệu vĩ mô phù hợp với từ khóa trong báo cáo tháng này."
            
        return "\n\n".join(relevant_sections)

    except Exception as e:
        log.error(f"GSO Parse Error: {e}")
        return f"Lỗi đọc file dữ liệu vĩ mô: {str(e)}"

# ==============================================================================
# 3. CÁC TOOL (Định nghĩa & Đăng ký)
# ==============================================================================

@registry.register(name="get_market_price")
def tool_get_market_price(symbol: str):
    """
    Lấy thông tin giá, % thay đổi, tổng khối lượng và tổng giá trị giao dịch.
    Sử dụng vnstock.Trading.price_board
    """
    symbol = symbol.upper()
    try:
        # 1. Khởi tạo Trading & Gọi API
        # Dùng chính symbol để init class, tránh phụ thuộc mã cứng khác
        trading = Trading(symbol=symbol)
        df = trading.price_board(symbols_list=[symbol])

        # 2. Kiểm tra dữ liệu rỗng
        if df is None or df.empty:
            print(f"⚠️ Không có dữ liệu cho mã {symbol}")
            return None

        # 3. Lấy dòng dữ liệu đầu tiên
        row = df.iloc[0]

        # 4. Trích xuất dữ liệu (Xử lý Key dạng Tuple)
        
        # --- A. GIÁ (Price) ---
        # Lấy giá khớp lệnh hiện tại.
        price = row.get(('match', 'match_price'), 0)
        ref_price = row.get(('match', 'reference_price'), 0)

        # Fallback: Nếu giá khớp = 0 (đầu phiên chưa khớp), dùng giá tham chiếu để hiển thị
        if price == 0:
            price = ref_price

        # --- B. % THAY ĐỔI (Change Percent) ---
        change_pct = 0.0
        if ref_price > 0:
            change_pct = ((price - ref_price) / ref_price) * 100

        # --- C. KHỐI LƯỢNG (Volume) ---
        # Key: ('match', 'accumulated_volume')
        vol = row.get(('match', 'accumulated_volume'), 0)

        # --- D. GIÁ TRỊ (Value) ---
        # Key: ('match', 'accumulated_value'). Đơn vị API trả về thường là Triệu đồng.
        val_raw = row.get(('match', 'accumulated_value'), 0)
        val = val_raw * 1_000_000 # Nhân 1 triệu để về đơn vị Đồng (VND)

        # 5. Trả về kết quả Clean
        result = {
            "symbol": symbol,
            "price": int(price),
            "change_percent": round(change_pct, 2),
            "accumulated_volume": int(vol),
            "accumulated_value": int(val)
        }

        return result

    except Exception as e:
        print(f"❌ Lỗi tool_get_market_price({symbol}): {e}")
        return None

@registry.register(name="get_fundamentals")
async def tool_get_fundamentals(symbol: str):
    """Lấy các chỉ số định giá P/E, P/B, EPS."""
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
    Lấy thông tin tổng quan, mô hình kinh doanh, vị thế của doanh nghiệp.
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

@registry.register(name="get_technical_indicators")
async def tool_get_technical_indicators(symbol: str):
    """
    Tính toán các chỉ báo kỹ thuật (RSI, MACD, EMA, Bollinger Bands) dựa trên dữ liệu lịch sử.
    Thay thế cho 'run_tech_agent'.
    """
    symbol = symbol.upper().strip()
    try:
        # 1. Lấy dữ liệu lịch sử (365 ngày để đủ tính EMA200)
        end_date = datetime.datetime.now()
        start_date = end_date - datetime.timedelta(days=365)
        
        stock = Vnstock().stock(symbol=symbol, source='VCI')
        df = await asyncio.to_thread(
            stock.quote.history, 
            start=start_date.strftime("%Y-%m-%d"), 
            end=end_date.strftime("%Y-%m-%d"), 
            interval='1D'
        )
        
        if df is None or df.empty or len(df) < 30:
            return f"Không đủ dữ liệu lịch sử để tính chỉ báo cho {symbol}."

        # Chuẩn hóa cột
        if 'time' not in df.columns and 'tradingDate' in df.columns:
            df = df.rename(columns={'tradingDate': 'time'})
        
        df['time'] = pd.to_datetime(df['time'])
        df = df.set_index('time').sort_index()
        df['close'] = pd.to_numeric(df['close'])

        # 2. Tính toán chỉ báo bằng pandas_ta
        # EMA
        df['EMA_20'] = df.ta.ema(length=20)
        df['EMA_50'] = df.ta.ema(length=50)
        df['EMA_200'] = df.ta.ema(length=200)
        
        # RSI
        df['RSI_14'] = df.ta.rsi(length=14)
        
        # MACD (12, 26, 9)
        macd = df.ta.macd(fast=12, slow=26, signal=9)
        # macd trả về 3 cột: MACD_12_26_9, MACDh_12_26_9 (hist), MACDs_12_26_9 (signal)
        
        # Bollinger Bands (20, 2)
        bbands = df.ta.bbands(length=20, std=2)
        # bbands trả về BBL (Lower), BBM (Mid), BBU (Upper)

        # 3. Lấy bản ghi mới nhất
        last = df.iloc[-1]
        last_macd = macd.iloc[-1]
        last_bb = bbands.iloc[-1]
        
        current_price = last['close']
        # Fix giá nếu cần
        if current_price < 500: current_price *= 1000

        # 4. Tổng hợp kết quả
        result = {
            "symbol": symbol,
            "date": df.index[-1].strftime('%Y-%m-%d'),
            "price": current_price,
            "indicators": {
                "RSI_14": round(last['RSI_14'], 2) if not pd.isna(last['RSI_14']) else None,
                "EMA_20": round(last['EMA_20'], 0) if not pd.isna(last['EMA_20']) else None,
                "EMA_50": round(last['EMA_50'], 0) if not pd.isna(last['EMA_50']) else None,
                "EMA_200": round(last['EMA_200'], 0) if not pd.isna(last['EMA_200']) else None,
                "MACD": {
                    "line": round(last_macd['MACD_12_26_9'], 2) if not pd.isna(last_macd['MACD_12_26_9']) else None,
                    "signal": round(last_macd['MACDs_12_26_9'], 2) if not pd.isna(last_macd['MACDs_12_26_9']) else None,
                    "hist": round(last_macd['MACDh_12_26_9'], 2) if not pd.isna(last_macd['MACDh_12_26_9']) else None
                },
                "BollingerBands": {
                    "upper": round(last_bb['BBU_20_2.0'], 0) if not pd.isna(last_bb['BBU_20_2.0']) else None,
                    "lower": round(last_bb['BBL_20_2.0'], 0) if not pd.isna(last_bb['BBL_20_2.0']) else None
                }
            },
            "trend_summary": "" # Để AI tự suy luận dựa trên số liệu
        }
        
        # Thêm một chút gợi ý xu hướng cho AI
        trends = []
        if result["indicators"]["EMA_20"]:
            if current_price > result["indicators"]["EMA_20"]: trends.append("Giá > EMA20 (Ngắn hạn Tăng)")
            else: trends.append("Giá < EMA20 (Ngắn hạn Giảm)")
            
        if result["indicators"]["RSI_14"]:
            if result["indicators"]["RSI_14"] > 70: trends.append("RSI Quá mua")
            elif result["indicators"]["RSI_14"] < 30: trends.append("RSI Quá bán")
            
        result["trend_summary"] = "; ".join(trends)

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        log.error(f"Tech Tool Error: {e}")
        return f"Lỗi tính toán kỹ thuật: {str(e)}"
    
@registry.register(name="get_market_index")
async def tool_get_market_index(index_name: Literal['VNINDEX', 'VN30'] = 'VNINDEX'):
    """
    Lấy thông tin chỉ số thị trường (VNINDEX, VN30).
    """
    index_name = index_name.upper()
    try:
        quote = Quote(symbol=index_name, source='TCBS') # TCBS ổn định hơn cho Index
        df = await asyncio.to_thread(quote.history, start=(datetime.date.today() - datetime.timedelta(days=3)).strftime('%Y-%m-%d'), end=datetime.date.today().strftime('%Y-%m-%d'), interval='1D')
        
        if df is None or df.empty:
            return f"Không lấy được dữ liệu {index_name}"
            
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        
        change = last['close'] - prev['close']
        pct = (change / prev['close']) * 100 if prev['close'] > 0 else 0
        
        return json.dumps({
            "index": index_name,
            "current": last['close'],
            "change": round(change, 2),
            "percent": round(pct, 2),
            "volume": int(last['volume']),
            "date": str(last['time'] if 'time' in last else last.get('tradingDate'))
        }, ensure_ascii=False)
        
    except Exception as e:
        return f"Lỗi lấy chỉ số {index_name}: {str(e)}"

@registry.register(name="get_financial_report")
async def tool_get_financial_report(
    symbol: str, 
    report_type: Literal['income', 'balance', 'cash', 'ratio'], 
    period: Literal['year', 'quarter'] = 'quarter'
):
    """Lấy dữ liệu báo cáo tài chính chi tiết:
    Kết quả hoạt động kinh doanh (Income Statement): doanh thu, lợi nhuận, chi phí,...
    Bảng Cân Đối Kế Toán (Balance Sheet): tài sản, nợ phải trả, vốn chủ sở hữu,...
    Lưu Chuyển Tiền Tệ (Cash Flow): dòng tiền từ hoạt động kinh doanh, đầu tư, tài chính,...,
    Các Chỉ Số Tài Chính (Financial Ratios): biên lợi nhuận, ROE, ROA, hệ số nợ,...
    """
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

@registry.register(name="get_macro_data")
async def tool_get_macro_data(topic: Literal['gdp', 'cpi', 'industry', 'trade', 'fdi', 'retail', 'tourism', 'enterprise', 'agriculture', 'public_investment']):
    """
    Lấy số liệu kinh tế vĩ mô mới nhất từ báo cáo Tổng cục Thống kê (GSO).
    Dữ liệu được lấy từ file CSV trong thư mục GSO_Data.
    
    Topic mapping:
    - gdp: Tổng sản phẩm trong nước, tăng trưởng kinh tế.
    - cpi: Chỉ số giá tiêu dùng, lạm phát, giá vàng, USD.
    - industry: Chỉ số sản xuất công nghiệp (IIP), sản phẩm công nghiệp.
    - trade: Xuất nhập khẩu hàng hóa.
    - fdi: Đầu tư trực tiếp nước ngoài.
    - retail: Tổng mức bán lẻ hàng hóa và dịch vụ.
    - tourism: Khách quốc tế, vận tải hành khách.
    - enterprise: Tình hình đăng ký doanh nghiệp.
    - agriculture: Sản xuất nông nghiệp.
    - public_investment: Vốn đầu tư công.
    """
    # 1. Tìm file MACRO mới nhất
    try:
        # Tìm file bắt đầu bằng MACRO_ và có đuôi .csv
        pattern = os.path.join(GSO_DATA_DIR, "MACRO_*.csv")
        files = glob.glob(pattern)
        
        if not files:
            return "Hiện chưa có dữ liệu báo cáo vĩ mô nào trong hệ thống (GSO_Data trống)."
            
        # Sắp xếp để lấy file mới nhất (theo tên file có chứa ngày tháng năm)
        latest_file = sorted(files)[-1]
        file_name = os.path.basename(latest_file)
    except Exception as e:
        return f"Lỗi truy cập dữ liệu GSO: {str(e)}"

    # 2. Map Topic sang Keyword tìm kiếm trong file CSV
    # Các keyword này dựa trên tên SHEET trong file CSV bạn cung cấp
    keywords_map = {
        'gdp': ['GDP', 'tổng sản phẩm', 'tăng trưởng', 'quy mô nền kinh tế'],
        'cpi': ['CPI', 'giá tiêu dùng', 'lạm phát', 'giá vàng', 'đô la'],
        'industry': ['IIP', 'sản xuất công nghiệp', 'SPCN', 'công nghiệp'],
        'trade': ['xuất khẩu', 'nhập khẩu', 'cán cân', 'XK', 'NK'],
        'fdi': ['FDI', 'đầu tư nước ngoài'],
        'retail': ['bán lẻ', 'doanh thu dịch vụ', 'tiêu dùng', 'Tongmuc'],
        'tourism': ['khách quốc tế', 'du lịch', 'vận tải', 'KQT', 'VT HK'],
        'enterprise': ['doanh nghiệp', 'đăng ký thành lập', 'giải thể', 'DN'],
        'agriculture': ['nông nghiệp', 'lâm nghiệp', 'thủy sản'],
        'public_investment': ['vốn đầu tư', 'ngân sách', 'VDT']
    }
    
    target_keywords = keywords_map.get(topic, [])
    if not target_keywords:
        return f"Chủ đề '{topic}' không hợp lệ."

    # 3. Parse và trả về dữ liệu
    data = await asyncio.to_thread(_parse_gso_custom_csv, latest_file, target_keywords)
    
    return f"**Nguồn: {file_name}**\n\n{data}"

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

