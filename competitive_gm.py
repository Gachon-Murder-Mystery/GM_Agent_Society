"""C-owned GM for A's competitive v1.3 engine (`mode=competitive`, ruleset competitive-v1.3).

The GM is a game master, not a task planner (spec v1.3 C). Players roll by
submitting their own actions to the engine (B: action choice + `agent/action`);
the engine rolls the dice and adjudicates. The GM receives those dice results
through its supervisor events and continues the scenario:

  roll results -> wait until every roll and reply is resolved -> ADVANCE
  engine-written announcement data -> ANNOUNCE as soon as it exists
  official resolution -> FINISH -> C4 REPORT with evidence

The GM never acts for a player, never relays or narrates a roll to other
players (A11, C3) and never sees the case truth. Its scenario log is a private
GM record. A successor term rebuilds the log from engine events only (C5).
"""
import hashlib
import time

from . import gm_metrics, mission_policy, role_policy
from .contracts import ContractError
from .engine_v1 import AuthorityLost
from .settings import SETTINGS, verify_engine

PHASES = ("movement", "interaction", "deduction")


class SettingsDrift(ContractError):
    """The engine's rules differ from competitive_settings.json; results would not be comparable."""


def _code(exc):
    return getattr(exc, "code", None)


def _seed(game_id):
    """The GM does not see the engine seed; its own choices are seeded by the game id."""
    return int(hashlib.sha256(f"gm-policy:{game_id}".encode()).hexdigest()[:8], 16)


