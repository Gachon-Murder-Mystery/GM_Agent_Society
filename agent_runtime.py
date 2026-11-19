"""One persistent pool agent: player or GM, whichever role the engine gives it now.

`AgentWorker` polls `pool/session`. As a player it runs a player runtime; when the
engine promotes it to GM it drops the player runtime entirely (spec B8, no memory
carried) and runs `CompetitiveGMWorker`.

The player runtime is B's slot. Contract (V13_ALIGNMENT_20261026.md section 4):

    factory(role_client, session, catalog) -> object with step() -> str

`step()` must be non-blocking: observe, answer pending questions from
`response_options`, submit at most one `agent/action` for the current phase and
send `agent/heartbeat` well inside the 40 s lease. The engine rolls the dice for
the submitted action; the GM continues the scenario from the result.

`ScriptedPlayerRuntime` is the reference implementation with C's scripted policy
(no LLM). It is the stand-in until B's runtime is plugged in.
"""
import hashlib
import importlib
import json
import uuid
from pathlib import Path

from .competitive_gm import CompetitiveGMWorker
from .competitive_player import CompetitivePlayer

_PLAYER = json.loads(Path(__file__).with_name("competitive_settings.json").read_text(encoding="utf-8"))["player_runtime"]
REJECTED = ("disclosure_required", "untruthful", "invalid_input")


def _code(exc):
    return getattr(exc, "code", None)


def legal_statements(answer, question):
    """Keep only statements inside the engine's current response domain (narration from its grammar)."""
    options, result = question["response_options"], []
    for s in answer.get("statements", []):
        if s.get("kind") == "definite" and s.get("clue_id") in options["definite"]:
            result.append({"kind": "definite", "clue_id": s["clue_id"]})
        elif s.get("kind") == "ambiguous" and isinstance(s.get("pair"), dict) and any(
                x["pair"] == dict(sorted(s["pair"].items())) for x in options["ambiguous"]):  # [FIX-C11] pairs (A18)
            result.append({"kind": "ambiguous", "pair": dict(sorted(s["pair"].items()))})
        elif s.get("kind") == "indirect":
            option = next((x for x in options["indirect"] if x["clue_id"] == s.get("clue_id")), None)
            if option:
                text = s.get("narration", s.get("text"))
                result.append({"kind": "indirect", "clue_id": s["clue_id"],
                               "text": text if text in option["narrative_options"] else option["narrative_options"][0]})
    return result


def required_statements(question):
    """Smallest answer that meets the success-tier disclosure (spec A6) from the engine's options."""
    options, n = question["response_options"], question["successes"]
    definite = [{"kind": "definite", "clue_id": clue} for clue in options["definite"][:1]]
    vague = [{"kind": "ambiguous", "pair": dict(x["pair"])} for x in options["ambiguous"]]  # [FIX-C11]
    vague += [{"kind": "indirect", "clue_id": x["clue_id"], "text": x["narrative_options"][0]} for x in options["indirect"]]
    extra = question.get("extra_vague", 0)  # [FIX-C12] engine A19
    if n <= 0:
        return []
    if n == 1:
        return vague[:1 + extra] or definite
    if n == 2:
        return (definite + vague[:extra]) if definite else vague[:2 + extra]
    return definite + vague[:2 + extra]


