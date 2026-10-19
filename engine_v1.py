"""C-owned GM role for A's external integration API v1 (`/integration/v1/`).

The engine owns pool identity, GM selection/succession, official rules and
results. This module only submits the GM role policy: which pool members to
activate, job/personality/goal, tasks, dependencies, alternatives and the
reason for the selected alternative. Players act on the engine directly.

No team repository import: the HTTP client is dependency-free. A successor GM
rebuilds its context from the engine's GM observation and events only; it
never reads the promoted agent's player memory or the previous GM's ledger.
"""
import json
import random
import urllib.error
import urllib.request

from .contracts import ContractError

ENGINE_VERSION = "engine-integration-v1"
NAMESPACE = "gm-v1"
ROOMS = 8


class EngineV1Error(ContractError):
    def __init__(self, status, body):
        self.status, self.body = status, body if isinstance(body, dict) else {}
        self.code = self.body.get("code", "http_error")
        super().__init__(f"{status} {self.code}: {self.body.get('error', '')}")


class AuthorityLost(ContractError):
    """The engine fenced this GM term; only a new session may continue."""


class EngineV1Client:
    def __init__(self, base_url="http://127.0.0.1:8765", credentials=None, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.credentials = dict(credentials or {})
        self.timeout = timeout

    def bind(self, **credentials):
        """Separate operator, pool identity and game-role credentials."""
        return EngineV1Client(self.base_url, credentials, self.timeout)

    def call(self, route, **data):
        body = json.dumps(self.credentials | data, ensure_ascii=False, allow_nan=False).encode()
        request = urllib.request.Request(f"{self.base_url}/integration/v1/{route}", body,
                                         {"Content-Type": "application/json", "X-GM-Client": "local"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    payload = json.load(exc)
                except ValueError:
                    payload = {}
            raise EngineV1Error(exc.code, payload) from exc


def _estimate(card, tag):
    return card.get("competencies", {}).get(tag, {}).get("estimate", 0.5)


class V1Coordinator:
    """One GM term. Constructed only from engine-issued GM credentials."""

    def __init__(self, client, store, context_key, target_players=2):
        creds = client.credentials
        if not all(key in creds for key in ("game_id", "controller_id", "term", "controller_token")):
            raise ContractError("engine-issued GM credentials required")
        if type(target_players) is not int or target_players < 1:
            raise ContractError("target_players must be a positive integer")
        self.client, self.store = client, store
        self.game_id, self.controller_id, self.term = creds["game_id"], creds["controller_id"], creds["term"]
        self.context_key, self.target_players = context_key, target_players
        self.key = f"{self.game_id}:{context_key}"
        self.catalog = client.call("catalog")

    def _call(self, route, **data):
        try:
            return self.client.call(route, **data)
        except EngineV1Error as exc:
            if exc.code == "stale_controller":
                raise AuthorityLost(f"GM term {self.term} fenced by the engine") from exc
            raise

    # ---------- observation ----------

    def _ledger(self):
        return self.store.read(NAMESPACE, self.key) or {
            "game_id": self.game_id, "controller_id": self.controller_id, "term": self.term,
            "context_key": self.context_key, "event_cursor": 0, "rooms": {},
            "acquired_by_cycle": {}, "faults": [], "successions": [], "decisions": [],
            "human_requests": []}

    def _apply(self, ledger, event):
        payload, kind = event["payload"], event["type"]
        if kind == "action_completed":
            result = payload["result"]
            new = result.get("acquired_information_ids", [])
            cycle = str(result["cycle_id"])
            ledger["acquired_by_cycle"][cycle] = ledger["acquired_by_cycle"].get(cycle, 0) + len(new)
            if result.get("task_id") and result["outcome"] not in ("succeeded", "solved"):
                key = f"{result['task_id']}:{result.get('generation')}"
                ledger.setdefault("task_misses", {})[key] = ledger.setdefault("task_misses", {}).get(key, 0) + 1
            if result["action"] == "explore":
                room = ledger["rooms"].setdefault(str(result["room"]), {"attempts": 0, "successes": 0,
                                                                     "new_information": 0, "exhausted": False})
                room["attempts"] += 1
                if result.get("roll") and result["roll"]["successes"]:
                    room["successes"] += 1
                    room["new_information"] += len(new)
                    # A successful roll that yields nothing means no unknown clue is left there.
                    room["exhausted"] = room["exhausted"] or not new
        elif kind == "question_completed" and payload["status"] == "refused":
            pair = f"{payload['asker_id']}>{payload['target_id']}"
            ledger.setdefault("refusals", {})[pair] = ledger.setdefault("refusals", {}).get(pair, 0) + 1
        elif kind == "player_fault":
            ledger["faults"].append({"event_id": event["event_id"], "agent_id": payload["agent_id"],
                                     "reason": payload["reason"]})
        elif kind in ("gm_retired", "gm_succeeded"):
            ledger["successions"].append({"event_id": event["event_id"], "type": kind, **payload})
        elif kind == "human_intervention_required":
            ledger["human_requests"].append({"event_id": event["event_id"], **payload})

    def sync(self):
        ledger = self._ledger()
        for _ in range(5):
            batch = self._call("events", after=ledger["event_cursor"])
            for event in batch["events"]:
                if event["sequence"] > ledger["event_cursor"]:
                    self._apply(ledger, event)
            ledger["event_cursor"] = batch["event_sequence"]
            observation = self._call("gm/observe")
            if observation["event_sequence"] <= ledger["event_cursor"]:
                break
        else:
            raise ContractError("engine events kept moving; retry synchronization")
        control = observation["control"]
        if control != {"controller_id": self.controller_id, "term": self.term}:
            raise AuthorityLost("observation belongs to another GM term")
        with self.store.transaction() as db:
            self.store.put(db, NAMESPACE, self.key, ledger)
        return observation, ledger

    # ---------- planning ----------

    def _open_rooms(self, ledger):
        return [room for room in range(ROOMS) if not ledger["rooms"].get(str(room), {}).get("exhausted")]

    def _activation(self, observation, ledger, count):
        """Select reserves by official competency, then diversify roles."""
        cards = [card for card in observation.get("pool", [])
                 if card["pool_status"] == "available" and card["agent_id"] != self.controller_id
                 and not any(actor["id"] == card["agent_id"] for actor in observation["actors"])]
        cards.sort(key=lambda card: (-_estimate(card, "player"), card["agent_id"]))
        used_jobs = {actor["job"]["id"] for actor in observation["actors"] if actor["mode"] == "active"}
        used_traits = [actor["personality"] for actor in observation["actors"] if actor["mode"] == "active"]
        jobs, traits, goals = self.catalog["jobs"], self.catalog["personalities"], self.catalog["personal_goals"]
        activation = []
        for index, card in enumerate(cards[:count]):
            free = [job["id"] for job in jobs if job["id"] not in used_jobs] or [job["id"] for job in jobs]
            job = max(free, key=lambda jid: (_estimate(card, f"job:{jid}"), -jid))
            trait = min(traits, key=lambda name: (used_traits.count(name), -_estimate(card, f"personality:{name}"),
                                                  traits.index(name)))
            used_jobs.add(job)
            used_traits.append(trait)
            activation.append({"agent_id": card["agent_id"], "job_id": job, "personality": trait,
                               "personal_goal": goals[(len(ledger["decisions"]) + index) % len(goals)]})
        return activation

    @staticmethod
    def _gains(observation, active, refusals):
        """Clue IDs each player lacks and another active player holds (GM sees IDs only).

        Shared clues are visible to every player already, so they are never a gain.

        A holder that refused the same asker twice is treated as unavailable: the
        GM cannot force disclosure, so it plans around the refusal instead.
        """
        shared = {item["id"] for item in observation.get("shared_information", [])}
        held = {actor["id"]: set(actor["held_information_ids"]) for actor in observation["actors"]}
        return {(asker, holder): 0 if refusals.get(f"{asker}>{holder}", 0) >= 2 else len(held[holder] - held[asker] - shared)
                for asker in active for holder in active if asker != holder and holder in held and asker in held}

    def _alternatives(self, observation, ledger, active):
        open_rooms = self._open_rooms(ledger)
        gains = self._gains(observation, active, ledger.get("refusals", {}))
        complementary = sum(max((g for (a, _), g in gains.items() if a == asker), default=0) for asker in active)
        last_cycle = str(observation["cycle_id"] - 1)
        stalled = observation["cycle_id"] > 1 and ledger["acquired_by_cycle"].get(last_cycle, 0) == 0
        parallel = min(len(open_rooms), len(active))
        scores = {"parallel_explore": parallel * (0.5 if stalled else 1.0),
                  "exchange_questions": complementary * (1.0 if stalled or not open_rooms else 0.25),
                  "deduce_only": 0.1}
        alternatives = [
            {"id": "parallel_explore", "summary": "미탐색 공간을 플레이어별로 나눠 병렬 조사",
             "reason": f"open_rooms={len(open_rooms)}, active={len(active)}, stalled={stalled}, score={scores['parallel_explore']:.2f}"},
            {"id": "exchange_questions", "summary": "상대가 가진 미보유 단서를 같은 공간 질문으로 요청",
             "reason": f"complementary_clues={complementary}, stalled={stalled}, score={scores['exchange_questions']:.2f}"},
            {"id": "deduce_only", "summary": "새 작업 없이 보유·공개 정보로 추리",
             "reason": f"open_rooms={len(open_rooms)}, score={scores['deduce_only']:.2f}"},
        ]
        selected = max(scores, key=lambda key: (scores[key], key == "parallel_explore"))
        return alternatives, selected, scores, open_rooms, gains

    def _desired(self, observation, selected, active, open_rooms, gains, current):
        """(kind, room) per player for the selected alternative; None means no useful task."""
        rooms = {actor["id"]: actor["room"] for actor in observation["actors"]}
        desired, claimed = {}, set()

        def explore_room(aid):
            keep = current.get(aid)
            if keep and keep["kind"] == "explore" and keep["room"] in open_rooms and keep["room"] not in claimed:
                return keep["room"]
            return next((r for r in open_rooms if r not in claimed), open_rooms[0] if open_rooms else None)

        if selected == "exchange_questions":
            planned = {}
            order = sorted(active, key=lambda a: (-max((g for (x, _), g in gains.items() if x == a), default=0), a))
            for asker in order:
                holder, gain = max(((h, g) for (x, h), g in gains.items() if x == asker),
                                   key=lambda item: (item[1], item[0]), default=(None, 0))
                if not gain:
                    continue
                meet = planned.setdefault(holder, planned.get(asker, rooms[holder]))
                planned[asker] = meet
                desired[asker] = ("question", meet)
            for holder, meet in planned.items():
                desired.setdefault(holder, ("explore", meet))  # Stay at the meeting room.
        for aid in active:
            if aid in desired or selected == "deduce_only":
                continue
            room = explore_room(aid)
            if room is not None:
                claimed.add(room)
                desired[aid] = ("explore", room)
        return desired

    def _task_changes(self, observation, ledger, selected, active, open_rooms, gains):
        tasks = {task["task_id"]: task for task in observation["tasks"]}
        current = {task["owner"]: task for task in sorted(tasks.values(), key=lambda t: t["task_id"])
                   if task["status"] == "assigned" and task["owner"] in active}
        orphaned = sorted((task for task in tasks.values() if task["status"] == "pending" and task["owner"] is None),
                          key=lambda task: task["task_id"])
        desired = self._desired(observation, selected, active, open_rooms, gains, current)
        misses = ledger.get("task_misses", {})
        changes, cycle = [], observation["cycle_id"]
        # Player failure: the GM, not the engine, decides the new owner of released tasks.
        for task in orphaned:
            owner = next((aid for aid in active if aid not in current), None) or min(
                active, key=lambda aid: (sum(t["owner"] == aid for t in tasks.values() if t["status"] == "assigned"), aid))
            kind, room = desired.get(owner, (task["kind"], task["room"]))
            changes.append({"task_id": task["task_id"], "agent_id": owner, "generation": task["generation"],
                            "kind": kind, "room": room, "dependencies": task["dependencies"]})
            current.setdefault(owner, {**task, "owner": owner, "kind": kind, "room": room, "generation": task["generation"] + 1})
        for aid in active:
            want = desired.get(aid)
            task = current.get(aid)
            if want is None:
                continue
            if task is None:
                changes.append({"task_id": f"c{cycle}-{aid}-{want[0]}", "agent_id": aid,
                                "kind": want[0], "room": want[1], "dependencies": []})
                continue
            stuck = misses.get(f"{task['task_id']}:{task['generation']}", 0) >= 3
            if stuck and (task["kind"], task["room"]) == want and want[0] == "question" and open_rooms:
                want = ("explore", open_rooms[0])  # Same target keeps failing; switch to exploration.
            if (task["kind"], task["room"]) != want or stuck:
                if any(c["task_id"] == task["task_id"] for c in changes):
                    continue
                # Retarget useless or repeatedly failing work instead of leaving it open forever.
                changes.append({"task_id": task["task_id"], "agent_id": aid, "generation": task["generation"],
                                "kind": want[0], "room": want[1], "dependencies": task["dependencies"]})
        return changes

    def _plan(self, observation, ledger, activation=(), trigger="cycle"):
        active = sorted(aid for aid in observation["active_agents"]) + [x["agent_id"] for x in activation]
        alternatives, selected, scores, open_rooms, gains = self._alternatives(observation, ledger, active)
        changes = self._task_changes(observation, ledger, selected, active, open_rooms, gains)
        reason = (f"{trigger}: {selected} 선택 (점수 {scores[selected]:.2f}); "
                  f"활성 {len(active)}명, 남은 조사 공간 {len(open_rooms)}곳, 작업 변경 {len(changes)}건")
        if activation:
            reason += f", 활성화 {[x['agent_id'] for x in activation]} (풀 역량 순)"
        command = {"operation": "PLAN", "map_version": observation["map_version"],
                   "activate": list(activation), "tasks": changes, "alternatives": alternatives,
                   "selected_alternative": selected, "reason": reason, "evidence_ids": []}
        command["command_id"] = f"{self.context_key}:plan:{observation['map_version']}"
        result = self._call("gm/command", **command)
        record = {"controller_id": self.controller_id, "term": self.term,
                  "map_version": result["map_version"], "cycle_id": observation["cycle_id"],
                  "phase": observation["phase"], "phase_epoch": observation["phase_epoch"], "trigger": trigger,
                  "selected_alternative": selected, "scores": scores, "reason": reason,
                  "activated": [x["agent_id"] for x in activation],
                  "task_changes": [{k: x[k] for k in ("task_id", "agent_id", "kind", "room")} for x in changes]}
        with self.store.transaction() as db:
            current = self.store.get(db, NAMESPACE, self.key) or ledger
            current["decisions"].append(record)
            current["last_plan_epoch"] = observation["phase_epoch"]
            self.store.put(db, NAMESPACE, self.key, current)
        return record

    # ---------- control loop ----------

    def tick(self):
        """One GM step. Returns a status string; never performs player actions."""
        self._call("gm/heartbeat")
        observation, ledger = self.sync()
        status = observation["status"]
        if observation["ended"]:
            return "ended"
        if observation["resolution"] == "solved":
            self._call("gm/command", operation="FINISH", command_id=f"{self.context_key}:finish")
            return "ended"
        if status in ("paused", "waiting_for_human"):
            return status
        active = observation["active_agents"]
        if status == "awaiting_setup":
            activation = self._activation(observation, ledger, max(0, self.target_players - len(active)))
            if not activation:
                return "waiting_for_players"  # The engine does not recruit; an operator must add pool members.
            self._plan(observation, ledger, activation, "setup")
            return "planned"
        orphaned = any(t["status"] == "pending" and t["owner"] is None for t in observation["tasks"])
        fresh_movement = observation["phase"] == "movement" and not observation["spent"]
        try:
            if fresh_movement and ledger.get("last_plan_epoch") != observation["phase_epoch"]:
                missing = max(0, self.target_players - len(active))
                activation = self._activation(observation, ledger, missing) if missing else []
                self._plan(observation, ledger, activation, "replace_fault" if activation else "cycle")
                return "planned"
            if orphaned:
                self._plan(observation, ledger, (), "reassign_fault")
                return "planned"
        except EngineV1Error as exc:
            if exc.code != "conflict":
                raise
            return "retry"  # Map or roster changed under the plan; re-observe next tick.
        if active and all(aid in observation["spent"] for aid in active) and \
                not any(q["status"] == "pending" for q in observation["questions"]):
            try:
                advanced = self._call("gm/command", operation="ADVANCE", phase_epoch=observation["phase_epoch"],
                                      command_id=f"{self.context_key}:advance:{observation['phase_epoch']}")
            except EngineV1Error as exc:
                if exc.code != "pending":
                    raise
                return "waiting"
            if advanced["phase"] == "movement":
                # Plan before players can spend the fresh movement phase.
                self.tick()
            return "advanced"
        return "waiting"

    def report(self):
        observation, ledger = self.sync()
        return {"game_id": self.game_id, "control": observation["control"], "status": observation["status"],
                "cycle_id": observation["cycle_id"], "phase": observation["phase"],
                "map_version": observation["map_version"], "metrics": observation["metrics"],
                "decisions": ledger["decisions"], "faults": ledger["faults"],
                "successions": ledger["successions"], "human_requests": ledger["human_requests"],
                "rooms": ledger["rooms"], "refusals": ledger.get("refusals", {})}


class GMWorker:
    """Persistent pool worker's GM side. Polls its own session for promotions."""

    def __init__(self, identity, store, target_players=2):
        if not all(key in identity.credentials for key in ("agent_id", "pool_token")):
            raise ContractError("pool identity client required")
        self.identity, self.store, self.target_players = identity, store, target_players
        self.coordinator = None

    def poll(self):
        session = self.identity.call("pool/session")
        if session["role"] != "gm":
            self.coordinator = None
            return None
        if self.coordinator is None or self.coordinator.context_key != session["context_key"]:
            # New term: fresh context from engine GM observation only, no player-side memory.
            role = self.identity.bind(**session["credentials"])
            self.coordinator = V1Coordinator(role, self.store, session["context_key"], self.target_players)
        return self.coordinator

    def step(self):
        coordinator = self.coordinator or self.poll()
        if coordinator is None:
            return "not_gm"
        try:
            return coordinator.tick()
        except AuthorityLost:
            self.coordinator = None
            return "fenced"
