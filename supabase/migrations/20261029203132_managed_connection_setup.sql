-- Operator-issued per-PC profiles. Client credentials cannot assign carrier routes.
alter table public.relay_workspaces add column settings jsonb not null default '{}', add column gap_since timestamptz;
create table public.relay_invitations (
 hash text primary key, workspace_id uuid not null unique, label text not null,
 settings jsonb not null, routes jsonb not null,
 expires_at timestamptz not null default now()+interval '1 hour'
);
alter table public.relay_invitations enable row level security;
revoke all on public.relay_invitations from public,anon,authenticated;
grant all on public.relay_invitations to service_role;
create function public.relay_activate(invitation text) returns void language plpgsql security invoker set search_path='' as $$
declare i public.relay_invitations; r jsonb;
begin
 select * into i from public.relay_invitations where hash=invitation and expires_at>now() for update;
 if not found then raise exception 'Invitation unavailable'; end if;
 insert into public.relay_workspaces(id,label,settings) values(i.workspace_id,i.label,i.settings) on conflict(id) do nothing;
 for r in select * from jsonb_array_elements(i.routes) loop
  if exists(select 1 from public.relay_routes where connection_id=r->>'connection_id' and workspace_id<>i.workspace_id) then raise exception 'Route already assigned'; end if;
  insert into public.relay_routes(connection_id,account_id,workspace_id,public_key)
  values(r->>'connection_id',r->>'account_id',i.workspace_id,r->>'public_key') on conflict(connection_id) do nothing;
 end loop;
end $$;
revoke all on function public.relay_activate(text) from public,anon,authenticated;
grant execute on function public.relay_activate(text) to service_role;

create or replace function public.relay_exchange(store uuid,installation uuid,acknowledgements jsonb default '[]',probe text default '')
returns jsonb language sql security invoker set search_path='' as $$
 select relay_private.exchange(store,installation,acknowledgements,probe) || jsonb_build_object(
 'numbers',(select coalesce(jsonb_agg(phone),'[]') from public.relay_numbers where workspace_id=auth.uid()),
 'gap_since',(select gap_since from public.relay_workspaces where id=auth.uid()));
$$;
-- Once an authenticated caller has reconciled its local calls, it can acknowledge a delivery gap.
create function relay_private.clear_gap() returns void language sql security definer set search_path='' as $$
 update public.relay_workspaces set gap_since=null where id=auth.uid() and active;
$$;
revoke all on function relay_private.clear_gap() from public,anon;
grant execute on function relay_private.clear_gap() to authenticated;
create function public.relay_clear_gap() returns void language sql security invoker set search_path='' as $$select relay_private.clear_gap()$$;
revoke all on function public.relay_clear_gap() from public,anon;
grant execute on function public.relay_clear_gap() to authenticated;

-- Bounded retention; never silently discard pending events.
create function relay_private.cleanup() returns void language plpgsql security invoker set search_path='' as $$
begin
 update public.relay_workspaces w set gap_since=coalesce(w.gap_since,now()) where exists(
  select 1 from public.relay_events e where e.workspace_id=w.id and e.acknowledged_at is null and e.received_at<now()-interval '24 hours');
 update public.relay_events set acknowledged_at=now(),event=jsonb_build_object('id',event_id)
 where acknowledged_at is null and received_at<now()-interval '24 hours';
 delete from public.relay_events where acknowledged_at<now()-interval '7 days';
 delete from public.relay_call_routes where created_at<now()-interval '7 days';
 delete from public.relay_invitations where expires_at<now();
end $$;
revoke all on function relay_private.cleanup() from public,anon,authenticated;
create extension if not exists pg_cron;
select cron.schedule('relay-retention','17 * * * *','select relay_private.cleanup()');

create or replace function public.relay_receive(receipt jsonb)
returns text language plpgsql security invoker set search_path='' as $$
declare w public.relay_workspaces; old_hash text; p jsonb:=receipt->'event'->'payload'; inserted integer;
begin
 select * into w from public.relay_workspaces where id=(receipt->>'workspace_id')::uuid and active for share;
 if not found then raise exception 'Unknown workspace'; end if;
 if p->>'direction'='incoming' and p->>'to' ~ '^\+[1-9][0-9]{6,14}$' then
  insert into public.relay_numbers(phone,workspace_id) values(p->>'to',w.id) on conflict do nothing;
  if not exists(select 1 from public.relay_numbers where workspace_id=w.id and phone=p->>'to') then raise exception 'Number assigned elsewhere'; end if;
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
