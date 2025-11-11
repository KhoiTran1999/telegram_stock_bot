# test_report_cache.py
"""
Test đơn giản cho report_cache.py với Redis local.

Cách chạy:
    (.venv) python test_report_cache.py

Nhớ bật redis-server trước khi chạy.
"""

from datetime import datetime
from redis_client import get_redis
from report_cache import (
    make_report_cache_key,
    save_report_to_redis,
    get_report_from_redis,
    delete_report_from_redis,
)


def main():
    symbols = ["hpg", "Vnm", "ssi"]
    cache_key = make_report_cache_key(symbols)

    print("=== TEST REPORT CACHE VỚI REDIS LOCAL ===")
    print(f"symbols      = {symbols}")
    print(f"cache_key    = {cache_key}")
    redis_key = f"report_cache:{cache_key}"
    print(f"redis_key    = {redis_key}")

    # Đảm bảo xoá sạch trước khi test
    deleted = delete_report_from_redis(cache_key)
    print(f"1) Xoá key cũ (nếu có): deleted = {deleted}")

    # 1. Test get trước khi save -> phải là None
    cached = get_report_from_redis(cache_key)
    print(f"2) Lấy cache trước khi save -> {cached!r}")
    assert cached is None, "Expected no cache before save"

    # 2. Save report mới
    text = "📊 Đây là báo cáo test cho danh mục HPG-SSI-VNM."
    print(f"3) Lưu report vào Redis với text = {text!r}")
    save_report_to_redis(cache_key, text, source="test")

    # 3. Get lại report
    cached = get_report_from_redis(cache_key)
    print(f"4) Lấy cache sau khi save -> {cached!r}")
    assert cached is not None, "Expected cache after save"

    cached_text, generated_at = cached
    print(f"   - cached_text   = {cached_text!r}")
    print(f"   - generated_at  = {generated_at!r}")
    assert cached_text == text, "Text trong cache không khớp"

    # 4. Kiểm tra TTL Redis
    r = get_redis()
    ttl = r.ttl(redis_key)
    print(f"5) TTL hiện tại của key trong Redis (giây) = {ttl}")

    # 5. Test max_age_days cực nhỏ -> coi như hết hạn
    cached_old = get_report_from_redis(cache_key, max_age_days=0)
    print(f"6) Lấy cache với max_age_days=0 -> {cached_old!r}")
    assert cached_old is None, "Expected cache to be considered expired with max_age_days=0"

    print("=== TEST HOÀN TẤT OK ✅ ===")


if __name__ == "__main__":
    main()
