-- Identity and access (PL-001, PL-002): per-tenant identity providers (OIDC and SAML 2.0), users, browser sessions, service-account tokens.
-- Every table is tenant-owned with forced row-level security. Two narrow SECURITY DEFINER functions are the only way the request path can
-- discover a tenant from what a caller presents (an issuer, a tenant name) before any tenant is known; they return routing facts, never
-- secrets, and only for active tenants.

create table tenant_idps (
    tenant_id         uuid not null references tenants(id),
    id                uuid not null default gen_random_uuid(),
    name              text not null,
    protocol          text not null check (protocol in ('oidc', 'saml')),
    enabled           boolean not null default true,
    config            jsonb not null,                      -- non-secret settings: issuer, audience, endpoints, claim names, role map, certificates
    client_secret_ref text,                                -- NAME of a secret in the tenant's encrypted store (never the secret itself)
    created_by        text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    primary key (tenant_id, id),
    unique (tenant_id, name)
);
-- A bearer token is routed to a tenant by (issuer, audience); two tenants may not claim the same pair.
create unique index tenant_idps_issuer_audience on tenant_idps ((config ->> 'issuer'), (config ->> 'audience')) where protocol = 'oidc' and enabled;

create table tenant_users (
    tenant_id    uuid not null references tenants(id),
    id           uuid not null default gen_random_uuid(),
    idp_id       uuid,
    subject      text not null,
    email        text,
    display_name text,
    roles        text[],                                    -- an administrator's explicit assignment; null = derive from the identity provider
    scm_logins   jsonb not null default '{}'::jsonb,        -- {"github": "login", ...}: how this person appears in the source-code hosts
    disabled     boolean not null default false,
    first_seen   timestamptz not null default now(),
    last_login   timestamptz,
    primary key (tenant_id, id)
);
create unique index tenant_users_subject on tenant_users (tenant_id, coalesce(idp_id, '00000000-0000-0000-0000-000000000000'::uuid), subject);

create table auth_sessions (
    tenant_id    uuid not null references tenants(id),
    id           uuid not null default gen_random_uuid(),
    secret_hash  text not null,                             -- sha256 of the cookie secret; the secret itself is never stored
    user_id      uuid not null,
    idp_id       uuid,
    roles        text[] not null,
    groups       jsonb not null default '[]'::jsonb,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null,
    last_seen_at timestamptz not null default now(),
    revoked_at   timestamptz,
    primary key (tenant_id, id)
);
create index auth_sessions_user on auth_sessions (tenant_id, user_id);

create table auth_login_states (
    tenant_id     uuid not null references tenants(id),
    state_hash    text not null,
    idp_id        uuid not null,
    nonce         text,
    code_verifier text,                                    -- PKCE
    browser_hash  text,                                    -- sha256 of a cookie set at /auth/login: the callback must come from the same browser
    return_to     text,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    primary key (tenant_id, state_hash)
);

create table saml_assertions_seen (                         -- replay protection: an assertion is accepted once
    tenant_id    uuid not null references tenants(id),
    assertion_id text not null,
    expires_at   timestamptz not null,
    primary key (tenant_id, assertion_id)
);

create table service_tokens (
    tenant_id    uuid not null references tenants(id),
    id           uuid not null default gen_random_uuid(),
    name         text not null,
    roles        text[] not null,
    repos        jsonb not null default '"*"'::jsonb,       -- "*" or a list of repository names this token may read
    secret_hash  text not null,
    created_by   text,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz,
    revoked_at   timestamptz,
    last_used_at timestamptz,
    primary key (tenant_id, id),
    unique (tenant_id, name)
);

do $$
declare t text;
begin
    foreach t in array array['tenant_idps', 'tenant_users', 'auth_sessions', 'auth_login_states', 'saml_assertions_seen', 'service_tokens'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format($p$create policy tenant_isolation on %I
            using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)$p$, t);
    end loop;
end $$;

grant select, insert, update, delete on tenant_idps, tenant_users, auth_sessions, auth_login_states, saml_assertions_seen, service_tokens to tia_app;

-- Which tenant does a bearer token's (issuer, audience) belong to? Routing facts only; active tenants only.
create function tia_idp_by_issuer(iss text, aud text)
returns table (tenant_id uuid, idp_id uuid)
language sql stable security definer set search_path = public, pg_temp
as $$ select i.tenant_id, i.id from tenant_idps i join tenants t on t.id = i.tenant_id
      where i.protocol = 'oidc' and i.enabled and i.config ->> 'issuer' = iss and i.config ->> 'audience' = aud and t.status = 'active' $$;

-- Sign-in by organisation name: the tenant and the ids/names/protocols of its enabled identity providers.
create function tia_login_options(tname text)
returns table (tenant_id uuid, idp_id uuid, idp_name text, protocol text)
language sql stable security definer set search_path = public, pg_temp
as $$ select t.id, i.id, i.name, i.protocol from tenants t join tenant_idps i on i.tenant_id = t.id
      where t.name = tname and t.status = 'active' and i.enabled order by i.name $$;

revoke all on function tia_idp_by_issuer(text, text) from public;
revoke all on function tia_login_options(text) from public;
grant execute on function tia_idp_by_issuer(text, text), tia_login_options(text) to tia_app;
