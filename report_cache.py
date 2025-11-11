# report_cache.py
"""
Cache báo cáo weekly vào Redis, dùng chung cho:
- weekly_report_loop()
- lệnh /report

Mỗi danh mục tương ứng với một cache_key:
    cache_key = "HPG-SSI-VNM"
Key Redis:
    report_cache:HPG-SSI-VNM

Value là JSON:
{
  "text": "nội dung báo cáo...",
  "generated_at": "2025-11-11T02:00:05+00:00",
  "source": "weekly_loop" | "on_demand" | "test"
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from redis_client import get_redis

# TTL Redis: 10 ngày (key tự xoá sau 10 ngày)
REPORT_TTL_SECONDS = 10 * 24 * 60 * 60  # 10 days
DEFAULT_MAX_AGE_DAYS = 7  # logic code sẽ coi report > 7 ngày là "cũ"


def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """
    Chuẩn hoá danh sách mã:
    - strip
    - upper
    - bỏ mã rỗng
    - unique + sort
    """
    cleaned = []
    for s in symbols:
        if not s:
            continue
        s = s.strip().upper()
        if not s:
            continue
        cleaned.append(s)
    # unique + sort
    return sorted(set(cleaned))


def make_report_cache_key(symbols: Iterable[str]) -> str:
    """
    Tạo cache_key từ danh mục:
        ['hpg', 'Vnm', 'ssi'] -> 'HPG-SSI-VNM'
    """
    norm = normalize_symbols(symbols)
    return "-".join(norm)


def _redis_key(cache_key: str) -> str:
    return f"report_cache:{cache_key}"


def save_report_to_redis(
    cache_key: str,
    text: str,
    source: str = "weekly_loop",
) -> None:
    """
    Lưu report vào Redis với TTL 10 ngày.
    - cache_key: đã chuẩn hoá (HPG-SSI-VNM)
    - text: nội dung báo cáo
    - source: 'weekly_loop' | 'on_demand' | 'test' (tuỳ bạn)
    """
    if not cache_key or not text:
        return

    r = get_redis()
    now = datetime.now(timezone.utc).isoformat()

    payload = {
        "text": text,
        "generated_at": now,
        "source": source,
    }

    r.set(
        _redis_key(cache_key),
        json.dumps(payload, ensure_ascii=False),
        ex=REPORT_TTL_SECONDS,
    )


def get_report_from_redis(
    cache_key: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Optional[Tuple[str, datetime]]:
    """
    Lấy report từ Redis:
    - Nếu không có / lỗi parse / quá hạn max_age_days -> trả về None.
    - Nếu còn dùng được -> trả về (text, generated_at)
    """
    if not cache_key:
        return None

    r = get_redis()
    raw = r.get(_redis_key(cache_key))
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except Exception:
        # key bị lỗi format -> coi như không có cache
        return None

    text = data.get("text")
    generated_at_str = data.get("generated_at")

    if not text or not generated_at_str:
        return None

    # Parse thời gian tạo
    try:
        generated_at = datetime.fromisoformat(generated_at_str)
    except Exception:
        return None

    # Tính tuổi cache
    now = datetime.now(generated_at.tzinfo or timezone.utc)
    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < 0:
        age_seconds = 0
    age_days = age_seconds / 86400.0

    if age_days > max_age_days:
        # Hết hạn sử dụng theo logic app (dù TTL Redis có thể vẫn còn)
        return None

    return text, generated_at


def delete_report_from_redis(cache_key: str) -> int:
    """
    Xoá report trong Redis (dùng cho test/debug).
    Trả về số key đã xoá (0 hoặc 1).
    """
    if not cache_key:
        return 0
    r = get_redis()
    return r.delete(_redis_key(cache_key))
