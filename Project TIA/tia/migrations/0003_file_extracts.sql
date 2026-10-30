-- Per-file extraction cache (UC1-11, AC-021): facts are keyed by git blob id, so a push that changes one file
-- re-extracts only that file. Content-addressed; rows are only rewritten by an explicit full rescan (repair).
create table file_extracts (
    tenant_id   uuid not null references tenants(id),
    key         text not null,
    data        jsonb not null,
    created_at  timestamptz not null default now(),
    primary key (tenant_id, key)
);

alter table file_extracts enable row level security;
alter table file_extracts force row level security;
create policy tenant_isolation on file_extracts
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert, update, delete on file_extracts to tia_app;
