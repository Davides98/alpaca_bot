"""
Modulo 3 - Screener preliminare.

Per ogni ticker della watchlist:
1. calcola il regime (trend + volatilità) usando barre daily e IV ATM della chain;
2. misura lo spread bid-ask medio ATM;
3. assegna un Opportunity Score (0-100);
4. scarta SIDEWAYS+LOW_IV, ticker illiquidi o già in portafoglio.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import numpy as np
from loguru import logger

from config.settings import Settings
from src.analysis.regime import MarketRegime, Trend, VolRegime, detect_regime
from src.data.market_data import MarketDataService, OptionChain
from src.strategy.selector import StrategyType, choose_strategy

# Pesi delle componenti dello score
W_VOL = 0.45
W_TREND = 0.35
W_LIQUIDITY = 0.20


@dataclass
class ScreeningRow:
    ticker: str
    trend: str | None = None
    volatility_regime: str | None = None
    iv_rank: float | None = None
    adx: float | None = None
    avg_spread_pct: float | None = None
    opportunity_score: float = 0.0
    selected: bool = False
    strategy: str | None = None
    reason: str = ""

    def to_record(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    regime: MarketRegime
    chain: OptionChain
    strategy: StrategyType
    score: float
    row: ScreeningRow


@dataclass
class ScreenerOutput:
    ranked: list[Candidate]  # candidati idonei ordinati per score decrescente
    rows: list[ScreeningRow]  # tutti i ticker (per Supabase / dashboard)


# ---------------------------------------------------------------------- #
# Score
# ---------------------------------------------------------------------- #
def compute_opportunity_score(
    trend: Trend,
    vol: VolRegime,
    iv_rank: float,
    adx_value: float,
    spread_pct: float,
    max_spread_pct: float,
    adx_threshold: float = 20.0,
) -> float:
    """
    Score 0-100 = 45% segnale di volatilità + 35% segnale di trend + 20% liquidità.

    - Volatilità: HIGH_IV (strategie a credito) premia IV Rank alto; LOW_IV (debito) premia IV Rank basso.
    - Trend: per strategie direzionali premia ADX alto (20 -> 0 punti, 50+ -> 100);
             per SIDEWAYS (Iron Condor) premia ADX basso (range più pulito).
    - Liquidità: spread ATM 0% -> 100 punti, spread = MAX_SPREAD_PCT -> 0 punti.
    """
    if trend == Trend.SIDEWAYS and vol == VolRegime.LOW_IV:
        return 0.0
    vol_signal = iv_rank if vol == VolRegime.HIGH_IV else 100.0 - iv_rank
    if trend == Trend.SIDEWAYS:
        trend_signal = (adx_threshold - adx_value) / adx_threshold * 100.0
    else:
        trend_signal = (adx_value - adx_threshold) / 30.0 * 100.0
    liquidity = (1.0 - spread_pct / max_spread_pct) * 100.0 if max_spread_pct > 0 else 0.0

    score = (
        W_VOL * float(np.clip(vol_signal, 0, 100))
        + W_TREND * float(np.clip(trend_signal, 0, 100))
        + W_LIQUIDITY * float(np.clip(liquidity, 0, 100))
    )
    return round(score, 2)


# ---------------------------------------------------------------------- #
# Screener
# ---------------------------------------------------------------------- #
class Screener:
    def __init__(self, settings: Settings, market: MarketDataService) -> None:
        self.s = settings
        self.market = market

    def run(self, occupied: set[str]) -> ScreenerOutput:
        s = self.s
        logger.info("=== SCREENER: {} ticker in watchlist ===", len(s.WATCHLIST))
        bars_by_ticker = self.market.get_daily_bars(s.WATCHLIST)
        target_dte = (s.MIN_DTE + s.MAX_DTE) / 2

        rows: list[ScreeningRow] = []
        eligible: list[Candidate] = []

        for i, ticker in enumerate(s.WATCHLIST):
            if i > 0:
                time.sleep(s.RATE_LIMIT_SLEEP_SEC)  # rispetto dei rate limit Alpaca
            row = ScreeningRow(ticker=ticker)
            rows.append(row)
            try:
                bars = bars_by_ticker.get(ticker)
                if bars is None or bars.empty:
                    row.reason = "Storico prezzi non disponibile"
                    continue
                spot = float(bars["close"].iloc[-1])
                chain = self.market.get_option_chain(ticker, spot)

                atm = chain.atm_contracts(target_dte)
                ivs = [c.iv for c in atm if c.iv]
                atm_iv = float(np.mean(ivs)) if ivs else None
                valid_spreads = [c.spread_pct for c in atm if c.bid > 0 and c.ask > 0]
                spread = float(np.mean(valid_spreads)) if valid_spreads else None

                regime = detect_regime(
                    ticker,
                    bars,
                    atm_iv,
                    ema_fast_period=s.EMA_FAST,
                    ema_slow_period=s.EMA_SLOW,
                    adx_period=s.ADX_PERIOD,
                    adx_threshold=s.ADX_TREND_THRESHOLD,
                    hv_window=s.HV_WINDOW,
                    iv_rank_threshold=s.IV_RANK_HIGH_THRESHOLD,
                )
                logger.debug(regime.describe())
                strategy = choose_strategy(regime.trend, regime.volatility, s.PREFER_SINGLE_LEG_DEBIT)

                row.trend = regime.trend.value
                row.volatility_regime = regime.volatility.value
                row.iv_rank = round(regime.iv_rank, 2)
                row.adx = round(regime.adx, 2)
                row.avg_spread_pct = round(spread, 4) if spread is not None else None
                row.strategy = strategy.value

                # --- Filtri di esclusione ---
                if atm_iv is None:
                    row.reason = "IV ATM non disponibile"
                    continue
                if spread is None or spread > s.MAX_SPREAD_PCT or not chain.liquid:
                    row.reason = "Illiquido (spread ATM troppo largo o nessun contratto valido)"
                    continue

                row.opportunity_score = compute_opportunity_score(
                    regime.trend, regime.volatility, regime.iv_rank, regime.adx, spread, s.MAX_SPREAD_PCT, s.ADX_TREND_THRESHOLD
                )
                if strategy == StrategyType.NO_TRADE:
                    row.reason = "SIDEWAYS + LOW_IV: nessun edge"
                    continue
                if ticker in occupied:
                    row.reason = "Posizione/ordine già aperto sul ticker"
                    continue
                if row.opportunity_score < s.MIN_OPPORTUNITY_SCORE:
                    row.reason = f"Score sotto soglia ({s.MIN_OPPORTUNITY_SCORE:.0f})"
                    continue

                row.reason = "Idoneo"
                eligible.append(Candidate(regime=regime, chain=chain, strategy=strategy, score=row.opportunity_score, row=row))
            except Exception as exc:  # noqa: BLE001 - un ticker problematico non deve fermare lo screening
                logger.warning("{}: errore durante lo screening ({})", ticker, exc)
                row.reason = f"Errore: {type(exc).__name__}"

        eligible.sort(key=lambda c: c.score, reverse=True)
        for cand in eligible[: s.MAX_NEW_TRADES_PER_RUN]:
            cand.row.selected = True
            cand.row.reason = "Selezionato (miglior score)"

        self._log_table(rows)
        return ScreenerOutput(ranked=eligible, rows=rows)

    @staticmethod
    def _log_table(rows: list[ScreeningRow]) -> None:
        header = f"{'Ticker':<7}| {'Trend':<9}| {'IV':<8}| {'IVR':>5} | {'ADX':>5} | {'Spread':>7} | {'Score':>6} | {'Sel':<3} | Strategia / Motivo"
        lines = [header, "-" * len(header)]
        for r in sorted(rows, key=lambda x: x.opportunity_score, reverse=True):
            spread = f"{r.avg_spread_pct:.1%}" if r.avg_spread_pct is not None else "n/d"
            ivr = f"{r.iv_rank:.0f}" if r.iv_rank is not None else "n/d"
            adx_txt = f"{r.adx:.1f}" if r.adx is not None else "n/d"
            lines.append(
                f"{r.ticker:<7}| {(r.trend or 'n/d'):<9}| {(r.volatility_regime or 'n/d'):<8}| {ivr:>5} | {adx_txt:>5} | "
                f"{spread:>7} | {r.opportunity_score:>6.1f} | {'SI' if r.selected else 'no':<3} | {r.strategy or '-'} / {r.reason}"
            )
        logger.info("Risultati screening:\n{}", "\n".join(lines))
