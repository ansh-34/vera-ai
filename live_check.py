"""Push real data to the deployed bot, tick, and print what it composes (proves LLM keys work on Render)."""
import glob, json, os, sys, time, httpx
U = os.environ.get("BOT_URL", "https://vera-ai-8f4x.onrender.com"); D = "dataset/expanded"
c = httpx.Client(timeout=40)
n = 0
for scope, key, pat in [("category", "slug", "categories"), ("merchant", "merchant_id", "merchants"), ("customer", "customer_id", "customers"), ("trigger", "id", "triggers")]:
    for f in glob.glob(f"{D}/{pat}/*.json"):
        p = json.load(open(f, encoding="utf8"))
        r = c.post(U + "/v1/context", json={"scope": scope, "context_id": p[key], "version": 1, "payload": p}); n += r.status_code == 200
print("pushed ok:", n, "(expect 355)"); print(c.get(U + "/v1/healthz").json())
pairs = json.load(open(f"{D}/test_pairs.json"))["pairs"][:4]
t = time.time(); r = c.post(U + "/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": [p["trigger_id"] for p in pairs]}).json()
print(f"tick {time.time()-t:.1f}s -> {len(r['actions'])} actions")
for a in r["actions"]: print("-", a["trigger_id"], a["cta"], "|", a["rationale"][:60], "\n ", a["body"][:240])
