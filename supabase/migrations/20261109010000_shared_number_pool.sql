-- Seats share one number pool (D26, section 9C). The relay picks the caller ID for each dial, counts each number's
-- daily use across every seat, and sends a callback to the seat that last called that prospect.
-- Still no carrier secrets, audio, transcripts or lead details: a prospect is kept only as its phone number.
create table public.relay_pool (
 phone text primary key check (phone ~ '^\+1[2-9][0-9]{9}$'),
 area_code text not null check (area_code ~ '^[2-9][0-9]{2}$'),
 daily_cap integer not null default 50 check (daily_cap between 1 and 200),
 active boolean not null default true
);
create table public.relay_pool_use (
 phone text not null references public.relay_pool(phone) on delete cascade,
 day date not null,
 attempts integer not null default 0,
 primary key (phone, day)
);
create table public.relay_pool_leases (
 phone text primary key references public.relay_pool(phone) on delete cascade,
 workspace_id uuid not null references public.relay_workspaces(id),
 call_ref text not null,
 lease_until timestamptz not null
);
create unique index relay_pool_leases_call on public.relay_pool_leases(workspace_id, call_ref);
create table public.relay_last_caller (
 prospect text primary key,
 workspace_id uuid not null references public.relay_workspaces(id),
 pool_phone text,
 called_at timestamptz not null
);
create table public.relay_pool_calls (
 call_id text primary key,
 workspace_id uuid not null references public.relay_workspaces(id),
 created_at timestamptz not null default now()
);
-- The owner's seat takes callbacks from someone no seat has called.
create table public.relay_pool_settings (
 id boolean primary key default true check (id),
 owner_workspace uuid references public.relay_workspaces(id)
);
alter table public.relay_pool enable row level security;
alter table public.relay_pool_use enable row level security;
alter table public.relay_pool_leases enable row level security;
alter table public.relay_last_caller enable row level security;
alter table public.relay_pool_calls enable row level security;
alter table public.relay_pool_settings enable row level security;
revoke all on public.relay_pool, public.relay_pool_use, public.relay_pool_leases, public.relay_last_caller,
 public.relay_pool_calls, public.relay_pool_settings from public, anon, authenticated;
grant all on public.relay_pool, public.relay_pool_use, public.relay_pool_leases, public.relay_last_caller,
 public.relay_pool_calls, public.relay_pool_settings to service_role;

-- The pool's day is Pacific time, as the number ramp's business days are (NUMBER_CONNECT_PLAN.md).
create function relay_private.pool_day() returns date language sql stable set search_path='' as $$
 select (now() at time zone 'America/Los_Angeles')::date
$$;

-- Lease a caller ID for one dial: the prospect's area codes in the caller's order of preference, then the number used
-- least today, under its daily cap counted across every seat. A row lock skipped by a concurrent lease means two seats
-- never get one number at once. Asking again for the same call returns the same number and counts it once.
create function relay_private.lease_number(prospect text, areas text[], call_ref text, lease_seconds integer)
returns jsonb language plpgsql security definer set search_path='' as $$
declare w public.relay_workspaces; chosen text; used integer; claimed integer; tried text[] := '{}';
 today date := relay_private.pool_day();
