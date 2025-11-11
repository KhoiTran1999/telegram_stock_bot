# test_redis_watchlist_news_pref_cache.py
import json

from redis_client import get_redis
from db_utils import (
    save_watch_list_for_chat,
    get_watch_list_for_chat,
    get_all_watch,
    set_news_pref,
    get_news_pref,
    is_news_enabled_for_chat,
)

# Chọn 1 chat_id test riêng, tránh đụng dữ liệu thật
TEST_CHAT_ID = 99999999


def reset_redis_for_test():
    """Xóa các key Redis liên quan đến TEST_CHAT_ID để test cho sạch."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.delete(f"watch:{TEST_CHAT_ID}")
    pipe.srem("watch_chat_ids", TEST_CHAT_ID)
    pipe.delete(f"news_pref:{TEST_CHAT_ID}")
    pipe.execute()
    print("Đã reset các key Redis test cho chat_id =", TEST_CHAT_ID)


def print_separator(title: str):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def test_watchlist_flow():
    print_separator("WATCHLIST: save -> get_for_chat -> get_all_watch")

    reset_redis_for_test()

    r = get_redis()

    # 1. SAVE watchlist vào DB (và SAU NÀY sẽ vào Redis nữa)
    watch_list = ["HPG", "SSI", "VNM"]
    print(f"save_watch_list_for_chat({TEST_CHAT_ID}, {watch_list})")
    save_watch_list_for_chat(TEST_CHAT_ID, watch_list)

    # 2. Kiểm tra Redis (SAU KHI TA SỬA CODE db_utils MỚI THẤY KQ ĐÚNG)
    raw_redis_watch = r.get(f"watch:{TEST_CHAT_ID}")
    is_in_set = r.sismember("watch_chat_ids", TEST_CHAT_ID)
    print(f"Redis GET watch:{TEST_CHAT_ID} => {raw_redis_watch!r}")
    print(f"Redis SISMEMBER watch_chat_ids {TEST_CHAT_ID} => {is_in_set}")

    # 3. get_watch_list_for_chat
    wl = get_watch_list_for_chat(TEST_CHAT_ID)
    print(f"get_watch_list_for_chat({TEST_CHAT_ID}) => {wl}")

    # 4. get_all_watch
    all_watch = get_all_watch()
    print("get_all_watch() trả về tổng số chat_id:", len(all_watch))
    print("Entry cho TEST_CHAT_ID:", all_watch.get(str(TEST_CHAT_ID)))


def test_news_pref_flow():
    print_separator("NEWS PREF: set -> get -> is_news_enabled")

    r = get_redis()

    # 1. Set preference: tắt chuyên ngành, bật vĩ mô
    print(
        f"set_news_pref(chat_id={TEST_CHAT_ID}, "
        f"enable_specialized=False, enable_macro=True)"
    )
    set_news_pref(TEST_CHAT_ID, enable_specialized=False, enable_macro=True)

    # 2. Kiểm tra get_news_pref
    pref = get_news_pref(TEST_CHAT_ID)
    print("get_news_pref =>", pref)

    # 3. Kiểm tra is_news_enabled_for_chat
    spec_on = is_news_enabled_for_chat(TEST_CHAT_ID, "SPECIALIZED")
    macro_on = is_news_enabled_for_chat(TEST_CHAT_ID, "MACRO")
    print("is_news_enabled_for_chat(..., 'SPECIALIZED') =>", spec_on)
    print("is_news_enabled_for_chat(..., 'MACRO')       =>", macro_on)

    # 4. Kiểm tra key Redis (SAU KHI SỬA CODE db_utils)
    raw_pref_redis = r.get(f"news_pref:{TEST_CHAT_ID}")
    print(f"Redis GET news_pref:{TEST_CHAT_ID} => {raw_pref_redis!r}")

    # 5. Mô phỏng cache miss: xóa key Redis, nhưng DB vẫn còn
    print("\nXóa key Redis news_pref để test fallback DB + warm cache...")
    r.delete(f"news_pref:{TEST_CHAT_ID}")

    pref_after_del = get_news_pref(TEST_CHAT_ID)
    print("get_news_pref (sau khi DEL Redis) =>", pref_after_del)

    raw_pref_redis_after = r.get(f"news_pref:{TEST_CHAT_ID}")
    print(
        f"Redis GET news_pref:{TEST_CHAT_ID} (sau fallback) => "
        f"{raw_pref_redis_after!r}"
    )


def main():
    print("=== TEST WATCHLIST + NEWS_PREF (DB + Redis chuẩn bị) ===")
    test_watchlist_flow()
    test_news_pref_flow()
    print("\n>>> Hoàn tất test. Đọc log ở trên để xem hành vi hiện tại.")
    print("Lưu ý: Các dòng Redis sẽ chỉ đúng sau khi ta sửa db_utils để dùng cache.")


if __name__ == "__main__":
    main()
