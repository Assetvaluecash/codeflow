-- TIA schema v1: multi-tenant, shared schema, tenant_id on every tenant-owned row, FORCED row-level security.
-- Runs as the owner/admin role. The runtime role `tia_app` is created by tia.db.migrate (NOBYPASSRLS, not an owner).

create table tenants (
    id          uuid primary key default gen_random_uuid(),
    name        text not null unique,
    status      text not null default 'active' check (status in ('active', 'suspended', 'offboarded')),
    created_at  timestamptz not null default now()
);

create table repositories (
    tenant_id   uuid not null references tenants(id),
    name        text not null,
    data        jsonb not null,
    primary key (tenant_id, name)
);

create table extractions (
    tenant_id   uuid not null references tenants(id),
    key         text not null,
    data        jsonb not null,
    created_at  timestamptz not null default now(),
    primary key (tenant_id, key)
);

create table analyses (
    tenant_id   uuid not null references tenants(id),
    id          text not null,
    created_at  timestamptz not null default now(),
    data        jsonb not null,
    primary key (tenant_id, id)
);

-- Append-only, hash-chained per tenant. payload is stored as text so the hash can be recomputed exactly.
create table audit_log (
    tenant_id   uuid not null references tenants(id),
    seq         bigint not null,
    ts          text not null,
    event       text not null,
    payload     text not null,
    prev_hash   text not null,
    hash        text not null,
    primary key (tenant_id, seq)
);

do $$
declare t text;
begin
    foreach t in array array['repositories', 'extractions', 'analyses', 'audit_log'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format($p$create policy tenant_isolation on %I
            using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)$p$, t);
    end loop;
end $$;

-- Runtime role: no access to the tenant registry; audit_log is insert/select only (append-only).
grant usage on schema public to tia_app;
grant select, insert, update, delete on repositories, extractions, analyses to tia_app;
grant select, insert on audit_log to tia_app;
