"""Temporary A adapter. Rules, rolls, visibility and official state live here.

This is a replaceable mock, not an extension of the production single-player app.
"""
import copy
import json
import random
from contextlib import contextmanager
from pathlib import Path

from .contracts import ALLOWED_ACTIONS, PHASES, ActionRequest, ContractError, Control
from .store import Store


def _tuples(value):
    return tuple(_tuples(item) for item in value) if isinstance(value, list) else value


class MockEngine:
    def __init__(self, store: Store, catalog=None):
        self.store = store
        self._catalog = catalog or json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))

    def catalog(self):
        return copy.deepcopy(self._catalog)

    def _load(self, db, game_id):
        state = self.store.get(db, "engine", game_id)
        if state is None:
            raise ContractError("unknown game")
        return state

    @contextmanager
    def _change(self, game_id):
        with self.store.transaction() as db:
            state = self._load(db, game_id)
            rng = random.Random()
            rng.setstate(_tuples(state["rng_state"]))
            yield db, state, rng
            state["rng_state"] = rng.getstate()
            self.store.put(db, "engine", game_id, state)

    def _event(self, db, state, kind, **payload):
        state["sequence"] += 1
        event = {
            "schema_version": self._catalog["schema_version"], "game_id": state["game_id"],
            "event_id": f"{state['game_id']}:{state['sequence']}", "sequence": state["sequence"],
            "cycle_id": state["cycle_id"], "phase": state["phase"], "phase_epoch": state["phase_epoch"],
            "type": kind, "payload": payload,
        }
        db.execute("INSERT INTO events VALUES (?,?,?)", (state["game_id"], state["sequence"], json.dumps(event, ensure_ascii=False)))
        return event

    @staticmethod
    def _control(state, control):
        if state["control"] != {"controller_id": control.controller_id, "term": control.term}:
            raise ContractError("stale or unauthorized GM control")

    @staticmethod
    def _open(state):
        if state["ended"] or state["resolution"]:
            raise ContractError("game is ended or awaiting GM resolution")

    def create_game(self, game_id, assignments, seed, rules=None):
        if not isinstance(game_id, str) or not game_id or not assignments:
            raise ContractError("game_id and at least one player required")
        if type(seed) is not int:
            raise ContractError("seed must be an integer")
        policy = copy.deepcopy(self._catalog["rules"])
        if rules:
            if set(rules) - set(policy):
                raise ContractError("unknown rule option")
            editable = {"npc_random_movement", "corpse_decay_per_cycle"}
            if any(key not in editable and value != policy[key] for key, value in rules.items()):
                raise ContractError("this mock cannot override a fixed phase or game rule")
            policy.update(rules)
        if type(policy["npc_random_movement"]) is not bool:
            raise ContractError("npc_random_movement must be boolean")
        if policy["actions_per_phase"] != 1 or policy["phase_order"] != list(PHASES):
            raise ContractError("mock supports the three-phase, one-action contract")
        if type(policy["corpse_decay_per_cycle"]) is not int or policy["corpse_decay_per_cycle"] < 0:
            raise ContractError("corpse decay must be a nonnegative integer")
        rng = random.Random(seed)
        actors = {}
        for agent_id, assignment in assignments.items():
            if not isinstance(agent_id, str) or not agent_id or agent_id.startswith("npc:"):
                raise ContractError("invalid or reserved agent_id")
            for key, collection in (("job", "jobs"), ("personality", "personalities"), ("personal_goal", "personal_goals")):
                if assignment.get(key) not in self._catalog[collection]:
                    raise ContractError(f"assignment {key} not supplied by engine catalog")
            if assignment["stat"] not in assignment["job"]["stat_options"]:
                raise ContractError("stat not allowed by job")
            actors[agent_id] = {
                "id": agent_id, "origin": "player", "mode": "active", "room": 0,
                **copy.deepcopy(assignment), "information_ids": [], "prepared": False,
            }
        truth = {key: rng.randrange(len(self._catalog[collection])) for key, collection in
                 (("suspect", "suspects"), ("weapon", "weapons"), ("place", "places"))}
        clues = {}
        for category, collection in (("suspect", "suspects"), ("weapon", "weapons"), ("place", "places")):
            for item in self._catalog[collection]:
                if item["id"] == truth[category]:
                    continue
                clue_id = f"{category}:{item['id']}"
                clues[clue_id] = {
                    "id": clue_id, "category": category, "excluded_id": item["id"],
                    "text": f"기록과 물증을 대조한 결과 {item['name']}은(는) 사건의 {category} 후보에서 제외된다.",
                    "source_room": (len(clues) * 3 + 2) % len(self._catalog["places"]),
                }
        for suspect in self._catalog["suspects"]:
            ident = f"npc:{suspect['id']}"
            actors[ident] = {
                "id": ident, "origin": "npc", "mode": "npc", "room": suspect["initial_room"],
                "name": suspect["name"], "job": copy.deepcopy(self._catalog["jobs"][suspect["job_id"]]),
                "stat": self._catalog["jobs"][suspect["job_id"]]["default_stat"],
                "personality": copy.deepcopy(rng.choice(self._catalog["personalities"])),
                "personal_goal": None, "prepared": False,
                "information_ids": [key for key, clue in clues.items() if clue["source_room"] == suspect["initial_room"]],
            }
        state = {
            "game_id": game_id, "seed": seed, "cycle_id": 1, "phase": PHASES[0], "phase_epoch": 1,
            "control": {"controller_id": "gm-primary", "term": 1}, "actors": actors,
            "truth": truth, "clues": clues, "observed_ids": [], "shared_ids": [],
            "spent": {}, "receipts": {}, "share_receipts": {}, "sequence": 0,
            "ended": False, "end_reason": None, "resolution": None, "last_removal_reason": None,
            "rules": policy, "rng_state": rng.getstate(),
        }
        with self.store.transaction() as db:
            if self.store.get(db, "engine", game_id) is not None:
                raise ContractError("game already exists")
            self._event(db, state, "game_started", agent_ids=list(assignments), control=state["control"])
            self.store.put(db, "engine", game_id, state)

    def _state(self, game_id):
        state = self.store.read("engine", game_id)
        if state is None:
            raise ContractError("unknown game")
        return state

    @staticmethod
    def _presence(actor):
        return {"id": actor["id"], "name": actor.get("name", actor["id"]), "mode": actor["mode"],
                "room": actor["room"], "job": {"id": actor["job"]["id"], "name": actor["job"]["name"]}}

    def _information(self, state, ids):
        # No truth/source-room directory is sent through agent or GM projections.
        return [{key: clue[key] for key in ("id", "category", "excluded_id", "text")}
                for ident in ids for clue in (state["clues"][ident],)]

    def gm_snapshot(self, game_id):
        state = self._state(game_id)
        return {
            "game_id": game_id, "cycle_id": state["cycle_id"], "phase": state["phase"],
            "phase_epoch": state["phase_epoch"], "control": state["control"],
            "event_sequence": state["sequence"],
            "active_agents": [key for key, actor in state["actors"].items() if actor["mode"] == "active"],
            "actors": [self._presence(actor) for actor in state["actors"].values()],
            "inventories": {key: list(actor["information_ids"]) for key, actor in state["actors"].items() if actor["origin"] == "player"},
            "observed_information": self._information(state, state["observed_ids"]),
            "shared_information": self._information(state, state["shared_ids"]),
            "spent": state["spent"], "ended": state["ended"], "end_reason": state["end_reason"],
            "resolution": state["resolution"], "last_removal_reason": state["last_removal_reason"],
        }

    def agent_snapshot(self, game_id, agent_id):
        state = self._state(game_id)
        actor = state["actors"].get(agent_id)
        if actor is None or actor["origin"] != "player":
            raise ContractError("unknown player")
        own = {key: copy.deepcopy(actor[key]) for key in ("id", "mode", "room", "job", "stat", "personality", "personal_goal", "prepared")}
        return {
            "schema_version": self._catalog["schema_version"], "game_id": game_id,
            "cycle_id": state["cycle_id"], "phase": state["phase"], "phase_epoch": state["phase_epoch"],
            "self": own, "information": self._information(state, actor["information_ids"]),
            "shared_information": self._information(state, state["shared_ids"]),
            "occupants": [self._presence(other) for other in state["actors"].values() if other["room"] == actor["room"] and other["id"] != agent_id],
            "candidates": {category: copy.deepcopy(self._catalog[collection]) for category, collection in
                           (("suspect", "suspects"), ("weapon", "weapons"), ("place", "places"))},
            "ended": state["ended"],
        }

    def events_after(self, game_id, sequence):
        with self.store.lock:
            return [json.loads(row[0]) for row in self.store.db.execute(
                "SELECT body FROM events WHERE game_id=? AND sequence>? ORDER BY sequence", (game_id, sequence))]

    def _roll(self, state, actor, action, approach, rng):
        if approach not in ("compassion", "brutality"):
            raise ContractError("unknown approach")
        expert = any(action in skill["scopes"] or f"{action}@{actor['room']}" in skill["scopes"] for skill in actor["job"]["skills"])
        count = state["rules"]["base_dice"] + (state["rules"]["prepared_bonus_dice"] if actor["prepared"] else 0) + int(expert)
        values = [rng.randint(1, 6) for _ in range(count)]
        successes = sum(value < actor["stat"] if approach == "brutality" else value > actor["stat"] for value in values)
        actor["prepared"] = successes >= 3
        return {"dice": values, "successes": successes, "approach": approach, "stat": actor["stat"], "expert": expert,
                "complication": "temporary_time_cost" if successes == 1 else None}

    def submit_action(self, request: ActionRequest):
        if not isinstance(request, ActionRequest):
            raise ContractError("ActionRequest required")
        with self._change(request.game_id) as (db, state, rng):
            wire = request.to_dict()
            previous = state["receipts"].get(request.action_id)
            if previous:
                if previous["request"] != wire:
                    raise ContractError("action_id reused with different request")
                return copy.deepcopy(previous["result"])
            self._open(state)
            if (request.cycle_id, request.phase, request.phase_epoch) != (state["cycle_id"], state["phase"], state["phase_epoch"]):
                raise ContractError("stale phase token")
            actor = state["actors"].get(request.agent_id)
            if actor is None or actor["mode"] != "active":
                raise ContractError("actor is not an active player")
            if request.agent_id in state["spent"]:
                raise ContractError("phase action already consumed")
            payload = request.payload
            info_ids, roll = [], None
            outcome = "succeeded"
            target_id = None
            fields = {
                "move": {"room"}, "stay": set(), "pass": set(), "prepare": set(),
                "explore": {"approach"}, "question": {"target_id", "approach"},
                "examine_corpse": {"target_id", "approach"}, "accuse": {"suspect", "weapon", "place"},
            }
            if set(payload) - fields[request.action]:
                raise ContractError("unsupported action payload field")
            if request.action == "move":
                room = payload.get("room")
                if type(room) is not int or not 0 <= room < len(self._catalog["places"]):
                    raise ContractError("invalid room")
                actor["room"] = room
            elif request.action in ("stay", "pass"):
                outcome = "skipped"
            elif request.action == "prepare":
                actor["prepared"] = True
            elif request.action == "accuse":
                if set(payload) != {"suspect", "weapon", "place"} or any(
                    type(payload[key]) is not int or not 0 <= payload[key] < len(self._catalog[collection])
                    for key, collection in (("suspect", "suspects"), ("weapon", "weapons"), ("place", "places"))):
                    raise ContractError("complete valid accusation required")
                if payload == state["truth"]:
                    state["resolution"] = "solved"
                else:
                    outcome = "failed"
                    actor["mode"] = "corpse"
                    actor["death_cycle"] = state["cycle_id"]
                    state["last_removal_reason"] = "deduction_failure"
                    self._event(db, state, "actor_died", actor_id=request.agent_id, reason="deduction_failure",
                                remaining_information_ids=list(actor["information_ids"]))
            else:
                if request.action != "explore":
                    target_id = payload.get("target_id")
                    target = state["actors"].get(target_id) if isinstance(target_id, str) else None
                    if target is None or target_id == request.agent_id or target["room"] != actor["room"]:
                        raise ContractError("target must be another actor in the same room")
                    if (request.action == "examine_corpse") != (target["mode"] == "corpse"):
                        raise ContractError("target kind does not match interaction")
                    pool = list(target["information_ids"])
                else:
                    pool = [key for key, clue in state["clues"].items() if clue["source_room"] == actor["room"]]
                roll = self._roll(state, actor, request.action, payload.get("approach", "compassion"), rng)
                successes = roll["successes"]
                amount = 2 if successes >= 3 else int(successes > 0)
                if request.action == "examine_corpse" and successes >= 3:
                    amount = len(pool)  # Temporary full-recovery rule for a strong roll.
                if request.action == "question":
                    cap = target["personality"]["disclosure_cap"] + target["job"]["disclosure_bonus"]
                    if target["personality"]["id"] == "guarded" and successes < 2:
                        cap = 0
                    amount = min(amount, cap)
                unknown = [key for key in pool if key not in actor["information_ids"]]
                rng.shuffle(unknown)
                info_ids = unknown[:amount]
                actor["information_ids"].extend(info_ids)
                for ident in info_ids:
                    if ident not in state["observed_ids"]:
                        state["observed_ids"].append(ident)
                outcome = "succeeded" if successes else "failed"
            state["spent"][request.agent_id] = request.action_id
            result = {
                "schema_version": self._catalog["schema_version"], "game_id": request.game_id,
                "agent_id": request.agent_id, "action_id": request.action_id, "task_id": request.task_id,
                "cycle_id": request.cycle_id, "phase": request.phase, "phase_epoch": request.phase_epoch,
                "execution_status": "completed", "outcome": outcome, "consumed": True,
                "action": request.action, "room": actor["room"], "actor_mode": actor["mode"],
                "target_id": target_id, "roll": roll, "acquired_information": self._information(state, info_ids),
                "held_information_ids": list(actor["information_ids"]), "resolution": state["resolution"],
            }
            state["receipts"][request.action_id] = {"request": wire, "result": copy.deepcopy(result)}
            self._event(db, state, "action_completed", result=result, visibility="gm_supervisory")
            return result

    def share_information(self, game_id, agent_id, information_ids, message_id):
        """Communication side channel: does not spend or replenish a game action."""
        if not isinstance(information_ids, list) or any(not isinstance(item, str) for item in information_ids):
            raise ContractError("information_ids must be strings")
        if not isinstance(message_id, str) or not message_id:
            raise ContractError("message_id required")
        with self._change(game_id) as (db, state, rng):
            wire = {"agent_id": agent_id, "information_ids": information_ids}
            previous = state["share_receipts"].get(message_id)
            if previous:
                if previous["request"] != wire:
                    raise ContractError("message_id reused")
                return previous["result"]
            self._open(state)
            actor = state["actors"].get(agent_id)
            if actor is None or actor["mode"] != "active" or any(item not in actor["information_ids"] for item in information_ids):
                raise ContractError("only an active owner may share held information")
            for ident in information_ids:
                if ident not in state["shared_ids"]:
                    state["shared_ids"].append(ident)
            result = {"message_id": message_id, "agent_id": agent_id, "information": self._information(state, information_ids)}
            state["share_receipts"][message_id] = {"request": wire, "result": result}
            self._event(db, state, "information_shared", **result)
            return result

    def advance_phase(self, game_id, control, phase_epoch):
        with self._change(game_id) as (db, state, rng):
            self._control(state, control)
            self._open(state)
            if phase_epoch != state["phase_epoch"]:
                raise ContractError("stale phase advance")
            active = [key for key, actor in state["actors"].items() if actor["mode"] == "active"]
            if not active or any(key not in state["spent"] for key in active):
                raise ContractError("phase barrier not satisfied")
            index = PHASES.index(state["phase"])
            state["phase"] = PHASES[(index + 1) % len(PHASES)]
            state["phase_epoch"] += 1
            state["spent"] = {}
            if index == len(PHASES) - 1:
                state["cycle_id"] += 1
                for actor in state["actors"].values():
                    if actor["mode"] == "corpse":
                        removed = []
                        for _ in range(min(state["rules"]["corpse_decay_per_cycle"], len(actor["information_ids"]))):
                            ident = rng.choice(actor["information_ids"])
                            actor["information_ids"].remove(ident)
                            removed.append(ident)
                        if removed:
                            self._event(db, state, "information_decayed", actor_id=actor["id"], removed_information_ids=removed,
                                        remaining_information_ids=list(actor["information_ids"]))
                if state["rules"]["npc_random_movement"]:
                    for actor in state["actors"].values():
                        if actor["mode"] == "npc":
                            actor["room"] = rng.randrange(len(self._catalog["places"]))
                    self._event(db, state, "npc_moved", positions=[self._presence(a) for a in state["actors"].values() if a["mode"] == "npc"])
            self._event(db, state, "phase_started", active_agents=active)

    def convert_fault(self, game_id, agent_id, reason, control):
        if not isinstance(reason, str) or not reason:
            raise ContractError("explicit fault reason required")
        with self._change(game_id) as (db, state, rng):
            self._control(state, control)
            self._open(state)
            actor = state["actors"].get(agent_id)
            if actor is None or actor["mode"] != "active":
                raise ContractError("only active players can become fault NPCs")
            actor["mode"] = "npc"
            state["last_removal_reason"] = "technical_failure"
            self._event(db, state, "actor_became_npc", actor_id=agent_id, reason=reason,
                        retained_information_ids=list(actor["information_ids"]))

    def take_over(self, game_id, agent_id, previous_control):
        with self._change(game_id) as (db, state, rng):
            self._control(state, previous_control)
            if state["ended"]:
                raise ContractError("ended game cannot elect a GM")
            actor = state["actors"].get(agent_id)
            if actor is None or actor["mode"] != "active":
                raise ContractError("successor must be an active player")
            actor["mode"] = "npc"
            state["last_removal_reason"] = "technical_failure"
            control = Control(agent_id, previous_control.term + 1)
            state["control"] = {"controller_id": control.controller_id, "term": control.term}
            self._event(db, state, "gm_replaced", previous_control=vars(previous_control), control=vars(control),
                        actor_id=agent_id, retained_information_ids=list(actor["information_ids"]))
            return control

    def finish(self, game_id, reason, control):
        with self._change(game_id) as (db, state, rng):
            self._control(state, control)
            if state["ended"]:
                if state["end_reason"] != reason:
                    raise ContractError("termination reason cannot be changed")
                return
            if reason == "solved":
                if state["resolution"] != "solved":
                    raise ContractError("engine has not confirmed a solution")
            elif reason in ("technical_failure", "deduction_failure"):
                if any(actor["mode"] == "active" for actor in state["actors"].values()):
                    raise ContractError("active players still remain")
                if reason != state["last_removal_reason"]:
                    raise ContractError("termination cause must match final removal")
            else:
                raise ContractError("unknown termination reason")
            state["ended"] = True
            state["end_reason"] = reason
            self._event(db, state, "game_ended", reason=reason, control=state["control"])
