"""GM sub-mission choice and "GM 지정" targets (spec v1.2 C1, design doc 2.2-2.3, 2.7). Pure functions.

The engine draws three candidates per player per tier (request R5); the GM picks one
per tier, fills designated targets and quantity N, and records two compared alternatives.
The GM never sees the case truth, so no choice here can depend on it.
Relationship thresholds stay at the table values (default). The target's E/I is
recorded so analysis can use it as a covariate; adjust only after achievement rates.
"""
import json
import random
from pathlib import Path

DATA = Path(__file__).with_name("v12_data.json")
TIERS = ["하", "중", "상"]
# Design doc 2.7. Each entry: missions on one side conflict with missions on the other.
CONFLICTS = [({"O1"}, {"M45"}), ({"O3"}, {"M50"}), ({"M07"}, {"M08"}),
             ({"M04", "M06", "M11", "M49"}, {"M10", "M44", "M48"}),
             ({"M36"}, {"M37"}), ({"M14"}, {"M46"}), ({"O2", "M26"}, {"M43"})]
# Pairs whose designated target is the opposing player (2.7: "서로를 지정 대상으로").
MUTUAL = [("M07", "M08"), ("M14", "M46")]


def load_data(path=DATA):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def conflicts(a, b):
    return any((a in left and b in right) or (b in left and a in right) for left, right in CONFLICTS)


def _matches(token, character, mbti, data):
    if token in data["stat_tokens"]:
        return character["stats"][data["stat_tokens"][token]] == 5
    if token in data["jobs"]:
        return character["job"] == token
    return token in mbti


def effective_tier(mission, character, mbti, data, n=None):
    """Base tier shifted one step easier/harder; both or neither keep the base; clamped to 하..상."""
    base = mission["quantity"][str(n)] if mission.get("quantity") else mission["base"]
    index = TIERS.index(base)
    easier = any(_matches(t, character, mbti, data) for t in mission["easier"])
    harder = any(_matches(t, character, mbti, data) for t in mission["harder"])
    if easier != harder:
        index = max(0, index - 1) if easier else min(2, index + 1)
    return TIERS[index]


def quantity_for(mission, tier, character, mbti, data):
    """N whose effective tier equals the slot; prefer N whose base tier already equals it."""
    fits = [int(n) for n in mission["quantity"] if effective_tier(mission, character, mbti, data, int(n)) == tier]
    if not fits:
        return None
    return min(fits, key=lambda n: (mission["quantity"][str(n)] != tier, n))


def _feasible(mission, characters, players, indirect_clue_ids):
    kind = mission["designate"]
    if kind == "npc":
        return any(c["controller"] is None for c in characters)
    if kind == "player":
        return len(players) >= 2
    if kind == "indirect_clues":
        return len(indirect_clue_ids) >= mission["designate_count"]
    return True


def _pick(players, candidates, fits, received, prefer_conflict, rng):
    """{agent_id: {tier: mission_id}} by novelty (missions the agent has received least) and optionally conflicts."""
    chosen = {}
    order = list(players)
    rng.shuffle(order)
    for player in order:
        aid = player["agent_id"]
        chosen[aid] = {}
        for tier in reversed(TIERS):
            # A player never gets two missions that conflict with each other.
            options = [m for m in candidates[aid][tier] if fits(player, m, tier)
                       and not any(conflicts(m, mine) for mine in chosen[aid].values())]
            if not options:
                raise ValueError(f"no feasible {tier} mission for {aid}")

            def score(mission_id):
                cross = sum(conflicts(mission_id, other) for bid, picks in chosen.items() if bid != aid
                            for other in picks.values())
                return (-cross if prefer_conflict else 0, received.get(aid, []).count(mission_id), rng.random())
            chosen[aid][tier] = min(options, key=score)
    return chosen


def _cross_conflicts(chosen):
    pairs = []
    agents = sorted(chosen)
    for i, a in enumerate(agents):
        for b in agents[i + 1:]:
            pairs += [(a, ma, b, mb) for ma in chosen[a].values() for mb in chosen[b].values() if conflicts(ma, mb)]
    return pairs


