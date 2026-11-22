import hashlib
import json
from datetime import timedelta
from app.config import pack_for
from app.contracts import (RunState as S, WorkflowRun, WorkflowEvent, Clarification, SupplierCheck,
                           ResearchPurchase, Evidence, Order, RFQDraft, Approval, now)
from app.domain.state_machines import transition, order_transition
from app.domain.decision_engine import decide, assess, landed_cost
from app.domain.data_value import worth_buying
from app.persistence.repositories import get_run, save_run, reserve, can_commit, record_spend
from app.providers.fixtures.discovery import FixtureDiscovery
from app.providers.live import LiveBoundary
from app.providers.crustdata import CrustdataClient, CreditLimitReached, RateLimited
from app.providers.live_discovery import LiveDiscovery, LiveCompanyDiscovery, LiveCompanyChecks
from app.domain.supplier_signals import news_verdict
from app.domain.compliance import is_owner, classify_articles, front_company_flags, verdict as compliance_verdict
from app.providers.interfaces import IntegrationUnavailable
from app.providers.mock_checkout import MockPayments
from app.services.specification_parser import FixtureParser
from app.providers.gemini import GeminiClient
from app.services.llm_extraction import LLMExtractionService

ACTIVE = {S.CREATED, S.PARSING, S.DISCOVERING, S.VERIFYING, S.RESEARCHING, S.DECIDING, S.SCREENING, S.EXECUTING}


def fingerprint(run):
    payload = {"specs": [s.model_dump(mode="json") for s in run.specs],
               "quotes": [q.model_dump(mode="json") for q in run.quotes],
               # Profiles and contacts are display data; screening notes must not invalidate approvals.
               "suppliers": [s.model_dump(mode="json", exclude={"profile", "contact"}) for s in run.suppliers],
               "pack": pack_for(run.request.category).model_dump(mode="json"),
               "destination": run.request.destination, "deadline": run.request.deadline.isoformat(),
               "threshold": run.request.approval_threshold_minor}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


