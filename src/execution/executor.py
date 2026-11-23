"""
Modulo 5a - Esecuzione ordini.

Regole:
- MAI ordini Market: solo Limit, TimeInForce.DAY.
- Multi-leg -> `OrderClass.MLEG` (un solo ordine atomico, limit_price netto con segno:
  > 0 = debito, < 0 = credito). Single-leg -> `OrderClass.SIMPLE` (limit_price sempre positivo).
- Price chasing: si parte dal mid; se non eseguito entro CHASE_INTERVAL_SEC si sposta il netto
  verso il mercato, fino a MAX_CHASE_ATTEMPTS volte, poi si cancella.
- Tetto di prezzo (`max_net`): il chasing non supera mai il tetto, anche se il prezzo "naturale"
  è più lontano (es. quote larghissime nei primi minuti dopo l'apertura).

Internamente il prezzo è sempre espresso come "netto con segno" (convenzione MLEG). Spostarsi verso il
mercato significa SEMPRE aumentare il netto: pagare un po' di più o incassare un po' di meno.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, PositionIntent, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest, ReplaceOrderRequest
from loguru import logger

from config.settings import Settings

TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
    OrderStatus.DONE_FOR_DAY,
    OrderStatus.REPLACED,
}
MIN_ABS_PRICE = 0.01


@dataclass(slots=True)
class OrderLegSpec:
    symbol: str
    side: OrderSide
    intent: PositionIntent
    ratio: int
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def signed_mid(self) -> float:
        return self.mid * self.ratio if self.side == OrderSide.BUY else -self.mid * self.ratio

    @property
    def signed_natural(self) -> float:
        """Prezzo 'naturale': si compra all'ask e si vende al bid (esecuzione quasi certa)."""
        return self.ask * self.ratio if self.side == OrderSide.BUY else -self.bid * self.ratio


@dataclass
class ExecutionResult:
    filled: bool
    dry_run: bool = False
    order_id: str | None = None
    filled_qty: int = 0
    fill_net_price: float | None = None  # per unità, con segno (+ debito / - credito)
    status: str = ""
    attempts: int = 0
    message: str = ""
    history: list[str] = field(default_factory=list)
    ladder: list[float] = field(default_factory=list)  # prezzi netti tentati
    natural: float | None = None
    leg_fills: dict[str, float] = field(default_factory=dict)  # prezzo medio eseguito per gamba (sempre positivo)


def net_mid(legs: list[OrderLegSpec]) -> float:
    return sum(leg.signed_mid for leg in legs)


def net_natural(legs: list[OrderLegSpec]) -> float:
    return sum(leg.signed_natural for leg in legs)


def slippage_base(legs: list[OrderLegSpec], width: float = 0.0) -> float:
    """Base su cui misurare slippage e larghezza delle quote: la larghezza dello spread, o il premio se single-leg."""
    return width if width > 0 else max(abs(net_mid(legs)), MIN_ABS_PRICE)


def quotes_too_wide(legs: list[OrderLegSpec], width: float, max_pct: float) -> bool:
    """True se attraversare il mercato (naturale - mid) costa più di `max_pct` della base."""
    return net_natural(legs) - net_mid(legs) > max_pct * slippage_base(legs, width) + 1e-9


def price_cap(legs: list[OrderLegSpec], width: float, max_slippage_pct: float) -> float:
    """Netto massimo accettabile: mid + max_slippage_pct della base (larghezza o premio)."""
    return net_mid(legs) + max_slippage_pct * slippage_base(legs, width)


