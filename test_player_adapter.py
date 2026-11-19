"""Verify C's privacy, routing and retry boundary independently of B repairs."""
import copy
import unittest
from pathlib import Path

from .contracts import ContractError, EnginePort
from .coordinator import Coordinator
from .demo import run
from .mock_engine import MockEngine
from .player_adapter import ReasoningPlayerAdapter
from .store import Store
from .team_adapters import IntegrationUnavailable, port_methods


class PlayerEndpoint:
    def __init__(self, agent_id="p1"):
        self.agent_id, self.decisions, self.results = agent_id, 0, []
        self.choice = ("stay", {}, [])

    def sync(self, notice, obs):
        self.notice, self.obs = copy.deepcopy(notice), copy.deepcopy(obs)

    def decide(self, notice, obs):
        self.decisions += 1
        return copy.deepcopy(self.choice)

    def ingest_result(self, result):
        self.results.append(copy.deepcopy(result))

    def respond_to_question(self, asker, topic):
        return [item["id"] for item in self.obs["information"]], False


class PlayerAdapterTests(unittest.TestCase):
    def make(self):
        store = Store()
        self.addCleanup(store.close)
        engine = MockEngine(store)
        gm = Coordinator.start(engine, store, "g", ["p1", "p2"], seed=42)
        player = PlayerEndpoint()
        adapter = ReasoningPlayerAdapter(player)
        return engine, gm, player, adapter

    def inputs(self, engine, gm):
        return gm.phase_notices()["p1"], engine.agent_snapshot("g", "p1")

    def test_private_projection_and_candidate_ids_are_preserved(self):
        engine, gm, player, adapter = self.make()
        notice, obs = self.inputs(engine, gm)
        obs["truth"] = obs["inventories"] = "PRIVATE"
        notice["observed_information"] = "PRIVATE"
        obs["self"]["hidden_answer"] = notice["self"]["hidden_answer"] = "PRIVATE"
        notice["positions"][0]["information_ids"] = "PRIVATE"
        notice["positions"][0]["personal_goal"] = "PRIVATE"
        obs["information"] = [{"id": "engine-place-clue", "category": "place", "excluded_id": 0, "text": "hint"}]
        original = copy.deepcopy(obs)
        adapter.choose_action(notice, obs)
        self.assertNotIn("PRIVATE", repr((player.notice, player.obs)))
        self.assertEqual(player.obs["candidates"]["places"][0]["name"], obs["candidates"]["place"][0]["name"])
        self.assertEqual(player.obs["information"][0]["id"], "engine-place-clue")
        self.assertEqual(player.obs["information"][0]["kind"], "location")
        self.assertEqual(player.obs["information"][0]["excludes"], 0)
        self.assertEqual(obs, original)

    def test_retry_reuses_request_and_completion_is_delivered_once(self):
        engine, gm, player, adapter = self.make()
        inputs = self.inputs(engine, gm)
        first = adapter.choose_action(*inputs)
        self.assertEqual(adapter.choose_action(*inputs), first)
        self.assertEqual(player.decisions, 1)
        result = engine.submit_action(first)
        result["hidden_answer"] = "PRIVATE"
        adapter.receive_result(result)
        adapter.receive_result(copy.deepcopy(result))
        self.assertEqual(len(player.results), 1)
        self.assertNotIn("PRIVATE", repr((player.results, adapter.receipts)))
        with self.assertRaises(ContractError):
            adapter.receive_result({**result, "outcome": "changed"})
        with self.assertRaises(ContractError):
            adapter.choose_action(*inputs)

    def test_misrouted_stale_and_pending_messages_are_rejected(self):
        engine, gm, player, adapter = self.make()
        notice, obs = self.inputs(engine, gm)
        with self.assertRaises(ContractError):
            adapter.choose_action(notice, {**obs, "phase_epoch": 2})
        with self.assertRaises(ContractError):
            adapter.choose_action({**notice, "agent_id": "p2"}, obs)
        self.assertEqual(player.decisions, 0)
        request = adapter.choose_action(notice, obs)
        result = engine.submit_action(request)
        for mutation in ({"agent_id": "p2"}, {"execution_status": "pending"}, {"consumed": False}):
            with self.assertRaises(ContractError):
                adapter.receive_result({**result, **mutation})
        self.assertEqual(player.results, [])

    def test_invalid_B_fallback_is_not_replaced_by_C(self):
        engine, gm, player, adapter = self.make()
        player.choice = ("move", {}, [])
        with self.assertRaises(ContractError):
            adapter.choose_action(*self.inputs(engine, gm))
        self.assertFalse(engine.gm_snapshot("g")["spent"])

    def test_question_alias_and_optional_disclosure_do_not_execute_sharing(self):
        engine, gm, player, adapter = self.make()
        first = adapter.choose_action(*self.inputs(engine, gm))
        engine.submit_action(first)
        other = PlayerEndpoint("p2")
        other_adapter = ReasoningPlayerAdapter(other)
        engine.submit_action(other_adapter.choose_action(gm.phase_notices()["p2"], engine.agent_snapshot("g", "p2")))
        gm.tick()
        notice, obs = self.inputs(engine, gm)
        player.choice = ("question", {"target": "p2", "approach": "compassion"}, ["proposal-only"])
        request = adapter.choose_action(notice, obs)
        self.assertEqual(request.payload, {"target_id": "p2", "approach": "compassion"})
        self.assertEqual(adapter.disclosure_proposals[request.action_id], ["proposal-only"])
        self.assertFalse(engine.gm_snapshot("g")["shared_information"])
        adapter.receive_result(engine.submit_action(request))
        before = copy.deepcopy(engine.gm_snapshot("g")["spent"])
        reply = adapter.answer_question({"game_id": "g", "target_id": "p1", "asker_id": "p2", "question_id": "q1"})
        self.assertEqual(reply["offer_ids"], [])
        self.assertEqual(engine.gm_snapshot("g")["spent"], before)
        self.assertIn("p1", before)

    def test_current_B_two_value_decide_and_target_id(self):
        engine, gm, player, adapter = self.make()
        player.choice = ("stay", {})
        first = adapter.choose_action(*self.inputs(engine, gm))
        self.assertEqual(adapter.disclosure_proposals[first.action_id], [])
        engine.submit_action(first)
        other = ReasoningPlayerAdapter(PlayerEndpoint("p2"))
        engine.submit_action(other.choose_action(gm.phase_notices()["p2"], engine.agent_snapshot("g", "p2")))
        gm.tick()
        player.choice = ("question", {"target_id": "p2", "approach": "compassion"})
        request = adapter.choose_action(*self.inputs(engine, gm))
        self.assertEqual(request.payload, {"target_id": "p2", "approach": "compassion"})
        for bad in (("question", {"target": "p2", "target_id": "p2"}), ("stay",), ["stay", {}]):
            fresh = ReasoningPlayerAdapter(PlayerEndpoint("p1"))
            fresh.agent.choice = bad
            with self.assertRaises(ContractError):
                fresh.choose_action(*self.inputs(engine, gm))

    def test_engine_v1_partial_clue_is_never_an_exclusion(self):
        engine, gm, player, adapter = self.make()
        notice, obs = self.inputs(engine, gm)
        obs["information"] = [{"id": "clue:abc", "category": "weapon", "redacted": True, "text": "가려진 자료"}]
        adapter.choose_action(notice, obs)
        item = player.obs["information"][0]
        self.assertTrue(item["redacted"])
        self.assertNotIn("excludes", item)
        self.assertNotIn("excluded_id", item)

    def test_event_gap_rolls_back_cursor_until_requery(self):
        engine, gm, _, _ = self.make()
        before = copy.deepcopy(gm.store.read("gm", "g"))
        notices = gm.phase_notices()
        for ident in ("p1", "p2"):
            engine.submit_action(ReasoningPlayerAdapter(PlayerEndpoint(ident)).choose_action(
                notices[ident], engine.agent_snapshot("g", ident)))
        # Store a baseline before introducing a missing final event.
        baseline = copy.deepcopy(gm.store.read("gm", "g"))
        original = engine.events_after
        for missing in ("interior", "tail"):
            engine.events_after = (lambda game, cursor: original(game, cursor)[1:]) if missing == "interior" else (lambda game, cursor: [])
            with self.assertRaises(ContractError):
                gm.sync()
            self.assertEqual(gm.store.read("gm", "g"), baseline)
        engine.events_after = original
        self.assertGreater(gm.sync()["event_cursor"], before["event_cursor"])

    def test_optional_sharing_is_not_required_or_enabled_by_default(self):
        self.assertNotIn("share_information", port_methods(EnginePort))
        result = run("normal", agents=1, max_cycles=1)
        self.assertNotIn("information_shared", result["event_counts"])


