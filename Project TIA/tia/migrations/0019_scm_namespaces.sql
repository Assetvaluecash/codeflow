-- PRD PL-003 for GitLab and Bitbucket (and Azure DevOps): a webhook is routed to the tenant that owns its namespace (GitLab top-level group,
-- Bitbucket workspace or Data Center project), mapped by the platform operator in tenant_installations like a GitHub App installation.
-- Once a provider has any mapping, an event from an unmapped namespace must be ignored rather than land in the default tenant: this
-- function tells the request path whether a provider is mapped at all, and nothing about which tenants own what.
create function tia_provider_mapped(prov text)
returns boolean
language sql stable security definer set search_path = public, pg_temp
as $$ select exists (select 1 from tenant_installations where provider = prov) $$;

revoke all on function tia_provider_mapped(text) from public;
grant execute on function tia_provider_mapped(text) to tia_app;
