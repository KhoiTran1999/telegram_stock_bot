import pytest
import asyncio
from unittest.mock import MagicMock, patch
from datetime import datetime
import alert_bot

class BreakLoopException(Exception):
    pass

@pytest.fixture
def mock_env():
    alert_bot.BOT_ACTIVE = True
    alert_bot._stock_current_watch_cache = {}
    alert_bot._stock_current_price_cache = {}
    alert_bot.ALERT_STATE = {}
    while not alert_bot._stock_broadcast_queue.empty():
        alert_bot._stock_broadcast_queue.get_nowait()
    alert_bot.get_state_for_all = MagicMock(side_effect=lambda: alert_bot.ALERT_STATE)
    alert_bot.save_state_for_all = MagicMock(side_effect=lambda x: x)

@pytest.mark.asyncio
async def test_zigzag_volatility(mock_env):
    """Case 5: Zigzag"""
    chat_id = 1001
    symbol = "VND"
    alert_bot._stock_current_watch_cache = {str(chat_id): {"list": [symbol]}}
    today_iso = datetime.now().isoformat()
    alert_bot.ALERT_STATE = {str(chat_id): {symbol: {"last_pct": 2.5, "last_alert_at": today_iso}}}
    alert_bot._stock_current_price_cache = {symbol: {"price": 20000, "pct": 0.4}}

    with patch('alert_bot.in_session_vietnam', return_value=True), \
         patch('alert_bot.asyncio.sleep', side_effect=BreakLoopException):
        try:
            await alert_bot.alert_loop()
        except BreakLoopException: pass

    item = await alert_bot._stock_broadcast_queue.get()
    print(f"\n>>> [Case 5 - Zigzag] Output MỚI:\n{item['body']}")

    # Kiểm tra format mới
    assert "🟢 VND tăng +0.40%" in item['body']
    assert "Biến động" not in item['body']
    
@pytest.mark.asyncio
async def test_drop_alert(mock_env):
    """Case 4: Giảm -2.5%"""
    chat_id = 123
    symbol = "VIC"
    alert_bot._stock_current_watch_cache = {str(chat_id): {"list": [symbol]}}
    alert_bot._stock_current_price_cache = {symbol: {"price": 45000, "pct": -2.5}}

    with patch('alert_bot.in_session_vietnam', return_value=True), \
         patch('alert_bot.asyncio.sleep', side_effect=BreakLoopException):
        try:
            await alert_bot.alert_loop()
        except BreakLoopException: pass

    item = await alert_bot._stock_broadcast_queue.get()
    print(f"\n>>> [Case 4] Output MỚI:\n{item['body']}")

    # Kiểm tra format mới
    assert "🔴 VIC giảm -2.50%" in item['body']
    assert "Giá hiện tại: 45.000" in item['body']