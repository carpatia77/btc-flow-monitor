"""
Vectorized Black-Scholes Greeks — transplanted from spx-gex-pro and adapted for BTC/Deribit.

Key adaptations for crypto options:
- No dividend yield (q=0). BTC does not pay dividends.
- Risk-free rate from Deribit's own `interest_rate` field or a sane default.
- Gamma is calculated identically (it's model-agnostic for European options).

All operations are NumPy-vectorized: N contracts evaluated in a single pass.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm


def vectorized_gamma(
    spot: float,
    strikes: np.ndarray,
    T: np.ndarray,
    vol: np.ndarray,
    r: float = 0.05,
    q: float = 0.0,       # BTC has no dividend yield
) -> np.ndarray:
    """
    Calculates Black-Scholes gamma for N contracts simultaneously.

    Parameters
    ----------
    spot : float
        Current underlying price (BTC/USD).
    strikes : np.ndarray
        Array of strike prices.
    T : np.ndarray
        Time to expiration in years for each contract.
    vol : np.ndarray
        Implied volatility (decimal) for each contract.
    r : float
        Risk-free interest rate (decimal).
    q : float
        Continuous dividend yield (0 for BTC).

    Returns
    -------
    np.ndarray
        Gamma values for each contract.
    """
    # Prevent division by zero on near-expiry or zero-vol contracts
    T = np.maximum(T, 1e-6)
    vol = np.maximum(vol, 1e-6)

    # Forward price adjustment (European options)
    forward = spot * np.exp((r - q) * T)

    d1 = (np.log(forward / strikes) + 0.5 * vol**2 * T) / (vol * np.sqrt(T))
    gamma = norm.pdf(d1) / (spot * vol * np.sqrt(T))
    return gamma


def vectorized_gamma_profile(
    spot: float,
    strikes: np.ndarray,
    T: np.ndarray,
    vol: np.ndarray,
    oi: np.ndarray,
    contract_types: np.ndarray,
    n_levels: int = 60,
    r: float = 0.05,
    q: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Computes the full gamma profile across a simulated price grid using 2D broadcasting.

    For BTC the contract multiplier is 1 (not 100 like SPX),
    so the GEX formula is: Gamma * OI * Spot^2 * 0.01 * sign

    Parameters
    ----------
    spot : float
        Current BTC spot price.
    strikes, T, vol, oi : np.ndarray
        Contract-level arrays.
    contract_types : np.ndarray
        +1 for calls, -1 for puts.
    n_levels : int
        Number of price levels to simulate.
    r, q : float
        Interest rate and dividend yield.

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        (price_levels, total_gex_at_each_level)
    """
    T = np.maximum(T, 1e-6)
    vol = np.maximum(vol, 1e-6)

    levels = np.linspace(0.80 * spot, 1.20 * spot, n_levels)

    # Broadcasting: levels (M,1) x strikes (1,N) -> (M,N) matrix
    levels_2d = levels[:, np.newaxis]     # (M, 1)
    strikes_2d = strikes[np.newaxis, :]   # (1, N)
    T_2d = T[np.newaxis, :]              # (1, N)
    vol_2d = vol[np.newaxis, :]          # (1, N)

    forward_2d = levels_2d * np.exp((r - q) * T_2d)
    d1 = (np.log(forward_2d / strikes_2d) + 0.5 * vol_2d**2 * T_2d) / (vol_2d * np.sqrt(T_2d))
    gamma_2d = norm.pdf(d1) / (levels_2d * vol_2d * np.sqrt(T_2d))

    # BTC GEX per contract: Gamma * OI * Spot^2 * 0.01 * sign
    # (Deribit: 1 contract = 1 BTC, so multiplier = 1)
    gex_2d = gamma_2d * oi[np.newaxis, :] * (levels_2d ** 2) * 0.01 * contract_types[np.newaxis, :]

    total_gamma = gex_2d.sum(axis=1)  # shape (M,)
    return levels, total_gamma
