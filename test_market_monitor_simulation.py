import asyncio
import sys
import os
from unittest.mock import MagicMock, patch
import datetime
import pytz
import pandas as pd

# 1. MOCK DEPENDENCIES BEFORE IMPORTING WORKER
sys.modules['redis'] = MagicMock()
sys.modules['google'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['feedparser'] = MagicMock()
sys.modules['apscheduler'] = MagicMock()
sys.modules['apscheduler.schedulers.asyncio'] = MagicMock()
sys.modules['apscheduler.triggers.cron'] = MagicMock()
sys.modules['apscheduler.triggers.interval'] = MagicMock()
sys.modules['apscheduler.jobstores.redis'] = MagicMock()
sys.modules['apscheduler.events'] = MagicMock()

# Mock vnstock
mock_vnstock = MagicMock()
sys.modules['vnstock'] = mock_vnstock

# Define MockQuote to simulate price changes
# Scenario:
# VN30F1M: 1200 (Init) -> 1206 (+6, Alert) -> 1204 (-2, No Alert) -> 1210 (+6 from 1204? No, anchor is 1206. 1210-1206=4. No Alert? Wait. Anchor updates on trigger.)
# Let's trace:
# T1: 1200. Anchor=1200.
# T2: 1206. Delta=6 > 5. Trigger! Anchor=1206.
# T3: 1204. Delta=1204-1206=-2. No Trigger.
# T4: 1212. Delta=1212-1206=6. Trigger! Anchor=1212.

price_sequence = {
    "VN30F1M": [1200.0, 1206.0, 1204.0, 1212.0, 1212.0],
    "VNINDEX": [1100.0, 1100.0, 1110.0, 1115.0, 1115.0], # 1100->1110 (+10 Trigger) -> 1115 (+5 Trigger)
    "VN30":    [1150.0, 1152.0, 1153.0, 1160.0, 1160.0]  # 1150->1152->1153->1160 (+7 Trigger from 1150? No anchor=1150. 1160-1150=10. Trigger)
}
call_counts = {k: 0 for k in price_sequence}

class MockQuote:
    def __init__(self, symbol, source='VCI'):
        self.symbol = symbol

    def history(self, start, end, interval):
        # Return a mock DataFrame-like object
        idx = call_counts.get(self.symbol, 0)
        seq = price_sequence.get(self.symbol, [0])
        
        if idx >= len(seq):
            price = seq[-1]
        else:
            price = seq[idx]
            call_counts[self.symbol] += 1
        
        # print(f"   [MockQuote] {self.symbol} fetching... Price={price}")

        # Mock DataFrame
        # We need .iloc[-1]['close'] and .iloc[-2]['close']
        # We simulate a DF with 2 rows: [Prev, Curr]
        # Prev price is just current - 1 for simplicity, or we can track history.
        # For this test, we mainly care about 'close' of the last row (current price).
        # And for Ref price logic, it checks iloc[-2].
        
        mock_df = MagicMock()
        mock_df.__len__.return_value = 2
        mock_df.empty = False
        
        # Create a mock for .iloc that handles __getitem__
        mock_iloc = MagicMock()
        
        def iloc_getitem(index):
            if index == -1: return {'close': price, 'volume': 1000}
            if index == -2: return {'close': price, 'volume': 1000}
            return {'close': price}
            
        # Use side_effect on the __getitem__ mock of the iloc mock
        mock_iloc.__getitem__.side_effect = iloc_getitem
        mock_df.iloc = mock_iloc
        
        return mock_df

mock_vnstock.Quote = MockQuote
mock_vnstock.Trading = MagicMock()

# 2. IMPORT WORKER
import worker

# 3. SETUP WORKER MOCKS
worker.r_client = MagicMock()
worker.get_bot_active = MagicMock(return_value=True)
worker.in_session_vietnam = MagicMock(return_value=True)

# Mock DB functions
worker.get_vn30f1m_enabled_map = MagicMock(return_value={1001: True})
worker.get_vnindex_enabled_map = MagicMock(return_value={1002: True})
worker.get_vn30_enabled_map = MagicMock(return_value={1003: True})

# Mock push_telegram_msg
def mock_push(chat_id, text, reply_markup=None, msg_type='GENERAL', **kwargs):
    print(f"🔔 [ALERT SENT] To: {chat_id} | Type: {msg_type}")
    print(f"   Content: {text.replace(chr(10), ' ')}") # Replace newline with space for clean log
worker.push_telegram_msg = mock_push

# Mock Trading for Ref Price fallback
mock_trading = mock_vnstock.Trading.return_value
mock_row = MagicMock()
mock_row.get.side_effect = lambda k: 1190.0 if 'ref_price' in str(k) else None
mock_df_board = MagicMock()
mock_df_board.iloc = [mock_row]
mock_trading.price_board.return_value = mock_df_board
worker.stock_trading = mock_trading

# 4. RUN SIMULATION
original_sleep = asyncio.sleep
async def fast_sleep(delay):
    # Skip long sleeps
    if delay >= 1:
        await original_sleep(0.1)
    else:
        await original_sleep(delay)

async def run_test():
    print("🚀 STARTING MARKET MONITOR SIMULATION")
    print("-------------------------------------")
    
    # Start loops
    task_fetch = asyncio.create_task(worker.market_monitor_fetcher_loop())
    task_alert = asyncio.create_task(worker.market_monitor_alert_loop())
    
    # Run for 5 cycles
    for i in range(5):
        print(f"\n--- Tick {i+1} ---")
        await asyncio.sleep(0.2) # Wait for tasks to execute
        
        # Print current state
        print("   [State]", end=" ")
        for sym, data in worker._market_data.items():
            p = data.get('price')
            a = data.get('anchor')
            print(f"{sym}: P={p} A={a} |", end=" ")
        print("")

    print("\n-------------------------------------")
    print("🛑 STOPPING SIMULATION")
    task_fetch.cancel()
    task_alert.cancel()

if __name__ == "__main__":
    with patch('asyncio.sleep', side_effect=fast_sleep):
        try:
            asyncio.run(run_test())
        except KeyboardInterrupt:
            pass
