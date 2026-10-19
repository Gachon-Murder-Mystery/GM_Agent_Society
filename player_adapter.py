"""C-owned boundary for B's sync/decide/ingest_result API.

Accept an already constructed B instance. No import rewriting, belief repair,
decision fallback, engine execution or automatic publication is performed.
Candidate IDs come from the official observation, never B's fake catalog.
"""
import copy
import uuid

from .contracts import ActionRequest, ContractError
from .team_adapters import IntegrationUnavailable


CONTEXT = ("game_id", "cycle_id", "phase", "phase_epoch")
POOLS = {"suspect": "suspects", "weapon": "weapons", "place": "places"}


def information_for_b(items):
    projected = []
    for item in items:
        if not isinstance(item, dict) or item.get("category") not in POOLS:
            raise ContractError("expected engine-confirmed exclusion information")
        if item.get("redacted") is True and isinstance(item.get("id"), str) and "excluded_id" not in item:
            # Engine v1 partial disclosure: never an exclusion basis, so no excludes key.
            projected.append({"id": item["id"], "category": item["category"], "redacted": True,
                              "text": copy.deepcopy(item.get("text", "")),
                              "kind": "location" if item["category"] == "place" else item["category"]})
            continue
        if not isinstance(item.get("id"), str) or "excluded_id" not in item or "text" not in item:
            raise ContractError("incomplete exclusion information")
        info = {key: copy.deepcopy(item[key]) for key in ("id", "category", "excluded_id", "text")}
        info.update(kind="location" if info["category"] == "place" else info["category"],
                    excludes=info["excluded_id"])
        projected.append(info)
    return projected


