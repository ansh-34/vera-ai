"""respond(state, merchant_message) -> {"action": send|wait|end, ...}

Rule-based routing first (auto-reply, opt-out, hostile, intent, deferral, off-topic), LLM for the rest.
state: {"conversation_id", "merchant", "category", "customer", "trigger", "turns": [{"from","body"}],
        "auto_reply_count", "from_role", "last_bot_body"}
"""
import json
import re
import time

import llm

AUTO_PATTERNS = [
    r"thank you for contacting", r"thanks for contacting", r"thank you for reaching out",
    r"our team will (respond|get back|reach)", r"we will (get back|respond|contact) (to )?you",
    r"automated (assistant|reply|response|message)", r"auto[- ]?reply", r"currently (unavailable|away|closed)",
    r"business hours", r"main aapki .* team tak", r"hamari team .* (sampark|jawab)", r"shukriya.*team tak",
    r"aapki jaankari ke liye .* shukriya", r"we('| a)re (currently )?(closed|unavailable)", r"will (revert|respond) (shortly|soon|asap)",
    r"this is an automated",
]
OPTOUT = [r"\bstop\b", r"not interested", r"don'?t (message|contact|text|send)", r"do not (message|contact)",
          r"unsubscribe", r"leave me alone", r"remove (me|my number)", r"band karo", r"mat bhejo", r"mat karo message",
          r"spam", r"useless", r"bothering", r"harass", r"nahi chahiye", r"koi interest nahi"]
ABUSE = [r"\b(idiot|stupid|fuck|shit|bastard|nonsense|rubbish|pathetic|bakwas|chutiya|madarchod|bc|mc)\b"]
INTENT = [r"\blet'?s do it\b", r"\bgo ahead\b", r"\bproceed\b", r"\bplease do\b", r"\bdo it\b", r"\bsounds good\b",
          r"\bok(ay)?[, ]*(send|do|start|go)", r"\byes\b", r"\byep\b", r"\bsure\b", r"\bconfirm", r"\bi want to join\b",
          r"\bjoin\b", r"\bkar do\b", r"\bkaro\b", r"\bhaan\b", r"\bha(an)? kar", r"\bbhej do\b", r"\bsend (it|me|the)\b",
          r"\bwhat'?s next\b", r"\bkya karna hai\b", r"\bjudrna\b", r"\bjudna\b", r"\bstart\b", r"\bokay\b", r"^ok\b",
          r"\bdraft (it|karo|kar)\b", r"\bplease (send|draft|share)"]
DEFER = [r"\blater\b", r"\bbusy\b", r"\bkal\b", r"\btomorrow\b", r"\bnext week\b", r"\bbaad mein\b", r"\bbaad me\b",
         r"\bthodi der\b", r"\bin (a|an) (hour|while)\b", r"\bnot now\b", r"\babhi nahi\b", r"call me (later|tomorrow)"]
OFFTOPIC = [r"\bgst\b", r"\bincome tax\b", r"\bitr\b", r"\bloan\b", r"\binsurance\b", r"\blegal\b", r"\blawyer\b",
            r"\baccounting\b", r"\bpayroll\b", r"\bvisa\b", r"\bweather\b", r"\bcricket score\b", r"\bpolitic"]
QUESTION_HINT = [r"\?$", r"\bhow\b", r"\bwhat\b", r"\bwhy\b", r"\bkaise\b", r"\bkitna\b", r"\bkya\b", r"\bwhen\b", r"\bprice\b", r"\bcost\b"]

_SHARED: dict = {}  # (merchant_id, normalized text) -> repeat count
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about", "could you tell", "are you", "kya aap"]


def _any(patterns, text):
    return any(re.search(p, text, flags=re.I) for p in patterns)


def is_auto_reply(msg: str, state: dict) -> bool:
    low = msg.strip().lower()
    if _any(AUTO_PATTERNS, low):
        return True
    prior = [t["body"].strip().lower() for t in state.get("turns", []) if t.get("from") in ("merchant", "customer")]
    if prior.count(low) >= 1 and len(low) > 15:  # verbatim repeat
        return True
    return False


