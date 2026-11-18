"""
Modulo 2 - Market Regime Detector.

Classifica un sottostante su due assi:
- Trend: BULLISH / BEARISH / SIDEWAYS (EMA20, EMA50, ADX14 di Wilder)
- Volatilità: HIGH_IV / LOW_IV (proxy di IV Rank: IV ATM corrente vs range della HV20 a 1 anno)

Tutte le funzioni sono pure (input pandas -> output numerico) e quindi facilmente testabili.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd
from pydantic import BaseModel


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"


class VolRegime(str, Enum):
    HIGH_IV = "HIGH_IV"
    LOW_IV = "LOW_IV"


class MarketRegime(BaseModel):
    ticker: str
    price: float
    ema_fast: float
    ema_slow: float
    adx: float
    trend: Trend
    atm_iv: float | None
    hv_current: float
    hv_min: float
    hv_max: float
    iv_rank: float
    volatility: VolRegime

    def describe(self) -> str:
        iv_txt = f"{self.atm_iv:.1%}" if self.atm_iv is not None else "n/d"
        return (
            f"{self.ticker}: prezzo {self.price:.2f} | EMA20 {self.ema_fast:.2f} vs EMA50 {self.ema_slow:.2f} | "
            f"ADX {self.adx:.1f} -> {self.trend.value} | IV ATM {iv_txt}, HV20 range "
            f"[{self.hv_min:.1%}-{self.hv_max:.1%}] -> IVR {self.iv_rank:.0f} -> {self.volatility.value}"
        )


# ---------------------------------------------------------------------- #
# Indicatori
# ---------------------------------------------------------------------- #
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index con smoothing di Wilder (alpha = 1/period)."""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)

    prev_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    alpha = 1.0 / period
    atr = true_range.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean().fillna(0.0)


def historical_volatility(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Volatilità realizzata annualizzata (deviazione standard dei log-rendimenti)."""
    log_returns = np.log(close / close.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(periods_per_year)


def iv_rank_proxy(current_iv: float | None, hv_series: pd.Series) -> float:
    """
    Proxy di IV Rank (0-100): posizione della IV ATM attuale nel range [min, max] della HV dell'ultimo anno.

    Nota didattica: la IV implicita tende a stare sopra la volatilità realizzata (volatility risk
    premium), quindi questa proxy è tendenzialmente "ottimista". Un vero IV Rank richiede lo storico
    della IV, che i feed gratuiti non forniscono.
    """
    hv = hv_series.dropna()
    if current_iv is None or hv.empty:
        return 0.0
    hv_min, hv_max = float(hv.min()), float(hv.max())
    if hv_max - hv_min <= 1e-9:
        return 50.0
    return float(np.clip((current_iv - hv_min) / (hv_max - hv_min) * 100.0, 0.0, 100.0))


# ---------------------------------------------------------------------- #
# Classificazione
# ---------------------------------------------------------------------- #
def classify_trend(price: float, ema_fast: float, ema_slow: float, adx_value: float, adx_threshold: float = 20.0) -> Trend:
    if adx_value < adx_threshold:
        return Trend.SIDEWAYS
    if ema_fast > ema_slow and price > ema_fast:
        return Trend.BULLISH
    if ema_fast < ema_slow and price < ema_fast:
        return Trend.BEARISH
    # ADX alto ma segnali EMA/prezzo contrastanti: nessun trend affidabile
    return Trend.SIDEWAYS


def classify_volatility(iv_rank: float, threshold: float = 50.0) -> VolRegime:
    return VolRegime.HIGH_IV if iv_rank >= threshold else VolRegime.LOW_IV


def detect_regime(
    ticker: str,
    bars: pd.DataFrame,
    atm_iv: float | None,
    *,
    ema_fast_period: int = 20,
    ema_slow_period: int = 50,
    adx_period: int = 14,
    adx_threshold: float = 20.0,
    hv_window: int = 20,
    iv_rank_threshold: float = 50.0,
) -> MarketRegime:
    """Calcola il regime a partire dalle barre daily (colonne high/low/close) e dalla IV ATM corrente."""
    if len(bars) < ema_slow_period + adx_period:
        raise ValueError(f"{ticker}: storico insufficiente ({len(bars)} barre)")

    close, high, low = bars["close"].astype(float), bars["high"].astype(float), bars["low"].astype(float)
    ema_f = float(ema(close, ema_fast_period).iloc[-1])
    ema_s = float(ema(close, ema_slow_period).iloc[-1])
    adx_value = float(adx(high, low, close, adx_period).iloc[-1])
    price = float(close.iloc[-1])

    hv = historical_volatility(close, hv_window)
    hv_clean = hv.dropna()
    ivr = iv_rank_proxy(atm_iv, hv)

    return MarketRegime(
        ticker=ticker,
        price=price,
        ema_fast=ema_f,
        ema_slow=ema_s,
        adx=adx_value,
        trend=classify_trend(price, ema_f, ema_s, adx_value, adx_threshold),
        atm_iv=atm_iv,
        hv_current=float(hv_clean.iloc[-1]) if not hv_clean.empty else 0.0,
        hv_min=float(hv_clean.min()) if not hv_clean.empty else 0.0,
        hv_max=float(hv_clean.max()) if not hv_clean.empty else 0.0,
        iv_rank=ivr,
        volatility=classify_volatility(ivr, iv_rank_threshold),
    )
