# Vera merchant assistant — submission

**Run:** `pip install -r requirements.txt && uvicorn bot:app --host 0.0.0.0 --port 8080`
**Regenerate submission:** `python make_submission.py` (needs `dataset/expanded/`, built with `python dataset/generate_dataset.py --out dataset/expanded` run from `dataset/`).
**LLM keys (all free tiers work):** put any of `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` in `.env`; they are tried in that order, temperature 0.

## Approach
1. **Facts are resolved in code, not by the LLM.** The digest item a trigger points at (`top_item_id` / `digest_item_id` / `alert_id`), CTR vs peer, lapsed share, active offers, salutation (`Dr. <name>` for dentists) and language rule are computed first and handed to the model as one JSON context. Peer stats are labelled `peer_benchmarks_NOT_this_merchant` so they are not attributed to the merchant.
2. **Trigger-kind routing.** ~25 kinds each have a short guide (which fact to anchor on, which lever, which CTA shape). Unknown kinds use a generic guide.
3. **Honest with thin data.** About half the canonical triggers carry `placeholder` payloads. The prompt forbids inventing event details (competitor names, fixtures, stats) and tells the model to derive the hook from merchant/customer/category data instead.
4. **Validate, then retry once.** Rejects taboo words from the category voice, non-Roman script, `**` markdown, long preambles, multiple CTAs, invented URLs, and numbers that do not appear anywhere in the context. An optional second fact-check pass exists (`FACT_CHECK=1`).
5. **Deterministic fallback.** If no LLM answers (rate limit / timeout) a fact-only template per trigger kind is returned, so a tick never times out or sends an empty body. Results from the LLM are cached by input hash (determinism + cost).
6. **Restraint.** `/v1/tick` sends at most one message per merchant per tick, ranks by urgency, skips already-used `suppression_key`s, stops after 2 unanswered nudges, and never messages a customer without a customer context.
7. **Replies (`conversation_handlers.respond`).** Rule-based first, LLM second:
   auto-reply detection (canned phrases + verbatim repeats, counted per merchant so a new `conversation_id` does not reset it) → one nudge, then 24h wait, then end;
   opt-out / abuse → `end`; off-topic (GST, loans, ...) → decline + steer back; "later/kal" → `wait`;
   explicit intent ("let's do it", "mujhe judrna hai") → action mode with no qualifying questions; everything else → contextual LLM reply. A reply is never sent verbatim twice.
8. **Ops.** Context store is versioned and idempotent (409 on stale), the LLM is never called while holding the global lock (so `/v1/healthz` stays instant), tick composes in parallel under a 24s budget and ships whatever finished, `/v1/teardown` wipes state.

## Tradeoffs
- Free-tier models are rate-limited and weaker than frontier models; the fallback templates guarantee valid output but are less persuasive. A paid or higher-limit key is the single biggest quality lever.
- Trigger expiry is deliberately not enforced: the judge's simulated clock is unknown and wrongly dropping a trigger costs more than sending a slightly late one.
- The number validator is permissive for small integers and dates, so it catches invented stats, not every possible fabrication.

## What additional context would have helped most
Real payloads for the ~50% placeholder triggers; open appointment slots per merchant (only some triggers carry them); a per-merchant list of recent customer questions; and the judge's simulated clock on `/v1/tick` for exact "days until" wording.

## Status / how it was verified
- `submission.jsonl`: 30/30 messages LLM-composed (Gemini free tier), zero template fallbacks; scanned for invented dates, snake_case leaks, non-Roman script, ISO dates.
- `judge_simulator.py` scenarios (warmup, auto-reply, intent transition, hostile) all pass against the live server; a 3-trigger `/v1/tick` returns in ~5s.
- Local rubric scoring (judge = same free LLM chain, so treat as indicative): ~37/50 on rows that scored; the free tier rate-limited the rest of the pass.

## Deploy
`Procfile` is included (`uvicorn bot:app --host 0.0.0.0 --port $PORT`). On Render/Railway/Fly: set env var `GEMINI_API_KEY` (and optionally `GROQ_API_KEY`, `OPENROUTER_API_KEY`), build `pip install -r requirements.txt`, keep the instance always-on (no sleeping) for the whole test window, then submit `https://<host>` as the bot URL. For a quick demo: `ngrok http 8080`.
