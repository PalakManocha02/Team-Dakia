import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArrowUp, Box, Check, FileText, Layers3, LoaderCircle, Plus, ShieldCheck, Wallet } from 'lucide-react';
import { api, AccessDenied, setAccessCode } from './api/client';
import type { Run, Ledger, Reservation } from './types';
import './styles.css';
import { LLMInsights } from './components/LLMInsights';
import { TrustCards, CreditLedger, plain } from './components/TrustCards';

const money = (minor: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(minor / 100);
const date = (value: string) => new Date(value).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short' });
const stageNames = ['PARSING', 'DISCOVERING', 'VERIFYING', 'RESEARCHING', 'DECIDING', 'SCREENING', 'EXECUTING'];
const terminals = new Set(['COMPLETED', 'COMPLETED_WITH_GAPS', 'FAILED']);
const examples = [
  'Find three leather-goods manufacturers in Uttar Pradesh, check each is real and active, and draft the first email to each.',
  'Source ESP32-S3 modules, a 3.3V 500mA SOT-23 LDO regulator, 10uF 0805 capacitors and USB-C connectors. 500 of each, delivered to Bengaluru within three weeks.',
];
const leatherWords = /leather|tanner|handbag|wallet|belt|footwear|shoe|saddle|bags?\b/i;
type Health = { gemini_configured?: boolean; crustdata_configured?: boolean };

function App() {
  const [run, setRun] = useState<Run | null>(null);
  const [selected, setSelected] = useState<string | null>(() => { try { return localStorage.getItem('sourcing-run'); } catch { return null; } });
  const [ledger, setLedger] = useState<Ledger[]>([]);
  const [reservations, setReservations] = useState<Reservation[]>([]);
  const [text, setText] = useState('');
  const [answer, setAnswer] = useState('');
  const [budgetEdit, setBudgetEdit] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [locked, setLocked] = useState(false);
  const [passcode, setPasscode] = useState('');
  const [health, setHealth] = useState<Health>({});
  const generation = useRef(0);

  useEffect(() => { fetch('/api/health').then(r => r.json()).then(setHealth).catch(() => {}); }, []);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    try {
      if (!selected) { await api('/access'); if (generation.current === current) setLocked(false); return; }
      const [r, l, b] = await Promise.all([api<Run>(`/runs/${selected}`), api<Ledger[]>(`/runs/${selected}/ledger`), api<Reservation[]>(`/runs/${selected}/reservations`)]);
      if (generation.current !== current) return;
      setRun(r); setLedger(l); setReservations(b); setLocked(false);
    } catch (e) {
      if (generation.current !== current) return;
      if (e instanceof AccessDenied) { setLocked(true); return; }
      if ((e as Error).message === 'Run not found') { pick(null); return; }
      setError((e as Error).message);
    }
  }, [selected]);

  useEffect(() => { void refresh(); if (!selected) return; const timer = setInterval(() => void refresh(), 1500); return () => { clearInterval(timer); generation.current++; }; }, [refresh, selected]);
  useEffect(() => { try { if (selected) localStorage.setItem('sourcing-run', selected); else localStorage.removeItem('sourcing-run'); } catch { /* storage unavailable */ } }, [selected]);

  async function action(work: () => Promise<unknown>) {
    setBusy(true); setError('');
    try { await work(); await refresh(); } catch (e) { if (e instanceof AccessDenied) setLocked(true); else setError((e as Error).message); } finally { setBusy(false); }
  }
  function pick(id: string | null) { generation.current++; setSelected(id); setRun(null); setLedger([]); setReservations([]); setError(''); setAnswer(''); setBudgetEdit(''); }
  async function create() {
    const prompt = text.trim();
    if (!prompt) return;
    await action(async () => {
      const leather = leatherWords.test(prompt);
      const category = leather ? 'leather_goods' : 'electronics';
      const mode = leather && health.crustdata_configured ? 'live' : 'fixture';
      const result = await api<Run>('/runs', 'POST', { text: prompt, category, request_type: leather ? 'shortlist' : 'purchase', destination: leather ? 'Uttar Pradesh, India' : 'Bengaluru, India',
        deadline: new Date(Date.now() + 21 * 86400000).toISOString(), research_budget_minor: 200000, approval_threshold_minor: 100000000,
        mode, scenario: 'normal', specs: [], llm_enabled: !!health.gemini_configured, credit_budget: 25, ...(mode === 'live' && !leather ? { supplier_domains: ['mouser.com'] } : {}) });
      setText(''); pick(result.id);
    });
  }
  const spent = ledger.filter(l => l.account === 'research').reduce((s, l) => s + l.amount_minor, 0);
  const held = reservations.filter(r => r.status === 'reserved').reduce((s, r) => s + r.amount_minor, 0);
  const supplierName = (id: string) => run?.suppliers.find(s => s.id === id)?.name ?? id;
  const selectedTotal = run?.orders.reduce((s, o) => s + o.amount_minor, 0) ?? 0;

  if (locked) return <div className="gate"><form className="card gate-card" onSubmit={e => { e.preventDefault(); setAccessCode(passcode.trim()); setPasscode(''); void refresh(); }}>
    <span className="brand-icon"><Layers3 size={23}/></span><h1>Sourcing Desk</h1><p className="muted">Enter the team passcode to continue.</p>
    <label>Passcode<input autoFocus required type="password" autoComplete="current-password" value={passcode} onChange={e => setPasscode(e.target.value)}/></label>
    <button className="primary">Unlock</button></form></div>;

  const prompt = <form className="prompt" onSubmit={e => { e.preventDefault(); void create(); }}>
    <textarea aria-label="What do you need to source?" rows={selected ? 2 : 4} value={text} placeholder="What do you need to source? Include specs, quantities and where it should go."
      onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void create(); } }}/>
    <button className="send" aria-label="Start sourcing" disabled={busy || !text.trim()}>{busy ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={18}/>}</button>
  </form>;

  if (!selected) return <div className="home">
    <div className="home-inner">
      <div className="home-brand"><span className="brand-icon"><Layers3 size={22}/></span>Sourcing Desk</div>
      <h1>What do you need to source?</h1>
      {prompt}
      {error && <div role="alert" className="error">{error}</div>}
      <div className="examples">{examples.map(x => <button key={x} type="button" onClick={() => setText(x)}>{x}</button>)}</div>
      <p className="muted center"><ShieldCheck size={13}/> Payments are simulated and RFQs stay drafts. Nothing is sent to suppliers.</p>
    </div>
  </div>;

  return <div className="run-page">
    <header className="run-top"><button className="home-brand link" onClick={() => pick(null)}><span className="brand-icon"><Layers3 size={18}/></span>Sourcing Desk</button><button className="secondary" onClick={() => pick(null)}><Plus size={16}/>New request</button></header>
    <div className="content narrow">
      {error && <div role="alert" className="error">{error}<button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
      {!run ? <div className="card empty"><LoaderCircle className="spin"/><p>Loading request…</p></div> : <>
        <section className="card progress-card"><div className="spread"><h2>{run.request.category === 'electronics' ? 'Electronics sourcing' : 'Leather-goods manufacturers'}</h2><span className={`pill ${terminals.has(run.state) ? 'green' : ''}`}>{terminals.has(run.state) ? run.state.replaceAll('_', ' ') : <><LoaderCircle size={11} className="spin"/> {run.state.replaceAll('_', ' ')}</>}</span></div><p className="request-text">{run.request.text}</p><div className="stages">{stageNames.map((s,i) => { const visited = run.events.some(e => e.state === s); return <div key={s} className={visited ? 'visited' : ''}><span>{visited ? <Check size={14}/> : i+1}</span>{s.toLowerCase()}</div>; })}</div><small className="muted">Started {date(run.created_at)} · {run.request.mode === 'live' ? 'Live Crustdata company data' : 'Sample data'}{run.request.llm_enabled ? ' · Gemini on' : ''}</small></section>
        <div className="stats">{run.request.request_type === 'shortlist' ? <Stat label="SHORTLIST" value={`${run.decisions[0]?.selected_supplier_ids.length ?? 0} of ${run.request.shortlist_count}`} sub={`verified makers · ${run.rfqs.length} RFQ draft${run.rfqs.length === 1 ? '' : 's'}`} icon={<Box size={19}/>}/> : <Stat label="ORDER PLAN" value={money(selectedTotal)} sub="Estimated landed total · demo tax rates" icon={<Box size={19}/>}/>}{run.request.mode === 'live' ? <Stat label="DATA SPEND" value={`${(run.provider_usage as Record<string, any>[]).reduce((t, r) => t + Number(r.charged ?? r.reserved ?? 0), 0).toFixed(2)} credits`} sub={`of a ${run.request.credit_budget}-credit ceiling you can lower`} icon={<Wallet size={19}/>}/> : <Stat label="RESEARCH SPEND" value={money(spent)} sub={`${money(held)} reserved · ${money(run.request.research_budget_minor)} ceiling`} icon={<Wallet size={19}/>}/>}<Stat label="SUPPLIERS CHECKED" value={String(run.checks.filter(c => c.kind === 'profile' && c.result === 'pass').length || run.suppliers.length).padStart(2, '0')} sub={run.request.mode === 'live' ? `of ${run.suppliers.length} found · one record per company` : 'Canonical identities · aliases merged'} icon={<ShieldCheck size={19}/>}/><Stat label="WORKFLOW STATUS" value={run.state.replaceAll('_', ' ')} sub={run.request.mode === 'fixture' ? 'Sample data · simulated execution' : 'Live Crustdata company data'} icon={<Activity size={19}/>}/></div>
        {run.state === 'WAITING_FOR_CLARIFICATION' && <section className="card attention"><h2>One detail before we continue</h2><p>{run.clarification?.question}</p><form onSubmit={e => { e.preventDefault(); void action(() => api(`/runs/${run.id}/clarifications`, 'POST', { answer })); }}><label>Your answer<input required value={answer} onChange={e => setAnswer(e.target.value)} placeholder="e.g. 500 of each"/></label><button className="primary" disabled={busy}>Save and continue</button></form></section>}
        {run.error && <div className="error" role="alert">{run.error}</div>}
        <LLMInsights run={run}/>
        <h2 className="section-title">Plan</h2>
        <section><TrustCards run={run}/>{!run.decisions.length ? <Empty title="Your plan is taking shape" text="Supplier comparisons appear after verification and research finish."/> : run.decisions.map(d => <section className="card decision" key={d.spec_id}><div className="spread"><h2>{run.specs.find(s => s.id === d.spec_id)?.description}</h2><span className={`pill ${d.outcome === 'selected' ? 'green' : 'amber'}`}>{d.outcome.replaceAll('_',' ')}</span></div><p>{d.explanation}</p>{d.alternatives.map(a => <p className="muted" key={a}>{a}</p>)}<div className="table-scroll"><table><thead><tr><th>Supplier</th><th>Reliability</th><th>Landed cost</th><th>Decision</th></tr></thead><tbody>{d.candidates.map(c => <tr key={c.quote_id}><td><strong>{supplierName(c.supplier_id)}</strong><small>{run.suppliers.find(s => s.id === c.supplier_id)?.kind} · {run.request.mode}</small></td><td><strong>{c.reliability}/100</strong><small>{c.coverage}% evidence coverage</small><details><summary>Score breakdown</summary>{Object.entries(c.reliability_breakdown).map(([k,v]) => <small key={k}>{k}: {v}</small>)}</details></td><td>{c.cost ? <><strong>{money(c.cost.total_minor)}</strong><details><summary>Cost breakdown</summary><small>Goods {money(c.cost.goods_minor)}</small><small>Shipping {money(c.cost.shipping_minor)}</small><small>Duty {money(c.cost.duty_minor)}</small><small>Tax {money(c.cost.tax_minor)}</small><small>{c.cost.assumptions}</small></details></> : <span className="muted">{run.request.request_type === 'shortlist' ? 'RFQ required' : 'Incomplete'}</span>}</td><td>{d.selected_supplier_ids.includes(c.supplier_id) ? <span className="selected"><Check size={14}/>Selected</span> : <span className="muted">{c.eligible ? 'Eligible alternative' : 'Ineligible'}</span>}<small className="reason">{plain(c.reason_codes).join(' · ')}</small><details><summary>Sources & timestamps</summary>{c.evidence_ids.map(id => { const e=run.evidence.find(x => x.id === id); return e && <div className="evidence" key={id}><strong>{e.provider} · {e.mode}</strong><small>{e.status} · {e.mode} · {date(e.observed_at)}</small><code>{e.reference}</code><p>{e.fact}: {e.value}</p></div>; })}</details></td></tr>)}</tbody></table></div></section>)}</section>
        <h2 className="section-title">Orders & RFQ drafts</h2>
        <section className="card pad"><div className="spread"><h2>Orders & RFQ drafts</h2><span className="pill">SIMULATED</span></div>{!run.orders.length && !run.rfqs.length && <p className="muted">Orders or drafts appear after a plan is selected.</p>}{run.orders.map(o => <div className="order-entry" key={o.id}><div className="spread"><div><h3>{supplierName(o.supplier_id)}</h3><p>{run.specs.find(s => s.id === o.spec_id)?.description}</p></div><strong>{money(o.amount_minor)}</strong></div><div className="spread"><span className={`pill ${o.state === 'PAID' ? 'green' : 'amber'}`}>{o.state} · mock</span>{o.state === 'PLANNED' && <button className="primary" disabled={busy} onClick={() => void action(() => api(`/runs/${run.id}/approvals`, 'POST', { plan_version: o.plan_version, order_id: o.id, amount_minor: o.amount_minor }))}>Approve {money(o.amount_minor)}</button>}</div><details><summary>Payment reference</summary><code>Idempotency key: {o.idempotency_key}</code><small>Plan version: {o.plan_version}</small><small>Mock payment: {o.payment_id ?? 'not settled'}</small></details></div>)}{run.rfqs.map(r => <div className="order-entry" key={r.id}><div className="spread"><h3>{supplierName(r.supplier_id)}</h3><span className="pill">DRAFT · UNSENT</span></div>{(() => { const c = (run.suppliers.find(s => s.id === r.supplier_id)?.profile as Record<string, any> | undefined)?.compliance; return c?.verdict === 'hold' ? <p className="hold-note"><strong>Held for your review before sending:</strong> {(c.reasons as string[]).join(' · ')}</p> : c?.verdict === 'approve' ? <p className="approve-note">Compliance screen passed</p> : null; })()}{r.recipient && <p className="muted">To: {r.recipient}</p>}<h4>{r.subject}</h4><p className="rfq-body">{r.body}</p></div>)}<button className="secondary" disabled={busy} onClick={() => void action(() => api(`/runs/${run.id}/resume`, 'POST'))}>Resume / reconcile saved work</button></section>
        <h2 className="section-title">Research & spending</h2>
        {run.request.mode === 'live' && <CreditLedger run={run} busy={busy} onLower={v => void action(() => api(`/runs/${run.id}/budget`, 'PATCH', { credit_budget: v }))}/>}
        <section className="card pad"><div className="spread"><div><h2>Research & budget</h2><p>Only purchase data that could change a decision.</p></div><span className="pill">MOCK WALLET</span></div><form className="budget-form" onSubmit={e => { e.preventDefault(); void action(() => api(`/runs/${run.id}/budget`, 'PATCH', { research_budget_minor: Math.round(Number(budgetEdit) * 100) })); }}><label>New research ceiling · INR<input required type="number" min="0" step="0.01" value={budgetEdit} placeholder={String(run.request.research_budget_minor/100)} onChange={e => setBudgetEdit(e.target.value)}/></label><button className="secondary" disabled={busy}>Update ceiling</button></form><p className="muted">Spent {money(spent)} · Reserved {money(held)} · Remaining {money(Math.max(0,run.request.research_budget_minor-spent-held))}. A reduction cannot reverse committed spending.</p>{run.research.length ? run.research.map(p => <div className="research-entry" key={p.id}><div className="spread"><h3>{p.question}</h3><span className="pill">{p.status} · {p.quoted_credits ? `${p.quoted_credits} credit` : money(p.quoted_minor)}</span></div><p>{p.rationale}</p><small>Decision changed: {p.changed_decision === null || p.changed_decision === undefined ? 'not yet evaluated' : p.changed_decision ? 'yes' : 'no'}</small></div>) : <p className="muted">No research purchase proposals yet.</p>}<h3 className="ledger-title">Settled ledger</h3>{ledger.length ? ledger.map(l => <div className="ledger-row" key={l.id}><div>{l.description}<small>{l.account} · {date(l.created_at)}</small></div><strong>{money(l.amount_minor)}</strong></div>) : <p className="muted">No money has been committed.</p>}</section>
        <h2 className="section-title">Activity</h2>
        <section className="card pad"><div className="timeline">{run.events.map(e => <div className="timeline-entry" key={e.id}><span className="timeline-dot"/><div><strong>{e.state.replaceAll('_',' ')}</strong><p>{e.message}</p><small>{date(e.time)}</small></div></div>)}</div></section>
      </>}
      <div className="bottom-prompt">{prompt}</div>
      <footer><span>Sourcing Desk</span><span>Evidence before decisions.</span><a href="/docs" target="_blank" rel="noreferrer">API reference</a></footer>
    </div>
  </div>;
}

function Stat({label,value,sub,icon}: {label:string;value:string;sub:string;icon:React.ReactNode}) { return <section className="card stat"><div>{label}{icon}</div><strong>{value}</strong><p>{sub}</p></section>; }
function Empty({title,text}: {title:string;text:string}) { return <div className="card empty"><FileText size={28}/><h2>{title}</h2><p>{text}</p></div>; }

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
