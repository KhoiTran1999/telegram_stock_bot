async def send_eod_summary():
    """
    Gửi tổng kết cuối phiên cho từng user dựa trên watchlist:
    - Mã cổ phiếu
    - % thay đổi (tự tính từ match_price & reference_price)
    - Tổng giá trị khớp (VND) – từ match_accumulated_value * 1_000_000
    - GT mua/bán khối ngoại (VND)
    """
    vn_tz = pytz.timezone(TIMEZONE)
    now = datetime.datetime.now(vn_tz)
    today_str = now.strftime("%d/%m/%Y")

    log.info(f"[{INSTANCE_ID}][EOD] Bắt đầu tổng kết cuối phiên cho ngày {today_str}.")

    # 1️⃣ Lấy toàn bộ watchlist từ DB (chạy trong thread)
    try:
        all_watch = await asyncio.to_thread(get_all_watch)
    except Exception as e:
        log.warning(f"[{INSTANCE_ID}][EOD] Lỗi get_all_watch: {e}")
        return

    if not all_watch:
        log.info(f"[{INSTANCE_ID}][EOD] Không có user nào theo dõi mã, bỏ qua.")
        return

    # Gom tất cả mã cần lấy board
    all_symbols: set[str] = set()
    for block in all_watch.values():
        for sym in (block.get("list", []) or []):
            s = str(sym).upper().strip()
            if s:
                all_symbols.add(s)

    if not all_symbols:
        log.info(f"[{INSTANCE_ID}][EOD] Watchlist rỗng, bỏ qua.")
        return

    # 2️⃣ Gọi price_board cho toàn bộ mã (chạy trong thread)
    def _fetch_price_board(symbols: list[str]):
        try:
            tr = Trading(source="VCI")
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Không khởi tạo được Trading: {e}")
            return None
        try:
            return tr.price_board(symbols)
        except Exception as e:
            log.warning(f"[{INSTANCE_ID}][EOD] Lỗi price_board: {e}")
            return None

    pb_df = await asyncio.to_thread(_fetch_price_board, sorted(all_symbols))
    if pb_df is None or pb_df.empty:
        log.warning(f"[{INSTANCE_ID}][EOD] price_board trả về rỗng.")
        return

    # 3️⃣ Helpers parse dữ liệu
    def _norm(x):
        if x is None:
            return None
        try:
            if hasattr(x, "item"):
                x = x.item()
        except Exception:
            pass
        try:
            x = float(x)
        except Exception:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return x

    def _find_by_substrings(row, required_substrings: list[str]):
        """Tìm cột có chứa đầy đủ các substring yêu cầu."""
        for k in row.index:
            if isinstance(k, tuple):
                name = f"{k[0]}_{k[1]}".lower()
            else:
                name = str(k).lower()
            if all(sub in name for sub in required_substrings):
                try:
                    v = _norm(row.get(k))
                except Exception:
                    v = None
                if v is not None:
                    return v
        return None

    summary_by_symbol: dict[str, dict] = {}

    for idx, row in pb_df.iterrows():
        # Lấy symbol
        sym = None
        for ksym in [("listing", "symbol"), ("stock", "symbol"), ("listing", "ticker")]:
            try:
                if ksym in row.index:
                    val = row.get(ksym)
                else:
                    val = None
            except Exception:
                val = None
            if val:
                sym = str(val).upper().strip()
                break

        if not sym and isinstance(idx, str):
            sym = idx.upper().strip()
        if not sym:
            continue

        # --- % thay đổi: tự tính từ match_price & reference_price ---
        match_price = None
        ref_price = None
        try:
            if ("match", "match_price") in row.index:
                match_price = _norm(row[("match", "match_price")])
        except Exception:
            match_price = None

        # ưu tiên reference_price trong "match", nếu không có thì lấy ref_price trong "listing"
        try:
            if ("match", "reference_price") in row.index:
                ref_price = _norm(row[("match", "reference_price")])
            elif ("listing", "ref_price") in row.index:
                ref_price = _norm(row[("listing", "ref_price")])
        except Exception:
            ref_price = None

        pct = None
        if match_price is not None and ref_price not in (None, 0):
            pct = (match_price - ref_price) / ref_price * 100.0

        # --- Tổng GT khớp ---
        # match_accumulated_value đang là "triệu VND" => nhân 1_000_000 để ra VND
        raw_acc_value = _find_by_substrings(row, ["accumulated_value"])
        total_value_vnd = None
        if raw_acc_value is not None:
            total_value_vnd = raw_acc_value * 1_000_000.0

        # --- GT khối ngoại mua/bán (đã là VND) ---
        foreign_buy = _find_by_substrings(row, ["foreign", "buy", "value"])
        foreign_sell = _find_by_substrings(row, ["foreign", "sell", "value"])

        summary_by_symbol[sym] = {
            "pct": pct,
            "value": total_value_vnd,
            "foreign_buy": foreign_buy,
            "foreign_sell": foreign_sell,
        }

    if not summary_by_symbol:
        log.warning(f"[{INSTANCE_ID}][EOD] Không parse được dữ liệu cho mã nào.")
        return

    # 4️⃣ Hàm format cho đẹp
    def fmt_pct(p):
        if p is None:
            return "N/A"
        try:
            return f"{float(p):+,.2f}%"
        except Exception:
            return "N/A"

    def fmt_vnd(v):
        if v is None:
            return "N/A"
        try:
            v = float(v)
        except Exception:
            return "N/A"
        av = abs(v)
        if av >= 1_000_000_000_000:
            return f"{v/1_000_000_000_000:.1f} nghìn tỷ"
        if av >= 1_000_000_000:
            return f"{v/1_000_000_000:.1f} tỷ"
        if av >= 1_000_000:
            return f"{v/1_000_000:.1f} triệu"
        if av >= 1_000:
            return f"{v/1_000:.1f} nghìn"
        return f"{v:.0f}"

    # 5️⃣ Build & gửi message cho từng user
    tasks = []
    for chat_key, user_block in all_watch.items():
        try:
            chat_id = int(chat_key)
        except Exception:
            continue

        watch_list = user_block.get("list", []) or []
        if not watch_list:
            continue

        lines: list[str] = [
            f"📊 *Tổng kết cuối phiên {today_str}*",
            "",
        ]
        has_any = False

        for sym in watch_list:
            s = str(sym).upper().strip()
            info = summary_by_symbol.get(s)
            if not info:
                lines.append(f"• *{s}*: không có dữ liệu giao dịch hôm nay.")
                has_any = True
                continue

            pct_str = fmt_pct(info["pct"])
            val_str = fmt_vnd(info["value"])

            fb = info.get("foreign_buy")
            fs = info.get("foreign_sell")

            if fb is not None or fs is not None:
                fb_str = fmt_vnd(fb) if fb is not None else "0"
                fs_str = fmt_vnd(fs) if fs is not None else "0"
                line = (
                    f"• *{s}*: {pct_str}"
                    f" | GT khớp: {val_str}"
                    f" | NN Mua/Bán: {fb_str} / {fs_str}"
                )
            else:
                line = f"• *{s}*: {pct_str} | GT khớp: {val_str}"

            lines.append(line)
            has_any = True

        if not has_any:
            continue

        lines.append("")
        lines.append("🤖 Dữ liệu được tổng hợp tự động bởi *StockBot*.")

        text = "\n".join(lines)
        tasks.append(send_md(tg_app.bot, chat_id, text))

    if not tasks:
        log.info(f"[{INSTANCE_ID}][EOD] Không có user nào cần gửi tổng kết.")
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    log.info(f"[{INSTANCE_ID}][EOD] Đã gửi tổng kết cuối phiên cho {ok} user.")
