import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ArrowRight, BookOpen, ChartNoAxesCombined, Check, ChevronRight, CircleHelp, Headphones, LayoutDashboard, List, LoaderCircle, LogOut, Phone, Radio, Settings, ShieldCheck, Signal, Users, Wifi, X } from 'lucide-react';
import { Context } from './context';
import { clearSession, loadSession, localRuntime, native, request, saveSession, validateServer, waitForRuntime, readCache, writeCache } from './api/client';
import type { Runtime, Session, Workspace } from './types';
import { DialerPage } from './components/DialerPage';
import { LeadsPage, NumbersPage, IntelligencePage } from './components/Operations';
import { ReviewPage } from './components/ReviewPage';
import { SettingsPage } from './components/SettingsPage';

type View = 'Dialer' | 'Leads' | 'Review' | 'Numbers' | 'Intelligence' | 'Settings';
const navigation = [{ title: 'Dialer', icon: Phone }, { title: 'Leads', icon: Users }, { title: 'Review', icon: BookOpen },
  { title: 'Numbers', icon: Radio }, { title: 'Intelligence', icon: ChartNoAxesCombined }, { title: 'Settings', icon: Settings }] as const;
export function Brand() { return <div className="brand"><span className="brand-symbol"><Phone size={19} strokeWidth={2.6}/></span><span>OpenDialer<span className="brand-dot">.</span></span></div>; }