class Orchestrator:
    def __init__(self, database, payments=None, live_client=None, llm_client=None):
        self.db = database
        self.payments = payments or MockPayments(database.path + ".payments.db")
        self.live_client = live_client
        self.llm_client = llm_client

    def crustdata(self, run):
        if self.live_client is None:
            self.live_client = CrustdataClient(journal_path=self.db.path + ".credits.db")
        self.live_client.set_run_limit(run.id, run.request.credit_budget)
        return self.live_client

    def llm(self):
        if self.llm_client is None:
            self.llm_client = GeminiClient(journal_path=self.db.path + ".llm.db")
        return LLMExtractionService(self.llm_client)

    def tick(self, run_id):
        with self.db.transaction() as c:
            run = get_run(c, run_id)
            if run.state not in ACTIVE:
                return run
            try:
                if run.request.mode == "live" and self.live_client is not None:
                    self.live_client.set_run_limit(run.id, run.request.credit_budget)
                self.advance(c, run)
            except IntegrationUnavailable as exc:
                run.error = str(exc)
                transition(run, S.FAILED, run.error)
            if run.request.mode == "live" and self.live_client is not None:
                run.provider_usage = self.live_client.usage(run.id)
            if run.request.llm_enabled and self.llm_client is not None:
                run.llm_usage = self.llm_client.usage(run.id)
            save_run(c, run)
            return run

    def advance(self, c, run):
        pack = pack_for(run.request.category)
        if run.state == S.CREATED:
            transition(run, S.PARSING, "Gemini request parsing enabled." if run.request.llm_enabled else "Reading the request with the bounded reference/structured parser; Gemini disabled for this run.")
        elif run.state == S.PARSING:
            if run.request.llm_enabled:
                run.specs, question = self.llm().parse(run)
                if run.request.mode == "fixture" and not question:
                    # Sample suppliers are keyed by reference items; keep Gemini's saved
                    # interpretation but match on the bounded parser's specs when it recognizes them.
                    fixture_specs, fixture_question = FixtureParser().parse(run.request)
                    if fixture_specs and not fixture_question:
                        run.specs = fixture_specs
            else:
                run.specs, question = FixtureParser().parse(run.request)
            if question:
                run.clarification = Clarification(question=question)
                transition(run, S.WAITING_FOR_CLARIFICATION, question)
            else:
                transition(run, S.DISCOVERING, f"Specifications saved; finding {run.request.mode} suppliers.")
        elif run.state == S.DISCOVERING:
            if run.request.mode == "live":
                client = self.crustdata(run)
                provider = (LiveCompanyDiscovery if pack.live_discovery == "company_search" else LiveDiscovery)(client, run.id)
            else:
                provider = FixtureDiscovery()
            # Simulated bounded provider retries occur on separate persisted ticks.
            attempts = run.stage_attempts.get("discovery", 0) + 1
            run.stage_attempts["discovery"] = attempts
            if run.request.mode == "fixture" and run.request.scenario == "provider_timeout" and attempts < 3:
                run.events.append(WorkflowEvent(state=run.state, message=f"Simulated provider timeout / 429 attempt {attempts}/3; retry on next worker poll."))
                return
            run.suppliers, run.quotes, run.evidence = provider.discover(run.request, run.specs)
            if run.request.mode == "live" and pack.live_discovery == "company_search":
                transition(run, S.VERIFYING, f"Found {len(run.suppliers)} candidate companies in Crustdata's company graph; checking each one next.")
                return
            transition(run, S.VERIFYING, "Canonical company IDs merged; source timestamps saved." + (" Provider retries exhausted; quote facts remain unverified." if attempts == 3 else ""))
        elif run.state == S.VERIFYING:
            live_company = run.request.mode == "live" and pack.live_discovery == "company_search"
            if live_company and self.live_verify_step(run, pack):
                return
            if run.request.llm_enabled:
                self.llm().extract(run)
                run.events.append(WorkflowEvent(state=run.state, message="Gemini provider evidence extraction saved with cited claims and explicit gaps. LLM claims do not authorize purchases."))
            if live_company:
                transition(run, S.RESEARCHING, "Supplier checks saved. Deciding whether any extra data is worth buying.")
                return
            run.checks = []
            for supplier in run.suppliers:
                quote = next(q for q in run.quotes if q.supplier_id == supplier.id)
                run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="identity", result="pass" if supplier.identity == "resolved" else "unverified",
                    reason=f"{run.request.mode} canonical identity resolved." if supplier.identity == "resolved" else "No matching company evidence; this does not prove fraud.", evidence_ids=quote.evidence_ids))
                trust = "authorized" if "authorized" in pack.trust_signals else "factory"
                value = quote.signals.get(trust)
                run.checks.append(SupplierCheck(supplier_id=supplier.id, kind=trust, result="pass" if value is True else "fail" if value is False else "unverified",
                    reason="Synthetic combined trust evidence; hiring alone is not conclusive." if run.request.mode == "fixture" else "Category trust signal not yet verified by live evidence; identity alone is insufficient.", evidence_ids=quote.evidence_ids))
            transition(run, S.RESEARCHING, "Supplier checks saved. Evaluating decision-changing gaps.")
        elif run.state == S.RESEARCHING:
            if self.research_step(c, run, pack):
                return
            transition(run, S.DECIDING, "Research complete or budget unavailable; uncertainty retained.")
        elif run.state == S.DECIDING:
            self.build_plan(run, pack)
            if self.unscreened_finalists(run, pack):
                transition(run, S.SCREENING, "Shortlist drafted. Screening each finalist (who runs it, adverse news, front-company signs) before anything goes out.")
                return
            needs_approval = any(o.state == "PLANNED" for o in run.orders)
            transition(run, S.WAITING_FOR_APPROVAL if needs_approval else S.EXECUTING,
                       "Plan saved; exact amounts require approval." if needs_approval else "Plan saved; executing mock orders or preparing RFQ drafts.")
        elif run.state == S.SCREENING:
            finalists = self.unscreened_finalists(run, pack)
            if finalists:
                if self.screen_finalist(run, finalists[0], pack) == "block":
                    transition(run, S.RESEARCHING, f"{finalists[0].name} blocked by the compliance screen; looking for a replacement.")
                return
            needs_approval = any(o.state == "PLANNED" for o in run.orders)
            transition(run, S.WAITING_FOR_APPROVAL if needs_approval else S.EXECUTING,
                       "Finalists screened. " + ("Exact amounts require approval." if needs_approval else "Preparing RFQ drafts; held ones are marked for your review."))
        elif run.state == S.EXECUTING:
            # Resolve effects from a prior crash before considering any replacement plan.
            for order in run.orders:
                if order.state in {"PAY_PENDING", "RECONCILING"}:
                    previous = self.payments.lookup(order.idempotency_key)
                    if previous:
                        order.payment_id = previous["payment_id"]
                        order_transition(order, "PAID")
                        record_spend(c, run.id, "orders", order.amount_minor, "Simulated supplier order (reconciled)", order.idempotency_key)
            if run.plan_version != fingerprint(run) or any(q.expires_at <= now() for q in run.quotes if any(o.quote_id == q.id and o.state != "PAID" for o in run.orders)):
                transition(run, S.DECIDING, "Plan changed or evidence expired; old approvals cannot authorize a new plan.")
                return
            pending = next((o for o in run.orders if o.state != "PAID"), None)
            if pending:
                self.order_step(c, run, pending)
                return
            if pack.ordering_method == "rfq":
                for decision in run.decisions:
                    for supplier_id in decision.selected_supplier_ids:
                        draft_id = f"{run.id}:{decision.spec_id}:{supplier_id}"
                        if not any(d.id == draft_id for d in run.rfqs):
                            spec = next(s for s in run.specs if s.id == decision.spec_id)
                            supplier = next(s for s in run.suppliers if s.id == supplier_id)
                            run.rfqs.append(rfq_draft(draft_id, run, spec, supplier))
            gaps = any(d.outcome != "selected" for d in run.decisions) or (run.request.request_type == "shortlist" and any(len(d.selected_supplier_ids) < run.request.shortlist_count for d in run.decisions))
            transition(run, S.COMPLETED_WITH_GAPS if gaps else S.COMPLETED, "Finished with explicit gaps." if gaps else "Finished: mock payments reconciled; RFQs remain drafts.")

    def research_step(self, c, run, pack):
        suppliers = {s.id: s for s in run.suppliers}
        if run.request.mode == "live" and pack.live_discovery == "company_search":
            return self.live_research_step(run, pack)
        if run.request.request_type != "purchase" or run.request.mode == "live":
            return False
        for quote in run.quotes:
            if quote.stock is not None:
                continue
            purchase = next((p for p in run.research if p.quote_id == quote.id), None)
            if purchase and purchase.status in {"skipped", "released", "paid"}:
                continue
            if purchase is None:
                price = pack.research_cost_minor
                useful, rationale = worth_buying(quote, price, pack)
                hypothetical = quote.model_copy(update={"stock": max(quote.quantity, 10000)})
                could_qualify = assess(hypothetical, suppliers[quote.supplier_id], run.request, pack).eligible
                existing = [assess(q, suppliers[q.supplier_id], run.request, pack) for q in run.quotes if q.spec_id == quote.spec_id]
                best = min((v.cost.total_minor for v in existing if v.eligible and v.cost), default=None)
                cost = landed_cost(quote, pack)
                could_change = best is None or (cost is not None and cost.total_minor < best)
                purchase = ResearchPurchase(id=f"research:{run.id}:{quote.id}", quote_id=quote.id,
                    question="Can the supplier fulfil the required stock quantity?", quoted_minor=price, rationale=rationale,
                    status="quoted" if useful and could_qualify and could_change else "skipped")
                if purchase.status == "skipped":
                    purchase.rationale += " Other hard checks fail or existing eligible options are cheaper."
                run.research.append(purchase)
                return True
            if purchase.status == "quoted":
                purchase.status = "reserved" if reserve(c, run, purchase.id, purchase.quoted_minor) else "skipped"
                if purchase.status == "skipped": purchase.rationale += " Budget ceiling prevents purchase."
                run.events.append(WorkflowEvent(state=run.state, message=f"Research {purchase.status}: {purchase.quoted_minor / 100:.2f} INR (mock)."))
                return True
            if purchase.status == "reserved":
                paid = self.payments.lookup(purchase.id)
                if not paid and not can_commit(c, run, purchase.id):
                    c.exec_driver_sql("UPDATE reservations SET status='released' WHERE id=?", (purchase.id,))
                    purchase.status = "released"
                    purchase.rationale += " Budget lowered before payment; reservation released."
                    return True
                # Reconcile any previous provider commit before attempting another payment.
                self.payments.pay(purchase.id, purchase.quoted_minor, "research")
                c.exec_driver_sql("UPDATE reservations SET status='settled' WHERE id=?", (purchase.id,))
                record_spend(c, run.id, "research", purchase.quoted_minor, purchase.question, purchase.id)
                fact = Evidence(reference=f"mock-paid-data://{purchase.id}", provider="mock_paid_data", fact="stock", value="10000 units available in synthetic scenario")
                run.evidence.append(fact)
                quote.stock = 10000
                quote.evidence_ids.append(fact.id)
                purchase.evidence_ids = [fact.id]
                purchase.status = "paid"
                return True
        return False

    def qualified_suppliers(self, run, pack):
        suppliers = {s.id: s for s in run.suppliers}
        return {q.supplier_id for q in run.quotes if assess(q, suppliers[q.supplier_id], run.request, pack).eligible}

    def live_verify_step(self, run, pack):
        """Check one supplier per tick. Returns True while there is more to check."""
        checked = {c.supplier_id for c in run.checks if c.kind == "profile"}
        pending = [s for s in run.suppliers if s.id not in checked]
        if not pending:
            return False
        if run.request.request_type == "shortlist":
            qualified = self.qualified_suppliers(run, pack)
            if len(qualified) >= run.request.shortlist_count:
                for s in pending:
                    run.checks.append(SupplierCheck(supplier_id=s.id, kind="profile", result="not_applicable", evidence_ids=[],
                        reason=f"Not checked: {len(qualified)} verified manufacturers already qualify, so this check could not change the shortlist."))
                run.events.append(WorkflowEvent(state=run.state, message=f"Stopped checking: {len(qualified)} makers already qualify. Skipped {len(pending)} paid check(s) that could not change the result."))
                return False
        supplier = pending[0]
        client = self.crustdata(run)
        try:
            report, evidence = LiveCompanyChecks(client, run.id).check(supplier, run.request, run.specs, pack)
        except CreditLimitReached:
            for s in pending:
                run.checks.append(SupplierCheck(supplier_id=s.id, kind="profile", result="unverified", evidence_ids=[],
                    reason="Credit ceiling reached before this supplier was checked; its facts stay unverified."))
            run.events.append(WorkflowEvent(state=run.state, message=f"Credit ceiling reached; {len(pending)} supplier(s) left unverified. Finishing with what we have."))
            return False
        except RateLimited as exc:
            key = "verify:" + supplier.id
            run.stage_attempts[key] = run.stage_attempts.get(key, 0) + 1
            if run.stage_attempts[key] < 3:
                run.events.append(WorkflowEvent(state=run.state, message=f"{exc}. Attempt {run.stage_attempts[key]}/3; retrying on the next step."))
                return True
            run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="profile", result="unverified", evidence_ids=[],
                reason="Crustdata kept rate-limiting this check after 3 attempts; facts stay unverified."))
            return True
        except IntegrationUnavailable as exc:
            run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="profile", result="unverified", evidence_ids=[],
                reason=f"Check failed ({str(exc).split(':')[0]}); facts stay unverified rather than guessed."))
            return True
        run.evidence.extend(evidence)
        ids = [e.id for e in evidence]
        if report is None:
            run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="profile", result="unverified", evidence_ids=ids,
                reason="Crustdata returned no profile for this company; facts stay unverified."))
            return True
        supplier.kind, supplier.profile, supplier.contact = report.kind, report.profile, report.contact
        supplier.name = report.profile.get("name") or supplier.name
        if report.locations:
            supplier.locations = report.locations
        india = "india" in run.request.destination.lower() or "bengaluru" in run.request.destination.lower()
        for quote in (q for q in run.quotes if q.supplier_id == supplier.id):
            quote.signals = {k: report.signals.get(k) for k in pack.trust_signals}
            quote.matches_spec = False if (report.region_match is False) else (True if report.region_match and report.relevant else None)
            quote.serves_destination = report.in_country if india else None
            quote.status = "verified"
            quote.observed_at, quote.expires_at = now(), now() + timedelta(hours=pack.freshness_hours)
            quote.evidence_ids = list(dict.fromkeys(quote.evidence_ids + ids))
        label = {True: "pass", False: "fail", None: "unverified"}
        run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="profile", result="pass", evidence_ids=ids,
            reason=f"Company profile checked: {supplier.name}."))
        run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="identity", result="pass", evidence_ids=ids[:1],
            reason=f"One canonical company in Crustdata (ID {supplier.canonical_id})."))
        run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="location", result=label[report.region_match], evidence_ids=ids[1:2],
            reason=(f"Office in {run.specs[0].parameters.get('location')}: " + ("yes" if report.region_match else "no" if report.region_match is False else "no address data")) if run.specs and run.specs[0].parameters.get("location") else "No region requested."))
        for kind, idx in (("factory", 2), ("stable", 3), ("complaints_clear", 4)):
            reasons = report.reasons.get(kind) or ["No data"]
            run.checks.append(SupplierCheck(supplier_id=supplier.id, kind=kind, result=label[report.signals.get(kind)], evidence_ids=ids[idx:idx + 1],
                reason="; ".join(reasons)))
        verdict = {"manufacturer": "a manufacturer", "trader": "a trader, not a factory", "unknown": "not proven to be a factory"}[report.kind]
        run.events.append(WorkflowEvent(state=run.state, message=f"Checked {supplier.name}: {verdict}."))
        return True

    def live_research_step(self, run, pack):
        """Buy a 1-credit news check only when it could move a maker onto the shortlist."""
        suppliers = {s.id: s for s in run.suppliers}
        qualified = self.qualified_suppliers(run, pack)
        for quote in run.quotes:
            if quote.signals.get("complaints_clear") is not None or quote.status != "verified":
                continue
            earlier = [p for p in run.research if p.quote_id == quote.id]
            if any(p.status == "paid" for p in earlier):
                continue
            # A skip because the shortlist was full is reconsidered once a finalist is blocked.
            if earlier and not ("already full" in earlier[-1].rationale and len(qualified) < run.request.shortlist_count):
                continue
            supplier = suppliers[quote.supplier_id]
            hypothetical = quote.model_copy(update={"signals": dict(quote.signals, complaints_clear=True)})
            could_qualify = assess(hypothetical, supplier, run.request, pack).eligible
            already = quote.supplier_id in qualified
            needed = len(qualified) < run.request.shortlist_count
            pid = f"research:{run.id}:{quote.id}" + (f":{len(earlier)}" if earlier else "")
            if already or not could_qualify or not needed:
                why = ("it already qualifies without it" if already else
                       "other checks rule it out anyway" if not could_qualify else "the shortlist is already full")
                run.research.append(ResearchPurchase(id=pid, quote_id=quote.id, provider="crustdata_web_search", question=f"Any complaints or bad news about {supplier.name}?",
                    quoted_minor=0, quoted_credits="1", status="skipped", rationale=f"Skipped: {why}, so a news check could not change the shortlist."))
                return True
            purchase = ResearchPurchase(id=pid, quote_id=quote.id, provider="crustdata_web_search", question=f"Any complaints or bad news about {supplier.name}?",
                quoted_minor=0, quoted_credits="1", status="quoted",
                rationale=f"{supplier.name} passes every other check; a clean news record would put it on the shortlist. Up to 1 credit to settle it.")
            try:
                start = (now() - timedelta(days=365)).timestamp()
                result = self.crustdata(run).news_search(run.id, f'"{supplier.name}"', start_date=start,
                                                         purpose=f"News check could put {supplier.name} on the shortlist")
            except CreditLimitReached:
                purchase.status, purchase.rationale = "skipped", purchase.rationale + " Blocked: credit ceiling reached; complaints stay unverified."
                run.research.append(purchase)
                return True
            except IntegrationUnavailable as exc:
                purchase.status, purchase.rationale = "skipped", purchase.rationale + f" News search failed ({str(exc).split(':')[0]}); complaints stay unverified."
                run.research.append(purchase)
                return True
            articles = [{"title": r.get("title"), "url": r.get("url")} for r in (result.get("results") or []) if r.get("title")]
            relevant = [a for a in articles if supplier.name.split()[0].lower() in (a["title"] or "").lower()]
            clear, reasons, _ = news_verdict(relevant)
            if clear is None:
                clear, reasons = True, ["No news about this company in the last 12 months (Indian news search)"]
            fact = Evidence(reference="https://api.crustdata.com/web/search/live", provider="crustdata_web_search", mode="live",
                            fact="news and complaints (bought)", value=json.dumps({"verdict": clear, "reasons": reasons, "articles": relevant[:5]}))
            run.evidence.append(fact)
            quote.signals["complaints_clear"] = clear
            quote.evidence_ids.append(fact.id)
            purchase.status, purchase.evidence_ids = "paid", [fact.id]
            run.research.append(purchase)
            run.checks = [ch for ch in run.checks if not (ch.supplier_id == supplier.id and ch.kind == "complaints_clear")]
            run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="complaints_clear", result="pass" if clear else "fail",
                                            evidence_ids=[fact.id], reason="; ".join(reasons)))
            run.events.append(WorkflowEvent(state=run.state, message=f"Bought a news check on {supplier.name} (quoted up to 1 credit; the ledger shows the actual charge): {'clean' if clear else 'red flag found'}."))
            return True
        return False

    def unscreened_finalists(self, run, pack):
        if not (pack.compliance_screen and run.request.mode == "live" and run.decisions):
            return []
        screened = {c.supplier_id for c in run.checks if c.kind == "compliance"}
        chosen = [sid for d in run.decisions for sid in d.selected_supplier_ids]
        return [s for s in run.suppliers if s.id in chosen and s.id not in screened]

    def screen_finalist(self, run, supplier, pack):
        """Who runs it, adverse news on them (namesakes cleared), front-company flags -> approve / hold / block."""
        client = self.crustdata(run)
        people = list((supplier.profile or {}).get("people") or [])
        directors = [p for p in people if is_owner(p.get("title"))]
        source, lookup_failed, evidence = "company profile", None, []
        if not directors:
            try:
                found = client.person_search(run.id, supplier.canonical_id, ["founder", "owner", "proprietor", "director", "partner", "ceo"],
                                             purpose=f"Find who owns or runs {supplier.name}")
                directors, source = [p for p in found if is_owner(p.get("title"))] or found[:1], "Person Search"
            except CreditLimitReached:
                lookup_failed = "Credit ceiling reached before the owner could be looked up"
            except IntegrationUnavailable as exc:
                lookup_failed = f"Owner lookup failed ({str(exc).split(':')[0]})"
        screened = []
        adverse = []
        for person in directors[:2]:
            try:
                start = (now() - timedelta(days=3 * 365)).timestamp()
                result = client.news_search(run.id, f'"{person["name"]}"', start_date=start, purpose=f"Adverse-media screen on {person['name']} ({supplier.name})")
                articles = [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("snippet")} for r in result.get("results") or [] if r.get("title")]
                split = classify_articles(person["name"], supplier, articles)
            except CreditLimitReached:
                lookup_failed = "Credit ceiling reached before every director could be screened"
                break
            except IntegrationUnavailable as exc:
                lookup_failed = f"News screen failed for {person['name']} ({str(exc).split(':')[0]})"
                continue
            adverse += split["adverse"]
            screened.append({"name": person["name"], "title": person.get("title"), "source": source, **split})
            evidence.append(Evidence(reference="https://api.crustdata.com/web/search/live", provider="crustdata_web_search", mode="live",
                fact=f"adverse-media screen: {person['name']}", value=json.dumps(screened[-1])[:8000],
                status="verified"))
        flags = front_company_flags(supplier, run.suppliers)
        result, reasons = compliance_verdict(screened or directors, flags, adverse, lookup_failed)
        run.evidence.extend(evidence)
        supplier.profile = dict(supplier.profile or {}, compliance={"verdict": result, "reasons": reasons, "front_flags": flags,
                                "directors": screened or [dict(d, source=source) for d in directors]})
        run.checks.append(SupplierCheck(supplier_id=supplier.id, kind="compliance", evidence_ids=[e.id for e in evidence],
            result={"approve": "pass", "hold": "unverified", "block": "fail"}[result], reason="; ".join(reasons)))
        if result == "block":
            for quote in (q for q in run.quotes if q.supplier_id == supplier.id):
                quote.signals["compliance"] = False
        label = {"approve": "approved", "hold": "held for your review", "block": "blocked"}[result]
        run.events.append(WorkflowEvent(state=run.state, message=f"Compliance screen on {supplier.name}: {label}. {reasons[0] if reasons else ''}".strip()))
        return result

    def build_plan(self, run, pack):
        suppliers = {s.id: s for s in run.suppliers}
        run.decisions = [decide(s, run.quotes, suppliers, run.request, pack) for s in run.specs]
        run.plan_version = fingerprint(run)
        paid = [o for o in run.orders if o.state == "PAID"]
        run.orders = paid
        if run.request.request_type == "purchase":
            for decision in run.decisions:
                if not decision.selected_quote_id or any(o.spec_id == decision.spec_id for o in paid):
                    continue
                candidate = next(c for c in decision.candidates if c.quote_id == decision.selected_quote_id)
                oid = f"{run.id}:{decision.spec_id}:{candidate.supplier_id}:{run.plan_version}"
                order = Order(id=oid, spec_id=decision.spec_id, supplier_id=candidate.supplier_id, quote_id=candidate.quote_id,
                    plan_version=run.plan_version, amount_minor=candidate.cost.total_minor,
                    idempotency_key=hashlib.sha256(oid.encode()).hexdigest())
                if order.amount_minor <= run.request.approval_threshold_minor:
                    order_transition(order, "APPROVED")
                run.orders.append(order)
        for purchase in run.research:
            if purchase.status == "paid":
                quote = next((q for q in run.quotes if q.id == purchase.quote_id), None)
                purchase.changed_decision = any(d.selected_quote_id == purchase.quote_id or (quote and quote.supplier_id in d.selected_supplier_ids and d.selected_quote_id is None)
                                                for d in run.decisions)

    def order_step(self, c, run, order):
        if order.state == "APPROVED":
            order_transition(order, "PAY_PENDING")
            return  # Durable intent before any provider side effect.
        if order.state not in {"PAY_PENDING", "RECONCILING"}:
            raise ValueError("order is not authorized")
        existing = self.payments.lookup(order.idempotency_key)
        try:
            result = existing or self.payments.pay(order.idempotency_key, order.amount_minor, "orders", run.request.scenario)
        except TimeoutError:
            order_transition(order, "RECONCILING")
            run.events.append(WorkflowEvent(state=run.state, message="Mock checkout timed out after committing; will reconcile by idempotency key."))
            return
        order.payment_id = result["payment_id"]
        order_transition(order, "PAID")
        record_spend(c, run.id, "orders", order.amount_minor, "Simulated supplier order", order.idempotency_key)


