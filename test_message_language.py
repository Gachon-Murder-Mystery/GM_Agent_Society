"""Catch plausible but incorrect interpretations at the A/B/C boundary."""
import json
import unittest
from pathlib import Path

from .contracts import ActionRequest, ContractError
from .language_probe import run
from .message_language import (denormalize_acquired_clue, intent_to_request, normalize_acquired_clue,
                              npc_actor_id, npc_candidate_id, request_to_intent)


ROOT = Path(__file__).resolve().parent.parent


class MessageMeaningTests(unittest.TestCase):
    def context(self, phase="movement"):
        return {"game_id": "g", "agent_id": "player_1", "action_id": "a", "cycle_id": 1,
                "phase": phase, "phase_epoch": 1}

    def test_place_is_destination_only_for_move_and_crime_candidate_for_accuse(self):
        move = intent_to_request({"kind": "move", "place": 2}, self.context())
        accuse = intent_to_request({"kind": "accuse", "suspect": 1, "weapon": 0, "place": 2}, self.context("deduction"))
        self.assertEqual(move.payload, {"room": 2})
        self.assertEqual(accuse.payload, {"suspect": 1, "weapon": 0, "place": 2})
        self.assertEqual(request_to_intent(move), {"kind": "move", "place": 2})
        self.assertEqual(request_to_intent(accuse)["place"], 2)

    def test_phase_actor_and_correlation_are_required_not_inferred_from_legacy_turn(self):
        with self.assertRaises(ContractError):
            intent_to_request({"kind": "move", "place": 2}, {"id": "g", "turn": 3})
        with self.assertRaises(ContractError):
            intent_to_request({"kind": "move", "place": 2}, self.context("interaction"))

    def test_pending_compound_flavor_and_risk_commands_are_not_completed_questions(self):
        for intent in ({"kind": "check", "target": 10}, {"kind": "check", "purpose": "risk"},
                       {"kind": "roll"}, {"kind": "find", "target": 10}, {"kind": "talk", "target": 10},
                       {"kind": "look"}, {"kind": "cancel"}):
            with self.subTest(intent=intent), self.assertRaises(ContractError):
                intent_to_request(intent, self.context("interaction"))

    def test_player_corpse_and_skip_do_not_have_legacy_equivalent_commands(self):
        for action, payload, phase in (("question", {"target_id": "player_2"}, "interaction"),
                                       ("examine_corpse", {"target_id": "player_2"}, "interaction"),
                                       ("pass", {}, "deduction"), ("stay", {}, "movement")):
            with self.subTest(action=action), self.assertRaises(ContractError):
                request_to_intent(ActionRequest(**self.context(phase), action=action, payload=payload))

    def test_actor_candidate_and_information_id_namespaces_do_not_collapse(self):
        for candidate in range(12):
            self.assertEqual(npc_candidate_id(npc_actor_id(candidate)), candidate)
        for ident in ("player_1", "corpse:1", "suspect:1", "npc:01", "npc:-1", "npc:12"):
            with self.subTest(ident=ident), self.assertRaises(ContractError):
                npc_candidate_id(ident)

    def test_exclusion_identity_source_and_cost_survive_all_clue_categories(self):
        for category in ("suspect", "weapon", "place"):
            clue = {"id": f"{category}:0", "category": category, "index": 0,
                    "text": "이 후보는 제외된다", "source": "식당 칸", "turn": 5}
            projected = normalize_acquired_clue(clue)
            self.assertEqual(projected["information"]["excluded_id"], 0)
            self.assertNotIn("cycle_id", projected["legacy_metadata"])
            self.assertEqual(denormalize_acquired_clue(projected), clue)

    def test_unknown_fields_bad_ids_and_hidden_directory_are_not_silently_translated(self):
        for intent in ({"kind": "move", "place": True}, {"kind": "move", "place": 8},
                       {"kind": "move", "place": 2, "target": 10}):
            with self.subTest(intent=intent), self.assertRaises(ContractError):
                intent_to_request(intent, self.context())
        valid = {"id": "suspect:0", "category": "suspect", "index": 0, "text": "제외", "source": "식당 칸", "turn": 1}
        for update in ({"id": "npc:0"}, {"index": 1}, {"source_room": 2}, {"category": "unknown"}):
            with self.subTest(update=update), self.assertRaises(ContractError):
                normalize_acquired_clue(valid | update)

    def test_shared_fixture_correlates_player_result_and_GM_event_without_turning_failed_roll_into_fault(self):
        fixture = json.loads(Path(__file__).with_name("examples").joinpath("common_message_fixture.json").read_text(encoding="utf-8"))
        notice, request, result, event = [message["data"] for message in fixture["messages"]]
        ActionRequest.from_dict(request)
        for key in ("game_id", "agent_id", "cycle_id", "phase", "phase_epoch", "task_id"):
            self.assertEqual(notice[key], request[key])
            self.assertEqual(request[key], result[key])
        self.assertEqual(event["payload"]["result"], result)
        self.assertEqual(event["payload"]["visibility"], "gm_supervisory")
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(result["consumed"])
        self.assertEqual(result["actor_mode"], "active")

    @unittest.skipUnless((ROOT / "Game-engine/engine.py").exists() and (ROOT / "Agent_Reasoning/llm.py").exists(),
                         "Clone the team repositories beside gm_coordinator")
    def test_actual_B_commands_A_clues_and_C_decoder_and_receipt_agree_on_supported_meaning(self):
        report = run()
        self.assertTrue(report["all_catalog_ids_and_labels_match"])
        self.assertEqual(len(report["action_body_roundtrips"]), 3)
        self.assertEqual(len(report["acquired_information_roundtrips"]), 2)
        self.assertEqual([step["b_intent"]["kind"] for step in report["native_information_flow"]], ["move", "prepare", "check", "roll"])
        self.assertEqual(report["native_information_flow"][-2]["new_clue_ids"], [])
        self.assertIsNotNone(report["native_information_flow"][-2]["a_pending"])
        self.assertIsNone(report["native_information_flow"][-1]["a_pending"])
        self.assertFalse(report["boundaries"]["native_gm_completion_event"])


if __name__ == "__main__":
    unittest.main()
