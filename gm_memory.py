"""[FIX-C15] The GM's own memory, and how it learns to keep a game moving through bug-like situations.

User decisions 2026-11-09: every agent holds its own memory, the GM too. The GM's memory is about
keeping the game progressing stably, as the project intends, when something goes wrong (a player
stops acting, a request is not issued, the engine refuses or fails, a phase does not advance), and
that handling must be learned across games.

Only remedies that never change a game's outcome are allowed (the GM never acts for a player and
time never decides results; EXPIRE stays the rule-defined mechanical timeout):
  wait          give it more time (the stall threshold is learned from this pool's normal pace)
  resync        re-read the engine (events and GM view) before acting again
  re_request    issue REQUEST again for players the engine says still need one
  retry_short / retry_long   back off 1 s / 5 s after a transient engine error
  escalate      record it for the operator (report + memory); the game goes on

Learning (persisted per GM agent in <memory dir>/gm_<agent>.json):
  playbook  incident type -> remedy -> {tries, resolved, seconds, a, b}; untried remedies are tried
            first in a safe default order, then [FIX-C16] Thompson sampling (textbook II ch11,
            Bayesian bandits): draw from Beta(1+a, 1+b) per remedy and take the highest. a/b are
            resolved/failed counts discounted by DECAY at every new result of that incident type,
            so the choice re-adapts when the environment changes (e.g. Codex gets slower).
  norms     seconds each phase stage normally takes in this pool -> stall threshold
            clamp(2 x p90, 60 s, 900 s); 180 s until there is data.
  games     one summary per game (incidents, how they ended, stage durations).
  time      [FIX-C29] score per game = 100 x typical / actual stage time; fixes score by how fast they worked.
  lessons   [FIX-C28] feedback with its reason (incident type, remedy to prefer/avoid, why, evidence); binds choose().
"""
import json
import os
import random
import statistics
import time
from pathlib import Path

REMEDIES = {
    # incident type -> safe remedies in default (exploration) order
    # [FIX-C26] re_request re-sends the open request under a new number (engine A59); the player decides, as always
    "no_action": ("wait", "re_request", "resync", "escalate"),
    "request_not_issued": ("re_request", "resync", "escalate"),
    "not_advancing": ("resync", "wait", "escalate"),
    "answer_pending": ("wait", "escalate"),
    "command_refused": ("resync", "wait"),
    "engine_error": ("retry_short", "retry_long", "escalate"),
    # [FIX-C22/A57] a player the engine cut off: keep its seat and wait, or make its character an NPC;
    # when a player made an NPC reconnects: give the character back, or keep the NPC
    "player_cut": ("wait", "npc"),
    "player_back": ("restore", "keep_npc"),
    # [FIX-C20] a model call that outlived the GM's deadline: ask the model once more, or let the player's
    # rule-based stand-in act (counted as a fallback). 'retry' is resolved when the second call answers;
    # 'stand_in' never counts as resolved (the decision is no longer the model's), so the GM moves to it only
    # when retries keep failing.
    "model_timeout": ("retry", "stand_in"),
}
MIN_TRIES = 1              # warm start: every remedy once, in the safe default order
DECAY = 0.9                # [FIX-C16] older outcomes weigh less (non-stationary bandit)
TRACE_VIEWS = 12           # [FIX-C16] recent GM views kept for a regression trace
DEFAULT_STALL = 180.0
STALL_FLOOR, STALL_CEIL = 60.0, 900.0
NORM_KEEP = 300            # recent durations per stage (statistics, not memory content)
ENGINE_ERROR_LIMIT = 12    # consecutive transient errors before the GM gives up (re-raise)
# [FIX-C20] user decision 2026-11-11: the deadline of one model call is the GM's call, learned from this pool's
# answered calls: clamp(4 x p90, 60 s, 300 s); 300 s (the old fixed cap) until there are 20 calls.
MODEL_KEEP, MODEL_MIN_DATA = 300, 20
MODEL_FLOOR, MODEL_CEIL = 60.0, 300.0


