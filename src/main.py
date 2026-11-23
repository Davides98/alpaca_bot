"""
Orchestratore principale: un singolo run completo (pensato per GitHub Actions).

Flusso:
  1. Config + logging (con mascheramento dei segreti)
  2. Controllo mercato aperto (get_clock) -> se chiuso, exit 0
  3. Snapshot equity su Supabase
  4. Risk manager: TP / SL / chiusura pre-scadenza delle posizioni aperte
  5. Screener della watchlist -> candidati
  6. Strategy selector -> TradePlan -> esecuzione Limit con price chasing
  7. Salvataggio log di screening

Esecuzione:  python -m src.main
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, PositionIntent
from loguru import logger
from pydantic import ValidationError

from config.settings import PAPER_TRADING, Settings, get_settings
from src.analysis.screener import Screener
from src.data.market_data import MarketDataService
from src.execution.executor import OrderExecutor, OrderLegSpec, net_mid, net_natural, price_cap, quotes_too_wide
from src.execution.risk_manager import RiskManager
from src.storage.supabase_client import SupabaseStorage
from src.strategy.selector import StrategySelector, TradePlan


# ---------------------------------------------------------------------- #
# Logging sicuro
# ---------------------------------------------------------------------- #
def setup_logging(settings: Settings) -> None:
    secrets = settings.secret_values()
    try:  # console Windows: evita UnicodeEncodeError su caratteri come "Δ"
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    def redacting_sink(message: Any) -> None:
        text = str(message)
        for secret in secrets:
            text = text.replace(secret, "***REDACTED***")
        sys.stderr.write(text)

    logger.remove()
    logger.add(
        redacting_sink,
        level=settings.LOG_LEVEL.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
        colorize=sys.stderr.isatty(),
        backtrace=False,
        diagnose=False,  # NON stampare i valori delle variabili locali nei traceback (potrebbero contenere chiavi)
    )


# ---------------------------------------------------------------------- #
# Helper
# ---------------------------------------------------------------------- #
def plan_to_order_legs(plan: TradePlan) -> list[OrderLegSpec]:
    legs: list[OrderLegSpec] = []
    for leg in plan.legs:
        if leg.side == "buy":
            legs.append(OrderLegSpec(leg.symbol, OrderSide.BUY, PositionIntent.BUY_TO_OPEN, leg.ratio, leg.bid, leg.ask))
        else:
            legs.append(OrderLegSpec(leg.symbol, OrderSide.SELL, PositionIntent.SELL_TO_OPEN, leg.ratio, leg.bid, leg.ask))
    return legs


def plan_to_trade_row(plan: TradePlan, entry_price: float, qty: int, order_id: str | None) -> dict[str, Any]:
    return {
        "ticker": plan.ticker,
        "strategy": plan.strategy.value,
        "entry_price": round(entry_price, 4),
        "qty": qty,
        "dte": plan.dte,
        "alpaca_order_id": order_id,
        "legs_json": {
            "expiration": plan.expiration.isoformat(),
            "width": plan.width,
            "net_mid_at_signal": plan.net_mid,
            "max_profit_per_contract": plan.max_profit_per_contract,
            "max_loss_per_contract": plan.max_loss_per_contract,
            "rationale": plan.rationale,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "type": leg.option_type,
                    "strike": leg.strike,
                    "side": leg.side,
                    "ratio": leg.ratio,
                    "delta": leg.delta,
                    "bid": leg.bid,
                    "ask": leg.ask,
                }
                for leg in plan.legs
            ],
        },
    }


# ---------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------- #
@dataclass
class RunContext:
    """Esito del run: viene sempre registrato su Supabase (tabella bot_runs) e su GitHub Actions."""

    run_id: str
    storage: SupabaseStorage
    status: str = "STARTED"  # OK | MARKET_CLOSED | CONFIG_ERROR | AUTH_ERROR | ERROR
    message: str = ""
    bot_status: BotStatus | None = None
    last_clock: Any = None  # ultimo clock Alpaca letto (per gli orari di mercato in dashboard)

    def finish(self, status: str, message: str) -> None:
        self.status, self.message = status, message


def gh_annotation(level: str, title: str, message: str) -> None:
    """Annotazione visibile nella pagina del run su GitHub Actions (::notice / ::warning / ::error)."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        clean = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level} title={title}::{clean}", flush=True)


