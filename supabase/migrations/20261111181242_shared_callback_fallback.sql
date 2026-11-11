-- One durable handler per incoming shared call. The signed webhook is the source of carrier progress.
alter table relay_private.number_pools add column fallback_enabled boolean not null default false;
alter table public.relay_workspaces
 add column callback_reachable_until timestamptz,
 add column callback_greeting text not null default '' check(length(callback_greeting)<=500);

create table relay_private.shared_callbacks (
 account_id text not null,
 call_id text not null,
 pool_id uuid not null references relay_private.number_pools(id) on delete cascade,
 workspace_id uuid not null references public.relay_workspaces(id) on delete cascade,
 handler text not null default 'pending' check(handler in ('pending','desktop','hosted','done')),
 action text not null default 'answer' check(action in ('answer','speak','wait','hangup')),
 next_at timestamptz not null,
 expires_at timestamptz not null,
 action_started_at timestamptz,
 desktop_owner uuid,
 desktop_until timestamptz,
 worker_owner uuid,
 worker_until timestamptz,
 answer_id uuid not null default gen_random_uuid(),
 speak_id uuid not null default gen_random_uuid(),
 hangup_id uuid not null default gen_random_uuid(),
 greeting text not null,
 primary key(account_id,call_id),
 foreign key(account_id,call_id) references public.relay_call_routes(account_id,call_id) on delete cascade
);
create index callback_by_workspace on relay_private.shared_callbacks(workspace_id,call_id);
create index callback_by_pool on relay_private.shared_callbacks(pool_id);
create index callback_due on relay_private.shared_callbacks(next_at) where handler<>'done';
revoke all on relay_private.shared_callbacks from public,anon,authenticated;
grant all on relay_private.shared_callbacks to service_role;

create or replace function relay_private.pool_snapshot() returns jsonb
language sql stable security definer set search_path='' as $$
 select jsonb_build_object('id',m.pool_id,'segment_id',m.segment_id,'shared_connection_id',g.shared_connection_id,
  'offline_fallback',g.fallback_enabled,'numbers',coalesce((
   select jsonb_agg(jsonb_build_object('phone',n.phone,'outbound_enabled',n.outbound_enabled,
    'daily_limit',n.daily_limit,'used',relay_private.pool_load(n.pool_id,n.phone)) order by n.phone)
   from relay_private.number_pool_numbers n where n.pool_id=m.pool_id
    and (g.shared_connection_id is null or n.connection_id=g.shared_connection_id)), '[]'::jsonb))
 from relay_private.number_pool_members m join public.relay_workspaces w on w.id=m.workspace_id and w.active
 join relay_private.number_pools g on g.id=m.pool_id where m.workspace_id=auth.uid()
$$;

