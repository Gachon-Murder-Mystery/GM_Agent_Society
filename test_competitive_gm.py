"""Competitive v1.3 GM against A's real engine (in-process, fake clock, no LLM, no network).

Players here are minimal scripted stand-ins for B: they submit one action per phase
through `agent/action`, so the engine rolls the dice. The tests check that the GM
receives those results and continues the scenario. Skipped when the engine checkout
is missing (set ORNATE_ENGINE_DIR or keep ../Game-engine).
"""
import copy
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from .competitive_gm import CompetitiveGM, CompetitiveGMWorker, SettingsDrift
from . import gm_metrics
from .settings import SETTINGS, verify_engine

ENGINE_DIR = Path(os.environ.get("ORNATE_ENGINE_DIR", Path(__file__).resolve().parents[1] / "Game-engine"))
HAVE_ENGINE = (ENGINE_DIR / "competitive.py").is_file()
if HAVE_ENGINE and str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


class Local:
    """Same tool calls as the HTTP client, bound to one credential set."""

    def __init__(self, engine, credentials=None):
        self.engine, self.credentials = engine, dict(credentials or {})

    def bind(self, **credentials):
        return Local(self.engine, credentials)

    def call(self, route, **data):
        return self.engine.handle(route, self.credentials | data)


def explore_only(obs):
    return {"movement": ("stay", {}), "interaction": ("explore", {}), "deduction": ("pass", {})}[obs["phase"]]


def question_first(obs):
    """Ask a living character in the same room after the first cycle; otherwise explore."""
    if obs["phase"] == "interaction" and obs["cycle_id"] >= 2:
        living = [c for c in obs["occupants"] if c["mode"] != "corpse"]
        if living:
            return "question", {"target_id": living[0]["id"], "approach": "question",
                                "hypothesis": {"suspect": 0, "weapon": 0, "place": 0}}
    return explore_only(obs)