def write_step_summary(ctx: RunContext, settings: Settings) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icon = {"OK": "✅", "MARKET_CLOSED": "🌙"}.get(ctx.status, "❌")
    db = "attivo" if ctx.storage.enabled and not ctx.storage.error_count else (
        f"{ctx.storage.error_count} scritture fallite" if ctx.storage.enabled else "NON attivo: nessun dato salvato"
    )
    lines = [
        f"### {icon} Options Bot - {ctx.status}",
        "",
        "| | |",
        "|---|---|",
        f"| Run | `{ctx.run_id}` |",
        f"| Modalità | {'DRY_RUN' if settings.DRY_RUN else 'PAPER TRADING'} |",
        f"| Esito | {ctx.message} |",
        f"| Supabase | {db} |",
        "",
    ]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


@dataclass
class Services:
    trading: TradingClient
    market: MarketDataService
    executor: OrderExecutor
    risk: RiskManager
    selector: StrategySelector
    storage: SupabaseStorage


@dataclass
class SessionStats:
    trading_cycles: int = 0
    refresh_cycles: int = 0
    orders_sent: int = 0
    orders_filled: int = 0
    positions_closed: int = 0
    errors: int = 0


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class BotStatus:
    """Stato attuale del bot (tabella bot_status, una riga): alimenta i contatori della dashboard."""

    storage: SupabaseStorage
    settings: Settings
    run_id: str
    session_started_at: datetime | None = None
    session_deadline: datetime | None = None
    last_trade_cycle_at: datetime | None = None
    next_trade_cycle_at: datetime | None = None
    trading_cycles: int = 0

    def row(self, state: str, clock: Any = None, message: str = "") -> dict[str, Any]:
        s = self.settings
        row: dict[str, Any] = {
            "state": state,
            "message": message[:300],
            "run_id": self.run_id,
            "trigger": os.getenv("BOT_TRIGGER") or os.getenv("GITHUB_EVENT_NAME", "local"),
            "dry_run": s.DRY_RUN,
            "session_mode": s.SESSION_MODE,
            "refresh_interval_sec": s.REFRESH_INTERVAL_SEC,
            "trade_interval_min": s.TRADE_INTERVAL_MIN,
            "session_max_minutes": s.SESSION_MAX_MINUTES,
            "session_started_at": _iso(self.session_started_at),
            "session_deadline": _iso(self.session_deadline),
            "last_trade_cycle_at": _iso(self.last_trade_cycle_at),
            "next_trade_cycle_at": _iso(self.next_trade_cycle_at),
            "trading_cycles": self.trading_cycles,
        }
        if clock is not None:  # orari ufficiali di Alpaca (festività e chiusure anticipate incluse)
            row.update(
                market_is_open=bool(clock.is_open),
                market_next_open=_iso(clock.next_open),
                market_next_close=_iso(clock.next_close),
                clock_at=_iso(clock.timestamp),
            )
        return row

    def publish(self, state: str, clock: Any = None, message: str = "") -> None:
        self.storage.update_bot_status(self.row(state, clock, message))


def account_snapshot(svc: Services, verbose: bool = False) -> float:
    """Legge l'account Alpaca, salva lo snapshot dell'equity e ritorna l'equity."""
    account = svc.trading.get_account()
    equity = float(account.equity or 0)
    cash = float(account.cash or 0)
    last_equity = float(account.last_equity or equity)
    if verbose:
        logger.info("Account {} | options level {} | buying power opzioni ${:,.2f}",
                    account.status.value if account.status else "?",
                    getattr(account, "options_trading_level", "?"),
                    float(getattr(account, "options_buying_power", 0) or 0))
    svc.storage.save_equity_snapshot(equity, cash, last_equity)
    return equity


NEW_YORK = ZoneInfo("America/New_York")