-- Same exchange, so reachability is refreshed by the existing authenticated polling and lease.
drop function public.relay_exchange(uuid,uuid,jsonb,text);
create function public.relay_exchange(store uuid,installation uuid,acknowledgements jsonb default '[]',probe text default '',
 reachable boolean default null,greeting text default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 if length(greeting)>500 then raise exception 'Invalid callback greeting'; end if;
 result:=relay_private.exchange(store,installation,acknowledgements,probe);
 if reachable is not null and (relay_private.shared_pool_for(auth.uid())).id is not null then
  update public.relay_workspaces set
   callback_reachable_until=case when reachable then now()+interval '45 seconds' end,
   callback_greeting=coalesce(greeting,callback_greeting)
  where id=auth.uid();
 end if;
 return result || jsonb_build_object(
  'numbers',(select coalesce(jsonb_agg(phone),'[]') from public.relay_numbers where workspace_id=auth.uid()),
  'gap_since',(select gap_since from public.relay_workspaces where id=auth.uid()),
  'pool',relay_private.pool_snapshot());
end $$;
revoke all on function public.relay_exchange(uuid,uuid,jsonb,text,boolean,text) from public,anon;
grant execute on function public.relay_exchange(uuid,uuid,jsonb,text,boolean,text) to authenticated;

create function relay_private.record_callback_progress() returns trigger
language plpgsql set search_path='' as $$
declare kind text:=new.event->>'event_type'; root text:=new.event->'payload'->>'call_control_id';
 arrival timestamptz;
begin
 if new.event->>'relay_callback' is distinct from 'true' then return new; end if;
 if kind='call.initiated' then
  arrival:=least(new.received_at,coalesce((new.event->>'occurred_at')::timestamptz,new.received_at));
  insert into relay_private.shared_callbacks(account_id,call_id,pool_id,workspace_id,next_at,expires_at,greeting,handler)
  select new.account_id,root,g.id,w.id,
   arrival+case when w.active and w.lease_until>now() and w.callback_reachable_until>now()
                then interval '23 seconds' else interval '0' end,
   arrival+interval '55 seconds',w.callback_greeting,case when arrival<now()-interval '55 seconds' then 'done' else 'pending' end
  from public.relay_call_routes c join relay_private.number_pools g on g.shared_connection_id=c.connection_id
  join public.relay_workspaces w on w.id=c.workspace_id
  where c.account_id=new.account_id and c.call_id=root and c.shared_kind='return'
  on conflict do nothing;
 elsif kind='call.hangup' then
  update relay_private.shared_callbacks set handler='done',worker_owner=null,worker_until=null
  where account_id=new.account_id and call_id=root;
 elsif kind='call.answered' then
  update relay_private.shared_callbacks set action='speak',action_started_at=null,worker_owner=null,
   worker_until=null,next_at=now(),expires_at=greatest(expires_at,now()+interval '40 seconds')
  where account_id=new.account_id and call_id=root and handler<>'done' and action='answer';
 elsif kind='call.speak.started' then
  update relay_private.shared_callbacks set action='wait',action_started_at=null,worker_owner=null,
   worker_until=null,next_at=now()+interval '35 seconds',expires_at=greatest(expires_at,now()+interval '45 seconds')
  where account_id=new.account_id and call_id=root and handler='hosted' and action='speak';
 elsif kind='call.speak.ended' then
  update relay_private.shared_callbacks set action='hangup',action_started_at=null,worker_owner=null,
   worker_until=null,next_at=now(),expires_at=greatest(expires_at,now()+interval '15 seconds')
  where account_id=new.account_id and call_id=root and handler='hosted' and action in ('speak','wait');
 end if;
 return new;
end $$;
revoke all on function relay_private.record_callback_progress() from public,anon,authenticated;
create trigger relay_callback_progress after insert on public.relay_events
 for each row when (new.event->>'relay_callback'='true') execute function relay_private.record_callback_progress();

create function public.relay_callback_claim(root_id text,store uuid,installation uuid)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid()); c relay_private.shared_callbacks;
begin
 if g.id is null or not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
 then return '{"status":"unavailable"}'; end if;
 select * into c from relay_private.shared_callbacks q
 where q.account_id=g.account_id and q.call_id=root_id and q.pool_id=g.id and q.workspace_id=auth.uid() for update;
 if not found then return '{"status":"unavailable"}'; end if;
 if c.handler in ('hosted','done') then return jsonb_build_object('status',c.handler); end if;
 if c.handler='desktop' and c.desktop_owner=installation and c.action<>'answer'
 then return jsonb_build_object('status','desktop','answer_id',c.answer_id); end if;
 if c.expires_at<=now() then return '{"status":"done"}'; end if;
 if g.fallback_enabled and (
  (c.handler='pending' and (c.next_at<=now() or not exists(select 1 from public.relay_workspaces w
   where w.id=c.workspace_id and w.callback_reachable_until>now())))
  or (c.handler='desktop' and (c.desktop_owner<>installation or c.desktop_until<=now()))
 ) then return '{"status":"hosted"}'; end if;
 update relay_private.shared_callbacks set handler='desktop',desktop_owner=installation,
  desktop_until=least(now()+interval '18 seconds',c.expires_at),action_started_at=coalesce(action_started_at,now())
 where account_id=c.account_id and call_id=c.call_id;
 return jsonb_build_object('status','desktop','answer_id',c.answer_id);
end $$;
revoke all on function public.relay_callback_claim(text,uuid,uuid) from public,anon;
grant execute on function public.relay_callback_claim(text,uuid,uuid) to authenticated;

create function relay_private.callback_due(c relay_private.shared_callbacks) returns boolean
language sql stable set search_path='' as $$
 select c.expires_at>now() and (
  (c.handler='pending' and c.next_at<=now()) or
  (c.handler='desktop' and c.action='answer' and c.desktop_until<=now()) or
  (c.handler='hosted' and c.next_at<=now() and (c.worker_until is null or c.worker_until<=now()))
 )
