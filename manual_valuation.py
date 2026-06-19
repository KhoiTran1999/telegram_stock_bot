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
TRADING_SOURCE = "KBS"


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
	# First, check if candidates are in columns (like KBS parser or old format made them)
	for col in candidates:
		if col in df.columns:
			# If the column has numbers, calculate the sum
			series_str = df[col].astype(str).str.replace(',', '', regex=False)
			series = pd.to_numeric(series_str, errors="coerce").dropna()
			if not series.empty:
				return float(series.head(periods).sum())

	# Second, check if candidates are in rows (like VCI raw data)
	item_col = next((c for c in df.columns if c.lower() in ['item', 'chỉ tiêu']), None)
	if item_col:
		for col in candidates:
			# VCI column names might match exactly
			mask = df[item_col].astype(str).str.lower() == col.lower()
			if mask.any():
				row = df[mask].iloc[0]
				period_vals = []
				for c in df.columns:
					if c != item_col and c != '_fallback_order' and not str(c).startswith('_'):
						val = str(row[c]).replace(',', '')
						try:
							period_vals.append(float(val))
						except (ValueError, TypeError):
							pass
				if period_vals:
					# Normally data is sorted newest to oldest. We want the trailing sum.
					# Take the first 'periods' values
					return float(sum(period_vals[:periods]))

	# VCI ratio quarter does NOT contain EPS or BVPS! It only contains P/E and P/B!
	# We can't sum P/E or P/B. If the user asks for P/E and the API doesn't return EPS...
	# We can extract P/E directly from the VCI data if they requested P/E, but _trailing_sum is for EPS.

	return None

def _pick_latest_value(df: pd.DataFrame, candidates: Iterable[str]) -> float | None:
	# Same logic for picking latest value
	for col in candidates:
		if col in df.columns:
			series_str = df[col].astype(str).str.replace(',', '', regex=False)
			series = pd.to_numeric(series_str, errors="coerce").dropna()
			if not series.empty:
				return float(series.iloc[0])

	item_col = next((c for c in df.columns if c.lower() in ['item', 'chỉ tiêu']), None)
	if item_col:
		for col in candidates:
			mask = df[item_col].astype(str).str.lower() == col.lower()
			if mask.any():
				row = df[mask].iloc[0]
				for c in df.columns:
					if c != item_col and c != '_fallback_order' and not str(c).startswith('_'):
						val = str(row[c]).replace(',', '')
						try:
							return float(val)
						except (ValueError, TypeError):
							pass
	return None

def _extract_direct_pe_pb(df: pd.DataFrame) -> tuple[float | None, float | None]:
	pe, pb = None, None
	item_col = next((c for c in df.columns if c.lower() in ['item', 'chỉ tiêu']), None)
	if item_col:
		for idx, row in df.iterrows():
			item_name = str(row[item_col]).lower()
			if 'p/e' in item_name or 'p\e' in item_name:
				for c in df.columns:
					if c != item_col and c != '_fallback_order' and not str(c).startswith('_'):
						val = str(row[c]).replace(',', '')
						try:
							pe = float(val)
							break
						except (ValueError, TypeError):
							pass
			if 'p/b' in item_name or 'p\b' in item_name:
				for c in df.columns:
					if c != item_col and c != '_fallback_order' and not str(c).startswith('_'):
						val = str(row[c]).replace(',', '')
						try:
							pb = float(val)
							break
						except (ValueError, TypeError):
							pass
	return pe, pb


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
			"close_price",
			"last_price",
			"match_price",
		],
	)
	if price is None or price <= 0:
		price = _extract_board_value(
			row,
			[
				"reference_price",
				"ref_price",
			],
		)
	if price is None or price <= 0:
		raise RuntimeError("price_board did not contain a valid price")
	if 0 < price < 500:
		price *= 1000.0
	return float(price)


def _fetch_ratio_quarter(symbol: str) -> pd.DataFrame:
	try:
		finance = Finance(symbol=symbol, source="VCI")
		df = finance.ratio(period="quarter", lang="vi")
	except Exception:
		# Fallback to KBS source if VCI fails (like KeyError 'data')
		finance = Finance(symbol=symbol, source="KBS")
		df = finance.ratio(period="quarter", lang="vi")

		# KBS returns transposed data compared to VCI
		# We need to map it back to the expected format
		if df is not None and not df.empty and 'item' in df.columns:
			# Get all period columns (e.g., '2026-Q1', '2025-Q4')
			period_cols = [c for c in df.columns if c not in ('item', 'item_id')]

			# Create a new structure
			new_data = []
			for col in period_cols:
				try:
					year, quarter = col.split('-Q')
					year = int(year)
					# Handle duplicate quarter columns like '2025-Q4_1'
					quarter = int(quarter.split('_')[0])

					row_data = {
						'year': year,
						'quarter': quarter
					}

					# Map values from the KBS rows to columns
					for idx, row in df.iterrows():
						item_name = str(row['item']).lower()
						val = row[col]

						# Only try to cast to float if it looks like a number, or it's NaN
						try:
							# Remove comma separators like "1,668.51"
							if isinstance(val, str):
								val = val.replace(',', '')
							val = float(val) if pd.notna(val) else None
						except (ValueError, TypeError):
							val = None

						# For KBS, we only have these strings:
						# "Thu nhập trên mỗi cổ phần của 4 quý gần nhất (EPS)"
						# "Giá trị sổ sách của cổ phiếu (BVPS)"
						if 'eps' in item_name or 'thu nhập trên mỗi cổ phần' in item_name:
							row_data['eps'] = val
							row_data['eps_vnd'] = val
						elif 'bvps' in item_name or 'giá trị sổ sách' in item_name:
							row_data['bvps'] = val
							row_data['bvps_vnd'] = val

					new_data.append(row_data)
				except ValueError:
					pass

			df = pd.DataFrame(new_data)

			# Fix the sorting error by making sure the year and quarter columns are ints
			if not df.empty and 'year' in df.columns:
				df['year'] = df['year'].astype(int)
				df['quarter'] = df['quarter'].astype(int)

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