class CompetitiveGM:
    """One GM term. Constructed only from engine-issued GM credentials."""

    def __init__(self, client, context_key, strict_settings=True):
        creds = client.credentials
        if not all(key in creds for key in ("game_id", "controller_id", "term", "controller_token")):
            raise ContractError("engine-issued GM credentials required")
        self.client, self.context_key = client, context_key
        self.game_id, self.controller_id, self.term = creds["game_id"], creds["controller_id"], creds["term"]
        self.drift = verify_engine(client.call("catalog"))
        if self.drift and strict_settings:
            raise SettingsDrift("engine settings differ: " + "; ".join(self.drift))
        self.ledger = {"event_cursor": 0, "cycle": 1, "phase": "movement", "epoch": 1,
                       "rolls": [], "scenario": [], "verdicts": [], "announced": [],
                       "faults": [], "successions": [], "human_requests": [], "decisions": []}
        self.reported = False

    def _call(self, route, **data):
        try:
            return self.client.call(route, **data)
        except Exception as exc:
            if _code(exc) == "stale_controller":
                raise AuthorityLost(f"GM term {self.term} fenced by the engine") from exc
            raise

    def _command(self, name, **data):
        return self._call("gm/command", command_id=f"{self.context_key}:{name}", **data)

    # ---------- dice results and other engine events ----------

    def _apply(self, event):
        ledger, kind, payload = self.ledger, event["type"], event["payload"]
        if kind == "action_completed":
            result = payload["result"]
            entry = {"cycle": result["cycle_id"], "phase": result["phase"], "kind": result["action"],
                     "agent_id": result["agent_id"], "event_id": event["event_id"]}
            if result["action"] in ("question", "accuse"):
                return  # Their outcomes arrive as question_supervision / deduction_ready.
            if result["action"] == "explore":
                roll = result.get("roll") or {}
                # A's GM projection carries acquired IDs only; successes are kept once the engine adds them.
                entry.update(acquired=len(result.get("acquired_information_ids", [])),
                             successes=result.get("successes", roll.get("successes")))
            ledger["rolls"].append(entry)
        elif kind == "question_supervision":
            ledger["rolls"].append({"cycle": ledger["cycle"], "phase": "interaction", "kind": "question",
                                    "question_id": payload["question_id"], "asker": payload["asker_id"],
                                    "target": payload["target_id"], "approach": payload["approach"],
                                    "successes": payload["successes"], "transferred": len(payload["transferred_ids"]),
                                    "event_id": event["event_id"]})
        elif kind == "deduction_ready":
            ledger["verdicts"].append({"cycle": ledger["cycle"], "ref": payload["ref"], "verdicts": payload["verdicts"],
                                       "event_id": event["event_id"]})
        elif kind == "phase_advanced":
            self._close_phase()
            for s in ledger["successions"]:
                # C4: a successor's recovery lasts until the game first moves on under it.
                if s["type"] == "gm_succeeded" and "recovered_cycle" not in s:
                    s["recovered_cycle"] = ledger["cycle"]
            ledger.update(cycle=payload["cycle_id"], phase=payload["phase"], epoch=payload["phase_epoch"])
        elif kind == "announcement":
            ledger["announced"].append(payload["ref"])
        elif kind == "player_fault":
            ledger["faults"].append({"cycle": ledger["cycle"], "event_id": event["event_id"],
                                     "agent_id": payload["agent_id"], "reason": payload["reason"]})
        elif kind in ("gm_retired", "gm_succeeded"):
            ledger["successions"].append({"cycle": ledger["cycle"], "type": kind, "event_id": event["event_id"], **payload})
        elif kind == "human_intervention_required":
            ledger["human_requests"].append({"event_id": event["event_id"], **payload})

    def _close_phase(self):
        """One private scenario line per finished phase, built from that phase's results."""
        ledger = self.ledger
        rolls = [r for r in ledger["rolls"] if r["cycle"] == ledger["cycle"] and r["phase"] == ledger["phase"]]
        line = {"cycle": ledger["cycle"], "phase": ledger["phase"], "actions": len(rolls)}
        if ledger["phase"] == "interaction":
            questions = [r for r in rolls if r["kind"] == "question"]
            explores = [r for r in rolls if r["kind"] == "explore"]
            line.update(questions=len(questions), question_successes=[r["successes"] for r in questions],
                        transfers=sum(r["transferred"] for r in questions),
                        explores=len(explores), explore_acquired=sum(r["acquired"] for r in explores))
            line["text"] = (f"{ledger['cycle']}사이클 탐문·탐색: 질문 {len(questions)}건(성공 수 "
                            f"{line['question_successes']}), 이전 {line['transfers']}건, 탐색 {len(explores)}건·"
                            f"획득 {line['explore_acquired']}개")
        elif ledger["phase"] == "deduction":
            verdicts = [v for x in ledger["verdicts"] if x["cycle"] == ledger["cycle"] for v in x["verdicts"]]
            line.update(challengers=len(verdicts), winners=sum(v["winner"] for v in verdicts))
            line["text"] = f"{ledger['cycle']}사이클 추리: 도전 {len(verdicts)}명, 정답 {line['winners']}명"
        else:
            line["text"] = f"{ledger['cycle']}사이클 이동 완료"
        ledger["scenario"].append(line)

    def sync(self):
        """Apply new events, then observe; repeat while the engine kept moving."""
        for _ in range(5):
            batch = self._call("events", after=self.ledger["event_cursor"])
            for event in batch["events"]:
                if event["sequence"] > self.ledger["event_cursor"]:
                    self._apply(event)
            # Private player events also take sequence numbers; the batch's sequence covers them.
            self.ledger["event_cursor"] = batch["event_sequence"]
            observation = self._call("gm/observe")
            if observation["event_sequence"] <= self.ledger["event_cursor"]:
                break
        else:
            raise ContractError("engine events kept moving; retry synchronization")
        if observation["control"] and observation["control"] != {"controller_id": self.controller_id, "term": self.term}:
            raise AuthorityLost("observation belongs to another GM term")
        return observation

    # ---------- Role assignment (C1) ----------

    def _setup(self, observation):
        seed = _seed(self.game_id)
        pool = observation["pool"]
        available = sorted(c["agent_id"] for c in pool if c["pool_role"] == "player" and c["pool_status"] == "available")
        history = {c["agent_id"]: c["history"] for c in pool}
        roles = role_policy.plan_roles(available, history, seed)
        draw = self._call("gm/mission-candidates", activate=roles["activation"])
        mbti = {c["character_id"]: c["mbti"] for c in draw["characters"]}
        players = [{"agent_id": x["agent_id"], "character_id": x["character_id"], "mbti": mbti[x["character_id"]]}
                   for x in roles["activation"]]
        received = {aid: [m["mission_id"] for h in records for m in h.get("sub_missions", [])]
                    for aid, records in history.items()}
        missions = mission_policy.plan_missions(players, draw["characters"], draw["mission_candidates"], seed,
                                                received=received, indirect_clue_ids=draw["indirect_clue_ids"])
        activation = [x | {"sub_missions": missions["sub_missions"][x["agent_id"]]} for x in roles["activation"]]
        try:
            self._command("plan:setup", operation="PLAN", map_version=observation["map_version"], activate=activation,
                          alternatives=roles["alternatives"], selected_alternative=roles["selected_alternative"],
                          reason=roles["reason"] + " / 서브 미션: " + missions["reason"])
        except Exception as exc:
            if _code(exc) == "conflict":
                return "retry"  # Roster or map changed under the plan; observe again.
            raise
        # The engine stores one decision per PLAN; the mission decision is kept here (D1).
        self.ledger["decisions"] += [
            {"kind": "roles", "alternatives": roles["alternatives"], "selected_alternative": roles["selected_alternative"],
             "reason": roles["reason"], "exploratory": roles["exploratory"]},
            {"kind": "sub_missions", "alternatives": missions["alternatives"],
             "selected_alternative": missions["selected_alternative"], "reason": missions["reason"],
             "first_time": {aid: [m["mission_id"] not in received.get(aid, []) for m in entries]
                            for aid, entries in missions["sub_missions"].items()}}]
        return "planned"

    # ---------- progression (C2) ----------

    @staticmethod
    def _resolved(observation):
        """A3: every active player spent this phase and no question awaits its reply."""
        active = observation["active_agents"]
        return bool(active) and all(aid in observation["spent"] for aid in active) and \
            not any(q["pending"] for q in observation["questions"])

    def _announce(self, observation):
        published = []
        for item in observation["pending_announcements"]:
            self._command(f"announce:{item['ref']}", operation="ANNOUNCE", ref=item["ref"], kind=item["kind"])
            published.append(item["ref"])
        return published

    def tick(self):
        """One GM step. Returns a status string; never performs player actions."""
        observation = self.sync()
        if observation["ended"]:
            if not self.reported:
                self._report(observation)
            return "ended"
        self._call("gm/heartbeat")
        status = observation["status"]
        if status in ("paused", "waiting_for_human"):
            return status
        if status == "awaiting_setup":
            return self._setup(observation)
        self._announce(observation)
        if observation["resolution"] is not None:
            self._finish(observation)
            return "ended"
        if not self._resolved(observation):
            return "waiting"
        try:
            self._command(f"advance:{observation['phase_epoch']}", operation="ADVANCE",
                          phase_epoch=observation["phase_epoch"])
        except Exception as exc:
            if _code(exc) == "pending":
                return "waiting"
            if _code(exc) == "conflict":
                return "retry"
            raise
        # Verdicts and deadline warnings appear with the advance; publish them before anyone acts again.
        observation = self.sync()
        self._announce(observation)
        if observation["resolution"] is not None:
            self._finish(observation)
            return "ended"
        return "advanced"

    # ---------- ending and C4 report ----------

    def metrics(self, observation):
        """C4/C6 metrics from engine records; history excludes this game, before or after FINISH."""
        history = {c["agent_id"]: [h for h in c["history"] if h["game_id"] != self.game_id] for c in observation["pool"]}
        roles = observation["role_assignments"]
        role_flags = [role_policy.exploratory(a["agent_id"], a["character_id"], history) for a in roles]
        strengths = []
        for a in roles:
            competency = role_policy.job_competency(history.get(a["agent_id"], []))
            if competency is not None:
                strengths.append(competency[a["character_id"]])
        # Only the term that planned knows the missions; the engine's GM view omits them.
        mission_flags = [flag for d in self.ledger["decisions"] if d["kind"] == "sub_missions"
                         for flags in d["first_time"].values() for flag in flags]
        recovery = sum(s.get("recovered_cycle", observation["cycle_id"]) - s["cycle"]
                       for s in self.ledger["successions"] if s["type"] == "gm_succeeded")
        stalled = any(s["type"] == "gm_retired" and s.get("reason") == "lease_expired" for s in self.ledger["successions"])
        return {"completion": gm_metrics.completion(observation["resolution"], gm_stalled=stalled,
                                                    recovery_cycles=recovery, cycles=observation["cycle_id"]),
                "balance": gm_metrics.balance(strengths),
                "exploration": gm_metrics.exploration(role_flags, mission_flags),
                "record": gm_metrics.record_completeness(observation["plans"])}

    def _finish(self, observation):
        self._command("finish", operation="FINISH")
        self._report(self.sync())

    def _report(self, observation):
        last = self.ledger["scenario"][-1] if self.ledger["scenario"] else {}
        if (last.get("cycle"), last.get("phase")) != (self.ledger["cycle"], self.ledger["phase"]):
            self._close_phase()  # The final phase ends without a phase_advanced event.
        if observation["gm_report"] is None:
            events = self._call("events", after=0)["events"]
            evidence = [e["event_id"] for e in events if e["type"] in
                        ("roles_assigned", "deduction_ready", "resolution_ready", "announcement", "game_finished")]
            self._command("report", operation="REPORT", metrics=self.metrics(observation), evidence=evidence)
        self.reported = True

    def report(self):
        """GM-side record for D1/C-6 (private to the GM and the operator)."""
        observation = self.sync()
        return {"game_id": self.game_id, "control": observation["control"], "status": observation["status"],
                "cycle_id": observation["cycle_id"], "phase": observation["phase"],
                "resolution": observation["resolution"], "engine_metrics": observation["metrics"],
                "gm_report": observation["gm_report"], "settings_drift": self.drift,
                "decisions": self.ledger["decisions"], "scenario": self.ledger["scenario"],
                "rolls": self.ledger["rolls"], "verdicts": self.ledger["verdicts"],
                "faults": self.ledger["faults"], "successions": self.ledger["successions"],
                "human_requests": self.ledger["human_requests"]}


