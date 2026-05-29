"""
GEX + VEX Analytics Engine — Computes Gamma and Vega Exposure from a MarketState snapshot.

This module is designed to be called from a ProcessPoolExecutor (CPU-bound, GIL-free).
It accepts a plain dict snapshot (serializable) and returns a plain dict result.

Architecture note:
- Deribit's `get_book_summary_by_currency` does NOT return Greeks.
- We compute Gamma and Vega locally via vectorized Black-Scholes (black_scholes.py).
- GEX Formula: Gamma * OI * Spot^2 * 0.01 * sign  (1 contract = 1 BTC on Deribit).
- VEX Formula: Vega * OI * sign  (Vega already in USD per 1% IV move).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .black_scholes import vectorized_gamma, vectorized_vega, vectorized_gamma_profile


def calculate_gex_from_snapshot(snapshot: dict) -> dict:
    """
    Main entry point: takes a state snapshot dict and returns a full GEX analysis.

    Parameters
    ----------
    snapshot : dict
        {"spot": float, "chain": {instrument_name: InstrumentData, ...}}

    Returns
    -------
    dict
        Complete GEX analysis payload.
    """
    spot = snapshot["spot"]
    chain = snapshot["chain"]

    if not spot or spot <= 0:
        raise ValueError("Spot price must be > 0 to calculate GEX")
    if not chain:
        raise ValueError("Options chain is empty — no instruments to analyze")

    # ── 1. Build DataFrame from chain dict ───────────────────────────────
    records = []
    now = datetime.now(timezone.utc)

    for inst in chain.values():
        # Parse expiration to compute days-to-expiry
        try:
            exp_date = datetime.strptime(inst.expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue

        dte_days = (exp_date - now).days
        if dte_days < 0:
            continue  # Skip expired contracts

        if inst.open_interest <= 0:
            continue  # No liquidity

        if inst.mark_iv <= 0:
            continue  # Cannot compute gamma without IV

        records.append({
            "instrument_name": inst.instrument_name,
            "strike": inst.strike,
            "expiration": inst.expiration,
            "type": inst.option_type,  # "C" or "P"
            "open_interest": inst.open_interest,
            "mark_iv": inst.mark_iv,   # Already in decimal (e.g. 0.70)
            "dte_days": dte_days,
        })

    if not records:
        raise ValueError("No valid contracts with OI > 0 and future expiration")

    df = pd.DataFrame(records)

    # ── 2. Vectorized Black-Scholes Gamma ────────────────────────────────
    strikes = df["strike"].values
    T = np.maximum(df["dte_days"].values / 365.0, 1e-6)
    vol = df["mark_iv"].values
    oi = df["open_interest"].values
    contract_types = np.where(df["type"].values == "C", 1.0, -1.0)

    # Risk-free rate: use 5% default (could be parameterized or fetched)
    r = 0.05

    gammas = vectorized_gamma(
        spot=spot,
        strikes=strikes,
        T=T,
        vol=vol,
        r=r,
        q=0.0,  # BTC pays no dividends
    )

    # ── 3. GEX Calculation (Deribit: 1 contract = 1 BTC) ────────────────
    # GEX = Gamma * OI * Spot^2 * 0.01 * sign(contract_type)
    gex_values = gammas * oi * (spot ** 2) * 0.01 * contract_types

    df["gamma"] = gammas
    df["gex"] = gex_values

    # ── 3b. VEX Calculation (Vega Exposure) ──────────────────────────────
    # Vega already returned in USD per 1% IV change (per_1pct=True)
    # VEX = Vega * OI * sign(contract_type)
    # No hedge ratio applied — same dealer positioning assumption as GEX (1.0)
    vegas = vectorized_vega(
        spot=spot,
        strikes=strikes,
        T=T,
        vol=vol,
        r=r,
        q=0.0,
        per_1pct=True,
    )

    vex_values = vegas * oi * contract_types

    df["vega"] = vegas
    df["vex"] = vex_values

    # ── 4. Aggregate by Strike ───────────────────────────────────────────
    gex_by_strike = df.groupby("strike")["gex"].sum()
    call_mask = df["type"] == "C"
    put_mask = df["type"] == "P"
    call_gex_by_strike = df[call_mask].groupby("strike")["gex"].sum()
    put_gex_by_strike = df[put_mask].groupby("strike")["gex"].sum()

    # VEX aggregation by strike
    vex_by_strike = df.groupby("strike")["vex"].sum()
    call_vex_by_strike = df[call_mask].groupby("strike")["vex"].sum()
    put_vex_by_strike = df[put_mask].groupby("strike")["vex"].sum()

    # ── 5. Identify Walls ────────────────────────────────────────────────
    total_gex = float(gex_by_strike.sum())

    # Call Wall: strike with highest positive GEX
    call_wall_strike = float(gex_by_strike.idxmax()) if not gex_by_strike.empty else 0.0
    call_wall_gex = float(gex_by_strike.max()) if not gex_by_strike.empty else 0.0

    # Put Wall: strike with lowest (most negative) GEX
    put_wall_strike = float(gex_by_strike.idxmin()) if not gex_by_strike.empty else 0.0
    put_wall_gex = float(gex_by_strike.min()) if not gex_by_strike.empty else 0.0

    # ── 5b. VEX Totals ───────────────────────────────────────────────────
    total_vex = float(vex_by_strike.sum())
    call_vex_total = float(df[call_mask]["vex"].sum())
    put_vex_total = float(df[put_mask]["vex"].sum())

    # ── 6. Gamma Profile & Flip ──────────────────────────────────────────
    levels, profile_values = vectorized_gamma_profile(
        spot=spot,
        strikes=strikes,
        T=T,
        vol=vol,
        oi=oi,
        contract_types=contract_types,
        n_levels=60,
        r=r,
        q=0.0,
    )

    gamma_flip = _find_gamma_flip(levels, profile_values)

    # ── 7. Regime Detection ──────────────────────────────────────────────
    gex_regime = "Positive" if total_gex > 0 else "Negative"
    vex_regime = _classify_vex_regime(total_vex, spot)
    vex_gex_ratio = abs(total_vex) / abs(total_gex) if abs(total_gex) > 1e-6 else 0.0

    # ── 8. Build payload ─────────────────────────────────────────────────
    payload = {
        "asset": "BTC/USD",
        "spot_price": round(spot, 2),

        # GEX metrics
        "total_gex_usd": round(total_gex, 2),
        "gex_regime": gex_regime,
        "call_wall_strike": round(call_wall_strike, 0),
        "call_wall_gex_usd": round(call_wall_gex, 2),
        "put_wall_strike": round(put_wall_strike, 0),
        "put_wall_gex_usd": round(put_wall_gex, 2),
        "gamma_flip": round(gamma_flip, 2) if gamma_flip else None,

        # VEX metrics
        "total_vex_usd": round(total_vex, 2),
        "call_vex_usd": round(call_vex_total, 2),
        "put_vex_usd": round(put_vex_total, 2),
        "vex_regime": vex_regime,
        "vex_gex_ratio": round(vex_gex_ratio, 3),

        # Metadata
        "instruments_analyzed": len(df),
        "gamma_profile": {
            "levels": levels.tolist(),
            "values": profile_values.tolist(),
        },
        "gex_by_strike": {
            str(int(k)): round(v, 2) for k, v in gex_by_strike.items()
        },
        "call_gex_by_strike": {
            str(int(k)): round(v, 2) for k, v in call_gex_by_strike.items()
        },
        "put_gex_by_strike": {
            str(int(k)): round(v, 2) for k, v in put_gex_by_strike.items()
        },
        "vex_by_strike": {
            str(int(k)): round(v, 2) for k, v in vex_by_strike.items()
        },
        "call_vex_by_strike": {
            str(int(k)): round(v, 2) for k, v in call_vex_by_strike.items()
        },
        "put_vex_by_strike": {
            str(int(k)): round(v, 2) for k, v in put_vex_by_strike.items()
        },
    }

    return payload


def run_heavy_math_analysis(snapshot: dict) -> dict:
    """
    Top-level function to be called via ProcessPoolExecutor.run_in_executor().
    Must be a module-level function (pickle-serializable).

    Returns a dict with the full GEX analysis plus timing metadata.
    """
    start = time.perf_counter()
    try:
        result = calculate_gex_from_snapshot(snapshot)
        elapsed_ms = (time.perf_counter() - start) * 1000
        result["computation_time_ms"] = round(elapsed_ms, 2)
        result["status"] = "ok"
        return result
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "asset": "BTC/USD",
            "status": "error",
            "error": str(e),
            "computation_time_ms": round(elapsed_ms, 2),
        }


def _find_gamma_flip(levels: np.ndarray, total_gamma: np.ndarray) -> float | None:
    """
    Find the zero-crossing of the gamma profile (Gamma Flip line).
    Uses linear interpolation between sign-change points for sub-level precision.
    """
    sign_changes = np.where(np.diff(np.sign(total_gamma)))[0]
    if len(sign_changes) == 0:
        return None

    # Take the sign change closest to the center of the profile (nearest spot)
    mid_idx = len(levels) // 2
    closest = sign_changes[np.argmin(np.abs(sign_changes - mid_idx))]

    x1, x2 = levels[closest], levels[closest + 1]
    y1, y2 = total_gamma[closest], total_gamma[closest + 1]

    if y2 - y1 == 0:
        return float(x1)

    gamma_flip = x1 - y1 * (x2 - x1) / (y2 - y1)
    return float(gamma_flip)


def _classify_vex_regime(total_vex_usd: float, spot_price: float) -> str:
    """
    Classify VEX regime using spot-normalized thresholds.

    Normalization ensures the regime classification scales with market size.
    When BTC doubles, the same absolute VEX becomes proportionally smaller.

    Thresholds are expressed as |VEX| / spot_price:
    - Low:      < 70   (minimal vol pressure)
    - Moderate: < 200  (normal vol exposure)
    - Elevated: < 400  (significant vol buildup)
    - Extreme:  >= 400 (squeeze risk zone)
    """
    if spot_price <= 0:
        return "Unknown"
    vex_per_btc = abs(total_vex_usd) / spot_price
    if vex_per_btc < 70:
        return "Low"
    elif vex_per_btc < 200:
        return "Moderate"
    elif vex_per_btc < 400:
        return "Elevated"
    else:
        return "Extreme"
