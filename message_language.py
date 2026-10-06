"""Meaning-preserving projections between the native demo and C's draft wire format.

This module translates data, not engine authority or game rules. Caller-supplied
phase/actor/correlation fields are never inferred from a legacy observation.
Unsupported commands and unknown fields require an explicit mapping review.
"""
import copy

from .contracts import ActionRequest, ContractError


LIMITS = {"suspect": 12, "weapon": 8, "place": 8}


def index(value, category):
    if type(value) is not int or not 0 <= value < LIMITS[category]:
        raise ContractError(f"invalid {category} candidate ID: {value!r}")
    return value


def exact_keys(value, required, optional=()):
    if not isinstance(value, dict):
        raise ContractError("message must be an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ContractError("missing or unmapped fields; review the message contract")


def npc_actor_id(candidate_id):
    """Only map the original NPC catalog, never player/corpse identifiers."""
    return f"npc:{index(candidate_id, 'suspect')}"


def npc_candidate_id(actor_id):
    if not isinstance(actor_id, str) or not actor_id.startswith("npc:"):
        raise ContractError("legacy target cannot represent a player or corpse")
    try:
        candidate_id = index(int(actor_id[4:]), "suspect")
    except (ValueError, ContractError) as error:
        raise ContractError("invalid legacy NPC actor ID") from error
    if actor_id != npc_actor_id(candidate_id):
        raise ContractError("NPC actor ID must use canonical decimal notation")
    return candidate_id


def intent_to_request(intent, context):
    """Project only move/prepare/accuse intent, with explicit C-owned context.

    check/roll form a pending operation; find may combine moving and checking;
    talk need not acquire information. None is silently renamed to question.
    """
    required = ("game_id", "agent_id", "action_id", "cycle_id", "phase", "phase_epoch")
    exact_keys(context, required, ("task_id",))
    if not isinstance(intent, dict):
        raise ContractError("intent must be an object")
    kind = intent.get("kind")
    if kind == "move":
        exact_keys(intent, ("kind", "place"))
        action, payload = "move", {"room": index(intent["place"], "place")}
    elif kind == "prepare":
        exact_keys(intent, ("kind",))
        action, payload = "prepare", {}
    elif kind == "accuse":
        exact_keys(intent, ("kind", "suspect", "weapon", "place"))
        action = "accuse"
        payload = {key: index(intent[key], key) for key in LIMITS}
    else:
        raise ContractError(f"no equivalent single C action for legacy kind={kind!r}")
    return ActionRequest(**copy.deepcopy(context), action=action, payload=payload)


def request_to_intent(request):
    """Return a legacy command body; native A does not enforce the envelope."""
    if not isinstance(request, ActionRequest):
        request = ActionRequest.from_dict(request)
    payload = request.payload
    if request.action == "move":
        exact_keys(payload, ("room",))
        return {"kind": "move", "place": index(payload["room"], "place")}
    if request.action == "prepare":
        exact_keys(payload, ())
        return {"kind": "prepare"}
    if request.action == "accuse":
        exact_keys(payload, tuple(LIMITS))
        return {"kind": "accuse", **{key: index(payload[key], key) for key in LIMITS}}
    raise ContractError(f"legacy engine cannot represent C action={request.action!r} as an equivalent single command")


def normalize_acquired_clue(clue):
    """Convert an acquired exclusion clue; preserve source/cost as legacy metadata.

    No ownership, discovery event, phase, or publication status is manufactured.
    The caller retains the real observation and its routing/recipient context.
    """
    exact_keys(clue, ("id", "category", "index", "text", "source", "turn"))
    category = clue["category"]
    if category not in LIMITS:
        raise ContractError("unknown clue category")
    candidate_id = index(clue["index"], category)
    if clue["id"] != f"{category}:{candidate_id}":
        raise ContractError("clue identity does not match its exclusion meaning")
    if not isinstance(clue["text"], str) or not isinstance(clue["source"], str):
        raise ContractError("clue text/source must be strings")
    if type(clue["turn"]) is not int or clue["turn"] < 1:
        raise ContractError("legacy clue turn must be a positive cost counter")
    return {
        "information": {"id": clue["id"], "category": category,
                        "excluded_id": candidate_id, "text": clue["text"]},
        "legacy_metadata": {"source": clue["source"], "turn_cost": clue["turn"]},
    }


def denormalize_acquired_clue(projection):
    exact_keys(projection, ("information", "legacy_metadata"))
    info, metadata = projection["information"], projection["legacy_metadata"]
    exact_keys(info, ("id", "category", "excluded_id", "text"))
    exact_keys(metadata, ("source", "turn_cost"))
    clue = {"id": info["id"], "category": info["category"], "index": info["excluded_id"],
            "text": info["text"], "source": metadata["source"], "turn": metadata["turn_cost"]}
    normalize_acquired_clue(clue)
    return clue
