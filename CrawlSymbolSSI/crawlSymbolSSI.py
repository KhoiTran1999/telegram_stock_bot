import time
import csv
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# =========================
# Cấu hình cơ bản
# =========================

BASE_URL = "https://iboard.ssi.com.vn"
SECTORS_CSV = "ssi_sectors_urls.csv"
OUTPUT_CSV = "ssi_symbol_industry.csv"

# Nếu bạn dùng Chrome:
def make_driver(headless=False):
    options = webdriver.ChromeOptions()
    # options.add_argument("--disable-gpu")
    # options.add_argument("--window-size=1400,900")
    if headless:
        options.add_argument("--headless=new")
    # Giả lập user-agent trình duyệt thật để hạn chế bị chặn
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(40)
    return driver


# =========================
# Hàm cuộn & lấy mã
# =========================

def collect_symbols_for_sector(driver, url, wait_timeout=25, max_scroll=200):
    """
    Mở 1 URL ngành, cuộn bảng cổ phiếu cho tới khi hết,
    trả về set(symbol).
    """
    print(f"🔗 Mở {url}")
    driver.get(url)

    wait = WebDriverWait(driver, wait_timeout)

    try:
        # 1) Chờ header cột CK (stockSymbol) xuất hiện
        header = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.ag-header-cell[col-id='stockSymbol']")
            )
        )
    except TimeoutException:
        print("  ⚠️ Timeout: không tìm thấy header cột CK (stockSymbol)")
        return set()

    # 2) Tìm đúng grid chứa cột CK này
    #    Từ ô header, đi ngược lên cha là ag-root, rồi tìm viewport của body
    ag_root = header.find_element(
        By.XPATH, "ancestor::div[contains(@class,'ag-root')]"
    )

    viewport = ag_root.find_element(
        By.CSS_SELECTOR, "div.ag-body-viewport.ag-layout-normal.ag-row-animation"
    )

    symbols = set()
    last_total = 0
    no_new_counter = 0

    # 3) Vòng lặp cuộn
    for i in range(max_scroll):
        # Lấy tất cả ô mã CK hiện có
        cells = ag_root.find_elements(
            By.CSS_SELECTOR, "div.ag-cell[col-id='stockSymbol']"
        )
        for c in cells:
            txt = c.text.strip()
            if txt:
                symbols.add(txt)

        print(f"   🔁 Lần cuộn {i+1}, hiện có {len(symbols)} mã")

        # Kiểm tra có mã mới không
        if len(symbols) > last_total:
            last_total = len(symbols)
            no_new_counter = 0
        else:
            no_new_counter += 1

        # Nếu cuộn vài lần mà không có mã mới ⇒ dừng
        if no_new_counter >= 8:
            print("   ✅ Không thấy mã mới sau nhiều lần cuộn, dừng.")
            break

        # Lấy thông tin scroll hiện tại
        scroll_top = driver.execute_script("return arguments[0].scrollTop;", viewport)
        scroll_height = driver.execute_script("return arguments[0].scrollHeight;", viewport)
        client_height = driver.execute_script("return arguments[0].clientHeight;", viewport)

        # Nếu gần chạm đáy ⇒ cuộn thêm nhỏ rồi dừng
        if scroll_top + client_height >= scroll_height - 5:
            print("   ⬇️ Đã tới gần đáy, cuộn thêm lần cuối.")
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight;", viewport
            )
            time.sleep(1.0)
            # Lấy thêm lần cuối
            cells = ag_root.find_elements(
                By.CSS_SELECTOR, "div.ag-cell[col-id='stockSymbol']"
            )
            for c in cells:
                txt = c.text.strip()
                if txt:
                    symbols.add(txt)
            break

        # Cuộn xuống thêm 1 "page"
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;",
            viewport
        )

        time.sleep(0.6)  # cho grid vẽ lại hàng mới

    return symbols


# =========================
# Đọc file ngành & crawl
# =========================

def read_sectors_csv(path):
    sectors = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # cột tên ngành: ưu tiên 'industry', nếu không có thì dùng 'industry_raw'
            name = row.get("industry") or row.get("industry_raw") or ""
            url = row.get("url") or row.get("href") or ""
            if not url:
                continue
            # thêm BASE_URL nếu url chỉ là path /?nav=...
            if url.startswith("/"):
                full_url = BASE_URL + url
            else:
                full_url = url
            sectors.append(
                {
                    "industry": name.strip(),
                    "url": full_url.strip()
                }
            )
    return sectors


def main():
    if not Path(SECTORS_CSV).exists():
        print(f"❌ Không tìm thấy {SECTORS_CSV}")
        return

    sectors = read_sectors_csv(SECTORS_CSV)
    print(f"📋 Đọc {len(sectors)} ngành từ {SECTORS_CSV}")

    driver = make_driver(headless=False)

    all_rows = []
    all_symbols_seen = set()

    try:
        for idx, sec in enumerate(sectors, start=1):
            industry = sec["industry"]
            url = sec["url"]
            print(f"\n[{idx}/{len(sectors)}] Ngành: {industry}")

            try:
                symbols = collect_symbols_for_sector(driver, url)
            except Exception as e:
                print(f"  ⚠️ Lỗi khi crawl ngành {industry}: {e}")
                continue

            symbols = sorted(symbols)
            print(f"  → Thu được {len(symbols)} mã")

            # Ghi vào all_rows (nếu 1 mã trùng ngành khác thì cứ thêm, lát nữa xử lý sau)
            for sym in symbols:
                all_rows.append(
                    {
                        "symbol": sym,
                        "industry": industry
                    }
                )
                all_symbols_seen.add(sym)

    finally:
        driver.quit()

    # Gộp & loại trùng (nếu cùng 1 symbol xuất hiện nhiều ngành, bạn có thể xử lý thêm)
    # Ở đây tạm thời để nguyên, chỉ loại trùng hẳn cả 2 cột
    unique_rows = []
    seen_pairs = set()
    for r in all_rows:
        key = (r["symbol"], r["industry"])
        if key not in seen_pairs:
            seen_pairs.add(key)
            unique_rows.append(r)

    print(f"\n💾 Tổng cộng {len(unique_rows)} dòng (symbol, industry)")
    print(f"   Tổng số mã khác nhau: {len({r['symbol'] for r in unique_rows})}")

    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "industry"])
        writer.writeheader()
        writer.writerows(unique_rows)

    print(f"✅ Đã ghi ra {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
