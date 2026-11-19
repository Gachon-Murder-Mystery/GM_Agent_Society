"""GM-side tests for the experiment-30 fixes (FIX-C7)."""
import random
import unittest

from .mission_policy import _pick, conflicts


class MissionPickTests(unittest.TestCase):
    def test_greedy_dead_end_falls_back_to_a_working_combination(self):
        self.assertTrue(conflicts("O1", "M45"))  # documented pair: 금고 털기 <-> 금고 경비
        players = [{"agent_id": "p1", "character_id": 7, "mbti": "ISTJ"}]
        # Novelty makes greedy take O1 first (M12 was received before); the only 하 candidate
        # conflicts with O1. The combination (M12, M02, M45) is valid.
        candidates = {"p1": {"상": ["O1", "M12"], "중": ["M02"], "하": ["M45"]}}
        chosen = _pick(players, candidates, lambda p, m, t: True, {"p1": ["M12"]}, False, random.Random(1))
        self.assertEqual(chosen["p1"], {"상": "M12", "중": "M02", "하": "M45"})

    def test_truly_infeasible_set_still_raises(self):
        players = [{"agent_id": "p1", "character_id": 7, "mbti": "ISTJ"}]
        candidates = {"p1": {"상": ["O1"], "중": ["M02"], "하": ["M45"]}}
        with self.assertRaises(ValueError):
            _pick(players, candidates, lambda p, m, t: True, {}, False, random.Random(1))


if __name__ == "__main__":
    unittest.main()