class ActualBComponentTests(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).resolve().parent.parent / "Agent_Reasoning/deploy/agent.py").exists(),
                         "Optional sibling B repository is not installed")
    def test_actual_B_methods_with_diagnostic_imports_and_explicit_belief_off(self):
        from .team_update_probe import B, TEMP, StubClient, diagnostic_agent
        from .team_adapters import load_legacy_module
        TEMP.mkdir(parents=True, exist_ok=True)
        schema = load_legacy_module(B / "deploy/schema.py")
        llm = load_legacy_module(B / "llm.py")
        module = diagnostic_agent(schema, llm)
        store = Store()
        self.addCleanup(store.close)
        engine = MockEngine(store)
        gm = Coordinator.start(engine, store, "diagnostic", ["p1"])
        client = StubClient({"action": {"type": "stay", "payload": {}}})
        native = module.LLMAgent("p1", client, belief_mode=False, log_path=TEMP / "adapter-test.jsonl")
        adapter = ReasoningPlayerAdapter(native)
        for action in ("stay", "explore", "pass"):
            client.response = {"action": {"type": action, "payload": {}}}
            request = adapter.choose_action(gm.phase_notices()["p1"], engine.agent_snapshot("diagnostic", "p1"))
            result = engine.submit_action(request)
            adapter.receive_result(result)
            self.assertEqual(gm.tick(), "advanced")
        self.assertEqual(len(adapter.receipts), 3)
        self.assertEqual(gm.sync()["cycle_id"], 2)
        # The known B defect is preserved, not repaired or silently disabled by C.
        broken = module.LLMAgent("p1", client, log_path=TEMP / "adapter-test.jsonl")
        obs = engine.agent_snapshot("diagnostic", "p1")
        obs["information"] = [{"id": "probe", "category": "suspect", "excluded_id": 0, "text": "hint"}]
        with self.assertRaisesRegex(IntegrationUnavailable, "BeliefState.*apply"):
            ReasoningPlayerAdapter(broken).choose_action(gm.phase_notices()["p1"], obs)


if __name__ == "__main__":
    unittest.main()
