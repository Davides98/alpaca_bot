"""
Modulo 5b - Risk Management.

- Position sizing: qty = floor(rischio_massimo / max_loss_per_contratto).
- Gestione posizioni aperte (prima di cercare nuovi trade):
    1. Take Profit  (+50% del credito massimo / +40% del debito pagato)
    2. Stop Loss    (-100% del credito incassato / -50% del debito pagato)
    3. Protezione assegnazione: DTE <= CLOSE_DTE_THRESHOLD

Protezioni sull'esecuzione delle uscite:
- quote troppo larghe (naturale lontano dal mid) -> take profit / stop loss rimandati al ciclo dopo;
- stop loss solo se confermato per STOP_LOSS_CONFIRM_CHECKS controlli consecutivi;
- tetto di prezzo: mid + MAX_SLIPPAGE_PCT_OF_WIDTH; per il take profit mai oltre la soglia di profitto
  (un take profit non può trasformarsi in una perdita);
- dopo uno stop loss il ticker resta in pausa per STOP_LOSS_COOLDOWN_HOURS.

Le posizioni Alpaca sono per singolo contratto: le raggruppiamo per (sottostante, scadenza) e le
abbiniamo al trade OPEN salvato su Supabase (se disponibile) per recuperare strategia e prezzo di entrata.

Convenzione: `entry_net` = prezzo netto dell'ordine di apertura (+ debito / - credito).
`position_value` = valore attuale della posizione (long - short, a mid).
P&L per unità = position_value - entry_net.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import reduce
from typing import TYPE_CHECKING, Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, PositionIntent, PositionSide, QueryOrderStatus
from alpaca.trading.models import Position
from alpaca.trading.requests import GetOrdersRequest
from loguru import logger

from config.settings import Settings

from src.execution.executor import net_mid, net_natural, quotes_too_wide, slippage_base

if TYPE_CHECKING:  # evita import circolari a runtime
    from src.data.market_data import MarketDataService
    from src.execution.executor import ExecutionResult, OrderExecutor, OrderLegSpec
    from src.storage.supabase_client import SupabaseStorage

CONTRACT_MULTIPLIER = 100
MAX_WIDE_QUOTE_POSTPONES = 15  # stop loss rimandato al massimo per 15 controlli di fila (~15 min in sessione)


# ---------------------------------------------------------------------- #
# Funzioni pure
# ---------------------------------------------------------------------- #
def position_size(max_loss_per_contract: float, equity: float, risk_pct: float, max_contracts: int) -> int:
    """Numero di contratti tale che la perdita massima non superi risk_pct dell'equity."""
    if max_loss_per_contract <= 0 or equity <= 0:
        return 0
    return max(0, min(max_contracts, math.floor(equity * risk_pct / max_loss_per_contract)))


def evaluate_exit(entry_net: float, position_value: float, dte: int, settings: Settings) -> tuple[str | None, float]:
    """
    Ritorna (motivo_chiusura | None, pnl_per_unità).
    motivo: TAKE_PROFIT | STOP_LOSS | DTE_THRESHOLD
    """
    pnl = position_value - entry_net
    basis = abs(entry_net)
    if basis <= 0:
        return (("DTE_THRESHOLD" if dte <= settings.CLOSE_DTE_THRESHOLD else None), pnl)
    if entry_net < 0:  # strategia a credito
        tp, sl = settings.TAKE_PROFIT_CREDIT_PCT * basis, settings.STOP_LOSS_CREDIT_PCT * basis
    else:  # strategia a debito
        tp, sl = settings.TAKE_PROFIT_DEBIT_PCT * basis, settings.STOP_LOSS_DEBIT_PCT * basis
    if pnl >= tp:
        return "TAKE_PROFIT", pnl
    if pnl <= -sl:
        return "STOP_LOSS", pnl
    if dte <= settings.CLOSE_DTE_THRESHOLD:
        return "DTE_THRESHOLD", pnl
    return None, pnl


def _px(price: float) -> str:
    """Prezzo per azione in formato italiano, sempre positivo (es. 4,27)."""
    return f"{abs(price):.2f}".replace(".", ",")


