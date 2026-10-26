"""GM Role assignment (spec v1.3 C1; defaults in C6). Pure functions; no engine, no LLM.

Character i holds job i, so choosing a job for an agent chooses its character.
`history` is the engine's participation record (request R6):
{agent_id: [{"game_id", "job_id", "final_score", "participation_index"}]}.
A successor GM gets the same history from the engine, so it computes the same values.
"""
import json
import random
from pathlib import Path
from statistics import mean, pstdev

# Single source shared with the engine check (settings.verify_engine).
_SETTINGS = json.loads(Path(__file__).with_name("competitive_settings.json").read_text(encoding="utf-8"))
JOBS = _SETTINGS["role_policy"]["jobs"]
MIN_PLAYERS, MAX_PLAYERS = _SETTINGS["engine_rules"]["min_players"], _SETTINGS["engine_rules"]["max_players"]
MATURITY_K = _SETTINGS["role_policy"]["maturity_k"]  # w = (n / (n + K))^2 (default)
# A9 + A13: death -20 .. 1st 100 + all missions 60
SCORE_MIN, SCORE_SPAN = _SETTINGS["role_policy"]["score_min"], _SETTINGS["role_policy"]["score_span"]


def maturity_weight(n, k=MATURITY_K):
    """Weight of an agent's n-th game: early games, played with little understanding, barely count."""
    return (n / (n + k)) ** 2


def normalized_score(score):
    return min(1.0, max(0.0, (score - SCORE_MIN) / SCORE_SPAN))


def play_counts(records):
    counts = [0] * JOBS
    for record in records:
        counts[record["job_id"]] += 1
    return counts


def covered(records):
    """Every job played at least once; until then the agent's competency is not judged."""
    return min(play_counts(records)) >= 1


def job_competency(records):
    """{job_id: weighted mean normalized score}, or None before coverage."""
    if not covered(records):
        return None
    totals = {}
    for record in records:
        w = maturity_weight(record["participation_index"])
        value, weight = totals.get(record["job_id"], (0.0, 0.0))
        totals[record["job_id"]] = (value + w * normalized_score(record["final_score"]), weight + w)
    return {job: value / weight for job, (value, weight) in totals.items()}


def player_count(agents, history, requested=None):
    """12 until every agent is covered (spread experience fast), then the experiment's choice (3..12)."""
    limit = min(MAX_PLAYERS, len(agents))
    if limit < MIN_PLAYERS:
        raise ValueError(f"at least {MIN_PLAYERS} player agents required")
    if not all(covered(history.get(aid, [])) for aid in agents):
        return limit
    count = limit if requested is None else requested
    if not MIN_PLAYERS <= count <= limit:
        raise ValueError(f"player count must be {MIN_PLAYERS}..{limit}")
    return count


def choose_participants(agents, history, count, rng):
    """The agents with the fewest games play; the rest (most played) sit out. Ties by seed."""
    order = sorted(agents, key=lambda aid: (len(history.get(aid, [])), rng.random()))
    return sorted(order[:count])


def _explore(participants, history, rng):
    """Maximize agents that get one of their least-played jobs (bipartite matching), then fill."""
    counts = {aid: play_counts(history.get(aid, [])) for aid in participants}
    edges = {aid: [j for j in range(JOBS) if counts[aid][j] == min(counts[aid])] for aid in participants}
    for jobs in edges.values():
        rng.shuffle(jobs)
    owner = {}

    def augment(aid, seen):
        for job in edges[aid]:
            if job not in seen:
                seen.add(job)
                if job not in owner or augment(owner[job], seen):
                    owner[job] = aid
                    return True
        return False

    order = list(participants)
    rng.shuffle(order)
    for aid in order:
        augment(aid, set())
    assignment = {aid: job for job, aid in owner.items()}
    for aid in order:
        if aid not in assignment:
            free = [j for j in range(JOBS) if j not in assignment.values()]
            assignment[aid] = min(free, key=lambda j: (counts[aid][j], rng.random()))
    return assignment


def _balance(start, competency):
    """Pairwise swaps (and moves to unused characters) among covered agents to minimize strength spread."""
    assignment = dict(start)
    movable = sorted(aid for aid in assignment if aid in competency)

    def spread(candidate):
        return pstdev([competency[aid][candidate[aid]] for aid in movable])

    improved = True
    while improved:
        improved = False
        free = [j for j in range(JOBS) if j not in assignment.values()]
        best = spread(assignment)
        for i, a in enumerate(movable):
            options = [("swap", b) for b in movable[i + 1:]] + [("move", j) for j in free]
            for kind, other in options:
                trial = dict(assignment)
                if kind == "swap":
                    trial[a], trial[other] = assignment[other], assignment[a]
                else:
                    trial[a] = other
                if spread(trial) < best - 1e-12:
                    assignment, best, improved = trial, spread(trial), True
                    break
            if improved:
                break
    return assignment


def exploratory(aid, job, history):
    """The job is one of the agent's least-played jobs before this game."""
    counts = play_counts(history.get(aid, []))
    return counts[job] == min(counts)


def _summary(assignment, history, competency):
    strengths = [competency[aid][job] for aid, job in assignment.items() if aid in competency]
    flags = [exploratory(aid, job, history) for aid, job in assignment.items()]
    return {"exploratory_ratio": mean(flags),
            "covered_strengths": strengths,
            "strength_spread": pstdev(strengths) if len(strengths) >= 2 else None}


def plan_roles(agents, history, seed, requested=None):
    """Participants, characters, two compared alternatives and the reason (spec C1, D1)."""
    rng = random.Random(seed)
    count = player_count(agents, history, requested)
    participants = choose_participants(agents, history, count, rng)
    competency = {aid: c for aid in participants if (c := job_competency(history.get(aid, []))) is not None}
    explore = _explore(participants, history, rng)
    candidates = {"explore": explore}
    explore_info = _summary(explore, history, competency)
    alternatives = [{"id": "explore", "summary": "참가자마다 덜 플레이한 직업을 무작위로 배정",
                     "reason": f"탐색 비율 {explore_info['exploratory_ratio']:.2f}"}]
    if len(competency) >= 2:
        balance = _balance(explore, competency)
        candidates["balance"] = balance
        info = _summary(balance, history, competency)
        alternatives.append({"id": "balance", "summary": "커버리지를 채운 참가자의 직업 역량 편차를 최소화(나머지는 탐색 유지)",
                             "reason": f"역량 편차 {info['strength_spread']:.3f} (탐색안 {explore_info['strength_spread']:.3f}), "
                                       f"탐색 비율 {info['exploratory_ratio']:.2f}"})
        selected = "balance"
        reason = f"커버리지 충족 참가자 {len(competency)}명: 역량 편차를 줄이는 균형안 선택"
    else:
        alternatives.append({"id": "balance", "summary": "직업 역량 편차 최소화",
                             "reason": f"판정 불가: 커버리지 충족 참가자 {len(competency)}명 (2명 이상 필요)"})
        selected = "explore"
        reason = "커버리지 전이라 직업 역량을 판정하지 않음: 탐색안 선택" + (" (첫 게임 무작위)" if not any(history.values()) else "")
    chosen = candidates[selected]
    info = _summary(chosen, history, competency)
    return {"participants": participants, "player_count": count,
            "activation": [{"agent_id": aid, "character_id": chosen[aid]} for aid in participants],
            "alternatives": alternatives, "selected_alternative": selected, "reason": reason,
            "exploratory": {aid: exploratory(aid, chosen[aid], history) for aid in participants},
            "covered_agents": sorted(competency), **info}
