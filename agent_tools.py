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

# Vnstock 4.x Unified API
from vnstock import Quote, Finance, Company, Trading
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
        self.tools = {}
        self.schema = []

    def register(self, name=None):
        """
        Decorator đăng ký tool.
        Tự động trích xuất tên, mô tả (docstring) và tham số (signature) để tạo Schema.
        """
        def decorator(func):
            tool_name = name or func.__name__

            docstring = inspect.getdoc(func) or ""
            description = docstring.strip().split("\n")[0]

            sig = inspect.signature(func)
            params_schema = {
                "type": "object",
                "properties": {},
                "required": []
            }

            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'cls'):
                    continue

                param_type = "string"
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == float:
                    param_type = "number"
                elif param.annotation == bool:
                    param_type = "boolean"

                enum_values = None
                if hasattr(param.annotation, "__origin__") and param.annotation.__origin__ is Literal:
                    enum_values = list(param.annotation.__args__)

                prop_config = {
                    "type": param_type,
                    "description": f"Tham số {param_name}"
                }
                if enum_values:
                    prop_config["enum"] = enum_values

                params_schema["properties"][param_name] = prop_config

                if param.default == inspect.Parameter.empty:
                    params_schema["required"].append(param_name)

            self.schema.append({
                "name": tool_name,
                "description": description,
                "parameters": params_schema
            })

            @wraps(func)
            async def wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    error_msg = f"Lỗi thực thi tool {tool_name}: {str(e)}"
                    if isinstance(e, KeyError) and str(e) == "'data'":
                        error_msg = f"Lỗi thực thi tool {tool_name}: Lấy dữ liệu từ API vnstock thất bại (có thể do mã không có dữ liệu hoặc bị API chặn rate limit)."
                    log.error(error_msg)
                    return json.dumps({"error": error_msg}, ensure_ascii=False)

            self.tools[tool_name] = wrapper
            return wrapper
        return decorator

# Khởi tạo Registry
registry = ToolRegistry()

# ==============================================================================
# 2. HELPER FUNCTIONS
# ==============================================================================

def _df_to_markdown(df: pd.DataFrame, limit: int = 5) -> str:
    """Chuyển DataFrame sang Markdown Table."""
    if df is None or df.empty:
        return "Không có dữ liệu."

    df_limited = df.head(limit).copy()
    df_limited = df_limited.replace([np.inf, -np.inf, np.nan], "-")

    for col in df_limited.columns:
        col_str = str(col).lower()
        if 'time' in col_str or 'date' in col_str:
            try:
                df_limited[col] = pd.to_datetime(df_limited[col]).dt.strftime('%d/%m/%Y')
            except:
                pass

    try:
        return df_limited.to_markdown(index=False)
    except ImportError:
        return df_limited.to_string(index=False)