@dataclass
class TradingWindow:
    exits_allowed: bool  # take profit / stop loss / DTE
    new_trades_allowed: bool
    reason: str = ""
    resume_at: datetime | None = None  # quando finisce la pausa (UTC)


def trading_window(now: datetime, next_close: datetime | None, settings: Settings) -> TradingWindow:
    """
    Finestre in cui il bot NON opera, anche a mercato aperto:
      - primi NO_TRADE_AFTER_OPEN_MIN minuti dopo l'apertura (9:30 New York): quote delle opzioni
        larghissime e inaffidabili -> niente nuovi trade e niente take profit / stop loss;
      - ultimi NO_NEW_TRADES_BEFORE_CLOSE_MIN minuti prima della chiusura -> niente nuovi trade.
    """
    ny = now.astimezone(NEW_YORK)
    opening = ny.replace(hour=9, minute=30, second=0, microsecond=0)
    resume = opening + timedelta(minutes=settings.NO_TRADE_AFTER_OPEN_MIN)
    if opening <= ny < resume:
        return TradingWindow(
            False, False,
            f"primi {settings.NO_TRADE_AFTER_OPEN_MIN} min dopo l'apertura (quote inaffidabili): "
            f"si opera dalle {resume:%H:%M} di New York",
            resume.astimezone(timezone.utc),
        )
    if next_close is not None:
        to_close_min = (next_close - now).total_seconds() / 60
        if to_close_min <= settings.NO_NEW_TRADES_BEFORE_CLOSE_MIN:
            return TradingWindow(
                True, False, f"ultimi {settings.NO_NEW_TRADES_BEFORE_CLOSE_MIN} min prima della chiusura: niente nuovi trade"
            )
    return TradingWindow(True, True)


def refresh_cycle(svc: Services, stats: SessionStats) -> None:
    """Ciclo leggero (ogni REFRESH_INTERVAL_SEC): P&L posizioni + controllo TP/SL/DTE."""
    report = svc.risk.manage_open_positions()
    stats.positions_closed += len(report.closed)
    stats.refresh_cycles += 1


