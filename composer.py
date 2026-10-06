"""compose(category, merchant, trigger, customer) -> dict

Pipeline: resolve facts in code -> kind-specific prompt -> LLM (temp 0) -> validate -> retry once -> template fallback.
"""
import hashlib
import json
import os
import re
import time

import llm

# ---------------------------------------------------------------- guidance per trigger kind
KIND_GUIDE = {
    "research_digest": "Lead with the digest item: cite source + trial size + headline number. Tie it to a cohort in THIS merchant's data (e.g. high_risk_adult_count). Offer to pull the abstract / draft a patient-ed message. Levers: specificity, curiosity, reciprocity. CTA open_ended or binary.",
    "regulation_change": "Compliance alert: state what changes, the exact deadline, source. Say what passes/fails. Offer a concrete audit checklist/SOP draft. Loss aversion (deadline). Calm, precise, no alarmism.",
    "cde_opportunity": "Invite to the event: date/time, credits, fee as in the data, speaker if given. Effort externalization: offer to add to calendar / register. Light, collegial tone.",
    "category_trend_movement": "Cite the trend query and the YoY delta; connect to one of the merchant's offers/services.",
    "recall_due": "CUSTOMER-FACING (send_as merchant_on_behalf). Name the service due and months since last visit, offer the REAL slots from payload, real price from merchant offers. Reply format: 1/2 slot choice allowed. Honour customer language + preferred slots.",
    "appointment_tomorrow": "CUSTOMER-FACING. Friendly reminder of tomorrow's appointment with the actual time/service from the payload; easy reschedule path. Short.",
    "customer_lapsed_soft": "CUSTOMER-FACING. Warm, no-guilt nudge; mention what they last had and a real offer; single easy CTA.",
    "customer_lapsed_hard": "CUSTOMER-FACING. No-shame winback; reference their past goal/focus from payload; low-commitment trial or offer from merchant offers only; single binary CTA (Reply YES).",
    "trial_followup": "CUSTOMER-FACING. Follow up on the trial using payload date; propose the real next session option; single CTA.",
    "wedding_package_followup": "CUSTOMER-FACING. Use days-to-wedding and trial date from payload; propose the next-step program named in payload; only quote prices that exist in merchant offers/catalog; single CTA.",
    "chronic_refill_due": "CUSTOMER-FACING pharmacy refill. List the molecules and run-out date from payload; mention delivery only if payload/offers say so; respectful tone (address family/senior appropriately); CTA: reply CONFIRM. Never invent totals/discounts not in data.",
    "perf_dip": "Name the metric, % drop and window from payload, against the baseline given. Give one plausible, data-backed next action (e.g. active offers, stale posts, unverified profile signals from merchant data). Loss aversion + effort externalization. Do not blame.",
    "perf_spike": "Celebrate with the exact metric/% and the likely_driver if given; propose doubling down on what worked (one concrete action).",
    "seasonal_perf_dip": "Reframe: dip is expected for the season (payload says so). Anchor on the exact delta and the merchant's own numbers; recommend retention over ad spend; don't invent industry percentages that are not in the data.",
    "renewal_due": "Subscription days_remaining + plan + amount from payload; show what they'd lose/keep using their own performance numbers; single binary CTA (Reply YES to renew / get link).",
    "winback_eligible": "Subscription lapsed: use days since expiry, perf dip and lapsed customers added (payload). Loss aversion with their real numbers; offer a simple reactivation step.",
    "dormant_with_vera": "Merchant silent for long. Do NOT guilt-trip. Reciprocity: share one new, concrete, specific thing about their account/category (from data) and ask one very easy question.",
    "festival_upcoming": "Festival name + date + days_until from payload. If far away, be a gentle early-planning nudge; propose a concrete service+price offer from catalog/merchant offers for that festival. One CTA.",
    "ipl_match_today": "Match, venue, time from payload. Give a judgment call (e.g. match-night combo vs delivery push) grounded in the merchant's active offers and is_weeknight flag. Do not invent cover-percentage stats. Offer to draft the banner/post.",
    "review_theme_emerged": "Quote the theme + occurrences + common_quote from payload. Propose a concrete fix + an offer to draft a reply/response. Effort externalization.",
    "milestone_reached": "Celebrate the specific milestone (value_now vs milestone_value); suggest a simple way to use it (post/ask for reviews). Light, warm.",
    "curious_ask_due": "ASKING-THE-MERCHANT lever. One easy, specific question about their business this week; promise a concrete output (post / reply draft) from their answer. Reference a real service from their offers to make it specific. open_ended CTA.",
    "active_planning_intent": "The merchant already asked for this. Skip qualifying: deliver a complete first draft (structure, tiers, prices ONLY as suggestions labelled 'suggested'), reference merchant data; end by offering the next artifact. No pitch.",
    "competitor_opened": "Use competitor name, distance, their offer, open date exactly as in payload. Calm positioning advice using the merchant's own offers/strengths (ratings, reviews, retention) — no panic, no fabrication.",
    "supply_alert": "Urgent: molecule, batches, manufacturer from payload. Use merchant's chronic-Rx customer count ONLY if it exists in data; otherwise offer to pull the list. Offer drafted customer note + replacement workflow.",
    "category_seasonal": "List the seasonal shifts from payload with their numbers; recommend a concrete shelf/menu/offer action; offer to draft the update.",
    "gbp_unverified": "Profile unverified: state verification path from payload and estimated uplift; make it a 2-step easy task; offer to guide.",
}
DEFAULT_GUIDE = "Anchor on the most specific fact in the trigger payload, explain why now, and end with one low-friction CTA."

CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "customer_lapsed_soft", "customer_lapsed_hard",
                  "trial_followup", "wedding_package_followup", "chronic_refill_due"}

SYSTEM = """You are Vera, magicpin's merchant-growth assistant on WhatsApp, writing ONE message. You are judged on: specificity, category fit, merchant fit, trigger relevance (why now), engagement compulsion.

HARD RULES
1. Use ONLY facts present in the provided JSON (numbers, dates, names, offers, sources). Never invent stats, competitors, research, slots, prices or batch numbers. Small derived arithmetic (e.g. months since a date, % gap vs peer) is fine.
2. Anchor on 1-2 verifiable facts (number/date/headline/source). Cite the source for research/compliance items.
3. Make the "why now" (the trigger) obvious in the first sentence or two.
4. Service+price phrasing ("Haircut @ ₹99") over "% off". Use the merchant's own active offers or the category catalog; never invent an offer.
5. Exactly ONE call-to-action, in the LAST sentence. Binary (Reply YES) for action triggers; open question for curiosity triggers; none only for pure information. Slot-choice (1/2) is allowed only for customer bookings.
6. Voice: peer/colleague, not promotional. Respect the category voice and NEVER use its taboo words. No hype, no emojis spam (0-1 emoji; customer messages may use 1).
7. Language: if the target's language includes Hindi (hi / hi-en mix), write natural Hinglish in Roman script (e.g. "Aapke liye", "Kya aap..."), else English. Match the customer's language_pref for customer messages.
8. Address merchant-facing messages by owner first name (dentists: "Dr. <first name>"). No self-introduction ("I'm Vera...") and no long preamble ("Hope you're well").
9. Levers to use (pick 1-3): specificity, loss aversion, social proof (only with real peer_stats/data), effort externalization ("I've drafted X — say go"), curiosity, reciprocity, asking the merchant.
10. Keep it tight: merchant-facing 280-520 characters; customer-facing 200-420. Plain WhatsApp text (no markdown headers; *bold* sparingly).
11. Do not repeat anything already in conversation_history bodies verbatim; build on it if relevant.
12. Customer-facing (send_as = merchant_on_behalf): speak AS the merchant's business ("<Clinic> here"), not as Vera; honour language, consent and preferred slots; no medical claims, no "guaranteed".

13. NEVER invent trigger details. If trigger.payload is empty/"placeholder", the ONLY thing you know about the event is its kind; derive the hook from the merchant/customer/category data (signals, performance deltas, offers, review_themes, customer relationship, digest items, seasonal_beats) and say nothing about specifics (competitor names/distances/discounts, match fixtures, stats) that are not provided.
14. "peer_benchmarks" are category averages, NOT this merchant's numbers: never say "your rating is 4.4" from a benchmark; compare explicitly ("peer avg 3.0% vs your 2.1%").
15. Don't state anything the merchant could check and find false: no unverifiable percentages ("+18% covers"), no claims of what you already did unless the prompt says you did it. Offering to draft/pull/prepare is fine.
16. If the trigger kind doesn't fit the category (e.g. a medicine refill for a dentist), write the closest category-natural equivalent (recall / follow-up) without inventing details.
17. Time honesty: if days_until/due dates are far away (>30 days), frame as early planning, not "just around the corner".
18a. Write ONLY in Roman/Latin script (English or Hinglish). NEVER use Devanagari or other scripts. Never open with 'Hope you're doing well'-style filler. Never invent counts of visits/orders: use only numbers present in the customer/merchant data.
18. WhatsApp formatting: plain text, *single asterisks* for bold at most once, no markdown headers, no ** double asterisks; short paragraphs.
19. Customer-facing messages must never contain anything the customer's consent scope doesn't cover (e.g. don't upsell in a reminder).

20. Customer-facing messages must be concrete: use the customer's first name, the last visit date and the last service from customer.relationship (readable form, e.g. "12 May"), preferred slot style from customer.preferences, and ONE real merchant offer or slot from the data; for appointment reminders say 'tomorrow' and offer an easy reschedule, and mention the service ONLY if customer.relationship.services_received is non-empty (never invent an appointment date, time or service; if services_received is empty say nothing about the service); end with one clear reply instruction. Dates must be readable ("1 Apr"), never ISO "2026-04-01".
21. Always use one concrete number or named item from the data in merchant-facing messages too; "trend" claims need a source in the data.

OUTPUT: strict JSON only: {"body": str, "cta": "open_ended"|"binary_yes_no"|"multi_choice_slot"|"none", "rationale": "1-2 sentences: which fact anchors it, which trigger, which lever"}"""


