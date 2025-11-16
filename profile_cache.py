# profile_cache.py
"""
Cache hồ sơ doanh nghiệp (từ /info) vào Redis.
Dựa trên kiến trúc của report_cache.py.

Mỗi mã cổ phiếu tương ứng với một cache_key:
    cache_key = "FPT"
Key Redis:
    profile_cache:FPT

Value là JSON (giữ nguyên cấu trúc của report_cache):
{
  "text": "nội dung hồ sơ hoặc message lỗi",
  "generated_at": "2025-11-11T02:00:05+00:00",   # ISO
  "source": "on_demand" | "error",
  "is_error": false,                              # true nếu là cache lỗi
  "wait_sec": 120,                                # nếu là cache lỗi: thời gian user nên đợi
  "error_type": "RuntimeError",                   # optional
  "error_detail": "message exception chi tiết"    # optional
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple, Any

# ⚠️ Giả định file này nằm cùng thư mục
# và có thể import từ redis_client của bạn
from redis_client import get_redis

# TTL Redis: 30 ngày cho hồ sơ OK
PROFILE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
DEFAULT_MAX_AGE_DAYS = 30

# TTL cho cache lỗi: 2 phút (120 giây)
ERROR_PROFILE_TTL_SECONDS = 120


def make_profile_cache_key(symbol: str) -> str:
    """
    Chuẩn hoá 1 mã cổ phiếu thành cache_key.
    (Đơn giản hơn report_cache, vì chỉ có 1 mã).
    """
    if not symbol:
        return "UNKNOWN"
    return symbol.strip().upper()


def _redis_key(cache_key: str) -> str:
    """Tạo key đầy đủ cho Redis (ví dụ: profile_cache:FPT)"""
    return f"profile_cache:{cache_key}"


def save_profile_to_redis(
    cache_key: str,
    text: str,
    source: str = "on_demand",
    *,
    is_error: bool = False,
    wait_sec: Optional[int] = None,
    error_type: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> None:
    """
    Lưu hồ sơ (hoặc message lỗi) vào Redis.

    - cache_key: Mã đã chuẩn hoá (ví dụ: FPT)
    - text: nội dung hồ sơ hoặc message lỗi gửi cho user
    - source: 'on_demand' hoặc 'error'
    - is_error: True nếu đây là cache lỗi
    - wait_sec: nếu is_error=True, thời gian user nên đợi (vd: 120 giây)
    """
    if not cache_key or not text:
        return

    r = get_redis()
    now = datetime.now(timezone.utc).isoformat()

    payload: dict[str, Any] = {
        "text": text,
        "generated_at": now,
        "source": source,
        "is_error": bool(is_error),
    }

    if wait_sec is not None:
        try:
            payload["wait_sec"] = int(wait_sec)
        except Exception:
            pass

    if error_type:
        payload["error_type"] = str(error_type)
    if error_detail:
        payload["error_detail"] = str(error_detail)[:2000]

    # TTL:
    # - Hồ sơ OK  -> 30 ngày (PROFILE_TTL_SECONDS)
    # - Hồ sơ lỗi -> 120s (ERROR_PROFILE_TTL_SECONDS)
    ttl = ERROR_PROFILE_TTL_SECONDS if is_error else PROFILE_TTL_SECONDS

    try:
        r.set(
            _redis_key(cache_key),
            json.dumps(payload, ensure_ascii=False),
            ex=ttl,
        )
    except Exception as e:
        # Ghi log (nếu có logger) hoặc print, không raise
        print(f"[profile_cache] Lỗi lưu Redis: {e}")


def get_profile_from_redis(
    cache_key: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Optional[Tuple[str, datetime, bool, Optional[int]]]:
    """
    Lấy hồ sơ từ Redis:
    - Nếu không có / lỗi parse / quá hạn max_age_days -> trả về None.
    - Nếu còn dùng được -> trả về tuple:
        (text, generated_at, is_error, wait_sec)
    """
    if not cache_key:
        return None

    try:
        r = get_redis()
        raw = r.get(_redis_key(cache_key))
    except Exception as e:
        print(f"[profile_cache] Lỗi đọc Redis: {e}")
        return None
        
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    text = payload.get("text")
    if not text:
        return None

    gen_str = payload.get("generated_at")
    if not gen_str:
        return None

    try:
        generated_at = datetime.fromisoformat(gen_str)
    except Exception:
        generated_at = datetime.now(timezone.utc)

    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    # Tính tuổi cache theo ngày
    now = datetime.now(generated_at.tzinfo or timezone.utc)
    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < 0:
        age_seconds = 0.0
    age_days = age_seconds / 86400.0

    if age_days > max_age_days and not bool(payload.get("is_error", False)):
        # Hết hạn sử dụng (chỉ áp dụng cho hồ sơ OK)
        return None

    is_error = bool(payload.get("is_error", False))
    wait_raw = payload.get("wait_sec")
    wait_sec: Optional[int]
    if isinstance(wait_raw, (int, float)):
        wait_sec = int(wait_raw)
    else:
        wait_sec = None

    return text, generated_at, is_error, wait_sec


def delete_profile_from_redis(cache_key: str) -> int:
    """
    Xoá hồ sơ trong Redis (dùng cho test/debug).
    Trả về số key đã xoá (0 hoặc 1).
    """
    if not cache_key:
        return 0
    try:
        r = get_redis()
        return r.delete(_redis_key(cache_key))
    except Exception as e:
        print(f"[profile_cache] Lỗi xoá Redis: {e}")
        return 0