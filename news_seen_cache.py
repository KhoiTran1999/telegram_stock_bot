# news_seen_cache.py
import hashlib
import json
import datetime
from typing import Optional

from redis_client import get_redis

# TTL mặc định ~ 6 tháng (180 ngày)
NEWS_SEEN_TTL_SECONDS = 180 * 24 * 60 * 60  # ~ 6 months


def canonicalize_link(link: str) -> str:
    """
    Chuẩn hoá link để giảm trùng lặp do query tracking (utm_*, fbclid, ...).
    Không cố gắng xử lý mọi trường hợp, chỉ làm gọn những case phổ biến.

    Ví dụ:
        https://example.com/a?utm_source=rss&fbclid=XYZ
    ->     https://example.com/a

    Nếu parse lỗi thì trả lại link strip().
    """
    if not link:
        return ""

    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = link.strip()
    try:
        parts = urlsplit(raw)
    except Exception:
        return raw

    scheme = (parts.scheme or "").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or "/"

    # Lọc bỏ một số query tracking phổ biến
    filtered_query = []
    if parts.query:
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            kl = (k or "").lower()
            if kl.startswith("utm_") or kl in {"fbclid", "gclid", "ref"}:
                continue
            filtered_query.append((k, v))

    query = urlencode(filtered_query, doseq=True) if filtered_query else ""
    fragment = ""  # bỏ #... cho chắc

    return urlunsplit((scheme, netloc, path, query, fragment))


def _make_key(feed_type: str, link: str) -> Optional[str]:
    """
    Tạo key Redis dạng:
        news_seen:MACRO:<hash canonical_link>
        news_seen:SPECIALIZED:<hash canonical_link>
    """
    ft = (feed_type or "").strip().upper()
    if not ft:
        return None

    canonical = canonicalize_link(link)
    if not canonical:
        return None

    # Hash canonical link để key gọn & đồng đều
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"news_seen:{ft}:{h}"


def has_news_seen_redis(feed_type: str, link: str) -> bool:
    """
    Kiểm tra xem bài (feed_type, link) đã được đánh dấu trong Redis chưa.
    Chỉ dùng key tồn tại hay không, không quan tâm value.
    """
    key = _make_key(feed_type, link)
    if not key:
        return False

    r = get_redis()
    try:
        # decode_responses=True nên exists trả int 0/1
        return bool(r.exists(key))
    except Exception:
        return False


def mark_news_seen_redis(
    feed_type: str,
    link: str,
    ttl: Optional[int] = None,
    title: Optional[str] = None,
    published=None,
) -> None:
    """
    Đánh dấu 1 bài đã được xử lý trong Redis.

    - ttl: nếu truyền vào sẽ override TTL mặc định (dùng cho test).
    - title/published: nếu có, sẽ lưu JSON để tiện debug / hiển thị:
        {"title": "...", "published": "2025-11-16T00:00:00+07:00"}

      published có thể là datetime hoặc string ISO.
    """
    key = _make_key(feed_type, link)
    if not key:
        return

    r = get_redis()
    expire = int(ttl if ttl is not None else NEWS_SEEN_TTL_SECONDS)

    # Chuẩn hoá published -> string ISO nếu có
    pub_str: Optional[str]
    if published is None or published == "":
        pub_str = None
    elif isinstance(published, datetime.datetime):
        # Nếu datetime không có tz, để nguyên (Postgres TIMESTAMPTZ đã lo tz)
        pub_str = published.isoformat()
    else:
        # các kiểu khác (str, ...) -> str
        pub_str = str(published)

    payload = {}
    if title:
        payload["title"] = title
    if pub_str:
        payload["published"] = pub_str

    # Nếu không có title/published thì lưu "1" cho nhẹ
    value = json.dumps(payload) if payload else "1"

    try:
        r.set(name=key, value=value, ex=expire)
    except Exception:
        # Không raise để tránh làm hỏng luồng chính nếu Redis lỗi
        return


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
    try:
        for _ in r.scan_iter(pattern, count=1000):
            count += 1
    except Exception:
        return 0
    return count