def price_ladder(
    start: float, natural: float, attempts: int, min_step: float, to_natural: bool = True, cap: float | None = None
) -> list[float]:
    """
    Sequenza di prezzi netti per il chasing (sempre crescente = sempre più verso il mercato).

    to_natural=True: i tentativi sono distribuiti in modo uniforme dal mid al prezzo naturale, così
    l'ultimo tentativo è al naturale (bid per chi vende, ask per chi compra), con step minimo `min_step`.
    to_natural=False: comportamento "classico", step fissi da `min_step` (spesso troppo piccoli sugli spread).
    cap: netto massimo da non superare mai (sostituisce il naturale se più basso).
    """
    if cap is not None:
        cap = round(cap, 2)
        if cap <= round_net(start):  # il tetto è già al mid o sotto: un solo tentativo al tetto
            return [round_net(cap)]
        natural = min(natural, cap)
    start = round_net(start)
    ladder = [start]
    if to_natural and natural > start:
        for i in range(1, attempts + 1):
            target = round_net(start + (natural - start) * i / attempts)
            price = min(max(target, round_net(ladder[-1] + min_step)), round_net(natural))
            if price > ladder[-1]:
                ladder.append(price)
    else:
        for _ in range(attempts):
            nxt = round_net(ladder[-1] + min_step)
            if cap is not None and nxt > cap:
                break
            ladder.append(nxt)
    return ladder


def round_net(price: float) -> float:
    """Arrotonda al centesimo evitando un netto pari a zero (non accettato come limite)."""
    rounded = round(price, 2)
    if abs(rounded) < MIN_ABS_PRICE:
        rounded = MIN_ABS_PRICE
    return rounded


