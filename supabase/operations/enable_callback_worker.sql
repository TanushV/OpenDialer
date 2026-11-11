-- Operator runs this on the intended relay after deploying callback-fallback and setting its secrets.
-- Use Vault secret names opendialer_callback_worker_url and opendialer_callback_worker_token.
-- This sends only a wake-up; call state and worker credentials are never included in the body.
create extension if not exists pg_net;
do $$
begin
 if not exists(select 1 from vault.decrypted_secrets where name='opendialer_callback_worker_url'
               and decrypted_secret ~ '^https://[a-z]{20}\.supabase\.co/functions/v1/callback-fallback$')
  or not exists(select 1 from vault.decrypted_secrets where name='opendialer_callback_worker_token'
               and length(decrypted_secret)>=32)
 then raise exception 'Set the callback worker URL and token in Vault first'; end if;
end $$;

select cron.schedule('opendialer-callback-worker','5 seconds',$job$
 select net.http_post(
  url:=(select decrypted_secret from vault.decrypted_secrets where name='opendialer_callback_worker_url'),
  headers:=jsonb_build_object('Content-Type','application/json','x-opendialer-worker-token',
   (select decrypted_secret from vault.decrypted_secrets where name='opendialer_callback_worker_token')),
  body:='{}'::jsonb, timeout_milliseconds:=30000)
 where exists(select 1 from relay_private.shared_callbacks c
  join relay_private.number_pools g on g.id=c.pool_id and g.fallback_enabled
  where c.handler<>'done' and relay_private.callback_due(c));
$job$);
