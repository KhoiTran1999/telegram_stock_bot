# report_cache.py
"""
Cache báo cáo weekly vào Redis, dùng chung cho:
- weekly_report_loop()
- lệnh /report

Mỗi danh mục tương ứng với một cache_key:
    cache_key = "HPG-SSI-VNM"
Key Redis:
    report_cache:HPG-SSI-VNM

Value là JSON (version mới – backward compatible):
{
  "text": "nội dung báo cáo hoặc message lỗi",
  "generated_at": "2025-11-11T02:00:05+00:00",   # ISO
  "source": "weekly_loop" | "on_demand" | "test" | "weekly_error" | "error",
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

from redis_client import get_redis

# TTL Redis: 10 ngày (key tự xoá sau 7 ngày) cho báo cáo OK
REPORT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
DEFAULT_MAX_AGE_DAYS = 7  # giữ nguyên

# TTL cho cache lỗi: chỉ giữ trong thời gian ngắn để tránh spam
ERROR_REPORT_TTL_SECONDS = 60  # 60s

def normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """
    Chuẩn hoá danh sách mã:
    - strip
    - upper
    - bỏ mã rỗng
    - KHÔNG unique (giữ nguyên số lượng mã nếu trùng)
    - sort để:
        ['HPG','SSI','VNM'] và ['VNM','SSI','HPG'] => cùng danh mục
        nhưng ['HPG','SSI','VNM'] khác ['HPG','HPG','SSI','VNM']
    """
    cleaned: list[str] = []
    for s in symbols:
        if not s:
            continue
        s = s.strip().upper()
        if not s:
            continue
        cleaned.append(s)
    # sort nhưng không dùng set() để không mất thông tin số lượng
    return sorted(cleaned)


def make_report_cache_key(symbols: Iterable[str]) -> str:
    """
    Tạo cache_key từ danh mục (multiset):
        ['hpg', 'Vnm', 'ssi']           -> 'HPG-SSI-VNM'
        ['VNM','SSI','HPG','HPG']       -> 'HPG-HPG-SSI-VNM'
    """
    norm = normalize_symbols(symbols)
    return "-".join(norm)


def _redis_key(cache_key: str) -> str:
    return f"report_cache:{cache_key}"

def save_report_to_redis(
    cache_key: str,
    text: str,
    source: str = "weekly_loop",
    *,
    is_error: bool = False,
    wait_sec: Optional[int] = None,
    error_type: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> None:
    """
    Lưu report (hoặc message lỗi) vào Redis.

    - cache_key: đã chuẩn hoá (HPG-SSI-VNM, multiset)
    - text: nội dung báo cáo hoặc message lỗi gửi cho user
    - source:
        'weekly_loop'  -> báo cáo weekly gửi tự động
        'on_demand'    -> báo cáo từ lệnh /report
        'weekly_error' -> message lỗi trong weekly
        'error'        -> message lỗi trong /report
        ... (tuỳ bạn đặt)
    - is_error: True nếu đây là cache lỗi (để /report biết không gọi API lại trong thời gian chờ)
    - wait_sec: nếu is_error=True, thời gian user nên đợi (vd: 120 giây)
    - error_type / error_detail: giúp debug, xem lại lỗi gì đã xảy ra
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
            # Nếu convert int fail thì bỏ qua, không quan trọng
            pass

    if error_type:
        payload["error_type"] = str(error_type)
    if error_detail:
        # Cắt bớt cho an toàn, tránh message quá dài
        payload["error_detail"] = str(error_detail)[:2000]

    # TTL:
    # - Report OK  -> 7 ngày (REPORT_TTL_SECONDS)
    # - Report lỗi -> 60s (ERROR_REPORT_TTL_SECONDS)
    ttl = ERROR_REPORT_TTL_SECONDS if is_error else REPORT_TTL_SECONDS

    r.set(
        _redis_key(cache_key),
        json.dumps(payload, ensure_ascii=False),
        ex=ttl,
    )


def get_report_from_redis(
    cache_key: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Optional[Tuple[str, datetime, bool, Optional[int]]]:
    """
    Lấy report từ Redis:
    - Nếu không có / lỗi parse / quá hạn max_age_days -> trả về None.
    - Nếu còn dùng được -> trả về tuple:
        (text, generated_at, is_error, wait_sec)

    Trong đó:
    - text: nội dung báo cáo hoặc message lỗi
    - generated_at: thời điểm tạo (UTC hoặc timezone tương ứng)
    - is_error: True nếu payload là cache lỗi
    - wait_sec: có thể None nếu không set
    """
    if not cache_key:
        return None

    r = get_redis()
    raw = r.get(_redis_key(cache_key))
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
        # Fallback: coi như UTC now nếu parse lỗi
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
        # Hết hạn sử dụng (chỉ apply cho report OK).
        # Với cache lỗi (TTL ngắn), thường TTL tự xoá nên case này hiếm.
        return None

    is_error = bool(payload.get("is_error", False))
    wait_raw = payload.get("wait_sec")
    wait_sec: Optional[int]
    if isinstance(wait_raw, (int, float)):
        wait_sec = int(wait_raw)
    else:
        wait_sec = None

    return text, generated_at, is_error, wait_sec



def delete_report_from_redis(cache_key: str) -> int:
    """
    Xoá report trong Redis (dùng cho test/debug).
    Trả về số key đã xoá (0 hoặc 1).
    """
    if not cache_key:
        return 0
    r = get_redis()
    return r.delete(_redis_key(cache_key))
