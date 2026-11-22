import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowUp, Check, ChevronDown, ChevronRight, CircleAlert, Copy, Layers3, LoaderCircle, Mail, Plus, ShieldCheck, Sparkles } from 'lucide-react';
import { api, AccessDenied, setAccessCode } from './api/client';
import type { Run } from './types';
import './styles.css';
import { TrustCards, plain } from './components/TrustCards';

const money = (minor: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(minor / 100);
const terminals = new Set(['COMPLETED', 'COMPLETED_WITH_GAPS', 'FAILED']);
const waiting = new Set(['WAITING_FOR_CLARIFICATION', 'WAITING_FOR_APPROVAL']);
const examples = [
  'Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each.',
  'Source ESP32-S3 modules, a 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors. 500 of each, delivered to Bengaluru within three weeks.',
];
const leatherWords = /leather|tanner|handbag|wallet|belt|footwear|shoe|saddle|bags?\b/i;
const electronicsWords = /esp32|ldo|regulator|capacitor|usb|resistor|connector|module|pcb|bom|chip|ic\b|mcu|sensor/i;
const approveWords = /^\s*(approve|approved|yes|go ahead|confirm|ok(ay)?)\b/i;
const STEP: Record<string, string> = {
  CREATED: 'Received your request', PARSING: 'Understanding the request', DISCOVERING: 'Finding suppliers', VERIFYING: 'Checking suppliers',
  RESEARCHING: 'Filling information gaps', DECIDING: 'Comparing options', SCREENING: 'Compliance screen', EXECUTING: 'Preparing orders and RFQs',
  WAITING_FOR_CLARIFICATION: 'Need a detail from you', WAITING_FOR_APPROVAL: 'Waiting for your approval', COMPLETED: 'Done',
  COMPLETED_WITH_GAPS: 'Done, with gaps', FAILED: 'Stopped',
};

type Health = { gemini_configured?: boolean; crustdata_configured?: boolean };
type Entry = { kind: 'user'; text: string } | { kind: 'agent'; runId: string; segment: number } | { kind: 'note'; text: string; error?: boolean };
type Step = { id: string; title: string; detail: string; thought?: boolean };

const load = <T,>(key: string, fallback: T): T => { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) as T : fallback; } catch { return fallback; } };
const save = (key: string, value: unknown) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } };

/** Events belonging to one agent turn: a run is split at each clarification question. */
function segmentEvents(run: Run, segment: number) {
  const out: Run['events'] = [];
  let current = 0;
  for (const e of run.events) {
    if (current === segment) out.push(e);
    if (e.state === 'WAITING_FOR_CLARIFICATION') current++;
    if (current > segment) break;
  }
  return out;
}
const segmentCount = (run: Run) => run.events.filter(e => e.state === 'WAITING_FOR_CLARIFICATION').length;

function humanize(run: Run, e: Run['events'][number]) {
  const m = e.message;
  if (e.state === 'CREATED') return run.request.mode === 'live' ? 'Using live company data from Crustdata.' : 'Using sample supplier data for this demo.';
  if (m.startsWith('Gemini request parsing enabled')) return 'Reading your request with Gemini.';
  if (m.startsWith('Reading the request with the bounded')) return 'Reading your request.';
  if (m.startsWith('Clarification saved')) return 'Got your answer. Reading the request again.';
  return m.replace(/^Specifications saved; finding (fixture|live) suppliers\./, (_, mode) => mode === 'live' ? 'Requirements are clear. Searching live company data.' : 'Requirements are clear. Searching sample suppliers.');
}

function stepsFor(run: Run, events: Run['events']): Step[] {
  const steps: Step[] = [];
  const name = (id: string) => run.suppliers.find(s => s.id === id)?.name ?? id;
  let understood = false, insights = false;
  for (const e of events) {
    if (e.state === 'WAITING_FOR_CLARIFICATION') continue;
    if (!understood && e.state === 'DISCOVERING' && run.request_interpretation?.summary) {
      understood = true;
      steps.push({ id: e.id + ':u', title: 'What I understood', detail: String(run.request_interpretation.summary), thought: true });
    }
    steps.push({ id: e.id, title: STEP[e.state] ?? e.state, detail: humanize(run, e) });
    if (!insights && e.message.startsWith('Gemini provider evidence extraction') && run.supplier_insights.length) {
      insights = true;
      for (const [i, s] of run.supplier_insights.entries())
        steps.push({ id: `${e.id}:i${i}`, title: `Notes on ${name(String(s.supplier_id ?? ''))}`, detail: String(s.summary ?? ''), thought: true });
    }
  }
  return steps;
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return <button className="icon-btn" aria-label="Copy" onClick={() => { void navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }); }}>{done ? <Check size={14}/> : <Copy size={14}/>}</button>;
}