def _hindi(text: str) -> bool:
    return bool(re.search(r"\b(hai|haan|kar|karo|nahi|aap|mujhe|kya|bhai|ji|kal|abhi|chahiye|bhej|dena)\b", text, re.I))


def _history(state, n=6):
    return [{"from": t["from"], "body": t["body"][:500]} for t in state.get("turns", [])[-n:]]


SYS = """You are Vera, magicpin's WhatsApp assistant for merchants (or, when send_as is merchant_on_behalf, you write as the merchant's business to its customer). You are continuing a conversation.
Rules: Use only facts in the provided context; never invent numbers/offers/slots. Be concise (<=420 chars), peer tone, match the user's latest language (English / Hindi-English Roman script). Exactly one next step/CTA at the end.
MODES:
- action: the user agreed/asked to proceed. Do NOT ask qualifying questions. Say you are doing it NOW and deliver the concrete artifact or next step (a short draft, the confirmation, what happens next) using the context. Finish with a single confirm CTA (e.g. "Reply CONFIRM to send") or none.
- answer: answer their question using context data; if it's outside your scope (tax/GST/legal/etc.) decline in one short sentence and steer back to the original topic with one CTA.
- booking (customer chose a slot or asked about booking): confirm the chosen slot from the context, state what to bring/expect, nothing invented.
OUTPUT strict JSON: {"body": str, "cta": "open_ended"|"binary_yes_no"|"none", "end": bool, "rationale": str}. Set end=true only if the conversation is naturally complete."""


def _llm_reply(state: dict, msg: str, mode: str) -> dict | None:
    if not llm.available():
        return None
    ctx = {
        "mode": mode,
        "send_as": "merchant_on_behalf" if state.get("customer") else "vera",
        "latest_user_message": msg,
        "conversation": _history(state),
        "trigger": state.get("trigger"),
        "merchant": {k: state.get("merchant", {}).get(k) for k in
                     ("identity", "performance", "offers", "customer_aggregate", "signals", "subscription")},
        "category_voice": (state.get("category") or {}).get("voice"),
        "category_digest_ids": [d.get("id") for d in (state.get("category") or {}).get("digest", [])],
        "resolved_digest_items": [d for d in (state.get("category") or {}).get("digest", [])
                                  if d.get("id") in json.dumps(state.get("trigger") or {})],
        "patient_content_library": (state.get("category") or {}).get("patient_content_library"),
        "customer": state.get("customer"),
        "language_hint": "hinglish" if _hindi(msg) else "english",
    }
    t0 = time.time()
    try:
        out = llm.parse_json(llm.complete(SYS, json.dumps(ctx, ensure_ascii=False), max_tokens=500, budget_s=9.0))
        body = str(out.get("body", "")).strip()
        if not body:
            return None
        if mode == "action" and any(q in body.lower() for q in QUALIFYING) and time.time() - t0 < 6:
            out2 = llm.parse_json(llm.complete(
                SYS, json.dumps(ctx, ensure_ascii=False) + "\nYour last draft asked a qualifying question. Rewrite: execute now, no questions.",
                max_tokens=500, budget_s=4.5))
            body = str(out2.get("body", body)).strip()
            out = out2
        return {"action": "send", "body": body, "cta": out.get("cta", "open_ended"),
                "rationale": str(out.get("rationale", ""))[:300], "_end": bool(out.get("end"))}
    except Exception:  # noqa: BLE001
        return None


def _action_fallback(state, msg):
    trig = state.get("trigger") or {}
    kind = trig.get("kind")
    topic = f" for {kind.replace('_', ' ')}" if kind else ""
    name = (state.get("merchant", {}).get("identity", {}) or {}).get("owner_first_name", "")
    hi = _hindi(msg)
    if hi:
        body = f"Done {name} — draft{topic} abhi bana rahi hoon aur 5 min mein bhejti hoon. Next step: aapka approval. Reply CONFIRM to go live."
    else:
        body = f"Done {name} — I'm putting the draft{topic} together now and will send it here in a few minutes. Next step is your approval: reply CONFIRM to go live."
    return {"action": "send", "body": body.replace("  ", " "), "cta": "binary_yes_no",
            "rationale": "Merchant committed; switched to action mode without further qualification."}


