"""GM performance score (spec v1.2 C4) and fairness monitoring (C3). Pure functions.

The GM submits these with evidence (request R9); the engine adds the technical-failure
flag it alone knows. Fairness findings go to the GM decision record only: the GM
never intervenes in play.
"""
from statistics import mean, pstdev

WEIGHTS = {"completion": 0.35, "balance": 0.25, "exploration": 0.20, "record": 0.20}
SUCCESS_THRESHOLD = 0.7
LEGITIMATE_ENDINGS = {"winner", "all_dead", "deadline"}
FAIRNESS_MIN_EVENTS, FAIRNESS_RATIO = 3, 2.0  # defaults


def completion(ending, gm_stalled=False, recovery_cycles=0, cycles=1):
    """1 for a legitimate ending without a GM-caused stall; a successor's recovery time counts against it."""
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


def fairness_report(questions, players):
    """questions: [{asker_id, target_id, approach, successes}] as the GM observes them (A11).

    Flags a player who received forced disclosures or threats at least FAIRNESS_RATIO times the
    mean of the other players and at least FAIRNESS_MIN_EVENTS times.
    """
    tally = {aid: {"forced": 0, "threats": 0, "received": 0} for aid in players}
    for q in questions:
        if q["target_id"] not in tally:
            continue
        row = tally[q["target_id"]]
        row["received"] += 1
        row["forced"] += q["successes"] >= 1
        row["threats"] += q["approach"] == "threaten"
    flagged = []
    for key in ("forced", "threats"):
        for aid, row in tally.items():
            others = [r[key] for b, r in tally.items() if b != aid]
            baseline = mean(others) if others else 0
            if row[key] >= FAIRNESS_MIN_EVENTS and row[key] >= FAIRNESS_RATIO * max(baseline, 1e-9):
                flagged.append({"agent_id": aid, "metric": key, "count": row[key], "others_mean": baseline})
    reason = ("쏠림 없음" if not flagged else
              "; ".join(f"{f['agent_id']} {f['metric']} {f['count']}회 (다른 플레이어 평균 {f['others_mean']:.1f})" for f in flagged))
    return {"per_player": tally, "flagged": flagged, "reason": reason, "intervention": None}
