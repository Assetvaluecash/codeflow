-- Operator-set tenant settings (PRD PL-014 plan limits, PL-015 data residency, PL-016 customer-managed keys). They live in the tenant
-- registry, which the runtime cannot read; tia_tenant_settings() returns the CURRENT tenant's own settings only.
alter table tenants add column plan_limits jsonb not null default '{}'::jsonb;          -- the plan name stays in tenants.plan
alter table tenants add column region text check (region is null or region ~ '^[a-z]{2,8}$');
alter table tenants add column key_ref text;

create function tia_tenant_settings()
returns table (plan jsonb, region text, key_ref text)
language sql stable security definer set search_path = public, pg_temp
as $$ select t.plan_limits, t.region, t.key_ref from tenants t
      where t.id = nullif(current_setting('tia.tenant_id', true), '')::uuid $$;
revoke all on function tia_tenant_settings() from public;
grant execute on function tia_tenant_settings() to tia_app;

-- the worker claims only jobs of tenants it may serve: unpinned, or pinned to the worker's own region
create function tia_tenant_in_region(t uuid, here text)
returns boolean
language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from tenants x where x.id = t and (x.region is null or x.region = here)) $$;
revoke all on function tia_tenant_in_region(uuid, text) from public;
grant execute on function tia_tenant_in_region(uuid, text) to tia_worker;
