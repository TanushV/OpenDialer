import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createSetup, profileEndpoint } from './setup.ts';
const request = token => new Request('https://test/setup', {method:'POST',body:JSON.stringify({token})});
test('missing and unknown invitations cannot create users', async () => {
  let created = 0;
  const handler = createSetup({lookup:async()=>null, create:async()=>{created++}, activate:async()=>{}, url:'test', publicKey:'public'});
  assert.equal((await handler(request('x'))).status, 400);
  assert.equal((await handler(request('x'.repeat(48)))).status, 403);
  assert.equal(created,0);
});
test('profile uses only operator-bound metadata and never admin credentials', async () => {
  const calls=[];
  const handler = createSetup({lookup:async()=>({workspace_id:'id',label:'Caller'}), create:async(id,email,password)=>calls.push({id,email,password}), activate:async(hash)=>calls.push(hash), url:'https://project.supabase.co',publicKey:'sb_publishable_test'});
  const response=await handler(request('x'.repeat(48)));
  assert.equal(response.status,200);
  const profile=await response.json();
  assert.equal(profile.workspace_id,'id');
  assert.equal(profile.password,'x'.repeat(48));
  assert.equal(calls[0].id,'id');
  assert.equal(calls[1].length,64);
  assert.equal(profile.publishable_key,'sb_publishable_test');
});
test('failed activation never returns a working setup claim', async () => {
  const handler=createSetup({lookup:async()=>({workspace_id:'id',label:'Caller'}),create:async()=>{},activate:async()=>{throw Error('private provider body')},url:'test',publicKey:'public'});
  const response=await handler(request('x'.repeat(48)));
  assert.equal(response.status,503);
  assert.ok(!(await response.text()).includes('private provider body'));
});
test('setup profiles use the target project URL and its publishable key', () => {
  const env = {
    SUPABASE_URL: 'http://internal-gateway:8000',
    SUPABASE_PUBLISHABLE_KEYS: JSON.stringify({default:'sb_publishable_stage',other:'sb_publishable_other'}),
    OPENDIALER_RELAY_PUBLIC_URL: 'https://stagestagestagestage.supabase.co',
  };
  assert.deepEqual(profileEndpoint(key => env[key]), {
    url: env.OPENDIALER_RELAY_PUBLIC_URL, publicKey: 'sb_publishable_stage',
  });
  assert.throws(() => profileEndpoint(key => ({...env, OPENDIALER_RELAY_PUBLISHABLE_KEY:'sb_secret_no'})[key]));
  assert.throws(() => profileEndpoint(key => ({...env, OPENDIALER_RELAY_PUBLIC_URL:undefined})[key]));
});