class ScriptedPlayerRuntime:
    """Reference player runtime: engine communication around C's scripted CompetitivePlayer."""

    live_llm = False

    def __init__(self, role, session, catalog):
        self.role, self.catalog = role, catalog
        self.seed = int(hashlib.sha256(f"player:{session['game_id']}:{session['agent_id']}".encode()).hexdigest()[:8], 16)
        self.policy, self.applied = None, set()
        self.fallbacks, self.idle = 0, 0
        self.pending = {}  # [FIX-C8] action_id -> payload of room searches settled at phase end

    def _update(self, obs):
        me = obs["self"]
        if self.policy is None:
            sheet = next(c for c in self.catalog["characters"] if c["id"] == me["character_id"])
            self.policy = CompetitivePlayer(me["id"], sheet, me["mbti"], self.seed, missions=obs["sub_missions"],
                                            names={c["id"]: c["name"] for c in self.catalog["characters"]},
                                            places={p["id"]: p["name"] for p in self.catalog["places"]})
        for info in obs["information"]:
            marker = (info["id"], info["kind"], info.get("text"))
            if marker not in self.applied:
                self.policy.ingest({"clues": [info]})
                self.applied.add(marker)
        # A narrated clue can later arrive in full; answer only with clues held in full.
        self.policy.held = [x for x in obs["information"] if x["kind"] in ("definite", "indirect")]
        simple = {"room": me["room"], "cycle": obs["cycle_id"],
                  "relationship": {int(k): v for k, v in obs["relationships"].items()},
                  "achieved_missions": [m["mission_id"] for m in obs["sub_missions"] if m["achieved"]],
                  "here": [{"id": c["id"], "kind": "player" if c["mode"] == "active" else c["mode"]} for c in obs["occupants"]]}
        self.policy.observe(simple)
        return simple

    def _answer(self):
        for question in self.role.call("questions/inbox")["questions"]:
            answer = self.policy.respond(question | {"asker_id": question["asker_character"]})
            try:
                self.role.call("questions/respond", question_id=question["question_id"], response_id=uuid.uuid4().hex,
                               statements=legal_statements(answer, question))
            except Exception as exc:
                if _code(exc) == "expired":
                    # The engine owns timeout settlement; never retry a closed question.
                    continue
                if _code(exc) not in REJECTED:
                    raise
                self.fallbacks += 1  # The policy's form did not meet the tier; disclose the required minimum.
                try:
                    self.role.call("questions/respond", question_id=question["question_id"], response_id=uuid.uuid4().hex,
                                   statements=required_statements(question))
                except Exception as retry_exc:
                    if _code(retry_exc) != "expired":
                        raise


    def _choose(self, obs, simple):
        if obs["phase"] == "movement":
            choice = self.policy.move(simple)
            return choice["action"], ({"room": choice["room"]} if choice["action"] == "move" else {})
        if obs["phase"] == "interaction":
            choice = self.policy.interact(simple)
            if choice["action"] == "explore":
                target = choice.get("target", "room")
                return "explore", ({} if target == "room" else {"target_id": int(target.split(":")[1])})
            if choice["action"] == "question":
                return "question", {k: choice[k] for k in ("target_id", "approach", "hypothesis")}
            return choice["action"], {}
        decision = self.policy.deduce()
        if decision["decision"] == "challenge":
            return "accuse", decision["accusation"] | {"output_tokens": decision["reasoning_tokens"]}
        return "pass", {}

    def step(self):
        obs = self.role.call("agent/observe")
        if obs["ended"] or obs["self"]["mode"] != "active":
            return "inactive"
        simple = self._update(obs)
        self._collect()
        self._answer()
        if not obs["remaining_actions"]:
            self.idle += 1
            if self.idle % _PLAYER["heartbeat_every_idle_steps"] == 0:
                self.role.call("agent/heartbeat")
            return "waiting"
        self.idle = 0
        action, payload = self._choose(obs, simple)
        request = {k: obs[k] for k in ("game_id", "cycle_id", "phase", "phase_epoch")}
        request.update(agent_id=obs["self"]["id"], action_id=uuid.uuid4().hex, action=action, payload=payload)
        result = self.role.call("agent/action", request=request)
        if action == "explore" and result["execution_status"] == "completed":
            self._ingest_explore(result, payload)
        elif action == "explore" and result["execution_status"] in ("pending", "held"):  # [FIX-C12] simultaneous holds actions
            self.pending[request["action_id"]] = payload
        return action

    def _ingest_explore(self, result, payload):
        # The engine rolled; the result carries the dice for this player's own belief update.
        self.policy.ingest({"action": "explore", "target": "corpse" if payload else "room",
                            "clues": result["acquired_information"], "successes": result["roll"]["successes"]})

    def _collect(self):
        """[FIX-C8] Room searches now settle at the end of the interaction phase (engine FIX-A14).
        Without this the policy never saw an empty room and searched forever."""
        for action_id, payload in list(self.pending.items()):
            result = self.role.call("agent/result", action_id=action_id)
            if result["execution_status"] == "completed":
                self.pending.pop(action_id)
                self._ingest_explore(result, payload)


def load_factory(spec):
    """`package.module:callable` -> player factory (B's runtime); None -> scripted stand-in."""
    if not spec:
        return ScriptedPlayerRuntime
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


class AgentWorker:
    """Engine-assigned role of one pool member, stepped by a runner."""

    def __init__(self, identity, player_factory=None):
        self.identity = identity
        self.factory = player_factory or ScriptedPlayerRuntime
        self.catalog = identity.call("catalog")
        self.gm = CompetitiveGMWorker(identity)
        self.player, self.context = None, None
        self.runtimes = []  # Every player runtime this member had, for the run summary.

    def step(self):
        if self.gm.gm is not None and not self.gm.gm.reported:
            return "gm:" + self.gm.step()  # A finished term still owes its REPORT.
        session = self.identity.call("pool/session")
        if session["role"] == "gm":
            self.player, self.context = None, None  # B8: no player context carried into the GM term.
            return "gm:" + self.gm.step()
        if session["role"] == "disconnected":   # [FIX-B69] cut for now: keep my in-game memory for when I am back
            return "disconnected"
        if session["role"] != "player":
            self.player, self.context = None, None
            return session["role"]
        if self.context != session["context_key"]:
            # [FIX-C1] The player runtime belongs to this persistent pool member, so it gets the
            # member's own pool credential to read/write its cross-game learning
            # (pool/learning/get|update, pool/feedback). Game secrets stay behind role tokens.
            session = dict(session, pool_credentials=dict(self.identity.credentials))
            self.player = self.factory(self.identity.bind(**session["credentials"]), session, self.catalog)
            self.context = session["context_key"]
            self.runtimes.append(self.player)
        try:
            return "player:" + self.player.step()
        except Exception as exc:
            # [FIX-C25] 2026-11-16 v11 fault seed 101: a player whose lease expired is turned into an NPC by the
            # engine (the designed fault path), but its runtime kept calling with the old credential and the 401
            # ended the whole game. If the seat is really gone, stop playing it; any other error still surfaces.
            if getattr(exc, "code", None) not in ("unauthorized", "expired"):
                raise
            now = self.identity.call("pool/session")
            if now["role"] == "player" and now.get("context_key") == self.context:
                raise
            if now["role"] != "disconnected":   # [FIX-B69] a cut seat may come back: keep the runtime
                self.player, self.context = None, None
            return "player:seat_lost"