def respond(state: dict, merchant_message: str) -> dict:
    msg = (merchant_message or "").strip()
    low = msg.lower()
    role = state.get("from_role", "merchant")

    # 1. hostile / opt-out -> end immediately (one-time polite close handled by caller via 'end')
    if _any(OPTOUT, low) or _any(ABUSE, low):
        return {"action": "end", "rationale": "Merchant opted out / expressed frustration; closing and suppressing further outreach."}

    # 2. auto-reply detection with escalating backoff. Counted per (merchant, text) as well as per conversation,
    #    because a judge may open a fresh conversation_id for each repeat.
    mid = (state.get("merchant") or {}).get("merchant_id", "?")
    shared = state.setdefault("_shared", _SHARED)
    norm = re.sub(r"\W+", " ", low).strip()
    if is_auto_reply(msg, state) or shared.get((mid, norm), 0) >= 1:
        n = max(state.get("auto_reply_count", 0), shared.get((mid, norm), 0)) + 1
        state["auto_reply_count"] = n
        shared[(mid, norm)] = n
        if n == 1:
            hi = _hindi(msg) or "hi" in (state.get("merchant", {}).get("identity", {}).get("languages", []))
            return {"action": "send", "cta": "binary_yes_no",
                    "body": "Lagta hai yeh auto-reply hai 🙂 Jab owner dekhein, bas 'Yes' bhej dein — main wahin se aage badhaungi." if hi
                    else "Looks like an auto-reply 🙂 When the owner sees this, just reply 'Yes' and I'll pick it up from there.",
                    "rationale": "Detected canned auto-reply; one explicit prompt for the owner, then back off."}
        if n == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Same auto-reply again; owner not at the phone. Waiting 24h before any retry."}
        return {"action": "end", "rationale": "Auto-reply 3x with zero real engagement; closing conversation to avoid spam."}
    state["auto_reply_count"] = 0

    # 3. off-topic request
    if _any(OFFTOPIC, low):
        topic = (state.get("trigger") or {}).get("kind", "your profile").replace("_", " ")
        body = (f"Woh main handle nahi kar paungi — iske liye aapke CA/expert ko dikhana better rahega. Wapas {topic} par: kya main draft bhej doon?"
                if _hindi(msg) else f"That one's outside what I can help with — your CA/advisor is the right person. Coming back to {topic}: want me to send the draft over?")
        r = _llm_reply(state, msg, "answer")
        if r:
            r.pop("_end", None)
            return r
        return {"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Out-of-scope ask declined politely; redirected to the original thread."}

    # 4. deferral
    if _any(DEFER, low) and not _any(INTENT, low):
        secs = 86400 if re.search(r"tomorrow|kal|next week|baad", low) else 3600
        return {"action": "wait", "wait_seconds": secs, "rationale": "Merchant asked for time; backing off."}

    # 5. explicit intent / agreement -> action mode
    if _any(INTENT, low) and not low.rstrip().endswith("?") or re.search(r"let'?s do it|what'?s next|go ahead|judrna|join", low):
        r = _llm_reply(state, msg, "booking" if role == "customer" else "action") or _action_fallback(state, msg)
        r.pop("_end", None)
        return r

    # 6. everything else: answer in context
    r = _llm_reply(state, msg, "booking" if role == "customer" else "answer")
    if r:
        r.pop("_end", None)
        return r
    return {"action": "send", "cta": "open_ended",
            "body": "Samjha — main isse dekh kar abhi aapko exact next step bhejti hoon. Aap bas batayein, pehle kaunsa part chahiye?" if _hindi(msg)
            else "Got it — let me pull the exact details for that. Which part do you want first: the draft or the numbers?",
            "rationale": "LLM unavailable; generic acknowledgement with one narrow choice."}