function Thinking({ steps, live, seconds }: { steps: Step[]; live: boolean; seconds: number }) {
  const [open, setOpen] = useState<boolean | null>(null);
  const expanded = open ?? live;
  const current = steps[steps.length - 1];
  return <div className="thinking">
    <button className="thinking-head" onClick={() => setOpen(!expanded)}>
      {live ? <LoaderCircle size={15} className="spin"/> : <Sparkles size={15}/>}
      <span className={live ? 'shimmer' : ''}>{live ? (current?.title ?? 'Starting') + '…' : `Worked through ${steps.length} step${steps.length === 1 ? '' : 's'}${seconds ? ` in ${seconds}s` : ''}`}</span>
      {expanded ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}
    </button>
    {expanded && <ol className="trace">{steps.map((s, i) => <li key={s.id} className={s.thought ? 'thought' : ''}>
      <span className="dot">{live && i === steps.length - 1 ? <LoaderCircle size={11} className="spin"/> : null}</span>
      <div><strong>{s.title}</strong><p>{s.detail}</p></div></li>)}</ol>}
  </div>;
}

function Answer({ run }: { run: Run }) {
  const name = (id: string) => run.suppliers.find(s => s.id === id)?.name ?? id;
  const spec = (id: string) => run.specs.find(s => s.id === id)?.description ?? id;
  if (run.state === 'FAILED') return <div className="answer"><p className="bad"><CircleAlert size={15}/> I couldn't finish this request.</p><p>{run.error}</p></div>;
  const decision = run.decisions[0];
  const credits = (run.provider_usage as Record<string, unknown>[]).reduce((t, r) => t + Number(r.charged ?? r.reserved ?? 0), 0);
  const planned = run.orders.filter(o => o.state === 'PLANNED');
  return <div className="answer">
    {run.request.request_type === 'shortlist' ? <>
      {decision?.selected_supplier_ids.length ? <>
        <p>I shortlisted <strong>{decision.selected_supplier_ids.length}</strong> of the {run.suppliers.length} companies I found{run.rfqs.length ? ' and drafted a first email to each' : ''}:</p>
        <ul>{decision.selected_supplier_ids.map(id => { const c = (run.suppliers.find(s => s.id === id)?.profile as Record<string, any> | undefined)?.compliance; return <li key={id}><strong>{name(id)}</strong>{c?.verdict === 'hold' ? <span className="tag amber">held for review</span> : c?.verdict === 'approve' ? <span className="tag green">compliance passed</span> : null}{c?.verdict === 'hold' && <small>{(c.reasons as string[]).join(' · ')}</small>}</li>; })}</ul>
      </> : <p>{decision?.explanation ?? 'I could not build a shortlist from the evidence I found.'}</p>}
      {decision && decision.candidates.some(c => !decision.selected_supplier_ids.includes(c.supplier_id)) && <details className="fold"><summary>Why the others were left out</summary><ul>{decision.candidates.filter(c => !decision.selected_supplier_ids.includes(c.supplier_id)).map(c => <li key={c.quote_id}><strong>{name(c.supplier_id)}</strong>: {plain(c.reason_codes).join(', ') || 'lower ranked'}</li>)}</ul></details>}
      {run.rfqs.map(r => <div className="rfq" key={r.id}><div className="rfq-head"><Mail size={14}/><span>{name(r.supplier_id)}{r.recipient ? ` · ${r.recipient}` : ''}</span><span className="tag">draft, not sent</span><CopyButton text={`Subject: ${r.subject}\n\n${r.body}`}/></div><strong>{r.subject}</strong><p className="rfq-body">{r.body}</p></div>)}
    </> : <>
      {run.decisions.length ? <><p>Here's the plan for each item:</p><ul>{run.decisions.map(d => { const best = d.candidates.find(c => d.selected_supplier_ids.includes(c.supplier_id)); return <li key={d.spec_id}><strong>{spec(d.spec_id)}</strong>: {best ? <>{name(best.supplier_id)}{best.cost ? `, ${money(best.cost.total_minor)} landed` : ''}</> : <span className="muted">{d.explanation}</span>}</li>; })}</ul></> : <p>I couldn't put together a plan for this request.</p>}
      {run.orders.length > 0 && <p>Orders (simulated payments): {run.orders.map(o => `${name(o.supplier_id)} ${money(o.amount_minor)} ${o.state.toLowerCase()}`).join(' · ')}. Total {money(run.orders.reduce((s, o) => s + o.amount_minor, 0))}.</p>}
    </>}
    {run.state === 'WAITING_FOR_APPROVAL' && planned.length > 0 && <p className="ask">{planned.length === 1 ? 'This order is' : 'These orders are'} above your approval limit ({planned.map(o => `${name(o.supplier_id)} ${money(o.amount_minor)}`).join(', ')}). Reply <strong>approve</strong> to place {planned.length === 1 ? 'it' : 'them'}. Payment is simulated.</p>}
    {run.state === 'COMPLETED_WITH_GAPS' && <p className="muted">Some facts couldn't be confirmed, so the result has gaps. The steps above show where.</p>}
    {run.request.mode === 'live' && <p className="muted">Spent {credits.toFixed(2)} of {run.request.credit_budget} Crustdata credits.</p>}
    {run.checks.length > 0 && <details className="fold"><summary>Supplier checks and evidence</summary><TrustCards run={run}/></details>}
  </div>;
}

function AgentTurn({ run, segment }: { run: Run | undefined; segment: number }) {
  if (!run) return <div className="msg agent"><div className="avatar"><Layers3 size={15}/></div><div className="body"><div className="thinking"><span className="thinking-head"><LoaderCircle size={15} className="spin"/><span className="shimmer">Starting…</span></span></div></div></div>;
  const events = segmentEvents(run, segment);
  const askedHere = events.some(e => e.state === 'WAITING_FOR_CLARIFICATION');
  const isLast = segment === segmentCount(run);
  const finished = askedHere || (isLast && (terminals.has(run.state) || waiting.has(run.state)));
  const seconds = events.length > 1 ? Math.round((new Date(events[events.length - 1].time).getTime() - new Date(events[0].time).getTime()) / 1000) : 0;
  const question = events.find(e => e.state === 'WAITING_FOR_CLARIFICATION')?.message;
  return <div className="msg agent"><div className="avatar"><Layers3 size={15}/></div><div className="body">
    <Thinking steps={stepsFor(run, events)} live={!finished} seconds={seconds}/>
    {askedHere ? <div className="answer"><p>{question}</p><p className="muted">Reply below and I'll pick up from here.</p></div> : finished ? <Answer run={run}/> : null}
  </div></div>;
}

function App() {
  const [thread, setThread] = useState<Entry[]>(() => load('sourcing-thread', []));
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [locked, setLocked] = useState(false);
  const [passcode, setPasscode] = useState('');
  const [health, setHealth] = useState<Health>({});
  const endRef = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => { fetch('/api/health').then(r => r.json()).then(setHealth).catch(() => {}); }, []);
  useEffect(() => save('sourcing-thread', thread), [thread]);

  const runIds = [...new Set(thread.flatMap(e => e.kind === 'agent' ? [e.runId] : []))];
  const lastRunId = runIds[runIds.length - 1];
  const lastRun = lastRunId ? runs[lastRunId] : undefined;

  const refresh = useCallback(async (all = false) => {
    try {
      const ids = all ? runIds : runIds.filter(id => !runs[id] || !terminals.has(runs[id].state));
      if (!ids.length) { if (!thread.length) await api('/access'); setLocked(false); return; }
      const fetched = await Promise.all(ids.map(id => api<Run>(`/runs/${id}`).catch(e => { if (e instanceof AccessDenied) throw e; return null; })));
      setRuns(prev => { const next = { ...prev }; for (const r of fetched) if (r) next[r.id] = r; return next; });
      setLocked(false);
    } catch (e) { if (e instanceof AccessDenied) setLocked(true); else setError((e as Error).message); }
  }, [runIds.join(','), runs, thread.length]);

  useEffect(() => { void refresh(true); }, [locked]);
  const active = runIds.some(id => !runs[id] || !(terminals.has(runs[id].state) || waiting.has(runs[id].state)));
  useEffect(() => { if (!active) return; const t = setInterval(() => void refresh(), 1000); return () => clearInterval(t); }, [active, refresh]);

  // Follow the conversation unless the reader has scrolled up to read something.
  useEffect(() => { if (stick.current) endRef.current?.scrollIntoView({ block: 'end' }); }, [thread, runs]);

  async function send() {
    const message = text.trim();
    if (!message || busy) return;
    setBusy(true); setError(''); setText(''); stick.current = true;
    const add = (...entries: Entry[]) => setThread(t => [...t, ...entries]);
    try {
      if (lastRun?.state === 'WAITING_FOR_CLARIFICATION') {
        add({ kind: 'user', text: message });
        const r = await api<Run>(`/runs/${lastRun.id}/clarifications`, 'POST', { answer: message });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'agent', runId: r.id, segment: segmentCount(r) });
      } else if (lastRun?.state === 'WAITING_FOR_APPROVAL' && approveWords.test(message)) {
        add({ kind: 'user', text: message });
        let r = lastRun;
        for (const o of lastRun.orders.filter(o => o.state === 'PLANNED'))
          r = await api<Run>(`/runs/${lastRun.id}/approvals`, 'POST', { plan_version: o.plan_version, order_id: o.id, amount_minor: o.amount_minor });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'note', text: 'Approved. Placing the simulated order…' });
      } else {
        add({ kind: 'user', text: message });
        const previous = lastRun?.request;
        const standalone = leatherWords.test(message) || electronicsWords.test(message) || !previous;
        const leather = standalone ? leatherWords.test(message) : previous!.category === 'leather_goods';
        const original = previous ? previous.text.split('\nClarification:')[0] : '';
        const requestText = standalone ? message : `${message}\n\nEarlier in this conversation I asked: ${original}`;
        const mode = leather && health.crustdata_configured ? 'live' : 'fixture';
        const r = await api<Run>('/runs', 'POST', { text: requestText, category: leather ? 'leather_goods' : 'electronics', request_type: leather ? 'shortlist' : 'purchase',
          destination: leather ? 'Uttar Pradesh, India' : 'Bengaluru, India', deadline: new Date(Date.now() + 21 * 86400000).toISOString(),
          research_budget_minor: 200000, approval_threshold_minor: 100000000, mode, scenario: 'normal', specs: [], llm_enabled: !!health.gemini_configured, credit_budget: 25 });
        setRuns(p => ({ ...p, [r.id]: r }));
        add({ kind: 'agent', runId: r.id, segment: 0 });
      }
    } catch (e) {
      if (e instanceof AccessDenied) setLocked(true);
      else add({ kind: 'note', text: (e as Error).message, error: true });
    } finally { setBusy(false); }
  }

  if (locked) return <div className="gate"><form className="card gate-card" onSubmit={e => { e.preventDefault(); setAccessCode(passcode.trim()); setPasscode(''); setLocked(false); }}>
    <span className="brand-icon"><Layers3 size={23}/></span><h1>Sourcing Desk</h1><p className="muted">Enter the team passcode to continue.</p>
    <label>Passcode<input autoFocus required type="password" autoComplete="current-password" value={passcode} onChange={e => setPasscode(e.target.value)}/></label>
    <button className="primary">Unlock</button></form></div>;

  const placeholder = lastRun?.state === 'WAITING_FOR_CLARIFICATION' ? 'Answer the question above…'
    : lastRun?.state === 'WAITING_FOR_APPROVAL' ? 'Reply "approve", or ask something else…'
    : thread.length ? 'Ask a follow-up or start a new request…' : 'What do you need to source? Include specs, quantities and where it should go.';
  const prompt = <form className="prompt" onSubmit={e => { e.preventDefault(); void send(); }}>
    <textarea aria-label="Message" rows={thread.length ? 1 : 4} value={text} placeholder={placeholder} autoFocus
      onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(); } }}/>
    <button className="send" aria-label="Send" disabled={busy || !text.trim()}>{busy ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={18}/>}</button>
  </form>;

  if (!thread.length) return <div className="home">
    <div className="home-inner">
      <div className="home-brand"><span className="brand-icon"><Layers3 size={22}/></span>Sourcing Desk</div>
      <h1>What do you need to source?</h1>
      {prompt}
      {error && <div role="alert" className="error">{error}</div>}
      <div className="examples">{examples.map(x => <button key={x} type="button" onClick={() => setText(x)}>{x}</button>)}</div>
      <p className="muted center"><ShieldCheck size={13}/> Payments are simulated and RFQs stay drafts. Nothing is sent to suppliers.</p>
    </div>
  </div>;

  return <div className="chat">
    <header className="chat-top"><span className="home-brand"><span className="brand-icon"><Layers3 size={16}/></span>Sourcing Desk</span>
      <button className="secondary" onClick={() => { setThread([]); setRuns({}); setText(''); }}><Plus size={15}/>New chat</button></header>
    <div className="chat-scroll" ref={scroller} onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
      <div className="chat-col">
        {thread.map((e, i) => e.kind === 'user' ? <div className="msg user" key={i}><div className="bubble">{e.text}</div></div>
          : e.kind === 'note' ? <div className={`note ${e.error ? 'bad' : ''}`} key={i}>{e.error && <CircleAlert size={14}/>}{e.text}</div>
          : <AgentTurn key={i} run={runs[e.runId]} segment={e.segment}/>)}
        {error && <div className="note bad"><CircleAlert size={14}/>{error}</div>}
        <div ref={endRef}/>
      </div>
    </div>
    <div className="composer"><div className="chat-col">{prompt}<p className="fine">Payments are simulated and RFQs stay drafts. Nothing is sent to suppliers.</p></div></div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
