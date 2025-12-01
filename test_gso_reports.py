import argparse
import json
import os
import sys
import tempfile
from datetime import datetime

try:
    from worker import collect_gso_reports_sync, MACRO_GSO_MONTH_LIMIT
except ImportError as exc:
    print(f"Cannot import worker helpers: {exc}", file=sys.stderr)
    sys.exit(1)


def dump_gso_reports(limit: int, output_path: str) -> str:
    reports = collect_gso_reports_sync(max_months=limit)
    payload = {
        "generated_at": datetime.now().isoformat(),
        "month_limit": limit,
        "count": len(reports),
        "reports": reports,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch raw GSO macro reports and dump to JSON for inspection."
    )
    parser.add_argument(
        "-m",
        "--months",
        type=int,
        default=MACRO_GSO_MONTH_LIMIT,
        help="Number of recent anchor months to crawl (default: repo macro limit).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Optional output file path. Defaults to temp folder with timestamped name.",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_path = os.path.join(tempfile.gettempdir(), f"gso_reports_raw_{timestamp}.json")
    output_path = args.output or default_path

    path = dump_gso_reports(limit=args.months, output_path=output_path)
    print(f"✅ GSO reports dumped to: {path}")

if __name__ == "__main__":
    main()
