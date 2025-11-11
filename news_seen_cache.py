# news_seen_cache.py
import hashlib
from typing import Optional

from redis_client import get_redis

# TTL = 2 ngày như bạn muốn
NEWS_SEEN_TTL_SECONDS = 2 * 24 * 60 * 60  # 2 days


def _make_key(feed_type: str, link: str) -> Optional[str]:
    """
    Tạo key Redis dạng:
    news_seen:MACRO:<hash link>
    news_seen:SPECIALIZED:<hash link>
    """
    if not feed_type or not link:
        return None

    ft = feed_type.strip().upper()
    lk = link.strip()
    if not ft or not lk:
        return None

    # Hash link để key gọn, không chứa ký tự lạ
    h = hashlib.sha256(lk.encode("utf-8")).hexdigest()[:32]
    return f"news_seen:{ft}:{h}"


def has_news_seen_redis(feed_type: str, link: str) -> bool:
    """
    True nếu bài đã được đánh dấu trong Redis.
    """
    key = _make_key(feed_type, link)
    if not key:
        return False

    r = get_redis()
    return bool(r.exists(key))


def mark_news_seen_redis(
    feed_type: str,
    link: str,
    ttl: int = NEWS_SEEN_TTL_SECONDS,
) -> None:
    """
    Đánh dấu 1 bài đã xử lý trong Redis.
    Value không quan trọng, chỉ cần tồn tại key là được.
    """
    key = _make_key(feed_type, link)
    if not key:
        return

    r = get_redis()
    r.set(key, "1", ex=ttl)  # ex = TTL (seconds)


def get_news_seen_count_redis(feed_type: str) -> int:
    """
    Đếm số bài đã lưu cho feed_type (chỉ dùng cho warm-up).
    Dùng SCAN để tránh block Redis nếu nhiều key.
    """
    ft = (feed_type or "").strip().upper()
    if not ft:
        return 0

    pattern = f"news_seen:{ft}:*"
    r = get_redis()

    count = 0
    for _ in r.scan_iter(pattern, count=1000):
        count += 1
    return count
