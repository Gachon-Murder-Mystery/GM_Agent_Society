"""C-owned GM for A's competitive v1.3 engine (`mode=competitive`, ruleset competitive-v1.3).

The GM is a game master, not a task planner (spec v1.3 C). [FIX-C14] It is the master controller:
it opens every phase, asks each player to act (REQUEST: the legal actions and targets the engine
computed for that player), closes the negotiation stage (RELEASE), declares mechanical timeouts
(EXPIRE) and advances (ADVANCE). The engine only referees: players submit the requested action,
their answers and trade replies to the engine directly, the engine rolls the dice and adjudicates,
so adjudication never waits for the GM. The GM receives the dice results through its supervisor
events and continues the scenario:

  open phase -> REQUEST each player -> (timeouts: EXPIRE, replacement REQUEST) -> RELEASE negotiation
  -> wait until every roll and reply is resolved -> ADVANCE
  engine-written announcement data -> ANNOUNCE as soon as it exists
  official resolution -> FINISH -> C4 REPORT with evidence

The GM never acts for a player, never relays or narrates a roll to other
players (A11, C3) and never sees the case truth. Its scenario log is a private
GM record. A successor term rebuilds the log from engine events only (C5).
"""
import hashlib
import json
import time

from . import gm_metrics, mission_policy, role_policy
from .contracts import ContractError
from .engine_v1 import AuthorityLost, EngineV1Error
from .gm_memory import GMMemory, IncidentWatch
from .settings import SETTINGS, verify_engine

PHASES = ("movement", "interaction", "deduction")


class SettingsDrift(ContractError):
    """The engine's rules differ from competitive_settings.json; results would not be comparable."""


def _code(exc):
    return getattr(exc, "code", None)


