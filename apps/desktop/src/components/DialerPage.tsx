import { useEffect, useRef, useState } from 'react';
import { ArrowRight, CalendarClock, Check, ChevronRight, CircleHelp, Clock3, FileText, Globe2, Headphones, Layers3, Lightbulb, LoaderCircle, Mic, MicOff, MoreHorizontal, Pause, Phone, PhoneOff, Play, Plus, RefreshCw, ShieldCheck, Sparkles, Volume2, X } from 'lucide-react';
import { useWorkspace, date, label } from '../context';
import type { Call, Packet, Reservation } from '../types';
import { terminal } from '../types';
import { openSource, native, readCache, writeCache } from '../api/client';
import { OptOutJournal, MemoryStorage, type OptOut } from '../state/optouts';
import { Telephony } from '../telephony/client';

export function DialerPage() {
  const { snapshot: w, session, api, refresh, notice, run } = useWorkspace();
  const [reservation, setReservation] = useState<Reservation | null>(null);
  const [packet, setPacket] = useState<Packet | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [dialing, setDialing] = useState(false);
  const [mute, setMute] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [outcome, setOutcome] = useState('right_party');
  const [callbackAt, setCallbackAt] = useState('');
  const [pauseAfter, setPauseAfter] = useState(false);
  const [keypad, setKeypad] = useState(false);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [audioStatus, setAudioStatus] = useState('');
  const [showDevices, setShowDevices] = useState(false); const [vmAsset, setVMAsset] = useState('');
  const audio = useRef<HTMLAudioElement>(null);
  const telephony = useRef(new Telephony());
  const journalStore = useRef(native() ? new MemoryStorage() : localStorage);
  const journal = useRef(new OptOutJournal(journalStore.current, session.tenant_id));
  const [journalReady, setJournalReady] = useState(!native());
  const [pendingOptouts, setPendingOptouts] = useState(journal.current.blocksCalling);
  const ready = w.user.presence.status === 'ready';
  const active = !terminal(call);
  const selectedLead = packet?.lead;

  async function reloadCall(id: string) { setCall(await api<Call>(`/v1/calls/${id}`)); }
  useEffect(() => { const current = w.calls.find(c => !terminal(c)); if (current && !call) { setCall(current); void api<Packet>(`/v1/leads/${current.lead_id}/packet`).then(setPacket); } }, [w.calls]);
  useEffect(() => {
    if (!call) return;
    const id = setInterval(() => { void reloadCall(call.id).catch(() => setAudioStatus('Authority unavailable; calling is paused.')); }, 800);
    return () => clearInterval(id);
  }, [call?.id]);
  useEffect(() => { telephony.current.onStatus = status => { setAudioStatus(status); if (status === 'agent-disconnected' || status === 'reconnecting') void api('/v1/rep/presence', 'POST', { status: 'paused', audio_ready: false, telephony_ready: false }).catch(() => {}); }; }, [api]);
  useEffect(() => { const handler = (e: KeyboardEvent) => { if (e.key === 'F2') { e.preventDefault(); telephony.current.mute(); setMute(v => !v); } if (e.key === 'F4' && call && active) { e.preventDefault(); void run(hangup); } if (e.key === 'F1' && call) { e.preventDefault(); void run(async () => { await api(`/v1/calls/${call.id}/coach`, 'POST'); await reloadCall(call.id); }); } }; window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler); }, [call, active]);

  async function persistJournal() { await writeCache(`optouts-${session.tenant_id}`, journal.current.pending, w.simulation); }
  async function syncOptouts() {
    await journal.current.synchronize(event => api('/v1/suppressions', 'POST', { phone: event.phone, reason: event.reason, event_id: event.id }));
    await persistJournal();
    setPendingOptouts(journal.current.blocksCalling);
  }
  useEffect(() => {
    void run(async () => {
      if (native()) {
        const saved = await readCache(`optouts-${session.tenant_id}`, w.simulation);
        if (saved) {
          const items = JSON.parse(saved) as OptOut[];
          if (!Array.isArray(items) || items.some(e => !e.phone || !e.id)) throw new Error('Protected opt-out journal needs recovery; calling remains paused.');
          journalStore.current.setItem(`opendialer.optouts.${session.tenant_id}`, JSON.stringify(items));
        }
      }
      setPendingOptouts(journal.current.blocksCalling);
      await syncOptouts(); setJournalReady(true);
    });
  }, []);
  useEffect(() => { if (!journalReady) return; void syncOptouts(); const handler = () => { void syncOptouts(); }; window.addEventListener('online', handler); return () => window.removeEventListener('online', handler); }, [journalReady]);
  async function goReady() {
    if (!journalReady) throw new Error('Wait for the protected opt-out journal to open.');
    await syncOptouts();
    if (journal.current.blocksCalling) throw new Error('Sync the pending opt-out before further outbound calls.');
    let agent = 'agent-simulated';
    if (!w.simulation) {
      setAudioStatus('Checking microphone and registering…');
      const result = await api<{ token: string }>('/v1/telephony/token', 'POST');
      await telephony.current.connect(result.token, audio.current!);
      telephony.current.expectAgentLeg();
      const leg = await api<{ agent_call_control_id: string }>('/v1/telephony/agent', 'POST');
      agent = leg.agent_call_control_id;
      let answered = false;
      for (let i = 0; i < 60; i++) {
        const status = await api<{ state: string }>(`/v1/telephony/agent/${encodeURIComponent(agent)}`);
        if (status.state === 'answered') { answered = true; break; }
        await new Promise(r => setTimeout(r, 500));
      }
      if (!answered) throw new Error('The verified agent audio leg did not become ready.');
    }
    await api('/v1/rep/presence', 'POST', { status: 'ready', audio_ready: true, telephony_ready: true, agent_call_control_id: agent, pending_optouts: 0 });
    setAudioStatus(w.simulation ? 'Simulated audio ready' : 'Audio ready'); await refresh();
  }
  async function pause() { await api('/v1/rep/presence', 'POST', { status: 'paused', audio_ready: w.user.presence.audio_ready ?? false, telephony_ready: false }); await refresh(); }
  async function getNext() {
    if (pendingOptouts) throw new Error('A pending opt-out must synchronize before another call.');
    const next = await api<Reservation>('/v1/queue/next', 'POST', { campaign_id: w.campaign.id });
    setReservation(next); setPacket(await api(`/v1/leads/${next.lead.id}/packet`)); setCall(null); setMute(false);
  }
  async function startCall() {
    if (!reservation || dialing || pendingOptouts) return;
    setDialing(true);
    try { setCall(await api('/v1/calls/start', 'POST', { reservation_id: reservation.reservation_id, idempotency_key: reservation.reservation_id })); await refresh(); }
    finally { setDialing(false); }
  }
  async function hangup() {
    if (!call) return;
    try { setCall(await api(`/v1/calls/${call.id}/hangup`, 'POST')); }
    catch (e) { await telephony.current.emergencyHangup(); throw e; }
    if (pauseAfter) await pause();
    await refresh();
  }
  async function optOut() {
    if (!selectedLead) return;
    journal.current.add(selectedLead.phone, 'opt_out'); setPendingOptouts(true);
    await persistJournal();
    await syncOptouts();
    if (call && active) await hangup();
    await refresh(); notice(journal.current.blocksCalling ? 'Opt-out saved locally. Outbound calling is blocked until synchronization.' : 'Opt-out saved. This endpoint is suppressed.');
  }
  async function simulate(type: string) { if (!call) return; await api('/v1/simulation/event', 'POST', { event_id: crypto.randomUUID(), event_type: type, call_id: call.id }); await reloadCall(call.id); }
  async function addTurn() {
    if (!call || !transcript.trim()) return;
    await api(`/v1/calls/${call.id}/transcript`, 'POST', { segment_id: crypto.randomUUID(), revision: 1, side: 'remote', text: transcript, is_final: true });
    setTranscript(''); await reloadCall(call.id); await refresh();
  }
  return <><audio ref={audio} autoPlay/><button className="text-button" onClick={() => void run(async () => { setShowDevices(!showDevices); if (!w.simulation) setDevices(await telephony.current.devices()); })}>Audio devices</button>{showDevices && <section className="card padded"><div className="two-column"><label>Microphone<select onChange={e => void run(() => telephony.current.input(e.target.value))}><option value="">System default</option>{devices.filter(d => d.kind === 'audioinput').map(d => <option key={d.deviceId} value={d.deviceId}>{d.label || 'Microphone'}</option>)}</select></label><label>Speaker / headset<select onChange={e => void run(() => telephony.current.output(e.target.value))}><option value="">System default</option>{devices.filter(d => d.kind === 'audiooutput').map(d => <option key={d.deviceId} value={d.deviceId}>{d.label || 'Audio output'}</option>)}</select></label></div><p className="fine">{w.simulation ? 'Simulation uses no microphone or carrier audio.' : 'Device names appear after microphone permission. Bluetooth, routing and long-call behavior need a hardware check.'}</p></section>}<div className="page-heading"><div><span className="eyebrow">{w.campaign.name}</span><h1>Your next conversation<span className="soft-dot">.</span></h1><p>Arrive prepared. Listen closely. Leave a clear next step.</p></div><button className={ready ? 'secondary ready-button' : 'primary'} disabled={pendingOptouts || !journalReady} onClick={() => void run(ready ? pause : goReady)}>{ready ? <Pause size={16}/> : <Headphones size={17}/>} {ready ? 'Pause dialing' : 'Go ready'}</button></div>
    {pendingOptouts && <div className="alert"><ShieldCheck size={18}/> A local opt-out is awaiting synchronization. New calls are blocked.<button onClick={() => void run(syncOptouts)}>Retry sync</button></div>}
    <div className="metric-row"><Metric title="LEADS IN YOUR COHORT" value={String(w.metrics.original_cohort)} detail={`${w.metrics.eligible_now} currently eligible`}/><Metric title="UNIQUE RIGHT-PARTY REACH" value={String(w.metrics.right_party_unique)} detail="Across the original cohort"/><Metric title="HELD MEETINGS" value={String(w.metrics.held_meetings)} detail="Confirmed outcomes"/><Metric title="COVERAGE OVERDUE" value={String(w.metrics.coverage_overdue)} detail="No lead left behind" last/></div>
    <div className="dialer-grid"><section className="card lead-card"><div className="card-eyebrow"><span><UsersIcon/> LEAD PACKET</span><span className="pill subtle">{selectedLead ? 'Ready to review' : 'Up next'}</span></div>{selectedLead ? <><div className="lead-identity"><span className="lead-avatar">{selectedLead.name.split(' ').map(n => n[0]).slice(0, 2).join('')}</span><div><h2 data-testid="lead-name">{selectedLead.name}</h2><p>{selectedLead.company || 'Company not provided'}</p></div></div><div className="lead-details"><p><Phone size={14}/>{selectedLead.phone}</p><p><Globe2 size={14}/>{selectedLead.timezone?.replaceAll('_', ' ') || 'Timezone needs review'}</p><p><Clock3 size={14}/>{selectedLead.attempt_count} previous attempts</p></div><div className="section-rule"/><span className="eyebrow">WHAT WE KNOW</span>{packet.research.facts.length ? packet.research.facts.map(f => <div className="fact" key={f.id}><Check size={13}/><div>{f.statement}<button className="source-link" onClick={() => void openSource(f.source_url)}>View source ↗</button></div></div>) : <p className="muted small">No verified company research yet. Use the approved introduction and ask what is relevant.</p>}<div className="lead-bottom"><ShieldCheck size={14}/><span>{selectedLead.suppressed ? 'Endpoint suppressed' : 'Permission is rechecked when dialing'}</span></div></> : <div className="empty-lead"><div className="orb"><Phone size={29} strokeWidth={1.5}/><span className="orb-spark">✦</span></div><h3>A person, not another dial.</h3><p>Your next lead arrives with context,<br/>a clear introduction, and room to listen.</p><button className="primary" disabled={!ready || active || pendingOptouts || !journalReady} onClick={() => void run(getNext)}>View next lead<ArrowRight size={16}/></button>{!ready && <small>Go ready to begin your session</small>}</div>}</section>
      <section className="card script-card"><div className="card-eyebrow"><span><FileText size={15}/> YOUR TALK TRACK</span><span className="pill subtle">{packet ? (packet.script.fallback ? 'Approved baseline' : 'Prepared') : 'Approved playbook'}</span></div><div className="script-opening"><span className="section-number">01</span><div><span className="eyebrow">OPEN WITH A LITTLE CONTEXT</span><blockquote>{packet?.script.opener || w.playbooks[0]?.opener.replace('{rep}', w.user.name) || 'Start with your name, business, and a truthful reason for calling.'}</blockquote></div></div><div className="section-rule"/><div className="script-discovery"><span className="section-number">02</span><div><span className="eyebrow">MAKE SPACE FOR THEIR STORY</span>{(packet?.script.discovery_questions || ['What would make this conversation useful for you?', 'How are you handling this today?']).map((question, i) => <p className="question" key={i}><span>{i+1}</span>{question}</p>)}</div></div><div className="screening-intro"><span><ShieldCheck size={14}/> Screening introduction</span><p>{packet?.script.screening_intro || w.playbooks[0]?.screening_intro.replace('{rep}', w.user.name)}</p></div><div className="script-footer"><Layers3 size={13}/><span>Version {packet?.script.script_version.slice(0, 12) || 'approved-v1'}</span><button className="text-button" onClick={() => notice('Flag this script in Intelligence for review before publishing a new version.')}>Flag an inaccuracy</button></div></section>
    </div><div className="live-grid"><section className="card transcript-card"><div className="card-eyebrow"><span><ActivityIcon/> LIVE CONVERSATION</span><span className="pill subtle">{call ? call.state : 'Standing by'}</span></div><div className="transcript-scroll">{call?.transcript?.length ? call.transcript.map(s => <div key={s.segment_id} className={`utterance ${s.side}`}><span>{s.side === 'rep' ? 'YOU' : s.side === 'remote' ? 'PROSPECT SIDE' : 'UNKNOWN SIDE'}</span><p>{s.text}</p></div>) : <div className="transcript-empty"><span className="audio-lines"><i/><i/><i/><i/><i/></span><p>{active ? 'Listening for permitted transcript events…' : 'The conversation lives here.'}</p><small>Speaker labels describe the audio side, not a verified identity.</small></div>}</div>{w.simulation && active && <div className="simulation-tools"><div className="simulation-buttons"><button className="small secondary" onClick={() => void run(async () => { await simulate('call.answered'); await simulate('call.machine.detection.ended:human'); })}>Simulate human answer</button><button className="small text-button" onClick={() => void run(async () => { await simulate('call.answered'); await simulate('call.machine.detection.ended:machine'); })}>Mailbox greeting</button><button className="small text-button" onClick={() => void run(() => simulate('call.machine.greeting.ended:beep_detected'))}>Mailbox beep</button></div><div className="input-row"><input aria-label="Simulation transcript" value={transcript} placeholder="Add a fictional prospect turn…" onChange={e => setTranscript(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') void run(addTurn); }}/><button className="icon-button" aria-label="Add prospect turn" onClick={() => void run(addTurn)}><Plus size={19}/></button></div></div>}</section><section className="card coach-card"><div className="card-eyebrow"><span><Sparkles size={15}/> A QUIET SECOND PAIR OF EARS</span><span className="tiny-light"/></div>{call?.coach ? <div className="coach-guidance"><span className="eyebrow">CONSIDER ASKING</span><h3>“{call.coach.say_or_ask}”</h3><p>{call.coach.reason}</p><small>Grounded in the current turn · {w.simulation ? 'Approved static card' : 'Approved guidance'}</small></div> : <div className="coach-placeholder"><Lightbulb size={25} strokeWidth={1.3}/><h3>The right nudge.<br/>At the right moment.</h3><p>One short, grounded suggestion.<br/>Always your choice to use it.</p></div>}<div className="coach-footer"><span>F1 for help</span><button className="text-button" disabled={!active} onClick={() => void run(async () => { if (call) { await api(`/v1/calls/${call.id}/coach`, 'POST'); await reloadCall(call.id); } })}>Get a nudge<ArrowRight size={13}/></button></div></section></div>
    {call?.state === 'VOICEMAIL_READY' && w.policy.voicemail_enabled && <section className="card padded"><h3>Mailbox ready</h3><label>Approved voicemail<select value={vmAsset} onChange={e => setVMAsset(e.target.value)}><option value="">Choose a reviewed message</option>{w.policy.approved_assets?.map(a => <option key={a} value={a}>{a}</option>)}</select></label><button className="secondary" disabled={!vmAsset} onClick={() => void run(async () => { setCall(await api(`/v1/calls/${call.id}/voicemail`, 'POST', { asset_id: vmAsset, detach: false })); })}>Start monitored voicemail</button>{w.policy.detached_topology_verified && <button className="secondary" disabled={!vmAsset} onClick={() => void run(async () => { setCall(await api(`/v1/calls/${call.id}/voicemail`, 'POST', { asset_id: vmAsset, detach: true })); })}>Start verified detached workflow</button>}</section>}
    {call && terminal(call) && call.state !== 'FINALIZED' && <section className="wrap-up card"><div><span className="eyebrow">LEAVE A CLEAR NEXT STEP</span><h3>How did the conversation go?</h3></div><label>Disposition<select value={outcome} onChange={e => setOutcome(e.target.value)}>{['right_party', 'callback_requested', 'meeting_booked', 'meeting_held', 'gatekeeper', 'screening', 'ivr', 'voicemail', 'no_answer', 'wrong_party', 'not_interested', 'temporary_failure'].map(o => <option key={o} value={o}>{label(o)}</option>)}</select></label>{outcome === 'callback_requested' && <label>Confirmed callback time<input type="datetime-local" value={callbackAt} onChange={e => setCallbackAt(e.target.value)}/></label>}<button className="primary" onClick={() => void run(async () => { const result = await api<Call>(`/v1/calls/${call.id}/disposition`, 'POST', { outcome, callback_at: callbackAt ? new Date(callbackAt).toISOString() : null }); setCall(result); await refresh(); notice('Outcome saved. Analysis continues in the background.'); })}>Save outcome<Check size={15}/></button></section>}
    <div className="call-dock"><div className="dock-status"><span className={`status-dot ${ready ? '' : 'paused'}`}/><div><strong>{active ? 'In conversation' : ready ? 'Ready when you are' : 'Session paused'}</strong><small>{reservation ? `${reservation.number.phone} · Your calling identity` : audioStatus || 'One active prospect at a time'}</small></div></div><div className="dock-controls"><button className="dock-tool" disabled={!active} title="Mute · F2" onClick={() => { telephony.current.mute(); setMute(!mute); }}>{mute ? <MicOff size={18}/> : <Mic size={18}/>}<span>{mute ? 'Unmute' : 'Mute'}</span></button><button className="dock-tool" disabled={!active} onClick={() => setKeypad(!keypad)}><span className="keypad-icon">⠿</span><span>Keypad</span></button><button className="dock-tool optout" disabled={!selectedLead} onClick={() => void run(optOut)}><ShieldCheck size={18}/><span>Opt out</span></button>{active ? <button className="danger-button" onClick={() => void run(hangup)}><PhoneOff size={17}/>Hang up</button> : reservation && !call ? <button className="primary dial-button" disabled={dialing || pendingOptouts} onClick={() => void run(startCall)}>{dialing ? <LoaderCircle size={17} className="spin"/> : <Phone size={17}/>}Start call</button> : <button className="primary dial-button" disabled={!ready || pendingOptouts || !journalReady} onClick={() => void run(getNext)}><Phone size={17}/>Get next lead</button>}</div><label className="pause-after"><input type="checkbox" checked={pauseAfter} onChange={e => setPauseAfter(e.target.checked)}/> Pause after call</label></div>
    {keypad && <div className="keypad-popover"><div><strong>Keypad</strong><button className="icon-button" aria-label="Close keypad" onClick={() => setKeypad(false)}><X size={15}/></button></div><div className="digits">{'123456789*0#'.split('').map(d => <button key={d} onClick={() => void run(async () => { if (call) await api(`/v1/calls/${call.id}/dtmf`, 'POST', { digits: d }); })}>{d}</button>)}</div></div>}
  </>;
}
function UsersIcon() { return <span className="tiny-circle">◎</span>; }
function ActivityIcon() { return <span className="tiny-circle">≋</span>; }
function Metric({ title, value, detail, last }: { title: string; value: string; detail: string; last?: boolean }) { return <div className={`metric ${last ? 'last' : ''}`}><span>{title}</span><strong>{value}<small>{title.includes('REACH') ? ' people' : ''}</small></strong><p>{detail}</p></div>; }
