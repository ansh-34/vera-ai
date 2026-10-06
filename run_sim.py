import os, sys, llm
import judge_simulator as js
js.BOT_URL = "http://127.0.0.1:8080"
class P(js.LLMProvider):
    def name(self): return "gemini-judge"
    def complete(self, prompt, system=None): return llm.complete(system or "", prompt, max_tokens=900, budget_s=40)
sim = js.JudgeSimulator(P())
sim.client = js.BotClient(js.BOT_URL)
sys.exit(0 if sim.run(sys.argv[1] if len(sys.argv) > 1 else "all") else 1)
