-- =====================================================================
-- Alpaca Options Bot - Schema Supabase (PostgreSQL)
-- Incolla l'intero file nel SQL Editor di Supabase ed esegui ("Run").
-- Lo script è idempotente: può essere rieseguito senza errori.
--
-- Sicurezza:
--   * RLS attiva su tutte le tabelle.
--   * Ruolo `anon` (chiave pubblica della dashboard): SOLO lettura.
--   * Nessuna policy INSERT/UPDATE/DELETE per `anon`: scrive solo il bot
--     con la SERVICE ROLE key (che bypassa la RLS).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) equity_snapshots: una riga per ogni run del bot
-- ---------------------------------------------------------------------
create table if not exists public.equity_snapshots (
    id                bigserial primary key,
    "timestamp"       timestamptz not null default now(),
    equity            numeric(14, 2) not null,
    cash              numeric(14, 2) not null,
    initial_capital   numeric(14, 2) not null,
    total_return_usd  numeric(14, 2) not null,
    total_return_pct  numeric(10, 4) not null,
    daily_pnl         numeric(14, 2) not null default 0
);

create index if not exists idx_equity_snapshots_ts on public.equity_snapshots ("timestamp" desc);

-- ---------------------------------------------------------------------
-- 2) trades: un record per strategia (tutte le gambe in legs_json)
--    entry_price / exit_price = prezzo NETTO per unità dell'ordine,
--    convenzione Alpaca: > 0 debito pagato, < 0 credito incassato.
--    pnl = P&L realizzato in $ = -(entry_price + exit_price) * 100 * qty
-- ---------------------------------------------------------------------
create table if not exists public.trades (
    id               bigserial primary key,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    closed_at        timestamptz,
    ticker           text not null,
    strategy         text not null,
    status           text not null default 'OPEN' check (status in ('OPEN', 'CLOSED')),
    entry_price      numeric(12, 4) not null,
    exit_price       numeric(12, 4),
    qty              integer not null default 1,
    pnl              numeric(14, 2),
    unrealized_pnl   numeric(14, 2),
    dte              integer,
    close_reason     text,
    legs_json        jsonb not null default '{}'::jsonb,
    alpaca_order_id  text
);

create index if not exists idx_trades_status on public.trades (status);
create index if not exists idx_trades_created_at on public.trades (created_at desc);
create index if not exists idx_trades_ticker on public.trades (ticker);

-- ---------------------------------------------------------------------
-- 3) screening_logs: una riga per ticker per ogni run (run_id comune)
-- ---------------------------------------------------------------------
create table if not exists public.screening_logs (
    id                 bigserial primary key,
    "timestamp"        timestamptz not null default now(),
    run_id             text,
    ticker             text not null,
    trend              text,
    volatility_regime  text,
    iv_rank            numeric(8, 2),
    adx                numeric(8, 2),
    avg_spread_pct     numeric(10, 4),
    opportunity_score  numeric(8, 2),
    selected           boolean not null default false,
    strategy           text,
    reason             text
);

create index if not exists idx_screening_logs_ts on public.screening_logs ("timestamp" desc);
create index if not exists idx_screening_logs_run on public.screening_logs (run_id);

-- ---------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------
alter table public.equity_snapshots enable row level security;
alter table public.trades           enable row level security;
alter table public.screening_logs   enable row level security;

drop policy if exists "Allow public read" on public.equity_snapshots;
create policy "Allow public read" on public.equity_snapshots for select to anon using (true);

drop policy if exists "Allow public read" on public.trades;
create policy "Allow public read" on public.trades for select to anon using (true);

drop policy if exists "Allow public read" on public.screening_logs;
create policy "Allow public read" on public.screening_logs for select to anon using (true);

-- Privilegi espliciti (alcuni progetti Supabase recenti non espongono
-- automaticamente le nuove tabelle alla Data API).
grant usage on schema public to anon, service_role;
grant select on public.equity_snapshots, public.trades, public.screening_logs to anon;
revoke insert, update, delete, truncate on public.equity_snapshots, public.trades, public.screening_logs from anon;
grant all on public.equity_snapshots, public.trades, public.screening_logs to service_role;
grant usage, select on all sequences in schema public to service_role;

-- ---------------------------------------------------------------------
-- Realtime (opzionale): la dashboard si aggiorna appena il bot scrive.
-- La RLS si applica anche al realtime: anon riceve solo ciò che può leggere.
-- ---------------------------------------------------------------------
do $$
declare
    t text;
begin
    if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        foreach t in array array['equity_snapshots', 'trades', 'screening_logs'] loop
            if not exists (
                select 1 from pg_publication_tables
                where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = t
            ) then
                execute format('alter publication supabase_realtime add table public.%I', t);
            end if;
        end loop;
    end if;
end $$;