# ---------------------------------------------------------------- fact resolution
def _find_digest_item(category: dict, trigger: dict):
    p = trigger.get("payload", {}) or {}
    ids = [p.get("top_item_id"), p.get("digest_item_id"), p.get("alert_id")]
    for it in category.get("digest", []) or []:
        if it.get("id") in ids:
            return it
    return None


def _first_name(merchant: dict) -> str:
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name", "").split()[0]


def _salutation(category: dict, merchant: dict) -> str:
    fn = _first_name(merchant)
    if category.get("slug") == "dentists" and not fn.lower().startswith("dr"):
        return f"Dr. {fn}"
    return fn


def _lang_instruction(merchant: dict, customer: dict | None) -> str:
    if customer:
        pref = (customer.get("identity", {}).get("language_pref") or "english").lower()
        if "hi" in pref or "hinglish" in pref:
            return f"Customer language_pref='{pref}': write Hinglish (Roman script)."
        return f"Customer language_pref='{pref}': write in that language mix, default simple English (Roman script only)."
    langs = merchant.get("identity", {}).get("languages", ["en"])
    if "hi" in langs:
        return f"Merchant languages={langs}: write natural Hindi-English mix (Roman script), mostly English technical terms."
    return f"Merchant languages={langs}: write in English (a light local greeting is fine)."


def _derived(category: dict, merchant: dict, trigger: dict) -> dict:
    d = {}
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    ctr, pctr = perf.get("ctr"), peer.get("avg_ctr")
    if ctr is not None and pctr:
        d["ctr_pct"] = f"{ctr*100:.1f}%"
        d["peer_avg_ctr_pct"] = f"{pctr*100:.1f}%"
        d["ctr_vs_peer"] = "below" if ctr < pctr else "above"
        d["ctr_gap_pct_points"] = f"{abs(ctr-pctr)*100:.1f}"
    if perf.get("views") and peer.get("avg_views_30d"):
        d["views_vs_peer_avg"] = f"{perf['views']} vs peer avg {peer['avg_views_30d']}"
    if perf.get("calls") is not None and peer.get("avg_calls_30d"):
        d["calls_vs_peer_avg"] = f"{perf['calls']} vs peer avg {peer['avg_calls_30d']}"
    agg = merchant.get("customer_aggregate", {}) or {}
    if agg.get("total_unique_ytd") and agg.get("lapsed_180d_plus"):
        d["lapsed_share_pct"] = f"{agg['lapsed_180d_plus']/agg['total_unique_ytd']*100:.0f}%"
    return d


