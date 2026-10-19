"""GM performance score (spec v1.3 C4; formulas and defaults in C6). Pure functions.

The GM submits these with evidence (request R9); the engine adds the technical-failure
flag it alone knows. The GM does no fairness monitoring (v1.3 C3).
"""
from statistics import mean, pstdev

WEIGHTS = {"completion": 0.35, "balance": 0.25, "exploration": 0.20, "record": 0.20}
SUCCESS_THRESHOLD = 0.7
LEGITIMATE_ENDINGS = {"winner", "all_dead", "deadline"}


def completion(ending, gm_stalled=False, recovery_cycles=0, cycles=1):
    """Provisional (C6): 1 for a legitimate ending without a GM-caused stall, minus the successor's recovery share."""
    if ending not in LEGITIMATE_ENDINGS or gm_stalled:
        return 0.0
    return max(0.0, 1 - recovery_cycles / max(1, cycles))


def balance(strengths):
    """1 - 2*spread of covered players' expected strength (0..1 values); None when not judged (< 2 covered)."""
    if len(strengths) < 2:
        return None
    return max(0.0, 1 - 2 * pstdev(strengths))


def exploration(role_flags, mission_flags=()):
    """Share of least-played job assignments, averaged with the share of first-time missions when given."""
    parts = [mean(flags) for flags in (list(role_flags), list(mission_flags)) if flags]
    return mean(parts) if parts else 0.0


def record_completeness(decisions):
    """Share of decisions with 2+ alternatives, a selected alternative among them, and a reason."""
    def complete(decision):
        ids = {alt.get("id") for alt in decision.get("alternatives", [])}
        return len(ids) >= 2 and decision.get("selected_alternative") in ids and bool(str(decision.get("reason", "")).strip())
    return mean(complete(d) for d in decisions) if decisions else 0.0


def gm_score(metrics, technical_failure=False):
    """Weighted score; a None metric (not judged) is dropped and the remaining weights rescaled to 1."""
    used = {key: weight for key, weight in WEIGHTS.items() if metrics.get(key) is not None}
    total = sum(used.values())
    score = sum(metrics[key] * weight for key, weight in used.items()) / total
    return {"score": score, "weights": {key: weight / total for key, weight in used.items()},
            "success": score >= SUCCESS_THRESHOLD and not technical_failure,
            "technical_failure": technical_failure}
