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
    assert evaluate_exit(2.00, 0.95, 30, settings)[0] == "STOP_LOSS"  # -52.5%
    assert evaluate_exit(2.00, 1.35, 30, settings)[0] is None  # -32.5%: con lo stop al 50% si tiene
    assert evaluate_exit(2.00, 2.10, 30, settings)[0] is None


def test_exit_dte_threshold(settings: Settings) -> None:
    reason, _ = evaluate_exit(-1.50, -1.40, 7, settings)
    assert reason == "DTE_THRESHOLD"


def test_settings_watchlist_parsing() -> None:
    s = Settings(_env_file=None, WATCHLIST="spy, qqq ,IWM")
    assert s.WATCHLIST == ["SPY", "QQQ", "IWM"]
    assert Settings(_env_file=None).DRY_RUN is False


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


# ---------------------------------------------------------------------- #
# Soglie di uscita configurabili
# ---------------------------------------------------------------------- #
def test_custom_take_profit_closes_earlier() -> None:
    # NVDA reale: credito 1.25, P&L +0.385/unità (+31%). Con TP 50% resta aperta, con TP 30% si chiude.
    default = Settings(_env_file=None)
    custom = Settings(_env_file=None, TAKE_PROFIT_CREDIT_PCT="30%")
    assert evaluate_exit(-1.25, -0.865, 35, default)[0] is None
    assert evaluate_exit(-1.25, -0.865, 35, custom)[0] == "TAKE_PROFIT"


def test_percent_parsing_and_bounds() -> None:
    from pydantic import ValidationError

    assert Settings(_env_file=None, TAKE_PROFIT_CREDIT_PCT="30%").TAKE_PROFIT_CREDIT_PCT == pytest.approx(0.30)
    assert Settings(_env_file=None, TAKE_PROFIT_CREDIT_PCT="0.3").TAKE_PROFIT_CREDIT_PCT == pytest.approx(0.30)
    assert Settings(_env_file=None, STOP_LOSS_DEBIT_PCT="25%").STOP_LOSS_DEBIT_PCT == pytest.approx(0.25)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TAKE_PROFIT_CREDIT_PCT="150%")  # oltre il massimo teorico del credito
    with pytest.raises(ValidationError):
        Settings(_env_file=None, TAKE_PROFIT_CREDIT_PCT="0")