def trace(record):
    """[FIX-C16] Append one incident to the campaign's regression traces (textbook II ch10,
    regression trace collection), replayable with tools/replay_traces.py."""
    path = os.environ.get("GAME_TRACE_FILE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(record, at=round(time.time(), 1)), ensure_ascii=False) + "\n")
    except OSError:
        pass


def memory_dir():
    d = os.environ.get("GAME_MEMORY_DIR")
    return Path(d) if d else None   # no directory (unit tests): memory lives in RAM only


# [FIX-C27] user decision 2026-11-16: the pool's GM keeps learning across games and campaigns (only the players are
# reset by the experiment). A GM term held by a successor (after a GM fault) works from the GM's memory, and what the
# successor goes through is kept apart as a hand-over record; the GM learns from it at its next term.
POOL_GM = os.environ.get("GAME_POOL_GM", "society:gm")
HANDOVER_FILE = "gm_handover.json"
HANDOVER_OPS_KEEP = 3000


# [FIX-C31] user 2026-11-17: "no authority without responsibility, no responsibility without authority". Causes beyond
# the GM's authority are outside its learning. A model outage is already paused out of every clock (FIX-I11); these
# kinds, if they ever reach the causes, are never the GM's.
OUTSIDE_AUTHORITY = {"game_paused", "model_outage", "gm_absent"}


def authority_time(actual, typical, wasted, causes):
    """[FIX-C31] the GM's time score counts only incident time (stalls it detected and handled): score =
    100 x (1 - accountable / actual). Stage time over the usual without an incident is outside its authority."""
    actual = float(actual or 0)
    accountable = sum(float(v) for k, v in (causes or {}).items() if k not in OUTSIDE_AUTHORITY)
    accountable = min(accountable, actual) if actual > 0 else 0.0
    score = round(100 * (1 - accountable / actual)) if actual > 0 else 100
    return {"score": score, "actual_s": round(actual), "typical_s": round(float(typical or 0)),
            "wasted_s": round(float(wasted or 0)), "accountable_s": round(accountable),
            "outside_authority_s": round(max(0.0, float(wasted or 0) - accountable)), "causes": dict(causes or {})}


class GMMemory:
    """Persistent. The pool GM's own memory; a successor GM reads it and writes a hand-over record instead."""

    def __init__(self, agent_id, directory=None, rng=None, context=None):
        self.agent_id = agent_id or "gm"
        self.rng = rng or random.Random()
        d = directory if directory is not None else memory_dir()
        self.dir = Path(d) if d else None
        self.successor = self.agent_id not in (POOL_GM, "gm")
        owner = POOL_GM if self.successor else self.agent_id
        self.path = self.dir / ("gm_" + owner.replace(":", "_") + ".json") if self.dir else None
        self.data = self._load()
        self.entry = None
        if self.successor:   # I hold the GM term for the pool GM: my record goes to the hand-over file
            self.entry = {"id": f"{self.agent_id}:{(context or {}).get('game_id')}:{(context or {}).get('term')}:{int(time.time())}",
                          "successor": self.agent_id, "status": "pending", "started": round(time.time()),
                          **{k: v for k, v in (context or {}).items() if k in ("game_id", "term")},
                          "gm_fault": [], "ops": [], "merged": 0}
            self._write_entry()
        else:
            self._receive_handovers()

    # ---------- [FIX-C27] hand-over ----------
    def _handover_path(self):
        return self.dir / HANDOVER_FILE if self.dir else None

    def _read_handovers(self):
        p = self._handover_path()
        if p and p.exists():
            try:
                rows = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    return rows
            except (OSError, ValueError):
                pass
        return []

    def _write_handovers(self, rows):
        p = self._handover_path()
        if not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)

    def _write_entry(self):
        rows = self._read_handovers()
        for i, r in enumerate(rows):
            if r.get("id") == self.entry["id"]:
                self.entry["merged"] = r.get("merged", 0)   # the GM may already have learned part of it
                if r.get("status") == "received":
                    self.entry["status"] = "received"
                rows[i] = self.entry
                break
        else:
            rows.append(self.entry)
        self._write_handovers(rows)

    def _op(self, **op):
        if self.successor:
            self.entry["ops"].append(op)
            del self.entry["ops"][:-HANDOVER_OPS_KEEP]

    def note_fault(self, kind, payload, cycle):
        """A successor writes down the GM fault it took over from (the engine's retire/succeed events)."""
        if self.successor:
            self.entry["gm_fault"].append({"type": kind, "cycle": cycle, **{k: payload.get(k) for k in
                                           ("reason", "controller_id", "term", "mode", "context_source") if k in payload}})
            self._write_entry()

    def _receive_handovers(self):
        """The pool GM learns what its successors went through (new ops since the last time only)."""
        rows = self._read_handovers()
        changed = False
        for r in rows:
            ops = r.get("ops") or []
            start = r.get("merged", 0)
            if start >= len(ops) and r.get("status") == "received":   # the fault itself is learned even with no ops
                continue
            learned = 0
            for op in ops[start:]:
                kind = op.get("op")
                if kind == "learn":
                    self.learn(op["kind"], op["remedy"], op["resolved"], op.get("seconds", 0), op.get("scale"))
                    learned += 1
                elif kind == "norm":
                    self.norm(op["stage"], op["seconds"])
                elif kind == "model_call":
                    self.model_call(op["seconds"])
                elif kind == "escalate":
                    self.data["escalations"].append(dict(op["row"], by_successor=r.get("successor")))
                    del self.data["escalations"][:-200]
                elif kind == "time_score":
                    self.data.setdefault("time_scores", []).append(dict(op["row"], by_successor=r.get("successor")))
                elif kind == "game":
                    self.data["games"].append(dict(op["summary"], by_successor=r.get("successor")))
            self.data.setdefault("handovers", []).append(
                {"from": r.get("successor"), "game_id": r.get("game_id"), "term": r.get("term"), "gm_fault": r.get("gm_fault"),
                 "ops": len(ops) - start, "learned": learned, "received": round(time.time())})
            r["merged"], r["status"] = len(ops), "received"
            changed = True
        if changed:
            self.save()
            self._write_handovers(rows)

    def _load(self):
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return {"agent_id": self.agent_id, "playbook": {}, "norms": {}, "games": [], "escalations": []}

    def save(self):
        if self.successor:   # [FIX-C27] never the pool GM's file: my part is the hand-over record
            if self.dir:
                self._write_entry()
            return
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(10):          # OneDrive may hold the file a moment (cf. I9)
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                time.sleep(0.5)

    # ---------- learning ----------
    # ---------- [FIX-C28] lessons: feedback with its reason ----------
    # user decision 2026-11-17: a lesson is what the GM was taught about an incident type and WHY (the operator's
    # feedback on a wrong response, with the evidence). It lives in the pool GM's own memory, so it carries across
    # games, campaigns and successors (who read this memory). A lesson binds the choice: the avoided remedy is never
    # drawn, the preferred one is taken when allowed. Counts alone (C16) cannot tell the GM what went wrong.
    def lessons(self):
        return self.data.setdefault("lessons", [])

    def lesson(self, kind):
        return next((l for l in reversed(self.lessons()) if l.get("kind") == kind and l.get("active", True)), None)

    def add_lesson(self, kind, why, prefer=None, avoid=(), evidence=None, source="operator"):
        if kind not in REMEDIES:
            raise ValueError(f"unknown incident type: {kind}")
        bad = [r for r in ([prefer] if prefer else []) + list(avoid) if r not in REMEDIES[kind]]
        if bad:
            raise ValueError(f"not a remedy of {kind}: {bad}")
        for l in self.lessons():
            if l.get("kind") == kind:
                l["active"] = False             # the newest lesson about a type replaces older ones (kept as history)
        row = {"kind": kind, "prefer": prefer, "avoid": list(avoid), "why": why, "evidence": evidence,
               "source": source, "at": round(time.time()), "active": True}
        self.lessons().append(row)
        return row

    def choose(self, kind, exclude=()):
        options = [r for r in REMEDIES.get(kind, ("wait",)) if r not in exclude]
        taught = self.lesson(kind)
        if taught:                              # [FIX-C28]
            options = [r for r in options if r not in taught.get("avoid", ())]
            if taught.get("prefer") in options:
                return taught["prefer"]
        if not options:
            return None
        fixes = [r for r in options if r != "escalate"]
        if not fixes:                           # escalation only after every real remedy was tried
            return "escalate"
        options = fixes
        stats = self.data["playbook"].get(kind, {})
        for r in options:                       # exploration in the safe default order
            if stats.get(r, {}).get("tries", 0) < MIN_TRIES:
                return r
        def draw(r):                            # [FIX-C16] Thompson sampling
            s = stats[r]
            a = s.get("a", s["resolved"])
            b = s.get("b", s["tries"] - s["resolved"])
            return self.rng.betavariate(1 + a, 1 + b)
        return max(options, key=draw)

    def learn(self, kind, remedy, resolved, seconds, scale=None):
        """[FIX-C29] user decision 2026-11-17: a smooth game wastes no time, so a fix scores by how fast it worked:
        reward = scale / (scale + seconds) (1 at once, 1/2 after one stall threshold); the rest counts as failure."""
        if remedy in (None, "escalate"):   # recording for the operator is not a fix: never scored
            return
        self._op(op="learn", kind=kind, remedy=remedy, resolved=bool(resolved), seconds=round(float(seconds), 1),
                 **({"scale": round(float(scale), 1)} if scale else {}))
        book = self.data["playbook"].setdefault(kind, {})
        for name, other in book.items():        # [FIX-C16] every new outcome ages the old ones
            if not name.startswith("_"):
                other["a"] = round(other.get("a", other["resolved"]) * DECAY, 4)
                other["b"] = round(other.get("b", other["tries"] - other["resolved"]) * DECAY, 4)
        s = book.setdefault(remedy, {"tries": 0, "resolved": 0, "seconds": 0.0, "a": 0.0, "b": 0.0})
        s["tries"] += 1
        if resolved:
            reward = scale / (scale + max(0.0, float(seconds))) if scale else 1.0   # [FIX-C29]
            s["resolved"] += 1
            s["a"] = round(s.get("a", 0) + reward, 4)
            s["b"] = round(s.get("b", 0) + 1 - reward, 4)
            s["seconds"] += round(float(seconds), 1)
        else:
            s["b"] = round(s.get("b", 0) + 1, 4)
            s["wasted"] = round(s.get("wasted", 0.0) + max(0.0, float(seconds)), 1)   # [FIX-C29] what the failure cost

    def norm(self, stage, seconds):
        self._op(op="norm", stage=stage, seconds=round(float(seconds), 1))
        rows = self.data["norms"].setdefault(stage, [])
        rows.append(round(float(seconds), 1))
        del rows[:-NORM_KEEP]

    def typical(self, stage):
        """[FIX-C29] how long this stage normally takes here (median); None until there is data."""
        rows = self.data["norms"].get(stage) or []
        return statistics.median(rows) if len(rows) >= 5 else None

    def time_score(self, game_id, actual, typical, wasted, causes):
        """[FIX-C29][FIX-C31] my score for a smooth game. User 2026-11-17: no responsibility without authority, so
        only time held by incidents I detected and handled counts (100 = none); slower stages without an incident
        (model speed, players thinking) are outside my authority: recorded, never scored or learned."""
        row = {"game_id": game_id, **authority_time(actual, typical, wasted, causes)}
        self._op(op="time_score", row=row)
        rows = self.data.setdefault("time_scores", [])
        rows.append(row)
        del rows[:-200]
        return row

    def rescore_time(self):
        """[FIX-C31] older rows (C29 score = typical/actual) rescored by the authority rule; old score kept."""
        for row in self.data.get("time_scores") or []:
            if "accountable_s" not in row:
                old = row.get("score")
                row.update(authority_time(row.get("actual_s", 0), row.get("typical_s", 0), row.get("wasted_s", 0),
                                          row.get("causes") or {}), score_c29=old)

    def stall_threshold(self, stage):
        rows = self.data["norms"].get(stage) or []
        if len(rows) < 5:
            return DEFAULT_STALL
        p90 = sorted(rows)[int(len(rows) * 0.9) - 1]
        return max(STALL_FLOOR, min(STALL_CEIL, 2 * p90))

    # ---------- [FIX-C20] model calls ----------
    def model_call(self, seconds):
        self._op(op="model_call", seconds=round(float(seconds), 1))
        rows = self.data.setdefault("model_calls", [])
        rows.append(round(float(seconds), 1))
        del rows[:-MODEL_KEEP]

    def model_deadline(self):
        rows = self.data.get("model_calls") or []
        if len(rows) < MODEL_MIN_DATA:
            return MODEL_CEIL
        p90 = sorted(rows)[int(len(rows) * 0.9) - 1]
        return max(MODEL_FLOOR, min(MODEL_CEIL, 4 * p90))

    def escalate(self, kind, subject, stage, context=None):
        """[FIX-C16] An operator hand-off says what was tried and why it is handed over (textbook II
        ch13, escalation design): tried remedies, how long nothing moved, the progress signals."""
        row = {"at": round(time.time()), "kind": kind, "subject": subject, "stage": stage, **(context or {})}
        self._op(op="escalate", row=row)
        self.data["escalations"].append(row)
        del self.data["escalations"][:-200]
        self.save()
        trace({"source": "gm", "type": "escalation", "agent_id": self.agent_id, **row})
        return row

    def record_game(self, summary):
        self._op(op="game", summary=summary)
        self.data["games"].append(summary)
        self.save()


