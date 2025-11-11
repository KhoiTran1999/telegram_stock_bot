# redis_client.py
import os
from dotenv import load_dotenv
import redis

load_dotenv()

# Nếu không có REDIS_URL thì mặc định dùng localhost
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis_instance: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """
    Trả về 1 instance Redis dùng chung cho toàn project.
    Dùng lazy-init để chỉ connect khi cần.
    """
    global _redis_instance
    if _redis_instance is None:
        _redis_instance = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,  # trả về str thay vì bytes
        )
    return _redis_instance