def _transient(exc):
    """[FIX-C15] Errors worth waiting out: engine 5xx, connection/timeouts, events still moving."""
    if isinstance(exc, EngineV1Error):
        return exc.status >= 500
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    return isinstance(exc, ContractError) and "retry synchronization" in str(exc)


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
                       "faults": [], "successions": [], "human_requests": [], "decisions": [],
                       "incidents": []}
        self.reported = False
        # [FIX-C15] my own memory (this GM agent only) and my watch over this game's progress
        self.memory = GMMemory(self.controller_id, context={"game_id": self.game_id, "term": self.term})   # [FIX-C27]
        try:   # [FIX-C20] the model-call deadline and the stuck-call remedy are this GM's to decide
            from model_outage import MODEL_POLICY
            MODEL_POLICY.bind(self.memory)
        except ImportError:   # the GM package used on its own (no model in this process)
            pass
        self.watch = IncidentWatch(self.memory)

    def _call(self, route, **data):
        try:
            return self.client.call(route, **data)
        except Exception as exc:
            if _code(exc) == "stale_controller":
                raise AuthorityLost(f"GM term {self.term} fenced by the engine") from exc
            if _code(exc) == "disconnected":   # [FIX-C22/A57] the engine cut my link: wait for it to come back
                raise LinkCut("GM link cut by the engine") from exc
            raise

    def _command(self, name, **data):
        return self._call("gm/command", command_id=f"{self.context_key}:{name}", **data)

    # ---------- dice results and other engine events ----------

    def _apply(self, event):
        ledger, kind, payload = self.ledger, event["type"], event["payload"]
        if kind == "relations_reported":    # [FIX-A35/C17] record only: never used to run the game
            ledger.setdefault("relations", []).append(
                {"cycle": payload.get("cycle"), "agent_id": payload.get("agent_id"), "scores": payload.get("scores") or {}})
            return
        if kind == "operator_action":       # [FIX-I11] a paused game is not a stalled game
            op = payload.get("operation")
            if op == "PAUSE":
                self._paused_at = event.get("timestamp")
            elif op == "RESUME" and getattr(self, "_paused_at", None) is not None:
                self.watch.paused_for(event.get("timestamp", self._paused_at) - self._paused_at)
                self.ledger["incidents"].append({"cycle": ledger["cycle"], "kind": "game_paused",
                                                 "seconds": round(event.get("timestamp", 0) - self._paused_at, 1)})
                self._paused_at = None
            return
        if kind == "action_completed":
            result = payload["result"]
            self.watch.acted(result.get("agent_id"))            # [FIX-C15]
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
            if payload.get("kind") == "time_out":
                ledger["scenario"].append({"cycle":ledger["cycle"], "phase":ledger["phase"], "text":payload["text"]})
        elif kind == "player_fault":
            ledger["faults"].append({"cycle": ledger["cycle"], "event_id": event["event_id"],
                                     "agent_id": payload["agent_id"], "reason": payload["reason"]})
        elif kind in ("gm_retired", "gm_succeeded"):
            ledger["successions"].append({"cycle": ledger["cycle"], "type": kind, "event_id": event["event_id"], **payload})
            self.memory.note_fault(kind, payload, ledger["cycle"])   # [FIX-C27] only a successor writes it down
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
                                                received=received, indirect_clue_ids=draw["indirect_clue_ids"],
                                                interference=draw.get("interference", []),   # [FIX-C21]
                                                blockable=draw.get("blockable"))   # [FIX-C23]
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

    def _orchestrate(self, observation):
        """[FIX-C14] Drive the open phase. Returns "ready" when the phase can advance."""
        epoch = observation["phase_epoch"]
        talk = observation.get("talk")
        if talk:   # [FIX-C19/A42] the talk rooms run their fixed rounds first; a silent member is let go after the timeout
            force = not talk["ready"] and talk["overdue"]
            if talk["ready"] or force:
                try:
                    self._command(f"talk:{epoch}:{talk['round']}:{'f' if force else 'r'}", operation="TALK_NEXT",
                                  phase_epoch=epoch, force=force)
                except Exception as exc:
                    if _code(exc) not in ("pending", "conflict"):
                        raise
                observation = self.sync()
            if observation.get("talk"):
                return "waiting"
        expired = False
        for item in observation.get("overdue", []):
            ref = hashlib.sha1(item["ref"].encode()).hexdigest()[:12]
            try:
                self._command(f"expire:{item['kind']}:{ref}", operation="EXPIRE", kind=item["kind"], ref=item["ref"])
                expired = True
            except Exception as exc:
                if _code(exc) != "conflict":  # settled meanwhile
                    raise
        if expired:
            observation = self.sync()
        ids = observation.get("needs_request") or []
        if ids:
            self.ledger["requests"] = self.ledger.get("requests", 0) + 1
            stamp = f"{epoch}:{self.ledger['requests']}:{int(time.time() * 1000) % 10 ** 8}"
            try:
                issued = self._command(f"request:{stamp}", operation="REQUEST", phase_epoch=epoch, agent_ids=ids)
                self.watch.requested(list((issued or {}).get("requested", {})))   # [FIX-C15]
            except Exception as exc:
                if _code(exc) != "conflict":  # the phase moved on under us
                    raise
                self.watch.refused("REQUEST", "conflict")
            observation = self.sync()
        if observation.get("ready_to_release"):
            try:
                self._command(f"release:{epoch}", operation="RELEASE", phase_epoch=epoch)
            except Exception as exc:
                if _code(exc) not in ("pending", "conflict"):
                    raise
                self.watch.refused("RELEASE", _code(exc))
            observation = self.sync()
        return "ready" if observation.get("ready_to_advance") else "waiting"

    def tick(self):
        """One GM step. Returns a status string; never performs player actions.
        [FIX-C15] Transient engine errors are waited out with the remedy my memory has learned
        works best (retry_short / retry_long); only a long run of them ends the game."""
        try:
            status = self._tick()
        except AuthorityLost:
            raise
        except Exception as exc:
            if not _transient(exc):
                raise
            remedy = self.watch.engine_error(exc)
            self.ledger["incidents"].append({"cycle": self.ledger["cycle"], "kind": "engine_error",
                                             "remedy": remedy, "error": str(exc)[:200]})
            if remedy == "escalate":
                context = self.watch.escalation_context("engine_error", "transient engine errors did not stop")
                context["errors"] = self.watch.engine_errors
                self.memory.escalate("engine_error", [str(exc)[:200]], self.ledger["phase"], context)
                raise
            time.sleep(1 if remedy == "retry_short" else 5)
            return "retry"
        self.watch.engine_ok()
        return status

    def _handle_links(self, observation):
        """[FIX-C22/A57] Players the engine cut off are my call: keep the seat (wait) or make the character an NPC
        (its personality stays); a reconnected NPC-made player: give the character back or keep the NPC. The choice
        comes from my memory (Thompson sampling over past outcomes); 'wait' counts as resolved if the player came back."""
        links = observation.get("links") or {}
        decided = self.__dict__.setdefault("link_decisions", {})
        now = time.time()
        for aid, (kind, remedy, t0) in list(decided.items()):
            if kind == "player_cut" and remedy == "wait" and aid not in (links.get("down") or []):
                self.memory.learn("player_cut", "wait", True, now - t0)
                decided.pop(aid)
        for aid in links.get("down") or []:
            if aid in decided or aid not in observation.get("active_agents", []):
                continue
            remedy = self.memory.choose("player_cut", exclude=("wait",) if aid in self.__dict__.get("wait_failed", ())
                                        else ()) or "npc"
            self._command(f"link:{aid}:{observation['cycle_id']}:{remedy}", operation="HANDLE_PLAYER", agent_id=aid,
                          action="wait" if remedy == "wait" else "npc")
            decided[aid] = ("player_cut", remedy, now)
            taught = self.memory.lesson("player_cut")                         # [FIX-C28] why I chose it
            self.ledger["incidents"].append({"cycle": self.ledger["cycle"], "kind": "player_cut", "agent": aid[-6:], "remedy": remedy,
                                             **({"lesson": taught["why"][:160]} if taught else {})})
            if remedy == "npc":
                self.memory.learn("player_cut", "npc", True, 0)
                decided.pop(aid)
        for aid in links.get("reconnected_npcs") or []:
            key = ("back", aid)
            if key in decided:
                continue
            remedy = self.memory.choose("player_back") or "restore"
            if remedy == "restore" and (observation.get("rules") or {}).get("cut_player_sits_out"):
                remedy = "keep_npc"   # [FIX-A61] the rule of this game: it sits the game out
            if remedy == "restore":
                self._command(f"restore:{aid}:{observation['cycle_id']}", operation="HANDLE_PLAYER", agent_id=aid, action="restore")
            decided[key] = ("player_back", remedy, now)
            self.memory.learn("player_back", remedy, True, 0)
            self.ledger["incidents"].append({"cycle": self.ledger["cycle"], "kind": "player_back", "agent": aid[-6:], "remedy": remedy})

    def _wait_costs(self, observation):
        """[FIX-C29] A stall caused by a cut player I am waiting for (its questions go unanswered) is the cost of
        that wait: I learn it as a failed wait, with the time it held the game, and decide about the player again."""
        decided = self.__dict__.get("link_decisions") or {}
        waiting = [aid for aid, (kind, remedy, t0) in decided.items() if kind == "player_cut" and remedy == "wait"]
        if not waiting:
            return
        char_of = {c.get("controller"): c.get("character_id") for c in observation.get("characters") or []}
        now = time.time()
        for inc in self.watch.open.values():
            if inc.get("kind") != "answer_pending":
                continue
            subject = inc.get("subject") or []
            for aid in waiting:
                if aid in decided and (aid in subject or char_of.get(aid) in subject):
                    self.memory.learn("player_cut", "wait", False, now - inc["since"])
                    decided.pop(aid)
                    self.__dict__.setdefault("wait_failed", set()).add(aid)   # next decision: not 'wait' again
                    self.ledger["incidents"].append({"cycle": self.ledger["cycle"], "kind": "wait_cost", "agent": aid[-6:],
                                                     "held_seconds": round(now - inc["since"], 1)})

    def _close_link_decisions(self):
        """Waits still open when the game ends: the player never came back."""
        for aid, (kind, remedy, t0) in list(self.__dict__.get("link_decisions", {}).items()):
            if kind == "player_cut" and remedy == "wait":
                self.memory.learn("player_cut", "wait", False, time.time() - t0)
        self.__dict__["link_decisions"] = {}

    RELATIONS_SUMMARY_LIMIT = 9000   # bytes; the engine's HTTP limit for a GM command body is 18,000

    def relations_summary(self):
        """[FIX-A35/C17] What the agents told me about their relationships, summarised for the record.
        These are reports (an agent's own feeling, not verifiable facts). I never use them to assign,
        request, advance or score anything, and they do not go into my own memory.
        [FIX-C17b] Compact (6-character agent tags, integers) and bounded so the REPORT fits the
        engine's request limit: when too large, the per-agent matrix and then the trend are thinned."""
        reports = self.ledger.get("relations") or []
        if not reports:
            return None
        tag = lambda aid: str(aid)[-6:]
        latest, trend = {}, {}
        for r in reports:
            latest[tag(r["agent_id"])] = {tag(k): round(v) for k, v in r["scores"].items()}
            for target, value in r["scores"].items():
                trend.setdefault(r["cycle"], {})[(r["agent_id"], target)] = value
        given = {a: round(sum(s.values()) / len(s), 1) for a, s in latest.items() if s}
        received = {}
        for a, s in latest.items():
            for target, value in s.items():
                received.setdefault(target, []).append(value)
        pairs = []
        for a, s in latest.items():
            for b, value in s.items():
                back = (latest.get(b) or {}).get(a)
                if back is not None and a < b:
                    pairs.append([a, b, value, back, abs(value - back)])
        pairs.sort(key=lambda x: -x[4])
        running, by_cycle = {}, []
        for cycle in sorted(c for c in trend if c is not None):
            running.update(trend[cycle])
            by_cycle.append([cycle, round(sum(running.values()) / len(running), 1), len(running)])
        out = {"kind": "reported_by_agents", "note": "each agent's own feeling; record only, not used by the GM or the engine",
               "reports": len(reports), "agents": len(latest), "given_mean": given,
               "received_mean": {k: round(sum(v) / len(v), 1) for k, v in received.items()},
               "mutual": {"pairs": len(pairs), "mean_gap": round(sum(x[4] for x in pairs) / len(pairs), 1) if pairs else None,
                          "largest_gaps": pairs[:5]},
               "by_cycle": by_cycle, "latest": latest}
        size = lambda o: len(json.dumps(o, ensure_ascii=False).encode("utf-8"))
        if size(out) > self.RELATIONS_SUMMARY_LIMIT:
            out.pop("latest")
            out["thinned"] = ["latest"]
        while size(out) > self.RELATIONS_SUMMARY_LIMIT and len(out["by_cycle"]) > 2:
            out["by_cycle"] = out["by_cycle"][::2]
            out.setdefault("thinned", []).append("by_cycle")
        return out

    def _remedy(self, key, remedy, observation):
        """[FIX-C15] Apply one safe remedy chosen by my memory for a stalled phase."""
        inc = self.watch.open.get(key) or {}
        self.ledger["incidents"].append({"cycle": self.ledger["cycle"], "phase": self.ledger["phase"],
                                         "kind": inc.get("kind"), "subject": inc.get("subject"), "remedy": remedy})
        if remedy == "resync":
            return self.sync()
        if remedy == "re_request" and inc.get("kind") == "no_action" and inc.get("subject"):   # [FIX-C26]
            stamp = f"{observation['phase_epoch']}:reissue:{int(time.time() * 1000) % 10 ** 8}"
            try:
                issued = self._command(f"request:{stamp}", operation="REQUEST", phase_epoch=observation["phase_epoch"],
                                       agent_ids=list(inc["subject"]), reissue=True)
                self.watch.requested(list((issued or {}).get("requested", {})))
            except Exception as exc:
                if _code(exc) != "conflict":
                    raise
        elif remedy == "re_request" and observation.get("needs_request"):
            stamp = f"{observation['phase_epoch']}:re:{int(time.time() * 1000) % 10 ** 8}"
            try:
                issued = self._command(f"request:{stamp}", operation="REQUEST", phase_epoch=observation["phase_epoch"],
                                       agent_ids=observation["needs_request"])
                self.watch.requested(list((issued or {}).get("requested", {})))
            except Exception as exc:
                if _code(exc) != "conflict":
                    raise
        elif remedy == "escalate":
            # [FIX-C16] the hand-off carries what was tried, for how long, and the progress signals
            context = self.watch.escalation_context(key, "every safe remedy was tried; the phase still does not move")
            self.ledger["human_requests"].append({"kind": "gm_escalation", "incident": inc.get("kind"),
                                                  "subject": inc.get("subject"), "cycle": self.ledger["cycle"],
                                                  **context})
            self.memory.escalate(inc.get("kind"), inc.get("subject"), inc.get("stage"), context)
        return observation

    def _tick(self):
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
        self._handle_links(observation)   # [FIX-C22/A57]
        down = set((observation.get("links") or {}).get("down") or [])
        if down:   # a cut player is my decision below, not a stall to remedy
            observation = dict(observation, active_agents=[x for x in observation["active_agents"] if x not in down])
        if observation.get("orchestration") == "gm":   # [FIX-C14] the GM drives the phase
            for key, remedy in self.watch.observe(observation):   # [FIX-C15] stalls -> learned remedy
                observation = self._remedy(key, remedy, observation) or observation
            self._wait_costs(observation)   # [FIX-C29]
            if self._orchestrate(observation) != "ready":
                return "waiting"
        elif not self._resolved(observation):
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
        if observation.get("orchestration") == "gm":   # [FIX-C14] ask for the new phase at once
            self._orchestrate(observation)
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
        if not observation.get("ended"):   # [FIX-C22] the engine may already have ended it (A57 gm_exhausted)
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
            self._command("report", operation="REPORT", metrics=self.metrics(observation), evidence=evidence,
                          reported_relationships=self.relations_summary())   # [FIX-A35/C17]
        for item in observation.get("pending_announcements") or []:   # [FIX-C18] tell each player its result, privately
            if item.get("kind") == "scoreboard":
                try:
                    self._command(f"announce:{item['ref']}", operation="ANNOUNCE", ref=item["ref"], kind="scoreboard")
                except Exception as exc:
                    if _code(exc) not in ("conflict", "not_running"):
                        raise
        self._close_link_decisions()   # [FIX-C22]
        if not self.reported:   # [FIX-C15] one summary per game in my own memory
            self.watch.close_game()                                    # [FIX-C16]
            t = self.watch.summary()["time"]                           # [FIX-C29] my score for a smooth game
            self.memory.time_score(self.game_id, t["actual_s"], t["typical_s"], t["wasted_s"], t["causes"])
            self.memory.record_game({"game_id": self.game_id, "cycles": observation.get("cycle_id"),
                                     "resolution": observation.get("resolution"), **self.watch.summary()})
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
                "human_requests": self.ledger["human_requests"],
                # [FIX-C15] what my memory saw and learned in this game
                "gm_memory": {"incidents": self.ledger["incidents"], "watch": self.watch.summary(),
                              "playbook": self.memory.data.get("playbook"),
                              "time_scores": (self.memory.data.get("time_scores") or [])[-5:],   # [FIX-C29]
                              "lessons": [l for l in self.memory.lessons() if l.get("active", True)],   # [FIX-C28]
                              "stall_thresholds": {k: self.memory.stall_threshold(k) for k in self.memory.data.get("norms", {})}}}


class LinkCut(Exception):
    """[FIX-C22] The engine cut this GM's link (fault scenario A57)."""


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
            # [FIX-C30] my term ended (a successor took over, or the GM came back): what I learned in it is kept.
            # 11-17 fake check: a successor fenced by the returning GM left an empty hand-over (0 ops).
            try:
                gm.memory.save()
            except Exception as exc:   # learning must not break the term change
                print(f"GM memory save at term end failed: {type(exc).__name__}: {exc}", flush=True)
            self.gm = None
            return "fenced"
        except LinkCut:   # [FIX-C22] keep my term; the engine either restores the link or hands the game to a successor
            return "disconnected"

    def run(self, stop=lambda: False, sleep=time.sleep):
        """Heartbeat well inside the GM lease (competitive_settings.json gm_runtime)."""
        interval = min(SETTINGS["gm_runtime"]["poll_seconds"], SETTINGS["gm_runtime"]["heartbeat_seconds"])
        while not stop():
            status = self.step()
            if status == "ended" and self.gm is not None and self.gm.reported:
                return self.gm.report()
            sleep(interval)
