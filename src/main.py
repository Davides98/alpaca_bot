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
import uuid
from dataclasses import dataclass
from datetime import timezone
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, PositionIntent
from loguru import logger

from config.settings import PAPER_TRADING, Settings, get_settings
from src.analysis.screener import Screener
from src.data.market_data import MarketDataService
from src.execution.executor import OrderExecutor, OrderLegSpec
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


def run(settings: Settings, ctx: RunContext) -> int:
    run_id = ctx.run_id
    storage = ctx.storage
    mode = "DRY_RUN (nessun ordine verrà inviato)" if settings.DRY_RUN else "PAPER TRADING (ordini reali sull'account paper)"
    logger.info("=" * 70)
    logger.info("Alpaca Options Bot | run {} | modalità: {}", run_id, mode)
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

    # 1) Mercato aperto?
    clock = trading.get_clock()
    if not clock.is_open:
        if settings.DRY_RUN and settings.IGNORE_MARKET_HOURS:
            logger.warning("Mercato CHIUSO, ma DRY_RUN + IGNORE_MARKET_HOURS: proseguo a scopo di test (quote potenzialmente stantie)")
        else:
            next_open = clock.next_open.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            logger.info("Mercato USA chiuso (prossima apertura: {}). Nessuna azione, esco.", next_open)
            ctx.finish("MARKET_CLOSED", f"Mercato chiuso, prossima apertura {next_open}. Nessuna operazione.")
            return 0
    else:
        logger.info("Mercato aperto (chiusura alle {})", clock.next_close)

    market = MarketDataService(settings)
    executor = OrderExecutor(trading, settings)
    risk = RiskManager(settings, trading, market, executor, storage)
    selector = StrategySelector(settings)

    # 2) Account + snapshot equity
    account = trading.get_account()
    equity = float(account.equity or 0)
    cash = float(account.cash or 0)
    last_equity = float(account.last_equity or equity)
    logger.info("Account {} | options level {} | buying power opzioni ${:,.2f}",
                account.status.value if account.status else "?",
                getattr(account, "options_trading_level", "?"),
                float(getattr(account, "options_buying_power", 0) or 0))
    storage.save_equity_snapshot(equity, cash, last_equity)

    # 3) Gestione posizioni aperte
    report = risk.manage_open_positions()

    # 4) Screening
    screener = Screener(settings, market)
    output = screener.run(occupied=report.occupied_underlyings)
    slots = min(settings.MAX_NEW_TRADES_PER_RUN, settings.MAX_OPEN_POSITIONS - report.open_count)

    # Lo screener pre-seleziona i migliori: qui la selezione diventa definitiva
    for row in output.rows:
        if row.selected:
            row.selected = False
            row.reason = "Idoneo"

    # 5) Nuovi trade
    opened = 0  # ordini tentati
    filled = 0  # ordini effettivamente eseguiti
    if slots <= 0:
        logger.info("Nessuno slot libero ({} posizioni aperte, max {}): non apro nuovi trade",
                    report.open_count, settings.MAX_OPEN_POSITIONS)
        for cand in output.ranked:
            cand.row.reason = "Idoneo ma limite posizioni raggiunto"
    elif not output.ranked:
        logger.info("Nessun candidato idoneo in questo run: il bot resta fermo")
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

            result = executor.execute(order_legs, plan.qty, f"OPEN {plan.ticker} {plan.strategy.value}")
            opened += 1
            if result.dry_run:
                cand.row.reason += f" (DRY_RUN, non inviato: {result.message})"
            elif result.filled_qty > 0 and result.fill_net_price is not None:
                filled += 1
                storage.insert_trade(plan_to_trade_row(plan, result.fill_net_price, result.filled_qty, result.order_id))
                cand.row.reason += f" - ESEGUITO {result.filled_qty} @ {result.fill_net_price:+.2f}"
            else:
                cand.row.reason += f" - NON eseguito: {result.message}"

    # 6) Log screening
    storage.save_screening_logs([r.to_record() for r in output.rows], run_id)

    summary = (
        f"Equity ${equity:,.0f} | {len(output.rows)} ticker analizzati, {len(output.ranked)} idonei | "
        f"posizioni chiuse {len(report.closed)} | "
        + (f"ordini simulati {opened}" if settings.DRY_RUN else f"ordini inviati {opened}, eseguiti {filled}")
        + f" | posizioni aperte prima del run {report.open_count}"
    )
    logger.info("=" * 70)
    logger.info("Fine run {} | {}", run_id, summary)
    logger.info("=" * 70)
    ctx.finish("OK", summary)
    return 0


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    ctx = RunContext(run_id=uuid.uuid4().hex[:12], storage=SupabaseStorage(settings))
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
