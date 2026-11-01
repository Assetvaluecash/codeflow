-- Per-repository access (PL-002, AC-016): who may read which repository, mirrored from the source-code host.
--   repo_access      how each repository is governed: 'scm' (readers are the people the source-code host says may read it),
--                    'restricted' (explicit grants only), 'tenant' (every member of the tenant)
--   repo_grants      explicit grants for 'restricted' repositories
--   scm_permissions  the mirror: who the host says can read a repository, with the time it was last confirmed
--   scm_sync_status  whether the last synchronisation of a repository worked
-- A repository with no repo_access row is governed by its provider: hosted repositories default to 'scm' (fail closed), uploads and local
-- paths to 'tenant'. A mirror older than the freshness limit grants nothing.

create table repo_access (
    tenant_id  uuid not null references tenants(id),
    repo       text not null,
    mode       text not null check (mode in ('scm', 'tenant', 'restricted')),
    updated_by text,
    updated_at timestamptz not null default now(),
    primary key (tenant_id, repo)
);

create table repo_grants (
    tenant_id  uuid not null references tenants(id),
    repo       text not null,
    user_id    uuid not null,
    granted_by text,
    created_at timestamptz not null default now(),
    primary key (tenant_id, repo, user_id)
);

create table scm_permissions (
    tenant_id  uuid not null references tenants(id),
    host       text not null,
    repo       text not null,
    login      text not null,                              -- lower-case
    permission text not null,                              -- pull | triage | push | maintain | admin
    synced_at  timestamptz not null default now(),
    primary key (tenant_id, host, repo, login)
);

create table scm_sync_status (
    tenant_id     uuid not null references tenants(id),
    host          text not null,
    repo          text not null,
    synced_at     timestamptz,
    ok            boolean not null,
    error         text,
    collaborators integer not null default 0,
    private       boolean,                                 -- as reported by the host; unknown is treated as public (fail closed)
    attempted_at  timestamptz not null default now(),
    primary key (tenant_id, host, repo)
);

do $$
declare t text;
begin
    foreach t in array array['repo_access', 'repo_grants', 'scm_permissions', 'scm_sync_status'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format($p$create policy tenant_isolation on %I
            using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)$p$, t);
    end loop;
end $$;

grant select, insert, update, delete on repo_access, repo_grants, scm_permissions, scm_sync_status to tia_app;
