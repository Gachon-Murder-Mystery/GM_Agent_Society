"""Spec v1.2 GM policies and scripted players. Pure unit tests: no engine, no LLM, no network."""
import random
import unittest

from . import build_v12_data
from .competitive_player import CompetitivePlayer, Knowledge, RandomPlayer
from .gm_metrics import (balance, completion, exploration, fairness_report, gm_score, record_completeness)
from .mission_policy import TIERS, conflicts, effective_tier, load_data, plan_missions, quantity_for
from .role_policy import (JOBS, choose_participants, covered, job_competency, maturity_weight, normalized_score,
                          plan_roles, player_count)

DATA = load_data()
SHEETS = {c["id"]: c for c in DATA["characters"]}
AGENTS = [f"p{i:02d}" for i in range(16)]
MBTI = dict(zip(AGENTS, DATA["mbti"]))


def full_history(scores=None, games_per_job=1):
    """Every agent played every job `games_per_job` times; score from `scores(agent, job)`."""
    history = {}
    for aid in AGENTS:
        records, n = [], 0
        for _ in range(games_per_job):
            for job in range(JOBS):
                n += 1
                records.append({"game_id": f"g{n}", "job_id": job, "participation_index": n,
                                "final_score": scores(aid, job) if scores else 30})
        history[aid] = records
    return history


class DataTests(unittest.TestCase):
    def test_extracted_data_matches_spec_and_design(self):
        self.assertEqual(build_v12_data.build(), DATA, "v12_data.json is stale: run build_v12_data")
        self.assertEqual(len(DATA["characters"]), 12)
        self.assertEqual(len(DATA["missions"]), 58)
        self.assertEqual(len(set(DATA["mbti"])), 16)
        self.assertEqual([c["job_id"] for c in DATA["characters"]], list(range(12)))
        self.assertTrue(all(sum(c["stats"].values()) == 11 for c in DATA["characters"]))
        known = set(DATA["jobs"]) | set(DATA["stat_tokens"]) | set("EISNTFJP")
        self.assertTrue(all(t in known for m in DATA["missions"] for t in m["easier"] + m["harder"]))
        designated = {m["id"]: m["designate"] for m in DATA["missions"] if m["designate"]}
        self.assertEqual(designated["O6"], "indirect_clues")
        self.assertEqual(designated["M15"], "npc")


class DifficultyTests(unittest.TestCase):
    missions = {m["id"]: m for m in DATA["missions"]}

    def test_design_doc_examples(self):
        stop_train = self.missions["O3"]  # 기준 중, 쉬워짐 기술자
        self.assertEqual(effective_tier(stop_train, SHEETS[8], "ISTJ", DATA), "하")
        self.assertEqual(effective_tier(stop_train, SHEETS[5], "ISTJ", DATA), "중")
        detective = self.missions["M51"]
        self.assertEqual(effective_tier(detective, SHEETS[0], "ESTJ", DATA, 5), "하")
        self.assertIsNone(quantity_for(detective, "상", SHEETS[0], "ESTJ", DATA))  # 상 unreachable for 탐정
        self.assertEqual(quantity_for(detective, "중", SHEETS[2], "ESTJ", DATA), 5)

    def test_both_conditions_keep_base(self):
        secret = self.missions["O2"]  # 쉬워짐 사기꾼·I, 어려워짐 사업가·E
        self.assertEqual(effective_tier(secret, SHEETS[3], "ESTP", DATA), "중")
        self.assertEqual(effective_tier(secret, SHEETS[3], "ISTP", DATA), "하")


