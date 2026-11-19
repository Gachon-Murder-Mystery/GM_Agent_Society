"""Tests exercise actual team repository code, never substitute MockEngine."""
import sys
import unittest
from pathlib import Path

from .coordinator import Coordinator
from .integration_probe import native_component_probe, run
from .store import Store
from .team_adapters import IntegrationUnavailable, NativeEngineBridge, NativeReasoningBridge


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless((ROOT / "Game-engine/engine.py").exists() and (ROOT / "Agent_Reasoning/engine.py").exists(),
                     "Legacy B layout (engine.py/deploy) absent; B was restructured after f00bc05")
class TeamAdapterTests(unittest.TestCase):
    def make(self):
        engine = NativeEngineBridge(ROOT / "Game-engine")
        reasoning = NativeReasoningBridge(ROOT / "Agent_Reasoning")
        store = Store()
        self.addCleanup(store.close)
        return engine, reasoning, store

    def test_loader_keeps_A_and_B_engines_separate_without_global_alias_changes(self):
        original = sys.modules.get("engine")
        engine, reasoning, _ = self.make()
        self.assertEqual(Path(engine.module.__file__).parent, ROOT / "Game-engine")
        self.assertEqual(Path(reasoning.engine_module.__file__).parent, ROOT / "Agent_Reasoning")
        if reasoning.capabilities()["legacy_interpreter_available"]:
            self.assertIs(reasoning.module.local_intent, reasoning.engine_module.local_intent)
            self.assertIsNot(reasoning.module.local_intent, engine.module.local_intent)
        self.assertIs(sys.modules.get("engine"), original)

    def test_full_GM_attachment_is_blocked_before_creating_or_mocking_a_game(self):
        engine, _, store = self.make()
        with self.assertRaises(IntegrationUnavailable) as caught:
            Coordinator.start(engine, store, "g", ["a", "b", "c"])
        self.assertIn("create_game", caught.exception.missing_methods)
        self.assertIsNone(engine._legacy_game)
        self.assertIsNone(store.read("gm", "g"))

    def test_B_receives_only_observed_fields_even_if_extra_private_data_is_supplied(self):
        engine, reasoning, _ = self.make()
        observation = engine.legacy_start()
        observation["truth"] = "PRIVATE_SENTINEL"
        observation["all_clues"] = "PRIVATE_SENTINEL"
        if not reasoning.capabilities()["legacy_interpreter_available"]:
            with self.assertRaises(IntegrationUnavailable):
                reasoning.interpret("식당 칸으로 이동한다", observation)
            self.assertIsNone(reasoning.last_input)
            return
        result = reasoning.interpret("식당 칸으로 이동한다", observation)
        self.assertEqual(result["intent"], {"kind": "move", "place": 2})
        self.assertEqual(set(reasoning.last_input), {"name", "role", "pending", "target"})
        self.assertNotIn("PRIVATE_SENTINEL", repr(reasoning.last_input))

    def test_native_B_flow_or_explicit_API_change_block(self):
        engine, reasoning, store = self.make()
        report = native_component_probe(engine, reasoning, store)
        if not reasoning.capabilities()["legacy_interpreter_available"]:
            self.assertEqual(report["status"], "blocked_by_B_API_change")
            self.assertEqual(report["trace"], [])
            self.assertFalse(report["new_information_received_by_observer"])
            return
        trace = report["trace"]
        self.assertEqual([step["b_interpretation"]["intent"]["kind"] for step in trace], ["move", "prepare", "check", "roll"])
        self.assertEqual(len(trace[-1]["a_native_result"]["acquired_clues"]), 2)
        last = trace[-1]["c_observer_record"]
        self.assertEqual(store.read("legacy_integration_observer", last["game_id"]), last)
        self.assertTrue(report["new_information_received_by_observer"])
        self.assertFalse(last["engine_completion_event"])
        self.assertIsNone(last["official_phase_token"])
        for step in trace:
            self.assertNotIn("truth", step["a_native_result"]["observation"])
            self.assertNotIn("all_clues", step["a_native_result"]["observation"])

    def test_B_text_interpretation_is_not_promoted_to_autonomous_PlayerPort(self):
        _, reasoning, _ = self.make()
        with self.assertRaises(IntegrationUnavailable):
            reasoning.choose_action({}, {})
        self.assertFalse(reasoning.capabilities()["autonomous_player_loop_verified"])

    def test_actual_native_HTTP_contract_gaps_and_server_cleanup(self):
        report = run(include_http=True)
        http = report["native_http_probe"]
        self.assertEqual(report["full_gm_attachment"]["status"], "blocked")
        self.assertTrue(http["observation_returned"])
        self.assertTrue(http["caller_phase_field_did_not_block_movement"])
        self.assertTrue(http["same_action_id_repeated_side_effect"])
        self.assertFalse(http["native_http_has_structured_action_result"])
        self.assertTrue(http["server_stopped"])
        self.assertTrue(http["port_closed"])
        self.assertFalse(http["live_llm"])


if __name__ == "__main__":
    unittest.main()
