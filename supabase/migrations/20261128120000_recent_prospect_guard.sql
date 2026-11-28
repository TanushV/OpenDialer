-- Call insights plan P5 step 8: two reps who import the same list must not call one person twice in a day.
-- Read-only: whether another rep's computer placed a confirmed call to this prospect in the last 24 hours, from the
-- sticky caller ID binding the relay already keeps. Older apps never call it, so nothing changes for them.
create function relay_private.pool_recent(target text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare pool uuid; other uuid; placed timestamptz;
begin
 if target is null or target !~ '^\+1[0-9]{10}$' then
  raise exception 'Invalid prospect';
 end if;
 select m.pool_id into pool from relay_private.number_pool_members m
 join public.relay_workspaces w on w.id=m.workspace_id and w.active
 where m.workspace_id=auth.uid();
 if pool is null then return '{}'::jsonb; end if;
 select b.last_workspace_id,b.last_submitted_at into other,placed
 from relay_private.number_pool_bindings b where b.pool_id=pool and b.prospect=target;
 if other is not null and other<>auth.uid() and placed>now()-interval '24 hours' then
  return jsonb_build_object('other_rep_at',placed);
 end if;
 return '{}'::jsonb;
end $$;

create function public.relay_pool_recent(target text,store uuid default null,installation uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare g relay_private.number_pools:=relay_private.shared_pool_for(auth.uid());
begin
 if g.id is not null and not relay_private.owns_pool_lease(g.shared_connection_id,store,installation)
 then return '{"reason":"unavailable"}'; end if;
 return relay_private.pool_recent(target);
end $$;

revoke all on function relay_private.pool_recent(text) from public,anon,authenticated;
revoke all on function public.relay_pool_recent(text,uuid,uuid) from public,anon;
grant execute on function public.relay_pool_recent(text,uuid,uuid) to authenticated;
