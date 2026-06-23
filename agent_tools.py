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


async def call_vnstock_api_with_retry(class_ref, symbol, method_name, *args, **kwargs):
    """
    Hàm helper hỗ trợ gọi API Vnstock.
    Tự động retry khi bị Rate Limit (429) và tự động đổi nguồn (fallback)
    nếu nguồn mặc định không khả dụng (ví dụ: KBS bị loại bỏ trên server).
    """
    sources_to_try = ["VCI", "TCBS", "KBS"]
    import time

    last_err = None
    rate_limit_err = None

    for source in sources_to_try:
        for attempt in range(2):
            try:
                # Khởi tạo object (Ví dụ: Company(symbol="HPG", source="VCI"))
                instance = class_ref(symbol=symbol, source=source)

                # Lấy thuộc tính/phương thức cần gọi (Ví dụ: events hoặc news)
                method_or_attr = getattr(instance, method_name)

                # Gọi (nếu là method) hoặc lấy giá trị (nếu là property/dataframe)
                if callable(method_or_attr):
                    df = await asyncio.to_thread(method_or_attr, *args, **kwargs)
                else:
                    df = method_or_attr # vnstock 4.x properties return df directly

                # Validate DataFrame
                if df is not None:
                    return df

            except BaseException as e:
                # Bắt cả BaseException (SystemExit) vì Vnstock (vnai) sử dụng sys.exit() khi đạt giới hạn quota
                last_err = e
                err_str = str(e).lower()

                # Kiểm tra nếu là SystemExit do Rate Limit
                is_rate_limit = "429" in err_str or "'data'" in err_str or "rate limit" in err_str or isinstance(e, SystemExit)

                # Lỗi không hỗ trợ source
                if "nhận giá trị tham số source" in err_str or "not supported" in err_str:
                    log.debug(f"Source {source} không được hỗ trợ bởi {class_ref.__name__}. Đổi source.")
                    break # Break vòng lặp retry, chuyển sang source tiếp theo

                # Lỗi bị chặn rate limit hoặc lỗi nội tại API (data)
                elif is_rate_limit:
                    rate_limit_err = e
                    log.warning(f"Vnstock rate limit/error (429/data) cho {symbol} với source {source}. Lần thử {attempt+1}/2. Đang chờ 5s...")
                    await asyncio.sleep(5) # Tăng thời gian chờ lên 5 giây
                    continue # Retry lại với cùng source

                # Lỗi khác -> thử source khác
                else:
                    log.debug(f"Lỗi gọi {method_name} cho {symbol} nguồn {source}: {e}")
                    break

    # Nếu có lỗi rate limit, ưu tiên báo lỗi này vì nó là nguyên nhân chính gây thất bại
    if rate_limit_err:
        raise RuntimeError(f"Hệ thống bị giới hạn truy cập (Rate Limit/429) từ API dữ liệu. Xin chờ một lúc rồi thử lại. (Lỗi gốc: Rate Limit)")

    raise RuntimeError(f"Tất cả các nguồn dữ liệu đều thất bại hoặc không trả về dữ liệu. Lỗi gốc: {last_err}")

@registry.register(name="get_market_price")
async def tool_get_market_price(symbol: str):
    """
    Lấy thông tin giá, % thay đổi, tổng khối lượng và tổng giá trị giao dịch.
    Sử dụng vnstock.Trading.price_board (vnstock 4.x flat columns).
    """
    symbol = symbol.upper()
    try:
        df = await call_vnstock_api_with_retry(Trading, symbol, 'price_board', symbols_list=[symbol])

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


AGENT_TOOLS_SCHEMA = registry.schema
TOOL_MAPPING = registry.tools

if __name__ == "__main__":
    print(json.dumps(AGENT_TOOLS_SCHEMA, indent=2, ensure_ascii=False))
