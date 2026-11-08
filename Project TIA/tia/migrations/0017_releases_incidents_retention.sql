-- Release 3 (PRD FR-011, FR-030 to FR-032, 11.4): named releases and manual incident links are recorded evidence; audit retention keeps an
-- anchor (the last purged entry's sequence number and hash) so the remaining chain still verifies after old entries are removed.
alter table evidence_records drop constraint evidence_records_kind_check;
alter table evidence_records add constraint evidence_records_kind_check check (kind in (
    'test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict',
    'approval', 'acknowledgement', 'notification', 'itsm_attachment', 'release', 'incident_link'));

create table audit_anchors (
    tenant_id     uuid primary key references tenants(id),
    seq           bigint not null,             -- the last removed entry
    hash          text not null,               -- its hash: the next entry's prev_hash
    purged_before text not null,               -- entries with ts before this were removed
    created_at    timestamptz not null default now()
);

alter table audit_anchors enable row level security;
alter table audit_anchors force row level security;
create policy tenant_isolation on audit_anchors
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

-- the runtime role reads the anchor to verify the chain; only the operator's retention job (admin role) writes it
grant select on audit_anchors to tia_app;
