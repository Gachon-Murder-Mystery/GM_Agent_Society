"""C's v1 GM against A's real engine process (skipped without the sibling checkout)."""
import unittest
from pathlib import Path

from .contracts import ContractError
from .engine_v1 import EngineV1Client, GMWorker, V1Coordinator
from .store import Store
from .v1_demo import ScriptedPlayer, engine_process

ROOT = Path(__file__).resolve().parent.parent


class GainPolicyTests(unittest.TestCase):
    def test_gain_counts_only_missing_ids_and_respects_repeated_refusal(self):
        observation = {"actors": [{"id": "a", "held_information_ids": ["x", "y"]},
                                  {"id": "b", "held_information_ids": ["y", "z", "w"]}]}
        gains = V1Coordinator._gains(observation, ["a", "b"], {})
        self.assertEqual(gains, {("a", "b"): 2, ("b", "a"): 1})
        gains = V1Coordinator._gains(observation, ["a", "b"], {"a>b": 2, "b>a": 1})
        self.assertEqual(gains, {("a", "b"): 0, ("b", "a"): 1})
        observation["shared_information"] = [{"id": "z"}]
        self.assertEqual(V1Coordinator._gains(observation, ["a", "b"], {}), {("a", "b"): 1, ("b", "a"): 1})


@unittest.skipUnless((ROOT / "Game-engine/integration.py").exists(), "A engine with /integration/v1 not installed")
class EngineV1GMTests(unittest.TestCase):
    def setUp(self):
        # A fresh engine per test: the pool is shared engine-wide and the GM may activate any member.
        context = engine_process(ROOT / "Game-engine")
        self.client, token = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.operator = self.client.bind(operator_token=token)
        prefix = "g"
        self.ids = [f"{prefix}-a{i}" for i in range(5)]
        tokens = {aid: self.operator.call("pool/register", agent_id=aid)["pool_token"] for aid in self.ids}
        # Cooperative v1 contract; the engine's default became competitive v1.3 (competitive_gm.py).
        self.game_id = self.operator.call("games/create", game_id=f"{prefix}-case", seed=11, mode="legacy",
                                          candidate_ids=self.ids)["game_id"]
        self.store = Store()
        self.addCleanup(self.store.close)
        self.identities = {aid: self.client.bind(agent_id=aid, pool_token=tokens[aid]) for aid in self.ids}
        self.workers = {aid: GMWorker(self.identities[aid], self.store) for aid in self.ids}
        self.bots = {aid: ScriptedPlayer(self.identities[aid]) for aid in self.ids}

    def observe(self):
        return self.operator.call("games/observe", game_id=self.game_id)

    def rounds(self, count):
        for _ in range(count):
            state = self.observe()
            if state["ended"]:
                return state
            for aid, worker in self.workers.items():
                if aid == (state["control"] or {}).get("controller_id") or worker.coordinator:
                    worker.step()
            for aid in self.observe()["active_agents"]:
                self.bots[aid].step()
        return self.observe()

    def gm_id(self):
        return self.observe()["control"]["controller_id"]

    def test_setup_activates_distinct_roles_and_records_alternatives(self):
        state = self.rounds(1)
        self.assertEqual(len(state["active_agents"]), 2)
        self.assertNotIn(self.gm_id(), state["active_agents"])
        actors = {a["id"]: a for a in state["actors"]}
        self.assertEqual(len({actors[a]["job"]["id"] for a in state["active_agents"]}), 2)
        self.assertEqual(len({actors[a]["personality"] for a in state["active_agents"]}), 2)
        plan = state["plans"][0]
        self.assertGreaterEqual(len(plan["alternatives"]), 2)
        self.assertIn(plan["selected_alternative"], [x["id"] for x in plan["alternatives"]])
        self.assertTrue(plan["reason"])
        self.assertEqual({t["owner"] for t in state["tasks"]}, set(state["active_agents"]))
        self.assertEqual(len({t["room"] for t in state["tasks"]}), 2)  # parallel split

    def test_gm_plans_each_cycle_and_advances_only_when_everyone_spent(self):
        state = self.rounds(12)
        self.assertGreaterEqual(state["cycle_id"], 3)
        plan_cycles = [p["map_version"] for p in state["plans"]]
        self.assertGreaterEqual(len(plan_cycles), state["cycle_id"] - 1)
        self.assertEqual(state["metrics"]["pending_questions"], 0)
        coordinator = self.workers[self.gm_id()].coordinator
        decisions = coordinator.report()["decisions"]
        self.assertEqual(decisions[0]["trigger"], "setup")
        self.assertTrue(all(d["scores"] for d in decisions))

    def test_player_fault_tasks_are_reassigned_by_gm_not_engine(self):
        for _ in range(20):
            state = self.rounds(1)
            owners = sorted(t["owner"] for t in state["tasks"]
                            if t["status"] == "assigned" and t["owner"] in state["active_agents"])
            if owners:
                break
        victim = owners[0]  # A player with an open task, so ownership must be released.
        self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_PLAYER", agent_id=victim)
        orphaned = [t for t in self.observe()["tasks"] if t["owner"] is None and t["status"] == "pending"]
        self.assertTrue(orphaned)  # The engine only releases ownership.
        state = self.rounds(6)
        survivors = set(state["active_agents"])
        self.assertNotIn(victim, survivors)
        tasks = {t["task_id"]: t for t in state["tasks"]}
        for task in orphaned:
            self.assertIn(tasks[task["task_id"]]["owner"], survivors)
            self.assertGreater(tasks[task["task_id"]]["generation"], task["generation"])
        decisions = self.workers[self.gm_id()].coordinator.report()["decisions"]
        triggers = {d["trigger"] for d in decisions}
        self.assertTrue(triggers & {"reassign_fault", "replace_fault"})
        self.assertEqual(len(survivors), 2)  # A reserve replaced the failed player at a fresh movement.

    def test_gm_succession_fences_old_term_and_starts_clean_context(self):
        self.rounds(3)
        old_id = self.gm_id()
        old = self.workers[old_id].coordinator
        term = self.observe()["control"]["term"]
        self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_GM", term=term)
        new_id = self.gm_id()
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(self.workers[old_id].step(), "fenced")
        self.assertIsNone(self.workers[old_id].coordinator)
        state = self.rounds(6)
        successor = self.workers[new_id].coordinator
        self.assertEqual(successor.term, term + 1)
        self.assertNotEqual(successor.context_key, old.context_key)
        self.assertNotIn(new_id, state["active_agents"])  # Former character is now an NPC.
        self.assertEqual(self.bots[new_id]._role(), None)
        report = successor.report()
        self.assertTrue(report["decisions"])
        self.assertTrue(all(d["term"] == term + 1 for d in report["decisions"]))
        self.assertTrue(any(s["type"] == "gm_succeeded" for s in report["successions"]))
        self.assertNotIn("personal_goal", repr(report))
        self.assertEqual(len(state["active_agents"]), 2)

    def test_no_successor_waits_for_operator_then_new_gm_recruits(self):
        self.rounds(2)
        state = self.observe()
        for aid in state["active_agents"]:
            self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_PLAYER", agent_id=aid)
        old_id = self.gm_id()
        self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_GM",
                           term=state["control"]["term"])
        self.assertEqual(self.observe()["status"], "waiting_for_human")
        self.assertEqual(self.workers[old_id].step(), "fenced")
        pool = {a["agent_id"]: a for a in self.operator.call("pool/list")["agents"]}
        chosen = next(aid for aid in self.ids if pool[aid]["pool_status"] == "available")
        self.operator.call("games/operator", game_id=self.game_id, operation="APPOINT_GM", agent_id=chosen)
        self.assertEqual(self.observe()["status"], "awaiting_setup")
        state = self.rounds(2)
        self.assertEqual(state["status"], "running")
        self.assertGreaterEqual(len(state["active_agents"]), 1)

    def test_role_credentials_are_required(self):
        with self.assertRaises(ContractError):
            V1Coordinator(EngineV1Client(self.client.base_url), self.store, "x")
        with self.assertRaises(ContractError):
            GMWorker(self.client.bind(agent_id="x"), self.store)
        self.assertEqual(self.workers[next(a for a in self.ids if a != self.gm_id())].step(), "not_gm")


if __name__ == "__main__":
    unittest.main()
