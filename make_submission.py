"""Generate submission.jsonl: one composed message per canonical test pair (dataset/expanded/test_pairs.json).
Offline there is no 30s limit, so pairs that only got a template fallback (free tiers rate-limit) are retried."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from composer import compose

D = Path("dataset/expanded")
load = lambda p: json.load(open(p, encoding="utf8"))
pairs = load(D / "test_pairs.json")["pairs"]
cats = {p.stem: load(p) for p in (D / "categories").glob("*.json")}
merchants = {load(p)["merchant_id"]: load(p) for p in (D / "merchants").glob("*.json")}
customers = {load(p)["customer_id"]: load(p) for p in (D / "customers").glob("*.json")}
triggers = {load(p)["id"]: load(p) for p in (D / "triggers").glob("*.json")}


def run(pr):
    m = merchants[pr["merchant_id"]]
    cust = customers.get(pr["customer_id"]) if pr["customer_id"] else None
    r = compose(cats[m["category_slug"]], m, triggers[pr["trigger_id"]], cust)
    return r, {"test_id": pr["test_id"], "body": r["body"], "cta": r["cta"], "send_as": r["send_as"],
               "suppression_key": r["suppression_key"], "rationale": r["rationale"]}


ROUNDS = 6
rows = {}
for rnd in range(ROUNDS):
    todo = [p for p in pairs if p["test_id"] not in rows]
    if not todo:
        break
    with ThreadPoolExecutor(2) as ex:
        for pr, (res, row) in zip(todo, ex.map(run, todo)):
            if not res.get("_fallback") or rnd == ROUNDS - 1:
                rows[pr["test_id"]] = (row, bool(res.get("_fallback")))
    left = len([p for p in pairs if p["test_id"] not in rows])
    print(f"round {rnd}: {len(rows)}/30 done, {left} pending", flush=True)
    if left:
        time.sleep(30)

with open("submission.jsonl", "w", encoding="utf8") as f:
    for p in pairs:
        f.write(json.dumps(rows[p["test_id"]][0], ensure_ascii=False) + "\n")
print(f"wrote {len(pairs)} rows; template-fallback rows: {sum(1 for v in rows.values() if v[1])}")
