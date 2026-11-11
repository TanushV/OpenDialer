-- Stable outbound caller IDs and return-call ownership for independently provisioned desktops.
-- Team membership and the fixed inbound routes are configured by the relay operator.
create table relay_private.number_pools (
 id uuid primary key,
 account_id text not null
);
create table relay_private.number_pool_members (
 workspace_id uuid primary key references public.relay_workspaces(id) on delete cascade,
 pool_id uuid not null references relay_private.number_pools(id) on delete cascade,
 segment_id text not null check (segment_id ~ '^[0-9]{1,40}$'),
 unique(pool_id,segment_id)
);
create table relay_private.number_pool_numbers (
 phone text primary key check (phone ~ '^\+1[0-9]{10}$'),
 pool_id uuid not null references relay_private.number_pools(id) on delete cascade,
 connection_id text not null references public.relay_routes(connection_id),
 daily_limit int not null default 90 check (daily_limit between 1 and 90),
 outbound_enabled boolean not null default true,
 unique(pool_id,phone)
);
create table relay_private.number_pool_bindings (
 pool_id uuid not null,
 prospect text not null check (prospect ~ '^\+1[0-9]{10}$'),
 phone text not null,
 last_workspace_id uuid references public.relay_workspaces(id) on delete set null,
 last_call_ref text,
 last_submitted_at timestamptz,
 primary key(pool_id,prospect),
 foreign key(pool_id,phone) references relay_private.number_pool_numbers(pool_id,phone) on delete cascade
);
create table relay_private.number_pool_attempts (
 workspace_id uuid not null references public.relay_workspaces(id) on delete cascade,
 pool_id uuid not null,
 ref text not null,
 prospect text not null check (prospect ~ '^\+1[0-9]{10}$'),
 phone text not null,
 expires_at timestamptz not null,
 submitted_at timestamptz,
 call_ref text,
 provider_id text,
 ended_at timestamptz,
 cancelled boolean not null default false,
 primary key(workspace_id,ref),
 unique(workspace_id,call_ref),
 foreign key(pool_id,phone) references relay_private.number_pool_numbers(pool_id,phone) on delete cascade
);
create index pool_attempts_by_number on relay_private.number_pool_attempts(pool_id,phone,submitted_at);
create index pool_attempts_by_prospect on relay_private.number_pool_attempts(pool_id,prospect,expires_at)
 where not cancelled and ended_at is null;
create index pool_attempts_by_carrier on relay_private.number_pool_attempts(workspace_id,provider_id);
create table relay_private.number_pool_returns (
 account_id text not null,
 call_id text not null,
 workspace_id uuid not null references public.relay_workspaces(id) on delete cascade,
 outbound_ref text,
 transfer_state text,
 created_at timestamptz not null default now(),
 primary key(account_id,call_id)
);
revoke all on relay_private.number_pools, relay_private.number_pool_members, relay_private.number_pool_numbers,
 relay_private.number_pool_bindings, relay_private.number_pool_attempts, relay_private.number_pool_returns
 from public, anon, authenticated;
grant all on relay_private.number_pools, relay_private.number_pool_members, relay_private.number_pool_numbers,
 relay_private.number_pool_bindings, relay_private.number_pool_attempts, relay_private.number_pool_returns
 to service_role;

-- Rolling 24 hours, like the local number selector. A held slot counts until it expires or is submitted.
create function relay_private.pool_load(pool uuid, number text) returns bigint
language sql stable set search_path='' as $$
 select count(*) from relay_private.number_pool_attempts a
 where a.pool_id=pool and a.phone=number and not a.cancelled and (
   a.submitted_at > now()-interval '24 hours' or (a.submitted_at is null and a.expires_at>now())
 )
$$;
revoke all on function relay_private.pool_load(uuid,text) from public, anon, authenticated;

