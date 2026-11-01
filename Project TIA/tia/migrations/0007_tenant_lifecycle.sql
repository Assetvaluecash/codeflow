-- Tenant lifecycle (UC1-20): provisioning attributes, suspension, SCM installation mapping, offboarding, per-tenant storage quota.
-- The registry stays invisible to the runtime roles. They get two narrow SECURITY DEFINER functions instead: the caller's OWN
-- status/quota (resolved from the transaction's tenant, so it cannot ask about another tenant) and, for workers only, whether a
-- given tenant may have work claimed (claiming is cross-tenant by design).

alter table tenants drop constraint tenants_status_check;
alter table tenants add constraint tenants_status_check
    check (status in ('active', 'suspended', 'offboarding', 'offboarded'));
alter table tenants
    add column display_name        text,
    add column plan                text not null default 'standard',
    add column storage_quota_bytes bigint check (storage_quota_bytes is null or storage_quota_bytes >= 0),
    add column suspended_at        timestamptz,
    add column suspended_reason    text,
    add column offboarded_at       timestamptz,
    add column deletion_certificate jsonb;

-- Which SCM installation (a GitHub App installation, an Azure DevOps organisation) belongs to which tenant: one owner each.
create table tenant_installations (
    provider        text not null,
    installation_id text not null,
    tenant_id       uuid not null references tenants(id),
    account         text,
    created_at      timestamptz not null default now(),
    primary key (provider, installation_id)
);
create index tenant_installations_tenant on tenant_installations (tenant_id);

-- Lifecycle log: metadata only (who/what/when), no tenant content, so it is kept after offboarding as evidence of the deletion.
-- The column is deliberately not called tenant_id: offboarding deletes every table that has a tenant_id column.
create table tenant_events (
    id             bigserial primary key,
    subject_tenant uuid not null,
    ts             timestamptz not null default now(),
    event          text not null,
    detail         jsonb not null default '{}'
);
create index tenant_events_subject on tenant_events (subject_tenant, id);

create function tia_tenant_state()
returns table (status text, storage_quota_bytes bigint)
language sql stable security definer set search_path = public, pg_temp
as $$ select t.status, t.storage_quota_bytes from tenants t
      where t.id = nullif(current_setting('tia.tenant_id', true), '')::uuid $$;
revoke all on function tia_tenant_state() from public;
grant execute on function tia_tenant_state() to tia_app;

create function tia_tenant_active(t uuid)
returns boolean
language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from tenants x where x.id = t and x.status = 'active') $$;
revoke all on function tia_tenant_active(uuid) from public;
grant execute on function tia_tenant_active(uuid) to tia_worker;
