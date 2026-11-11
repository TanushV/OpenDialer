-- A pool may opt into one Call Control application. Legacy standalone and per-rep applications keep their routes.
alter table public.relay_routes add unique (connection_id,account_id);
alter table relay_private.number_pools
 add column shared_connection_id text unique,
 add foreign key (shared_connection_id,account_id) references public.relay_routes(connection_id,account_id);

alter table public.relay_call_routes
 add column workspace_id uuid references public.relay_workspaces(id) on delete cascade,
 add column shared_kind text check(shared_kind in ('prospect','audio','return','transfer')),
 add column local_ref text,
 add column delivery_state jsonb;
create index shared_call_workspace on public.relay_call_routes(workspace_id) where workspace_id is not null;
create unique index shared_pool_call_ref on relay_private.number_pool_attempts(pool_id,call_ref) where call_ref is not null;

create table relay_private.shared_audio_intents (
 pool_id uuid not null references relay_private.number_pools(id) on delete cascade,
 workspace_id uuid not null references public.relay_workspaces(id) on delete cascade,
 ref text not null,
 phone text not null,
 sip text not null,
 provider_id text,
 expires_at timestamptz not null default now()+interval '2 hours 2 minutes',
 primary key(pool_id,ref)
);
create table relay_private.shared_pending_events (
 account_id text not null,
 event_id text not null,
 connection_id text not null references public.relay_routes(connection_id),
 call_id text not null,
 receipt jsonb not null,
 received_at timestamptz not null,
 primary key(account_id,event_id)
);
create index shared_pending_by_call on relay_private.shared_pending_events(account_id,call_id);
revoke all on relay_private.shared_audio_intents,relay_private.shared_pending_events from public,anon,authenticated;
grant all on relay_private.shared_audio_intents,relay_private.shared_pending_events to service_role;

create function relay_private.shared_pool_for(seat uuid) returns relay_private.number_pools
language sql stable set search_path='' as $$
 select g.* from relay_private.number_pools g join relay_private.number_pool_members m on m.pool_id=g.id
 where m.workspace_id=seat and g.shared_connection_id is not null
$$;
create function relay_private.owns_pool_lease(app text,store uuid,installation uuid) returns boolean
language sql set search_path='' as $$
 select exists(select 1 from public.relay_workspaces w
 where w.id=auth.uid() and w.active and w.settings->>'telnyx_connection_id'=app
 and w.store_id=store and w.lease_owner=installation and w.lease_until>now() for share)
$$;
revoke all on function relay_private.shared_pool_for(uuid),relay_private.owns_pool_lease(text,uuid,uuid)
 from public,anon,authenticated;

-- Keep the old argument sets valid for legacy profiles. Shared profiles must present the active installation.
drop function public.relay_pool_reserve(text,text,text,boolean);
drop function public.relay_pool_submit(text,text,text);
drop function public.relay_pool_cancel(text,boolean);
revoke execute on function relay_private.pool_reserve(text,text,text,boolean),
 relay_private.pool_submit(text,text,text),relay_private.pool_cancel(text,boolean) from authenticated;

create function public.relay_pool_reserve(request_ref text,target text,preferred text,
 preferred_is_bound boolean default false,store uuid default null,installation uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid());
begin
 if g.id is not null and not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
 then return '{"reason":"unavailable"}'; end if;
 return relay_private.pool_reserve(request_ref,target,preferred,preferred_is_bound);
end $$;

create function public.relay_pool_submit(request_ref text,local_call text,number text,
 store uuid default null,installation uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid());
begin
 if g.id is not null and (not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
  or not exists(select 1 from relay_private.number_pool_numbers n
   where n.pool_id=g.id and n.phone=number and n.connection_id=g.shared_connection_id))
 then return '{"reason":"unavailable"}'; end if;
 return relay_private.pool_submit(request_ref,local_call,number);
end $$;

create function public.relay_pool_cancel(request_ref text,was_unsent boolean default false,
 store uuid default null,installation uuid default null)
returns void language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid());
begin
 if g.id is not null and not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
 then return; end if;
 perform relay_private.pool_cancel(request_ref,was_unsent);
end $$;

