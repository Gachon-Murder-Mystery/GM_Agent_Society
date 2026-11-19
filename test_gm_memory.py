"""FIX-C15 (user decision 2026-11-09): the GM keeps its own memory and learns how to keep a game
moving through bug-like situations, with remedies that never change a game's outcome."""
import collections
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

from .gm_memory import GMMemory, IncidentWatch, MIN_TRIES, DEFAULT_STALL, ENGINE_ERROR_LIMIT


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def view(epoch=5, spent=(), needs=(), pending=0, ready_release=False, ready_advance=False, active=("a", "b")):
    return {"phase": "interaction", "stage": "negotiation", "phase_epoch": epoch, "active_agents": list(active),
            "spent": {x: 1 for x in spent}, "needs_request": list(needs), "overdue": [],
            "questions": [{"pending": True, "target_agent": "b"}] * pending,
            "ready_to_release": ready_release, "ready_to_advance": ready_advance}


class GMMemoryTests(unittest.TestCase):
    def test_untried_remedies_first_then_thompson_sampling(self):
        m = GMMemory("society:gm", directory="", rng=random.Random(1))
        self.assertEqual(m.choose("no_action"), "wait")
        for _ in range(MIN_TRIES):
            m.learn("no_action", "wait", False, 0)
        self.assertEqual(m.choose("no_action"), "re_request")  # warm start: untried first [FIX-C26 order]
        for _ in range(MIN_TRIES):
            m.learn("no_action", "re_request", False, 0)
        self.assertEqual(m.choose("no_action"), "resync")
        for _ in range(3):
            m.learn("no_action", "resync", True, 20)
            m.learn("no_action", "wait", False, 0)
        m.learn("no_action", "escalate", True, 1)              # never scored: recording is not a fix
        self.assertNotIn("escalate", m.data["playbook"]["no_action"])
        picks = collections.Counter(m.choose("no_action") for _ in range(400))
        self.assertGreater(picks["resync"], 340)               # mostly the remedy that works ...
        self.assertGreater(picks["wait"] + picks["re_request"], 0)   # ... but the others are still explored
        self.assertNotIn("escalate", picks)

    def test_old_outcomes_fade_so_the_choice_readapts(self):
        """[FIX-C16] Non-stationary: a remedy that used to work and now fails loses its lead."""
        m = GMMemory("society:gm", directory="", rng=random.Random(2))
        for _ in range(10):
            m.learn("engine_error", "retry_short", True, 1)
            m.learn("engine_error", "retry_long", False, 0)
        for _ in range(10):                                    # the environment changed
            m.learn("engine_error", "retry_short", False, 0)
            m.learn("engine_error", "retry_long", True, 5)
        picks = collections.Counter(m.choose("engine_error") for _ in range(400))
        self.assertGreater(picks["retry_long"], picks["retry_short"])
        s = m.data["playbook"]["engine_error"]["retry_short"]
        self.assertEqual((s["tries"], s["resolved"]), (20, 10))   # raw counts stay for the report

    def test_stall_threshold_is_learned_from_the_pool_pace(self):
        m = GMMemory("society:gm", directory="")
        self.assertEqual(m.stall_threshold("interaction:negotiation"), DEFAULT_STALL)
        for s in (40, 50, 60, 70, 80, 90, 100, 110, 120, 130):
            m.norm("interaction:negotiation", s)
        self.assertEqual(m.stall_threshold("interaction:negotiation"), 240)   # 2 x p90 (120)
        for _ in range(20):
            m.norm("movement:-", 5)
        self.assertEqual(m.stall_threshold("movement:-"), 60)                 # floor

    def test_memory_is_the_gms_own_file_and_persists(self):
        with tempfile.TemporaryDirectory() as d:
            m = GMMemory("society:gm", directory=d)
            m.learn("engine_error", "retry_short", True, 2)
            m.record_game({"game_id": "g1"})
            again = GMMemory("society:gm", directory=d)
            self.assertEqual(again.data["games"][0]["game_id"], "g1")
            self.assertEqual(again.data["playbook"]["engine_error"]["retry_short"]["resolved"], 1)
            self.assertTrue(again.path.name.startswith("gm_society_gm"))