def _parse_gso_custom_csv(file_path: str, keywords: list) -> str:
    """Đọc file CSV GSO (định dạng custom với separator 'SHEET:') và trích xuất phần liên quan."""
    if not os.path.exists(file_path):
        return "Chưa có dữ liệu báo cáo GSO."

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        sections = content.split("====================")
        relevant_sections = []

        for section in sections:
            lines = section.strip().split('\n')
            if not lines:
                continue

            header = lines[0]
            body = "\n".join(lines[1:])

            is_match = False
            section_lower = section.lower()
            for kw in keywords:
                if kw.lower() in section_lower:
                    is_match = True
                    break

            if is_match:
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
async def tool_get_market_price(symbol: str):
    """
    Lấy thông tin giá, % thay đổi, tổng khối lượng và tổng giá trị giao dịch.
    Sử dụng vnstock.Trading.price_board (vnstock 4.x flat columns).
    """
    symbol = symbol.upper()
    try:
        # vnstock 4.x: Trading(source="KBS") → flat columns (không còn tuple)
        trading = Trading(symbol=symbol, source="VCI")
        df = await asyncio.to_thread(trading.price_board, symbols_list=[symbol])

        if df is None or df.empty:
            log.warning(f"Không có dữ liệu price_board cho {symbol}")
            return json.dumps({"error": f"Không có dữ liệu cho mã {symbol}"})

        row = df.iloc[0]

        def _get_val(row, key_name):
            if key_name in row.index:
                val = row[key_name]
                return val if pd.notna(val) else 0
            for idx in row.index:
                if isinstance(idx, tuple) and len(idx) > 1 and idx[-1] == key_name:
                    val = row[idx]
                    return val if pd.notna(val) else 0
            return 0

        # vnstock 4.x flat or tuple columns
        close_price  = _get_val(row, 'match_price') or _get_val(row, 'close_price')
        ref_price    = _get_val(row, 'reference_price') or _get_val(row, 'ref_price')
        pct_change   = _get_val(row, 'percent_change')
        if pct_change == 0 and ref_price > 0:
            pct_change = ((close_price - ref_price) / ref_price) * 100

        vol          = _get_val(row, 'accumulated_volume') or _get_val(row, 'volume_accumulated')
        total_value  = _get_val(row, 'accumulated_value') or _get_val(row, 'total_value')

        if total_value > 0 and total_value < 1_000_000_000:
            # VCI may return value in absolute VND or millions. Let's make sure it's VND.
            # Usually absolute value is > 1B during day. If it's small, it might be in millions.
            # But let's keep it direct.
            pass

        # Fallback nếu close_price = 0
        if close_price == 0:
            close_price = ref_price

        result = {
            "symbol": symbol,
            "price": int(close_price),
            "change_percent": round(float(pct_change), 2),
            "accumulated_volume": int(vol),
            "accumulated_value": int(total_value)
        }
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        log.error(f"Lỗi tool_get_market_price({symbol}): {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@registry.register(name="get_fundamentals")
async def tool_get_fundamentals(symbol: str):
    """Lấy các chỉ số định giá P/E, P/B, EPS."""
    symbol = symbol.upper().strip()
    manual_val = await asyncio.to_thread(fetch_manual_pe_pb, symbol)

    if manual_val:
        pe_str = f"{manual_val.pe:.2f}x" if manual_val.pe is not None else "N/A"
        pb_str = f"{manual_val.pb:.2f}x" if manual_val.pb is not None else "N/A"
        eps_str = f"{manual_val.eps_ttm:,.0f} VND" if manual_val.eps_ttm is not None else "N/A"
        bvps_str = f"{manual_val.bvps:,.0f} VND" if manual_val.bvps is not None else "N/A"

        return (
            f"**Chỉ số cơ bản {symbol}**:\n"
            f"- P/E: {pe_str}\n"
            f"- P/B: {pb_str}\n"
            f"- EPS (TTM): {eps_str}\n"
            f"- Book Value: {bvps_str}\n"
            f"(Dữ liệu cập nhật: {manual_val.computed_at})"
        )
    return "Không tính được định giá (thiếu dữ liệu)."


@registry.register(name="get_company_profile")
async def tool_get_company_profile(symbol: str) -> str:
    """
    Lấy thông tin tổng quan, mô hình kinh doanh, vị thế của doanh nghiệp.
    Kết hợp (Merge) dữ liệu từ Redis Cache (AI Profile) và API Vnstock.
    """
    try:
        symbol = symbol.upper().strip()

        async def fetch_from_redis():
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
            """Lấy dữ liệu hành chính từ API Vnstock 4.x"""
            try:
                # vnstock 4.x: Company(source="VCI") - VCI có overview đầy đủ hơn KBS
                company = Company(symbol=symbol, source='VCI')
                df = await asyncio.to_thread(company.overview)
                if df is not None and not df.empty:
                    data = df.iloc[0].to_dict()
                    return {
                        "tên_đầy_đủ": data.get("organ_name"),
                        "tên_ngắn": data.get("organ_short_name"),
                        "ngành_nghề": data.get("sector"),
                        "sàn_giao_dịch": data.get("com_group_code"),
                        "ngày_niêm_yết": data.get("listing_date"),
                        "mô_tả": data.get("company_profile")
                    }
            except Exception as e:
                err_msg = str(e)
                if isinstance(e, KeyError) and err_msg == "'data'":
                    err_msg = "Lấy dữ liệu từ API vnstock thất bại (KeyError: 'data')"
                log.warning(f"API profile fetch error: {err_msg}")
            return {}

        ai_data, api_data = await asyncio.gather(fetch_from_redis(), fetch_from_api())

        if not ai_data and not api_data:
            return json.dumps({"error": f"Không tìm thấy thông tin cho {symbol}"})

        combined_data = {**api_data, **ai_data}
        combined_data["nguồn_dữ_liệu"] = []
        if api_data:
            combined_data["nguồn_dữ_liệu"].append("API Realtime")
        if ai_data:
            combined_data["nguồn_dữ_liệu"].append("AI Analysis Cache")

        return json.dumps(combined_data, ensure_ascii=False)

    except Exception as e:
        log.error(f"Error tool_get_company_profile: {e}")
        return json.dumps({"error": "Lỗi hệ thống khi lấy hồ sơ"})


@registry.register(name="get_technical_indicators")
async def tool_get_technical_indicators(symbol: str):
    """
    Tính toán các chỉ báo kỹ thuật (RSI, MACD, EMA, Bollinger Bands) dựa trên dữ liệu lịch sử.
    """
    symbol = symbol.upper().strip()
    try:
        # vnstock 4.x: Quote(symbol, source='KBS').history(length='1Y', interval='1D')
        quote = Quote(symbol=symbol, source='VCI')
        df = await asyncio.to_thread(
            quote.history,
            length='1Y',
            interval='1D'
        )

        if df is None or df.empty or len(df) < 30:
            return f"Không đủ dữ liệu lịch sử để tính chỉ báo cho {symbol}."

        # Chuẩn hóa cột time
        if 'time' not in df.columns and 'tradingDate' in df.columns:
            df = df.rename(columns={'tradingDate': 'time'})

        df['time'] = pd.to_datetime(df['time'])
        df = df.set_index('time').sort_index()
        df['close'] = pd.to_numeric(df['close'])

        # Tính toán chỉ báo bằng pandas_ta
        df['EMA_20']  = df.ta.ema(length=20)
        df['EMA_50']  = df.ta.ema(length=50)
        df['EMA_200'] = df.ta.ema(length=200)
        df['RSI_14']  = df.ta.rsi(length=14)

        macd   = df.ta.macd(fast=12, slow=26, signal=9)
        bbands = df.ta.bbands(length=20, std=2)

        last      = df.iloc[-1]
        last_macd = macd.iloc[-1]
        last_bb   = bbands.iloc[-1]

        current_price = last['close']
        # KBS trả giá đơn vị nghìn đồng → nhân 1000 nếu < 500
        if current_price < 500:
            current_price *= 1000

        result = {
            "symbol": symbol,
            "date": df.index[-1].strftime('%Y-%m-%d'),
            "price": current_price,
            "indicators": {
                "RSI_14": round(last['RSI_14'], 2) if not pd.isna(last['RSI_14']) else None,
                "EMA_20": round(last['EMA_20'], 0)  if not pd.isna(last['EMA_20'])  else None,
                "EMA_50": round(last['EMA_50'], 0)  if not pd.isna(last['EMA_50'])  else None,
                "EMA_200": round(last['EMA_200'], 0) if not pd.isna(last['EMA_200']) else None,
                "MACD": {
                    "line":   round(last_macd['MACD_12_26_9'],  2) if not pd.isna(last_macd['MACD_12_26_9'])  else None,
                    "signal": round(last_macd['MACDs_12_26_9'], 2) if not pd.isna(last_macd['MACDs_12_26_9']) else None,
                    "hist":   round(last_macd['MACDh_12_26_9'], 2) if not pd.isna(last_macd['MACDh_12_26_9']) else None
                },
                "BollingerBands": {
                    "upper": round(last_bb['BBU_20_2.0'], 0) if not pd.isna(last_bb['BBU_20_2.0']) else None,
                    "lower": round(last_bb['BBL_20_2.0'], 0) if not pd.isna(last_bb['BBL_20_2.0']) else None
                }
            },
            "trend_summary": ""
        }

        trends = []
        if result["indicators"]["EMA_20"]:
            if current_price > result["indicators"]["EMA_20"]:
                trends.append("Giá > EMA20 (Ngắn hạn Tăng)")
            else:
                trends.append("Giá < EMA20 (Ngắn hạn Giảm)")

        if result["indicators"]["RSI_14"]:
            if result["indicators"]["RSI_14"] > 70:
                trends.append("RSI Quá mua")
            elif result["indicators"]["RSI_14"] < 30:
                trends.append("RSI Quá bán")

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
        # vnstock 4.x: Quote(symbol, source='KBS') cho index
        quote = Quote(symbol=index_name, source='VCI')
        df = await asyncio.to_thread(quote.history, length='5D', interval='1D')

        if df is None or df.empty:
            return f"Không lấy được dữ liệu {index_name}"

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last

        change = last['close'] - prev['close']
        pct    = (change / prev['close']) * 100 if prev['close'] > 0 else 0

        return json.dumps({
            "index":   index_name,
            "current": last['close'],
            "change":  round(change, 2),
            "percent": round(pct, 2),
            "volume":  int(last['volume']),
            "date":    str(last.get('time', ''))
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
    # vnstock 4.x: Finance(source='VCI') - VCI có nhiều cột hơn KBS cho financial reports
    finance = Finance(symbol=symbol, source='VCI')

    func_map = {
        'income':  finance.income_statement,
        'balance': finance.balance_sheet,
        'cash':    finance.cash_flow,
        'ratio':   finance.ratio
    }

    if report_type not in func_map:
        return "Loại báo cáo không hợp lệ (chọn: income, balance, cash, ratio)."

    df = await asyncio.to_thread(func_map[report_type], period=period, lang='vi')

    # Sort descending để lấy kỳ mới nhất
    if df is not None and not df.empty:
        sort_cols = []
        if 'year' in df.columns and 'quarter' in df.columns:
            sort_cols = ['year', 'quarter']
        elif 'Năm' in df.columns and 'Kỳ' in df.columns:
            sort_cols = ['Năm', 'Kỳ']

        if sort_cols:
            try:
                for col in sort_cols:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.sort_values(by=sort_cols, ascending=[False, False])
            except:
                pass

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
    try:
        pattern = os.path.join(GSO_DATA_DIR, "MACRO_*.csv")
        files = glob.glob(pattern)

        if not files:
            return "Hiện chưa có dữ liệu báo cáo vĩ mô nào trong hệ thống (GSO_Data trống)."

        latest_file = sorted(files)[-1]
        file_name = os.path.basename(latest_file)
    except Exception as e:
        return f"Lỗi truy cập dữ liệu GSO: {str(e)}"

    # Keyword khớp với tên SHEET trong file CSV (cả tiếng Việt không dấu và có dấu)
    keywords_map = {
        'gdp':               ['GDP', 'tổng sản phẩm', 'tăng trưởng', 'quy mô nền kinh tế', 'tang truong'],
        'cpi':               ['CPI', 'giá tiêu dùng', 'lạm phát', 'giá vàng', 'đô la', 'gia tieu dung'],
        'industry':          ['IIP', 'IIPthang', 'sản xuất công nghiệp', 'SPCN', 'san xuat cong nghiep'],
        'trade':             ['xuất khẩu', 'nhập khẩu', 'cán cân', 'XK', 'NK', 'xuat khau', 'nhap khau'],
        'fdi':               ['FDI', 'đầu tư nước ngoài', 'dau tu nuoc ngoai'],
        'retail':            ['bán lẻ', 'doanh thu dịch vụ', 'tiêu dùng', 'Tongmuc', 'ban le'],
        'tourism':           ['khách quốc tế', 'du lịch', 'vận tải', 'KQT', 'VT HK', 'khach quoc te'],
        'enterprise':        ['doanh nghiệp', 'đăng ký thành lập', 'giải thể', 'DN', 'doanh nghiep'],
        'agriculture':       ['nông nghiệp', 'lâm nghiệp', 'thủy sản', 'Nong nghiep', 'nong nghiep'],
        'public_investment':  ['vốn đầu tư', 'ngân sách', 'VDT', 'von dau tu']
    }

    target_keywords = keywords_map.get(topic, [])
    if not target_keywords:
        return f"Chủ đề '{topic}' không hợp lệ."

    data = await asyncio.to_thread(_parse_gso_custom_csv, latest_file, target_keywords)
    return f"**Nguồn: {file_name}**\n\n{data}"


@registry.register(name="get_stock_events")
async def tool_get_stock_events(symbol: str):
    """Lấy lịch sự kiện: Cổ tức, phát hành thêm, họp ĐHCĐ."""
    symbol = symbol.upper().strip()
    # vnstock 4.x: Company.events() là method (gọi có ngoặc)
    # VCI có dữ liệu events đầy đủ hơn KBS
    company = Company(symbol=symbol, source='VCI')
    df = await asyncio.to_thread(company.events)

    if df is not None and not df.empty:
        # Cột vnstock 4.x Company.events() với VCI
        priority_cols = ['event_title_vi', 'public_date', 'display_date1', 'action_type_vi', 'exercise_ratio', 'value_per_share']
        valid_cols = [c for c in priority_cols if c in df.columns]
        if not valid_cols:
            valid_cols = df.columns[:5].tolist()

        return _df_to_markdown(df[valid_cols], limit=5)

    return "Không có sự kiện sắp tới."


@registry.register(name="get_stock_news")
async def tool_get_stock_news(symbol: str):
    """Tìm kiếm tin tức báo chí mới nhất liên quan trực tiếp đến mã cổ phiếu."""
    symbol = symbol.upper().strip()
    # vnstock 4.x: Company.news() là method
    company = Company(symbol=symbol, source='VCI')
    df = await asyncio.to_thread(company.news)

    if df is not None and not df.empty:
        priority_cols = ['news_title', 'public_date', 'news_source']
        valid_cols = [c for c in priority_cols if c in df.columns]
        if not valid_cols:
            valid_cols = df.columns[:3].tolist()

        return _df_to_markdown(df[valid_cols], limit=5)

    return "Không tìm thấy tin tức liên quan."


@registry.register(name="get_industry_peers")
async def tool_get_industry_peers(symbol: str):
    """Tìm danh sách các mã cổ phiếu khác trong cùng nhóm ngành (Dữ liệu từ Screener)."""
    symbol = symbol.upper().strip()

    hist_data = await asyncio.to_thread(get_historical_valuation_from_redis)
    stocks_map = hist_data.get("stocks", {}) if hist_data else {}

    my_sector = None
    if symbol in stocks_map:
        my_sector = stocks_map[symbol].get("sector")

    if not my_sector:
        try:
            sectors_path = os.path.join(BASE_DIR, "sectors.json")
            with open(sectors_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if symbol in data:
                    val = data[symbol]
                    my_sector = val.get("sector") if isinstance(val, dict) else val
        except:
            pass

    if not my_sector:
        return f"Không xác định được ngành của mã {symbol}."

    peers = [
        s for s, info in stocks_map.items()
        if s != symbol and info.get("sector") == my_sector
    ]
    peers.sort()

    if not peers:
        return f"Ngành '{my_sector}' chưa có mã tương tự nào đạt chuẩn thanh khoản."

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

AGENT_TOOLS_SCHEMA = registry.schema
TOOL_MAPPING = registry.tools

if __name__ == "__main__":
    print(json.dumps(AGENT_TOOLS_SCHEMA, indent=2, ensure_ascii=False))
