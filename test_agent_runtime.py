"""The three parts joined: A engine + player runtime slot (B) + GM (C). No LLM.

In-process runs use the real engine with a fake clock; the last test starts the
engine as its own HTTP process through run_scenario.
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from .agent_runtime import AgentWorker, ScriptedPlayerRuntime, load_factory, required_statements
from .run_scenario import engine_process, run
from .engine_v1 import EngineV1Client
from .test_competitive_gm import ENGINE_DIR, HAVE_ENGINE, Local


@unittest.skipUnless(HAVE_ENGINE, "A engine checkout not found")
class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        from integration import ExternalEngine
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = 1000.0
        self.engine = ExternalEngine(Path(self.dir.name) / "engine.sqlite3", lambda: self.now)
        self.operator = Local(self.engine, {"operator_token": self.engine.operator_token})

    def start(self, **rules):
        pool = self.operator.call("pool/bootstrap", pool_id="society")
        self.game_id = self.operator.call("games/create", pool_id="society", game_id="g-" + uuid.uuid4().hex[:8],
                                          seed=7, rules=rules)["game_id"]
        self.workers = {a["agent_id"]: AgentWorker(Local(self.engine).bind(
            **self.operator.call("pool/credentials", agent_id=a["agent_id"]))) for a in pool["agents"]}
        return pool["current_gm_id"]

    def loop(self, budget=2000, on_loop=None):
        for _ in range(budget):
            for worker in self.workers.values():
                worker.step()
            state = self.operator.call("games/observe", game_id=self.game_id)
            if state["ended"] and state["gm_report"] is not None:
                return state
            if on_loop:
                on_loop(state)
            self.now += 1
            self.engine.tick()
        self.fail("no official ending")

    def runtimes(self):
        return [p for w in self.workers.values() for p in w.runtimes]

    def test_full_game_with_scripted_runtime_reaches_official_ending(self):
        self.start()
        state = self.loop()
        self.assertIn(state["resolution"], ("winner", "all_dead", "deadline"))
        self.assertFalse(state["gm_report"]["technical_failure"])
        self.assertTrue(state["gm_report"]["success"])
        self.assertGreater(state["metrics"]["questions"], 0)
        runtimes = self.runtimes()
        self.assertEqual(len(runtimes), 12)
        self.assertTrue(all(isinstance(p, ScriptedPlayerRuntime) for p in runtimes))
        # Every answer went through the engine's truth/tier checks; count, don't hide, the minimum fallbacks.
        self.assertLessEqual(sum(p.fallbacks for p in runtimes), state["metrics"]["questions"])

    def test_promoted_player_switches_to_gm_and_finishes(self):
        first_gm = self.start(deadline_cycles=8)
        injected = []

        def fail_gm(state):
            if not injected and state["cycle_id"] == 2:
                self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_GM",
                                   term=state["control"]["term"])
                injected.append(True)

        state = self.loop(on_loop=fail_gm)
        successor = state["control"]["controller_id"]
        self.assertNotEqual(successor, first_gm)
        worker = self.workers[successor]
        self.assertIsNone(worker.player)          # player context dropped on promotion (B8)
        self.assertEqual(len(worker.runtimes), 1)  # it had played before the promotion
        self.assertTrue(worker.gm.gm.reported)
        self.assertEqual(state["resolution"], "deadline")
        self.assertTrue(state["gm_report"]["technical_failure"])

    def test_required_statements_meet_every_tier(self):
        options = {"definite": ["d1"], "ambiguous": [{"category": "suspect", "candidate": 3}],
                   "indirect": [{"clue_id": "i1", "narrative_options": ["자정에 라운지 칸에 누군가 있었어요."]}]}
        kinds = lambda n: [s["kind"] for s in required_statements({"response_options": options, "successes": n})]
        self.assertEqual([kinds(n) for n in range(4)],
                         [[], ["ambiguous"], ["definite"], ["definite", "ambiguous", "indirect"]])
        none = {"definite": [], "ambiguous": [], "indirect": []}
        self.assertEqual(required_statements({"response_options": none, "successes": 3}), [])
        self.assertIs(load_factory(None), ScriptedPlayerRuntime)
        self.assertIs(load_factory("gm_coordinator.agent_runtime:ScriptedPlayerRuntime"), ScriptedPlayerRuntime)

    def test_http_run_joins_engine_process_player_runtime_and_gm(self):
        with engine_process(ENGINE_DIR, Path(self.dir.name) / "http.sqlite3") as (url, token):
            result = run(EngineV1Client(url, timeout=30), token, seed=3, rules={"deadline_cycles": 3})
        self.assertIn(result["resolution"], ("winner", "all_dead", "deadline"))
        self.assertEqual(result["players"], 12)
        self.assertEqual(result["player_runtime"], ["ScriptedPlayerRuntime"])
        self.assertFalse(result["live_llm"])
        self.assertTrue(result["gm_report"]["success"])
        self.assertTrue(result["scenario"][-1].startswith(f"{result['cycles']}사이클 추리"))


if __name__ == "__main__":
    unittest.main()
