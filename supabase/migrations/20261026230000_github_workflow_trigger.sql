-- Reliable scheduler: GitHub's own cron drops most */5 runs and disables
-- scheduled workflows after 60 days without commits, so Supabase dispatches
-- the Vinted workflow instead. Token lives in Vault as 'github_actions_token'
-- (fine-grained PAT, this repo only, Actions: read & write):
--   select vault.create_secret('<token>', 'github_actions_token');
create extension if not exists pg_net;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table if not exists private.workflow_dispatches (
  id bigint generated always as identity primary key,
  workflow text not null,
  requested_at timestamptz not null default now(),
  request_id bigint,
  skipped_reason text
);

create or replace function private.dispatch_github_workflow(
  workflow_file text default 'vinted-cycle.yml',
  force boolean default false
) returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  token text;
  uk_hour int := extract(hour from (now() at time zone 'Europe/London'));
  req_id bigint;
begin
  -- Same window the workflow used: 08:00-21:59 UK time (handles BST/GMT).
  if not force and (uk_hour < 8 or uk_hour >= 22) then
    return null;
  end if;

  select decrypted_secret into token
  from vault.decrypted_secrets
  where name = 'github_actions_token'
  limit 1;

  if token is null then
    insert into private.workflow_dispatches (workflow, skipped_reason)
    values (workflow_file, 'missing vault secret github_actions_token');
    return null;
  end if;

  select net.http_post(
    url := 'https://api.github.com/repos/aaroncarlisle4-lang/Marketplace-extractor/actions/workflows/'
           || workflow_file || '/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || token,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'supabase-pg-cron',
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('ref', 'main')
  ) into req_id;

  insert into private.workflow_dispatches (workflow, request_id)
  values (workflow_file, req_id);
  return req_id;
end;
$$;

revoke all on function private.dispatch_github_workflow(text, boolean) from public, anon, authenticated;

-- Recent dispatches with GitHub's response (204 = accepted).
create or replace view private.workflow_dispatch_status as
select d.requested_at, d.workflow, d.skipped_reason, r.status_code, left(r.content, 300) as response
from private.workflow_dispatches d
left join net._http_response r on r.id = d.request_id
order by d.requested_at desc;

select cron.schedule(
  'dispatch-vinted-cycle',
  '*/5 * * * *',
  $$ select private.dispatch_github_workflow('vinted-cycle.yml'); $$
);

select cron.schedule(
  'prune-workflow-dispatches',
  '23 3 * * *',
  $$ delete from private.workflow_dispatches where requested_at < now() - interval '7 days'; $$
);
