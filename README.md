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
  - debito: TP a +40%, SL a −50%;
  - sempre: chiusura con `DTE <= 7`.
- **Ordini:** solo Limit `DAY`. Si parte dal mid-price; ogni 15 s senza esecuzione il prezzo si avvicina al mercato, con 3 tentativi distribuiti fino al **prezzo naturale** (vendi al bid, compri all'ask), poi l'ordine viene cancellato. Con `CHASE_TO_NATURAL=false` si torna agli step fissi da 0.01$. Il chasing usa prima `replace_order_by_id` e, se Alpaca non lo accetta, ripiega su cancel + nuovo ordine.
- **Protezioni sull'esecuzione** (introdotte dopo i trade QQQ e GLD del 23/11/2026, chiusi in forte perdita nei primi minuti di mercato per quote larghissime):
  - **pausa dopo l'apertura:** nei primi `NO_TRADE_AFTER_OPEN_MIN` minuti (15) il bot salva solo l'equity, senza nuovi trade e senza take profit o stop loss; negli ultimi `NO_NEW_TRADES_BEFORE_CLOSE_MIN` minuti (10) non apre nuovi trade;
  - **tetto al chasing:** non paga mai più del mid + `MAX_SLIPPAGE_PCT_OF_WIDTH` (10%) della larghezza dello spread. Per i debit spread il tetto è anche `MAX_DEBIT_PCT_OF_WIDTH` (65%) della larghezza. Se l'ordine non viene eseguito, si riprova al ciclo successivo;
  - **take profit coerente:** l'ordine di chiusura non supera mai il prezzo che realizza la soglia di profitto, quindi un take profit non può diventare una perdita;
  - **quote affidabili:** se attraversare il mercato costa più di `MAX_QUOTE_WIDTH_PCT_OF_WIDTH` (15%) della larghezza, il bot non apre e rimanda take profit e stop loss. Lo stop loss viene rimandato al massimo per 15 controlli di fila;
  - **stop loss confermato:** in sessione scatta solo dopo `STOP_LOSS_CONFIRM_CHECKS` (2) controlli consecutivi;
  - **pausa dopo uno stop:** il ticker non viene ritradato per `STOP_LOSS_COOLDOWN_HOURS` (24) ore;
  - **ripiego gamba per gamba** solo se Alpaca *rifiuta* l'ordine multi-leg, mai per una semplice mancata esecuzione.
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

Apri `.env` e compila almeno `ALPACA_API_KEY` e `ALPACA_SECRET_KEY`. Per il primo test imposta `DRY_RUN=true` (il default è `false`, cioè ordini sull'account paper). Supabase è facoltativo: se non lo configuri, il bot funziona lo stesso e lo segnala nei log.

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
   - la tabella `bot_runs`, con una riga per **ogni** run, anche a mercato chiuso o in errore, per sapere che il bot è vivo;
   - la **Row Level Security** su tutte le tabelle, con la sola policy `"Allow public read"` per il ruolo `anon`, quindi nessun INSERT/UPDATE/DELETE pubblico;
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
3. *(Facoltativo)* Nella tab **Variables** crea `DRY_RUN` con valore `true` o `false`: controlla i run **schedulati**. Se non la crei, i run schedulati usano `DRY_RUN=false`, cioè inviano ordini sull'account paper. Crea `DRY_RUN=true` per metterli in pausa senza fermare la dashboard.
4. **Test manuale:** tab **Actions → Run Options Bot → Run workflow**. Per una prova senza ordini spunta `dry_run` (di default è spento) e premi *Run*. Per provare a mercato chiuso spunta anche `ignore_market_hours`.
5. **Schedulazione in modalità sessione:** i cron di GitHub sono inaffidabili (ritardi anche di ore, slot saltati), quindi il bot non dipende da un cron frequente. Il cron `17 13-20 * * 1-5` parte una volta all'ora e avvia una **sessione continua** che resta attiva fino alla chiusura di Wall Street:
   - **ogni 60 secondi** (`REFRESH_INTERVAL_SEC`): snapshot dell'equity, P&L delle posizioni e controllo di take profit, stop loss e DTE;
   - **ogni 5 minuti** (`TRADE_INTERVAL_MIN`): ciclo completo di screening e nuovi trade;
   - se l'apertura è vicina (meno di 20 minuti), la sessione la aspetta; festivi e orari esatti li decide `get_clock()`;
   - un job GitHub dura al massimo 6 ore, mentre la sessione USA ne dura 6,5. La sessione si chiude quindi da sola dopo 340 minuti e il run orario successivo, già in coda, riprende fino alla chiusura.

   I **run manuali** eseguono un solo ciclo, salvo che tu spunti `session`.

Note su GitHub Actions:

- `concurrency` impedisce che due run si sovrappongano, evitando ordini duplicati. Mentre una sessione è attiva, i run orari successivi restano **in coda** (GitHub ne tiene al massimo uno; gli altri compaiono come *cancelled*, ed è normale). Anche un run manuale lanciato durante una sessione resta in coda finché la sessione non finisce.
- Per un repository pubblico i minuti di GitHub Actions sono gratuiti, quindi la sessione continua non ha costi.
- GitHub **disattiva i workflow schedulati** dei repository pubblici dopo 60 giorni senza attività (commit). Basta riattivarli dalla tab Actions.

---

## 3b. Scheduler Supabase (consigliato: avvio affidabile e gratuito)

Il cron di GitHub ritarda o salta spesso i run: è capitato che in un'intera giornata di mercato non partisse nessuno slot. Lo scheduler su Supabase risolve il problema. Ogni 5 minuti, dal lunedì al venerdì, un job dentro il database controlla se il bot ha dato segni di vita negli ultimi 12 minuti; se non li ha dati, avvia il workflow tramite l'API di GitHub. Durante una sessione il bot scrive uno snapshot al minuto, quindi lo scheduler non fa nulla. Se la sessione si interrompe, la riavvia entro pochi minuti.

È tutto gratuito: pg_cron, pg_net e Vault sono inclusi nel piano Free di Supabase.

**1. Crea un token GitHub limitato a questo repository**
1. GitHub → foto profilo → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** *Only select repositories* → scegli questo repository.
3. **Permissions → Repository permissions → Actions:** *Read and write*. Non servono altri permessi.
4. Scegli una scadenza (per esempio 1 anno), genera il token e copialo.

**2. Installa lo scheduler**
1. Supabase → **Database → Extensions**: cerca **pg_cron** e attivalo, poi fai lo stesso con **pg_net**. Attivali da qui e non da SQL: creare pg_cron via SQL può fallire con "dependent privileges exist".
2. Supabase → **SQL Editor → New query**: incolla tutto [`supabase/scheduler.sql`](supabase/scheduler.sql) e premi **Run**.
3. In una **nuova** query, *non* in un file del repository, salva il token nel Vault cifrato:
   ```sql
   select vault.create_secret('IL_TUO_TOKEN', 'github_dispatch_token');
   ```
4. Prova subito: `select private.dispatch_bot_if_idle();`. Se il bot è fermo risponde "avvio richiesto" e su GitHub → Actions compare un run.

**Verifiche utili** (in fondo a `scheduler.sql` ci sono le query pronte):
- `select * from cron.job_run_details order by start_time desc limit 10;` mostra cosa ha deciso lo scheduler a ogni controllo;
- `select status_code, content from net._http_response order by created desc limit 5;` mostra la risposta di GitHub. `204` significa avviato; `401`, `403` o `404` indicano un token scaduto o senza permessi, oppure un nome del repository sbagliato.

**Quando il token scade** crea un nuovo token e aggiorna il Vault:
`select vault.update_secret(id, 'NUOVO_TOKEN') from vault.secrets where name = 'github_dispatch_token';`

> Il cron di GitHub resta attivo come riserva. Nei giorni festivi o prima dell'apertura lo scheduler avvia qualche run breve che esce subito con `MARKET_CLOSED`: è normale.

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
- il **mercato USA**: aperto o chiuso, con un contatore al secondo fino alla chiusura o alla prossima apertura, gli orari di oggi (ora locale e di New York) e le prossime festività e chiusure anticipate. Gli orari vengono dal clock ufficiale di Alpaca pubblicato dal bot; quando non c'è, dal calendario NYSE calcolato nella pagina;
- il **bot**: in sessione, in attesa o fermo, con il contatore al prossimo ciclo di trading, l'ora di fine della sessione e il tempo rimanente, la cadenza dello scheduler (ogni 5 minuti) e gli intervalli della sessione (`REFRESH_INTERVAL_SEC`, `TRADE_INTERVAL_MIN`, `SESSION_MAX_MINUTES`);
- le posizioni aperte (con P&L non realizzato) e lo storico dei trade. Per ogni trade ci sono il **costo** (premio pagato o incassato = prezzo netto × 100 × contratti, più il costo di chiusura per i trade chiusi) e il **rischio massimo** in $ e in % del capitale. Le eventuali commissioni non sono incluse;
- l'ultimo screening, con il motivo per cui ogni ticker è stato scelto o scartato.
- una **scheda didattica** per ogni strategia (pulsante **i** accanto al nome): cos'è e come è costruita, quando guadagna e quando perde, perché il bot l'ha scelta, quando la chiude (con i prezzi esatti di take profit, stop loss e la data di uscita per scadenza), i rischi e il grafico del profitto a scadenza con i punti di pareggio.

Si aggiorna in tempo reale tramite Supabase Realtime, con un fallback di polling ogni 30 secondi.

> **Contatori del bot:** usano la tabella `bot_status`, che il bot aggiorna a ogni ciclo. Se hai creato il database prima di questa funzione, **riesegui `supabase/schema.sql`**: è idempotente e aggiunge solo la tabella mancante. Finché non lo fai, la dashboard mostra delle stime e il bot lo segnala una sola volta nel log, senza fermarsi.

---

## 5. Passare dal DRY_RUN al paper trading

Quando i log in DRY_RUN ti convincono:

- **in locale:** imposta `DRY_RUN=false` nel `.env`;
- **su GitHub:** imposta la variabile di repository `DRY_RUN` a `false` per i run schedulati, oppure lancia un run manuale togliendo la spunta a `dry_run`.

Gli ordini verranno inviati all'account **paper**. Prima di farlo, controlla nella dashboard di Alpaca (account Paper → *Account* → *Options*) che il livello di trading in opzioni sia **3**, necessario per spread e Iron Condor. Con livello 1–2 imposta `PREFER_SINGLE_LEG_DEBIT=true`; i credit spread verranno comunque rifiutati.

### Parametri configurabili da GitHub (senza toccare il codice)

Vai in **Settings → Secrets and variables → Actions** e crea un **Secret** o una **Variable** con uno di questi nomi. Se esistono entrambi vince il Secret; se non c'è nessuno dei due vale il default.

| Nome | Default | Significato |
|---|---|---|
| `MAX_NEW_TRADES_PER_RUN` | `1` | nuovi trade aperti al massimo per ogni run (1–10) |
| `MAX_OPEN_POSITIONS` | `3` | posizioni aperte al massimo in totale (1–20) |
| `MAX_RISK_PER_TRADE_PCT` | `0.02` | rischio massimo per trade come frazione dell'equity (`0.01` = 1%, massimo `0.10`) |
| `MIN_OPPORTUNITY_SCORE` | `30` | score minimo dello screener per considerare un ticker idoneo (0–100) |
| `WATCHLIST` | `SPY,QQQ,IWM,TLT,GLD,AAPL,MSFT,NVDA,AMZN,AMD` | ticker da analizzare, separati da virgola |
| `PREFER_SINGLE_LEG_DEBIT` | `false` | `true` = Long Call/Put invece dei debit spread |
| `CHASE_TO_NATURAL` | `true` | `false` = chasing a step fissi da 0.01$ invece che fino al prezzo naturale |
| `TAKE_PROFIT_CREDIT_PCT` | `0.50` | chiude una posizione a credito quando il guadagno arriva a questa % del credito incassato (es. `0.30` o `30%`) |
| `STOP_LOSS_CREDIT_PCT` | `1.00` | chiude una posizione a credito quando la perdita arriva a questa % del credito |
| `TAKE_PROFIT_DEBIT_PCT` | `0.40` | chiude una posizione a debito quando il guadagno arriva a questa % del debito pagato |
| `STOP_LOSS_DEBIT_PCT` | `0.50` | chiude una posizione a debito quando la perdita arriva a questa % del debito |
| `NO_TRADE_AFTER_OPEN_MIN` | `15` | minuti dopo l'apertura senza nuovi trade né take profit / stop loss (quote inaffidabili) |
| `NO_NEW_TRADES_BEFORE_CLOSE_MIN` | `10` | minuti prima della chiusura senza nuovi trade |
| `MAX_SLIPPAGE_PCT_OF_WIDTH` | `0.10` | slippage massima rispetto al mid, in frazione della larghezza dello spread (o del premio) |
| `MAX_QUOTE_WIDTH_PCT_OF_WIDTH` | `0.15` | oltre questo costo di attraversamento (naturale − mid) le quote sono considerate inaffidabili |
| `MAX_DEBIT_PCT_OF_WIDTH` | `0.65` | debito massimo di un debit spread rispetto alla sua larghezza |
| `STOP_LOSS_CONFIRM_CHECKS` | `2` | controlli consecutivi necessari per far scattare lo stop loss (in sessione) |
| `STOP_LOSS_COOLDOWN_HOURS` | `24` | ore di pausa su un ticker dopo uno stop loss (`0` = nessuna pausa) |
| `REFRESH_INTERVAL_SEC` | `60` | ogni quanto, in sessione, aggiorna equity e P&L e controlla le soglie (da 30 a 600 s) |
| `TRADE_INTERVAL_MIN` | `5` | ogni quanto, in sessione, fa screening e nuovi trade |

**Esempio: soglie di uscita.** Le soglie valgono per **ogni singola posizione**, in percentuale del credito incassato o del debito pagato. Con un credito di 625 $:
- `TAKE_PROFIT_CREDIT_PCT=0.50` (default) chiude a **+312,50 $**;
- `TAKE_PROFIT_CREDIT_PCT=30%` chiude a **+187,50 $**.

Nella dashboard, sotto il P&L di ogni posizione, vedi la stessa percentuale che il bot confronta con la soglia (es. "+31% del credito"). La regola di uscita a DTE ≤ 7 resta sempre attiva.

> 💡 Per questi valori conviene la tab **Variables**: puoi rileggerli e modificarli quando vuoi. I **Secrets** invece non si possono rileggere, e GitHub nasconde il loro valore (`***`) ovunque compaia nei log: un secret `2` può oscurare ogni "2" del log.

All'inizio di ogni run il log stampa i parametri effettivi (`Parametri: MAX_NEW_TRADES_PER_RUN=...`), così puoi verificare che il valore sia stato letto. Un valore non valido, per esempio `MAX_OPEN_POSITIONS=abc`, ferma il run con un errore chiaro.

In locale gli stessi nomi si impostano nel file `.env`. Tutti gli altri parametri di [`config/settings.py`](config/settings.py) sono sovrascrivibili via variabili d'ambiente.

---

## "Il run è verde ma la dashboard è vuota"

Un run verde significa solo che il bot non è andato in errore, non che abbia operato. Per capire cosa è successo:

1. **Riepilogo del run su GitHub.** Apri il run: in alto trovi un box con l'esito (`OK`, `MARKET_CLOSED`, `AUTH_ERROR`…) e l'avviso **"Supabase non attivo"** se i secret Supabase sono sbagliati.
2. **Card "Ultimo run del bot" nella dashboard.** Mostra l'ultimo run registrato in `bot_runs`. Se la card è vuota ma GitHub mostra run recenti, il bot non riesce a scrivere su Supabase: controlla i secret.
3. **Motivi più comuni:**
   - **mercato chiuso.** Il bot lavora solo dalle 15:30 alle 22:00 ora italiana, dal lunedì al venerdì; fuori orario registra `MARKET_CLOSED` e basta;
   - **`DRY_RUN` attivo.** Se hai creato la variabile di repository `DRY_RUN=true`, o hai spuntato `dry_run` in un run manuale, vedrai snapshot e screening ma **nessun trade**;
   - **nessun candidato idoneo.** Lo vedi nella tabella "Ultimo screening", colonna "Strategia / motivo".

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