begin
 if auth.uid() is null or prospect !~ '^\+[1-9][0-9]{6,14}$' or length(call_ref) not between 1 and 128
  or lease_seconds not between 30 and 7200 or coalesce(array_length(areas, 1), 0) > 20 then
  raise exception 'Invalid request';
 end if;
 select * into w from public.relay_workspaces where id = auth.uid() and active;
 if not found then raise exception 'Workspace unavailable'; end if;
 select l.phone into chosen from public.relay_pool_leases l
  where l.workspace_id = w.id and l.call_ref = lease_number.call_ref;
 if found then
  update public.relay_pool_leases set lease_until = now() + make_interval(secs => lease_seconds)
   where phone = chosen;
  select attempts into used from public.relay_pool_use where phone = chosen and day = today;
  return jsonb_build_object('phone', chosen, 'attempts_today', used);
 end if;
 -- The snapshot can predate a lease committed a moment ago, so the claim itself is the check: a number whose lease
 -- is still live is skipped and the next one tried.
 for attempt in 1..20 loop
  select p.phone into chosen
   from public.relay_pool p
   left join public.relay_pool_use u on u.phone = p.phone and u.day = today
   where p.active
    and coalesce(u.attempts, 0) < p.daily_cap
    and p.phone <> all(tried)
    and not exists (select 1 from public.relay_pool_leases l where l.phone = p.phone and l.lease_until > now())
   order by coalesce(array_position(areas, p.area_code), 1000), coalesce(u.attempts, 0), p.phone
   limit 1
   for update of p skip locked;
  if chosen is null then
   return jsonb_build_object('phone', null, 'reason', 'POOL_EXHAUSTED');
  end if;
  insert into public.relay_pool_leases(phone, workspace_id, call_ref, lease_until)
   values (chosen, w.id, lease_number.call_ref, now() + make_interval(secs => lease_seconds))
   on conflict (phone) do update set workspace_id = excluded.workspace_id, call_ref = excluded.call_ref,
    lease_until = excluded.lease_until
   where public.relay_pool_leases.lease_until <= now();
  get diagnostics claimed = row_count;
  exit when claimed = 1;
  tried := tried || chosen;
  chosen := null;
 end loop;
 if chosen is null then
  return jsonb_build_object('phone', null, 'reason', 'POOL_BUSY');
 end if;
 insert into public.relay_pool_use(phone, day, attempts) values (chosen, today, 1)
  on conflict (phone, day) do update set attempts = public.relay_pool_use.attempts + 1
  returning attempts into used;
 insert into public.relay_last_caller(prospect, workspace_id, pool_phone, called_at)
  values (lease_number.prospect, w.id, chosen, now())
  on conflict on constraint relay_last_caller_pkey do update set workspace_id = excluded.workspace_id, pool_phone = excluded.pool_phone,
   called_at = excluded.called_at;
 return jsonb_build_object('phone', chosen, 'attempts_today', used);
end $$;
revoke all on function relay_private.lease_number(text, text[], text, integer) from public, anon;
grant execute on function relay_private.lease_number(text, text[], text, integer) to authenticated;
create function public.relay_lease_number(prospect text, areas text[], call_ref text, lease_seconds integer default 600)
returns jsonb language sql security invoker set search_path='' as $$
 select relay_private.lease_number(prospect, areas, call_ref, lease_seconds)
$$;
revoke all on function public.relay_lease_number(text, text[], text, integer) from public, anon;
grant execute on function public.relay_lease_number(text, text[], text, integer) to authenticated;

-- A lease ends with its call. Only the seat that holds it can end it.
create function relay_private.release_number(call_ref text) returns void language sql security definer
set search_path='' as $$
 delete from public.relay_pool_leases where workspace_id = auth.uid() and call_ref = release_number.call_ref;
$$;
revoke all on function relay_private.release_number(text) from public, anon;
grant execute on function relay_private.release_number(text) to authenticated;
create function public.relay_release_number(call_ref text) returns void language sql security invoker
set search_path='' as $$ select relay_private.release_number(call_ref) $$;
revoke all on function public.relay_release_number(text) from public, anon;
grant execute on function public.relay_release_number(text) to authenticated;

-- Today's attempts per pool number, so each seat's number health reads the whole team's use.
create function relay_private.pool_usage() returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_object_agg(p.phone, jsonb_build_object('attempts_today', coalesce(u.attempts, 0),
  'daily_cap', p.daily_cap, 'area_code', p.area_code, 'active', p.active)), '{}')
 from public.relay_pool p left join public.relay_pool_use u on u.phone = p.phone and u.day = relay_private.pool_day()
 where exists (select 1 from public.relay_workspaces where id = auth.uid() and active)
$$;
revoke all on function relay_private.pool_usage() from public, anon;
grant execute on function relay_private.pool_usage() to authenticated;
create function public.relay_pool_usage() returns jsonb language sql stable security invoker set search_path='' as $$
 select relay_private.pool_usage()
$$;
revoke all on function public.relay_pool_usage() from public, anon;
grant execute on function public.relay_pool_usage() to authenticated;

