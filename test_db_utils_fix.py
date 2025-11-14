# test_db_utils_fix.py
import pytest
import fakeredis
import json
from unittest.mock import MagicMock

# Quan trọng: Import các module cần 'patch' (giả lập)
import redis_client
import db_utils


@pytest.fixture(autouse=True)
def mock_redis(mocker):
    """
    Tự động thay thế get_redis() bằng một instance fakeredis.
    """
    fake_r = fakeredis.FakeRedis(decode_responses=True)
    mocker.patch('db_utils.get_redis', return_value=fake_r)
    mocker.patch('redis_client.get_redis', return_value=fake_r)
    yield fake_r
    fake_r.flushall()

# ==================================================================
# HÀM HELPER GIẢ LẬP DB (để tránh lặp code)
# ==================================================================
def mock_db_call(mocker, fetch_one_row=None, fetch_all_rows=None):
    """
    Giả lập 'with get_conn() as conn: ...'
    - dùng fetch_one_row cho cur.fetchone()
    - dùng fetch_all_rows cho cur.fetchall()
    """
    mock_cursor = MagicMock()
    if fetch_one_row is not None:
        mock_cursor.fetchone.return_value = fetch_one_row
    if fetch_all_rows is not None:
        mock_cursor.fetchall.return_value = fetch_all_rows
    
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    
    mock_get_conn_return = MagicMock()
    mock_get_conn_return.__enter__.return_value = mock_conn
    
    mocker.patch('db_utils.get_conn', return_value=mock_get_conn_return)
    
    # Trả về cursor để ta có thể kiểm tra nó (ví dụ: assert_called_once)
    return mock_cursor
# ==================================================================


def test_get_watch_list_empty_list_caches_correctly(mock_redis, mocker):
    """
    KIỂM TRA LỖI CHÍNH (KHI USER CÓ LIST RỖNG)
    """
    chat_id = 12345
    db_return_value = []  # DB trả về list rỗng
    mock_row = (db_return_value,)

    # 1. Giả lập DB trả về 1 dòng rỗng
    mock_cursor = mock_db_call(mocker, fetch_one_row=mock_row)

    # 2. Chạy hàm (cache đang trống) -> Cache Miss
    result = db_utils.get_watch_list_for_chat(chat_id)

    # 3. Kiểm tra (Asserts)
    assert result == []
    assert mock_redis.get(f"watch:{chat_id}") == "[]"
    assert mock_redis.sismember("watch_chat_ids", chat_id) == True

    # 4. (Bonus) Gọi lần 2 -> Phải là Cache Hit
    result_2 = db_utils.get_watch_list_for_chat(chat_id)
    assert result_2 == []
    
    # Khẳng định rằng 'fetchone' (lệnh DB) chỉ được gọi 1 LẦN DUY NHẤT
    # (chứng tỏ lần 2 đã dùng cache)
    mock_cursor.fetchone.assert_called_once()


def test_get_watch_list_non_empty_list_caches_correctly(mock_redis, mocker):
    """
    Kiểm tra kịch bản thông thường (User có list ['HPG'])
    """
    chat_id = 98765
    db_return_value = ['HPG']  # DB trả về list có mã
    mock_row = (db_return_value,)

    # 1. Giả lập DB
    mock_cursor = mock_db_call(mocker, fetch_one_row=mock_row)

    # 2. Chạy hàm (cache đang trống) -> Cache Miss
    result = db_utils.get_watch_list_for_chat(chat_id)

    # 3. Kiểm tra
    assert result == ['HPG']
    assert mock_redis.get(f"watch:{chat_id}") == json.dumps(['HPG'])
    assert mock_redis.sismember("watch_chat_ids", chat_id) == True
    
    # 4. (Bonus) Gọi lần 2 -> Phải là Cache Hit
    result_2 = db_utils.get_watch_list_for_chat(chat_id)
    assert result_2 == ['HPG']
    mock_cursor.fetchone.assert_called_once()


def test_get_all_watch_includes_empty_list_user(mock_redis, mocker):
    """
    KIỂM TRA HÀM /allwatch (get_all_watch)
    """
    user_empty = 111
    user_full = 222
    
    db_data = [
        (user_empty, []),
        (user_full, ['HPG', 'SSI'])
    ]

    # 1. Giả lập DB
    mock_cursor = mock_db_call(mocker, fetch_all_rows=db_data)

    # 2. Chạy hàm get_all_watch (cache đang trống) -> Cache Miss
    result = db_utils.get_all_watch()

    # 3. Kiểm tra kết quả trả về từ DB (Fallback)
    expected_result = {
        str(user_empty): {"list": []},
        str(user_full): {"list": ['HPG', 'SSI']}
    }
    assert result == expected_result

    # 4. Kiểm tra cache đã được warm ĐÚNG
    assert mock_redis.get(f"watch:{user_empty}") == "[]"
    assert mock_redis.get(f"watch:{user_full}") == json.dumps(['HPG', 'SSI'])
    assert mock_redis.smembers("watch_chat_ids") == {str(user_empty), str(user_full)}

    # 5. (Bonus) Chạy get_all_watch lần 2 -> phải dùng cache
    result_2 = db_utils.get_all_watch()
    assert result_2 == expected_result
    
    # Khẳng định DB (fetchall) chỉ bị gọi 1 lần
    mock_cursor.fetchall.assert_called_once()