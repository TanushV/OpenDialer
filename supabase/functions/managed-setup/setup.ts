type Invite = { workspace_id: string; label: string };
type Services = {
  lookup: (hash: string) => Promise<Invite | null>;
  create: (id: string, email: string, password: string) => Promise<void>;
  activate: (hash: string) => Promise<void>;
  url: string; publicKey: string;
};
export function createSetup(service: Services) {
  return async (request: Request) => {
    if (request.method !== "POST") return new Response("Method not allowed", {status:405});
    try {
      const body = await request.text();
      if (body.length > 1024) return new Response("Invalid request", {status:400});
      const { token } = JSON.parse(body);
      if (typeof token !== "string" || !/^[A-Za-z0-9_-]{43,128}$/.test(token)) return new Response("Invalid invitation", {status:400});
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)))].map(x=>x.toString(16).padStart(2,"0")).join("");
      const invite = await service.lookup(hash);
      if (!invite) return new Response("Invitation unavailable", {status:403});
      const email = `${invite.workspace_id}@opendialer.invalid`;
      await service.create(invite.workspace_id, email, token);
      await service.activate(hash);
      return Response.json({version:1,label:invite.label,url:service.url,publishable_key:service.publicKey,workspace_id:invite.workspace_id,email,password:token}, {headers:{"Cache-Control":"no-store"}});
    } catch { return new Response("Setup temporarily unavailable", {status:503}); }
  };
}
