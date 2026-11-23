"""
Modulo 4 - Strategy Selector & Contract Picker.

Matrice decisionale deterministica (Trend x Volatilità):

    | Trend     | HIGH_IV           | LOW_IV                          |
    |-----------|-------------------|---------------------------------|
    | SIDEWAYS  | Iron Condor       | NO_TRADE                        |
    | BULLISH   | Bull Put Spread   | Bull Call Spread (o Long Call)  |
    | BEARISH   | Bear Call Spread  | Bear Put Spread (o Long Put)    |

Convenzione prezzi (identica ad Alpaca per gli ordini MLEG):
    prezzo netto > 0  -> DEBITO (paghiamo)
    prezzo netto < 0  -> CREDITO (incassiamo)
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from loguru import logger
from pydantic import BaseModel

from config.settings import Settings
from src.analysis.regime import MarketRegime, Trend, VolRegime
from src.data.market_data import OptionChain, OptionContract
from src.execution.risk_manager import position_size

CONTRACT_MULTIPLIER = 100


class StrategyType(str, Enum):
    IRON_CONDOR = "IRON_CONDOR"
    BULL_PUT_SPREAD = "BULL_PUT_SPREAD"
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    NO_TRADE = "NO_TRADE"


CREDIT_STRATEGIES = {StrategyType.IRON_CONDOR, StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD}


def choose_strategy(trend: Trend, vol: VolRegime, prefer_single_leg_debit: bool = False) -> StrategyType:
    """Applica la matrice decisionale."""
    if trend == Trend.SIDEWAYS:
        return StrategyType.IRON_CONDOR if vol == VolRegime.HIGH_IV else StrategyType.NO_TRADE
    if trend == Trend.BULLISH:
        if vol == VolRegime.HIGH_IV:
            return StrategyType.BULL_PUT_SPREAD
        return StrategyType.LONG_CALL if prefer_single_leg_debit else StrategyType.BULL_CALL_SPREAD
    # BEARISH
    if vol == VolRegime.HIGH_IV:
        return StrategyType.BEAR_CALL_SPREAD
    return StrategyType.LONG_PUT if prefer_single_leg_debit else StrategyType.BEAR_PUT_SPREAD


# ---------------------------------------------------------------------- #
# Modelli
# ---------------------------------------------------------------------- #
class TradeLeg(BaseModel):
    symbol: str
    option_type: str  # call | put
    strike: float
    expiration: date
    side: str  # buy | sell
    ratio: int = 1
    bid: float
    ask: float
    delta: float | None = None

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2.0, 4)

    @property
    def signed_mid(self) -> float:
        """Contributo al prezzo netto: + se compriamo (paghiamo), - se vendiamo (incassiamo)."""
        return self.mid * self.ratio if self.side == "buy" else -self.mid * self.ratio

    @classmethod
    def from_contract(cls, c: OptionContract, side: str) -> "TradeLeg":
        return cls(
            symbol=c.symbol,
            option_type=c.option_type,
            strike=c.strike,
            expiration=c.expiration,
            side=side,
            bid=c.bid,
            ask=c.ask,
            delta=c.delta,
        )


class TradePlan(BaseModel):
    ticker: str
    strategy: StrategyType
    legs: list[TradeLeg]
    qty: int
    net_mid: float  # per unità, segno convenzione Alpaca (+ debito / - credito)
    width: float  # larghezza dello spread (0 per single-leg)
    max_profit_per_contract: float | None  # in $ (None = illimitato)
    max_loss_per_contract: float  # in $
    expiration: date
    dte: int
    rationale: str

    @property
    def is_credit(self) -> bool:
        return self.net_mid < 0

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1

    @property
    def total_max_loss(self) -> float:
        return self.max_loss_per_contract * self.qty

    def describe(self) -> str:
        legs_txt = " / ".join(
            f"{'+' if leg.side == 'buy' else '-'}{leg.ratio} {leg.option_type.upper()} {leg.strike:g} "
            f"(Δ {leg.delta:+.2f}, mid {leg.mid:.2f})" if leg.delta is not None else
            f"{'+' if leg.side == 'buy' else '-'}{leg.ratio} {leg.option_type.upper()} {leg.strike:g} (mid {leg.mid:.2f})"
            for leg in self.legs
        )
        kind = "CREDITO" if self.is_credit else "DEBITO"
        mp = f"${self.max_profit_per_contract:,.0f}" if self.max_profit_per_contract is not None else "illimitato"
        return (
            f"{self.ticker} {self.strategy.value} scad. {self.expiration} ({self.dte} DTE) x{self.qty} | {legs_txt} | "
            f"netto mid {abs(self.net_mid):.2f} {kind} | max profit/contr {mp} | max loss/contr "
            f"${self.max_loss_per_contract:,.0f} | rischio totale ${self.total_max_loss:,.0f}"
        )


# ---------------------------------------------------------------------- #
# Contract picker
# ---------------------------------------------------------------------- #
def pick_by_delta(
    contracts: list[OptionContract], option_type: str, expiration: date, target_abs_delta: float
) -> OptionContract | None:
    """Contratto con |delta| più vicino al target."""
    pool = [c for c in contracts if c.option_type == option_type and c.expiration == expiration and c.delta is not None]
    if not pool:
        return None
    return min(pool, key=lambda c: (abs(abs(c.delta) - target_abs_delta), c.strike))  # type: ignore[arg-type]


def pick_wing(
    contracts: list[OptionContract], option_type: str, expiration: date, anchor_strike: float, width: float, direction: int
) -> OptionContract | None:
    """
    Gamba "ala" a ~`width` dollari dallo strike di riferimento.
    direction = -1 -> strike inferiori (put), +1 -> strike superiori (call).
    """
    target = anchor_strike + direction * width
    pool = [
        c
        for c in contracts
        if c.option_type == option_type
        and c.expiration == expiration
        and (c.strike - anchor_strike) * direction > 0
        and abs(c.strike - anchor_strike) <= width * 2
    ]
    if not pool:
        return None
    return min(pool, key=lambda c: (abs(c.strike - target), abs(c.strike - anchor_strike)))


# ---------------------------------------------------------------------- #
# Selector
# ---------------------------------------------------------------------- #
class StrategySelector:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def build_plan(self, regime: MarketRegime, chain: OptionChain, equity: float) -> TradePlan | None:
        strategy = choose_strategy(regime.trend, regime.volatility, self.s.PREFER_SINGLE_LEG_DEBIT)
        logger.info("{}: regime {} + {} -> strategia {}", regime.ticker, regime.trend.value, regime.volatility.value, strategy.value)
        if strategy == StrategyType.NO_TRADE:
            return None

        target_dte = (self.s.MIN_DTE + self.s.MAX_DTE) / 2
        expiration = chain.best_expiration(target_dte, liquid_only=True)
        if expiration is None:
            logger.warning("{}: nessuna scadenza liquida disponibile", regime.ticker)
            return None
        pool = [c for c in chain.liquid if c.expiration == expiration]
        dte = pool[0].dte
        logger.info("{}: scadenza scelta {} ({} DTE, target {:.0f})", regime.ticker, expiration, dte, target_dte)

        legs = self._pick_legs(strategy, pool, expiration)
        if legs is None:
            return None
        return self._finalize(regime, strategy, legs, expiration, dte, equity)

    # --------------------------------------------------------------- #
    def _pick_legs(self, strategy: StrategyType, pool: list[OptionContract], exp: date) -> list[TradeLeg] | None:
        s = self.s
        w = s.WING_WIDTH

        def short_with_wing(opt_type: str, delta: float, direction: int) -> list[TradeLeg] | None:
            short = pick_by_delta(pool, opt_type, exp, delta)
            if short is None:
                logger.warning("Nessuna {} con delta disponibile per la gamba short", opt_type)
                return None
            wing = pick_wing(pool, opt_type, exp, short.strike, w, direction)
            if wing is None:
                logger.warning("Nessuna ala {} liquida a ~{}$ dallo strike {}", opt_type, w, short.strike)
                return None
            return [TradeLeg.from_contract(short, "sell"), TradeLeg.from_contract(wing, "buy")]

        def long_with_short(opt_type: str, delta: float, direction: int) -> list[TradeLeg] | None:
            long_leg = pick_by_delta(pool, opt_type, exp, delta)
            if long_leg is None:
                logger.warning("Nessuna {} con delta disponibile per la gamba long", opt_type)
                return None
            short = pick_wing(pool, opt_type, exp, long_leg.strike, w, direction)
            if short is None:
                logger.warning("Nessuna gamba short {} liquida a ~{}$ dallo strike {}", opt_type, w, long_leg.strike)
                return None
            return [TradeLeg.from_contract(long_leg, "buy"), TradeLeg.from_contract(short, "sell")]

        if strategy == StrategyType.IRON_CONDOR:
            put_side = short_with_wing("put", s.IRON_CONDOR_SHORT_DELTA, -1)
            call_side = short_with_wing("call", s.IRON_CONDOR_SHORT_DELTA, +1)
            if put_side is None or call_side is None:
                return None
            return put_side + call_side
        if strategy == StrategyType.BULL_PUT_SPREAD:
            return short_with_wing("put", s.CREDIT_SPREAD_SHORT_DELTA, -1)
        if strategy == StrategyType.BEAR_CALL_SPREAD:
            return short_with_wing("call", s.CREDIT_SPREAD_SHORT_DELTA, +1)
        if strategy == StrategyType.BULL_CALL_SPREAD:
            return long_with_short("call", s.DEBIT_LONG_DELTA, +1)
        if strategy == StrategyType.BEAR_PUT_SPREAD:
            return long_with_short("put", s.DEBIT_LONG_DELTA, -1)
        if strategy in (StrategyType.LONG_CALL, StrategyType.LONG_PUT):
            opt_type = "call" if strategy == StrategyType.LONG_CALL else "put"
            c = pick_by_delta(pool, opt_type, exp, s.DEBIT_LONG_DELTA)
            return [TradeLeg.from_contract(c, "buy")] if c else None
        return None

    # --------------------------------------------------------------- #
    def _finalize(
        self, regime: MarketRegime, strategy: StrategyType, legs: list[TradeLeg], exp: date, dte: int, equity: float
    ) -> TradePlan | None:
        s = self.s
        net_mid = round(sum(leg.signed_mid for leg in legs), 4)

        # Larghezza dello spread: per l'Iron Condor conta il lato più largo (solo uno può perdere a scadenza)
        if strategy == StrategyType.IRON_CONDOR:
            puts = sorted(leg.strike for leg in legs if leg.option_type == "put")
            calls = sorted(leg.strike for leg in legs if leg.option_type == "call")
            width = max(puts[1] - puts[0], calls[1] - calls[0])
        elif len(legs) == 2:
            width = abs(legs[0].strike - legs[1].strike)
        else:
            width = 0.0

        if strategy in CREDIT_STRATEGIES:
            credit = -net_mid
            if credit <= 0:
                logger.warning("{}: {} non genera credito (netto {:.2f}), scarto", regime.ticker, strategy.value, net_mid)
                return None
            if credit < s.MIN_CREDIT_PCT_OF_WIDTH * width:
                logger.warning(
                    "{}: credito {:.2f} < {:.0%} della larghezza {:.2f}, rapporto rischio/rendimento sfavorevole",
                    regime.ticker, credit, s.MIN_CREDIT_PCT_OF_WIDTH, width,
                )
                return None
            max_profit = credit * CONTRACT_MULTIPLIER
            max_loss = (width - credit) * CONTRACT_MULTIPLIER
        else:
            debit = net_mid
            if debit <= 0:
                logger.warning("{}: {} ha debito non valido ({:.2f}), scarto", regime.ticker, strategy.value, net_mid)
                return None
            if width > 0:
                if debit > s.MAX_DEBIT_PCT_OF_WIDTH * width:
                    logger.warning(
                        "{}: debito {:.2f} > {:.0%} della larghezza {:.2f}, rapporto rischio/rendimento sfavorevole, scarto",
                        regime.ticker, debit, s.MAX_DEBIT_PCT_OF_WIDTH, width,
                    )
                    return None
                max_profit = (width - debit) * CONTRACT_MULTIPLIER
            else:
                max_profit = None
            max_loss = debit * CONTRACT_MULTIPLIER

        if max_loss <= 0:
            logger.warning("{}: max loss non valido ({:.2f}), scarto", regime.ticker, max_loss)
            return None

        qty = position_size(max_loss, equity, s.MAX_RISK_PER_TRADE_PCT, s.MAX_CONTRACTS_PER_TRADE)
        budget = equity * s.MAX_RISK_PER_TRADE_PCT
        logger.info(
            "{}: sizing -> rischio max/contratto ${:,.0f}, budget {:.1%} equity = ${:,.0f} -> qty {}",
            regime.ticker, max_loss, s.MAX_RISK_PER_TRADE_PCT, budget, qty,
        )
        if qty < 1:
            logger.warning("{}: anche 1 contratto supera il rischio massimo consentito, scarto", regime.ticker)
            return None

        plan = TradePlan(
            ticker=regime.ticker,
            strategy=strategy,
            legs=legs,
            qty=qty,
            net_mid=net_mid,
            width=width,
            max_profit_per_contract=max_profit,
            max_loss_per_contract=max_loss,
            expiration=exp,
            dte=dte,
            rationale=f"{regime.trend.value}+{regime.volatility.value} (ADX {regime.adx:.1f}, IVR {regime.iv_rank:.0f})",
        )
        logger.success("TradePlan: {}", plan.describe())
        return plan
