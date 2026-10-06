"""Vera merchant-assistant bot. Run: uvicorn bot:app --host 0.0.0.0 --port 8080

Also exports compose() per the challenge brief (section 7.1).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import conversation_handlers
from composer import compose  # noqa: F401  (re-exported for submission)

app = FastAPI()
START = time.time()
LOCK = threading.RLock()

contexts: dict[tuple[str, str], dict] = {}   # (scope, id) -> {version, payload}
conversations: dict[str, dict] = {}          # conversation_id -> state
sent_suppression: set[str] = set()
opted_out: set[str] = set()                  # merchant_ids that said stop
unanswered: dict[str, int] = {}              # merchant_id -> consecutive unanswered outbound
POOL = ThreadPoolExecutor(max_workers=16)
TICK_BUDGET_S = 24.0
MAX_ACTIONS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(scope: str, cid: str):
    with LOCK:
        c = contexts.get((scope, cid))
    return c["payload"] if c else None


@app.api_route("/v1/healthz", methods=["GET", "HEAD"])  # HEAD: uptime monitors probe with it
def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    with LOCK:
        for (scope, _cid) in contexts:
            counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    import llm
    return {"team_name": "Vera+", "team_members": ["Ansh"], "model": ",".join(llm.MODELS[:3]),
            "approach": "fact-resolved, trigger-routed LLM composer with deterministic validator + fallback; "
                        "rule-based reply router (auto-reply / intent / opt-out / off-topic) with LLM for the rest",
            "contact_email": "anshg5384@gmail.com", "version": "1.0.0",
            "submitted_at": _now_iso()}


class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None


@app.post("/v1/context")
def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": body.scope}, status_code=400)
    key = (body.scope, body.context_id)
    with LOCK:
        cur = contexts.get(key)
        if cur and cur["version"] >= body.version:
            return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": cur["version"]},
                                status_code=409)
        contexts[key] = {"version": body.version, "payload": body.payload}
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _build_action(trg: dict, now: str):
    mid = trg.get("merchant_id")
    merchant = _get("merchant", mid) if mid else None
    if not merchant:
        return None
    category = _get("category", merchant.get("category_slug"))
    if not category:
        return None
    cid = trg.get("customer_id")
    customer = _get("customer", cid) if cid else None
    if trg.get("scope") == "customer" and not customer:
        return None  # cannot message a customer we have no context/consent for
    res = compose(category, merchant, trg, customer, now=now)
    kind = trg.get("kind", "generic")
    res.pop("_fallback", None)
    body = res["body"]
    sents = [s for s in body.replace("\n", " ").split(". ") if s]
    name = merchant.get("identity", {}).get("owner_first_name") or merchant.get("identity", {}).get("name", "")
    conv_id = f"conv_{mid}_{trg['id']}" if not cid else f"conv_{cid}_{trg['id']}"
    return {
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": cid,
        "send_as": res["send_as"], "trigger_id": trg["id"],
        "template_name": f"vera_{kind}_v1" if res["send_as"] == "vera" else f"merchant_{kind}_v1",
        "template_params": [name, sents[0][:200] if sents else body[:200], sents[-1][:200] if sents else ""],
        "body": body, "cta": res["cta"], "suppression_key": res["suppression_key"],
        "rationale": res["rationale"],
        "_ctx": (category, merchant, trg, customer),
    }


@app.post("/v1/tick")
def tick(body: TickBody):
    t0 = time.time()
    cands = []
    with LOCK:
        for tid in body.available_triggers:
            trg = _get("trigger", tid)
            if not trg:
                continue
            sk = trg.get("suppression_key")
            mid = trg.get("merchant_id")
            if sk and sk in sent_suppression:
                continue
            if mid in opted_out or unanswered.get(mid, 0) >= 2:
                continue
            cands.append(trg)
    cands.sort(key=lambda t: -(t.get("urgency") or 0))
    seen_m, picked = set(), []
    for t in cands:  # one action per merchant per tick
        if t.get("merchant_id") in seen_m:
            continue
        seen_m.add(t.get("merchant_id"))
        picked.append(t)
    picked = picked[:MAX_ACTIONS]

    futs = {POOL.submit(_build_action, t, body.now): t for t in picked}
    actions = []
    try:
        for f in as_completed(futs, timeout=max(1.0, TICK_BUDGET_S - (time.time() - t0))):
            try:
                a = f.result()
            except Exception:  # noqa: BLE001
                a = None
            if a:
                actions.append(a)
    except Exception:  # noqa: BLE001  (timeout: ship whatever finished)
        pass
    out = []
    with LOCK:
        for a in sorted(actions, key=lambda a: -(_get("trigger", a["trigger_id"]) or {}).get("urgency", 0)):
            if a["suppression_key"] in sent_suppression:
                continue
            category, merchant, trg, customer = a.pop("_ctx")
            sent_suppression.add(a["suppression_key"])
            unanswered[a["merchant_id"]] = unanswered.get(a["merchant_id"], 0) + 1
            conversations[a["conversation_id"]] = {
                "conversation_id": a["conversation_id"], "merchant": merchant, "category": category,
                "customer": customer, "trigger": trg, "send_as": a["send_as"],
                "turns": [{"from": "vera", "body": a["body"]}], "auto_reply_count": 0, "ended": False,
            }
            out.append(a)
    return {"actions": out}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str = ""
    received_at: str | None = None
    turn_number: int | None = None


def _state_for(b: ReplyBody) -> dict:
    with LOCK:
        st = conversations.get(b.conversation_id)
        if st is None:  # unseen conversation (e.g. replay scenarios): rebuild from contexts
            merchant = _get("merchant", b.merchant_id) if b.merchant_id else None
            category = _get("category", (merchant or {}).get("category_slug", "")) if merchant else None
            customer = _get("customer", b.customer_id) if b.customer_id else None
            st = {"conversation_id": b.conversation_id, "merchant": merchant or {}, "category": category or {},
                  "customer": customer, "trigger": None, "turns": [], "auto_reply_count": 0, "ended": False}
            conversations[b.conversation_id] = st
        return st


@app.post("/v1/reply")
def reply(b: ReplyBody):
    st = _state_for(b)
    if st.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed."}
    st["from_role"] = b.from_role
    mid = b.merchant_id or (st.get("merchant") or {}).get("merchant_id")
    with LOCK:
        if mid:
            unanswered[mid] = 0
    result = conversation_handlers.respond(st, b.message)  # may call the LLM: do not hold the global lock
    with LOCK:
        st["turns"].append({"from": b.from_role, "body": b.message})
        if result.get("action") == "send":  # never repeat ourselves verbatim
            prev = {t["body"].strip() for t in st["turns"] if t["from"] == "vera"}
            if result.get("body", "").strip() in prev:
                result = {"action": "wait", "wait_seconds": 3600,
                          "rationale": "Avoiding verbatim repeat; waiting for the merchant."}
        if result.get("action") == "send":
            st["turns"].append({"from": "vera", "body": result["body"]})
        elif result.get("action") == "end":
            st["ended"] = True
            if mid and "opted out" in result.get("rationale", ""):
                opted_out.add(mid)
    result.pop("_end", None)
    return result


@app.post("/v1/teardown")
def teardown():
    with LOCK:
        contexts.clear()
        conversations.clear()
        sent_suppression.clear()
        opted_out.clear()
        unanswered.clear()
    from composer import _cache
    _cache.clear()
    return {"ok": True}


@app.exception_handler(Exception)
async def _err(_req: Request, exc: Exception):
    return JSONResponse({"error": str(exc)[:200]}, status_code=500)