export function App() {
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [snapshot, setSnapshot] = useState<Workspace | null>(null);
  const [view, setView] = useState<View>('Dialer');
  const [offline, setOffline] = useState(false);
  const [connection, setConnection] = useState<'local' | 'connected'>('local');
  const [startup, setStartup] = useState(true);
  const [working, setWorking] = useState(false);
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null);
  const [welcomeMode, setWelcomeMode] = useState<'choose' | 'local' | 'connected'>('choose');
  const [form, setForm] = useState({ seller: '', name: '', server: '', email: '', password: '' });
  const refreshLock = useRef(false);
  const api = useCallback(async <T,>(path: string, method?: string, body?: unknown): Promise<T> => {
    if (!runtime || !session) throw new Error('Sign in to a workspace first.');
    const cacheable = !method || method === 'GET';
    const packetMatch = path.match(/^\/v1\/leads\/([^/]+)\/packet$/);
    const analysisMatch = path.match(/^\/v1\/calls\/([^/]+)\/analysis$/);
    const key = packetMatch ? `packet-${session.user_id}-${packetMatch[1]}` : analysisMatch ? `analysis-${session.user_id}-${analysisMatch[1]}` : null;
    try {
      const result = await request<T>(runtime.url, session.access_token, path, method, body);
      if (cacheable && key) void writeCache(key, result, runtime.simulation).catch(() => {});
      return result;
    } catch (error) {
      if (cacheable && key) { const cached = await readCache(key, runtime.simulation); if (cached) return JSON.parse(cached) as T; }
      throw error;
    }
  }, [runtime, session]);
  const refresh = useCallback(async () => {
    if (!session || !runtime || refreshLock.current) return;
    refreshLock.current = true;
    try {
      const result = await request<Workspace>(runtime.url, session.access_token, '/v1/workspace');
      setSnapshot(result); setOffline(false);
      void writeCache(`workspace-${session.user_id}`, { ...result, leads: result.leads.slice(0, 20) }, result.simulation).catch(() => {});
    } catch (error) { setOffline(true); throw error; }
    finally { refreshLock.current = false; }
  }, [runtime, session]);
  const notice = useCallback((message: string) => setToast({ message }), []);
  const run = useCallback(async (work: () => Promise<void>) => { try { await work(); } catch (e) { setToast({ message: e instanceof Error ? e.message : 'The operation failed.', error: true }); } }, []);

  useEffect(() => { void (async () => {
    try {
      const saved = await loadSession();
      if (native() && !saved) { setStartup(false); return; }
      if (saved?.connection === 'connected' && saved.url && saved.session) {
        await validateServer(saved.url);
        let snap: Workspace;
        try { snap = await request<Workspace>(saved.url, saved.session.access_token, '/v1/workspace'); }
        catch (error) {
          const cached = await readCache(`workspace-${saved.session.user_id}`, saved.simulation);
          if (!cached) throw error;
          snap = JSON.parse(cached) as Workspace;
          snap.user.presence = { status: 'paused', audio_ready: false };
          snap.readiness = { ...snap.readiness, ready_to_call: false, blockers: ['AUTHORITY_OFFLINE'] };
          setOffline(true);
        }
        setConnection('connected'); setRuntime({ url: saved.url, local_token: '', simulation: snap.simulation }); setSession(saved.session); setSnapshot(snap);
      } else {
        const local = await localRuntime(saved?.simulation ?? false);
        setRuntime(local);
        const health = await waitForRuntime(local);
        if (health.initialized) {
          const token = await request<Session>(local.url, null, '/v1/local/session', 'POST', {}, { 'X-Local-Token': local.local_token });
          setSession(token); setSnapshot(await request<Workspace>(local.url, token.access_token, '/v1/workspace'));
        }
      }
    } catch (e) { setToast({ message: e instanceof Error ? e.message : 'Cannot restore the workspace.', error: true }); }
    finally { setStartup(false); }
  })(); }, []);
  useEffect(() => { if (!session) return; void run(refresh); const id = setInterval(() => { void refresh().catch(() => {}); }, 2500); return () => clearInterval(id); }, [session, refresh, run]);
  useEffect(() => { if (!toast || toast.error) return; const id = setTimeout(() => setToast(null), 5000); return () => clearTimeout(id); }, [toast]);

  async function beginLocal(simulation: boolean) {
    setWorking(true);
    await run(async () => {
      const local = await localRuntime(simulation); setRuntime(local);
      const health = await waitForRuntime(local);
      let token: Session;
      if (health.initialized) token = await request(local.url, null, '/v1/local/session', 'POST', {}, { 'X-Local-Token': local.local_token });
      else token = await request(local.url, null, '/v1/setup', 'POST', { seller_name: simulation ? 'Example Seller' : form.seller,
        campaign_name: simulation ? 'A better next conversation' : 'My first campaign', name: simulation ? 'Alex Morgan' : form.name || 'Local operator',
        email: `local-${crypto.randomUUID()}@opendialer.local`, password: crypto.randomUUID()+crypto.randomUUID() }, { 'X-Local-Token': local.local_token });
      if (simulation) await request(local.url, token.access_token, '/v1/simulation/seed', 'POST');
      await saveSession({ connection: 'local', simulation });
      setConnection('local'); setSession(token); setSnapshot(await request(local.url, token.access_token, '/v1/workspace')); setView('Dialer');
    });
    setWorking(false);
  }
  async function signIn() {
    setWorking(true);
    await run(async () => {
      await validateServer(form.server);
      const token = await request<Session>(form.server, null, '/v1/auth/session', 'POST', { email: form.email, password: form.password });
      const snap = await request<Workspace>(form.server, token.access_token, '/v1/workspace');
      setRuntime({ url: form.server, local_token: '', simulation: snap.simulation }); setSession(token); setSnapshot(snap); setConnection('connected');
      await saveSession({ connection: 'connected', simulation: snap.simulation, url: form.server, session: token });
      setForm({ ...form, password: '' }); setView('Dialer');
    }); setWorking(false);
  }
  async function disconnect() {
    if (runtime && session) await request(runtime.url, session.access_token, '/v1/auth/logout', 'POST').catch(() => {});
    await clearSession(); setSession(null); setSnapshot(null); setWelcomeMode('choose'); setToast(null);
  }
  const field = (key: keyof typeof form) => ({ value: form[key], onChange: (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: e.target.value }) });
  const message = toast && <div role={toast.error ? 'alert' : 'status'} className={`toast ${toast.error ? 'error' : ''}`}><span>{toast.message}</span><button aria-label="Dismiss notification" onClick={() => setToast(null)}><X size={17}/></button></div>;
  if (startup) return <div className="startup"><Brand/><LoaderCircle className="spin"/><p>Opening your workspace…</p><small>Starting the private local service</small>{message}</div>;
  if (!snapshot || !session) return <div className="welcome"><aside className="welcome-story"><Brand/><div><span className="eyebrow light">A LITTLE LESS FRICTION. A BETTER CONVERSATION.</span><h1>Make the call.<br/><em>Be present.</em></h1><p>Your leads, research, and next steps.<br/>A quiet assistant when you need one.</p><div className="welcome-mark"><Signal size={82} strokeWidth={1}/><span>Built around the human<br/>on both ends of the line.</span></div></div><footer><ShieldCheck size={16}/> Your workspace. Your calling identity.</footer></aside><main className="welcome-actions"><span className="version">DESKTOP · 0.1.0</span><div className="welcome-card">
    {welcomeMode === 'choose' ? <><span className="eyebrow">WELCOME TO OPENDIALER</span><h2>A workspace that<br/>works your way.</h2><p className="muted">Keep everything on this computer, or join your team's connected workspace.</p><button className="choice" onClick={() => setWelcomeMode('local')}><span className="choice-icon"><LayoutDashboard/></span><span><strong>Start a local workspace</strong><small>Private storage. Your own provider accounts.</small></span><ChevronRight/></button><button className="choice" onClick={() => setWelcomeMode('connected')}><span className="choice-icon"><Wifi/></span><span><strong>Connect to a server</strong><small>Shared leads, numbers, and team learning.</small></span><ChevronRight/></button><div className="welcome-divider"><span>JUST EXPLORING?</span></div><button className="primary wide" disabled={working} onClick={() => void beginLocal(true)}>{working ? <LoaderCircle className="spin" size={18}/> : <Activity size={18}/>}Try a simulated workspace<ArrowRight size={17}/></button><p className="fine">Fictional leads and simulated calls. No API keys required.</p></> : welcomeMode === 'connected' ? <form onSubmit={e => { e.preventDefault(); void signIn(); }}><button type="button" className="text-button back" onClick={() => setWelcomeMode('choose')}>← Back</button><span className="eyebrow">YOUR TEAM WORKSPACE</span><h2>Pick up where<br/>your team left off.</h2><label>Server URL<input {...field('server')} type="url" placeholder="https://dialer.example.com" required/></label><label>Email<input {...field('email')} type="email" autoComplete="username" required/></label><label>Password<input {...field('password')} type="password" autoComplete="current-password" required/></label><button className="primary wide" disabled={working}>Sign in<ArrowRight size={17}/></button></form> : <form onSubmit={e => { e.preventDefault(); void beginLocal(false); }}><button type="button" className="text-button back" onClick={() => setWelcomeMode('choose')}>← Back</button><span className="eyebrow">ON THIS COMPUTER</span><h2>Give your workspace<br/>a name.</h2><p className="muted">The app keeps your database here. Connect Telnyx and optional inference providers when you are ready.</p><label>Business name<input {...field('seller')} required placeholder="Your business"/></label><label>Your name<input {...field('name')} required placeholder="Your name"/></label><button className="primary wide" disabled={working}>Create local workspace<ArrowRight size={17}/></button></form>}
    </div><small className="welcome-footer">Thoughtful software for the work between hello and what's next.</small></main>{message}</div>;

  return <Context.Provider value={{ snapshot, session, api, refresh, notice, run }}><div className="app-shell"><aside className="sidebar"><Brand/><div className="workspace-label"><span className="avatar">{snapshot.seller.name.slice(0, 1)}</span><span><strong>{snapshot.seller.name}</strong><small>{connection === 'connected' ? 'Team workspace' : 'On this computer'}</small></span><ChevronRight size={14}/></div><div className="nav-caption">WORKSPACE</div><nav>{navigation.map(({ title, icon: Icon }) => <button key={title} aria-label={title} className={view === title ? 'active' : ''} onClick={() => setView(title)}><Icon size={18}/>{title}{title === 'Leads' && <span className="nav-count">{snapshot.leads.length}</span>}</button>)}</nav><div className="sidebar-bottom"><div className="safety-note"><ShieldCheck size={19}/><div><strong>You stay in control.</strong><p>One live conversation.<br/>Your voice, your judgment.</p></div></div><button className="profile" onClick={() => void disconnect()} title="Sign out"><span className="avatar small-avatar">{snapshot.user.name.slice(0, 1)}</span><span><strong>{snapshot.user.name}</strong><small>{snapshot.user.role}</small></span><LogOut size={15}/></button></div></aside><main className="app-main"><header className="topbar"><div className="breadcrumb">Workspace<ChevronRight size={12}/><strong>{view}</strong></div><div className="connection"><span className={`status-dot ${snapshot.readiness.integrity_ok ? '' : 'danger'}`}/><span>{offline ? 'Offline cache' : connection === 'connected' ? 'Connected' : 'Local workspace'}</span>{snapshot.simulation && <span className="simulation-badge">SIMULATION</span>}<span className="divider"/><Headphones size={16}/><small>{snapshot.user.presence.audio_ready ? (snapshot.simulation ? 'Simulated audio' : 'Audio ready') : 'Audio not ready'}</small></div></header>{snapshot.simulation && <div className="simulation-strip"><Activity size={14}/><span>Practice workspace · Calls and provider responses are simulated · Clock: Oct 14, 11:00 AM Pacific</span></div>}<div className="page-body">{offline && <div className="alert">The shared authority is unavailable. Cached scripts and notes remain readable. New calling requires the authority to reconnect.</div>}{view === 'Dialer' ? <DialerPage/> : view === 'Leads' ? <LeadsPage/> : view === 'Review' ? <ReviewPage/> : view === 'Numbers' ? <NumbersPage/> : view === 'Intelligence' ? <IntelligencePage/> : <SettingsPage onDisconnect={() => void disconnect()}/>}</div></main>{message}</div></Context.Provider>;
}