def trading_cycle(
    settings: Settings, svc: Services, run_id: str, equity: float, stats: SessionStats,
    allow_new_trades: bool = True, pause_reason: str = "",
) -> str:
    """Ciclo completo: gestione posizioni + screening + nuovi trade. Ritorna il riepilogo testuale."""
    storage, market, executor, selector = svc.storage, svc.market, svc.executor, svc.selector

    # Gestione posizioni aperte
    report = svc.risk.manage_open_positions()
    stats.positions_closed += len(report.closed)

    # Screening
    screener = Screener(settings, market)
    output = screener.run(occupied=report.occupied_underlyings | report.cooldown_underlyings)
    slots = min(settings.MAX_NEW_TRADES_PER_RUN, settings.MAX_OPEN_POSITIONS - report.open_count)

    # Lo screener pre-seleziona i migliori: qui la selezione diventa definitiva
    for row in output.rows:
        if row.selected:
            row.selected = False
            row.reason = "Idoneo"
        if row.ticker in report.cooldown_underlyings and row.ticker not in report.occupied_underlyings:
            row.reason = f"In pausa dopo uno stop loss (per {settings.STOP_LOSS_COOLDOWN_HOURS} ore)"

    # Nuovi trade
    opened = 0  # ordini tentati
    filled = 0  # ordini effettivamente eseguiti
    if not allow_new_trades:
        logger.info("Nessun nuovo trade in questo ciclo: {}", pause_reason)
        for cand in output.ranked:
            cand.row.reason = f"Idoneo, ma {pause_reason}"
    elif slots <= 0:
        logger.info("Nessuno slot libero ({} posizioni aperte, max {}): non apro nuovi trade",
                    report.open_count, settings.MAX_OPEN_POSITIONS)
        for cand in output.ranked:
            cand.row.reason = "Idoneo ma limite posizioni raggiunto"
    elif not output.ranked:
        logger.info("Nessun candidato idoneo in questo ciclo: il bot resta fermo")
    else:
        logger.info("=== STRATEGY SELECTOR: {} candidati idonei, {} slot disponibili ===", len(output.ranked), slots)
        for cand in output.ranked:
            if opened >= slots:
                cand.row.reason = "Idoneo, non selezionato (slot esauriti)"
                continue
            plan = selector.build_plan(cand.regime, cand.chain, equity)
            if plan is None:
                cand.row.reason = "Nessuna combinazione di contratti valida"
                continue
            cand.row.selected = True
            cand.row.reason = f"Selezionato: {plan.strategy.value} x{plan.qty}"

            # Le quote dello screening possono avere qualche minuto: le aggiorno subito prima dell'ordine
            order_legs = plan_to_order_legs(plan)
            fresh = market.get_option_quotes([leg.symbol for leg in order_legs])
            for leg in order_legs:
                bid, ask = fresh.get(leg.symbol, (0.0, 0.0))
                if bid > 0 and ask >= bid:
                    if (bid, ask) != (leg.bid, leg.ask):
                        logger.info("Quote aggiornate {}: {:.2f}/{:.2f} -> {:.2f}/{:.2f}", leg.symbol, leg.bid, leg.ask, bid, ask)
                    leg.bid, leg.ask = bid, ask

            # Quote troppo larghe: attraversare il mercato costerebbe troppo, si riprova al prossimo ciclo
            if quotes_too_wide(order_legs, plan.width, settings.MAX_QUOTE_WIDTH_PCT_OF_WIDTH):
                msg = (f"quote troppo larghe (mid {net_mid(order_legs):+.2f}, naturale {net_natural(order_legs):+.2f}, "
                       f"max {settings.MAX_QUOTE_WIDTH_PCT_OF_WIDTH:.0%} della larghezza)")
                logger.warning("[OPEN {}] Non apro: {}", plan.ticker, msg)
                cand.row.reason += f" - NON inviato: {msg}"
                continue
            # Tetto di prezzo: mid + slippage massima; per i debit spread mai oltre MAX_DEBIT_PCT_OF_WIDTH
            cap = price_cap(order_legs, plan.width, settings.MAX_SLIPPAGE_PCT_OF_WIDTH)
            if not plan.is_credit and plan.width > 0:
                cap = min(cap, settings.MAX_DEBIT_PCT_OF_WIDTH * plan.width)
            result = executor.execute(order_legs, plan.qty, f"OPEN {plan.ticker} {plan.strategy.value}", max_net=cap)
            opened += 1
            if result.dry_run:
                cand.row.reason += f" (DRY_RUN, non inviato: {result.message})"
            elif result.filled_qty > 0 and result.fill_net_price is not None:
                filled += 1
                storage.insert_trade(plan_to_trade_row(plan, result.fill_net_price, result.filled_qty, result.order_id))
                cand.row.reason += f" - ESEGUITO {result.filled_qty} @ {result.fill_net_price:+.2f}"
            else:
                cand.row.reason += f" - NON eseguito: {result.message}"

    storage.save_screening_logs([r.to_record() for r in output.rows], run_id)
    stats.orders_sent += opened
    stats.orders_filled += filled
    stats.trading_cycles += 1

    return (
        f"Equity ${equity:,.0f} | {len(output.rows)} ticker analizzati, {len(output.ranked)} idonei | "
        f"posizioni chiuse {len(report.closed)} | "
        + (f"ordini simulati {opened}" if settings.DRY_RUN else f"ordini inviati {opened}, eseguiti {filled}")
        + f" | posizioni già aperte {report.open_count}"
    )


def wait_for_open(settings: Settings, trading: TradingClient, ctx: RunContext | None = None) -> bool:
    """Modalità sessione: se l'apertura è vicina (<= SESSION_WAIT_FOR_OPEN_MIN) la aspetta. True se aperto."""
    clock = trading.get_clock()
    if ctx is not None:
        ctx.last_clock = clock
    if clock.is_open:
        return True
    wait_s = (clock.next_open - clock.timestamp).total_seconds()
    if 0 < wait_s <= settings.SESSION_WAIT_FOR_OPEN_MIN * 60:
        logger.info("Mercato chiuso, apre tra {:.0f} minuti: attendo l'apertura", wait_s / 60)
        # Attesa a blocchi di 60 s con un "battito" su bot_status: lo scheduler vede che il bot è vivo
        end = time.monotonic() + wait_s + 15
        while (left := end - time.monotonic()) > 0:
            if ctx is not None and ctx.bot_status is not None:
                ctx.bot_status.publish("WAITING_OPEN", clock, f"In attesa dell'apertura ({left / 60:.0f} min)")
            time.sleep(min(60.0, left))
        clock = trading.get_clock()
        if ctx is not None:
            ctx.last_clock = clock
        return bool(clock.is_open)
    return False


