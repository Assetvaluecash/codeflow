-- Materialised evidence snapshots (UC1-13, AC-015): the complete, pinned input of an analysis - revisions of every
-- repository, extractor and configuration versions, graph version, freshness and stale-relationship state at analysis
-- time. Immutable by privilege: the runtime role can insert and read, never update or delete.
create table evidence_snapshots (
    tenant_id   uuid not null references tenants(id),
    id          text not null,
    created_at  timestamptz not null default now(),
    data        jsonb not null,
    primary key (tenant_id, id)
);

alter table evidence_snapshots enable row level security;
alter table evidence_snapshots force row level security;
create policy tenant_isolation on evidence_snapshots
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert on evidence_snapshots to tia_app;
