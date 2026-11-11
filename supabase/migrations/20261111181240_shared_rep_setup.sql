-- A shared invitation adds the rep and their segment atomically. The application is registered once by the operator.
alter table public.relay_invitations
 add column shared_pool_id uuid references relay_private.number_pools(id) on delete cascade,
 add column shared_segment_id text check(shared_segment_id ~ '^[0-9]{1,40}$'),
 add check((shared_pool_id is null)=(shared_segment_id is null));
alter function public.relay_activate(text) rename to relay_activate_legacy;

create function public.relay_activate(invitation text) returns void
language plpgsql security invoker set search_path='' as $$
declare i public.relay_invitations; g relay_private.number_pools; sip text; credential text;
begin
 select * into i from public.relay_invitations where hash=invitation and expires_at>now() for update;
 if not found then raise exception 'Invitation unavailable'; end if;
 if i.shared_pool_id is null then
  perform public.relay_activate_legacy(invitation);
  return;
 end if;
 select * into g from relay_private.number_pools where id=i.shared_pool_id for update;
 sip:=i.settings->>'sip_username';
 credential:=i.settings->>'telephony_credential_id';
 if g.shared_connection_id is null or i.settings->>'telnyx_connection_id' is distinct from g.shared_connection_id
  or i.routes is distinct from '[]'::jsonb or sip is null or sip !~ '^[A-Za-z0-9_.+-]{1,128}$'
  or credential is null or credential !~ '^[A-Za-z0-9-]{1,128}$'
 then raise exception 'Invalid shared invitation'; end if;
 if exists(select 1 from relay_private.number_pool_members m join public.relay_workspaces w on w.id=m.workspace_id
  where m.pool_id=g.id and m.workspace_id<>i.workspace_id and
   (m.segment_id=i.shared_segment_id or w.settings->>'sip_username'=sip or
    w.settings->>'telephony_credential_id'=credential))
 then raise exception 'Segment or audio identity already assigned'; end if;
 insert into public.relay_workspaces(id,label,settings) values(i.workspace_id,i.label,i.settings) on conflict(id) do nothing;
 if not exists(select 1 from public.relay_workspaces w where w.id=i.workspace_id and w.active and w.settings=i.settings)
 then raise exception 'Workspace settings already assigned'; end if;
 insert into relay_private.number_pool_members(workspace_id,pool_id,segment_id)
 values(i.workspace_id,g.id,i.shared_segment_id) on conflict(workspace_id) do nothing;
 if not exists(select 1 from relay_private.number_pool_members m where m.workspace_id=i.workspace_id
  and m.pool_id=g.id and m.segment_id=i.shared_segment_id)
 then raise exception 'Workspace already belongs to another segment or pool'; end if;
end $$;
revoke all on function public.relay_activate(text) from public,anon,authenticated;
grant execute on function public.relay_activate(text) to service_role;
