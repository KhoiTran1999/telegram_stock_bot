"""Manual P/E and P/B calculation helpers with Redis caching."""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Iterable

import pandas as pd
from vnstock import Finance, Trading

from redis_client import get_redis

log = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "manual_valuation:"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 1 day as agreed with user
FINANCE_SOURCE = "VCI"
TRADING_SOURCE = "VCI"


@dataclass
class ManualValuation:
	"""Structured result for a manual valuation lookup."""

	symbol: str
	price: float | None
	eps_ttm: float | None
	bvps: float | None
	pe: float | None
	pb: float | None
	computed_at: str
	source: str
	error: str | None = None
	needs_admin_alert: bool = False

	def to_cache_dict(self) -> dict:
		"""Return a JSON-serialisable representation for Redis."""

		return {
			"symbol": self.symbol,
			"price": self.price,
			"eps_ttm": self.eps_ttm,
			"bvps": self.bvps,
			"pe": self.pe,
			"pb": self.pb,
			"computed_at": self.computed_at,
			"error": self.error,
		}


def _now_iso() -> str:
	return dt.datetime.now(dt.timezone.utc).isoformat()


def _cache_key(symbol: str) -> str:
	return f"{CACHE_KEY_PREFIX}{symbol.strip().upper()}"


def _get_redis_safe():
	try:
		return get_redis()
	except Exception as exc:  # pragma: no cover - defensive
		log.warning("Manual valuation cache unavailable: %s", exc)
		return None


def _load_from_cache(symbol: str) -> ManualValuation | None:
	client = _get_redis_safe()
	if not client:
		return None
	raw = client.get(_cache_key(symbol))
	if not raw:
		return None
	try:
		payload = json.loads(raw)
	except json.JSONDecodeError:
		return None
	if not isinstance(payload, dict):
		return None
	return ManualValuation(
		symbol=symbol,
		price=payload.get("price"),
		eps_ttm=payload.get("eps_ttm"),
		bvps=payload.get("bvps"),
		pe=payload.get("pe"),
		pb=payload.get("pb"),
		computed_at=payload.get("computed_at") or _now_iso(),
		source="cache",
		error=payload.get("error"),
		needs_admin_alert=False,
	)


def _save_to_cache(result: ManualValuation) -> None:
	client = _get_redis_safe()
	if not client:
		return
	try:
		client.setex(
			_cache_key(result.symbol),
			CACHE_TTL_SECONDS,
			json.dumps(result.to_cache_dict(), ensure_ascii=False),
		)
	except Exception as exc:  # pragma: no cover - cache best-effort
		log.warning("Unable to persist manual valuation for %s: %s", result.symbol, exc)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
	cols = []
	for col in df.columns:
		if isinstance(col, tuple):
			name = col[-1]
		else:
			name = str(col)
		clean = (
			name.lower()
			.strip()
			.replace("/", "")
			.replace(" ", "_")
			.replace("(", "")
			.replace(")", "")
		)
		cols.append(clean)
	df = df.copy()
	df.columns = cols
	return df


def _prepare_quarterly(df: pd.DataFrame) -> pd.DataFrame:
	df = _normalize_columns(df)
	df["_fallback_order"] = range(len(df), 0, -1)
	year_col = next((c for c in ("year", "nam", "f_year", "report_year") if c in df.columns), None)
	quarter_col = next((c for c in ("quarter", "quy", "f_quarter", "report_quarter") if c in df.columns), None)
	sort_cols: list[str] = []
	if year_col:
		df["_sort_year"] = pd.to_numeric(df[year_col], errors="coerce").fillna(0)
		sort_cols.append("_sort_year")
	if quarter_col:
		df["_sort_quarter"] = pd.to_numeric(df[quarter_col], errors="coerce").fillna(0)
		sort_cols.append("_sort_quarter")
	sort_cols.append("_fallback_order")
	return df.sort_values(sort_cols, ascending=False)


def _trailing_sum(df: pd.DataFrame, candidates: Iterable[str], periods: int = 4) -> float | None:
	for col in candidates:
		if col not in df.columns:
			continue
		series = pd.to_numeric(df[col], errors="coerce").dropna()
		if series.empty:
			continue
		return float(series.head(periods).sum())
	return None


def _pick_latest_value(df: pd.DataFrame, candidates: Iterable[str]) -> float | None:
	for col in candidates:
		if col not in df.columns:
			continue
		series = pd.to_numeric(df[col], errors="coerce").dropna()
		if series.empty:
			continue
		return float(series.iloc[0])
	return None