def approve(run, approval: Approval):
    if run.state != S.WAITING_FOR_APPROVAL:
        raise ValueError("run is not waiting for approval")
    if approval.plan_version != run.plan_version or run.plan_version != fingerprint(run):
        raise ValueError("stale plan approval; replan required")
    order = next((o for o in run.orders if o.id == approval.order_id), None)
    if order is None or order.amount_minor != approval.amount_minor or order.plan_version != approval.plan_version:
        raise ValueError("approval does not match exact order version and amount")
    if any(q.expires_at <= now() for q in run.quotes if q.id == order.quote_id):
        raise ValueError("quote expired; replan required")
    if order.state == "PLANNED": order_transition(order, "APPROVED")
    run.events.append(WorkflowEvent(state=run.state, message=f"Recorded approval for exact order {order.id}: {order.amount_minor} paise."))
    if all(o.state != "PLANNED" for o in run.orders):
        transition(run, S.EXECUTING, "All required approvals recorded.")


def rfq_draft(draft_id, run, spec, supplier):
    """First email to a verified maker, addressed to a named person when the company graph has one."""
    contact = supplier.contact or {}
    recipient = f"{contact['name']} ({contact['title']})" if contact.get("name") and contact.get("title") else contact.get("name")
    greeting = f"Dear {contact['name'].split()[0]}," if contact.get("name") else f"Dear {supplier.name} team,"
    product = spec.parameters.get("product") or "leather goods"
    region = spec.parameters.get("location")
    why = []
    profile = supplier.profile or {}
    if profile.get("founded"): why.append(f"in business since {profile['founded']}")
    if profile.get("headquarters"): why.append(f"based in {profile['headquarters']}")
    intro = f"We found {supplier.name}" + (f" ({', '.join(why)})" if why else "") + f" while looking for {product} manufacturers" + (f" in {region}" if region else "") + "."
    body = "\n\n".join([
        greeting,
        intro + " We would like to request a quotation.",
        f"Our requirement: {product} made in your own factory, delivered to {run.request.destination} by "
        f"{run.request.deadline.astimezone().strftime('%d %B %Y')}. Quantities will follow once we have your MOQ and prices.",
        "Could you please share:\n"
        "1. The products you make in-house, with photos or a catalogue\n"
        "2. Minimum order quantity and price per unit at MOQ and at 500 units\n"
        "3. Leather types, grades and finishes you offer\n"
        f"4. Production lead time and delivery time to {run.request.destination}\n"
        "5. Certifications (for example ISO 9001 or LWG) and your sample policy",
        "Thank you, and we look forward to your reply.",
    ])
    return RFQDraft(id=draft_id, supplier_id=supplier.id, recipient=recipient,
                    subject=f"Request for quotation: {product}" + (f", {run.request.destination}" if run.request.destination else ""), body=body)
