-- Backup/failover store for the marketplace workers (mirror of the Convex tables).
-- Written by workers/supabase_backup.py using the service_role key.
create table public.listings (
  source text not null,
  listing_id text not null,
  url text not null,
  title text not null,
  brand text,
  price_minor integer,
  currency text,
  size text,
  condition text,
  category text,
  published_at timestamptz,
  fetched_at timestamptz not null,
  location text,
  views integer,
  interested integer,
  description text,
  image_url text,
  -- screening result (Python port of convex/lib/screening.ts)
  is_match boolean not null default false,
  reasons text[] not null default '{}',
  score integer not null default 0,
  freshness_bucket text not null check (freshness_bucket in ('HOT','NEW','RECENT','STALE')),
  age_minutes integer not null default 0,
  eligible_for_notify boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (source, listing_id)
);
create index listings_published_at_idx on public.listings (published_at);
create index listings_eligible_idx on public.listings (updated_at desc) where eligible_for_notify;

create table public.notifications (
  idempotency_key text primary key,
  source text not null,
  listing_id text not null,
  status text not null check (status in ('sent','failed')),
  attempts integer not null default 1,
  sent_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.runs (
  id bigint generated always as identity primary key,
  kind text not null,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  scraped integer not null default 0,
  upserted integer not null default 0,
  matched integer not null default 0,
  eligible integer not null default 0,
  sent integer not null default 0,
  failed integer not null default 0,
  skipped integer not null default 0,
  convex_ok boolean,
  fallback_notify boolean not null default false,
  error text
);
create index runs_started_at_idx on public.runs (started_at desc);

-- Only the worker (service_role key, which bypasses RLS) may touch these.
alter table public.listings enable row level security;
alter table public.notifications enable row level security;
alter table public.runs enable row level security;

-- Housekeeping: keep the free-tier database small.
create extension if not exists pg_cron;
select cron.schedule(
  'prune-old-marketplace-data',
  '17 * * * *',
  $$
    delete from public.listings where coalesce(published_at, fetched_at) < now() - interval '5 days';
    delete from public.notifications where created_at < now() - interval '14 days';
    delete from public.runs where started_at < now() - interval '14 days';
  $$
);
