import os
from dotenv import load_dotenv
import redis

load_dotenv()

# Nếu không có REDIS_URL thì mặc định dùng localhost
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Fix Aiven Redis on Render (Needs to use rediss:// for secure connection)
if REDIS_URL.startswith("redis://") and "?" not in REDIS_URL and "localhost" not in REDIS_URL and "127.0.0.1" not in REDIS_URL:
    REDIS_URL = "rediss://" + REDIS_URL[8:]

_redis_instance: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """
    Trả về 1 instance Redis dùng chung cho toàn project.
    Dùng lazy-init để chỉ connect khi cần.
    """
    global _redis_instance
    if _redis_instance is None:
        kwargs = {"decode_responses": True}
        if REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = "none"

        _redis_instance = redis.Redis.from_url(
            REDIS_URL,
            **kwargs
        )
    return _redis_instance
