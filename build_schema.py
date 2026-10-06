"""Generate the provisional wire-format schemas without runtime dependencies."""
import json
from pathlib import Path


def build():
    string = {"type": "string"}
    ident = {"type": "string", "minLength": 1}
    integer = {"type": "integer", "minimum": 1}
    phase = {"enum": ["movement", "interaction", "deduction"]}
    mode = {"enum": ["active", "npc", "corpse"]}
    nullable_string = {"type": ["string", "null"]}
    def ref(name):
        return {"$ref": f"#/$defs/{name}"}
    def array(items):
        return {"type": "array", "items": items}
    def obj(properties, required=None, extra=False):
        return {"type": "object", "properties": properties,
                "required": list(properties) if required is None else required, "additionalProperties": extra}
    common = {"game_id": ident, "cycle_id": integer, "phase": phase, "phase_epoch": integer}
    presence = obj({"id": ident, "name": string, "mode": mode, "room": {"type": "integer", "minimum": 0, "maximum": 7},
                    "job": obj({"id": {"type": "integer", "minimum": 0, "maximum": 11}, "name": string})})
    information = obj({"id": ident, "category": {"enum": ["suspect", "weapon", "place"]},
                       "excluded_id": {"type": "integer", "minimum": 0}, "text": string})
    request = obj({**common, "agent_id": ident, "action_id": ident, "task_id": nullable_string,
                   "action": {"enum": ["move", "stay", "explore", "question", "examine_corpse", "prepare", "pass", "accuse"]},
                   "payload": {"type": "object"}},
                  ["game_id", "agent_id", "action_id", "cycle_id", "phase", "phase_epoch", "action"])
    request["allOf"] = []
    for phase_name, actions in (("movement", ["move", "stay"]),
                                ("interaction", ["explore", "question", "examine_corpse", "prepare", "pass"]),
                                ("deduction", ["accuse", "pass"])):
        request["allOf"].append({"if": {"properties": {"phase": {"const": phase_name}}},
                                 "then": {"properties": {"action": {"enum": actions}}}})
    action_payloads = {
        "move": obj({"room": {"type": "integer", "minimum": 0, "maximum": 7}}),
        "stay": obj({}), "pass": obj({}), "prepare": obj({}),
        "explore": obj({"approach": {"enum": ["compassion", "brutality"]}}, []),
        "question": obj({"target_id": ident, "approach": {"enum": ["compassion", "brutality"]}}, ["target_id"]),
        "examine_corpse": obj({"target_id": ident, "approach": {"enum": ["compassion", "brutality"]}}, ["target_id"]),
        "accuse": obj({"suspect": {"type": "integer", "minimum": 0, "maximum": 11},
                       "weapon": {"type": "integer", "minimum": 0, "maximum": 7},
                       "place": {"type": "integer", "minimum": 0, "maximum": 7}}),
    }
    for action, payload in action_payloads.items():
        request["allOf"].append({"if": {"properties": {"action": {"const": action}}},
                                 "then": {"properties": {"payload": payload},
                                          "required": ["payload"] if action in ("move", "question", "examine_corpse", "accuse") else []}})
    result = obj({**common, "schema_version": {"const": "gm-provisional-v0.1"},
                  "agent_id": ident, "action_id": ident, "task_id": nullable_string,
                  "execution_status": {"const": "completed"}, "outcome": {"enum": ["succeeded", "failed", "skipped"]},
                  "consumed": {"const": True}, "action": request["properties"]["action"],
                  "room": {"type": "integer", "minimum": 0, "maximum": 7}, "actor_mode": mode,
                  "target_id": nullable_string, "roll": {"type": ["object", "null"]},
                  "acquired_information": array(ref("Information")), "held_information_ids": array(ident),
                  "resolution": {"enum": [None, "solved"]}})
    notice = obj({**common, "agent_id": ident, "task_id": ident, "allowed_actions": array(string),
                  "remaining_actions": {"const": 1}, "self": {"type": "object"},
                  "occupants": array(ref("Presence")), "shared_information": array(ref("Information")),
                  "positions": array(ref("Presence"))},
                 list(common) + ["agent_id", "task_id", "allowed_actions", "remaining_actions", "self", "occupants", "shared_information"])
    notice["allOf"] = [{"if": {"properties": {"phase": {"const": "movement"}}},
                         "then": {"required": ["positions"]}, "else": {"not": {"required": ["positions"]}}}]
    event = obj({**common, "schema_version": {"const": "gm-provisional-v0.1"}, "event_id": ident, "sequence": integer,
                 "type": {"enum": ["game_started", "action_completed", "information_shared", "actor_died", "actor_became_npc",
                                    "gm_replaced", "phase_started", "information_decayed", "npc_moved", "game_ended"]},
                 "payload": {"type": "object"}})
    event["allOf"] = [{"if": {"properties": {"type": {"const": "action_completed"}}},
                        "then": {"properties": {"payload": obj({"result": ref("ActionResult"), "visibility": {"const": "gm_supervisory"}})}}}]
    control = obj({"controller_id": ident, "term": integer})
    snapshot = obj({**common, "control": ref("Control"), "event_sequence": integer,
                    "active_agents": array(ident), "actors": array(ref("Presence")),
                    "inventories": {"type": "object", "additionalProperties": array(ident)},
                    "observed_information": array(ref("Information")), "shared_information": array(ref("Information")),
                    "spent": {"type": "object", "additionalProperties": ident}, "ended": {"type": "boolean"},
                    "end_reason": {"enum": [None, "solved", "technical_failure", "deduction_failure"]},
                    "resolution": {"enum": [None, "solved"]},
                    "last_removal_reason": {"enum": [None, "technical_failure", "deduction_failure"]}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "gm-provisional-v0.1 wire formats",
            "description": "Validate with a selected $defs reference; game legality also requires engine state checks.",
            "$defs": {"Information": information, "Presence": presence, "Control": control,
                      "ActionRequest": request, "ActionResult": result, "PhaseNotice": notice,
                      "EngineEvent": event, "GMSnapshot": snapshot}}


if __name__ == "__main__":
    path = Path(__file__).with_name("io_schema.json")
    path.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
