"""
Configurazione centralizzata del bot (pydantic-settings).

Ogni campo può essere sovrascritto da una variabile d'ambiente con lo stesso nome
(o da un file `.env` nella root del progetto). Le credenziali sono `SecretStr`:
non vengono mai mostrate in chiaro da `repr()`/log.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Il bot opera ESCLUSIVAMENTE in paper trading. Non è un parametro configurabile.
PAPER_TRADING: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # su GitHub un Secret/Variable non definito arriva come stringa vuota -> uso il default
        case_sensitive=True,
    )

    # ------------------------------------------------------------------ #
    # Credenziali (solo da env / .env)
    # ------------------------------------------------------------------ #
    ALPACA_API_KEY: SecretStr = SecretStr("")
    ALPACA_SECRET_KEY: SecretStr = SecretStr("")
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: SecretStr = SecretStr("")

    # ------------------------------------------------------------------ #
    # Modalità di esecuzione
    # ------------------------------------------------------------------ #
    DRY_RUN: bool = False  # False = invia ordini (sempre e solo sull'account PAPER)
    IGNORE_MARKET_HOURS: bool = False  # efficace solo se DRY_RUN=true
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------ #
    # Modalità sessione (un job resta attivo per tutta la sessione di mercato)
    # ------------------------------------------------------------------ #
    SESSION_MODE: bool = False  # False = run singolo (locale / manuale); True = ciclo continuo (GitHub schedulato)
    REFRESH_INTERVAL_SEC: int = Field(default=60, ge=30, le=600)  # equity + P&L + TP/SL
    TRADE_INTERVAL_MIN: int = Field(default=1, ge=1, le=120)  # screening + nuovi trade
    SESSION_MAX_MINUTES: int = Field(default=340, ge=5, le=350)  # i job GitHub durano al massimo 360 min
    # Se l'apertura è vicina la aspetta. Breve di proposito: lo scheduler Supabase riavvia il bot ogni pochi
    # minuti, quindi è meglio uscire e ripartire che restare fermi a lungo senza dare segni di vita.
    SESSION_WAIT_FOR_OPEN_MIN: int = Field(default=20, ge=0, le=120)

    # ------------------------------------------------------------------ #
    # Universo e dati
    # ------------------------------------------------------------------ #
    WATCHLIST: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["SPY", "QQQ", "IWM", "TLT", "GLD", "AAPL", "MSFT", "NVDA", "AMZN", "AMD"]
    )
    HISTORY_TRADING_DAYS: int = 252
    STOCK_DATA_FEED: str = "iex"  # "iex" (gratuito) | "sip" (abbonamento)
    OPTIONS_DATA_FEED: str = "indicative"  # "indicative" (gratuito) | "opra" (abbonamento)
    STRIKE_RANGE_PCT: float = 0.20  # scarica solo strike entro ±20% dal prezzo spot
    RATE_LIMIT_SLEEP_SEC: float = 0.35  # pausa tra ticker (Alpaca: ~200 req/min)

    # ------------------------------------------------------------------ #
    # Filtri sulle opzioni
    # ------------------------------------------------------------------ #
    MIN_DTE: int = 30
    MAX_DTE: int = 45
    CLOSE_DTE_THRESHOLD: int = 7
    MAX_SPREAD_PCT: float = 0.10

    # ------------------------------------------------------------------ #
    # Regime detector
    # ------------------------------------------------------------------ #
    EMA_FAST: int = 20
    EMA_SLOW: int = 50
    ADX_PERIOD: int = 14
    ADX_TREND_THRESHOLD: float = 20.0
    HV_WINDOW: int = 20
    IV_RANK_HIGH_THRESHOLD: float = 50.0

    # ------------------------------------------------------------------ #
    # Screener
    # ------------------------------------------------------------------ #
    MIN_OPPORTUNITY_SCORE: float = Field(default=30.0, ge=0, le=100)

    # ------------------------------------------------------------------ #
    # Strategie / contract picker
    # ------------------------------------------------------------------ #
    WING_WIDTH: float = 5.0  # distanza in $ tra gamba short e long
    IRON_CONDOR_SHORT_DELTA: float = 0.20
    CREDIT_SPREAD_SHORT_DELTA: float = 0.30
    DEBIT_LONG_DELTA: float = 0.60
    PREFER_SINGLE_LEG_DEBIT: bool = False  # True = Long Call/Put invece del Debit Spread
    MIN_CREDIT_PCT_OF_WIDTH: float = 0.10  # credito minimo accettabile (10% della larghezza)
    # Debito massimo per i debit spread, in frazione della larghezza: oltre, il rapporto rischio/rendimento
    # è sfavorevole (es. 4.15 su 5$ = 83% -> guadagno max 0.85 contro perdita max 4.15). Vale anche come tetto al chasing.
    MAX_DEBIT_PCT_OF_WIDTH: float = Field(default=0.65, gt=0, lt=1.0)
    MAX_CONTRACTS_PER_TRADE: int = 10

    # ------------------------------------------------------------------ #
    # Risk management
    # ------------------------------------------------------------------ #
    MAX_RISK_PER_TRADE_PCT: float = 0.02
    MAX_OPEN_POSITIONS: int = Field(default=3, ge=1, le=20)
    MAX_NEW_TRADES_PER_RUN: int = Field(default=1, ge=1, le=10)
    # Soglie di uscita per OGNI posizione, in frazione del credito incassato / debito pagato.
    # Accettano sia 0.30 sia "30%". Esempio: credito incassato 625$, TAKE_PROFIT_CREDIT_PCT=0.30 -> chiude a +187.50$.
    TAKE_PROFIT_CREDIT_PCT: float = Field(default=0.50, gt=0, le=1.0)  # +50% del credito (il massimo teorico è 100%)
    STOP_LOSS_CREDIT_PCT: float = Field(default=1.00, gt=0, le=5.0)  # -100% del credito incassato
    TAKE_PROFIT_DEBIT_PCT: float = Field(default=0.40, gt=0, le=10.0)  # +40% del debito pagato
    STOP_LOSS_DEBIT_PCT: float = Field(default=0.50, gt=0, le=1.0)  # -50% del debito pagato (il massimo è 100%)
    # Lo stop loss scatta solo se la condizione si ripete per N controlli consecutivi (modalità sessione):
    # evita di chiudere su un singolo prezzo anomalo.
    STOP_LOSS_CONFIRM_CHECKS: int = Field(default=2, ge=1, le=10)
    # Dopo uno stop loss il ticker resta in pausa (nessun nuovo trade) per queste ore.
    STOP_LOSS_COOLDOWN_HOURS: int = Field(default=24, ge=0, le=240)

    # ------------------------------------------------------------------ #
    # Finestre di mercato
    # ------------------------------------------------------------------ #
    # Nei primi minuti dopo l'apertura le quote delle opzioni sono larghissime e inaffidabili:
    # niente nuovi trade e niente take profit / stop loss (lo snapshot dell'equity continua).
    NO_TRADE_AFTER_OPEN_MIN: int = Field(default=15, ge=0, le=120)
    # Negli ultimi minuti prima della chiusura non apre nuovi trade (le uscite restano attive).
    NO_NEW_TRADES_BEFORE_CLOSE_MIN: int = Field(default=10, ge=0, le=120)

    # ------------------------------------------------------------------ #
    # Esecuzione
    # ------------------------------------------------------------------ #
    CHASE_INTERVAL_SEC: int = 15
    MAX_CHASE_ATTEMPTS: int = 3
    CHASE_STEP: float = 0.01  # step MINIMO tra un tentativo e il successivo
    # True: i tentativi vanno dal mid fino al prezzo "naturale" (vendi al bid / compri all'ask), così
    # l'ultimo tentativo ha alta probabilità di esecuzione. False: step fissi da CHASE_STEP (spesso
    # insufficienti sugli spread multi-leg, il cui naturale dista facilmente 0.10-0.30$ dal mid).
    CHASE_TO_NATURAL: bool = True
    # Tetto al chasing: non si paga mai più del mid + questa frazione della larghezza dello spread
    # (per le opzioni singole: del premio). Se non viene eseguito, si riprova al ciclo successivo.
    MAX_SLIPPAGE_PCT_OF_WIDTH: float = Field(default=0.10, ge=0, le=1.0)
    # Quote troppo larghe: se (naturale - mid) supera questa frazione della larghezza, non si apre e
    # non si valuta take profit / stop loss in quel ciclo.
    MAX_QUOTE_WIDTH_PCT_OF_WIDTH: float = Field(default=0.15, gt=0, le=1.0)
    ORDER_POLL_SEC: float = 2.0

    # ------------------------------------------------------------------ #
    # Performance
    # ------------------------------------------------------------------ #
    INITIAL_CAPITAL: float = 100_000.0

    # ------------------------------------------------------------------ #
    # Validatori
    # ------------------------------------------------------------------ #
    @field_validator("WATCHLIST", mode="before")
    @classmethod
    def _parse_watchlist(cls, value: Any) -> Any:
        """Accetta sia 'SPY,QQQ' sia '["SPY","QQQ"]' sia una lista Python."""
        if isinstance(value, str):
            cleaned = value.strip().strip("[]")
            return [s.strip().strip("'\"").upper() for s in cleaned.split(",") if s.strip().strip("'\"")]
        if isinstance(value, (list, tuple)):
            return [str(s).strip().upper() for s in value if str(s).strip()]
        return value

    @field_validator(
        "TAKE_PROFIT_CREDIT_PCT", "STOP_LOSS_CREDIT_PCT", "TAKE_PROFIT_DEBIT_PCT", "STOP_LOSS_DEBIT_PCT",
        "MAX_RISK_PER_TRADE_PCT", "MAX_DEBIT_PCT_OF_WIDTH", "MAX_SLIPPAGE_PCT_OF_WIDTH", "MAX_QUOTE_WIDTH_PCT_OF_WIDTH",
        mode="before",
    )
    @classmethod
    def _parse_percent(cls, value: Any) -> Any:
        """Accetta '30%' come 0.30 (oltre al formato decimale 0.30)."""
        if isinstance(value, str) and value.strip().endswith("%"):
            return float(value.strip().rstrip("%").replace(",", ".")) / 100.0
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.MIN_DTE >= self.MAX_DTE:
            raise ValueError("MIN_DTE deve essere minore di MAX_DTE")
        if self.CLOSE_DTE_THRESHOLD >= self.MIN_DTE:
            raise ValueError("CLOSE_DTE_THRESHOLD deve essere minore di MIN_DTE")
        if not 0 < self.MAX_RISK_PER_TRADE_PCT <= 0.10:
            raise ValueError("MAX_RISK_PER_TRADE_PCT deve essere in (0, 0.10]")
        return self

    # ------------------------------------------------------------------ #
    # Helper
    # ------------------------------------------------------------------ #
    @property
    def alpaca_configured(self) -> bool:
        return bool(self.ALPACA_API_KEY.get_secret_value() and self.ALPACA_SECRET_KEY.get_secret_value())

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value())

    def secret_values(self) -> list[str]:
        """Valori segreti da mascherare nei log."""
        values = [
            self.ALPACA_API_KEY.get_secret_value(),
            self.ALPACA_SECRET_KEY.get_secret_value(),
            self.SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
        ]
        return [v for v in values if v and len(v) >= 6]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