create function public.relay_pool_agent(agent_ref text,number text,destination text,store uuid,installation uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid()); sip text;
begin
 if g.id is null or not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
  or agent_ref is null or agent_ref !~ '^[A-Za-z0-9-]{1,64}$' then return '{"reason":"unavailable"}'; end if;
 select w.settings->>'sip_username' into sip from public.relay_workspaces w where w.id=auth.uid();
 if sip is null or sip !~ '^[A-Za-z0-9_.+-]{1,128}$'
  or destination is distinct from ('sip:'||sip||'@sip.telnyx.com')
  or not exists(select 1 from relay_private.number_pool_numbers n where n.pool_id=g.id and n.phone=number
   and n.connection_id=g.shared_connection_id and n.outbound_enabled)
 then return '{"reason":"unavailable"}'; end if;
 insert into relay_private.shared_audio_intents(pool_id,workspace_id,ref,phone,sip)
 values(g.id,auth.uid(),agent_ref,number,destination) on conflict do nothing;
 if not found then return '{"reason":"expired"}'; end if;
 return '{"submitted":true}';
end $$;
revoke all on function public.relay_pool_reserve(text,text,text,boolean,uuid,uuid),
 public.relay_pool_submit(text,text,text,uuid,uuid),public.relay_pool_cancel(text,boolean,uuid,uuid),
 public.relay_pool_agent(text,text,text,uuid,uuid) from public,anon;
grant execute on function public.relay_pool_reserve(text,text,text,boolean,uuid,uuid),
 public.relay_pool_submit(text,text,text,uuid,uuid),public.relay_pool_cancel(text,boolean,uuid,uuid),
 public.relay_pool_agent(text,text,text,uuid,uuid) to authenticated;

create or replace function relay_private.pool_snapshot() returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('id',m.pool_id,'segment_id',m.segment_id,'shared_connection_id',g.shared_connection_id,
  'numbers',coalesce((select jsonb_agg(jsonb_build_object('phone',n.phone,'outbound_enabled',n.outbound_enabled,
   'daily_limit',n.daily_limit,'used',relay_private.pool_load(n.pool_id,n.phone)) order by n.phone)
   from relay_private.number_pool_numbers n where n.pool_id=m.pool_id
    and (g.shared_connection_id is null or n.connection_id=g.shared_connection_id)), '[]'::jsonb))
 from relay_private.number_pool_members m join public.relay_workspaces w on w.id=m.workspace_id and w.active
 join relay_private.number_pools g on g.id=m.pool_id
 where m.workspace_id=auth.uid()
$$;

create or replace function public.relay_route(connection text,call_identity text) returns jsonb
language sql security invoker set search_path='' as $$
 select case when count(*)=1 then jsonb_agg(to_jsonb(r)||jsonb_build_object('shared',g.id is not null))->0 else null end
 from public.relay_routes r join public.relay_workspaces w on w.id=r.workspace_id
 left join relay_private.number_pools g on g.shared_connection_id=r.connection_id
 where (w.active or g.id is not null)
 and ((connection<>'' and r.connection_id=connection) or
  (connection='' and exists(select 1 from public.relay_call_routes c
   where c.account_id=r.account_id and c.connection_id=r.connection_id and c.call_id=call_identity)));
$$;

create function relay_private.pin_shared_call(g relay_private.number_pools, fallback uuid,event jsonb)
returns public.relay_call_routes language plpgsql set search_path='' as $$
declare p jsonb:=event->'payload'; context jsonb:=relay_private.pool_context(p);
 carrier_call text:=p->>'call_control_id'; pin public.relay_call_routes; root public.relay_call_routes;
 a relay_private.number_pool_attempts; audio relay_private.shared_audio_intents;
 seat uuid; kind text; ref text; state jsonb;
