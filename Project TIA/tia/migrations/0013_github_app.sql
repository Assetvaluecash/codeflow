-- GitHub App installations (UC1-04). tenant_installations is admin-only (it maps across tenants); the request path reaches it through
-- four narrow SECURITY DEFINER functions, each of which can only do one thing:
--   tia_installation_tenant   which ACTIVE tenant owns an installation (routes a webhook; says nothing else)
--   tia_my_installations      the current tenant's own installations
--   tia_map_installation      claim a free installation for the current tenant (never takes one that belongs to someone else)
--   tia_unmap_installation    release one of the current tenant's own installations
-- Mapping and unmapping are recorded in tenant_events (metadata only), like every other lifecycle change.

create function tia_installation_tenant(prov text, inst text)
returns uuid
language sql stable security definer set search_path = public, pg_temp
as $$ select i.tenant_id from tenant_installations i join tenants t on t.id = i.tenant_id
      where i.provider = prov and i.installation_id = inst and t.status = 'active' $$;

create function tia_my_installations(prov text)
returns table (installation_id text, account text)
language sql stable security definer set search_path = public, pg_temp
as $$ select installation_id, account from tenant_installations
      where provider = prov and tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid order by installation_id $$;

create function tia_map_installation(prov text, inst text, acct text)
returns text
language plpgsql volatile security definer set search_path = public, pg_temp
as $$
declare me uuid := nullif(current_setting('tia.tenant_id', true), '')::uuid; owner uuid;
begin
    if me is null then return 'no_tenant'; end if;
    insert into tenant_installations (provider, installation_id, tenant_id, account) values (prov, inst, me, acct)
        on conflict (provider, installation_id) do nothing;
    select tenant_id into owner from tenant_installations where provider = prov and installation_id = inst;
    if owner = me then
        insert into tenant_events (subject_tenant, event, detail) values (me, 'installation_mapped', jsonb_build_object('provider', prov, 'installation_id', inst, 'account', acct, 'via', 'api'));
        return 'mapped';
    end if;
    return 'taken';                                          -- deliberately says nothing about who owns it
end $$;

create function tia_unmap_installation(prov text, inst text)
returns boolean
language plpgsql volatile security definer set search_path = public, pg_temp
as $$
declare me uuid := nullif(current_setting('tia.tenant_id', true), '')::uuid; n integer;
begin
    delete from tenant_installations where provider = prov and installation_id = inst and tenant_id = me;
    get diagnostics n = row_count;
    if n > 0 then
        insert into tenant_events (subject_tenant, event, detail) values (me, 'installation_unmapped', jsonb_build_object('provider', prov, 'installation_id', inst, 'via', 'api'));
    end if;
    return n > 0;
end $$;

revoke all on function tia_installation_tenant(text, text), tia_my_installations(text), tia_map_installation(text, text, text),
                       tia_unmap_installation(text, text) from public;
grant execute on function tia_installation_tenant(text, text), tia_my_installations(text), tia_map_installation(text, text, text),
                          tia_unmap_installation(text, text) to tia_app;
