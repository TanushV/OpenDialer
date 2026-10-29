-- Control delivery only. No carrier secrets, audio, transcript, leads or business history.
create schema if not exists relay_private;
revoke all on schema relay_private from public, anon;
grant usage on schema relay_private to authenticated, service_role;
create table public.relay_workspaces (
 id uuid primary key references auth.users(id) on delete cascade,
 label text not null, active boolean not null default true,
 store_id uuid, lease_owner uuid, lease_until timestamptz,
 generation uuid not null default gen_random_uuid()
);
create table public.relay_routes (
 connection_id text primary key, account_id text not null,
 workspace_id uuid not null references public.relay_workspaces(id),
 public_key text not null
);
create table public.relay_numbers (
 phone text primary key, workspace_id uuid not null references public.relay_workspaces(id)
);
create table public.relay_call_routes (
 account_id text not null, call_id text not null,
 connection_id text not null references public.relay_routes(connection_id),
 created_at timestamptz not null default now(), primary key(account_id,call_id)
);
create table public.relay_events (
 account_id text not null, event_id text not null,
 workspace_id uuid not null references public.relay_workspaces(id),
 hash text not null, event jsonb not null, received_at timestamptz not null,
 acknowledged_at timestamptz, primary key(account_id,event_id)
);
create index relay_events_pending on public.relay_events(workspace_id,received_at) where acknowledged_at is null;
create index relay_routes_workspace on public.relay_routes(workspace_id);
create index relay_numbers_workspace on public.relay_numbers(workspace_id);
create index relay_call_routes_connection on public.relay_call_routes(connection_id);
create index relay_call_routes_lookup on public.relay_call_routes(call_id);
create index relay_events_workspace on public.relay_events(workspace_id);
alter table public.relay_workspaces enable row level security;
alter table public.relay_routes enable row level security;
alter table public.relay_numbers enable row level security;
alter table public.relay_call_routes enable row level security;
alter table public.relay_events enable row level security;
revoke all on public.relay_workspaces,public.relay_routes,public.relay_numbers,public.relay_call_routes,public.relay_events from anon,authenticated;
grant all on public.relay_workspaces,public.relay_routes,public.relay_numbers,public.relay_call_routes,public.relay_events to service_role;
grant select on public.relay_workspaces,public.relay_routes,public.relay_numbers to authenticated;
create policy own_workspace on public.relay_workspaces for select to authenticated using(id=(select auth.uid()) and active);
create policy own_routes on public.relay_routes for select to authenticated using(workspace_id=(select auth.uid()) and exists(select 1 from public.relay_workspaces where id=workspace_id and active));
create policy own_numbers on public.relay_numbers for select to authenticated using(workspace_id=(select auth.uid()) and exists(select 1 from public.relay_workspaces where id=workspace_id and active));
create policy relay_receive on realtime.messages for select to authenticated using(extension='broadcast' and exists(select 1 from public.relay_workspaces w where w.id=(select auth.uid()) and w.active and (select realtime.topic())='relay:'||w.id::text||':'||w.generation::text));

create function public.relay_route(connection text, call_identity text)
returns jsonb language sql security invoker set search_path='' as $$
 select case when count(*)=1 then jsonb_agg(to_jsonb(r))->0 else null end
 from public.relay_routes r join public.relay_workspaces w on w.id=r.workspace_id and w.active
 where (connection<>'' and r.connection_id=connection) or (connection='' and exists(select 1 from public.relay_call_routes c where c.connection_id=r.connection_id and c.call_id=call_identity));
$$;
revoke all on function public.relay_route(text,text) from public,anon,authenticated;
grant execute on function public.relay_route(text,text) to service_role;

create function public.relay_receive(receipt jsonb)
returns text language plpgsql security invoker set search_path='' as $$
declare w public.relay_workspaces; old_hash text; p jsonb:=receipt->'event'->'payload'; inserted integer;
begin
 select * into w from public.relay_workspaces where id=(receipt->>'workspace_id')::uuid and active for share;
 if not found then raise exception 'Unknown workspace'; end if;
 if p->>'direction'='incoming' and not exists(select 1 from public.relay_numbers where workspace_id=w.id and phone=p->>'to') then
  -- Agent SIP events have no PSTN DID. Only numeric inbound destinations require a DID binding.
  if p->>'to' ~ '^\+' then raise exception 'Unassigned inbound number'; end if;
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
revoke all on function public.relay_receive(jsonb) from public,anon,authenticated;
grant execute on function public.relay_receive(jsonb) to service_role;

create function relay_private.exchange(store uuid, installation uuid, acknowledgements jsonb, probe text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare w public.relay_workspaces; batch jsonb;
begin
 if auth.uid() is null or store is null or installation is null or jsonb_array_length(acknowledgements)>100 or length(probe)>128 then raise exception 'Invalid request'; end if;
 select * into w from public.relay_workspaces where id=auth.uid() and active for update;
 if not found then raise exception 'Workspace unavailable'; end if;
 if w.store_id is not null and w.store_id<>store then raise exception 'Restore the existing workspace or request a new profile'; end if;
 if w.lease_until>now() and w.lease_owner<>installation then raise exception 'Workspace active on another computer'; end if;
 update public.relay_workspaces set store_id=store,lease_owner=installation,lease_until=now()+interval '45 seconds' where id=w.id;
 update public.relay_events e set acknowledged_at=now(),event=jsonb_build_object('id',event_id)
 where e.workspace_id=w.id and e.acknowledged_at is null and exists(select 1 from jsonb_array_elements(acknowledgements) a where a->>'account_id'=e.account_id and a->>'event_id'=e.event_id);
 select coalesce(jsonb_agg(to_jsonb(e)),'[]') into batch from(select account_id,event_id,hash,event,received_at from public.relay_events where workspace_id=w.id and acknowledged_at is null order by received_at,event_id limit 100)e;
 if probe<>'' then perform realtime.send(jsonb_build_object('probe',probe),'wake','relay:'||w.id::text||':'||w.generation::text,true);end if;
 return jsonb_build_object('events',batch,'topic','relay:'||w.id::text||':'||w.generation::text,'lease_seconds',45);
end $$;
revoke all on function relay_private.exchange(uuid,uuid,jsonb,text) from public,anon;
grant execute on function relay_private.exchange(uuid,uuid,jsonb,text) to authenticated;
create function public.relay_exchange(store uuid,installation uuid,acknowledgements jsonb default '[]',probe text default '')
returns jsonb language sql security invoker set search_path='' as $$select relay_private.exchange(store,installation,acknowledgements,probe)$$;
revoke all on function public.relay_exchange(uuid,uuid,jsonb,text) from public,anon;
grant execute on function public.relay_exchange(uuid,uuid,jsonb,text) to authenticated;
