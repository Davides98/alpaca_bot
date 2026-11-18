"""
Modulo 1 - Data Ingestion.

- Barre daily del sottostante (ultimi N giorni di trading) via `StockHistoricalDataClient`.
- Option chain filtrata per DTE e liquidità via `OptionHistoricalDataClient.get_option_chain`.
- Quote aggiornate di singoli contratti (per valutare le posizioni aperte).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.snapshots import OptionsSnapshot
from alpaca.data.requests import (
    OptionChainRequest,
    OptionSnapshotRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from config.settings import Settings

RISK_FREE_RATE = 0.04  # usato solo per il delta di fallback (Black-Scholes)


# ---------------------------------------------------------------------- #
# Modelli
# ---------------------------------------------------------------------- #
@dataclass(slots=True)
class OptionContract:
    symbol: str
    underlying: str
    expiration: date
    option_type: str  # "call" | "put"
    strike: float
    bid: float
    ask: float
    iv: float | None
    delta: float | None
    theta: float | None
    vega: float | None
    dte: int

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2.0, 4)

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")


@dataclass(slots=True)
class OptionChain:
    underlying: str
    spot: float
    contracts: list[OptionContract] = field(default_factory=list)  # tutti quelli con quote valide nel range DTE
    liquid: list[OptionContract] = field(default_factory=list)  # dopo il filtro liquidità

    def expirations(self, liquid_only: bool = True) -> list[date]:
        source = self.liquid if liquid_only else self.contracts
        return sorted({c.expiration for c in source})

    def best_expiration(self, target_dte: float, liquid_only: bool = True) -> date | None:
        """Scadenza con DTE più vicino al target (centro della finestra MIN/MAX DTE)."""
        source = self.liquid if liquid_only else self.contracts
        if not source:
            return None
        by_exp: dict[date, int] = {c.expiration: c.dte for c in source}
        return min(by_exp, key=lambda e: (abs(by_exp[e] - target_dte), e))

    def atm_contracts(self, target_dte: float) -> list[OptionContract]:
        """Call e put con strike più vicino allo spot, sulla scadenza migliore (chain completa)."""
        exp = self.best_expiration(target_dte, liquid_only=False)
        if exp is None:
            return []
        result: list[OptionContract] = []
        for opt_type in ("call", "put"):
            same = [c for c in self.contracts if c.expiration == exp and c.option_type == opt_type]
            if same:
                result.append(min(same, key=lambda c: abs(c.strike - self.spot)))
        return result


# ---------------------------------------------------------------------- #
# Utility
# ---------------------------------------------------------------------- #
def parse_occ_symbol(symbol: str) -> tuple[str, date, str, float]:
    """Decodifica un simbolo OCC, es. 'SPY261218P00560000' -> ('SPY', 2026-12-18, 'put', 560.0)."""
    root = symbol[:-15]
    expiration = datetime.strptime(symbol[-15:-9], "%y%m%d").date()
    option_type = "call" if symbol[-9] == "C" else "put"
    strike = int(symbol[-8:]) / 1000.0
    return root, expiration, option_type, strike


def bs_delta(spot: float, strike: float, dte: int, iv: float, option_type: str) -> float | None:
    """Delta Black-Scholes, usato solo se il feed non fornisce le greche."""
    if spot <= 0 or strike <= 0 or iv is None or iv <= 0 or dte <= 0:
        return None
    t = dte / 365.0
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return n_d1 if option_type == "call" else n_d1 - 1.0


def is_liquid(contract: OptionContract, max_spread_pct: float) -> bool:
    return contract.bid > 0 and contract.ask > 0 and contract.spread_pct <= max_spread_pct


# ---------------------------------------------------------------------- #
# Servizio dati
# ---------------------------------------------------------------------- #
class MarketDataService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = settings.ALPACA_API_KEY.get_secret_value()
        secret = settings.ALPACA_SECRET_KEY.get_secret_value()
        self.stock_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        self.option_client = OptionHistoricalDataClient(api_key=key, secret_key=secret)
        self.stock_feed = DataFeed(settings.STOCK_DATA_FEED.lower())
        self.options_feed = OptionsFeed(settings.OPTIONS_DATA_FEED.lower())

    # ---------------- Sottostante ---------------- #
    def get_daily_bars(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Barre daily (open/high/low/close/volume) per ogni simbolo, ultime HISTORY_TRADING_DAYS."""
        n_days = self.settings.HISTORY_TRADING_DAYS
        start = datetime.now(timezone.utc) - timedelta(days=int(n_days * 1.6) + 10)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            adjustment=Adjustment.ALL,
            feed=self.stock_feed,
        )
        logger.info("Download barre daily per {} ticker (feed={})", len(symbols), self.stock_feed.value)
        bars = self.stock_client.get_stock_bars(request)
        df = bars.df
        result: dict[str, pd.DataFrame] = {}
        if df.empty:
            logger.warning("Nessuna barra ricevuta da Alpaca")
            return result
        for symbol in symbols:
            if symbol not in df.index.get_level_values(0):
                logger.warning("{}: nessuna barra disponibile", symbol)
                continue
            sym_df = df.xs(symbol, level=0)[["open", "high", "low", "close", "volume"]].sort_index()
            result[symbol] = sym_df.tail(n_days)
            logger.debug("{}: {} barre, ultima chiusura {:.2f}", symbol, len(sym_df), sym_df["close"].iloc[-1])
        return result

    def get_latest_price(self, symbol: str) -> float | None:
        try:
            trade = self.stock_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.stock_feed)
            )
            return float(trade[symbol].price)
        except Exception as exc:  # noqa: BLE001
            logger.warning("{}: impossibile leggere l'ultimo prezzo ({})", symbol, exc)
            return None

    # ---------------- Opzioni ---------------- #
    def _snapshot_to_contract(self, symbol: str, snap: OptionsSnapshot, spot: float, today: date) -> OptionContract | None:
        try:
            root, expiration, option_type, strike = parse_occ_symbol(symbol)
        except (ValueError, IndexError):
            return None
        quote = snap.latest_quote
        if quote is None:
            return None
        bid = float(quote.bid_price or 0.0)
        ask = float(quote.ask_price or 0.0)
        dte = (expiration - today).days
        iv = float(snap.implied_volatility) if snap.implied_volatility else None
        greeks = snap.greeks
        delta = float(greeks.delta) if greeks and greeks.delta is not None else None
        if delta is None and iv:
            delta = bs_delta(spot, strike, dte, iv, option_type)
        return OptionContract(
            symbol=symbol,
            underlying=root,
            expiration=expiration,
            option_type=option_type,
            strike=strike,
            bid=bid,
            ask=ask,
            iv=iv,
            delta=delta,
            theta=float(greeks.theta) if greeks and greeks.theta is not None else None,
            vega=float(greeks.vega) if greeks and greeks.vega is not None else None,
            dte=dte,
        )

    def get_option_chain(self, underlying: str, spot: float, today: date | None = None) -> OptionChain:
        """Chain tra MIN_DTE e MAX_DTE, strike entro ±STRIKE_RANGE_PCT dallo spot, con filtro liquidità."""
        s = self.settings
        today = today or datetime.now(timezone.utc).date()
        request = OptionChainRequest(
            underlying_symbol=underlying,
            feed=self.options_feed,
            expiration_date_gte=today + timedelta(days=s.MIN_DTE),
            expiration_date_lte=today + timedelta(days=s.MAX_DTE),
            strike_price_gte=round(spot * (1 - s.STRIKE_RANGE_PCT), 2),
            strike_price_lte=round(spot * (1 + s.STRIKE_RANGE_PCT), 2),
        )
        snapshots = self.option_client.get_option_chain(request)
        chain = OptionChain(underlying=underlying, spot=spot)
        for symbol, snap in snapshots.items():
            contract = self._snapshot_to_contract(symbol, snap, spot, today)
            if contract is None or not (s.MIN_DTE <= contract.dte <= s.MAX_DTE):
                continue
            chain.contracts.append(contract)
            if is_liquid(contract, s.MAX_SPREAD_PCT):
                chain.liquid.append(contract)
        logger.info(
            "{}: option chain {} contratti nel range {}-{} DTE, {} liquidi (spread <= {:.0%}, bid > 0)",
            underlying,
            len(chain.contracts),
            s.MIN_DTE,
            s.MAX_DTE,
            len(chain.liquid),
            s.MAX_SPREAD_PCT,
        )
        return chain

    def get_option_quotes(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        """Bid/Ask correnti per una lista di contratti: {symbol: (bid, ask)}."""
        if not symbols:
            return {}
        try:
            snaps = self.option_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=symbols, feed=self.options_feed)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile leggere le quote delle opzioni ({})", exc)
            return {}
        quotes: dict[str, tuple[float, float]] = {}
        for symbol, snap in snaps.items():
            if snap.latest_quote is not None:
                quotes[symbol] = (float(snap.latest_quote.bid_price or 0.0), float(snap.latest_quote.ask_price or 0.0))
        return quotes