class RolePolicyTests(unittest.TestCase):
    def test_weights_and_normalization(self):
        self.assertAlmostEqual(maturity_weight(1), (1 / 6) ** 2)
        self.assertAlmostEqual(maturity_weight(5), 0.25)
        self.assertLess(maturity_weight(1), maturity_weight(10))
        self.assertEqual(normalized_score(-20), 0.0)
        self.assertEqual(normalized_score(160), 1.0)

    def test_competency_waits_for_coverage_and_discounts_early_games(self):
        records = [{"job_id": j, "participation_index": j + 1, "final_score": 0} for j in range(11)]
        self.assertFalse(covered(records))
        self.assertIsNone(job_competency(records))
        records += [{"job_id": 11, "participation_index": 12, "final_score": 0},
                    {"job_id": 0, "participation_index": 13, "final_score": 160}]
        competency = job_competency(records)
        # Job 0: a 1st game scored -20 vs a 13th game scored 160. The late game dominates.
        self.assertGreater(competency[0], 0.95)

    def test_first_game_is_random_full_and_records_alternatives(self):
        plan = plan_roles(AGENTS, {}, seed=1)
        self.assertEqual(plan["player_count"], 12)
        self.assertEqual(len({a["character_id"] for a in plan["activation"]}), 12)
        self.assertEqual(plan["selected_alternative"], "explore")
        self.assertGreaterEqual(len(plan["alternatives"]), 2)
        self.assertIn("첫 게임", plan["reason"])
        self.assertNotEqual(plan["activation"], plan_roles(AGENTS, {}, seed=2)["activation"])

    def test_resting_agents_are_the_most_played(self):
        history = {aid: [{"job_id": 0, "participation_index": 1, "final_score": 0}] * (3 if aid < "p04" else 1)
                   for aid in AGENTS}
        chosen = choose_participants(AGENTS, history, 12, random.Random(0))
        self.assertTrue(all(aid not in chosen for aid in AGENTS[:4]))

    def test_exploration_gives_least_played_jobs(self):
        rng = random.Random(3)
        history = {aid: [{"job_id": j, "participation_index": i + 1, "final_score": 0}
                         for i, j in enumerate(rng.sample(range(JOBS), 6))] for aid in AGENTS}
        plan = plan_roles(AGENTS, history, seed=3)
        self.assertEqual(plan["exploratory_ratio"], 1.0)
        for item in plan["activation"]:
            played = {r["job_id"] for r in history[item["agent_id"]]}
            self.assertNotIn(item["character_id"], played)

    def test_player_count_rules(self):
        self.assertEqual(player_count(AGENTS, {}, requested=3), 12)  # before coverage: always 12
        history = full_history()
        self.assertEqual(player_count(AGENTS, history, requested=3), 3)
        with self.assertRaises(ValueError):
            player_count(AGENTS, history, requested=2)

    def test_balance_after_coverage_reduces_spread(self):
        history = full_history(lambda aid, job: (int(aid[1:]) * 7 + job * 13) % 180 - 20, games_per_job=2)
        plan = plan_roles(AGENTS, history, seed=5)
        self.assertEqual(plan["selected_alternative"], "balance")
        ids = [alt["id"] for alt in plan["alternatives"]]
        self.assertEqual(ids, ["explore", "balance"])
        explore_spread = float(plan["alternatives"][1]["reason"].split("탐색안 ")[1].split(")")[0])
        self.assertLess(plan["strength_spread"], explore_spread)


class MissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.players = [{"agent_id": f"p{i:02d}", "character_id": i, "mbti": DATA["mbti"][i]} for i in range(4)]
        self.characters = [{"character_id": i, "mbti": DATA["mbti"][i], "controller": f"p{i:02d}" if i < 4 else None}
                           for i in range(12)]
        self.clues = [f"clue:i{i}" for i in range(6)]

    def candidates(self, per_player):
        return {p["agent_id"]: per_player[i] for i, p in enumerate(self.players)}

    def test_conflict_pair_is_chosen_and_targets_point_at_each_other(self):
        base = {"하": ["O7", "M09", "M23"], "중": ["M01", "M02", "M03"], "상": ["O1", "M12", "M18"]}
        follow = {"하": ["O7", "M09", "M23"], "중": ["M08", "M02", "M03"], "상": ["O1", "M12", "M18"]}
        tail = {"하": ["O7", "M09", "M23"], "중": ["M07", "M02", "M03"], "상": ["M12", "M18", "M39"]}
        guard = {"하": ["O7", "M09", "M23"], "중": ["M45", "M02", "M03"], "상": ["M12", "M18", "M39"]}
        plan = plan_missions(self.players, self.characters, self.candidates([base, follow, tail, guard]), seed=4,
                             indirect_clue_ids=self.clues)
        self.assertEqual(plan["selected_alternative"], "conflict")
        missions = {aid: {e["mission_id"]: e for e in entries} for aid, entries in plan["sub_missions"].items()}
        self.assertEqual(missions["p01"]["M08"]["targets"]["player"], "p02")
        self.assertEqual(missions["p02"]["M07"]["targets"]["player"], "p01")
        self.assertIn("M45", missions["p03"])
        for aid, entries in plan["sub_missions"].items():
            self.assertEqual([e["tier"] for e in entries], TIERS)
            for e in entries:
                self.assertNotEqual(e["targets"].get("player"), aid)

    def test_npc_targets_need_npcs_and_quantity_matches_tier(self):
        everyone = [{"character_id": i, "mbti": DATA["mbti"][i], "controller": f"x{i}"} for i in range(12)]
        slots = {"하": ["M47", "O7", "M23"], "중": ["M15", "M51", "M02"], "상": ["O6", "M12", "M18"]}
        plan = plan_missions(self.players, everyone, self.candidates([slots] * 4), seed=7, indirect_clue_ids=self.clues[:2])
        chosen = {e["mission_id"] for entries in plan["sub_missions"].values() for e in entries}
        self.assertFalse(chosen & {"M47", "M15", "O6"})  # no NPC, too few known indirect clues
        plan = plan_missions(self.players, self.characters, self.candidates([{**slots, "중": ["M51"]}] * 4), seed=7,
                             indirect_clue_ids=self.clues)
        for aid, entries in plan["sub_missions"].items():
            m51 = next(e for e in entries if e["mission_id"] == "M51")
            sheet = SHEETS[next(p for p in self.players if p["agent_id"] == aid)["character_id"]]
            # 탐정·관찰5 shift one step easier: base 상 (N=8) becomes 중; N=3/5 would land on 하.
            expected = 8 if sheet["job"] == "탐정" or sheet["stats"]["observe"] == 5 else 5
            self.assertEqual(m51["params"]["N"], expected)

    def test_self_conflict_is_avoided(self):
        slots = {"하": ["O7", "M23", "M09"], "중": ["M48", "M01", "M02"], "상": ["M49", "M12", "M18"]}
        slots["상"] = ["M12"]
        slots["중"] = ["M48", "M01"]
        plan = plan_missions(self.players[:3], self.characters, {p["agent_id"]: {**slots, "하": ["M49", "O7"]}
                                                                for p in self.players[:3]}, seed=1)
        for entries in plan["sub_missions"].values():
            ids = [e["mission_id"] for e in entries]
            self.assertFalse(any(conflicts(a, b) for a in ids for b in ids if a < b))