def _active_offers(merchant: dict):
    return [o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active"]


def build_context(category, merchant, trigger, customer, now=None) -> dict:
    kind = trigger.get("kind", "")
    cust_scope = bool(customer) or trigger.get("scope") == "customer" or kind in CUSTOMER_KINDS
    cat_view = {
        "slug": category.get("slug"),
        "voice": {k: v for k, v in (category.get("voice") or {}).items() if k != "tone_examples"},
        "peer_benchmarks_NOT_this_merchant": category.get("peer_stats"),
        "offer_catalog": [o.get("title") for o in category.get("offer_catalog", [])][:10],
        "seasonal_beats": category.get("seasonal_beats"),
        "trend_signals": category.get("trend_signals"),
    }
    # the digest item the trigger points at is resolved in code; other items are offered as optional extras
    item = _find_digest_item(category, trigger)
    others = [{k: v for k, v in it.items() if k in ("id", "kind", "title", "source", "summary", "date", "credits")}
              for it in category.get("digest", []) if it is not item][:4]
    m_view = {
        "merchant_id": merchant.get("merchant_id"),
        "identity": merchant.get("identity"),
        "subscription": merchant.get("subscription"),
        "performance": merchant.get("performance"),
        "active_offers": _active_offers(merchant),
        "all_offers": merchant.get("offers"),
        "customer_aggregate": merchant.get("customer_aggregate"),
        "signals": merchant.get("signals"),
        "review_themes": merchant.get("review_themes"),
        "conversation_history_last3": (merchant.get("conversation_history") or [])[-3:],
    }
    ctx = {
        "send_as": "merchant_on_behalf" if cust_scope else "vera",
        "address_as": _salutation(category, merchant),
        "language_instruction": _lang_instruction(merchant, customer if cust_scope else None),
        "category": cat_view,
        "merchant": m_view,
        "derived": _derived(category, merchant, trigger),
        "trigger": {"kind": kind, "urgency": trigger.get("urgency"), "source": trigger.get("source"),
                    "payload": trigger.get("payload"), "expires_at": trigger.get("expires_at")},
        "resolved_digest_item": item,
        "other_digest_items_optional": others,
        "now": now,
        "trigger_payload_is_placeholder": bool((trigger.get("payload") or {}).get("placeholder")),
    }
    if customer:
        ctx["customer"] = customer
    return ctx


# ---------------------------------------------------------------- validation
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text):
        n = m.replace(",", "").rstrip(".")
        if n:
            out.add(n)
    return out