@unittest.skipUnless(HAVE_ENGINE, "A engine checkout not found")
class CompetitiveGMTests(unittest.TestCase):
    def setUp(self):
        from integration import ExternalEngine
        self.dir = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.engine = ExternalEngine(Path(self.dir.name) / "engine.sqlite3", lambda: self.now)
        self.operator = Local(self.engine, {"operator_token": self.engine.operator_token})

    def tearDown(self):
        self.dir.cleanup()

    def start(self, seed=7, **rules):
        pool = self.operator.call("pool/bootstrap", pool_id="society")
        created = self.operator.call("games/create", pool_id="society", game_id="g-" + uuid.uuid4().hex[:8],
                                     seed=seed, rules=rules)
        self.game_id = created["game_id"]
        self.identities = {a["agent_id"]: Local(self.engine).bind(**self.operator.call("pool/credentials", agent_id=a["agent_id"]))
                           for a in pool["agents"]}
        self.workers = {aid: CompetitiveGMWorker(identity) for aid, identity in self.identities.items()}
        return pool["current_gm_id"]

    def players(self):
        roles = {}
        for aid, identity in self.identities.items():
            session = identity.call("pool/session")
            if session["role"] == "player":
                roles[aid] = identity.bind(**session["credentials"])
        return roles

    def play(self, policy):
        """B stand-in: one action per phase through the engine, plus the B-3 heartbeat."""
        for aid, role in self.players().items():
            obs = role.call("agent/observe")
            role.call("agent/heartbeat")
            if not obs["remaining_actions"]:
                continue
            action, payload = policy(obs)
            request = {k: obs[k] for k in ("game_id", "cycle_id", "phase", "phase_epoch")}
            request.update(agent_id=aid, action_id=uuid.uuid4().hex, action=action, payload=payload)
            role.call("agent/action", request=request)

    def step_gms(self):
        return {aid: worker.step() for aid, worker in self.workers.items()}

    def gm_of(self, statuses):
        return next(aid for aid, status in statuses.items() if status != "not_gm")

    def run_game(self, policy, budget=600, on_loop=None):
        statuses = {}
        for _ in range(budget):
            statuses = self.step_gms()
            if "ended" in statuses.values():
                return statuses
            if on_loop:
                on_loop()
            self.play(policy)
            self.now += 1
            self.engine.tick()
        self.fail("game did not reach an official ending")

    def observe(self):
        return self.operator.call("games/observe", game_id=self.game_id)

    def finished_gm(self):
        return next(w.gm for w in self.workers.values() if w.gm is not None and w.gm.reported)

    def test_settings_are_unified_with_engine(self):
        import competitive
        self.assertEqual(verify_engine(self.operator.call("catalog")), [])
        self.assertEqual(competitive.RULES, SETTINGS["engine_rules"])
        self.assertEqual(competitive.REWARDS, SETTINGS["scoring"]["missions"])
        drifted = copy.deepcopy(self.operator.call("catalog"))
        drifted["default_rules"]["question_timeout"] = 45
        drifted["scoring"]["dead"] = -10
        self.assertEqual(len(verify_engine(drifted)), 2)

        class Drifted(Local):
            def call(self, route, **data):
                return drifted if route == "catalog" else super().call(route, **data)
        gm = self.start()
        session = self.identities[gm].call("pool/session")
        with self.assertRaises(SettingsDrift):
            CompetitiveGM(Drifted(self.engine, session["credentials"]), session["context_key"])

    def test_dice_results_reach_gm_and_scenario_runs_to_deadline(self):
        self.gm_id = self.start(deadline_cycles=11)
        self.run_game(question_first)
        gm = self.finished_gm()
        record = gm.report()
        self.assertEqual(record["resolution"], "deadline")
        explores = [r for r in record["rolls"] if r["kind"] == "explore"]
        questions = [r for r in record["rolls"] if r["kind"] == "question"]
        self.assertTrue(explores and questions)
        self.assertTrue(all(r["successes"] in (0, 1, 2, 3) for r in questions))
        interaction = [line for line in record["scenario"] if line["phase"] == "interaction"]
        self.assertEqual([line["cycle"] for line in interaction], list(range(1, 12)))
        self.assertTrue(all("사이클 탐문·탐색" in line["text"] for line in interaction))
        # Deadline warning is published in the cycle it appears, not at the end.
        events = self.engine.handle("events", gm.client.credentials)["events"]
        warning = next(e for e in events if e["type"] == "announcement" and e["payload"]["kind"] == "deadline_warning")
        next_cycle = next(e for e in events if e["type"] == "phase_advanced" and e["payload"]["cycle_id"] == 3)
        self.assertLess(warning["sequence"], next_cycle["sequence"])
        # Role setup came from C policy: first game, 12 players, two alternatives.
        state = self.observe()
        self.assertEqual(len(state["role_assignments"]), 12)
        self.assertEqual(state["plans"][0]["selected_alternative"], "explore")
        report = state["gm_report"]
        self.assertEqual(report["metrics"], {"completion": 1.0, "balance": None, "exploration": 1.0,
                                             "record_completeness": 1.0})
        expected = gm_metrics.gm_score({"completion": 1.0, "balance": None, "exploration": 1.0, "record": 1.0})
        self.assertAlmostEqual(report["score"], expected["score"])
        self.assertTrue(report["success"])

    def test_gm_waits_for_pending_reply_then_advances(self):
        self.gm_id = self.start(deadline_cycles=30)
        pending = None
        for _ in range(300):
            self.step_gms()
            self.play(question_first)
            state = self.observe()
            pending = [q for q in state["questions"] if q["pending"]]
            if pending:
                break
            self.now += 1
            self.engine.tick()
        self.assertTrue(pending, "no player held a clue when asked")
        epoch = state["phase_epoch"]
        self.assertEqual(self.workers[self.gm_id].step(), "waiting")
        self.assertEqual(self.observe()["phase_epoch"], epoch)
        # No reply: the engine's mandatory fallback resolves it at the deadline, then the GM moves on.
        self.now += SETTINGS["engine_rules"]["question_timeout"] + 1
        self.engine.tick()
        self.assertEqual(self.workers[self.gm_id].step(), "advanced")
        self.assertEqual(self.observe()["phase_epoch"], epoch + 1)
        rolls = self.workers[self.gm_id].gm.ledger["rolls"]
        self.assertIn(pending[0]["question_id"], [r.get("question_id") for r in rolls])

    def test_deduction_results_are_announced_each_cycle_and_winner_ends(self):
        self.gm_id = self.start(deadline_cycles=30)
        with self.engine.transaction() as db:
            truth = self.engine.load(db, "games", self.game_id, "id")["truth"]
        wrong = dict(truth, suspect=(truth["suspect"] + 1) % 12)
        order = []

        def deduce(obs):
            if obs["phase"] != "deduction":
                return explore_only(obs)
            if not order:
                order.extend(sorted(self.players()))
            me = obs["self"]["id"]
            if obs["cycle_id"] == 1 and me == order[0]:
                return "accuse", wrong | {"output_tokens": 50}
            if obs["cycle_id"] == 2 and me == order[1]:
                return "accuse", truth | {"output_tokens": 120}
            return "pass", {}

        announced_in_cycle_2 = []

        def check():
            state = self.observe()
            if state["cycle_id"] == 2 and state["phase"] == "movement":
                announced_in_cycle_2.append([a["kind"] for a in state["announcements"]])

        self.run_game(deduce, on_loop=check)
        self.assertIn("deduction_result", announced_in_cycle_2[0])
        state = self.observe()
        self.assertEqual(state["resolution"], "winner")
        verdicts = [v for a in state["announcements"] if a["kind"] == "deduction_result" for v in a["verdicts"]]
        self.assertEqual([(v["agent_id"], v["winner"]) for v in verdicts], [(order[0], False), (order[1], True)])
        self.assertEqual(len(self.finished_gm().ledger["verdicts"]), 2)
        self.assertTrue(state["gm_report"]["success"])

    def test_successor_rebuilds_from_engine_and_reports_technical_failure(self):
        self.gm_id = self.start(deadline_cycles=11)
        injected = []

        def fail_gm():
            state = self.observe()
            if not injected and state["cycle_id"] == 3:
                self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_GM",
                                   term=state["control"]["term"])
                injected.append(state["control"]["controller_id"])

        statuses = self.run_game(explore_only, on_loop=fail_gm)
        successor = self.gm_of(statuses)
        self.assertNotEqual(successor, injected[0])
        self.assertEqual(self.workers[injected[0]].step(), "not_gm")
        gm = self.workers[successor].gm
        self.assertEqual(gm.term, 2)
        # Context rebuilt from engine events only: results from before the succession are present.
        self.assertTrue(any(r["cycle"] == 1 for r in gm.ledger["rolls"]))
        self.assertEqual([s["type"] for s in gm.ledger["successions"]], ["gm_retired", "gm_succeeded"])
        report = self.observe()["gm_report"]
        self.assertTrue(report["technical_failure"])
        self.assertFalse(report["success"])

    def test_no_players_left_finishes_with_zero_completion(self):
        self.gm_id = self.start(deadline_cycles=30)
        self.step_gms()
        for aid in self.players():
            self.operator.call("games/operator", game_id=self.game_id, operation="FAIL_PLAYER", agent_id=aid)
        self.assertEqual(self.step_gms()[self.gm_id], "ended")
        state = self.observe()
        self.assertEqual((state["resolution"], state["ended"]), ("no_players", True))
        self.assertEqual(state["gm_report"]["metrics"]["completion"], 0.0)
        self.assertFalse(state["gm_report"]["success"])


if __name__ == "__main__":
    unittest.main()