$$;
revoke all on function relay_private.callback_due(relay_private.shared_callbacks) from public,anon,authenticated;
grant execute on function relay_private.callback_due(relay_private.shared_callbacks) to service_role;

create function public.relay_callback_take(batch_size integer default 6) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare c relay_private.shared_callbacks; token uuid; step text; results jsonb:='[]';
begin
 for c in select q.* from relay_private.shared_callbacks q
  join relay_private.number_pools g on g.id=q.pool_id and g.fallback_enabled
  where q.handler<>'done' and relay_private.callback_due(q) order by q.next_at
  limit greatest(1,least(coalesce(batch_size,6),8)) for update of q skip locked
 loop
  -- Never replay an uncertain answer/greeting outside Telnyx's command-ID deduplication window.
  if c.action='answer' and c.action_started_at<now()-interval '50 seconds' then
   update relay_private.shared_callbacks set handler='done' where account_id=c.account_id and call_id=c.call_id;
   continue;
  end if;
  step:=case when c.action='wait' or (c.action='speak' and c.action_started_at<now()-interval '50 seconds')
             then 'hangup' else c.action end;
  token:=gen_random_uuid();
  update relay_private.shared_callbacks set handler='hosted',action=step,worker_owner=token,
   worker_until=now()+interval '12 seconds',
   action_started_at=case when step<>c.action then now() else coalesce(action_started_at,now()) end
  where account_id=c.account_id and call_id=c.call_id;
  results:=results || jsonb_build_object('account_id',c.account_id,'call_id',c.call_id,'lease',token,
   'action',step,'greeting',case when step='speak' then c.greeting end,
   'command_id',case step when 'answer' then c.answer_id when 'speak' then c.speak_id else c.hangup_id end);
 end loop;
 return results;
end $$;

create function public.relay_callback_finish(job jsonb,result text) returns void
language plpgsql security invoker set search_path='' as $$
begin
 if result not in ('ok','retry','ended') then raise exception 'Invalid worker result'; end if;
 update relay_private.shared_callbacks set worker_owner=null,worker_until=null,
  handler=case when result='ended' or (result='ok' and action='hangup') then 'done' else handler end,
  action=case when result='ok' and action='answer' then 'speak'
              when result='ok' and action='speak' then 'wait' else action end,
  action_started_at=case when result='ok' then null else action_started_at end,
  next_at=now()+case when result='retry' then interval '4 seconds'
                     when result='ok' and action='speak' then interval '35 seconds' else interval '0' end,
  expires_at=case when result='ok' and action in ('answer','speak') then greatest(expires_at,now()+interval '45 seconds') else expires_at end
 where account_id=job->>'account_id' and call_id=job->>'call_id'
  and handler='hosted' and worker_owner=(job->>'lease')::uuid and worker_until>now() and action=job->>'action';
end $$;
revoke all on function public.relay_callback_take(integer),public.relay_callback_finish(jsonb,text) from public,anon,authenticated;
grant execute on function public.relay_callback_take(integer),public.relay_callback_finish(jsonb,text) to service_role;

-- Keep one missed-call receipt for the rep. Subsequent controls expire normally.
create or replace function relay_private.cleanup() returns void language sql security invoker set search_path='' as $$
 update public.relay_workspaces w set gap_since=coalesce(w.gap_since,now()) where exists(
  select 1 from public.relay_events e where e.workspace_id=w.id and e.acknowledged_at is null
  and e.received_at<now()-(case when e.event ? 'callback_call_id'
    or (e.event->>'relay_callback'='true' and e.event->>'event_type'='call.initiated')
    then interval '30 days' else interval '24 hours' end));
 update public.relay_events set acknowledged_at=now(),event=jsonb_build_object('id',event_id)
 where acknowledged_at is null and received_at<now()-(case when event ? 'callback_call_id'
   or (event->>'relay_callback'='true' and event->>'event_type'='call.initiated')
   then interval '30 days' else interval '24 hours' end);
 delete from public.relay_events where acknowledged_at<now()-interval '7 days';
 delete from public.relay_call_routes where created_at<now()-interval '7 days';
 delete from public.relay_invitations where expires_at<now();
$$;