def run_session(settings: Settings, ctx: RunContext, svc: Services) -> str:
    """
    Modalità sessione: un solo job resta attivo per tutta la sessione di mercato.
      - ogni REFRESH_INTERVAL_SEC: snapshot equity + P&L posizioni + controllo TP/SL/DTE
      - ogni TRADE_INTERVAL_MIN:  ciclo completo (screening + nuovi trade)
    Termina a mercato chiuso o dopo SESSION_MAX_MINUTES (limite di durata dei job GitHub: 6 ore).
    """
    stats = SessionStats()
    deadline = time.monotonic() + settings.SESSION_MAX_MINUTES * 60
    status = ctx.bot_status or BotStatus(ctx.storage, settings, ctx.run_id)
    started = datetime.now(timezone.utc)
    status.session_started_at = started
    status.session_deadline = started + timedelta(minutes=settings.SESSION_MAX_MINUTES)
    next_trading = 0.0
    consecutive_errors = 0
    end_reason = ""
    pause_logged = False
    logger.info(
        "Modalità SESSIONE: aggiornamento ogni {}s, trading ogni {} min, durata massima {} min",
        settings.REFRESH_INTERVAL_SEC, settings.TRADE_INTERVAL_MIN, settings.SESSION_MAX_MINUTES,
    )

    while True:
        tick = time.monotonic()
        if tick >= deadline:
            end_reason = "durata massima raggiunta, il prossimo run schedulato riprende"
            break
        try:
            clock = svc.trading.get_clock()
            ctx.last_clock = clock
            is_open = clock.is_open
            test_mode = settings.DRY_RUN and settings.IGNORE_MARKET_HOURS and stats.trading_cycles == 0
            if not is_open and not test_mode:
                end_reason = "mercato chiuso"
                break
            equity = account_snapshot(svc, verbose=stats.trading_cycles == 0)
            window = TradingWindow(True, True) if test_mode else trading_window(
                datetime.now(timezone.utc), clock.next_close, settings)
            if not window.exits_allowed:
                # Pausa dopo l'apertura: solo snapshot dell'equity, nessuna decisione sulle posizioni
                if not pause_logged:
                    logger.info("Pausa operativa: {}", window.reason)
                    pause_logged = True
                status.next_trade_cycle_at = window.resume_at
                status.publish("RUNNING", clock, f"Pausa: {window.reason}")
            elif tick >= next_trading:
                logger.info("----- Ciclo di trading #{} -----", stats.trading_cycles + 1)
                summary = trading_cycle(settings, svc, ctx.run_id, equity, stats,
                                        window.new_trades_allowed, window.reason)
                logger.info("Ciclo di trading #{}: {}", stats.trading_cycles, summary)
                svc.storage.log_run(ctx.run_id, "OK", f"[ciclo {stats.trading_cycles}] {summary}", settings.DRY_RUN)
                next_trading = tick + settings.TRADE_INTERVAL_MIN * 60
                status.trading_cycles = stats.trading_cycles
                status.last_trade_cycle_at = datetime.now(timezone.utc)
                status.next_trade_cycle_at = status.last_trade_cycle_at + timedelta(
                    seconds=max(0.0, next_trading - time.monotonic()))
                if test_mode:
                    end_reason = "test a mercato chiuso (DRY_RUN + IGNORE_MARKET_HOURS), un solo ciclo"
                    break
            else:
                refresh_cycle(svc, stats)
            if window.exits_allowed:
                status.publish("RUNNING", clock, f"Sessione in corso: {stats.trading_cycles} cicli di trading")
            consecutive_errors = 0
        except APIError as exc:
            if getattr(exc, "status_code", None) in (401, 403):
                raise
            consecutive_errors += 1
            stats.errors += 1
            logger.error("Errore API Alpaca nel ciclo ({}/5 consecutivi): {}", consecutive_errors, str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - un ciclo fallito non deve fermare la sessione
            consecutive_errors += 1
            stats.errors += 1
            logger.exception("Errore nel ciclo ({}/5 consecutivi): {}: {}", consecutive_errors, type(exc).__name__, exc)
        if consecutive_errors >= 5:
            raise RuntimeError("5 errori consecutivi: sessione interrotta")

        time.sleep(max(1.0, settings.REFRESH_INTERVAL_SEC - (time.monotonic() - tick)))

    trades_txt = (f"ordini simulati {stats.orders_sent}" if settings.DRY_RUN
                  else f"ordini inviati {stats.orders_sent}, eseguiti {stats.orders_filled}")
    return (
        f"Sessione terminata ({end_reason}): {stats.trading_cycles} cicli di trading, "
        f"{stats.refresh_cycles} aggiornamenti | {trades_txt} | posizioni chiuse {stats.positions_closed} | errori {stats.errors}"
    )


def run(settings: Settings, ctx: RunContext) -> int:
    run_id = ctx.run_id
    storage = ctx.storage
    mode = "DRY_RUN (nessun ordine verrà inviato)" if settings.DRY_RUN else "PAPER TRADING (ordini reali sull'account paper)"
    logger.info("=" * 70)
    logger.info("Alpaca Options Bot | run {} | modalità: {} | {}", run_id, mode,
                "SESSIONE continua" if settings.SESSION_MODE else "run singolo")
    logger.info("=" * 70)

    if not PAPER_TRADING:  # salvaguardia: il bot non deve MAI operare su un conto reale
        logger.critical("PAPER_TRADING disattivato: esecuzione bloccata")
        ctx.finish("CONFIG_ERROR", "PAPER_TRADING disattivato")
        return 1
    if not settings.alpaca_configured:
        logger.error("ALPACA_API_KEY / ALPACA_SECRET_KEY mancanti: configura .env o i GitHub Secrets")
        ctx.finish("CONFIG_ERROR", "Chiavi Alpaca mancanti")
        return 1

    trading = TradingClient(
        api_key=settings.ALPACA_API_KEY.get_secret_value(),
        secret_key=settings.ALPACA_SECRET_KEY.get_secret_value(),
        paper=True,  # forzato: mai live
    )

    # Mercato aperto? (in modalità sessione aspetta l'apertura se manca poco)
    if settings.SESSION_MODE:
        is_open = wait_for_open(settings, trading, ctx)
    else:
        ctx.last_clock = trading.get_clock()
        is_open = ctx.last_clock.is_open
    if not is_open:
        if settings.DRY_RUN and settings.IGNORE_MARKET_HOURS:
            logger.warning("Mercato CHIUSO, ma DRY_RUN + IGNORE_MARKET_HOURS: proseguo a scopo di test (quote potenzialmente stantie)")
        else:
            clock = trading.get_clock()
            ctx.last_clock = clock
            next_open = clock.next_open.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info("Mercato USA chiuso (prossima apertura: {}). Nessuna azione, esco.", next_open)
            ctx.finish("MARKET_CLOSED", f"Mercato chiuso, prossima apertura {next_open}. Nessuna operazione.")
            return 0
    else:
        logger.info("Mercato aperto")

    market = MarketDataService(settings)
    executor = OrderExecutor(trading, settings)
    svc = Services(
        trading=trading,
        market=market,
        executor=executor,
        risk=RiskManager(settings, trading, market, executor, storage),
        selector=StrategySelector(settings),
        storage=storage,
    )

    if settings.SESSION_MODE:
        summary = run_session(settings, ctx, svc)
    else:
        equity = account_snapshot(svc, verbose=True)
        test_mode = settings.DRY_RUN and settings.IGNORE_MARKET_HOURS
        clock = ctx.last_clock
        window = TradingWindow(True, True) if test_mode or clock is None else trading_window(
            datetime.now(timezone.utc), clock.next_close, settings)
        if not window.exits_allowed:
            logger.info("Pausa operativa: {}. Nessuna operazione in questo run.", window.reason)
            ctx.finish("OK", f"Pausa operativa: {window.reason}")
            return 0
        summary = trading_cycle(settings, svc, run_id, equity, SessionStats(), window.new_trades_allowed, window.reason)
    logger.info("=" * 70)
    logger.info("Fine run {} | {}", run_id, summary)
    logger.info("=" * 70)
    ctx.finish("OK", summary)
    return 0


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Errore di configurazione (es. Secret/Variable con valore non valido): messaggio chiaro, senza valori segreti
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False))
        print(f"Configurazione non valida -> {problems}", file=sys.stderr)
        gh_annotation("error", "Configurazione non valida", problems)
        return 1
    setup_logging(settings)
    logger.info(
        "Parametri: MAX_NEW_TRADES_PER_RUN={} | MAX_OPEN_POSITIONS={} | MAX_RISK_PER_TRADE_PCT={:.2%} | "
        "MIN_OPPORTUNITY_SCORE={:g} | PREFER_SINGLE_LEG_DEBIT={} | CHASE_TO_NATURAL={} | WATCHLIST={}",
        settings.MAX_NEW_TRADES_PER_RUN, settings.MAX_OPEN_POSITIONS, settings.MAX_RISK_PER_TRADE_PCT,
        settings.MIN_OPPORTUNITY_SCORE, settings.PREFER_SINGLE_LEG_DEBIT, settings.CHASE_TO_NATURAL,
        ",".join(settings.WATCHLIST),
    )
    logger.info(
        "Uscite per posizione: credito TP +{:.0%} / SL -{:.0%} del credito | debito TP +{:.0%} / SL -{:.0%} del debito | "
        "chiusura a DTE <= {}",
        settings.TAKE_PROFIT_CREDIT_PCT, settings.STOP_LOSS_CREDIT_PCT, settings.TAKE_PROFIT_DEBIT_PCT,
        settings.STOP_LOSS_DEBIT_PCT, settings.CLOSE_DTE_THRESHOLD,
    )
    ctx = RunContext(run_id=uuid.uuid4().hex[:12], storage=SupabaseStorage(settings))
    ctx.bot_status = BotStatus(ctx.storage, settings, ctx.run_id)
    code = 1
    try:
        code = run(settings, ctx)
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            logger.error(
                "Alpaca ha rifiutato le credenziali (HTTP {}). Verifica di usare le chiavi dell'account PAPER "
                "(iniziano con 'PK') e che siano state rigenerate dopo un reset dell'account.", status,
            )
            ctx.finish("AUTH_ERROR", f"Credenziali Alpaca rifiutate (HTTP {status})")
        else:
            logger.error("Errore API Alpaca (HTTP {}): {}", status, str(exc)[:300])
            ctx.finish("ERROR", f"Errore API Alpaca (HTTP {status})")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Errore non gestito: {}: {}", type(exc).__name__, exc)
        ctx.finish("ERROR", f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        ctx.storage.log_run(ctx.run_id, ctx.status, ctx.message, settings.DRY_RUN)
        ctx.bot_status.next_trade_cycle_at = None
        ctx.bot_status.publish("STOPPED", ctx.last_clock, f"{ctx.status}: {ctx.message}")
        level = {"OK": "notice", "MARKET_CLOSED": "notice"}.get(ctx.status, "error")
        gh_annotation(level, f"Bot: {ctx.status}", ctx.message)
        if not ctx.storage.enabled:
            gh_annotation("warning", "Supabase non attivo",
                          "Nessun dato salvato: controlla i secret SUPABASE_URL (https://xxxx.supabase.co) e SUPABASE_SERVICE_ROLE_KEY")
        elif ctx.storage.error_count:
            gh_annotation("warning", "Supabase", f"{ctx.storage.error_count} scritture fallite: vedi il log dello step 'Run bot'")
        write_step_summary(ctx, settings)
    return code


if __name__ == "__main__":
    sys.exit(main())
