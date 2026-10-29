-- Job queue on PostgreSQL (UC1-03): idempotent submission, retries with backoff, leases, supersede,
-- per-tenant fair scheduling. Rows are tenant-isolated by RLS exactly like every other tenant table.
-- Worker processes connect as `tia_worker`, whose membership is the only thing that lets a session see
-- jobs across tenants (to claim them); every tenant-data read/write the worker then does is tenant-bound.

create sequence job_group_seq;

create table jobs (
    tenant_id         uuid not null references tenants(id),
    id                uuid not null default gen_random_uuid(),
    kind              text not null,
    key               text not null,                      -- idempotency key (same key -> same job)
    group_key         text,                               -- e.g. one pull request: newest submission wins
    group_seq         bigint not null default nextval('job_group_seq'),
    payload           jsonb not null,
    status            text not null default 'queued'
                      check (status in ('queued', 'running', 'succeeded', 'failed', 'superseded', 'cancelled')),
    attempts          int not null default 0,
    max_attempts      int not null default 3,
    run_after         timestamptz not null default now(),
    locked_by         text,
    locked_until      timestamptz,
    cancel_requested  boolean not null default false,
    result            jsonb,
    error             text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    primary key (tenant_id, id),
    unique (tenant_id, key)
);

create index jobs_claim on jobs (status, run_after);
create index jobs_group on jobs (tenant_id, group_key, group_seq);

alter table jobs enable row level security;
alter table jobs force row level security;
create policy tenant_isolation on jobs
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid
           or pg_has_role(current_user, 'tia_worker', 'member'))
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid
                or pg_has_role(current_user, 'tia_worker', 'member'));

grant select, insert, update on jobs to tia_app;
grant usage on sequence job_group_seq to tia_app;
