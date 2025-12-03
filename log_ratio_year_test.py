"""Utility script to inspect the payload returned by
Finance(symbol).ratio(period="quarter", lang="vi").

Run from project root:
    python log_ratio_year_test.py --symbol HPG
The script writes both console output and a log file `ratio_year_test.log`
with basic metadata plus the first few rows of the DataFrame.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
from vnstock import Finance

LOG_FILE = Path(__file__).with_name("ratio_year_test.log")


def configure_logger() -> logging.Logger:
    logger = logging.getLogger("ratio_year_test")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def fetch_ratio_quarter(symbol: str) -> pd.DataFrame:
    finance = Finance(symbol=symbol, source="VCI")
    return finance.ratio(period="quarter", lang="vi")


def main() -> None:
    parser = argparse.ArgumentParser(description="Log Finance.ratio(period='quarter') output")
    parser.add_argument("--symbol", "-s", required=True, help="Ticker symbol, e.g. HPG")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of rows from the DataFrame to emit in the log (default: 5)",
    )
    args = parser.parse_args()

    logger = configure_logger()
    symbol = args.symbol.strip().upper()

    logger.info("Fetching ratio(period='quarter') for %s", symbol)
    try:
        df = fetch_ratio_quarter(symbol)
    except Exception as exc:
        logger.exception("Failed to fetch ratios for %s: %s", symbol, exc)
        return

    if df is None or df.empty:
        logger.warning("Received empty DataFrame for %s", symbol)
        return

    head_df = df.head(args.limit)
    logger.info("DataFrame shape: rows=%s, columns=%s", head_df.shape[0], head_df.shape[1])
    logger.info("Columns: %s", ", ".join(map(str, head_df.columns)))
    logger.info("Preview:\n%s", head_df.to_string(index=False))
    logger.info("Full DataFrame stored in memory; inspect interactively if needed.")


if __name__ == "__main__":
    main()
