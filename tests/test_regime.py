"""Unit test per il Market Regime Detector e la logica dello screener (nessuna chiamata di rete)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from src.analysis.regime import (
    Trend,
    VolRegime,
    adx,
    classify_trend,
    classify_volatility,
    detect_regime,
    historical_volatility,
    iv_rank_proxy,
)
from src.analysis.screener import compute_opportunity_score
from src.execution.risk_manager import evaluate_exit, position_size
from src.strategy.selector import StrategyType, choose_strategy


# ---------------------------------------------------------------------- #
# Fixture: serie sintetiche
# ---------------------------------------------------------------------- #
def make_bars(close: np.ndarray, noise: float = 0.5) -> pd.DataFrame:
    idx = pd.bdate_range("2025-01-01", periods=len(close))
    high = close + noise
    low = close - noise
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1_000_000}, index=idx)


@pytest.fixture
def uptrend() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 + np.arange(252) * 0.5 + rng.normal(0, 0.3, 252)
    return make_bars(close)


@pytest.fixture
def downtrend() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    close = 250 - np.arange(252) * 0.5 + rng.normal(0, 0.3, 252)
    return make_bars(close)


@pytest.fixture
def sideways() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    close = 100 + 1.5 * np.sin(np.arange(252) / 3.0) + rng.normal(0, 0.8, 252)
    return make_bars(close, noise=1.2)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, DRY_RUN=True)


# ---------------------------------------------------------------------- #
# Regime
# ---------------------------------------------------------------------- #
def test_uptrend_is_bullish(uptrend: pd.DataFrame) -> None:
    regime = detect_regime("TEST", uptrend, atm_iv=0.20)
    assert regime.trend == Trend.BULLISH
    assert regime.ema_fast > regime.ema_slow
    assert regime.adx >= 20


def test_downtrend_is_bearish(downtrend: pd.DataFrame) -> None:
    regime = detect_regime("TEST", downtrend, atm_iv=0.20)
    assert regime.trend == Trend.BEARISH
    assert regime.ema_fast < regime.ema_slow


def test_range_is_sideways(sideways: pd.DataFrame) -> None:
    regime = detect_regime("TEST", sideways, atm_iv=0.20)
    assert regime.trend == Trend.SIDEWAYS
    assert regime.adx < 20


def test_adx_bounded(uptrend: pd.DataFrame) -> None:
    values = adx(uptrend["high"], uptrend["low"], uptrend["close"], 14)
    assert values.between(0, 100).all()


def test_insufficient_history_raises(uptrend: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        detect_regime("TEST", uptrend.head(30), atm_iv=0.2)


@pytest.mark.parametrize(
    ("price", "fast", "slow", "adx_value", "expected"),
    [
        (110, 105, 100, 25, Trend.BULLISH),
        (90, 95, 100, 25, Trend.BEARISH),
        (110, 105, 100, 15, Trend.SIDEWAYS),  # ADX basso
        (102, 105, 100, 30, Trend.SIDEWAYS),  # EMA rialziste ma prezzo sotto EMA20: segnale contrastante
    ],
)
def test_classify_trend(price: float, fast: float, slow: float, adx_value: float, expected: Trend) -> None:
    assert classify_trend(price, fast, slow, adx_value) == expected


def test_iv_rank_proxy_range() -> None:
    hv = pd.Series([0.10, 0.20, 0.30])
    assert iv_rank_proxy(0.10, hv) == pytest.approx(0.0)
    assert iv_rank_proxy(0.20, hv) == pytest.approx(50.0)
    assert iv_rank_proxy(0.30, hv) == pytest.approx(100.0)
    assert iv_rank_proxy(0.50, hv) == 100.0  # clip superiore
    assert iv_rank_proxy(0.05, hv) == 0.0  # clip inferiore
    assert iv_rank_proxy(None, hv) == 0.0


def test_classify_volatility() -> None:
    assert classify_volatility(50) == VolRegime.HIGH_IV
    assert classify_volatility(49.9) == VolRegime.LOW_IV


def test_historical_volatility_annualized() -> None:
    rng = np.random.default_rng(4)
    daily_sigma = 0.01
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, daily_sigma, 2000))))
    hv = historical_volatility(close, window=20).dropna()
    assert hv.mean() == pytest.approx(daily_sigma * np.sqrt(252), rel=0.1)


# ---------------------------------------------------------------------- #
# Matrice strategie
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("trend", "vol", "expected"),
    [
        (Trend.SIDEWAYS, VolRegime.HIGH_IV, StrategyType.IRON_CONDOR),
        (Trend.SIDEWAYS, VolRegime.LOW_IV, StrategyType.NO_TRADE),
        (Trend.BULLISH, VolRegime.HIGH_IV, StrategyType.BULL_PUT_SPREAD),
        (Trend.BULLISH, VolRegime.LOW_IV, StrategyType.BULL_CALL_SPREAD),
        (Trend.BEARISH, VolRegime.HIGH_IV, StrategyType.BEAR_CALL_SPREAD),
        (Trend.BEARISH, VolRegime.LOW_IV, StrategyType.BEAR_PUT_SPREAD),
    ],
)
def test_strategy_matrix(trend: Trend, vol: VolRegime, expected: StrategyType) -> None:
    assert choose_strategy(trend, vol) == expected


def test_single_leg_debit_option() -> None:
    assert choose_strategy(Trend.BULLISH, VolRegime.LOW_IV, prefer_single_leg_debit=True) == StrategyType.LONG_CALL
    assert choose_strategy(Trend.BEARISH, VolRegime.LOW_IV, prefer_single_leg_debit=True) == StrategyType.LONG_PUT


# ---------------------------------------------------------------------- #
# Opportunity score
# ---------------------------------------------------------------------- #
def test_score_zero_for_sideways_low_iv() -> None:
    assert compute_opportunity_score(Trend.SIDEWAYS, VolRegime.LOW_IV, 10, 12, 0.02, 0.10) == 0.0


def test_score_rewards_higher_iv_rank_for_credit() -> None:
    low = compute_opportunity_score(Trend.BULLISH, VolRegime.HIGH_IV, 55, 30, 0.03, 0.10)
    high = compute_opportunity_score(Trend.BULLISH, VolRegime.HIGH_IV, 95, 30, 0.03, 0.10)
    assert high > low


def test_score_rewards_higher_adx_for_directional() -> None:
    weak = compute_opportunity_score(Trend.BEARISH, VolRegime.LOW_IV, 20, 22, 0.03, 0.10)
    strong = compute_opportunity_score(Trend.BEARISH, VolRegime.LOW_IV, 20, 45, 0.03, 0.10)
    assert strong > weak


def test_score_rewards_tighter_spread() -> None:
    wide = compute_opportunity_score(Trend.SIDEWAYS, VolRegime.HIGH_IV, 80, 12, 0.09, 0.10)
    tight = compute_opportunity_score(Trend.SIDEWAYS, VolRegime.HIGH_IV, 80, 12, 0.01, 0.10)
    assert tight > wide


def test_score_bounds() -> None:
    best = compute_opportunity_score(Trend.BULLISH, VolRegime.HIGH_IV, 100, 80, 0.0, 0.10)
    worst = compute_opportunity_score(Trend.BULLISH, VolRegime.HIGH_IV, 0, 0, 0.5, 0.10)
    assert best == pytest.approx(100.0)
    assert worst == pytest.approx(0.0)


# ---------------------------------------------------------------------- #
# Risk: sizing e regole di uscita
# ---------------------------------------------------------------------- #
def test_position_size() -> None:
    assert position_size(350, 100_000, 0.02, 10) == 5  # 2000 / 350 = 5.7 -> 5
    assert position_size(2500, 100_000, 0.02, 10) == 0  # un contratto supera il rischio
    assert position_size(50, 100_000, 0.02, 10) == 10  # cap MAX_CONTRACTS


def test_exit_credit_take_profit(settings: Settings) -> None:
    # credito 1.50, ora costa 0.70 per ricomprare -> +0.80 (53%) >= 50%
    reason, pnl = evaluate_exit(-1.50, -0.70, 30, settings)
    assert reason == "TAKE_PROFIT"
    assert pnl == pytest.approx(0.80)


def test_exit_credit_stop_loss(settings: Settings) -> None:
    # credito 1.50, ora costa 3.00 -> -1.50 (-100%)
    reason, _ = evaluate_exit(-1.50, -3.00, 30, settings)
    assert reason == "STOP_LOSS"


def test_exit_debit_rules(settings: Settings) -> None:
    assert evaluate_exit(2.00, 2.85, 30, settings)[0] == "TAKE_PROFIT"  # +42.5%
    assert evaluate_exit(2.00, 1.35, 30, settings)[0] == "STOP_LOSS"  # -32.5%
    assert evaluate_exit(2.00, 2.10, 30, settings)[0] is None


def test_exit_dte_threshold(settings: Settings) -> None:
    reason, _ = evaluate_exit(-1.50, -1.40, 7, settings)
    assert reason == "DTE_THRESHOLD"


def test_settings_watchlist_parsing() -> None:
    s = Settings(_env_file=None, WATCHLIST="spy, qqq ,IWM")
    assert s.WATCHLIST == ["SPY", "QQQ", "IWM"]
    assert Settings(_env_file=None).DRY_RUN is True


# ---------------------------------------------------------------------- #
# Executor: piano di chasing
# ---------------------------------------------------------------------- #
def test_price_ladder_credit_reaches_natural() -> None:
    from src.execution.executor import price_ladder

    # credit spread: mid -1.50 (incasso 1.50), naturale -1.30 (incasso 1.30)
    ladder = price_ladder(-1.50, -1.30, attempts=3, min_step=0.01)
    assert ladder[0] == -1.50
    assert ladder[-1] == -1.30  # l'ultimo tentativo è al prezzo naturale
    assert all(b > a for a, b in zip(ladder, ladder[1:]))  # sempre verso il mercato


def test_price_ladder_tight_spread_respects_min_step_and_cap() -> None:
    from src.execution.executor import price_ladder

    ladder = price_ladder(2.00, 2.02, attempts=3, min_step=0.01)
    assert ladder == [2.00, 2.01, 2.02]  # niente oltre il naturale


def test_price_ladder_classic_mode() -> None:
    from src.execution.executor import price_ladder

    assert price_ladder(-1.50, -1.30, attempts=3, min_step=0.01, to_natural=False) == [-1.50, -1.49, -1.48, -1.47]