class IncidentWatchTests(unittest.TestCase):
    def watch(self, memory=None):
        clock = Clock()
        return IncidentWatch(memory or GMMemory("society:gm", directory=""), clock), clock

    def test_a_silent_player_opens_an_incident_and_failed_remedies_are_replaced(self):
        w, clock = self.watch()
        self.assertEqual(w.observe(view(spent=("a",))), [])
        clock.t += DEFAULT_STALL + 1
        [(key, remedy)] = w.observe(view(spent=("a",)))
        self.assertEqual((w.open[key]["kind"], w.open[key]["subject"], remedy), ("no_action", ["b"], "wait"))
        clock.t += DEFAULT_STALL / 2 + 1                       # 'wait' did not help
        self.assertEqual(w.observe(view(spent=("a",))), [(key, "re_request")])   # [FIX-C26]
        clock.t += 10
        self.assertEqual(w.observe(view(spent=("a", "b"))), [])  # progress: resolved by the re-sent request
        stats = w.memory.data["playbook"]["no_action"]
        self.assertEqual((stats["wait"]["resolved"], stats["re_request"]["resolved"]), (0, 1))
        self.assertEqual(w.summary()["incidents"]["no_action"]["resolved"], 1)

    def test_learning_carries_into_the_next_game(self):
        memory = GMMemory("society:gm", directory="")
        memory.rng = random.Random(3)
        for game in range(3):                                   # 'wait' never helps, 'resync' always does
            w, clock = self.watch(memory)
            w.observe(view(spent=("a",)))
            clock.t += DEFAULT_STALL + 1
            [(key, remedy)] = w.observe(view(spent=("a",)))
            while remedy != "resync":
                clock.t += DEFAULT_STALL / 2 + 1
                [(key, remedy)] = w.observe(view(spent=("a",)))
            clock.t += 5
            w.observe(view(spent=("a", "b")))
        picks = collections.Counter(memory.choose("no_action") for _ in range(200))
        self.assertGreater(picks["resync"], 150)                # learned: resync is tried first now

    def test_diagnosis(self):
        w, clock = self.watch()
        for v, kind in ((view(spent=("a", "b"), ready_advance=True), "not_advancing"),
                        (view(epoch=6, needs=("b",)), "request_not_issued"),
                        (view(epoch=7, spent=("a", "b"), pending=1), "answer_pending")):
            w.observe(v)
            clock.t += DEFAULT_STALL + 1
            [(key, _)] = w.observe(v)
            self.assertEqual(w.open[key]["kind"], kind)

    def test_engine_errors_are_waited_out_then_escalated(self):
        w, clock = self.watch()
        seen = [w.engine_error(OSError("refused")) for _ in range(ENGINE_ERROR_LIMIT)]
        self.assertEqual(seen[0], "retry_short")
        self.assertIn("retry_long", seen)
        self.assertEqual(seen[-1], "escalate")
        w2, _ = self.watch()
        w2.engine_error(OSError("blip"))
        w2.engine_ok()
        self.assertEqual(w2.memory.data["playbook"]["engine_error"]["retry_short"]["resolved"], 1)

    def test_paused_time_is_not_a_stall(self):
        """[FIX-I11] A model outage pauses the game; that time opens no incident afterwards."""
        w, clock = self.watch()
        w.observe(view(spent=("a",)))
        clock.t += 50
        self.assertEqual(w.observe(dict(view(spent=("a",)), status="paused")), [])
        clock.t += 3000                                          # paused for 50 minutes
        w.paused_for(3000)
        self.assertEqual(w.observe(view(spent=("a",))), [])
        clock.t += DEFAULT_STALL + 1
        self.assertEqual(len(w.observe(view(spent=("a",)))), 1)  # a real stall still counts

    def test_escalation_carries_what_was_tried(self):
        w, clock = self.watch()
        w.observe(view(spent=("a",)))
        clock.t += DEFAULT_STALL + 1
        [(key, _)] = w.observe(view(spent=("a",)))
        for _ in range(3):   # [FIX-C26] wait, re_request, resync, then the hand-off
            clock.t += DEFAULT_STALL / 2 + 1
            out = w.observe(view(spent=("a",)))
        self.assertEqual(out, [(key, "escalate")])
        ctx = w.escalation_context(key, "nothing moved")
        self.assertEqual(ctx["tried"], ["wait", "re_request", "resync"])
        self.assertGreater(ctx["quiet_seconds"], DEFAULT_STALL)
        self.assertEqual(ctx["signals"]["spent"], ["a"])
        row = w.memory.escalate("no_action", ["b"], "interaction:negotiation", ctx)
        self.assertEqual(w.memory.data["escalations"][-1]["tried"], ["wait", "re_request", "resync"])
        self.assertEqual(row["why"], "nothing moved")

    def test_incident_trace_replays_to_the_same_diagnosis(self):
        """[FIX-C16] Regression traces: the recorded incident is diagnosed the same on replay."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        import replay_traces
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "regression_traces.jsonl")
            old = os.environ.get("GAME_TRACE_FILE")
            os.environ["GAME_TRACE_FILE"] = path
            try:
                w, clock = self.watch()
                for _ in range(30):                              # GM ticks: only changes are kept
                    w.observe(view(spent=()))
                    clock.t += 0.1
                w.observe(view(spent=("a",)))
                clock.t += DEFAULT_STALL + 1
                w.observe(view(spent=("a",)))
                clock.t += 5
                w.observe(view(spent=("a", "b")))                 # resolved -> trace written
            finally:
                if old is None:
                    os.environ.pop("GAME_TRACE_FILE", None)
                else:
                    os.environ["GAME_TRACE_FILE"] = old
            [record] = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual((record["kind"], record["outcome"]), ("no_action", "resolved_by:wait"))
            self.assertLessEqual(len(record["views"]), 3)
            self.assertEqual(replay_traces.replay(record), (True, ["no_action"]))
            self.assertEqual(replay_traces.main([path]), 0)
            record["kind"] = "answer_pending"                    # a changed diagnosis is caught
            self.assertFalse(replay_traces.replay(record)[0])

    def test_relationship_reports_are_summarised_for_the_record_only(self):
        """FIX-A35/C17: what agents report is summarised in the REPORT; not used to run the game and
        not written to my own memory."""
        from types import SimpleNamespace
        from .competitive_gm import CompetitiveGM
        gm = SimpleNamespace(RELATIONS_SUMMARY_LIMIT=CompetitiveGM.RELATIONS_SUMMARY_LIMIT, ledger={"relations": [
            {"cycle": 1, "agent_id": "society:aaaaaa", "scores": {"society:bbbbbb": 60.0}},
            {"cycle": 2, "agent_id": "society:bbbbbb", "scores": {"society:aaaaaa": 40.0, "society:cccccc": 55.0}},
            {"cycle": 3, "agent_id": "society:aaaaaa", "scores": {"society:bbbbbb": 70.0}}]})
        s = CompetitiveGM.relations_summary(gm)
        self.assertEqual(s["kind"], "reported_by_agents")
        self.assertEqual(s["latest"], {"aaaaaa": {"bbbbbb": 70}, "bbbbbb": {"aaaaaa": 40, "cccccc": 55}})
        self.assertEqual(s["mutual"], {"pairs": 1, "mean_gap": 30.0, "largest_gaps": [["aaaaaa", "bbbbbb", 70, 40, 30]]})
        self.assertEqual(s["received_mean"], {"bbbbbb": 70.0, "aaaaaa": 40.0, "cccccc": 55.0})
        self.assertEqual([c[1] for c in s["by_cycle"]], [60.0, 51.7, 55.0])
        self.assertIsNone(CompetitiveGM.relations_summary(SimpleNamespace(ledger={})))
        memory = GMMemory("society:gm", directory="")
        self.assertNotIn("relations", json.dumps(memory.data))

    def test_relationship_summary_fits_the_engine_request_limit(self):
        """FIX-C17b: runs/v9-check-30c seed 1 (12 players, 30 cycles): REPORT refused as too large."""
        import uuid
        from types import SimpleNamespace
        from .competitive_gm import CompetitiveGM
        ids = ["society:" + uuid.uuid4().hex for _ in range(12)]
        reports = [{"cycle": c, "agent_id": a, "scores": {b: 50.0 + (c * 7 + i) % 40 for i, b in enumerate(ids) if b != a}}
                   for c in range(1, 31) for a in ids]
        gm = SimpleNamespace(RELATIONS_SUMMARY_LIMIT=CompetitiveGM.RELATIONS_SUMMARY_LIMIT, ledger={"relations": reports})
        s = CompetitiveGM.relations_summary(gm)
        body = {"command_id": "x" * 80, "operation": "REPORT", "metrics": {"completion": 1, "balance": None, "exploration": 1, "record": 1},
                "evidence": ["g" * 36 + ":event:%d" % n for n in range(6)], "reported_relationships": s,
                "game_id": "g" * 36, "controller_token": "t" * 43}
        self.assertLess(len(json.dumps(body, ensure_ascii=False).encode("utf-8")), 18000)
        self.assertEqual(s["agents"], 12)

    def test_response_times_per_player(self):
        w, clock = self.watch()
        w.requested(["a"])
        clock.t += 12
        w.acted("a")
        self.assertEqual(w.summary()["players"]["a"]["response_seconds"], 12)


if __name__ == "__main__":
    unittest.main()