create function relay_private.pool_snapshot() returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('id',m.pool_id,'segment_id',m.segment_id,'numbers',coalesce(
  (select jsonb_agg(jsonb_build_object('phone',n.phone,'outbound_enabled',n.outbound_enabled,
    'daily_limit',n.daily_limit,'used',relay_private.pool_load(n.pool_id,n.phone)) order by n.phone)
   from relay_private.number_pool_numbers n where n.pool_id=m.pool_id), '[]'::jsonb))
 from relay_private.number_pool_members m join public.relay_workspaces w on w.id=m.workspace_id and w.active
 where m.workspace_id=auth.uid()
$$;

create function relay_private.pool_reserve(request_ref text, target text, preferred text, preferred_is_bound boolean)
returns jsonb
language plpgsql security definer set search_path='' as $$
declare pool uuid; prior relay_private.number_pool_attempts; chosen text; bound text;
begin
 if request_ref is null or request_ref !~ '^[A-Za-z0-9-]{1,64}$' or target is null or target !~ '^\+1[0-9]{10}$' then
  raise exception 'Invalid pool reservation';
 end if;
 -- One short transaction per pool serializes bindings, prospect claims and daily capacity.
 select p.id into pool from relay_private.number_pools p
 join relay_private.number_pool_members m on m.pool_id=p.id
 join public.relay_workspaces w on w.id=m.workspace_id and w.active
 where m.workspace_id=auth.uid() for update of p;
 if not found then return '{"reason":"unavailable"}'::jsonb; end if;
 select * into prior from relay_private.number_pool_attempts a
 where a.workspace_id=auth.uid() and a.ref=request_ref;
 if found then
  if prior.pool_id=pool and prior.prospect=target and not prior.cancelled
     and prior.submitted_at is null and prior.expires_at>now() then
   return jsonb_build_object('phone',prior.phone);
  end if;
  return '{"reason":"expired"}'::jsonb;
 end if;
 if exists(select 1 from relay_private.number_pool_attempts a where a.pool_id=pool and a.prospect=target
           and not a.cancelled and a.ended_at is null and a.expires_at>now()) then
  return '{"reason":"busy"}'::jsonb;
 end if;
 select b.phone into bound from relay_private.number_pool_bindings b where b.pool_id=pool and b.prospect=target;
 if bound is null and preferred_is_bound then
  select n.phone into bound from relay_private.number_pool_numbers n where n.pool_id=pool and n.phone=preferred;
  if bound is not null then
   insert into relay_private.number_pool_bindings(pool_id,prospect,phone) values(pool,target,bound);
  end if;
 end if;
 select n.phone into chosen from relay_private.number_pool_numbers n
 where n.pool_id=pool and n.outbound_enabled and (bound is null or n.phone=bound)
   and relay_private.pool_load(pool,n.phone)<n.daily_limit
 order by (n.phone=preferred) desc nulls last, (substring(n.phone from 3 for 3)=substring(target from 3 for 3)) desc,
          relay_private.pool_load(pool,n.phone), n.phone limit 1;
 if chosen is null then
  return jsonb_strip_nulls(jsonb_build_object('reason','capacity','phone',bound));
 end if;
 insert into relay_private.number_pool_bindings(pool_id,prospect,phone) values(pool,target,chosen)
 on conflict do nothing;
 insert into relay_private.number_pool_attempts(workspace_id,pool_id,ref,prospect,phone,expires_at)
 values(auth.uid(),pool,request_ref,target,chosen,now()+interval '2 minutes');
 return jsonb_build_object('phone',chosen);
end $$;

