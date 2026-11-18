# Alpaca Options Bot (didattico, solo Paper Trading)

Bot autonomo di trading su opzioni che opera **esclusivamente su Alpaca Paper Trading**. Ad ogni run:

1. controlla che il mercato USA sia aperto;
2. salva uno snapshot dell'equity su Supabase;
3. gestisce le posizioni aperte (take profit, stop loss, chiusura pre-scadenza);
4. esegue lo **screening** della watchlist e calcola il **regime di mercato** (trend + volatilità);
5. sceglie una **strategia in opzioni** con regole deterministiche e i contratti reali dalla chain;
6. invia ordini **Limit** (single-leg o multi-leg `MLEG`) con *price chasing*;
7. salva trade e log di screening su Supabase, visibili in una **dashboard web** statica.

> ⚠️ Progetto a scopo **didattico**. Non è un consiglio finanziario. Il codice forza `paper=True`
> e non può operare su un conto reale.

---

## Indice

1. [Architettura](#architettura)
2. [Logica di trading](#logica-di-trading)
3. [Setup locale e test in DRY_RUN](#1-setup-locale-e-test-in-dry_run)
4. [Creazione del progetto Supabase](#2-creazione-del-progetto-supabase)
5. [GitHub Secrets e automazione](#3-github-secrets-e-automazione-github-actions)
6. [Dashboard su GitHub Pages](#4-dashboard-su-github-pages)
7. [Passare dal DRY_RUN al paper trading](#5-passare-dal-dry_run-al-paper-trading)
8. [Sicurezza](#sicurezza)
9. [Limiti noti](#limiti-noti-e-note-didattiche)

---

## Architettura

```text
.
├── .github/workflows/run_bot.yml   # cron ogni 30 min (lun-ven) + trigger manuale con dry_run
├── config/settings.py              # tutti i parametri (pydantic-settings, sovrascrivibili da env)
├── supabase/schema.sql             # tabelle + indici + RLS (anon = sola lettura) + realtime
├── docs/index.html                 # dashboard (Tailwind + Chart.js + supabase-js via CDN)
├── src/
│   ├── data/market_data.py         # barre daily + option chain filtrata (DTE, liquidità)
│   ├── analysis/regime.py          # EMA20/50, ADX14, HV20, proxy IV Rank -> MarketRegime
│   ├── analysis/screener.py        # Opportunity Score 0-100 + filtri di esclusione
│   ├── strategy/selector.py        # matrice strategie + contract picker per delta -> TradePlan
│   ├── execution/executor.py       # ordini Limit SIMPLE/MLEG + price chasing
│   ├── execution/risk_manager.py   # sizing, TP/SL, chiusura a DTE <= 7
│   ├── storage/supabase_client.py  # scritture resilienti su Supabase
│   └── main.py                     # orchestratore (un run completo)
└── tests/test_regime.py            # unit test (nessuna chiamata di rete)
```

---

## Logica di trading

### Regime di mercato (`regime.py`)

| Dimensione | Regola |
|---|---|
| **Trend** | `ADX(14) < 20` → `SIDEWAYS`; `ADX >= 20`, `EMA20 > EMA50` e prezzo > EMA20 → `BULLISH`; `ADX >= 20`, `EMA20 < EMA50` e prezzo < EMA20 → `BEARISH`; segnali contrastanti → `SIDEWAYS` |
| **Volatilità** | IV Rank proxy = posizione della IV ATM attuale nel range min-max della volatilità realizzata a 20 giorni dell'ultimo anno. `>= 50` → `HIGH_IV`, altrimenti `LOW_IV` |

### Opportunity Score (`screener.py`)

`Score = 45% segnale di volatilità + 35% segnale di trend + 20% liquidità`

- **Volatilità:** con `HIGH_IV` (strategie a credito) conta l'IV Rank; con `LOW_IV` (strategie a debito) conta `100 - IV Rank`.
- **Trend:** per le strategie direzionali premia un ADX alto (20 → 0 punti, 50 → 100 punti). Per l'Iron Condor premia un ADX basso.
- **Liquidità:** spread bid-ask ATM a 0% → 100 punti; spread pari a `MAX_SPREAD_PCT` → 0 punti.

Vengono scartati i ticker `SIDEWAYS + LOW_IV`, quelli illiquidi, quelli con una posizione o un ordine già aperto e quelli con score sotto `MIN_OPPORTUNITY_SCORE`.

### Matrice delle strategie (`selector.py`)

| Trend \ IV | HIGH_IV (credito) | LOW_IV (debito) |
|---|---|---|
| **SIDEWAYS** | Iron Condor (short ±0.20Δ, ali ±5$) | NO_TRADE |
| **BULLISH** | Bull Put Spread (short put 0.30Δ, long −5$) | Bull Call Spread (long call 0.60Δ, short +5$), oppure Long Call |
| **BEARISH** | Bear Call Spread (short call 0.30Δ, long +5$) | Bear Put Spread (long put 0.60Δ, short −5$), oppure Long Put |

La scadenza scelta è quella più vicina al centro della finestra `MIN_DTE`–`MAX_DTE` (~37 giorni). Con `PREFER_SINGLE_LEG_DEBIT=true` il bot usa Long Call/Put al posto dei debit spread.

### Rischio ed esecuzione

- **Sizing:** `qty = floor(equity × 2% / perdita massima per contratto)`. Se anche un solo contratto supera il budget, il trade viene scartato.
- **Uscite:**
  - credito: TP a +50% del credito, SL a −100% del credito;
  - debito: TP a +40%, SL a −30%;
  - sempre: chiusura con `DTE <= 7`.
- **Ordini:** solo Limit `DAY`, al mid-price. Se l'ordine non viene eseguito entro 15 s, il prezzo si sposta di 0.01$ verso il mercato, al massimo 3 volte, poi l'ordine viene cancellato. Il chasing usa prima `replace_order_by_id` e, se Alpaca non lo accetta, ripiega su cancel + nuovo ordine.
- **Convenzione prezzi** (come Alpaca per gli ordini `MLEG`): un prezzo netto **positivo è un debito**, uno **negativo è un credito**. Nella dashboard: `1,42 CR` = credito incassato, `2,10 DB` = debito pagato.

Esempio di ordine multi-leg generato (Bull Put Spread a credito):

```python
LimitOrderRequest(
    order_class=OrderClass.MLEG,
    qty=4,
    time_in_force=TimeInForce.DAY,
    limit_price=-0.53,  # negativo = credito
    legs=[
        OptionLegRequest(symbol="SPY261226P00485000", ratio_qty=1, side=OrderSide.SELL, position_intent=PositionIntent.SELL_TO_OPEN),
        OptionLegRequest(symbol="SPY261226P00480000", ratio_qty=1, side=OrderSide.BUY, position_intent=PositionIntent.BUY_TO_OPEN),
    ],
)
```

---

## 1. Setup locale e test in DRY_RUN

### Prerequisiti

- Python 3.11+
- Un account [Alpaca](https://alpaca.markets) (gratuito) con le chiavi **Paper Trading**: dashboard → seleziona l'account *Paper* → *API Keys* → *Generate New Keys*. La key inizia con `PK`.

### Installazione

```bash
git clone <url-del-tuo-repo>
cd <cartella-del-repo>

python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### Configurazione

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Apri `.env` e compila almeno `ALPACA_API_KEY` e `ALPACA_SECRET_KEY`. Lascia `DRY_RUN=true`. Supabase è facoltativo: se non lo configuri, il bot funziona lo stesso e lo segnala nei log.

### Test

```bash
python -m pytest -q        # unit test, nessuna chiamata di rete
python -m src.main         # un run completo
```

Con `DRY_RUN=true` il bot esegue l'intera pipeline: screening, regime, scelta della strategia, selezione dei contratti, sizing e calcolo dei prezzi di chasing. Stampa tutto nei log ma **non invia ordini** e **non scrive trade** su Supabase. Scrive comunque gli snapshot dell'equity e i log di screening, che sono dati reali utili alla dashboard.

**Mercato chiuso?** Normalmente il bot esce subito con exit code 0. Per testare nel weekend aggiungi `IGNORE_MARKET_HOURS=true` (vale solo con `DRY_RUN=true`). Tieni presente che a mercato chiuso le quote delle opzioni possono essere stantie o a zero, quindi molti contratti verranno scartati dal filtro di liquidità.

Esempio di output dello screener:

```text
Ticker | Trend    | IV      |   IVR |   ADX |  Spread |  Score | Sel | Strategia / Motivo
--------------------------------------------------------------------------------------
QQQ    | BULLISH  | HIGH_IV |    72 |  31.4 |    1.6% |   63.2 | SI  | BULL_PUT_SPREAD / Selezionato (miglior score)
SPY    | SIDEWAYS | HIGH_IV |    58 |  16.0 |    1.1% |   50.7 | no  | IRON_CONDOR / Idoneo
TLT    | SIDEWAYS | LOW_IV  |    21 |  14.2 |    2.8% |    0.0 | no  | NO_TRADE / SIDEWAYS + LOW_IV: nessun edge
```

---

## 2. Creazione del progetto Supabase

1. Vai su [supabase.com](https://supabase.com), crea un account e poi **New project** (il piano Free basta). Scegli una regione vicina e una password del database robusta.
2. Quando il progetto è pronto, apri **SQL Editor → New query**.
3. Incolla **tutto** il contenuto di [`supabase/schema.sql`](supabase/schema.sql) e premi **Run**. Lo script crea:
   - le tabelle `equity_snapshots`, `trades` e `screening_logs`, con i relativi indici;
   - la **Row Level Security** su tutte e 3, con la sola policy `"Allow public read"` per il ruolo `anon`, quindi nessun INSERT/UPDATE/DELETE pubblico;
   - l'abilitazione del **Realtime**, così la dashboard si aggiorna appena il bot scrive.

   Lo script è idempotente: puoi rieseguirlo senza problemi.
4. Vai in **Project Settings → API** (o **API Keys**) e annota:
   - **Project URL** → `SUPABASE_URL`;
   - la chiave **service_role** (o **secret**, `sb_secret_...`) → `SUPABASE_SERVICE_ROLE_KEY`. È segreta: va solo nel `.env` e nei GitHub Secrets;
   - la chiave **anon** (o **publishable**, `sb_publishable_...`) → `SUPABASE_ANON_KEY`. È pubblica e va solo nella dashboard.
5. Aggiungi `SUPABASE_URL` e `SUPABASE_SERVICE_ROLE_KEY` al tuo `.env` e rilancia `python -m src.main`. In **Table Editor** dovresti vedere comparire le prime righe.

> Verifica della RLS: con la sola chiave anon, un `insert` deve fallire con un errore di policy, mentre un `select` deve funzionare.

---

## 3. GitHub Secrets e automazione (GitHub Actions)

1. Crea un repository **pubblico** su GitHub e fai il push del progetto. Controlla che `.env` **non** sia tra i file committati (è già in `.gitignore`).
2. Nel repository vai in **Settings → Secrets and variables → Actions → New repository secret** e crea questi 4 secret:

   | Nome | Valore |
   |---|---|
   | `ALPACA_API_KEY` | key paper di Alpaca |
   | `ALPACA_SECRET_KEY` | secret paper di Alpaca |
   | `SUPABASE_URL` | Project URL di Supabase |
   | `SUPABASE_SERVICE_ROLE_KEY` | chiave service_role / secret di Supabase |

   I secret non vengono passati ai workflow lanciati da pull request di fork, e GitHub li maschera automaticamente nei log.
3. *(Facoltativo)* Nella tab **Variables** crea `DRY_RUN` con valore `true` o `false`: controlla i run **schedulati**. Se non la crei, i run schedulati restano in `DRY_RUN=true` per sicurezza.
4. **Test manuale:** tab **Actions → Run Options Bot → Run workflow**. Lascia `dry_run` spuntato e premi *Run*. Per provare a mercato chiuso spunta anche `ignore_market_hours`.
5. **Schedulazione:** il cron `0,30 14-20 * * 1-5` lancia il bot ogni 30 minuti dalle 14:00 alle 20:30 UTC, dal lunedì al venerdì. Questa fascia copre l'orario di Wall Street sia con l'ora solare sia con quella legale; fuori orario e nei festivi il bot esce subito grazie a `get_clock().is_open`.

Note su GitHub Actions:

- I cron di GitHub possono partire con qualche minuto di ritardo.
- GitHub **disattiva i workflow schedulati** dei repository pubblici dopo 60 giorni senza attività (commit). Basta riattivarli dalla tab Actions.
- `concurrency` impedisce che due run si sovrappongano, evitando ordini duplicati.

---

## 4. Dashboard su GitHub Pages

1. Apri [`docs/index.html`](docs/index.html) e, all'inizio del blocco `<script>`, compila:

   ```js
   const SUPABASE_URL = "https://abcdefgh.supabase.co";   // Project URL
   const SUPABASE_ANON_KEY = "eyJhbGciOi...";             // chiave anon / publishable (MAI la service_role!)
   ```

   La chiave anon può stare in un repository pubblico: grazie alla RLS permette **solo la lettura** delle tre tabelle.
2. Fai commit e push.
3. Su GitHub vai in **Settings → Pages**. Come *Source* scegli **Deploy from a branch**, come *Branch* `main` e come cartella **`/docs`**, poi premi **Save**.
4. Dopo circa un minuto la dashboard sarà online su `https://<utente>.github.io/<repo>/`.

**In alternativa, Vercel:** *New Project* → importa il repo → *Root Directory* `docs` → Framework *Other* → Deploy.

La dashboard mostra:
- le KPI: capitale iniziale e attuale, rendimento totale in `$` e `%`, P&L giornaliero, ultimo aggiornamento;
- il grafico dell'equity, con toggle `$`/`%`, filtro temporale e tabella dei dati;
- le posizioni aperte (con P&L non realizzato) e lo storico dei trade;
- l'ultimo screening, con il motivo per cui ogni ticker è stato scelto o scartato.

Si aggiorna in tempo reale tramite Supabase Realtime, con un fallback di polling ogni 60 secondi.

---

## 5. Passare dal DRY_RUN al paper trading

Quando i log in DRY_RUN ti convincono:

- **in locale:** imposta `DRY_RUN=false` nel `.env`;
- **su GitHub:** imposta la variabile di repository `DRY_RUN` a `false` per i run schedulati, oppure lancia un run manuale togliendo la spunta a `dry_run`.

Gli ordini verranno inviati all'account **paper**. Prima di farlo, controlla nella dashboard di Alpaca (account Paper → *Account* → *Options*) che il livello di trading in opzioni sia **3**, necessario per spread e Iron Condor. Con livello 1–2 imposta `PREFER_SINGLE_LEG_DEBIT=true`; i credit spread verranno comunque rifiutati.

Tutti i parametri di [`config/settings.py`](config/settings.py) sono sovrascrivibili via variabili d'ambiente, ad esempio `MAX_RISK_PER_TRADE_PCT=0.01` o `WATCHLIST=SPY,QQQ,IWM`.

---

## Sicurezza

- `paper=True` è **fisso nel codice** del `TradingClient`; non esiste un parametro per passare al live.
- Le credenziali sono lette solo da variabili d'ambiente o da `.env` e sono `SecretStr`, quindi non compaiono in `repr()` né nei log.
- Il logger maschera ogni occorrenza dei segreti (`***REDACTED***`) e ha `diagnose=False`, così i traceback non stampano i valori delle variabili locali.
- Supabase: il bot scrive con la service_role key, che sta solo nei secret. La dashboard legge con la chiave anon, limitata dalla RLS alla sola lettura.
- `.gitignore` esclude `.env`, `.venv/`, `__pycache__/`, i log e i file di chiavi.

---

## Limiti noti e note didattiche

- **IV Rank approssimato:** i feed gratuiti non forniscono lo storico della IV, quindi l'IV ATM attuale viene confrontata con il range della volatilità *realizzata*. Poiché la IV di solito è più alta della HV (*volatility risk premium*), il regime `HIGH_IV` risulta più frequente di quanto lo sarebbe con un vero IV Rank.
- **Feed gratuiti:** azioni via `iex` e opzioni via `indicative`. Le quote indicative possono essere meno precise del feed OPRA. Con un abbonamento imposta `STOCK_DATA_FEED=sip` e `OPTIONS_DATA_FEED=opra`.
- **Tick size:** alcune opzioni sopra i 3$ quotano a incrementi di 0.05$. Uno step di chasing di 0.01$ può quindi venire arrotondato o rifiutato; in quel caso regola `CHASE_STEP`.
- **Chiusura MLEG:** Alpaca a volte rifiuta la chiusura multi-leg con "uncovered short contracts". In quel caso il bot ripiega automaticamente su ordini single-leg: prima ricompra le short, poi vende le long.
- **Assegnazione ed esercizio:** il bot chiude a 7 DTE per ridurre il rischio. Se una posizione sparisce da Alpaca (scadenza, assegnazione o chiusura manuale), il trade viene marcato `CLOSED` con motivo `NOT_FOUND_IN_BROKER`.
- **Paper trading ≠ realtà:** i fill simulati di Alpaca sono più ottimistici di quelli reali, soprattutto sugli spread.
