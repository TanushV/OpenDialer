import { createSetup } from "./setup.ts";
const url = Deno.env.get("SUPABASE_URL")!;
const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const headers = {apikey:key,Authorization:`Bearer ${key}`,"Content-Type":"application/json"};
async function api(path: string, options: RequestInit = {}) {
  return fetch(url+path,{...options,headers,signal:AbortSignal.timeout(10000)});
}
Deno.serve(createSetup({
  url,
  publicKey: "sb_publishable_302qvobB2fOlxbu6UZWAfA_X-J7WmJi",
  lookup: async hash => {
    const r=await api(`/rest/v1/relay_invitations?hash=eq.${hash}&expires_at=gt.${encodeURIComponent(new Date().toISOString())}&select=workspace_id,label`);
    if (!r.ok) throw Error("lookup failed");
    return (await r.json())[0] || null;
  },
  create: async (id,email,password) => {
    const found=await api(`/auth/v1/admin/users/${id}`);
    if (found.ok) {
      const user=await found.json();
      if (user.email !== email) throw Error("identity mismatch");
      return;
    }
    if (found.status !== 404) throw Error("identity unavailable");
    const r=await api('/auth/v1/admin/users',{method:'POST',body:JSON.stringify({id,email,password,email_confirm:true})});
    if (!r.ok) throw Error("creation failed");
    const user=await r.json();
    if (user.id !== id) throw Error("identity mismatch");
  },
  activate: async hash => {
    const r=await api('/rest/v1/rpc/relay_activate',{method:'POST',body:JSON.stringify({invitation:hash})});
    if (!r.ok) throw Error("activation failed");
  },
}));