create function relay_private.pool_submit(request_ref text, local_call text, number text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare pool uuid; held relay_private.number_pool_attempts; limit_today int;
begin
 if local_call is null or local_call !~ '^[A-Za-z0-9-]{1,64}$' then raise exception 'Invalid call identity'; end if;
 select p.id into pool from relay_private.number_pools p
 join relay_private.number_pool_members m on m.pool_id=p.id
 join public.relay_workspaces w on w.id=m.workspace_id and w.active
 where m.workspace_id=auth.uid() for update of p;
 if not found then return '{"reason":"unavailable"}'::jsonb; end if;
 select * into held from relay_private.number_pool_attempts a where a.workspace_id=auth.uid()
 and a.pool_id=pool and a.ref=request_ref and a.phone=number and not a.cancelled and a.expires_at>now();
 if not found then return '{"reason":"expired"}'::jsonb; end if;
 if held.submitted_at is not null then return '{"reason":"expired"}'::jsonb; end if;
 select daily_limit into limit_today from relay_private.number_pool_numbers n
 where n.pool_id=pool and n.phone=number and n.outbound_enabled;
 if not found or relay_private.pool_load(pool,number)>limit_today then return '{"reason":"capacity"}'::jsonb; end if;
 update relay_private.number_pool_attempts set submitted_at=now(),call_ref=local_call,expires_at=now()+interval '2 hours 2 minutes'
 where workspace_id=auth.uid() and ref=request_ref;
 return '{"submitted":true}'::jsonb;
end $$;

create function relay_private.pool_cancel(request_ref text, was_unsent boolean) returns void
language sql security definer set search_path='' as $$
 update relay_private.number_pool_attempts set cancelled=true,ended_at=now()
 where workspace_id=auth.uid() and ref=request_ref and provider_id is null
   and (submitted_at is null or was_unsent)
   and exists(select 1 from public.relay_workspaces w where w.id=auth.uid() and w.active)
$$;

create function public.relay_pool_snapshot() returns jsonb language sql stable set search_path='' as $$
 select relay_private.pool_snapshot()
$$;
create function public.relay_pool_reserve(request_ref text,target text,preferred text,preferred_is_bound boolean default false)
returns jsonb language sql set search_path='' as $$
 select relay_private.pool_reserve(request_ref,target,preferred,preferred_is_bound)
$$;
create function public.relay_pool_submit(request_ref text,local_call text,number text) returns jsonb
language sql set search_path='' as $$ select relay_private.pool_submit(request_ref,local_call,number) $$;
create function public.relay_pool_cancel(request_ref text,was_unsent boolean default false) returns void
language sql set search_path='' as $$ select relay_private.pool_cancel(request_ref,was_unsent) $$;
revoke all on function relay_private.pool_snapshot(), relay_private.pool_reserve(text,text,text,boolean),
 relay_private.pool_submit(text,text,text), relay_private.pool_cancel(text,boolean), public.relay_pool_snapshot(),
 public.relay_pool_reserve(text,text,text,boolean), public.relay_pool_submit(text,text,text), public.relay_pool_cancel(text,boolean)
 from public,anon;
grant execute on function relay_private.pool_snapshot(), relay_private.pool_reserve(text,text,text,boolean),
 relay_private.pool_submit(text,text,text), relay_private.pool_cancel(text,boolean), public.relay_pool_snapshot(),
 public.relay_pool_reserve(text,text,text,boolean), public.relay_pool_submit(text,text,text), public.relay_pool_cancel(text,boolean)
 to authenticated;

create or replace function public.relay_exchange(store uuid,installation uuid,acknowledgements jsonb default '[]',probe text default '')
returns jsonb language sql security invoker set search_path='' as $$
 select relay_private.exchange(store,installation,acknowledgements,probe) || jsonb_build_object(
  'numbers',(select coalesce(jsonb_agg(phone),'[]') from public.relay_numbers where workspace_id=auth.uid()),
  'gap_since',(select gap_since from public.relay_workspaces where id=auth.uid()),
  'pool',relay_private.pool_snapshot());
$$;

create function relay_private.pool_context(p jsonb) returns jsonb language plpgsql immutable set search_path='' as $$
begin
 return convert_from(decode(p->>'client_state','base64'),'UTF8')::jsonb;
exception when invalid_parameter_value or character_not_in_repertoire or invalid_text_representation then
 return null;
end $$;
revoke all on function relay_private.pool_context(jsonb) from public,anon,authenticated;
grant execute on function relay_private.pool_context(jsonb) to service_role;

create function relay_private.record_pool_call(seat uuid, account text, event jsonb, context jsonb)
returns void language plpgsql security invoker set search_path='' as $$
declare p jsonb:=event->'payload'; kind text:=event->>'event_type'; placed relay_private.number_pool_attempts;
begin
 if kind in ('call.initiated','call.ringing','call.answered') and p->>'direction'='outgoing' then
  update relay_private.number_pool_attempts a set provider_id=p->>'call_control_id'
  from relay_private.number_pools g, relay_private.number_pool_members m
  where a.workspace_id=seat and a.call_ref=context->>'call_id' and a.submitted_at is not null
    and g.id=a.pool_id and g.account_id=account and m.pool_id=g.id and m.workspace_id=seat
    and a.phone=p->>'from' and a.prospect=p->>'to'
    and (a.provider_id is null or a.provider_id=p->>'call_control_id') and not a.cancelled
  returning a.* into placed;
  if found then
   update relay_private.number_pool_bindings b
   set last_workspace_id=seat,last_call_ref=placed.call_ref,last_submitted_at=placed.submitted_at
   where b.pool_id=placed.pool_id and b.prospect=placed.prospect and b.phone=placed.phone
     and (b.last_submitted_at is null or b.last_submitted_at<placed.submitted_at);
  end if;
 elsif kind='call.hangup' then
  update relay_private.number_pool_attempts a set ended_at=coalesce(a.ended_at,now())
  from relay_private.number_pools g
  where a.workspace_id=seat and g.id=a.pool_id and g.account_id=account and a.submitted_at is not null
    and (a.provider_id=p->>'call_control_id' or (a.provider_id is null and a.call_ref=context->>'call_id'
         and a.phone=p->>'from' and a.prospect=p->>'to'));
 end if;
end $$;
revoke all on function relay_private.record_pool_call(uuid,text,jsonb,jsonb) from public,anon,authenticated;
grant execute on function relay_private.record_pool_call(uuid,text,jsonb,jsonb) to service_role;

create or replace function public.relay_receive(receipt jsonb)
returns text language plpgsql security invoker set search_path='' as $$
declare w public.relay_workspaces; source_workspace uuid; old_hash text; inserted int;
 delivery jsonb:=receipt->'event'; p jsonb:=delivery->'payload'; context jsonb:=relay_private.pool_context(p);
 account text:=receipt->>'account_id'; carrier_call text:=p->>'call_control_id'; pool uuid;
 target relay_private.number_pool_returns;
begin
 select * into w from public.relay_workspaces where id=(receipt->>'workspace_id')::uuid and active for share;
 if not found then raise exception 'Unknown workspace'; end if;
 source_workspace := w.id;
 select hash into old_hash from public.relay_events where account_id=account and event_id=delivery->>'id';
 if found then return case when old_hash=receipt->>'hash' then 'duplicate' else 'collision' end; end if;
 select * into target from relay_private.number_pool_returns r where r.account_id=account and r.call_id=carrier_call;
 if not found and context ?& array['inbound_call','inbound_task','attempt'] then
  insert into relay_private.number_pool_returns(account_id,call_id,workspace_id,transfer_state)
  select account,carrier_call,r.workspace_id,p->>'client_state' from relay_private.number_pool_returns r
  where r.account_id=account and r.call_id=context->>'inbound_call' and r.transfer_state is null
  on conflict do nothing;
  select * into target from relay_private.number_pool_returns r where r.account_id=account and r.call_id=carrier_call;
 end if;
 if target.workspace_id is null and delivery->>'event_type'='call.initiated' and p->>'direction'='incoming' then
  select n.pool_id into pool from relay_private.number_pool_numbers n
  join relay_private.number_pools g on g.id=n.pool_id and g.account_id=account
  where n.phone=p->>'to' and n.connection_id=p->>'connection_id';
  if found then
   insert into relay_private.number_pool_returns(account_id,call_id,workspace_id,outbound_ref)
   select account,carrier_call,coalesce(destination.id,w.id),
          case when destination.id is not null then b.last_call_ref end
   from (select 1) x
   left join relay_private.number_pool_bindings b on b.pool_id=pool and b.phone=p->>'to' and b.prospect=p->>'from'
   left join relay_private.number_pool_members m on m.pool_id=b.pool_id and m.workspace_id=b.last_workspace_id
   left join public.relay_workspaces destination on destination.id=m.workspace_id and destination.active
   on conflict do nothing;
   select * into target from relay_private.number_pool_returns r where r.account_id=account and r.call_id=carrier_call;
  end if;
 end if;
 if target.workspace_id is not null then
  select * into w from public.relay_workspaces where id=target.workspace_id and active for share;
  if not found then raise exception 'Callback workspace unavailable'; end if;
  if target.transfer_state is not null then
   delivery := jsonb_set(delivery,'{payload,client_state}',to_jsonb(target.transfer_state));
  elsif target.outbound_ref is not null and delivery->>'event_type'='call.initiated' then
   delivery := delivery || jsonb_build_object('callback_call_id',target.outbound_ref);
  end if;
 elsif p->>'direction'='incoming' and p->>'to' ~ '^\+[1-9][0-9]{6,14}$' then
  insert into public.relay_numbers(phone,workspace_id) values(p->>'to',w.id) on conflict do nothing;
  if not exists(select 1 from public.relay_numbers where workspace_id=w.id and phone=p->>'to') then
   raise exception 'Number assigned elsewhere';
  end if;
 end if;
 insert into public.relay_events(account_id,event_id,workspace_id,hash,event,received_at)
 values(account,delivery->>'id',w.id,receipt->>'hash',delivery,(receipt->>'received_at')::timestamptz)
 on conflict do nothing;
 get diagnostics inserted=row_count;
 if inserted=0 then
  select hash into old_hash from public.relay_events where account_id=account and event_id=delivery->>'id';
  return case when old_hash=receipt->>'hash' then 'duplicate' else 'collision' end;
 end if;
 perform relay_private.record_pool_call(source_workspace,account,delivery,context);
 if coalesce(carrier_call,'')<>'' and exists(
    select 1 from public.relay_routes r where r.connection_id=p->>'connection_id' and r.account_id=account
    and (r.workspace_id=w.id or target.workspace_id is not null)) then
  insert into public.relay_call_routes(account_id,call_id,connection_id) values(account,carrier_call,p->>'connection_id')
  on conflict do nothing;
 end if;
 perform realtime.send(jsonb_build_object('pending',true),'wake','relay:'||w.id::text||':'||w.generation::text,true);
 return 'received';
end $$;

-- A matched missed callback can be restored on its rep's desktop after a longer offline period.
-- Later carrier state is subject to the normal expiry, so reconnect never answers a stale call.
create or replace function relay_private.cleanup() returns void language sql security invoker set search_path='' as $$
 update public.relay_workspaces w set gap_since=coalesce(w.gap_since,now()) where exists(
  select 1 from public.relay_events e where e.workspace_id=w.id and e.acknowledged_at is null
  and e.received_at<now()-(case when e.event ? 'callback_call_id' then interval '30 days' else interval '24 hours' end));
 update public.relay_events set acknowledged_at=now(),event=jsonb_build_object('id',event_id)
 where acknowledged_at is null and received_at<
  now()-(case when event ? 'callback_call_id' then interval '30 days' else interval '24 hours' end);
 delete from public.relay_events where acknowledged_at<now()-interval '7 days';
 delete from public.relay_call_routes where created_at<now()-interval '7 days';
 delete from public.relay_invitations where expires_at<now();
$$;

create function relay_private.cleanup_number_pool() returns void language sql set search_path='' as $$
 delete from relay_private.number_pool_attempts where expires_at<now()-interval '31 days';
 delete from relay_private.number_pool_returns where created_at<now()-interval '7 days';
$$;
revoke all on function relay_private.cleanup_number_pool() from public,anon,authenticated;
select cron.schedule('number-pool-retention','23 * * * *','select relay_private.cleanup_number_pool()');
