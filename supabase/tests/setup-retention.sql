-- Disposable assertions, entirely rolled back. Uses the provisioned workspace only as a FK.
begin;
do $$
declare w uuid; receipt jsonb;
begin
 select id into strict w from public.relay_workspaces where label='OpenDialer Mac';
 insert into public.relay_events(account_id,event_id,workspace_id,hash,event,received_at)
 values('test-retention','old-control',w,'test-hash','{"id":"old-control","payload":{"to":"+14155550123"}}',now()-interval '25 hours');
 perform relay_private.cleanup();
 if (select gap_since from public.relay_workspaces where id=w) is null then raise exception 'Pending expiration was hidden'; end if;
 if (select event from public.relay_events where account_id='test-retention') <> '{"id":"old-control"}'::jsonb then raise exception 'Expired payload retained'; end if;
end $$;
select set_config('request.jwt.claim.sub',(select id::text from public.relay_workspaces where label='OpenDialer Mac'),true);
set local role authenticated;
do $$
declare result jsonb;
begin
 result := public.relay_exchange(gen_random_uuid(),gen_random_uuid(),'[]','');
 if not (result ? 'numbers' and result ? 'gap_since') then raise exception 'Profile metadata missing'; end if;
 if result->>'gap_since' is null then raise exception 'Gap not visible to caller'; end if;
 if result->'events'<>'[]'::jsonb then raise exception 'Expired event delivered'; end if;
 perform public.relay_clear_gap();
 if exists(select 1 from public.relay_workspaces where gap_since is not null) then raise exception 'Gap not cleared'; end if;
 begin
  perform public.relay_activate('not-an-invitation');
  raise exception 'Client could provision routes';
 exception when insufficient_privilege then null;
 end;
end $$;
reset role;
select set_config('request.jwt.claim.sub',gen_random_uuid()::text,true);
set local role authenticated;
do $$
begin
 if exists(select 1 from public.relay_workspaces) or exists(select 1 from public.relay_routes) or exists(select 1 from public.relay_numbers) then raise exception 'Cross-user metadata leaked'; end if;
end $$;
rollback;
