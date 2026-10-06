"""Offline smoke test via FastAPI TestClient (no network needed; uses LLM if OPENROUTER_API_KEY set)."""
import glob, json, sys, time
from fastapi.testclient import TestClient
import bot

c = TestClient(bot.app)
D = "dataset/expanded"
for f in glob.glob(f"{D}/categories/*.json"):
    p = json.load(open(f, encoding="utf8")); assert c.post("/v1/context", json={"scope": "category", "context_id": p["slug"], "version": 1, "payload": p}).status_code == 200
for kind, key, pat in [("merchant", "merchant_id", "merchants"), ("customer", "customer_id", "customers"), ("trigger", "id", "triggers")]:
    for f in glob.glob(f"{D}/{pat}/*.json"):
        p = json.load(open(f, encoding="utf8")); assert c.post("/v1/context", json={"scope": kind, "context_id": p[key], "version": 1, "payload": p}).status_code == 200
print(c.get("/v1/healthz").json())
print("dup ->", c.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": {}}).status_code)
tp = json.load(open(f"{D}/test_pairs.json"))["pairs"]
ids = [p["trigger_id"] for p in tp[:int(sys.argv[1]) if len(sys.argv) > 1 else 5]]
t = time.time(); r = c.post("/v1/tick", json={"now": "2026-04-26T10:30:00Z", "available_triggers": ids}).json()
print(f"tick {time.time()-t:.1f}s, {len(r['actions'])} actions")
for a in r["actions"]:
    print("\n", a["trigger_id"], a["send_as"], a["cta"], "\n", a["body"], "\n  >>", a["rationale"])
conv = r["actions"][0]["conversation_id"]; mid = r["actions"][0]["merchant_id"]
def rep(m, cid=conv): 
    x = c.post("/v1/reply", json={"conversation_id": cid, "merchant_id": mid, "from_role": "merchant", "message": m, "received_at": "x", "turn_number": 2}).json(); print("\nMERCHANT:", m, "\nBOT:", x); return x
for i in range(4): rep("Thank you for contacting us! Our team will respond shortly.", f"auto{i}")
rep("Ok lets do it. Whats next?", "intent1")
rep("Stop messaging me. This is useless spam.", "host1")
rep("can you also help me file my GST?", conv)
rep("kal baat karte hain, abhi busy hoon", "defer1")
