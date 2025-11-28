
import asyncio
import datetime
import pytz
from vnstock import Trading, Quote
import pandas as pd

# Setup
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)

async def test_fetch():
    symbols = ["VN30F1M", "VNINDEX", "VN30"]
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    
    print(f"--- Testing Price Board for {symbols} ---")
    try:
        stock_trading = Trading(source="VCI")
        df_board = stock_trading.price_board(symbols)
        print("Price Board Result:")
        if df_board is not None and not df_board.empty:
            print(df_board)
        else:
            print("Empty DataFrame from price_board")
    except Exception as e:
        print(f"Error fetching price_board: {e}")

    print("\n--- Testing Quote History (1m) ---")
    for sym in symbols:
        print(f"Fetching history for {sym}...")
        try:
            q = Quote(symbol=sym, source='VCI')
            # Try fetching for today. If market is closed or no data yet, it might be empty.
            # If today is weekend, use Friday.
            # For testing purpose, let's just try today first.
            df_hist = q.history(start=today_str, end=today_str, interval='1m')
            
            if df_hist is not None and not df_hist.empty:
                print(f"Success {sym}: Last close = {df_hist.iloc[-1]['close']}")
                print(df_hist.tail(2))
            else:
                print(f"Failed {sym}: Empty DataFrame")
        except Exception as e:
            print(f"Error fetching history for {sym}: {e}")

if __name__ == "__main__":
    asyncio.run(test_fetch())