begin
 select * into pin from public.relay_call_routes c where c.account_id=g.account_id and c.call_id=carrier_call;
 if found then
  if pin.connection_id=g.shared_connection_id and pin.shared_kind is not null then return pin; end if;
  return null;
 end if;
 if event->>'event_type'='call.initiated' and p->>'direction'='incoming' and exists(
  select 1 from relay_private.number_pool_numbers n
  where n.pool_id=g.id and n.connection_id=g.shared_connection_id and n.phone=p->>'to') then
  kind:='return';
  select b.last_workspace_id,b.last_call_ref into seat,ref
  from relay_private.number_pool_bindings b join relay_private.number_pool_members m
   on m.pool_id=b.pool_id and m.workspace_id=b.last_workspace_id
  where b.pool_id=g.id and b.phone=p->>'to' and b.prospect=p->>'from';
  seat:=coalesce(seat,fallback);
 elsif context ?& array['inbound_call','inbound_task','attempt'] and p->>'direction' in ('incoming','outgoing') then
  select c.* into root from public.relay_call_routes c join public.relay_workspaces w on w.id=c.workspace_id
  where c.account_id=g.account_id and c.connection_id=g.shared_connection_id and c.shared_kind='return'
   and c.call_id=context->>'inbound_call' and p->>'to'=('sip:'||(w.settings->>'sip_username')||'@sip.telnyx.com');
  if not found then return null; end if;
  seat:=root.workspace_id;
  kind:='transfer';
  state:=jsonb_build_object('inbound_call',context->>'inbound_call','inbound_task',context->>'inbound_task','attempt',context->>'attempt');
 elsif p->>'direction'='outgoing' then
  select * into a from relay_private.number_pool_attempts t
   where t.pool_id=g.id and t.call_ref=coalesce(context->>'call_id',p->>'command_id')
   and t.submitted_at is not null and not t.cancelled
   and t.phone=p->>'from' and t.prospect=p->>'to' and t.expires_at>now()
   and (t.provider_id is null or t.provider_id=carrier_call);
  if found then
   seat:=a.workspace_id;
   kind:='prospect';
   ref:=a.call_ref;
   state:=jsonb_build_object('call_id',ref);
   update relay_private.number_pool_attempts set provider_id=carrier_call where workspace_id=seat and number_pool_attempts.ref=a.ref;
  else
   select * into audio from relay_private.shared_audio_intents i
    where i.pool_id=g.id and i.ref=coalesce(context->>'agent_leg_id',p->>'command_id')
    and i.phone=p->>'from' and i.sip=p->>'to' and i.expires_at>now()
    and (i.provider_id is null or i.provider_id=carrier_call);
   if found then
    seat:=audio.workspace_id;
    kind:='audio';
    state:=jsonb_build_object('agent_leg_id',audio.ref);
    update relay_private.shared_audio_intents i set provider_id=carrier_call where i.pool_id=g.id and i.ref=audio.ref;
   end if;
  end if;
 end if;
 if seat is null then return null; end if;
 insert into public.relay_call_routes(account_id,call_id,connection_id,workspace_id,shared_kind,local_ref,delivery_state)
 values(g.account_id,carrier_call,g.shared_connection_id,seat,kind,ref,state) returning * into pin;
 return pin;
end $$;

create function relay_private.deliver_shared_event(g relay_private.number_pools,pin public.relay_call_routes,receipt jsonb)
returns void language plpgsql set search_path='' as $$
declare delivery jsonb:=receipt->'event'; kind text:=delivery->>'event_type'; inserted int;
begin
 if pin.delivery_state is not null then
  delivery:=jsonb_set(delivery,'{payload,client_state}',to_jsonb(replace(encode(convert_to(pin.delivery_state::text,'UTF8'),'base64'),chr(10),'')));
 else
  delivery:=delivery #- '{payload,client_state}';
 end if;
 delivery:=delivery || jsonb_build_object('relay_shared',true);
 if pin.shared_kind='return' then
  delivery:=delivery || jsonb_build_object('relay_callback',true);
  if kind='call.initiated' and pin.local_ref is not null then
   delivery:=delivery || jsonb_build_object('callback_call_id',pin.local_ref);
  end if;
 end if;
 insert into public.relay_events(account_id,event_id,workspace_id,hash,event,received_at)
 values(g.account_id,delivery->>'id',pin.workspace_id,receipt->>'hash',delivery,(receipt->>'received_at')::timestamptz)
 on conflict do nothing;
 get diagnostics inserted=row_count;
 if inserted=0 then return; end if;
 if pin.shared_kind='prospect' then
  if kind in ('call.initiated','call.ringing','call.answered') then
   update relay_private.number_pool_bindings b set last_workspace_id=a.workspace_id,last_call_ref=a.call_ref,
    last_submitted_at=a.submitted_at from relay_private.number_pool_attempts a
   where a.pool_id=g.id and a.workspace_id=pin.workspace_id and a.call_ref=pin.local_ref and not a.cancelled
    and b.pool_id=a.pool_id and b.prospect=a.prospect and b.phone=a.phone
    and (b.last_submitted_at is null or b.last_submitted_at<a.submitted_at);
  elsif kind='call.hangup' then
   update relay_private.number_pool_attempts a set ended_at=coalesce(a.ended_at,now())
   where a.pool_id=g.id and a.workspace_id=pin.workspace_id and a.call_ref=pin.local_ref;
  end if;
 end if;
 perform realtime.send(jsonb_build_object('pending',true),'wake','relay:'||w.id::text||':'||w.generation::text,true)
 from public.relay_workspaces w where w.id=pin.workspace_id and w.active;
