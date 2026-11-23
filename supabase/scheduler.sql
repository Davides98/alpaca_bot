-- =====================================================================
-- Scheduler affidabile e gratuito: Supabase (pg_cron + pg_net) avvia il bot su GitHub Actions.
--
-- Perché: il cron di GitHub ritarda o salta spesso i run. pg_cron gira dentro il database Supabase
-- ed è puntuale. Ogni 5 minuti (lun-ven, 13:00-20:59 UTC) controlla se il bot ha dato segni di vita
-- negli ultimi 12 minuti (snapshot equity o riga in bot_runs). Se no, avvia il workflow via API GitHub.
-- Durante una sessione il bot scrive uno snapshot al minuto, quindi lo scheduler non fa nulla.
--
-- PREREQUISITI
--   1. Aver eseguito supabase/schema.sql (tabelle equity_snapshots e bot_runs).
--   2. Un token GitHub "fine-grained" (vedi README, sezione "Scheduler Supabase"):
--      Repository access: solo questo repository | Permissions -> Actions: Read and write.
--
-- COME SI USA
--   1. Incolla tutto questo file nel SQL Editor di Supabase e premi Run.
--   2. In una NUOVA query del SQL Editor (non in questo file!) salva il token nel Vault cifrato:
--        select vault.create_secret('IL_TUO_TOKEN', 'github_dispatch_token');
--      Per sostituirlo in futuro:
--        select vault.update_secret(id, 'NUOVO_TOKEN') from vault.secrets where name = 'github_dispatch_token';
--   NON scrivere mai il token in un file del repository: il repo è pubblico.
--   Rieseguire questo file è sicuro: aggiorna funzione e job senza duplicarli.
-- =====================================================================

-- Estensioni (incluse nel piano gratuito; sintassi dalla documentazione Supabase)
create extension if not exists pg_cron with schema pg_catalog;
grant usage on schema cron to postgres;
grant all privileges on all tables in schema cron to postgres;
create extension if not exists pg_net with schema extensions;

-- ---------------------------------------------------------------------
-- A) Funzione "guardiano" in uno schema NON esposto dall'API pubblica
-- ---------------------------------------------------------------------
create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create or replace function private.dispatch_bot_if_idle()
returns text
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
    c_repo        constant text := 'Davides98/alpaca_bot';   -- <proprietario>/<repository>
    c_workflow    constant text := 'run_bot.yml';
    c_branch      constant text := 'main';
    c_idle        constant interval := interval '12 minutes';
    v_last        timestamptz;
    v_token       text;
    v_request_id  bigint;
begin
    select greatest(
        coalesce((select max("timestamp") from public.equity_snapshots), 'epoch'::timestamptz),
        coalesce((select max("timestamp") from public.bot_runs), 'epoch'::timestamptz)
    ) into v_last;

    if v_last > now() - c_idle then
        return format('bot attivo (ultimo segnale %s): nessuna azione', v_last);
    end if;

    select decrypted_secret into v_token from vault.decrypted_secrets where name = 'github_dispatch_token';
    if v_token is null then
        raise warning 'Token GitHub mancante nel Vault (github_dispatch_token)';
        return 'token mancante';
    end if;

    select net.http_post(
        url     := format('https://api.github.com/repos/%s/actions/workflows/%s/dispatches', c_repo, c_workflow),
        body    := jsonb_build_object('ref', c_branch, 'inputs', jsonb_build_object('scheduler', 'true')),
        headers := jsonb_build_object(
            'Authorization', 'Bearer ' || v_token,
            'Accept', 'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'User-Agent', 'alpaca-bot-supabase-scheduler',
            'Content-Type', 'application/json'
        ),
        timeout_milliseconds := 10000
    ) into v_request_id;

    return format('bot inattivo da %s: avvio richiesto (pg_net request %s)', v_last, v_request_id);
end;
$$;

revoke all on function private.dispatch_bot_if_idle() from public, anon, authenticated;

-- ---------------------------------------------------------------------
-- B) Job pg_cron: ogni 5 minuti, lun-ven, 13:00-20:59 UTC
--    (copre l'apertura di Wall Street con ora solare 14:30 UTC e legale 13:30 UTC;
--     festivi e orari esatti li gestisce il bot, che esce subito se il mercato è chiuso)
-- ---------------------------------------------------------------------
select cron.unschedule(jobid) from cron.job where jobname = 'alpaca-bot-watchdog';
select cron.schedule('alpaca-bot-watchdog', '*/5 13-20 * * 1-5', $$select private.dispatch_bot_if_idle()$$);

-- ---------------------------------------------------------------------
-- VERIFICHE (eseguile quando vuoi, una alla volta)
-- ---------------------------------------------------------------------
-- Job registrato:
--   select jobid, jobname, schedule, active from cron.job;
-- Ultime esecuzioni del job (esito della funzione):
--   select start_time, status, return_message from cron.job_run_details order by start_time desc limit 10;
-- Risposte di GitHub (204 = workflow avviato; 401/403/404 = problema col token o col nome del repo):
--   select created, status_code, left(content::text, 200) from net._http_response order by created desc limit 10;
-- Prova manuale immediata (avvia il bot se è inattivo):
--   select private.dispatch_bot_if_idle();
-- Disattivare lo scheduler:
--   select cron.unschedule('alpaca-bot-watchdog');
