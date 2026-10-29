-- Run against a disposable project containing the signed webhook test fixtures.
-- All lease/ack changes roll back, including negative tests.
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub','d1273570-3838-4900-96b3-c8ff8f9d9471',true);
do $$
declare r jsonb;
begin
 if (select count(*) from public.relay_workspaces)<>1 then raise exception 'Workspace isolation failed';end if;
 if (select count(*) from public.relay_routes)<>1 then raise exception 'Route isolation failed';end if;
 r:=public.relay_exchange('60cf2bdb-60f0-4ce4-92c5-1f92ed23d14a','648ff653-f277-42a5-a94e-7029a76111ee');
 if jsonb_array_length(r->'events')<>2 then raise exception 'Pending isolation failed: %',r;end if;
 if exists(select 1 from jsonb_array_elements(r->'events') e where e->>'event_id'='event-B') then raise exception 'Cross workspace event';end if;
 begin
 perform public.relay_exchange('60cf2bdb-60f0-4ce4-92c5-1f92ed23d14a','a17787a2-6d4d-47fb-b0f2-3e14d6b6d59c');
 raise exception 'Second consumer accepted';
 exception when others then if sqlerrm <> 'Workspace active on another computer' then raise;end if;end;
 r:=public.relay_exchange('60cf2bdb-60f0-4ce4-92c5-1f92ed23d14a','648ff653-f277-42a5-a94e-7029a76111ee','[{"account_id":"synthetic-check-account","event_id":"event-B"}]');
 if jsonb_array_length(r->'events')<>2 then raise exception 'Foreign ack changed batch';end if;
 r:=public.relay_exchange('60cf2bdb-60f0-4ce4-92c5-1f92ed23d14a','648ff653-f277-42a5-a94e-7029a76111ee','[{"account_id":"synthetic-check-account","event_id":"event-A"}]');
 if jsonb_array_length(r->'events')<>1 then raise exception 'Own ack failed';end if;
 if has_table_privilege('authenticated','public.relay_events','INSERT') then raise exception 'Client insertion permitted';end if;
 if has_function_privilege('authenticated','public.relay_receive(jsonb)','EXECUTE') then raise exception 'Client can forge receipt';end if;
end $$;
select set_config('request.jwt.claim.sub','e58ed79e-a708-48a0-8940-f8d37f577ee0',true);
do $$declare r jsonb;begin
 r:=public.relay_exchange('a649d366-05c8-4300-a5a4-451e42dac971','18515f18-965a-448e-b3b6-9197e7a48d26');
 if jsonb_array_length(r->'events')<>1 or r->'events'->0->>'event_id'<>'event-B' then raise exception 'Other workspace changed';end if;
end $$;
rollback;
