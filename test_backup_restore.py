import os
import json
import datetime
from db_utils import (
    export_core_data,
    import_core_data,
    get_conn,
)

def count_rows(table):
    """Đếm số dòng trong bảng"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]


def show_counts(label=""):
    tables = ["bot_watch", "news_pref", "bot_config", "bctc_notified"]
    print(f"\n=== {label} ===")
    for t in tables:
        try:
            n = count_rows(t)
            print(f"{t:<15}: {n} dòng")
        except Exception as e:
            print(f"{t:<15}: lỗi ({e})")


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"core_backup_test_{ts}.json"

    print("🟢 BẮT ĐẦU TEST BACKUP / RESTORE\n")

    # 1️⃣ Export dữ liệu core
    print("➡️ Export dữ liệu core...")
    data = export_core_data()

    # 2️⃣ Ghi ra file
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ Đã ghi file backup: {filename}")

    # 3️⃣ Hiển thị thống kê trước khi xóa
    show_counts("Trước khi xóa")

    confirm = input("\n⚠️ Bạn có chắc muốn xóa sạch 4 bảng core để test restore? (y/N): ").strip().lower()
    if confirm != "y":
        print("⏹ Dừng test.")
        return

    # 4️⃣ Xóa dữ liệu cũ
    print("🧹 Đang truncate 4 bảng core...")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE bot_watch")
            cur.execute("TRUNCATE news_pref")
            cur.execute("TRUNCATE bot_config")
            cur.execute("TRUNCATE bctc_notified")
        conn.commit()
    show_counts("Sau khi truncate")

    # 5️⃣ Restore lại từ file backup
    print("\n🔁 Restore lại từ file backup...")
    import_core_data(data, "replace")

    # 6️⃣ Kiểm tra lại
    show_counts("Sau khi restore")

    print("\n✅ HOÀN THÀNH TEST BACKUP / RESTORE.")


if __name__ == "__main__":
    main()
