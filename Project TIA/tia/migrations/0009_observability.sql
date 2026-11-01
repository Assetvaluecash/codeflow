-- Observability (UC1-22): what readiness needs to know that the runtime roles cannot read directly.
--  * tia_schema_version(): the newest applied migration, so a process can refuse to call itself ready when its schema is behind its code.
--  * worker_heartbeats: workers announce themselves, so the API can say whether anything is consuming the queue. Not tenant data (no
--    tenant_id): worker ids and timestamps only.

create function tia_schema_version()
returns text
language sql stable security definer set search_path = public, pg_temp
as $$ select max(version) from schema_migrations $$;
revoke all on function tia_schema_version() from public;
grant execute on function tia_schema_version() to tia_app;

create table worker_heartbeats (
    worker_id  text primary key,
    started_at timestamptz not null default now(),
    last_seen  timestamptz not null default now(),
    info       jsonb not null default '{}'
);
grant select on worker_heartbeats to tia_app;
grant insert, update, delete on worker_heartbeats to tia_worker;
