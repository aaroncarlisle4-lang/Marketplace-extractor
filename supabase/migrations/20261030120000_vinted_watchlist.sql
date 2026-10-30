-- Service-role access only; one configured Telegram chat owns this watch list.
create table if not exists public.vinted_bot_state (
    id integer primary key check (id = 1),
    state jsonb not null
);
alter table public.vinted_bot_state enable row level security;
revoke all on public.vinted_bot_state from anon, authenticated;
grant select, insert, update on public.vinted_bot_state to service_role;
