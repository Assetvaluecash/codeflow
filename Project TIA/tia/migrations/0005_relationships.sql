-- Relationship history (UC1-12, AC-011): every cross-repository relationship TIA has confirmed, with its current
-- state. 'current' = confirmed at the repositories' latest analysed revisions; 'stale' = no longer confirmed, with
-- the reason (scan_failed | parse_failed | evidence_removed). A stale relationship is never presented as current.
create table relationships (
    tenant_id   uuid not null references tenants(id),
    key         text not null,
    state       text not null check (state in ('current', 'stale')),
    data        jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (tenant_id, key)
);
create index relationships_state on relationships (tenant_id, state);

alter table relationships enable row level security;
alter table relationships force row level security;
create policy tenant_isolation on relationships
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert, update, delete on relationships to tia_app;