def validate(body: str, ctx: dict, category: dict) -> list[str]:
    issues = []
    b = body.strip()
    low = b.lower()
    if len(b) < 40:
        issues.append("too short")
    if len(b) > 900:
        issues.append("too long (keep under ~600 chars)")
    for t in (category.get("voice", {}).get("vocab_taboo") or []):
        t0 = re.sub(r"\(.*?\)", "", t).strip().lower()
        if t0 and re.search(r"\b" + re.escape(t0) + r"\b", low):
            issues.append(f"uses taboo word '{t0}'")
    if re.search(r"[\u0900-\u097F\u0C00-\u0C7F\u0B80-\u0BFF]", b):
        issues.append("must be Roman script only (no Devanagari/Telugu/Tamil letters)")
    if re.search(r"(we hope you|hope you('| a)re|i hope you|i'm reaching out|i am reaching out)", low[:80]):
        issues.append("long preamble")
    if re.search(r"\b[a-z]+_[a-z_]+\b", b):
        issues.append("raw snake_case token in text; write it as normal words")
    if re.search(r"\d{4}-\d{2}-\d{2}", b):
        issues.append("write dates in readable form like '15 Dec 2026', not ISO 2026-12-15")
    if "**" in b:
        issues.append("use single * for bold, not **")
    # numbers must come from the contexts
    corpus = json.dumps({k: v for k, v in ctx.items() if k != "now"}, ensure_ascii=False)
    allowed = _numbers(corpus)
    for k in ("now",):
        if ctx.get(k):
            allowed |= _numbers(str(ctx[k]))
    bad = []
    for n in _numbers(b):
        if n in allowed:
            continue
        try:
            f = float(n)
            if f in (1, 2, 3, 4, 5, 10, 15, 20, 30, 60, 90):  # tiny counts / durations / reply options
                continue
            if any(abs(float(a) - f) < 1e-9 for a in allowed if re.fullmatch(r"\d+\.?\d*", a)):
                continue
            # percent forms, e.g. 0.38 -> 38
            if any(abs(float(a) * 100 - f) < 0.51 for a in allowed if re.fullmatch(r"0?\.\d+", a)):
                continue
            # derived differences / thousands separators handled by allowed set; allow small derived ints (months, days)
            if f <= 31:
                continue
        except ValueError:
            pass
        bad.append(n)
    if bad:
        issues.append(f"numbers not found in context: {bad[:6]} (remove or replace with provided facts)")
    # dates (day + month) must exist in the context (ISO or "5 Nov" style)
    MON = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    have = set()
    for y, mo, d in re.findall(r"(\d{4})-(\d{2})-(\d{2})", corpus):
        have.add((int(d), int(mo)))
    for d, mo in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", corpus, flags=re.I):
        have.add((int(d), MON.index(mo.lower()[:3]) + 1))
    for d, mo in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*", b, flags=re.I):
        if (int(d), MON.index(mo.lower()[:3]) + 1) not in have:
            issues.append(f"date '{d} {mo}' is not in the context; remove it")
    # single CTA: at most 2 question marks, and a question/CTA should end the message
    if b.count("?") > 2:
        issues.append("multiple questions/CTAs; keep exactly one")
    if re.search(r"https?://", b) and "http" not in json.dumps(ctx):
        issues.append("invented URL")
    return issues


CHECK_SYS = """You are a strict fact-checker and editor for a WhatsApp message. You get CONTEXT JSON and a DRAFT.
1) List every claim in the draft not supported by the context (invented numbers/names/offers/dates, benchmark misattributed to the merchant, claims of actions already done, wrong language for the audience, markdown **, multiple CTAs, taboo words, generic filler).
2) Return a corrected body that keeps the strongest specific anchor(s), makes the trigger ("why now") obvious, ends with exactly one CTA, and removes or fixes every unsupported claim. Do not shorten into something generic: keep concrete facts that ARE supported. Keep language/voice. Keep the same audience (merchant vs customer).
Return strict JSON: {"problems": [str], "body": str, "cta": "open_ended"|"binary_yes_no"|"multi_choice_slot"|"none"}"""


