# agent_tools.py
import logging
import json
import datetime
import asyncio
from vnstock import Quote, Finance, Company
from manual_valuation import fetch_manual_pe_pb
from db_utils import get_historical_valuation_from_redis
import pandas as pd
import numpy as np

# Cấu hình Logger riêng cho Tool
log = logging.getLogger("AgentTools")

# --- CÁC HÀM HELPER ---

def _clean_df_to_json(df: pd.DataFrame, limit: int = 5) -> str:
    """
    Chuyển DataFrame sang JSON, xử lý NaN và giới hạn số kỳ.
    """
    if df is None or df.empty:
        return json.dumps({"error": "Không có dữ liệu"})
    
    # 1. Giới hạn số lượng kỳ (mặc định lấy 5 kỳ gần nhất để không tràn Context Window của AI)
    # vnstock thường trả về dữ liệu mới nhất ở đầu hoặc cuối, ta lấy head(limit)
    df_limited = df.head(limit)
    
    # 2. Xử lý NaN/Inf thành null (JSON standard)
    df_clean = df_limited.replace([np.inf, -np.inf, np.nan], None)
    
    # 3. Convert sang list dict
    try:
        # orient='records' tạo ra list các object: [{"Nam": 2023, "Doanh thu": ...}, ...]
        return df_clean.to_json(orient='records', force_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Lỗi convert JSON: {str(e)}"})

# ========= 1. CÁC HÀM CÔNG CỤ (IMPLEMENTATION) ======

async def tool_get_market_price(symbol: str) -> str:
    """
    Lấy giá thị trường realtime (Khớp lệnh, Tăng giảm, Khối lượng).
    """
    try:
        symbol = symbol.upper().strip()
        # Dùng VCI cho nhanh (hoặc fallback TCBS nếu cần logic phức tạp hơn)
        quote = Quote(symbol=symbol, source='VCI')
        # Lấy 1 ngày để có data realtime
        now = datetime.datetime.now()
        df = await asyncio.to_thread(
            quote.history, 
            start=now.strftime('%Y-%m-%d'), 
            end=now.strftime('%Y-%m-%d'), 
            interval='1D'
        )
        
        if df is None or df.empty:
            return json.dumps({"error": f"Không tìm thấy dữ liệu giá cho {symbol}"})
            
        last = df.iloc[-1]
        # Fix lỗi đơn vị giá x1000 nếu cần (giống logic cũ)
        close = float(last['close'])
        if close < 500: close *= 1000
        
        # Tính % thay đổi (giả định có giá tham chiếu hoặc tự tính)
        # Ở đây lấy đơn giản, nếu muốn chính xác cần lấy thêm ref_price
        # Tạm thời trả về giá close và volume
        return json.dumps({
            "symbol": symbol,
            "price": close,
            "volume": float(last['volume']),
            "time": str(last['time']) if 'time' in last else "N/A"
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"Error tool_get_market_price: {e}")
        return json.dumps({"error": "Lỗi hệ thống khi lấy giá"})

async def tool_get_fundamentals(symbol: str) -> str:
    """
    Lấy chỉ số cơ bản P/E, P/B (Dùng module manual_valuation có sẵn).
    """
    try:
        symbol = symbol.upper().strip()
        # Gọi hàm có sẵn trong manual_valuation.py
        manual_val = await asyncio.to_thread(fetch_manual_pe_pb, symbol)
        
        if manual_val:
            return json.dumps({
                "symbol": symbol,
                "pe": manual_val.pe,
                "pb": manual_val.pb,
                "eps_ttm": manual_val.eps_ttm,
                "book_value": manual_val.bvps,
                "updated_at": manual_val.computed_at
            }, ensure_ascii=False)
        else:
             return json.dumps({"error": "Không tính được định giá"})
             
    except Exception as e:
        return json.dumps({"error": str(e)})

async def tool_get_company_profile(symbol: str) -> str:
    """
    Lấy thông tin tổng quan doanh nghiệp: Ngành nghề, mô hình kinh doanh.
    """
    try:
        symbol = symbol.upper().strip()
        # Lấy overview từ vnstock
        company = Company(symbol=symbol, source='TCBS') # TCBS thường có profile khá đầy đủ
        df = await asyncio.to_thread(company.overview)
        
        if df is None or df.empty:
            return json.dumps({"error": f"Không tìm thấy hồ sơ {symbol}"})
        
        # Convert về dict cho gọn
        # Các trường quan trọng: short_name, industry, established_year, employees, website, history, ...
        # Tùy API trả về, ta lấy row đầu tiên
        data = df.iloc[0].to_dict()
        
        # Lọc bớt các trường không cần thiết để tiết kiệm token cho AI
        simplified = {
            "tên_công_ty": data.get("short_name") or data.get("organ_name"),
            "ngành": data.get("industry") or data.get("icb_name2"),
            "mô_tả": data.get("business_type") or data.get("about"), # TCBS hay có trường about/business_type
            "năm_thành_lập": data.get("established_year"),
            "sàn": data.get("exchange")
        }
        return json.dumps(simplified, ensure_ascii=False)

    except Exception as e:
        log.error(f"Error tool_get_company_profile: {e}")
        return json.dumps({"error": "Lỗi lấy hồ sơ công ty"})

# --- [NEW] TOOL LẤY BÁO CÁO TÀI CHÍNH ---
async def tool_get_financial_report(symbol: str, report_type: str, period: str = 'quarter') -> str:
    """
    Lấy dữ liệu báo cáo tài chính chi tiết.
    report_type: 'income' (KQKD), 'balance' (Cân đối KT), 'cash' (Dòng tiền), 'ratio' (Chỉ số).
    period: 'year' (Năm) hoặc 'quarter' (Quý).
    """
    try:
        symbol = symbol.upper().strip()
        
        # Khởi tạo Finance với source='VCI' như yêu cầu
        finance = Finance(symbol=symbol, source='VCI')
        
        # Chọn hàm dựa trên loại báo cáo
        if report_type == 'income':
            # Báo cáo kết quả kinh doanh
            task = lambda: finance.income_statement(period=period, lang='vi')
        elif report_type == 'balance':
            # Bảng cân đối kế toán
            task = lambda: finance.balance_sheet(period=period, lang='vi')
        elif report_type == 'cash':
            # Báo cáo lưu chuyển tiền tệ
            task = lambda: finance.cash_flow(period=period, lang='vi')
        elif report_type == 'ratio':
            # Chỉ số tài chính
            task = lambda: finance.ratio(period=period, lang='vi')
        else:
            return json.dumps({"error": "Loại báo cáo không hợp lệ. Chọn: income, balance, cash, ratio"})

        # Chạy trong thread
        df = await asyncio.to_thread(task)
        
        # Trả về JSON đã clean (lấy 5 kỳ gần nhất)
        return _clean_df_to_json(df, limit=5)

    except Exception as e:
        log.error(f"Error tool_get_financial_report ({report_type}): {e}")
        return json.dumps({"error": f"Lỗi lấy báo cáo {report_type}: {str(e)}"})

async def tool_get_stock_events(symbol: str) -> str:
    """Lấy lịch sự kiện: Cổ tức, phát hành thêm, họp ĐHCĐ."""
    try:
        symbol = symbol.upper().strip()
        company = Company(symbol=symbol, source='TCBS')
        
        # Lấy lịch sự kiện
        df = await asyncio.to_thread(company.events)
        
        if df is None or df.empty:
            return json.dumps({"message": f"Không có sự kiện sắp tới cho {symbol}"})

        # Chỉ lấy các cột quan trọng: LoaiSuKien, NgayGDKHQ, NoiDung
        # Và lấy 5 sự kiện mới nhất
        return _clean_df_to_json(df, limit=5)

    except Exception as e:
        log.error(f"Error tool_get_stock_events: {e}")
        return json.dumps({"error": f"Lỗi lấy sự kiện: {str(e)}"})
    
async def tool_get_stock_news(symbol: str) -> str:
    """Lấy tin tức liên quan đến mã cổ phiếu."""
    try:
        symbol = symbol.upper().strip()
        company = Company(symbol=symbol, source='TCBS')
        
        # Lấy tin tức
        df = await asyncio.to_thread(company.news)
        
        if df is None or df.empty:
            return json.dumps({"message": f"Không tìm thấy tin tức cho {symbol}"})

        # Lấy 5 tin mới nhất, chỉ lấy Tiêu đề và Ngày
        return _clean_df_to_json(df[['title', 'publish_date']], limit=5)

    except Exception as e:
        log.error(f"Error tool_get_stock_news: {e}")
        return json.dumps({"error": f"Lỗi lấy tin tức: {str(e)}"})
    
async def tool_get_industry_peers(symbol: str) -> str:
    """
    Tìm các mã cổ phiếu cùng ngành.
    ƯU TIÊN: Lấy từ dữ liệu Screener trên Redis (đã lọc cổ phiếu tốt/thanh khoản cao).
    """
    try:
        symbol = symbol.upper().strip()

        # 1. Lấy dữ liệu Screener từ Redis (Source of Truth cho các mã tốt)
        hist_data = await asyncio.to_thread(get_historical_valuation_from_redis)
        
        # Biến để lưu danh sách mã tốt
        stocks_map = {}
        
        if hist_data and "stocks" in hist_data:
            stocks_map = hist_data["stocks"]
        
        # 2. Xác định ngành của mã đang hỏi (Target Sector)
        my_sector = None
        
        # Cách A: Tìm trong Redis trước (Nhanh & Chuẩn)
        if symbol in stocks_map:
            my_sector = stocks_map[symbol].get("sector")
            
        # Cách B: Nếu mã này KHÔNG nằm trong danh sách mã tốt (VD: mã rác, mã mới lên sàn)
        # Ta vẫn cần biết nó thuộc ngành nào để tìm Peer xịn cho nó.
        # -> Fallback về file sectors.json chỉ để lấy tên ngành.
        if not my_sector:
            import os
            base_dir = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base_dir, "sectors.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    sectors_file = json.load(f)
                if symbol in sectors_file:
                    info = sectors_file[symbol]
                    # Xử lý trường hợp file json lưu dạng object hoặc string
                    if isinstance(info, dict):
                        my_sector = info.get("sector")
                    else:
                        my_sector = info # Trường hợp cũ
            except Exception: 
                pass
            
        if not my_sector:
             return json.dumps({"error": f"Không xác định được ngành của mã {symbol}"})

        # 3. Lọc danh sách Peer (CHỈ LẤY TỪ REDIS STOCKS_MAP)
        # Điều này đảm bảo các mã gợi ý đều là mã đã qua lọc (thanh khoản > 50 tỷ, vốn hóa > 5000 tỷ...)
        peers = []
        for sym, info in stocks_map.items():
            if sym != symbol and info.get("sector") == my_sector:
                peers.append(sym)
        
        # Sắp xếp alphabet
        peers.sort()

        # 4. Trả về kết quả
        # Nếu không tìm thấy peer nào trong Redis (ngành quá nhỏ hoặc toàn mã rác), danh sách sẽ rỗng
        if not peers:
            return json.dumps({
                "symbol": symbol,
                "sector": my_sector,
                "message": "Không tìm thấy mã cùng ngành nào đạt tiêu chuẩn thanh khoản.",
                "peers": []
            }, ensure_ascii=False)

        # Lấy tối đa 15 mã để AI không bị ngợp
        return json.dumps({
            "symbol": symbol,
            "sector": my_sector,
            "source": "Screener (High Quality)",
            "peers": peers[:15],
            "count": len(peers)
        }, ensure_ascii=False)

    except Exception as e:
        log.error(f"Error tool_get_industry_peers: {e}")
        return json.dumps({"error": f"Lỗi tìm mã cùng ngành: {str(e)}"})

# ========= 2. ĐỊNH NGHĨA SCHEMA CHO GEMINI ======

# Đây là phần quan trọng để Gemini hiểu cách gọi hàm
AGENT_TOOLS_SCHEMA = [
    {
        "name": "get_market_price",
        "description": "Lấy giá khớp lệnh, khối lượng và biến động hiện tại của một mã cổ phiếu.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Mã cổ phiếu 3 chữ cái (ví dụ: HPG, VNM)"
                }
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_fundamentals",
        "description": "Lấy các chỉ số định giá cơ bản (P/E, P/B, EPS) để đánh giá đắt rẻ.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Mã cổ phiếu 3 chữ cái"
                }
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_company_profile",
        "description": "Lấy thông tin tổng quan về công ty: tên đầy đủ, ngành nghề kinh doanh chính, năm thành lập. Dùng khi người dùng hỏi 'Công ty này làm gì?', 'Mã này thuộc ngành nào?'.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Mã cổ phiếu 3 chữ cái"
                }
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_financial_report",
        "description": "Lấy dữ liệu báo cáo tài chính chi tiết (Doanh thu, Lợi nhuận, Tài sản, Dòng tiền, Chỉ số tài chính). Dùng khi cần phân tích sâu về sức khỏe tài chính qua các năm/quý.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string", 
                    "description": "Mã cổ phiếu 3 chữ cái"
                },
                "report_type": {
                    "type": "string", 
                    "enum": ["income", "balance", "cash", "ratio"],
                    "description": "Loại báo cáo: 'income' (Kết quả kinh doanh), 'balance' (Cân đối kế toán), 'cash' (Dòng tiền), 'ratio' (Chỉ số tài chính)"
                },
                "period": {
                    "type": "string",
                    "enum": ["year", "quarter"],
                    "description": "Kỳ báo cáo: 'year' (theo năm) hoặc 'quarter' (theo quý). Mặc định nên dùng 'quarter' để có được dữ liệu cập nhật nhất.",
                    "default": "quarter"
                }
            },
            "required": ["symbol", "report_type"]
        }
    },
    {
        "name": "get_stock_events",
        "description": "Lấy lịch sự kiện quan trọng của công ty: Trả cổ tức (tiền/cổ phiếu), Phát hành thêm, Họp đại hội cổ đông. Dùng khi user hỏi về quyền lợi hoặc lịch chốt quyền.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Mã cổ phiếu 3 chữ cái"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_stock_news",
        "description": "Tìm kiếm tin tức báo chí mới nhất liên quan trực tiếp đến mã cổ phiếu. Dùng để giải thích lý do tăng/giảm giá hoặc cập nhật tình hình.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Mã cổ phiếu 3 chữ cái"}
            },
            "required": ["symbol"]
        }
    },
    {
        "name": "get_industry_peers",
        "description": "Tìm danh sách các mã cổ phiếu khác trong cùng nhóm ngành. Rất hữu ích khi cần so sánh định giá (P/E, P/B) hoặc tìm cơ hội đầu tư thay thế.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Mã cổ phiếu gốc để tìm mã tương tự"}
            },
            "required": ["symbol"]
        }
    }
]

# Mapping tên hàm (str) -> Hàm thực thi (python function)
TOOL_MAPPING = {
    "get_market_price": tool_get_market_price,
    "get_fundamentals": tool_get_fundamentals,
    "get_company_profile": tool_get_company_profile,
    "get_financial_report": tool_get_financial_report,
    "get_stock_events": tool_get_stock_events,
    "get_stock_news": tool_get_stock_news,
    "get_industry_peers": tool_get_industry_peers,
}