def _targets(chosen, players, characters, catalog, indirect_clue_ids, places, rng):
    """Designated targets; mutual pairs point at each other, others are drawn by seed (never self)."""
    by_agent = {p["agent_id"]: p for p in players}
    mbti = {c["character_id"]: c["mbti"] for c in characters}
    targets = {}
    for a, ma, b, mb in _cross_conflicts(chosen):
        for x, y in MUTUAL:
            if {ma, mb} == {x, y}:
                targets.setdefault((a, ma), {"player": b, "character": by_agent[b]["character_id"]})
                targets.setdefault((b, mb), {"player": a, "character": by_agent[a]["character_id"]})
    for aid, picks in chosen.items():
        own = by_agent[aid]["character_id"]
        for mission_id in picks.values():
            kind = catalog[mission_id]["designate"]
            if kind is None or (aid, mission_id) in targets:
                continue
            if kind == "player":
                other = rng.choice(sorted(b for b in by_agent if b != aid))
                targets[(aid, mission_id)] = {"player": other, "character": by_agent[other]["character_id"]}
            elif kind == "npc":
                targets[(aid, mission_id)] = {"character": rng.choice(sorted(c["character_id"] for c in characters
                                                                          if c["controller"] is None))}
            elif kind == "character":
                targets[(aid, mission_id)] = {"character": rng.choice(sorted(c["character_id"] for c in characters
                                                                          if c["character_id"] != own))}
            elif kind == "indirect_clues":
                targets[(aid, mission_id)] = {"clue_ids": sorted(rng.sample(sorted(indirect_clue_ids),
                                                                            catalog[mission_id]["designate_count"]))}
            elif kind == "place":
                targets[(aid, mission_id)] = {"place": rng.randrange(len(places))}
    for target in targets.values():
        if "character" in target:
            target["target_ei"] = mbti[target["character"]][0]
    return targets


def plan_missions(players, characters, candidates, seed, received=None, indirect_clue_ids=(), data=None):
    """players: [{agent_id, character_id, mbti}]; characters: [{character_id, mbti, controller}];
    candidates: {agent_id: {tier: [mission_id x3]}}; received: {agent_id: [past mission_ids]}."""
    data = data or load_data()
    catalog = {m["id"]: m for m in data["missions"]}
    sheets = {c["id"]: c for c in data["characters"]}
    received, rng = received or {}, random.Random(seed)

    def fits(player, mission_id, tier):
        mission = catalog[mission_id]
        if not _feasible(mission, characters, players, indirect_clue_ids):
            return False
        return not mission.get("quantity") or quantity_for(
            mission, tier, sheets[player["character_id"]], player["mbti"], data) is not None

    novelty = _pick(players, candidates, fits, received, False, random.Random(seed))
    conflict = _pick(players, candidates, fits, received, True, random.Random(seed))

    def novel_ratio(chosen):
        picks = [(aid, m) for aid, tiers in chosen.items() for m in tiers.values()]
        return sum(m not in received.get(aid, []) for aid, m in picks) / len(picks)

    n_conf, c_conf = len(_cross_conflicts(novelty)), len(_cross_conflicts(conflict))
    alternatives = [
        {"id": "novelty", "summary": "에이전트가 덜 받아 본 미션 우선",
         "reason": f"신규 비율 {novel_ratio(novelty):.2f}, 플레이어 간 충돌 {n_conf}쌍"},
        {"id": "conflict", "summary": "설계서 2.7의 충돌 짝을 플레이어 사이에 배정",
         "reason": f"신규 비율 {novel_ratio(conflict):.2f}, 플레이어 간 충돌 {c_conf}쌍"}]
    selected = "conflict" if c_conf > n_conf else "novelty"
    chosen = conflict if selected == "conflict" else novelty
    reason = (f"충돌 {c_conf}쌍으로 상호작용을 만드는 충돌안 선택" if selected == "conflict"
              else "후보 안에서 충돌 짝을 만들 수 없어 신규성안 선택")
    targets = _targets(chosen, players, characters, catalog, indirect_clue_ids, data["places"], rng)
    assignments = {}
    for player in players:
        aid = player["agent_id"]
        sheet = sheets[player["character_id"]]
        entries = []
        for tier in TIERS:
            mission_id = chosen[aid][tier]
            params = {}
            if catalog[mission_id].get("quantity"):
                params["N"] = quantity_for(catalog[mission_id], tier, sheet, player["mbti"], data)
            entries.append({"mission_id": mission_id, "tier": tier,
                            "targets": targets.get((aid, mission_id), {}), "params": params})
        assignments[aid] = entries
    return {"sub_missions": assignments, "alternatives": alternatives, "selected_alternative": selected,
            "reason": reason, "cross_conflicts": [list(pair) for pair in _cross_conflicts(chosen)]}
