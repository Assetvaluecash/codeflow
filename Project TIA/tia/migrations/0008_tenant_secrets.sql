-- Per-tenant secret storage (PL-011, TFD 9.1): envelope encryption. Each tenant has its own data-encryption key (DEK), stored only
-- wrapped by a key-encryption key that lives in a secrets manager / KMS; each secret is AES-256-GCM encrypted under the tenant's
-- DEK with the tenant, name and key version bound in as authenticated data, so a ciphertext cannot be moved to another tenant or name.
-- Deleting a tenant's wrapped keys (offboarding does) makes every copy of its secrets, including old backups, unreadable.

create table tenant_keys (
    tenant_id   uuid not null references tenants(id),
    version     integer not null,
    wrapped_dek bytea not null,
    kek_id      text not null,
    created_at  timestamptz not null default now(),
    primary key (tenant_id, version)
);

create table tenant_secrets (
    tenant_id   uuid not null references tenants(id),
    name        text not null,
    key_version integer not null,
    ciphertext  bytea not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    primary key (tenant_id, name)
);

do $$
declare t text;
begin
    foreach t in array array['tenant_keys', 'tenant_secrets'] loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format($p$create policy tenant_isolation on %I
            using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
            with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)$p$, t);
    end loop;
end $$;

grant select, insert, update, delete on tenant_keys, tenant_secrets to tia_app;