def reduce_view(view):
    """[FIX-C16] The parts of the GM view the watch reads: enough to replay a diagnosis."""
    return {"phase": view.get("phase"), "stage": view.get("stage"), "phase_epoch": view.get("phase_epoch"),
            "status": view.get("status"), "active_agents": list(view.get("active_agents") or []),
            "spent": sorted(view.get("spent") or {}), "needs_request": list(view.get("needs_request") or []),
            "overdue": list(view.get("overdue") or []),
            "questions": [{"pending": True, "target_agent": q.get("target_agent") or q.get("target_id")}
                          for q in view.get("questions") or [] if q.get("pending")],
            "ready_to_release": bool(view.get("ready_to_release")),
            "ready_to_advance": bool(view.get("ready_to_advance"))}


def expand_view(reduced):
    """Inverse of reduce_view for replay: 'spent' back to a mapping."""
    return dict(reduced, spent={a: 1 for a in reduced.get("spent") or []})


class IncidentWatch:
    """The GM's in-game memory: per-player progress cards and the open incidents. Pure logic on
    the GM view, so the decisions are testable without an engine."""

    def __init__(self, memory, clock=time.time):
        self.memory, self.clock = memory, clock
        self.cards = {}            # agent id -> {"requests", "actions", "response_seconds", "incidents"}
        self.open = {}             # incident key -> {"kind", "subject", "since", "remedy", "remedy_at", "tried"}
        self.closed = []
        self.stage_key = None
        self.stage_since = None
        self.progress = None
        self.progress_at = None
        self.requested_at = {}     # agent id -> when the GM issued its open request
        self.engine_errors = 0
        self.recent = []           # [FIX-C16] (t, reduced view) for regression traces
        self.time = {"actual": 0.0, "typical": 0.0, "wasted": 0.0}   # [FIX-C29] stage seconds this game
        self.causes = {}           # [FIX-C29] incident kind -> seconds it held the game

    def card(self, aid):
        return self.cards.setdefault(aid, {"requests": 0, "actions": 0, "response_seconds": [], "incidents": 0})

    # ---------- what the GM did ----------
    def requested(self, ids):
        now = self.clock()
        for aid in ids or []:
            self.card(aid)["requests"] += 1
            self.requested_at[aid] = now

    def acted(self, aid):
        c = self.card(aid)
        c["actions"] += 1
        t = self.requested_at.pop(aid, None)
        if t is not None:
            c["response_seconds"].append(round(self.clock() - t, 1))

    # ---------- a paused game (FIX-I11) ----------
    def paused_for(self, seconds):
        """The game was paused (model outage) for this long: that time is not a stall, not a
        stage duration and not a player's response time."""
        s = max(0.0, float(seconds or 0))
        if not s:
            return
        if self.progress_at is not None:
            self.progress_at += s
        if self.stage_since is not None:
            self.stage_since += s
        for inc in self.open.values():
            inc["since"] += s
            inc["remedy_at"] += s
        for aid in self.requested_at:
            self.requested_at[aid] += s

    # ---------- watching ----------
    def observe(self, view):
        """Called every GM tick with the GM view. Returns remedies to apply now:
        [(incident key, remedy)]."""
        now = self.clock()
        reduced = reduce_view(view)                     # [FIX-C16] keep only changes (GM ticks ~10/s)
        if not self.recent or self.recent[-1][1] != reduced:
            self.recent.append((round(now, 1), reduced))
            del self.recent[:-TRACE_VIEWS]
        if view.get("status") == "paused":          # [FIX-I11] nothing can move while paused
            if self.progress_at is not None:
                self.progress_at = now
            return []
        stage = f"{view.get('phase')}:{view.get('stage') or '-'}"
        key = (view.get("phase_epoch"), stage)
        if key != self.stage_key:
            if self.stage_key is not None and self.stage_since is not None:
                took = now - self.stage_since
                usual = self.memory.typical(self.stage_key[1])                     # [FIX-C29] before this one counts
                self.time["actual"] += took
                self.time["typical"] += took if usual is None else min(took, usual)
                if usual is not None and took > usual:
                    self.time["wasted"] += took - usual
                self.memory.norm(self.stage_key[1], took)
            self.stage_key, self.stage_since = key, now
            self.progress, self.progress_at = None, now
        spent = view.get("spent") or {}
        pending_q = [q for q in view.get("questions") or [] if q.get("pending")]
        signature = (len(spent), len(pending_q), tuple(view.get("needs_request") or ()),
                     bool(view.get("ready_to_release")), bool(view.get("ready_to_advance")))
        if signature != self.progress:
            self.progress, self.progress_at = signature, now
            self._resolve_all(now, reason="progress")
            return []
        quiet = now - self.progress_at
        threshold = self.memory.stall_threshold(stage)
        if quiet < threshold:
            return self._follow_up(now, threshold)
        kind, subject = self._diagnose(view, spent, pending_q)
        if kind is None:
            return self._follow_up(now, threshold)
        ikey = f"{view.get('phase_epoch')}:{kind}"
        if ikey not in self.open:
            remedy = self.memory.choose(kind)
            self.open[ikey] = {"kind": kind, "subject": subject, "stage": stage, "since": now,
                               "remedy": remedy, "remedy_at": now, "tried": [remedy],
                               "view": reduce_view(view), "threshold": threshold}   # [FIX-C16] for replay
            for aid in subject or []:
                self.card(aid)["incidents"] += 1
            return [(ikey, remedy)]
        return self._follow_up(now, threshold)

    def _diagnose(self, view, spent, pending_q):
        active = view.get("active_agents") or []
        needs = view.get("needs_request") or []
        if view.get("ready_to_advance") or view.get("ready_to_release"):
            return "not_advancing", []
        if needs:
            return "request_not_issued", list(needs)
        silent = [a for a in active if a not in spent and a not in needs]
        if silent:
            return "no_action", silent
        if pending_q and not view.get("overdue"):
            return "answer_pending", [q.get("target_agent") or q.get("target_id") for q in pending_q]
        return None, None

    def _follow_up(self, now, threshold):
        """An applied remedy that did not help within half a threshold is recorded as failed and the
        next remedy is chosen (never the same one twice for one incident)."""
        out = []
        for ikey, inc in self.open.items():
            if inc["remedy"] and now - inc["remedy_at"] >= threshold / 2:
                self.memory.learn(inc["kind"], inc["remedy"], False, now - inc["remedy_at"])   # [FIX-C29] its cost
                nxt = self.memory.choose(inc["kind"], exclude=inc["tried"])
                inc.update(remedy=nxt, remedy_at=now)
                if nxt:
                    inc["tried"].append(nxt)
                    out.append((ikey, nxt))
        return out

    def escalation_context(self, ikey, why):
        """[FIX-C16] What the operator needs to act without re-reading the game."""
        inc = self.open.get(ikey) or {}
        now = self.clock()
        return {"why": why, "tried": [r for r in inc.get("tried") or [] if r and r != "escalate"],
                "open_seconds": round(now - inc["since"], 1) if inc.get("since") else None,
                "quiet_seconds": round(now - self.progress_at, 1) if self.progress_at else None,
                "threshold": self.memory.stall_threshold(inc.get("stage") or ""),
                "signals": self.recent[-1][1] if self.recent else None}

    def _trace_incident(self, inc, outcome):
        trace({"source": "gm", "type": "incident", "kind": inc["kind"], "stage": inc.get("stage"),
               "subject": inc.get("subject"), "tried": inc.get("tried"), "outcome": outcome,
               "opened_at": round(inc["since"], 1), "threshold": inc.get("threshold"),
               "open_view": inc.get("view"),
               "views": [(t, v) for t, v in self.recent if t <= inc["since"]]})

    def _resolve_all(self, now, reason):
        for ikey, inc in list(self.open.items()):
            if inc["remedy"]:
                self.memory.learn(inc["kind"], inc["remedy"], True, now - inc["remedy_at"], inc.get("threshold"))
            self.closed.append(dict(inc, resolved_by=inc["remedy"], seconds=round(now - inc["since"], 1), how=reason))
            self.causes[inc["kind"]] = round(self.causes.get(inc["kind"], 0) + now - inc["since"], 1)   # [FIX-C29]
            if inc["kind"] != "engine_error":
                self._trace_incident(inc, f"resolved_by:{inc['remedy']}")
            del self.open[ikey]
        if self.closed and reason == "progress":
            self.memory.save()

    # ---------- transient engine errors ----------
    def engine_error(self, exc):
        """Returns the remedy for one more transient error, or 'escalate' when it keeps failing."""
        self.engine_errors += 1
        ikey = "engine_error"
        now = self.clock()
        inc = self.open.get(ikey)
        if inc is None:
            remedy = self.memory.choose("engine_error")
            inc = self.open[ikey] = {"kind": "engine_error", "subject": [str(exc)[:120]], "stage": str(self.stage_key),
                                     "since": now, "remedy": remedy, "remedy_at": now, "tried": [remedy]}
        elif self.engine_errors % 3 == 0:     # this remedy did not stop the errors: next one
            self.memory.learn("engine_error", inc["remedy"], False, 0)
            nxt = self.memory.choose("engine_error", exclude=inc["tried"]) or "escalate"
            inc.update(remedy=nxt, remedy_at=now)
            inc["tried"].append(nxt)
        if self.engine_errors >= ENGINE_ERROR_LIMIT:
            trace({"source": "gm", "type": "incident", "kind": "engine_error", "subject": inc.get("subject"),
                   "tried": inc.get("tried"), "outcome": "escalate", "errors": self.engine_errors,
                   "views": list(self.recent)})
            return "escalate"
        return inc["remedy"]

    def engine_ok(self):
        if self.engine_errors:
            self.engine_errors = 0
            inc = self.open.pop("engine_error", None)
            if inc:
                now = self.clock()
                self.memory.learn("engine_error", inc["remedy"], True, now - inc["remedy_at"])
                self.closed.append(dict(inc, resolved_by=inc["remedy"], seconds=round(now - inc["since"], 1), how="engine_ok"))

    def refused(self, op, code):
        """A GM command refused by the engine (conflict / pending): remembered per operation."""
        kind = "command_refused"
        stats = self.memory.data["playbook"].setdefault(kind, {})
        stats.setdefault("_by_op", {}).setdefault(f"{op}:{code}", 0)
        stats["_by_op"][f"{op}:{code}"] += 1

    def close_game(self):
        """[FIX-C16] Incidents still open when the game ends go to the regression traces too."""
        for inc in self.open.values():
            self._trace_incident(inc, "open_at_end")

    def summary(self):
        incidents = self.closed + [dict(i, resolved_by=None, how="open_at_end") for i in self.open.values()]
        incidents = [{k: v for k, v in i.items() if k != "view"} for i in incidents]
        by_kind = {}
        for i in incidents:
            k = by_kind.setdefault(i["kind"], {"count": 0, "resolved": 0, "remedies": {}})
            k["count"] += 1
            k["resolved"] += int(i.get("resolved_by") is not None)
            for r in i.get("tried") or []:
                if r:
                    k["remedies"][r] = k["remedies"].get(r, 0) + 1
        response = [s for c in self.cards.values() for s in c["response_seconds"]]
        t = self.time
        return {"incidents": by_kind,
                "time": authority_time(t["actual"], t["typical"], t["wasted"], dict(self.causes)),   # [FIX-C29][FIX-C31]
                "players": {aid: {k: (round(statistics.median(v), 1) if k == "response_seconds" and v else v)
                                  for k, v in c.items() if k != "response_seconds" or v}
                            for aid, c in self.cards.items()},
                "median_response_seconds": round(statistics.median(response), 1) if response else None}