class MetricsTests(unittest.TestCase):
    def test_score_renormalizes_when_balance_not_judged(self):
        result = gm_score({"completion": 1.0, "balance": None, "exploration": 0.5, "record": 1.0})
        self.assertAlmostEqual(sum(result["weights"].values()), 1.0)
        self.assertNotIn("balance", result["weights"])
        self.assertAlmostEqual(result["score"], (0.35 + 0.10 + 0.20) / 0.75)
        self.assertTrue(result["success"])
        self.assertFalse(gm_score({"completion": 1, "balance": 1, "exploration": 1, "record": 1},
                                  technical_failure=True)["success"])

    def test_components(self):
        self.assertEqual(completion("no_players"), 0.0)
        self.assertEqual(completion("deadline", recovery_cycles=12, cycles=120), 0.9)
        self.assertIsNone(balance([0.4]))
        self.assertEqual(balance([0.5, 0.5]), 1.0)
        self.assertEqual(exploration([True, False], [True, True]), 0.75)
        decisions = [{"alternatives": [{"id": "a"}, {"id": "b"}], "selected_alternative": "a", "reason": "x"},
                     {"alternatives": [{"id": "a"}], "selected_alternative": "a", "reason": "x"}]
        self.assertEqual(record_completeness(decisions), 0.5)

    def test_fairness_flags_concentration_without_intervening(self):
        questions = [{"asker_id": "a", "target_id": "c", "approach": "threaten", "successes": 2}] * 4 + \
                    [{"asker_id": "c", "target_id": "a", "approach": "question", "successes": 1}]
        report = fairness_report(questions, ["a", "b", "c"])
        self.assertEqual({(f["agent_id"], f["metric"]) for f in report["flagged"]}, {("c", "forced"), ("c", "threats")})
        self.assertIsNone(report["intervention"])
        self.assertEqual(fairness_report(questions[-1:], ["a", "b", "c"])["flagged"], [])


def definite(cid, category, candidate):
    return {"id": cid, "kind": "definite", "category": category, "candidate": candidate}


class KnowledgeTests(unittest.TestCase):
    def test_design_doc_examples(self):
        k = Knowledge()
        k.add({"id": "i1", "kind": "indirect", "type": "character_room", "character": 0, "room": 0})
        k.add(definite("d1", "place", 0))
        k.add({"id": "i2", "kind": "indirect", "type": "companion", "character": 1, "other": 2})
        k.add({"id": "i3", "kind": "indirect", "type": "character_room", "character": 2, "room": 2})
        k.add(definite("d2", "place", 2))
        k.add({"id": "i4", "kind": "indirect", "type": "weapon_room", "weapon": 6, "room": 4})
        k.add(definite("d3", "place", 4))
        k.infer()
        self.assertEqual(k.excluded["suspect"][0], {"i1", "d1"})               # example 1
        self.assertEqual(set(k.excluded["suspect"]) & {1, 2}, {1, 2})           # example 2
        self.assertIn("i2", k.excluded["suspect"][1])
        self.assertEqual(k.excluded["weapon"][6], {"i4", "d3"})                 # example 3

    def test_known_scene_excludes_people_elsewhere(self):
        k = Knowledge()
        for place in range(7):
            k.add(definite(f"p{place}", "place", place))
        k.add({"id": "i1", "kind": "indirect", "type": "character_room", "character": 5, "room": 3})
        k.infer()
        self.assertIn(5, k.excluded["suspect"])