def test_bot_status_row_for_dashboard() -> None:
    """La riga bot_status contiene intervalli, scadenza sessione e orari Alpaca in UTC ISO."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from src.main import BotStatus

    class FakeStorage:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def update_bot_status(self, fields: dict) -> None:
            self.rows.append(fields)

    s = Settings(_env_file=None, TRADE_INTERVAL_MIN=5, REFRESH_INTERVAL_SEC=45, SESSION_MODE=True)
    storage = FakeStorage()
    status = BotStatus(storage, s, "abc123")  # type: ignore[arg-type]
    start = datetime(2026, 11, 23, 14, 31, tzinfo=timezone.utc)
    status.session_started_at = start
    status.session_deadline = start + timedelta(minutes=s.SESSION_MAX_MINUTES)
    clock = SimpleNamespace(
        is_open=True,
        next_open=datetime(2026, 11, 24, 9, 30, tzinfo=timezone(timedelta(hours=-5))),
        next_close=datetime(2026, 11, 23, 16, 0, tzinfo=timezone(timedelta(hours=-5))),
        timestamp=start,
    )
    status.publish("RUNNING", clock, "ok")
    row = storage.rows[-1]
    assert row["state"] == "RUNNING" and row["run_id"] == "abc123"
    assert row["trade_interval_min"] == 5 and row["refresh_interval_sec"] == 45
    assert row["market_is_open"] is True
    assert row["market_next_close"] == "2026-11-23T21:00:00+00:00"
    assert row["market_next_open"] == "2026-11-24T14:30:00+00:00"
    assert row["session_deadline"] == (start + timedelta(minutes=s.SESSION_MAX_MINUTES)).isoformat()

    status.publish("STOPPED", None, "fine")  # senza clock non sovrascrive gli orari di mercato
    assert "market_next_open" not in storage.rows[-1]


# ---------------------------------------------------------------------- #
# Protezioni introdotte dopo l'analisi dei trade del 23/11 (QQQ e GLD)
# ---------------------------------------------------------------------- #
def _close_legs(short_bid: float, short_ask: float, long_bid: float, long_ask: float):
    from alpaca.trading.enums import OrderSide, PositionIntent

    from src.execution.executor import OrderLegSpec

    return [
        OrderLegSpec("QQQ261224C00695000", OrderSide.BUY, PositionIntent.BUY_TO_CLOSE, 1, short_bid, short_ask),
        OrderLegSpec("QQQ261224C00700000", OrderSide.SELL, PositionIntent.SELL_TO_CLOSE, 1, long_bid, long_ask),
    ]


def test_trading_window_blocks_first_minutes_after_open() -> None:
    from datetime import datetime, timezone

    from src.main import trading_window

    s = Settings(_env_file=None)
    close = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
    w = trading_window(datetime(2026, 11, 23, 14, 30, 36, tzinfo=timezone.utc), close, s)  # 9:30:36 New York
    assert not w.exits_allowed and not w.new_trades_allowed
    assert w.resume_at == datetime(2026, 11, 23, 14, 45, tzinfo=timezone.utc)
    w = trading_window(datetime(2026, 11, 23, 14, 45, 5, tzinfo=timezone.utc), close, s)
    assert w.exits_allowed and w.new_trades_allowed
    w = trading_window(datetime(2026, 11, 23, 20, 55, tzinfo=timezone.utc), close, s)  # 5 min alla chiusura
    assert w.exits_allowed and not w.new_trades_allowed
    # ora legale USA: l'apertura è alle 13:30 UTC
    summer_close = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
    assert not trading_window(datetime(2026, 7, 1, 13, 35, tzinfo=timezone.utc), summer_close, s).exits_allowed
    assert trading_window(datetime(2026, 7, 1, 13, 46, tzinfo=timezone.utc), summer_close, s).exits_allowed


def test_take_profit_close_can_never_become_a_loss() -> None:
    """QQQ: credito 1.33, TP 50% -> la chiusura non deve mai pagare più di 0.665 (prima pagò 4.92)."""
    from src.execution.executor import net_mid, net_natural, price_ladder
    from src.execution.risk_manager import exit_price_cap

    s = Settings(_env_file=None)
    legs = _close_legs(0.20, 5.00, 0.08, 0.60)  # quote da apertura: naturale 4.92
    assert round(net_natural(legs), 2) == 4.92
    cap = exit_price_cap("TAKE_PROFIT", -1.33, net_mid(legs), 5.0, s)
    assert cap <= 0.665 + 1e-9
    ladder = price_ladder(net_mid(legs), net_natural(legs), 3, 0.01, True, cap=cap)
    assert max(ladder) <= 0.67


def test_wide_quotes_are_detected() -> None:
    from src.execution.executor import quotes_too_wide

    assert quotes_too_wide(_close_legs(0.20, 5.00, 0.08, 0.60), 5.0, 0.20)       # apertura: naturale lontanissimo
    assert not quotes_too_wide(_close_legs(0.55, 0.65, 0.20, 0.26), 5.0, 0.20)   # mercato normale


def test_debit_entry_is_capped() -> None:
    """GLD: mid 2.945 su larghezza 5 -> mai pagare più di min(mid + 10% larghezza, 65% larghezza) = 3.25 (pagò 4.15)."""
    from alpaca.trading.enums import OrderSide, PositionIntent

    from src.execution.executor import OrderLegSpec, net_mid, net_natural, price_cap, price_ladder

    s = Settings(_env_file=None)
    legs = [
        OrderLegSpec("GLD261231P00350000", OrderSide.BUY, PositionIntent.BUY_TO_OPEN, 1, 17.48, 19.00),
        OrderLegSpec("GLD261231P00345000", OrderSide.SELL, PositionIntent.SELL_TO_OPEN, 1, 14.85, 15.53),
    ]
    cap = min(price_cap(legs, 5.0, s.MAX_SLIPPAGE_PCT_OF_WIDTH), s.MAX_DEBIT_PCT_OF_WIDTH * 5.0)
    assert cap == pytest.approx(3.25)
    ladder = price_ladder(net_mid(legs), net_natural(legs), 3, 0.01, True, cap=cap)
    assert max(ladder) <= 3.25


class _FakeExecutor:
    def __init__(self, status: str = "filled") -> None:
        self.calls: list[tuple] = []
        self.status = status

    def execute(self, legs, qty, label, max_net=None):
        from src.execution.executor import ExecutionResult, net_mid

        self.calls.append((label, qty, max_net))
        if self.status == "filled":
            price = min(net_mid(legs), max_net) if max_net is not None else net_mid(legs)
            return ExecutionResult(filled=True, filled_qty=qty, fill_net_price=price, status="filled",
                                   leg_fills={leg.symbol: round(leg.mid, 2) for leg in legs})
        return ExecutionResult(filled=False, status=self.status)


def _risk_manager(quotes: dict, executor, session: bool = True):
    from types import SimpleNamespace

    from alpaca.trading.enums import AssetClass, PositionSide

    from src.execution.risk_manager import RiskManager

    s = Settings(_env_file=None, SESSION_MODE=session)
    positions = [
        SimpleNamespace(symbol="QQQ261224C00695000", asset_class=AssetClass.US_OPTION, qty="-5",
                        side=PositionSide.SHORT, avg_entry_price="9.40", current_price="1.0"),
        SimpleNamespace(symbol="QQQ261224C00700000", asset_class=AssetClass.US_OPTION, qty="5",
                        side=PositionSide.LONG, avg_entry_price="8.07", current_price="0.5"),
    ]
    trade = {"id": 2, "ticker": "QQQ", "strategy": "BEAR_CALL_SPREAD", "entry_price": -1.33, "qty": 5,
             "legs_json": {"width": 5.0, "legs": [{"symbol": p.symbol} for p in positions]}}

    class Storage:
        closed: list = []

        def get_open_trades(self):
            return [trade]

        def update_trade(self, *a):
            pass

        def close_trade(self, trade_id, exit_price, pnl, reason, legs_json=None):
            self.closed.append((trade_id, exit_price, pnl, reason))
            self.legs_json = legs_json

        def get_recent_stop_tickers(self, since):
            return []

    client = SimpleNamespace(get_all_positions=lambda: positions, get_orders=lambda req: [])
    market = SimpleNamespace(get_option_quotes=lambda symbols: quotes)
    storage = Storage()
    storage.closed = []
    return RiskManager(s, client, market, executor, storage), storage  # type: ignore[arg-type]


def test_risk_manager_skips_take_profit_on_wide_quotes() -> None:
    ex = _FakeExecutor()
    rm, storage = _risk_manager({"QQQ261224C00695000": (0.05, 1.85), "QQQ261224C00700000": (0.0, 0.60)}, ex)
    report = rm.manage_open_positions()
    assert ex.calls == [] and storage.closed == [] and report.open_count == 1


def test_risk_manager_take_profit_closes_with_profit_cap() -> None:
    ex = _FakeExecutor()
    rm, storage = _risk_manager({"QQQ261224C00695000": (0.55, 0.65), "QQQ261224C00700000": (0.20, 0.26)}, ex)
    rm.manage_open_positions()
    assert len(ex.calls) == 1 and ex.calls[0][2] <= 0.665 + 1e-9
    trade_id, exit_price, pnl, reason = storage.closed[0]
    assert reason == "TAKE_PROFIT" and pnl > 0
    exit_fills = {leg["symbol"]: leg["exit_fill"] for leg in storage.legs_json["legs"]}
    assert exit_fills == {"QQQ261224C00695000": 0.6, "QQQ261224C00700000": 0.23}


def test_risk_manager_stop_loss_needs_confirmation_and_no_leg_fallback() -> None:
    # spread a 2.75 di mid (credito 1.33 -> -107% del credito): stop loss
    quotes = {"QQQ261224C00695000": (3.95, 4.05), "QQQ261224C00700000": (1.20, 1.30)}
    ex = _FakeExecutor(status="canceled")
    rm, storage = _risk_manager(quotes, ex)
    rm.manage_open_positions()
    assert ex.calls == []  # 1/2: solo conferma
    rm.manage_open_positions()
    assert len(ex.calls) == 1  # 2/2: prova a chiudere con tetto, ma NON ripiega gamba per gamba
    assert ex.calls[0][0].startswith("CLOSE ") and storage.closed == []