def _fact_check(ctx: dict, draft: dict, kind: str):
    user = (f"TRIGGER KIND: {kind}\nCONTEXT JSON:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"DRAFT:\n{draft.get('body')}\n\nReturn JSON.")
    out = llm.parse_json(llm.complete(CHECK_SYS, user, budget_s=14.0))
    body = str(out.get("body", "")).strip()
    return (body, out.get("cta", draft.get("cta")), out.get("problems", [])) if body else None


# ---------------------------------------------------------------- fallback templates
def _fallback(category, merchant, trigger, customer, ctx) -> dict:
    """Deterministic, fact-only message used when no LLM answer is available."""
    kind = trigger.get("kind", "")
    p = trigger.get("payload", {}) or {}
    name = ctx["address_as"]
    item = ctx.get("resolved_digest_item")
    offers = ctx["merchant"]["active_offers"]
    ident = merchant.get("identity", {})
    perf = merchant.get("performance", {}) or {}
    d = ctx["derived"]
    send_as, cta = ctx["send_as"], "binary_yes_no"

    def pct(v):
        return f"{abs(v) * 100:.0f}%"

    if customer:
        cname = (customer.get("identity", {}).get("name") or "").split(" (")[0]
        biz = ident.get("name", "our clinic")
        last = (customer.get("relationship") or {}).get("last_visit")
        slots = p.get("available_slots") or p.get("next_session_options") or []
        labels = " ya ".join(s.get("label", "") for s in slots[:2])
        body = f"Hi {cname}, {biz} here. "
        if kind == "appointment_tomorrow":
            body += "Aapka appointment kal hai — reply YES to confirm, ya time badalna ho to bata dijiye."
        elif kind == "chronic_refill_due" and p.get("molecule_list"):
            body += (f"Aapki {', '.join(p['molecule_list'])} ki refill {str(p.get('stock_runs_out_iso', ''))[:10]} "
                     f"tak khatam hogi. Reply CONFIRM, hum ready rakhenge.")
        else:
            body += (f"Aapki last visit {last} ko thi" if last else "Kaafi time ho gaya")
            body += (f", ab {str(p.get('service_due', 'check-up')).replace('_', ' ')} due hai. "
                     if kind == "recall_due" else " — aapko wapas dekhna accha lagega. ")
            if labels:
                body += f"Slots: {labels}. "
            if offers:
                body += f"{offers[0]}. "
            body += "Reply YES to book, or tell us a time that works."
        return {"body": body, "cta": "multi_choice_slot" if labels else "binary_yes_no", "send_as": send_as,
                "rationale": f"Fact-only template for {kind}: real slots/offer/last visit from customer + merchant data."}

    lead = f"{name}, "
    if item:
        body = lead + f"{item.get('title')} ({item.get('source', '')}). "
        if item.get("summary"):
            body += item["summary"][:200].rstrip(".") + ". "
        if item.get("actionable"):
            body += item["actionable"].rstrip(".") + ". "
        body += "Want me to pull the details and draft a short note you can share?"
    elif kind in ("perf_dip", "seasonal_perf_dip") and p.get("delta_pct") is not None:
        body = lead + f"your {p.get('metric', 'calls')} are down {pct(p['delta_pct'])} over {p.get('window', '7d')}"
        body += (f" (baseline {p['vs_baseline']})" if p.get("vs_baseline") else "") + ". "
        if kind == "seasonal_perf_dip":
            body += "This is the expected seasonal dip — retention matters more than ad spend right now. "
        else:
            body += f"Active offer: {offers[0]}. " if offers else "You have no active offer right now. "
        body += "Want me to draft a fix for you?"
    elif kind in ("perf_dip", "perf_spike") and p.get("delta_pct") is None:
        body = (lead + f"last 30d: {perf.get('views')} views, {perf.get('calls')} calls, CTR {d.get('ctr_pct')} "
                f"vs peer {d.get('peer_avg_ctr_pct')}. Want me to build a plan from this?")
    elif kind == "perf_spike":
        body = lead + (f"your {p.get('metric', 'calls')} are up {pct(p.get('delta_pct', 0))} over "
                       f"{p.get('window', '7d')}. Want me to build on it with one more post?")
    elif kind == "festival_upcoming" and p.get("festival"):
        body = lead + f"{p['festival']} is on {p.get('date')} ({p.get('days_until')} days away) — early planning window. "
        body += f"Want me to draft a festive post around {offers[0]}?" if offers else "Want me to draft a festive offer + post?"
    elif kind == "competitor_opened" and p.get("competitor_name"):
        body = lead + f"{p['competitor_name']} opened {p.get('distance_km')} km away on {p.get('opened_date')}"
        body += (f" with {p['their_offer']}" if p.get("their_offer") else "") + ". "
        body += (f"Your {offers[0]} is live. " if offers else "") + "Want me to draft a positioning post?"
    elif kind == "milestone_reached" and p.get("value_now"):
        body = lead + (f"you're at {p['value_now']} {p.get('metric', 'reviews').replace('_', ' ')}, "
                       f"{p.get('milestone_value')} is next. Want me to draft a short note asking happy customers to review?")
    elif kind == "gbp_unverified":
        body = lead + (f"your Google profile is unverified; verification ({str(p.get('verification_path', '')).replace('_', ' ')}) "
                       f"can lift visibility ~{pct(p.get('estimated_uplift_pct', 0))}. Want me to walk you through it?")
    elif kind == "renewal_due":
        body = lead + (f"your {p.get('plan', '')} plan ends in {p.get('days_remaining')} days "
                       f"(renewal ₹{p.get('renewal_amount')}). Reply YES and I'll send the renewal link.")
    elif kind == "curious_ask_due":
        ex = offers[0] if offers else "your top service"
        body = lead + (f"quick one — which service has been asked for most this week: {ex} or something else? "
                       f"I'll turn your answer into a Google post + reply draft.")
        cta = "open_ended"
    elif kind in ("dormant_with_vera", "winback_eligible"):
        body = lead + (f"quick check-in — {perf.get('views')} people viewed your listing in 30 days and "
                       f"{perf.get('calls')} called. Want me to show what's driving it?")
    elif kind == "ipl_match_today":
        body = lead + f"{p.get('match')} at {p.get('venue')} tonight. "
        body += f"Want me to draft a banner for {offers[0]}?" if offers else "Want me to draft a match-night post?"
    elif kind == "supply_alert":
        body = lead + (f"{p.get('molecule')} recall: batches {', '.join(p.get('affected_batches', []))} "
                       f"(mfr {p.get('manufacturer')}). Want me to draft the customer note + replacement workflow?")
    else:
        body = lead + (f"{perf.get('views')} views and {perf.get('calls')} calls in the last 30 days at "
                       f"{ident.get('locality', 'your location')} ({kind.replace('_', ' ')}). Want me to draft the next step?")
    return {"body": body, "cta": cta, "send_as": send_as,
            "rationale": f"Fact-only template for {kind}; anchors on trigger payload/merchant numbers (LLM unavailable)."}


# ---------------------------------------------------------------- compose
_cache: dict[str, dict] = {}


def _key(*objs) -> str:
    return hashlib.sha256(json.dumps(objs, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, now: str | None = None) -> dict:
    ck = _key(category, merchant, trigger, customer)
    if ck in _cache:
        return dict(_cache[ck])
    ctx = build_context(category, merchant, trigger, customer, now=None)
    kind = trigger.get("kind", "")
    result = None
    if llm.available():
        user = (f"TRIGGER KIND: {kind}\nGUIDE: {KIND_GUIDE.get(kind, DEFAULT_GUIDE)}\n\n"
                f"CONTEXT JSON:\n{json.dumps(ctx, ensure_ascii=False)}\n\nReturn the JSON now.")
        best, best_issues = None, None
        t_start = time.time()
        for attempt in range(2):
            if attempt and time.time() - t_start > 11:
                break
            try:
                raw = llm.complete(SYSTEM, user, budget_s=12.0 if attempt == 0 else 8.0)
                out = llm.parse_json(raw)
                body = str(out.get("body", "")).strip()
                issues = validate(body, ctx, category)
                if best is None or len(issues) < len(best_issues):
                    best, best_issues = out, issues
                if not issues:
                    break
                user += f"\n\nYour previous draft had problems: {issues}. Fix them and return the JSON again."
            except Exception:  # noqa: BLE001
                break
        if best and best.get("body") and os.environ.get("FACT_CHECK", "0") == "1":
            try:
                fc = _fact_check(ctx, best, kind)
                if fc and not validate(fc[0], ctx, category):
                    best = {**best, "body": fc[0], "cta": fc[1] or best.get("cta")}
                    best_issues = []
            except Exception:  # noqa: BLE001
                pass
        if best and (not best_issues or all(i.startswith("numbers not found") for i in best_issues)) and best.get("body"):
            result = {"body": best["body"].strip(), "cta": best.get("cta", "open_ended"),
                      "send_as": ctx["send_as"], "rationale": str(best.get("rationale", ""))[:400]}
    if result is None:
        result = _fallback(category, merchant, trigger, customer, ctx)
        result["_fallback"] = True
    if result["cta"] not in ("open_ended", "binary_yes_no", "multi_choice_slot", "none"):
        result["cta"] = "open_ended"
    result["suppression_key"] = trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id')}")
    if not result.get("_fallback"):
        _cache[ck] = result
    return dict(result)