class CompetitiveGMWorker:
    """Persistent pool worker's GM side. Polls its own session for the GM role and promotions."""

    def __init__(self, identity, strict_settings=True):
        if not all(key in identity.credentials for key in ("agent_id", "pool_token")):
            raise ContractError("pool identity client required")
        self.identity, self.strict_settings = identity, strict_settings
        self.gm = None

    def poll(self):
        session = self.identity.call("pool/session")
        if session["role"] != "gm":
            return None
        if self.gm is None or self.gm.context_key != session["context_key"]:
            # New term: fresh context from the engine's GM observation only, no player-side memory.
            self.gm = CompetitiveGM(self.identity.bind(**session["credentials"]), session["context_key"],
                                    self.strict_settings)
        return self.gm

    def step(self):
        # A finished term still owes its REPORT although the session is already dormant.
        gm = self.gm if self.gm is not None and not self.gm.reported else self.poll()
        if gm is None:
            return "not_gm"
        try:
            return gm.tick()
        except AuthorityLost:
            self.gm = None
            return "fenced"

    def run(self, stop=lambda: False, sleep=time.sleep):
        """Heartbeat well inside the GM lease (competitive_settings.json gm_runtime)."""
        interval = min(SETTINGS["gm_runtime"]["poll_seconds"], SETTINGS["gm_runtime"]["heartbeat_seconds"])
        while not stop():
            status = self.step()
            if status == "ended" and self.gm is not None and self.gm.reported:
                return self.gm.report()
            sleep(interval)
