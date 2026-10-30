-- Persisted, versioned dependency graph (UC1-11). A version is immutable and identified by its inputs
-- (repository -> commit, extractor and configuration versions); the head table says which version is current.
create table graph_versions (
    tenant_id   uuid not null references tenants(id),
    id          text not null,
    created_at  timestamptz not null default now(),
    inputs      jsonb not null,
    stats       jsonb not null,
    data        jsonb not null,
    primary key (tenant_id, id)
);

create table graph_heads (
    tenant_id   uuid not null references tenants(id),
    scope       text not null,                  -- 'landscape' = all registered repositories at their default branches
    graph_id    text not null,
    previous_id text,
    updated_at  timestamptz not null default now(),
    primary key (tenant_id, scope)
);

do $$
declare t text;
begin
    foreach t in array array['graph_versions', 'graph_heads'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format($p$create policy tenant_isolation on %I
            using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)$p$, t);
    end loop;
end $$;

grant select, insert, update, delete on graph_versions to tia_app;
grant select, insert, update, delete on graph_heads to tia_app;