class ReasoningPlayerAdapter:
    def __init__(self, agent):
        missing = [name for name in ("sync", "decide", "ingest_result") if not callable(getattr(agent, name, None))]
        if missing:
            raise IntegrationUnavailable("B must provide sync/decide/ingest_result", missing)
        if not isinstance(getattr(agent, "agent_id", None), str) or not agent.agent_id:
            raise ContractError("B agent_id must use the agreed string actor ID")
        self.agent, self.agent_id = agent, agent.agent_id
        self.requests, self.receipts, self.disclosure_proposals = {}, {}, {}
        self.last_input = None
        self.game_id = None
        self.held_ids = set()
        self.actor_mode = None

    def _inputs(self, notice, observation):
        if notice.get("agent_id") != self.agent_id or observation.get("self", {}).get("id") != self.agent_id:
            raise ContractError("notice or observation belongs to another player")
        for key in CONTEXT:
            if key not in notice or observation.get(key) != notice[key]:
                raise ContractError(f"notice and observation disagree on {key}")
        if self.game_id is not None and notice["game_id"] != self.game_id:
            raise ContractError("one adapter instance belongs to one game")
        if observation.get("ended") or observation["self"].get("mode") != "active":
            raise ContractError("only an active player in an open game may choose an action")
        if notice.get("self") != observation["self"]:
            raise ContractError("notice and observation disagree on character state")
        if not isinstance(notice.get("task_id"), str) or not notice["task_id"]:
            raise ContractError("GM task_id required")
        # Let ActionRequest validate context before invoking B or the LLM.
        allowed = notice.get("allowed_actions", [])
        if not allowed:
            raise ContractError("GM allowed_actions required")
        for action in allowed:
            ActionRequest(**{key: notice[key] for key in CONTEXT}, agent_id=self.agent_id,
                          action_id="context-check", task_id=notice["task_id"], action=action)
        pools = observation.get("candidates", {})
        if set(pools) != set(POOLS):
            raise ContractError("expected agreed suspect/weapon/place candidate pools")
        # Retain A's IDs and labels; only rename the container keys.
        candidates = {POOLS[key]: [{"id": copy.deepcopy(item["id"]), "name": item["name"]}
                                   for item in pools[key]] for key in POOLS}
        safe_notice = {key: copy.deepcopy(notice[key]) for key in
                       (*CONTEXT, "agent_id", "task_id", "allowed_actions", "remaining_actions")
                       if key in notice}
        own = {key: copy.deepcopy(observation["self"][key]) for key in
               ("id", "mode", "room", "job", "stat", "personality", "personal_goal", "prepared")
               if key in observation["self"]}
        safe_notice["self"] = own
        if "positions" in notice:
            safe_notice["positions"] = [{key: copy.deepcopy(actor[key]) for key in
                                         ("id", "mode", "room", "origin", "name") if key in actor}
                                        for actor in notice["positions"]]
        safe_notice["occupants"] = [{key: copy.deepcopy(actor[key]) for key in
                                     ("id", "mode", "room", "origin", "name") if key in actor}
                                    for actor in observation.get("occupants", [])]
        shared = information_for_b(observation.get("shared_information", []))
        safe_notice["shared_information"] = shared
        safe_observation = {**{key: observation[key] for key in CONTEXT},
                            "self": copy.deepcopy(own), "candidates": candidates,
                            "occupants": copy.deepcopy(safe_notice["occupants"]),
                            "information": information_for_b(observation.get("information", [])),
                            "shared_information": copy.deepcopy(shared)}
        return safe_notice, safe_observation

    def choose_action(self, phase_notice, observation):
        notice, obs = self._inputs(phase_notice, observation)
        task = notice["task_id"]
        if task in self.requests:
            previous = self.requests[task]
            if any(getattr(previous, key) != notice[key] for key in CONTEXT):
                raise ContractError("task_id reused in another phase")
            if previous.action_id in self.receipts:
                raise ContractError("task already completed")
            return copy.deepcopy(previous)  # Same unconfirmed request, no second B decision.
        self.last_input = copy.deepcopy((notice, obs))
        self.game_id = notice["game_id"]
        self.actor_mode = obs["self"]["mode"]
        self.held_ids = {item["id"] for item in obs["information"]}
        try:
            self.agent.sync(notice, obs)
            decision = self.agent.decide(notice, obs)
        except (AttributeError, TypeError, ValueError) as exc:
            raise IntegrationUnavailable(f"B sync/decide failed: {type(exc).__name__}: {exc}") from exc
        # B d06c650+ returns (action, payload); the earlier 3-value form adds a disclosure proposal.
        if not isinstance(decision, tuple) or len(decision) not in (2, 3):
            raise ContractError("B decide must return (action, payload[, disclosure])")
        action, payload, disclose = decision if len(decision) == 3 else (*decision, [])
        if action not in notice["allowed_actions"] or not isinstance(payload, dict):
            raise ContractError("B returned an unsupported action or payload")
        payload = copy.deepcopy(payload)
        if action == "question":
            if ("target" in payload) == ("target_id" in payload):
                raise ContractError("B question must provide one target field")
            if "target" in payload:
                payload["target_id"] = payload.pop("target")
        if action == "move" and "room" not in payload:
            raise ContractError("B move has no destination; B must choose a valid fallback")
        if not isinstance(disclose, list):
            raise ContractError("B disclosure proposal must be a list")
        request = ActionRequest(**{key: notice[key] for key in CONTEXT}, agent_id=self.agent_id,
                                action_id=uuid.uuid4().hex, task_id=task, action=action, payload=payload)
        self.requests[task] = copy.deepcopy(request)
        self.disclosure_proposals[request.action_id] = copy.deepcopy(disclose)
        # Optional disclosure is a proposal only; do not invoke a sharing endpoint.
        return request

    def receive_result(self, result):
        request = self.requests.get(result.get("task_id"))
        if request is None or any(result.get(key) != getattr(request, key) for key in
                                  (*CONTEXT, "agent_id", "action_id", "task_id", "action")):
            raise ContractError("result does not match this player's submitted action")
        if result.get("execution_status") != "completed" or result.get("consumed") is not True:
            raise ContractError("pending or rejected action is not a completed result")
        safe = {key: copy.deepcopy(result[key]) for key in
                (*CONTEXT, "agent_id", "action_id", "task_id", "action", "execution_status", "consumed",
                 "outcome", "roll", "room", "actor_mode", "target_id", "held_information_ids", "resolution")
                if key in result}
        safe["acquired_information"] = information_for_b(result.get("acquired_information", []))
        if safe.get("actor_mode") not in ("active", "npc", "corpse"):
            raise ContractError("official actor_mode required")
        held = safe.get("held_information_ids")
        if not isinstance(held, list) or any(not isinstance(item, str) for item in held):
            raise ContractError("official held information IDs required")
        if any(item["id"] not in held for item in safe["acquired_information"]):
            raise ContractError("acquired information missing from the official inventory")
        if request.action_id in self.receipts:
            if self.receipts[request.action_id] != safe:
                raise ContractError("completed result changed for the same action_id")
            return
        try:
            self.agent.ingest_result(safe)
        except (AttributeError, TypeError, ValueError) as exc:
            raise IntegrationUnavailable(f"B ingest_result failed: {type(exc).__name__}: {exc}") from exc
        self.receipts[request.action_id] = copy.deepcopy(safe)
        self.held_ids = set(held)
        self.actor_mode = safe["actor_mode"]

    def answer_question(self, question):
        """Normal-player response for A's future router; not an active game action."""
        if question.get("game_id") != self.game_id or question.get("target_id") != self.agent_id:
            raise ContractError("question routed to another game or player")
        if self.actor_mode != "active":
            raise ContractError("NPC and corpse responses belong to the engine's passive policy")
        if not isinstance(question.get("question_id"), str) or not question["question_id"]:
            raise ContractError("question_id required")
        if not isinstance(question.get("asker_id"), str) or question["asker_id"] == self.agent_id:
            raise ContractError("another player's asker_id required")
        respond = getattr(self.agent, "respond_to_question", None)
        if not callable(respond):
            raise IntegrationUnavailable("B question response capability unavailable")
        offer, refused = respond(question["asker_id"], question.get("topic"))
        if type(refused) is not bool or not isinstance(offer, list) or any(item not in self.held_ids for item in offer):
            raise ContractError("B response must offer only owned information IDs")
        return {"question_id": question["question_id"], "game_id": self.game_id,
                "asker_id": question["asker_id"], "target_id": self.agent_id,
                "offer_ids": [] if refused else copy.deepcopy(offer), "refused": refused}