def exit_price_cap(reason: str, entry_net: float, exit_mid: float, base: float, settings: Settings) -> float:
    """
    Netto massimo (con segno, + = pago) accettabile per l'ordine di chiusura.
      - tutte le uscite: mid + MAX_SLIPPAGE_PCT_OF_WIDTH x base (base = larghezza dello spread o premio);
      - TAKE_PROFIT: inoltre mai oltre il prezzo che realizza esattamente la soglia di profitto.
    Realizzato per unità = -(entry_net + exit_net)  ->  exit_net <= -entry_net - target.
    """
    cap = exit_mid + settings.MAX_SLIPPAGE_PCT_OF_WIDTH * abs(base)
    if reason == "TAKE_PROFIT":
        basis = abs(entry_net)
        pct = settings.TAKE_PROFIT_CREDIT_PCT if entry_net < 0 else settings.TAKE_PROFIT_DEBIT_PCT
        cap = min(cap, -entry_net - pct * basis)
    return cap


# ---------------------------------------------------------------------- #
# Modelli
# ---------------------------------------------------------------------- #
@dataclass
class GroupLeg:
    symbol: str
    is_long: bool
    ratio: int
    avg_entry: float
    current_price: float | None
    bid: float | None = None
    ask: float | None = None

    @property
    def mark(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.current_price


@dataclass
class PositionGroup:
    underlying: str
    expiration: date
    legs: list[GroupLeg]
    qty: int
    trade: dict[str, Any] | None = None  # riga Supabase abbinata

    @property
    def dte(self) -> int:
        return (self.expiration - datetime.now(timezone.utc).date()).days

    @property
    def symbols(self) -> set[str]:
        return {leg.symbol for leg in self.legs}

    @property
    def strategy(self) -> str:
        return (self.trade or {}).get("strategy") or "UNKNOWN"

    @property
    def width(self) -> float:
        """Larghezza dello spread (dal trade salvato, altrimenti dagli strike); 0 = single-leg."""
        from src.data.market_data import parse_occ_symbol

        saved = (self.trade or {}).get("legs_json", {}).get("width")
        if saved:
            return float(saved)
        by_type: dict[str, list[float]] = {}
        for leg in self.legs:
            _, _, opt_type, strike = parse_occ_symbol(leg.symbol)
            by_type.setdefault(str(opt_type), []).append(float(strike))
        widths = [max(v) - min(v) for v in by_type.values() if len(v) > 1]
        return max(widths) if widths else 0.0

    @property
    def entry_net(self) -> float:
        if self.trade and self.trade.get("entry_price") is not None:
            return float(self.trade["entry_price"])
        return sum((leg.avg_entry if leg.is_long else -leg.avg_entry) * leg.ratio for leg in self.legs)

    def position_value(self) -> float | None:
        total = 0.0
        for leg in self.legs:
            mark = leg.mark
            if mark is None:
                return None
            total += (mark if leg.is_long else -mark) * leg.ratio
        return total


@dataclass
class RiskReport:
    open_groups: list[PositionGroup] = field(default_factory=list)  # ancora aperte dopo la gestione
    closed: list[str] = field(default_factory=list)
    occupied_underlyings: set[str] = field(default_factory=set)  # posizioni + ordini pendenti
    cooldown_underlyings: set[str] = field(default_factory=set)  # in pausa dopo uno stop loss recente

    @property
    def open_count(self) -> int:
        return len(self.open_groups)


# ---------------------------------------------------------------------- #
# Risk manager
# ---------------------------------------------------------------------- #
class RiskManager:
    def __init__(
        self,
        settings: Settings,
        trading_client: TradingClient,
        market: "MarketDataService",
        executor: "OrderExecutor",
        storage: "SupabaseStorage",
    ) -> None:
        self.s = settings
        self.client = trading_client
        self.market = market
        self.executor = executor
        self.storage = storage
        # Controlli consecutivi in stop loss per gruppo (persistono tra i cicli della stessa sessione)
        self._sl_streak: dict[frozenset[str], int] = {}
        self._wide_streak: dict[frozenset[str], int] = {}  # controlli di fila rimandati per quote larghe
        self.sl_confirm_checks = settings.STOP_LOSS_CONFIRM_CHECKS if settings.SESSION_MODE else 1

    # ---------------- Caricamento posizioni ---------------- #
    def load_groups(self) -> list[PositionGroup]:
        from src.data.market_data import parse_occ_symbol

        positions: list[Position] = [p for p in self.client.get_all_positions() if p.asset_class == AssetClass.US_OPTION]
        buckets: dict[tuple[str, date], list[Position]] = {}
        for p in positions:
            root, expiration, _, _ = parse_occ_symbol(p.symbol)
            buckets.setdefault((root, expiration), []).append(p)

        open_trades = self.storage.get_open_trades()
        groups: list[PositionGroup] = []
        for (root, expiration), plist in buckets.items():
            qtys = [abs(int(float(p.qty))) for p in plist]
            unit = reduce(math.gcd, qtys) or 1
            legs = [
                GroupLeg(
                    symbol=p.symbol,
                    is_long=p.side == PositionSide.LONG,
                    ratio=abs(int(float(p.qty))) // unit,
                    avg_entry=float(p.avg_entry_price),
                    current_price=float(p.current_price) if p.current_price is not None else None,
                )
                for p in plist
            ]
            group = PositionGroup(underlying=root, expiration=expiration, legs=legs, qty=unit)
            group.trade = self._match_trade(group, open_trades)
            groups.append(group)

        self._reconcile_orphan_trades(open_trades, groups)
        return groups

    @staticmethod
    def _match_trade(group: PositionGroup, open_trades: list[dict[str, Any]]) -> dict[str, Any] | None:
        for trade in open_trades:
            legs = (trade.get("legs_json") or {}).get("legs", [])
            symbols = {leg.get("symbol") for leg in legs}
            if symbols and symbols == group.symbols:
                return trade
        return None

    def _reconcile_orphan_trades(self, open_trades: list[dict[str, Any]], groups: list[PositionGroup]) -> None:
        """Trade OPEN su Supabase senza posizione su Alpaca (scaduti, assegnati o chiusi a mano)."""
        matched_ids = {g.trade["id"] for g in groups if g.trade}
        for trade in open_trades:
            if trade["id"] in matched_ids:
                continue
            logger.warning(
                "Trade #{} {} {} OPEN su Supabase ma non presente su Alpaca: lo marco CLOSED (NOT_FOUND_IN_BROKER)",
                trade["id"], trade.get("ticker"), trade.get("strategy"),
            )
            if not self.s.DRY_RUN:
                self.storage.close_trade(trade["id"], exit_price=None, pnl=None, reason="NOT_FOUND_IN_BROKER")

    def pending_order_underlyings(self) -> set[str]:
        from src.data.market_data import parse_occ_symbol

        underlyings: set[str] = set()
        try:
            orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Impossibile leggere gli ordini aperti ({})", exc)
            return underlyings
        for order in orders:
            symbols = [leg.symbol for leg in (order.legs or []) if leg.symbol] or ([order.symbol] if order.symbol else [])
            for sym in symbols:
                try:
                    underlyings.add(parse_occ_symbol(sym)[0] if len(sym) > 15 else sym)
                except ValueError:
                    underlyings.add(sym)
        return underlyings

    # ---------------- Gestione ---------------- #
    def manage_open_positions(self) -> RiskReport:
        report = RiskReport()
        logger.info("=== RISK MANAGER: controllo posizioni aperte ===")
        groups = self.load_groups()
        if not groups:
            logger.info("Nessuna posizione in opzioni aperta")

        # quote aggiornate per tutte le gambe in un'unica chiamata
        all_symbols = sorted({leg.symbol for g in groups for leg in g.legs})
        quotes = self.market.get_option_quotes(all_symbols)
        for g in groups:
            for leg in g.legs:
                if leg.symbol in quotes:
                    leg.bid, leg.ask = quotes[leg.symbol]

        for g in groups:
            state, text, pnl_usd = self._manage_group(g, report)
            # Ultima decisione salvata sul trade: la dashboard mostra il motivo per cui non chiude
            if g.trade and not self.s.DRY_RUN and g in report.open_groups:
                fields: dict[str, Any] = {"dte": g.dte, "legs_json": self._with_exit_status(g.trade, state, text)}
                if pnl_usd is not None:
                    fields["unrealized_pnl"] = round(pnl_usd, 2)
                self.storage.update_trade(g.trade["id"], fields)

        report.occupied_underlyings = {g.underlying for g in report.open_groups} | self.pending_order_underlyings()
        report.cooldown_underlyings |= self.cooldown_underlyings()
        logger.info(
            "Posizioni aperte dopo la gestione: {} / max {} | ticker occupati: {}",
            report.open_count, self.s.MAX_OPEN_POSITIONS, sorted(report.occupied_underlyings) or "-",
        )
        return report

    @staticmethod
    def _with_exit_status(trade: dict[str, Any], state: str, text: str) -> dict[str, Any]:
        legs_json = dict(trade.get("legs_json") or {})
        legs_json["exit_status"] = {"state": state, "text": text, "at": datetime.now(timezone.utc).isoformat()}
        trade["legs_json"] = legs_json
        return legs_json

    def _manage_group(self, g: PositionGroup, report: RiskReport) -> tuple[str, str, float | None]:
        """Valuta e, se serve, chiude una posizione. Ritorna (stato, spiegazione, P&L non realizzato in $)."""
        value = g.position_value()
        label = f"{g.underlying} {g.strategy} {g.expiration}"
        if value is None:
            logger.warning("[{}] quote non disponibili, salto la valutazione", label)
            report.open_groups.append(g)
            return "NO_QUOTES", "Quote non disponibili in questo controllo: nessuna decisione, riprovo al prossimo.", None
        reason, pnl_unit = evaluate_exit(g.entry_net, value, g.dte, self.s)
        pnl_usd = pnl_unit * CONTRACT_MULTIPLIER * g.qty
        basis = abs(g.entry_net)
        pnl_pct = pnl_unit / basis if basis else 0.0
        logger.info(
            "[{}] x{} | entrata {:+.2f} | valore attuale {:+.2f} | P&L {:+.2f}/unità = ${:+,.0f} ({:+.0%} della base) | DTE {}",
            label, g.qty, g.entry_net, value, pnl_unit, pnl_usd, pnl_pct, g.dte,
        )

        credit = g.entry_net < 0
        specs = self.close_specs(g)
        width = g.width
        mid_close, nat_close = net_mid(specs), net_natural(specs)
        # Prezzo di chiusura detto "a parole": a credito si paga per chiudere, a debito si incassa
        market_txt = (f"chiudere ora costa {_px(nat_close)} (prezzo medio {_px(mid_close)})" if credit
                      else f"vendendo ora incassi {_px(-nat_close)} (prezzo medio {_px(-mid_close)})")
        label_it = {"TAKE_PROFIT": "Take profit", "STOP_LOSS": "Stop loss", "DTE_THRESHOLD": "Uscita per scadenza vicina"}

        key = frozenset(g.symbols)
        if reason != "STOP_LOSS":
            self._sl_streak.pop(key, None)
        if reason is None:
            logger.info("[{}] nessuna condizione di uscita: mantengo la posizione", label)
            report.open_groups.append(g)
            return "HOLD", self._hold_text(g, value), pnl_usd

        if reason in ("TAKE_PROFIT", "STOP_LOSS") and quotes_too_wide(specs, width, self.s.MAX_QUOTE_WIDTH_PCT_OF_WIDTH):
            waited = self._wide_streak.get(key, 0) + 1
            self._wide_streak[key] = waited
            # Il take profit può aspettare; lo stop loss non all'infinito: dopo MAX_WIDE_QUOTE_POSTPONES
            # controlli di fila chiude comunque (con il tetto di prezzo che limita la slippage).
            if reason == "TAKE_PROFIT" or waited < MAX_WIDE_QUOTE_POSTPONES:
                logger.warning(
                    "[{}] {} rilevato ma quote troppo larghe (mid {:+.2f}, naturale {:+.2f}): non mi fido del prezzo, "
                    "ricontrollo al prossimo ciclo ({}/{})", label, reason, mid_close, nat_close,
                    waited, MAX_WIDE_QUOTE_POSTPONES,
                )
                report.open_groups.append(g)
                extra = ("" if reason == "TAKE_PROFIT"
                         else f" Se resta così, chiude comunque dopo {MAX_WIDE_QUOTE_POSTPONES} controlli ({waited}/{MAX_WIDE_QUOTE_POSTPONES}).")
                return (f"{reason}_WAIT_WIDE",
                        f"{label_it[reason]} raggiunto al prezzo medio, ma le quote sono troppo larghe: {market_txt}. "
                        f"Il bot non si fida e ricontrolla tra un minuto.{extra}", pnl_usd)
        else:
            self._wide_streak.pop(key, None)
        if reason == "STOP_LOSS":
            streak = self._sl_streak.get(key, 0) + 1
            self._sl_streak[key] = streak
            if streak < self.sl_confirm_checks:
                logger.warning("[{}] STOP_LOSS da confermare ({}/{}): ricontrollo al prossimo ciclo",
                               label, streak, self.sl_confirm_checks)
                report.open_groups.append(g)
                return ("SL_CONFIRM",
                        f"Stop loss raggiunto, da confermare ({streak}/{self.sl_confirm_checks}): chiude solo se si ripete "
                        f"al prossimo controllo. Ora {market_txt}.", pnl_usd)

        cap = exit_price_cap(reason, g.entry_net, mid_close, slippage_base(specs, width), self.s)
        logger.warning("[{}] condizione di uscita {} -> chiudo la posizione (tetto prezzo {:+.2f})", label, reason, cap)
        result = self.close_group(g, label, cap)
        if result.filled:
            exit_net = result.fill_net_price if result.fill_net_price is not None else -value
            realized = -(g.entry_net + exit_net) * CONTRACT_MULTIPLIER * g.qty
            logger.success("[{}] CHIUSA ({}) P&L realizzato ${:+,.2f}", label, reason, realized)
            report.closed.append(label)
            self._sl_streak.pop(key, None)
            if reason == "STOP_LOSS":
                report.cooldown_underlyings.add(g.underlying)
            if g.trade:
                self.storage.close_trade(g.trade["id"], exit_price=round(exit_net, 4), pnl=round(realized, 2), reason=reason,
                                         legs_json=self._with_exit_fills(g.trade, result.leg_fills))
            return "CLOSED", f"{label_it[reason]}: chiusa.", None

        report.open_groups.append(g)
        if result.dry_run:
            report.closed.append(f"{label} (DRY_RUN)")
            return "DRY_RUN", f"{label_it[reason]} raggiunto: in DRY_RUN l'ordine di chiusura non viene inviato.", pnl_usd
        if result.status == "rejected":
            return ("CLOSE_REJECTED",
                    f"{label_it[reason]} raggiunto, ma Alpaca ha rifiutato l'ordine di chiusura: {result.message}", pnl_usd)
        tried = result.ladder or [cap]
        limit_txt = (f"pagando al massimo {_px(max(tried))}" if credit else f"incassando almeno {_px(-max(tried))}")
        why_cap = " È il prezzo che garantisce il take profit: sotto non vende." if reason == "TAKE_PROFIT" else ""
        return ("CLOSE_NOT_FILLED",
                f"{label_it[reason]} raggiunto: ordine di chiusura provato {limit_txt}, ma non eseguito ({market_txt}).{why_cap} "
                f"Riprova al prossimo controllo.", pnl_usd)

    def _hold_text(self, g: PositionGroup, value: float) -> str:
        """Spiegazione della posizione mantenuta: dove si trova rispetto alle soglie."""
        s, base = self.s, abs(g.entry_net)
        if g.entry_net < 0:  # credito: conta quanto costa ricomprarla
            cost = -value
            return (f"Nessuna soglia raggiunta: chiuderla costa {_px(cost)}. Take profit se scende a {_px(base * (1 - s.TAKE_PROFIT_CREDIT_PCT))}, "
                    f"stop loss se sale a {_px(base * (1 + s.STOP_LOSS_CREDIT_PCT))}, uscita a {s.CLOSE_DTE_THRESHOLD} giorni dalla scadenza (ora {g.dte}).")
        return (f"Nessuna soglia raggiunta: vale {_px(value)}. Take profit se sale a {_px(base * (1 + s.TAKE_PROFIT_DEBIT_PCT))}, "
                f"stop loss se scende a {_px(base * (1 - s.STOP_LOSS_DEBIT_PCT))}, uscita a {s.CLOSE_DTE_THRESHOLD} giorni dalla scadenza (ora {g.dte}).")

    @staticmethod
    def _with_exit_fills(trade: dict[str, Any], fills: dict[str, float]) -> dict[str, Any] | None:
        """legs_json del trade con il prezzo di chiusura di ogni gamba (None se non ci sono prezzi)."""
        if not fills:
            return None
        legs_json = dict(trade.get("legs_json") or {})
        legs_json["legs"] = [{**leg, "exit_fill": fills.get(leg.get("symbol"))} for leg in legs_json.get("legs", [])]
        return legs_json

    def cooldown_underlyings(self) -> set[str]:
        """Ticker chiusi in stop loss nelle ultime STOP_LOSS_COOLDOWN_HOURS ore (letti da Supabase)."""
        if self.s.STOP_LOSS_COOLDOWN_HOURS <= 0:
            return set()
        since = datetime.now(timezone.utc) - timedelta(hours=self.s.STOP_LOSS_COOLDOWN_HOURS)
        tickers = set(self.storage.get_recent_stop_tickers(since))
        if tickers:
            logger.info("Ticker in pausa dopo uno stop loss recente: {}", sorted(tickers))
        return tickers

    @staticmethod
    def close_specs(group: PositionGroup) -> list["OrderLegSpec"]:
        """Gambe dell'ordine di chiusura (vende le long, ricompra le short)."""
        from src.execution.executor import OrderLegSpec

        specs = []
        for leg in group.legs:
            mark = leg.mark or 0.0
            bid = leg.bid if leg.bid is not None else mark
            ask = leg.ask if leg.ask is not None else mark
            if leg.is_long:
                specs.append(OrderLegSpec(leg.symbol, OrderSide.SELL, PositionIntent.SELL_TO_CLOSE, leg.ratio, bid, ask))
            else:
                specs.append(OrderLegSpec(leg.symbol, OrderSide.BUY, PositionIntent.BUY_TO_CLOSE, leg.ratio, bid, ask))
        return specs

    def close_group(self, group: PositionGroup, label: str, max_net: float | None = None) -> "ExecutionResult":
        """
        Chiude il gruppo con un ordine MLEG senza superare `max_net`. Solo se Alpaca RIFIUTA l'MLEG
        (es. "uncovered short contracts") ripiega su ordini single-leg, ognuno con il proprio tetto.
        Se invece l'ordine semplicemente non viene eseguito entro il tetto, si riprova al ciclo dopo.
        """
        from src.execution.executor import ExecutionResult, OrderLegSpec

        specs = self.close_specs(group)
        result = self.executor.execute(specs, group.qty, f"CLOSE {label}", max_net=max_net)
        if result.filled or result.dry_run or len(specs) == 1 or result.filled_qty > 0 or result.status != "rejected":
            return result

        logger.warning("[{}] chiusura MLEG rifiutata: provo gamba per gamba (prima le short)", label)
        ordered = sorted(specs, key=lambda sp: sp.side != OrderSide.BUY)  # BUY_TO_CLOSE prima
        # Il margine di slippage concesso al gruppo viene diviso tra le gambe
        room = max(0.0, (max_net - net_mid(specs)) / len(specs)) if max_net is not None else None
        total_net, all_filled = 0.0, True
        leg_fills: dict[str, float] = {}
        for sp in ordered:
            single = OrderLegSpec(sp.symbol, sp.side, sp.intent, 1, sp.bid, sp.ask)
            leg_cap = single.signed_mid + room if room is not None else None
            r = self.executor.execute([single], group.qty * sp.ratio, f"CLOSE-LEG {sp.symbol}", max_net=leg_cap)
            if not r.filled or r.fill_net_price is None:
                all_filled = False
                logger.error("[{}] gamba {} non chiusa: intervento manuale consigliato", label, sp.symbol)
                break
            total_net += r.fill_net_price * sp.ratio
            leg_fills.update(r.leg_fills)
        return ExecutionResult(
            filled=all_filled,
            filled_qty=group.qty if all_filled else 0,
            fill_net_price=total_net if all_filled else None,
            status="filled" if all_filled else "partial_legs",
            message="Chiusura gamba per gamba",
            leg_fills=leg_fills,
        )
