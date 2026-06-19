import asyncio
import json
import logging
from agent_tools import registry

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

async def test_all_tools():
    print("=== BẮT ĐẦU TEST AGENT TOOLS ===")

    tools = registry.tools

    # Danh sách tham số giả lập cho từng tool
    test_params = {
        "get_market_price": {"symbol": "FPT"},
        "get_fundamentals": {"symbol": "FPT"},
        "get_company_profile": {"symbol": "FPT"},
        "get_technical_indicators": {"symbol": "FPT"},
        "get_market_index": {"index_name": "VNINDEX"},
        "get_financial_report": {"symbol": "FPT", "report_type": "income", "period": "quarter"},
        "get_macro_data": {"topic": "gdp"},
        "get_stock_events": {"symbol": "FPT"},
        "get_stock_news": {"symbol": "FPT"},
        "get_industry_peers": {"symbol": "FPT"}
    }

    for tool_name, tool_func in tools.items():
        print(f"\n--- Đang test: {tool_name} ---")
        params = test_params.get(tool_name, {})
        try:
            if asyncio.iscoroutinefunction(tool_func) or asyncio.iscoroutinefunction(getattr(tool_func, '__wrapped__', None)):
                result = await tool_func(**params)
            else:
                result = tool_func(**params)

            # Cắt ngắn kết quả nếu quá dài
            result_str = str(result)
            if len(result_str) > 500:
                result_str = result_str[:500] + "... (truncated)"

            print(f"✅ THÀNH CÔNG.\nKết quả: {result_str}")
        except Exception as e:
            print(f"❌ LỖI: {e}")

    print("\n=== HOÀN THÀNH TEST ===")

if __name__ == "__main__":
    asyncio.run(test_all_tools())
