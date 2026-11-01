-- Per-tenant configuration, versioned (UC1-24). Insert-only: a change is a new version, history is never rewritten, and the runtime role
-- cannot update or delete a version (offboarding removes them through the admin role).

create table tenant_config_versions (
    tenant_id   uuid not null references tenants(id),
    version     integer not null,
    config      jsonb not null,
    config_hash text not null,
    comment     text,
    actor       text,
    created_at  timestamptz not null default now(),
    primary key (tenant_id, version)
);

alter table tenant_config_versions enable row level security;
alter table tenant_config_versions force row level security;
create policy tenant_isolation on tenant_config_versions
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert on tenant_config_versions to tia_app;

-- The worker claims work across tenants, so it needs each tenant's concurrency limit without being able to read their configuration.
create function tia_tenant_job_cap(t uuid)
returns integer
language sql stable security definer set search_path = public, pg_temp
as $$ select (config -> 'limits' ->> 'max_concurrent_jobs')::integer
      from tenant_config_versions where tenant_id = t order by version desc limit 1 $$;
revoke all on function tia_tenant_job_cap(uuid) from public;
grant execute on function tia_tenant_job_cap(uuid) to tia_worker;
