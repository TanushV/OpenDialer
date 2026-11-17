-- The pool already serializes short reservation and submission transactions.
-- Store only the desk's existing company hash, the calling zone and the requested-call exception.
alter table relay_private.number_pool_attempts
 add column company_key text check(company_key ~ '^[0-9a-f]{32}$'),
 add column company_zone text,
 add column company_requested boolean not null default false;
create index pool_attempts_by_company
 on relay_private.number_pool_attempts(pool_id,company_key,submitted_at)
 where company_key is not null and not cancelled;

create function relay_private.company_already_called(pool uuid, company text, target text, zone text)
returns boolean language sql stable set search_path='' as $$
 select exists(
   select 1 from relay_private.number_pool_attempts a
   where a.pool_id=pool and a.company_key=company and a.prospect<>target and not a.cancelled
     and (a.submitted_at >= (date_trunc('day',now() at time zone zone) at time zone zone)
          or (a.ended_at is null and a.expires_at>now()))
 )
$$;
revoke all on function relay_private.company_already_called(uuid,text,text,text) from public,anon,authenticated;

create function relay_private.reserve_for_company(request_ref text,target text,preferred text,
 preferred_is_bound boolean,store uuid,installation uuid,company jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid());
 pool uuid; result jsonb; key text:=company->>'key'; zone text:=company->>'zone';
 requested boolean:=company->'requested'='true'::jsonb;
begin
 if g.id is not null and not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
 then return '{"reason":"unavailable"}'; end if;
 if key is null or key !~ '^[0-9a-f]{32}$' or zone is null or length(zone)>100
  or not exists(select 1 from pg_catalog.pg_timezone_names where name=zone)
 then raise exception 'Invalid company calling scope'; end if;
 select p.id into pool from relay_private.number_pools p
 join relay_private.number_pool_members m on m.pool_id=p.id
 join public.relay_workspaces w on w.id=m.workspace_id and w.active
 where m.workspace_id=auth.uid() for update of p;
 if not found then return '{"reason":"unavailable"}'; end if;
 if not coalesce(requested,false) and relay_private.company_already_called(pool,key,target,zone)
 then return '{"reason":"company"}'; end if;
 result:=relay_private.pool_reserve(request_ref,target,preferred,preferred_is_bound);
 if result ? 'phone' and not result ? 'reason' then
   update relay_private.number_pool_attempts a
   set company_key=key,company_zone=zone,company_requested=coalesce(requested,false)
   where a.pool_id=pool and a.workspace_id=auth.uid() and a.ref=request_ref
    and (a.company_key is null or (a.company_key=key and a.company_zone=zone
         and a.company_requested=coalesce(requested,false)));
   if not found then return '{"reason":"expired"}'; end if;
 end if;
 return result;
end $$;
revoke all on function relay_private.reserve_for_company(text,text,text,boolean,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function relay_private.reserve_for_company(text,text,text,boolean,uuid,uuid,jsonb) to authenticated;

-- Bind the private function at definition time, without exposing the private schema in PostgREST.
create function public.relay_pool_reserve_company(request_ref text,target text,preferred text,
 preferred_is_bound boolean default false,store uuid default null,installation uuid default null,
 company jsonb default null)
returns jsonb language sql security invoker set search_path=''
begin atomic
 select relay_private.reserve_for_company(request_ref,target,preferred,preferred_is_bound,store,installation,company);
end;
revoke all on function public.relay_pool_reserve_company(text,text,text,boolean,uuid,uuid,jsonb) from public,anon;
grant execute on function public.relay_pool_reserve_company(text,text,text,boolean,uuid,uuid,jsonb) to authenticated;

-- Recheck at submission in case an exempt requested call joined the company after reservation.
create or replace function relay_private.pool_submit(request_ref text, local_call text, number text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare pool uuid; held relay_private.number_pool_attempts; limit_today int;
begin
 if local_call is null or local_call !~ '^[A-Za-z0-9-]{1,64}$' then raise exception 'Invalid call identity'; end if;
 select p.id into pool from relay_private.number_pools p
 join relay_private.number_pool_members m on m.pool_id=p.id
 join public.relay_workspaces w on w.id=auth.uid() and w.active
 where m.workspace_id=auth.uid() for update of p;
 if not found then return '{"reason":"unavailable"}'::jsonb; end if;
 select * into held from relay_private.number_pool_attempts a where a.workspace_id=auth.uid()
 and a.pool_id=pool and a.ref=request_ref and a.phone=number and not a.cancelled and a.expires_at>now();
 if not found then return '{"reason":"expired"}'::jsonb; end if;
 if held.submitted_at is not null then return '{"reason":"expired"}'::jsonb; end if;
 if held.company_key is not null and not held.company_requested
  and relay_private.company_already_called(pool,held.company_key,held.prospect,held.company_zone)
 then return '{"reason":"company"}'; end if;
 select daily_limit into limit_today from relay_private.number_pool_numbers n
 where n.pool_id=pool and n.phone=number and n.outbound_enabled;
 if not found or relay_private.pool_load(pool,number)>limit_today then return '{"reason":"capacity"}'::jsonb; end if;
 update relay_private.number_pool_attempts set submitted_at=now(),call_ref=local_call,expires_at=now()+interval '2 hours 2 minutes'
 where workspace_id=auth.uid() and ref=request_ref;
 return '{"submitted":true}'::jsonb;
end $$;
