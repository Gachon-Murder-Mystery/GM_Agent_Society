"""Behavioral checks for the provisional protocol and GM/engine boundaries."""
import copy
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .contracts import ActionRequest, ContractError, Control
from .coordinator import Coordinator
from .demo import run
from .mock_engine import MockEngine
from .store import Store


class CoordinatorTests(unittest.TestCase):
    def make(self, n=2, seed=42, path=":memory:", decay=1):
        store = Store(path)
        self.addCleanup(store.close)
        engine = MockEngine(store)
        catalog = engine.catalog()
        assignments = {f"p{i}": {"job": catalog["jobs"][0], "stat": 2,
                                "personality": catalog["personalities"][0], "personal_goal": catalog["personal_goals"][0]}
                       for i in range(n)}
        engine.create_game("g", assignments, seed, {"corpse_decay_per_cycle": decay})
        gm = Coordinator(engine, store, "g", Control("gm-primary", 1))
        gm.sync()
        return store, engine, gm

    @staticmethod
    def act(engine, ident, action, payload=None, action_id=None):
        snapshot = engine.agent_snapshot("g", ident)
        return engine.submit_action(ActionRequest(
            "g", ident, action_id or f"{ident}:{snapshot['phase_epoch']}",
            snapshot["cycle_id"], snapshot["phase"], snapshot["phase_epoch"], action, payload or {}))

    def movement(self, engine, gm):
        for ident in engine.gm_snapshot("g")["active_agents"]:
            self.act(engine, ident, "move", {"room": 2})
        self.assertEqual(gm.tick(), "advanced")

    def deduction(self, engine, gm):
        self.movement(engine, gm)
        result = self.act(engine, "p0", "explore")
        self.assertTrue(result["acquired_information"])
        for ident in engine.gm_snapshot("g")["active_agents"]:
            if ident != "p0":
                self.act(engine, ident, "pass")
        gm.tick()
        return result

    @staticmethod
    def wrong_guess(result):
        guess = {"suspect": 0, "weapon": 0, "place": 0}
        clue = result["acquired_information"][0]
        guess[clue["category"]] = clue["excluded_id"]
        return guess

    def test_three_phase_barrier_and_no_extra_movement(self):
        _, engine, gm = self.make()
        self.act(engine, "p0", "move", {"room": 2})
        self.assertEqual(gm.tick(), "waiting")
        self.assertEqual(set(gm.phase_notices()), {"p1"})
        self.act(engine, "p1", "stay")
        gm.tick()
        with self.assertRaises(ContractError):
            self.act(engine, "p0", "move", {"room": 3})
        self.assertEqual(engine.agent_snapshot("g", "p0")["self"]["room"], 2)

    def test_duplicate_action_is_idempotent_even_after_phase_change(self):
        _, engine, gm = self.make(1)
        request = ActionRequest("g", "p0", "same", 1, "movement", 1, "move", {"room": 2})
        first = engine.submit_action(request)
        gm.tick()
        sequence = engine.gm_snapshot("g")["event_sequence"]
        self.assertEqual(engine.submit_action(request), first)
        self.assertEqual(engine.gm_snapshot("g")["event_sequence"], sequence)
        with self.assertRaises(ContractError):
            engine.submit_action(ActionRequest("g", "p0", "same", 1, "movement", 1, "stay"))

    def test_concurrent_requests_cannot_double_spend(self):
        _, engine, _ = self.make(1)
        def submit(index):
            try:
                engine.submit_action(ActionRequest("g", "p0", str(index), 1, "movement", 1, "stay"))
                return True
            except ContractError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(submit, range(8))), 1)
        self.assertEqual(len(engine.events_after("g", 0)), 2)

    def test_late_action_rejected_and_invalid_request_does_not_spend(self):
        _, engine, gm = self.make(1)
        with self.assertRaises(ContractError):
            self.act(engine, "p0", "move", {"room": True})
        self.assertFalse(engine.gm_snapshot("g")["spent"])
        self.act(engine, "p0", "stay")
        gm.tick()
        with self.assertRaises(ContractError):
            engine.submit_action(ActionRequest("g", "p0", "late", 1, "movement", 1, "stay"))

    def test_phase_notice_reveals_presence_but_not_other_private_state(self):
        _, engine, gm = self.make()
        movement = gm.phase_notices()["p0"]
        self.assertTrue(movement["positions"])
        for occupant in movement["positions"]:
            self.assertNotIn("information_ids", occupant)
            self.assertNotIn("personal_goal", occupant)
            self.assertNotIn("stat", occupant)
        self.movement(engine, gm)
        interaction = gm.phase_notices()["p0"]
        self.assertNotIn("positions", interaction)
        self.assertIn("p1", {item["id"] for item in interaction["occupants"]})

    def test_gm_sees_acquired_information_without_automatic_sharing(self):
        _, engine, gm = self.make()
        self.movement(engine, gm)
        result = self.act(engine, "p0", "explore")
        ident = result["acquired_information"][0]["id"]
        supervisor = gm.sync()
        self.assertIn(ident, supervisor["inventories"]["p0"])
        self.assertIn(ident, {item["id"] for item in supervisor["observed_information"]})
        self.assertFalse(engine.agent_snapshot("g", "p1")["information"])
        self.assertFalse(gm.phase_notices()["p1"]["shared_information"])
        for view in (supervisor, engine.agent_snapshot("g", "p1")):
            self.assertNotIn("truth", view)
            self.assertNotIn("clues", view)
        self.assertEqual(len(supervisor["observed_information"]), 1)

    def test_explicit_sharing_requires_ownership_and_preserves_action_budget(self):
        _, engine, gm = self.make()
        self.movement(engine, gm)
        result = self.act(engine, "p0", "explore")
        ident = result["acquired_information"][0]["id"]
        with self.assertRaises(ContractError):
            engine.share_information("g", "p1", [ident], "bad")
        spent = engine.gm_snapshot("g")["spent"]
        first = engine.share_information("g", "p0", [ident], "share")
        self.assertEqual(engine.share_information("g", "p0", [ident], "share"), first)
        self.assertEqual(engine.gm_snapshot("g")["spent"], spent)
        self.assertEqual(engine.agent_snapshot("g", "p1")["shared_information"][0]["id"], ident)

    def test_interaction_requires_colocation(self):
        _, engine, gm = self.make()
        self.act(engine, "p0", "move", {"room": 2})
        self.act(engine, "p1", "move", {"room": 3})
        gm.tick()
        with self.assertRaises(ContractError):
            self.act(engine, "p0", "question", {"target_id": "p1"})
        self.assertNotIn("p0", engine.gm_snapshot("g")["spent"])

    def test_fault_npc_preserves_character_and_information_is_passive(self):
        _, engine, gm = self.make()
        self.movement(engine, gm)
        self.act(engine, "p0", "explore")
        before = engine.agent_snapshot("g", "p0")
        gm.report_player_fault("p0", "runtime_timeout")
        after = engine.agent_snapshot("g", "p0")
        for key in ("job", "stat", "personality", "personal_goal", "room"):
            self.assertEqual(before["self"][key], after["self"][key])
        self.assertEqual(before["information"], after["information"])
        self.assertEqual(after["self"]["mode"], "npc")
        with self.assertRaises(ContractError):
            self.act(engine, "p0", "explore", action_id="npc-cannot-act")
        result = self.act(engine, "p1", "question", {"target_id": "p0"})
        self.assertEqual(result["target_id"], "p0")
        self.assertTrue(result["acquired_information"])
        self.assertEqual(gm.tick(), "advanced")

    def test_death_is_not_fault_or_state_inheritance(self):
        _, engine, gm = self.make()
        result = self.deduction(engine, gm)
        death = self.act(engine, "p0", "accuse", self.wrong_guess(result))
        self.assertEqual(death["actor_mode"], "corpse")
        self.assertFalse(engine.agent_snapshot("g", "p1")["information"])
        kinds = [event["type"] for event in engine.events_after("g", 0)]
        self.assertIn("actor_died", kinds)
        self.assertNotIn("actor_became_npc", kinds)
        self.assertEqual(gm.tick(), "waiting")

    def test_corpse_does_not_move_and_decays_without_erasing_other_memory(self):
        _, engine, gm = self.make()
        result = self.deduction(engine, gm)
        ident = result["acquired_information"][0]["id"]
        engine.share_information("g", "p0", [ident], "share-before-death")
        self.act(engine, "p0", "accuse", self.wrong_guess(result))
        self.act(engine, "p1", "pass")
        gm.tick()
        corpse = engine.agent_snapshot("g", "p0")
        self.assertEqual(corpse["self"]["room"], 2)
        self.assertFalse(corpse["information"])
        self.assertEqual(engine.agent_snapshot("g", "p1")["shared_information"][0]["id"], ident)
        self.assertFalse(gm.sync()["inventories"]["p0"])

    def test_corpse_is_a_valid_in_place_investigation_target(self):
        _, engine, gm = self.make(seed=1, decay=0)
        result = self.deduction(engine, gm)
        self.act(engine, "p0", "accuse", self.wrong_guess(result))
        self.act(engine, "p1", "pass")
        gm.tick()
        self.act(engine, "p1", "move", {"room": 2})
        gm.tick()
        result = self.act(engine, "p1", "examine_corpse", {"target_id": "p0"})
        self.assertIsNotNone(result["roll"])
        self.assertTrue(result["consumed"])
        self.assertTrue(result["acquired_information"])

    def test_zero_players_ends_only_after_gm_and_preserves_cause(self):
        for cause in ("technical_failure", "deduction_failure"):
            with self.subTest(cause=cause):
                _, engine, gm = self.make(1)
                if cause == "technical_failure":
                    gm.report_player_fault("p0", "transport_failure")
                else:
                    result = self.deduction(engine, gm)
                    self.act(engine, "p0", "accuse", self.wrong_guess(result))
                self.assertFalse(engine.gm_snapshot("g")["ended"])
                self.assertEqual(gm.tick(), "ended")
                self.assertEqual(engine.gm_snapshot("g")["end_reason"], cause)

    def test_failed_corpse_roll_gives_no_information(self):
        _, engine, gm = self.make(seed=2, decay=0)
        result = self.deduction(engine, gm)
        self.act(engine, "p0", "accuse", self.wrong_guess(result))
        self.act(engine, "p1", "pass")
        gm.tick()
        self.act(engine, "p1", "move", {"room": 2})
        gm.tick()
        result = self.act(engine, "p1", "examine_corpse", {"target_id": "p0"})
        self.assertEqual(result["roll"]["successes"], 0)
        self.assertFalse(result["acquired_information"])
        self.assertTrue(engine.agent_snapshot("g", "p0")["information"])
        self.assertFalse(engine.agent_snapshot("g", "p1")["information"])

    def test_last_removal_cause_wins_for_a_mixed_history(self):
        _, engine, gm = self.make()
        result = self.deduction(engine, gm)
        gm.report_player_fault("p1", "runtime_failure")
        self.act(engine, "p0", "accuse", self.wrong_guess(result))
        gm.tick()
        self.assertEqual(engine.gm_snapshot("g")["end_reason"], "deduction_failure")

    def test_gm_takeover_preserves_progress_and_fences_old_gm(self):
        _, engine, gm = self.make()
        self.movement(engine, gm)
        self.act(engine, "p0", "explore")
        before = gm.sync()
        successor = Coordinator.replace_failed_gm(engine, gm.store, "g", gm.control)
        after = successor.sync()
        self.assertEqual(successor.control, Control("p0", 2))
        self.assertEqual(after["phase_epoch"], before["phase_epoch"])
        self.assertEqual(after["inventories"], before["inventories"])
        self.assertEqual(after["action_ledger"], before["action_ledger"])
        self.assertEqual(engine.agent_snapshot("g", "p0")["self"]["mode"], "npc")
        with self.assertRaises(ContractError):
            gm.tick()
        with self.assertRaises(ContractError):
            engine.advance_phase("g", gm.control, after["phase_epoch"])
        with self.assertRaises(ContractError):
            Coordinator.replace_failed_gm(engine, gm.store, "g", gm.control)
        self.act(engine, "p1", "pass")
        self.assertEqual(successor.tick(), "advanced")

    def test_last_player_becoming_gm_causes_technical_ending(self):
        store, engine, gm = self.make(1)
        successor = Coordinator.replace_failed_gm(engine, store, "g", gm.control)
        successor.tick()
        self.assertEqual(engine.gm_snapshot("g")["end_reason"], "technical_failure")

    def test_restart_and_event_replay_preserve_ledger(self):
        folder = Path(__file__).resolve().parent / ".test_tmp"
        folder.mkdir(exist_ok=True)
        path = folder / f"{uuid.uuid4().hex}.sqlite3"
        first, engine, gm = self.make(path=path)
        second = None
        try:
            self.movement(engine, gm)
            self.act(engine, "p0", "explore")
            before = copy.deepcopy(gm.sync())
            second = Store(path)
            restored = Coordinator(MockEngine(second), second, "g", gm.control)
            self.assertEqual(restored.sync(), before)
            self.assertEqual(restored.sync(), before)
            self.assertEqual(set(restored.phase_notices()), {"p1"})
        finally:
            if second is not None:
                second.close()
            first.close()
            path.unlink(missing_ok=True)

    def test_failure_before_commit_rolls_back_state_and_outbox(self):
        store, engine, _ = self.make(1)
        original = store.put
        before = engine.gm_snapshot("g")
        def fail(db, namespace, game_id, value):
            if namespace == "engine":
                raise OSError("injected storage failure")
            original(db, namespace, game_id, value)
        store.put = fail
        with self.assertRaises(OSError):
            self.act(engine, "p0", "move", {"room": 2})
        store.put = original
        self.assertEqual(engine.gm_snapshot("g"), before)
        self.assertEqual(len(engine.events_after("g", 0)), 1)

    def test_variable_rosters_and_reproducible_assignments(self):
        for n in (1, 2, 7):
            with self.subTest(n=n):
                store = Store()
                try:
                    engine = MockEngine(store)
                    gm = Coordinator.start(engine, store, "g", [f"a{i}" for i in range(n)], 9)
                    self.assertEqual(len(gm.phase_notices()), n)
                    for ident in gm.sync()["active_agents"]:
                        self.act(engine, ident, "stay")
                    self.assertEqual(gm.tick(), "advanced")
                    self.assertEqual(len(gm.phase_notices()), n)
                    other = Coordinator.start(engine, store, "g2", [f"a{i}" for i in range(n)], 9)
                    for ident in other.sync()["active_agents"]:
                        left = engine.agent_snapshot("g", ident)["self"]
                        right = engine.agent_snapshot("g2", ident)["self"]
                        for key in ("job", "stat", "personality", "personal_goal"):
                            self.assertEqual(left[key], right[key])
                finally:
                    store.close()

    def test_normal_parallel_mock_can_reach_solution_without_hidden_answer(self):
        result = run("normal", 3, max_cycles=100)
        self.assertTrue(result["ended"])
        self.assertEqual(result["end_reason"], "solved")
        self.assertEqual(result["event_counts"].get("actor_died", 0), 0)
        self.assertFalse(result["driver_stopped"])

    def test_unsupported_rule_override_is_rejected_instead_of_ignored(self):
        store, engine, _ = self.make()
        catalog = engine.catalog()
        assignment = {"p": {"job": catalog["jobs"][0], "stat": 2,
                            "personality": catalog["personalities"][0], "personal_goal": catalog["personal_goals"][0]}}
        with self.assertRaises(ContractError):
            engine.create_game("different", assignment, 42, {"wrong_accusation": "second_chance"})
        self.assertIsNone(store.read("engine", "different"))


if __name__ == "__main__":
    unittest.main()