-- Which seat a call to a pool number belongs to: pinned at the call's first event to the seat that last called the
-- caller within 30 days, else the owner's seat. Every later event of that call follows the pin.
create function public.relay_pool_seat(call_id text, caller text, fallback uuid) returns uuid
language plpgsql security invoker set search_path='' as $$
declare seat uuid;
begin
 select workspace_id into seat from public.relay_pool_calls c where c.call_id = relay_pool_seat.call_id;
 if found then return seat; end if;
 select l.workspace_id into seat from public.relay_last_caller l
  join public.relay_workspaces w on w.id = l.workspace_id and w.active
  where l.prospect = caller and l.called_at > now() - interval '30 days';
 if seat is null then select owner_workspace into seat from public.relay_pool_settings; end if;
 seat := coalesce(seat, fallback);
 insert into public.relay_pool_calls(call_id, workspace_id) values (relay_pool_seat.call_id, seat)
  on conflict do nothing;
 return seat;
end $$;
revoke all on function public.relay_pool_seat(text, text, uuid) from public, anon, authenticated;
grant execute on function public.relay_pool_seat(text, text, uuid) to service_role;

create or replace function public.relay_receive(receipt jsonb)
returns text language plpgsql security invoker set search_path='' as $$
declare w public.relay_workspaces; old_hash text; p jsonb:=receipt->'event'->'payload'; inserted integer; seat uuid;
begin
 select * into w from public.relay_workspaces where id=(receipt->>'workspace_id')::uuid and active for share;
 if not found then raise exception 'Unknown workspace'; end if;
 -- A call to or from a pool number belongs to the seat that last called the caller (section 9C).
 if p->>'direction'='incoming' and exists(select 1 from public.relay_pool where phone=p->>'to') then
  seat := public.relay_pool_seat(p->>'call_control_id', p->>'from', w.id);
  select * into w from public.relay_workspaces where id=seat and active for share;
  if not found then raise exception 'Unknown workspace'; end if;
 elsif p->>'direction'='incoming' and p->>'to' ~ '^\+[1-9][0-9]{6,14}$' then
  insert into public.relay_numbers(phone,workspace_id) values(p->>'to',w.id) on conflict do nothing;
  if not exists(select 1 from public.relay_numbers where workspace_id=w.id and phone=p->>'to') then raise exception 'Number assigned elsewhere'; end if;
 elsif exists(select 1 from public.relay_pool_calls c where c.call_id=p->>'call_control_id') then
  seat := public.relay_pool_seat(p->>'call_control_id', '', w.id);
  select * into w from public.relay_workspaces where id=seat and active for share;
 end if;
 insert into public.relay_events(account_id,event_id,workspace_id,hash,event,received_at)
 values(receipt->>'account_id',receipt->'event'->>'id',w.id,receipt->>'hash',receipt->'event',(receipt->>'received_at')::timestamptz)
 on conflict do nothing;
 get diagnostics inserted=row_count;
 if inserted=0 then
  select hash into old_hash from public.relay_events where account_id=receipt->>'account_id' and event_id=receipt->'event'->>'id';
  if old_hash<>receipt->>'hash' then return 'collision'; end if;
  return 'duplicate';
 end if;
 if coalesce(p->>'call_control_id','')<>'' and exists(select 1 from public.relay_routes where connection_id=p->>'connection_id' and workspace_id=w.id) then
  insert into public.relay_call_routes(account_id,call_id,connection_id) values(receipt->>'account_id',p->>'call_control_id',p->>'connection_id') on conflict do nothing;
 end if;
 perform realtime.send(jsonb_build_object('pending',true),'wake','relay:'||w.id::text||':'||w.generation::text,true);
 return 'received';
end $$;

-- Retention: expired leases and old call pins go with the hourly cleanup; daily counts are kept 60 days.
create or replace function relay_private.pool_cleanup() returns void language sql security invoker set search_path='' as $$
 delete from public.relay_pool_leases where lease_until < now() - interval '1 hour';
 delete from public.relay_pool_calls where created_at < now() - interval '7 days';
 delete from public.relay_pool_use where day < relay_private.pool_day() - 60;
 delete from public.relay_last_caller where called_at < now() - interval '90 days';
$$;
revoke all on function relay_private.pool_cleanup() from public, anon, authenticated;
select cron.schedule('relay-pool-retention', '23 * * * *', 'select relay_private.pool_cleanup()');
