-- Ingested evidence (PRD FR-022 to FR-029, FR-031, FR-044): CI test results, coverage, runtime dependencies observed in telemetry,
-- deployments, incidents and reviewer verdicts. Evidence is ingested BEFORE an analysis and read from here; an analysis never queries an
-- external system live, and it records the ids of the records it used in its evidence snapshot so a reproduction reads the same records.
--   kind         test_run | coverage | runtime_edge | deployment | incident | verdict
--   digest       content hash: the same evidence ingested twice is one record
--   observed_*   the time window the evidence describes (a run, a trace window, a deployment instant, an incident)

create table evidence_records (
    tenant_id     uuid not null references tenants(id),
    id            bigint generated always as identity,
    kind          text not null check (kind in ('test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict')),
    source        text not null,
    repo          text,
    service       text,
    environment   text,
    revision      text,
    observed_from timestamptz,
    observed_to   timestamptz,
    data          jsonb not null,
    digest        text not null,
    ingested_by   text,
    ingested_at   timestamptz not null default now(),
    primary key (tenant_id, id),
    unique (tenant_id, kind, digest)
);
create index evidence_by_repo on evidence_records (tenant_id, kind, repo, observed_to desc);
create index evidence_by_service on evidence_records (tenant_id, kind, service, environment, observed_to desc);

alter table evidence_records enable row level security;
alter table evidence_records force row level security;
create policy tenant_isolation on evidence_records
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert, update, delete on evidence_records to tia_app;