class PlayerTests(unittest.TestCase):
    def player(self, mbti="ISTJ", seed=0):
        return CompetitivePlayer("p00", SHEETS[0], mbti, seed, names={c["id"]: c["name"] for c in DATA["characters"]},
                                 places=dict(enumerate(DATA["places"])))

    def test_hypothesis_probes_one_unknown_and_fills_with_excluded(self):
        p = self.player()
        for s in range(11):
            p.ingest({"clues": [definite(f"s{s}", "suspect", s)]})
        p.ingest({"clues": [definite("w0", "weapon", 0), definite("r0", "place", 0)]})
        for _ in range(20):
            hyp = p.hypothesis()
            probes = [c for c in hyp if hyp[c] not in p.knowledge.excluded[c]]
            self.assertEqual(len(probes), 1)

    def test_responses_are_true_and_hide_by_default(self):
        p = self.player("ISTJ")
        p.ingest({"clues": [definite("d1", "suspect", 3),
                            {"id": "i1", "kind": "indirect", "type": "character_room", "character": 4, "room": 6}]})
        hyp = {"suspect": 3, "weapon": 1, "place": 2}
        answer = p.respond({"asker_id": "p01", "hypothesis": hyp, "successes": 2})
        self.assertEqual(answer["form"], "ambiguous")
        for s in answer["statements"]:
            self.assertNotEqual((s.get("category"), s.get("candidate")), ("suspect", 3))  # stays true
        self.assertEqual(p.respond({"asker_id": "p01", "hypothesis": hyp, "successes": 0})["form"], "none")
        three = p.respond({"asker_id": "p01", "hypothesis": hyp, "successes": 3})
        self.assertEqual([s["kind"] for s in three["statements"]], ["definite", "ambiguous", "ambiguous"])

    def test_warm_feeler_gives_definite_and_indirect_avoids_connection(self):
        p = self.player("ISFJ")
        p.relationship["p01"] = 80
        p.ingest({"clues": [definite("d1", "suspect", 3)]})
        self.assertEqual(p.respond({"asker_id": "p01", "hypothesis": {"suspect": 1, "weapon": 1, "place": 1},
                                    "successes": 2})["form"], "definite")
        q = self.player()
        # Holding a clue on every hypothesis candidate leaves no true ambiguous statement: indirect clues fill in.
        q.ingest({"clues": [definite("s0", "suspect", 0), definite("w0", "weapon", 0), definite("r0", "place", 0),
                            {"id": "near", "kind": "indirect", "type": "character_room", "character": 4, "room": 6},
                            {"id": "far", "kind": "indirect", "type": "character_room", "character": 5, "room": 1}]})
        q.asked_me["p02"] = [{"suspect": 9, "weapon": 1, "place": 6}]  # p02 has looked at place 6
        hyp = {"suspect": 0, "weapon": 0, "place": 0}
        answer = q.respond({"asker_id": "p02", "hypothesis": hyp, "successes": 1})
        self.assertEqual(answer["statements"], [{"kind": "indirect", "clue_id": "far",
                                                 "narration": q.narrate({"type": "character_room", "room": 1})}])
        hyp_with_gap = {"suspect": 7, "weapon": 0, "place": 0}
        answer = q.respond({"asker_id": "p02", "hypothesis": hyp_with_gap, "successes": 1})
        self.assertEqual(answer["statements"][0], {"kind": "ambiguous", "category": "suspect", "candidate": 7})

    def test_narration_omits_and_never_concludes(self):
        p = self.player()
        clue = {"id": "i1", "kind": "indirect", "type": "character_room", "character": 0, "room": 0}
        text = p.narrate(clue)
        self.assertIn("라운지 칸", text)
        self.assertNotIn("알렉스", text)  # who is omitted
        self.assertNotIn("범인", text)

    def test_challenge_threshold_follows_s_n(self):
        for mbti, expected in (("ISTJ", "hold"), ("INTJ", "challenge")):
            p = self.player(mbti)
            for s in range(11):
                p.ingest({"clues": [definite(f"s{s}", "suspect", s)]})
            for w in range(6):
                p.ingest({"clues": [definite(f"w{w}", "weapon", w)]})
            for r in range(7):
                p.ingest({"clues": [definite(f"r{r}", "place", r)]})
            decision = p.deduce()  # 1 x 2 x 1 = 2 answers left
            self.assertEqual(decision["decision"], expected)
            self.assertEqual(decision["reasoning_tokens"], 0)

    def test_exhausted_room_triggers_move_and_random_player_is_legal(self):
        p = self.player()
        p.ingest({"action": "explore", "target": "room", "successes": 1, "clues": []})
        self.assertEqual(p.move()["action"], "move")
        r = RandomPlayer("p01", SHEETS[1], "ENFP", 3)
        for _ in range(50):
            action = r.interact({"here": [{"id": "p02", "kind": "player"}]})
            self.assertIn(action["action"], {"explore", "question", "prepare"})


if __name__ == "__main__":
    unittest.main()