end $$;

alter function public.relay_receive(jsonb) rename to relay_receive_legacy;
create function public.relay_receive(receipt jsonb) returns text
language plpgsql security invoker set search_path='' as $$
declare g relay_private.number_pools; pin public.relay_call_routes; pending relay_private.shared_pending_events;
 delivery jsonb:=receipt->'event'; p jsonb:=delivery->'payload';
 account text:=receipt->>'account_id'; carrier_call text:=p->>'call_control_id';
 app text:=coalesce(nullif(receipt->>'connection_id',''),nullif(p->>'connection_id',''));
 fallback uuid; known_hash text;
begin
 if app is null then
  select c.connection_id into app from public.relay_call_routes c where c.account_id=account and c.call_id=carrier_call;
 end if;
 select * into g from relay_private.number_pools where shared_connection_id=app and account_id=account for update;
 if not found then return public.relay_receive_legacy(receipt); end if;
 select r.workspace_id into fallback from public.relay_routes r where r.connection_id=app and r.account_id=account;
 if fallback is distinct from (receipt->>'workspace_id')::uuid
  or coalesce(nullif(p->>'connection_id',''),app)<>app then raise exception 'Unknown shared application'; end if;
 select e.hash into known_hash from public.relay_events e where e.account_id=account and e.event_id=delivery->>'id';
 if not found then
  select q.receipt->>'hash' into known_hash from relay_private.shared_pending_events q
  where q.account_id=account and q.event_id=delivery->>'id';
 end if;
 if found then return case when known_hash=receipt->>'hash' then 'duplicate' else 'collision' end; end if;
 pin:=relay_private.pin_shared_call(g,fallback,delivery);
 if pin.workspace_id is null then
  insert into relay_private.shared_pending_events(account_id,event_id,connection_id,call_id,receipt,received_at)
  values(account,delivery->>'id',app,carrier_call,receipt,(receipt->>'received_at')::timestamptz) on conflict do nothing;
  return 'pending';
 end if;
 perform relay_private.deliver_shared_event(g,pin,receipt);
 for pending in select * from relay_private.shared_pending_events q
  where q.account_id=account and q.connection_id=app and q.call_id=carrier_call order by q.received_at,q.event_id
 loop
  perform relay_private.deliver_shared_event(g,pin,pending.receipt);
  delete from relay_private.shared_pending_events q where q.account_id=account and q.event_id=pending.event_id;
 end loop;
 return 'received';
end $$;
revoke all on function public.relay_receive(jsonb),relay_private.pin_shared_call(relay_private.number_pools,uuid,jsonb),
 relay_private.deliver_shared_event(relay_private.number_pools,public.relay_call_routes,jsonb) from public,anon,authenticated;
grant execute on function public.relay_receive(jsonb),relay_private.pin_shared_call(relay_private.number_pools,uuid,jsonb),
 relay_private.deliver_shared_event(relay_private.number_pools,public.relay_call_routes,jsonb) to service_role;

create or replace function relay_private.cleanup_number_pool() returns void language sql set search_path='' as $$
 delete from relay_private.number_pool_attempts where expires_at<now()-interval '31 days';
 delete from relay_private.number_pool_returns where created_at<now()-interval '7 days';
 delete from relay_private.shared_audio_intents where expires_at<now()-interval '1 day';
 delete from relay_private.shared_pending_events where received_at<now()-interval '1 day';
$$;
