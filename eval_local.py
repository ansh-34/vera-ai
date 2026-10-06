"""Score submission.jsonl with the judge_simulator rubric (judge model != composer model)."""
import json, os, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import llm  # loads .env
import judge_simulator as js

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gemini-3.5-flash-lite")
D = Path("dataset/expanded"); load = lambda p: json.load(open(p, encoding="utf8"))
pairs = {p["test_id"]: p for p in load(D / "test_pairs.json")["pairs"]}
cats = {p.stem: load(p) for p in (D / "categories").glob("*.json")}
merch = {load(p)["merchant_id"]: load(p) for p in (D / "merchants").glob("*.json")}
cust = {load(p)["customer_id"]: load(p) for p in (D / "customers").glob("*.json")}
trig = {load(p)["id"]: load(p) for p in (D / "triggers").glob("*.json")}
class _P(js.LLMProvider):
    def name(self): return "llm-chain"
    def complete(self, prompt, system=None):
        return llm.complete(system or "", prompt, max_tokens=900, budget_s=40)
scorer = js.LLMScorer(_P(), None)
rows = [json.loads(l) for l in open("submission.jsonl", encoding="utf8")]

def score(r):
    p = pairs[r["test_id"]]; m = merch[p["merchant_id"]]
    s = scorer.score(r, cats[m["category_slug"]], m, trig[p["trigger_id"]], cust.get(p["customer_id"]) if p["customer_id"] else None)
    return r["test_id"], s
import contextlib, io
with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(6) as ex: res = list(ex.map(score, rows))
tot = 0
for tid, s in res:
    tot += s.total
    print(f"{tid} {s.total:>2}/50 spec{s.specificity} cat{s.category_fit} mer{s.merchant_fit} trg{s.decision_quality} eng{s.engagement_compulsion} | {s.hint[:90]}")
print(f"\nAVG {tot/len(res):.1f}/50")