class OrderExecutor:
    def __init__(self, trading_client: TradingClient, settings: Settings) -> None:
        self.client = trading_client
        self.s = settings

    # ------------------------------------------------------------------ #
    # API pubblica
    # ------------------------------------------------------------------ #
    def execute(self, legs: list[OrderLegSpec], qty: int, label: str, max_net: float | None = None) -> ExecutionResult:
        """Esegue un ordine limit (single o multi-leg) con price chasing, senza mai superare `max_net`."""
        if not legs or qty < 1:
            return ExecutionResult(filled=False, message="Ordine vuoto o qty < 1")

        start_price = round_net(net_mid(legs))
        natural = round_net(net_natural(legs))
        ladder = price_ladder(start_price, natural, self.s.MAX_CHASE_ATTEMPTS, self.s.CHASE_STEP,
                              self.s.CHASE_TO_NATURAL, cap=max_net)
        order_class = OrderClass.MLEG if len(legs) > 1 else OrderClass.SIMPLE
        kind = "DEBITO" if start_price > 0 else "CREDITO"
        legs_txt = ", ".join(f"{leg.intent.value} {leg.ratio}x {leg.symbol} (bid {leg.bid:.2f}/ask {leg.ask:.2f})" for leg in legs)
        logger.info(
            "[{}] Ordine LIMIT {} qty {} @ netto {:.2f} {} | naturale {:+.2f} | gambe: {}",
            label, order_class.value.upper(), qty, abs(start_price), kind, natural, legs_txt,
        )
        logger.info(
            "[{}] Piano di chasing ({} tentativi ogni {}s): {}{}",
            label, len(ladder), self.s.CHASE_INTERVAL_SEC, " -> ".join(f"{p:+.2f}" for p in ladder),
            f" | tetto {max_net:+.2f}" if max_net is not None else "",
        )

        if self.s.DRY_RUN:
            msg = f"DRY_RUN: ordine NON inviato. Chasing previsto {ladder[0]:+.2f} -> {ladder[-1]:+.2f} (naturale {natural:+.2f})"
            logger.warning("[{}] {}", label, msg)
            return ExecutionResult(filled=False, dry_run=True, fill_net_price=start_price, message=msg, ladder=ladder, natural=natural)

        result = self._chase(legs, qty, ladder, order_class, label)
        result.ladder, result.natural = ladder, natural
        return result

    # ------------------------------------------------------------------ #
    # Chasing
    # ------------------------------------------------------------------ #
    def _chase(self, legs: list[OrderLegSpec], qty: int, ladder: list[float], order_class: OrderClass, label: str) -> ExecutionResult:
        result = ExecutionResult(filled=False)
        filled_total = 0
        fill_value = 0.0  # somma(prezzo netto * qty) delle esecuzioni parziali di ordini precedenti
        price = ladder[0]
        rejected = False

        try:
            order = self._submit(legs, qty, price, order_class)
        except APIError as exc:
            result.message = f"Ordine RIFIUTATO da Alpaca: {str(exc)[:200]}"
            result.status = "rejected"
            logger.error("[{}] {}", label, result.message)
            return result

        for attempt in range(len(ladder)):
            result.attempts = attempt + 1
            result.history.append(f"{order.id}@{price:+.2f}")
            order = self._wait(order, self.s.CHASE_INTERVAL_SEC)
            if order.status == OrderStatus.FILLED:
                break
            if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                rejected = order.status == OrderStatus.REJECTED
                logger.error("[{}] Ordine {} terminato con stato {}", label, order.id, order.status.value)
                break
            if attempt == len(ladder) - 1:
                logger.warning("[{}] Ultimo prezzo del piano ({:+.2f}) non eseguito: cancello l'ordine", label, price)
                order = self._cancel_and_wait(order)
                break

            new_price = ladder[attempt + 1]
            logger.info(
                "[{}] Non eseguito in {}s (stato {}). Chase #{}: netto {:+.2f} -> {:+.2f}",
                label, self.s.CHASE_INTERVAL_SEC, order.status.value, attempt + 1, price, new_price,
            )
            price = new_price
            order, carried_qty, carried_value = self._replace_or_resubmit(order, legs, qty - filled_total, price, order_class, label)
            filled_total += carried_qty
            fill_value += carried_value
            if order is None:  # tutto eseguito durante la cancellazione
                break

        if order is not None:
            order_filled = int(float(order.filled_qty or 0))
            if order_filled > 0:
                order_price = self._fill_net_price(order, order_class) or price
                filled_total += order_filled
                fill_value += order_price * order_filled
                result.leg_fills = self._leg_fills(order, order_class, legs)
            result.order_id = str(order.id)
            result.status = order.status.value

        result.filled_qty = filled_total
        result.filled = filled_total >= qty
        result.fill_net_price = round(fill_value / filled_total, 4) if filled_total else None
        if result.filled:
            result.status = "filled"
            result.message = f"Eseguito {filled_total}/{qty} @ netto {result.fill_net_price:+.2f}"
            logger.success("[{}] {}", label, result.message)
        elif filled_total > 0:
            result.message = f"Esecuzione PARZIALE {filled_total}/{qty} @ netto {result.fill_net_price:+.2f}"
            logger.warning("[{}] {}", label, result.message)
        elif rejected:
            result.message = "Ordine RIFIUTATO da Alpaca (verifica livello opzioni e buying power)"
        else:
            result.message = f"Nessuna esecuzione: provati {ladder[0]:+.2f} -> {price:+.2f}, poi cancellato"
            logger.warning("[{}] {}", label, result.message)
        return result

    # ------------------------------------------------------------------ #
    # Primitive Alpaca
    # ------------------------------------------------------------------ #
    def _build_request(self, legs: list[OrderLegSpec], qty: int, net_price: float, order_class: OrderClass) -> LimitOrderRequest:
        client_id = f"bot-{uuid.uuid4().hex[:20]}"
        if order_class == OrderClass.MLEG:
            return LimitOrderRequest(
                qty=qty,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                limit_price=round_net(net_price),  # MLEG: + debito / - credito
                legs=[
                    OptionLegRequest(symbol=leg.symbol, ratio_qty=leg.ratio, side=leg.side, position_intent=leg.intent)
                    for leg in legs
                ],
                client_order_id=client_id,
            )
        leg = legs[0]
        # SIMPLE: il prezzo limite è sempre positivo, il verso è dato da `side`
        # (buy: netto +2.00 -> limit 2.00 | sell: netto -1.50 -> limit 1.50)
        limit = abs(net_price)
        return LimitOrderRequest(
            symbol=leg.symbol,
            qty=qty * leg.ratio,
            side=leg.side,
            position_intent=leg.intent,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.DAY,
            limit_price=round(max(limit, MIN_ABS_PRICE), 2),
            client_order_id=client_id,
        )

    def _submit(self, legs: list[OrderLegSpec], qty: int, net_price: float, order_class: OrderClass) -> Order:
        order = self.client.submit_order(self._build_request(legs, qty, net_price, order_class))
        logger.info("Ordine inviato id={} stato={}", order.id, order.status.value)
        return order

    def _wait(self, order: Order, seconds: float) -> Order:
        deadline = time.monotonic() + seconds
        while True:
            order = self.client.get_order_by_id(order.id)
            if order.status in TERMINAL_STATUSES or time.monotonic() >= deadline:
                return order
            time.sleep(self.s.ORDER_POLL_SEC)

    def _cancel_and_wait(self, order: Order, timeout: float = 10.0) -> Order:
        try:
            self.client.cancel_order_by_id(order.id)
        except APIError as exc:
            logger.debug("Cancel ordine {}: {}", order.id, exc)  # tipicamente già eseguito/cancellato
        deadline = time.monotonic() + timeout
        while True:
            order = self.client.get_order_by_id(order.id)
            if order.status in TERMINAL_STATUSES or time.monotonic() >= deadline:
                return order
            time.sleep(1.0)

    def _replace_or_resubmit(
        self, order: Order, legs: list[OrderLegSpec], remaining_qty: int, net_price: float, order_class: OrderClass, label: str
    ) -> tuple[Order | None, int, float]:
        """
        Prova `replace_order_by_id`; se Alpaca non lo consente (es. su alcuni MLEG) ripiega su
        cancel + nuovo ordine. Ritorna (nuovo_ordine, qty_eseguita_dal_vecchio, valore_eseguito_dal_vecchio).
        """
        limit = round_net(net_price) if order_class == OrderClass.MLEG else round(max(abs(net_price), MIN_ABS_PRICE), 2)
        try:
            new_order = self.client.replace_order_by_id(order.id, ReplaceOrderRequest(limit_price=limit))
            logger.debug("[{}] Replace OK: {} -> {}", label, order.id, new_order.id)
            return new_order, 0, 0.0
        except APIError as exc:
            logger.info("[{}] Replace non disponibile ({}), uso cancel + nuovo ordine", label, exc)

        old = self._cancel_and_wait(order)
        old_filled = int(float(old.filled_qty or 0))
        old_value = (self._fill_net_price(old, order_class) or net_price) * old_filled if old_filled else 0.0
        remaining = remaining_qty - old_filled
        if old.status == OrderStatus.FILLED or remaining <= 0:
            return None, old_filled, old_value
        try:
            return self._submit(legs, remaining, net_price, order_class), old_filled, old_value
        except APIError as exc:
            logger.error("[{}] Nuovo ordine rifiutato: {}", label, exc)
            return None, old_filled, old_value

    @staticmethod
    def _leg_fills(order: Order, order_class: OrderClass, legs: list[OrderLegSpec]) -> dict[str, float]:
        """Prezzo medio di esecuzione di ogni gamba, per mostrarlo in dashboard."""
        fills: dict[str, float] = {}
        if order_class == OrderClass.MLEG:
            for leg in order.legs or []:
                if leg.symbol and leg.filled_avg_price is not None:
                    fills[leg.symbol] = round(float(leg.filled_avg_price), 4)
        elif order.filled_avg_price is not None and legs:
            fills[legs[0].symbol] = round(float(order.filled_avg_price), 4)
        return fills

    @staticmethod
    def _fill_net_price(order: Order, order_class: OrderClass) -> float | None:
        """Prezzo netto di esecuzione per unità, con segno (+ debito / - credito)."""
        if order_class == OrderClass.MLEG and order.legs:
            total = 0.0
            for leg in order.legs:
                if leg.filled_avg_price is None:
                    return None
                price = float(leg.filled_avg_price) * float(leg.ratio_qty or 1)
                total += price if leg.side == OrderSide.BUY else -price
            return total
        if order.filled_avg_price is None:
            return None
        price = float(order.filled_avg_price)
        return price if order.side == OrderSide.BUY else -price