def _extract_board_value(row: pd.Series, keys: Iterable[tuple | str]) -> float | None:
	for key in keys:
		if key not in row.index:
			continue
		value = row[key]
		if pd.isna(value):
			continue
		try:
			return float(value)
		except (TypeError, ValueError):
			continue
	return None


def _fetch_price(symbol: str) -> float:
	board = Trading(symbol=symbol, source=TRADING_SOURCE).price_board([symbol])
	if board is None or board.empty:
		raise RuntimeError("price_board returned empty data")
	row = board.iloc[0]
	price = _extract_board_value(
		row,
		[
			("match", "match_price"),
			("match", "price"),
			("match", "last_price"),
			"match_price",
			"last_price",
		],
	)
	if price is None or price <= 0:
		price = _extract_board_value(
			row,
			[
				("listing", "ref_price"),
				("listing", "reference_price"),
				"ref_price",
				"reference_price",
			],
		)
	if price is None or price <= 0:
		raise RuntimeError("price_board did not contain a valid price")
	if 0 < price < 500:
		price *= 1000.0
	return float(price)


def _fetch_ratio_quarter(symbol: str) -> pd.DataFrame:
	finance = Finance(symbol=symbol, source=FINANCE_SOURCE)
	df = finance.ratio(period="quarter", lang="vi")
	if df is None or df.empty:
		raise RuntimeError("Finance ratio API returned empty data")
	return _prepare_quarterly(df)


def _safe_positive(value: float | None) -> float | None:
	if value is None:
		return None
	try:
		numeric = float(value)
	except (TypeError, ValueError):
		return None
	return numeric if numeric > 0 else None


def _build_result(
	symbol: str,
	*,
	price: float | None,
	eps: float | None,
	bvps: float | None,
	source: str,
	error: str | None = None,
	needs_admin_alert: bool = False,
) -> ManualValuation:
	price_val = _safe_positive(price)
	eps_val = _safe_positive(eps)
	bvps_val = _safe_positive(bvps)
	pe = (price_val / eps_val) if (price_val and eps_val) else None
	pb = (price_val / bvps_val) if (price_val and bvps_val) else None
	return ManualValuation(
		symbol=symbol,
		price=price_val,
		eps_ttm=eps_val,
		bvps=bvps_val,
		pe=pe,
		pb=pb,
		computed_at=_now_iso(),
		source=source,
		error=error,
		needs_admin_alert=needs_admin_alert,
	)


def fetch_manual_pe_pb(
	symbol: str,
	*,
	use_cache: bool = True,
	force_refresh: bool = False,
	ratio_df: pd.DataFrame | None = None,
	price: float | None = None,
) -> ManualValuation:
	"""Return manual P/E and P/B values for *symbol* with daily caching."""

	if not symbol:
		raise ValueError("symbol is required")
	sym = symbol.strip().upper()

	if use_cache and not force_refresh:
		cached = _load_from_cache(sym)
		if cached:
			return cached

	try:
		ratio_df = ratio_df if ratio_df is not None else _fetch_ratio_quarter(sym)
		eps = _trailing_sum(ratio_df, ("eps", "eps_vnd"))
		bvps = _pick_latest_value(ratio_df, ("bvps", "bvps_vnd", "book_value_ps"))
	except Exception as exc:
		log.warning("Manual valuation ratio fetch failed for %s: %s", sym, exc)
		result = _build_result(
			sym,
			price=None,
			eps=None,
			bvps=None,
			source="fresh",
			error=str(exc),
			needs_admin_alert=True,
		)
		_save_to_cache(result)
		return result

	try:
		price_value = price if price is not None else _fetch_price(sym)
	except Exception as exc:
		log.warning("Manual valuation price fetch failed for %s: %s", sym, exc)
		result = _build_result(
			sym,
			price=None,
			eps=eps,
			bvps=bvps,
			source="fresh",
			error=str(exc),
			needs_admin_alert=True,
		)
		_save_to_cache(result)
		return result

	missing_reasons: list[str] = []
	if _safe_positive(eps) is None:
		missing_reasons.append("EPS")
	if _safe_positive(bvps) is None:
		missing_reasons.append("BVPS")
	if _safe_positive(price_value) is None:
		missing_reasons.append("price")

	error_msg = None
	needs_alert = False
	if missing_reasons:
		error_msg = f"Missing {'/'.join(missing_reasons)}"
		needs_alert = True

	result = _build_result(
		sym,
		price=price_value,
		eps=eps,
		bvps=bvps,
		source="fresh",
		error=error_msg,
		needs_admin_alert=needs_alert,
	)
	_save_to_cache(result)
	return result

