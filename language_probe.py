"""Re-run the shared-vocabulary evidence against local A/B repositories."""
import argparse
import json
from contextlib import closing
from pathlib import Path

from .contracts import VERSION, ActionRequest
from .integration_probe import FixedSixes, git
from .message_language import denormalize_acquired_clue, intent_to_request, normalize_acquired_clue, request_to_intent
from .store import Store
from .team_adapters import LegacyGMObserver, NativeEngineBridge, NativeReasoningBridge


def compare_catalogs(engine, reasoning, catalog):
    rows = []
    for constant, collection in (("JOBS", "jobs"), ("SUSPECTS", "suspects"),
                                 ("WEAPONS", "weapons"), ("PLACES", "places")):
        a, b = list(getattr(engine.module, constant)), list(getattr(reasoning.engine_module, constant))
        c = sorted(catalog[collection], key=lambda item: item["id"])
        rows.append({"collection": collection, "a_labels_by_index": a, "b_labels_by_index": b,
                     "c_id_names": [{"id": item["id"], "name": item["name"]} for item in c],
                     "same_ids_and_labels": a == b == [item["name"] for item in c]
                     and [item["id"] for item in c] == list(range(len(a))),
                     "scope": "catalog identity only; no skill/personality/rule equivalence claim"})
    return rows


def run(engine_repo=None, agent_repo=None):
    root = Path(__file__).resolve().parent.parent
    engine_repo, agent_repo = Path(engine_repo or root / "Game-engine"), Path(agent_repo or root / "Agent_Reasoning")
    engine, reasoning = NativeEngineBridge(engine_repo), NativeReasoningBridge(agent_repo)
    catalog = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
    catalogs = compare_catalogs(engine, reasoning, catalog)
    if not reasoning.capabilities()["legacy_interpreter_available"]:
        return {
            "schema_version": VERSION, "report_kind": "message_semantics_evidence",
            "status": "blocked_by_B_API_change",
            "reason": "B removed Zen.intent; use the new player adapter after B execution fixes",
            "catalog_identity": catalogs,
            "all_catalog_ids_and_labels_match": all(row["same_ids_and_labels"] for row in catalogs),
            "action_body_roundtrips": [], "acquired_information_roundtrips": [], "native_information_flow": [],
            "team_adoption": "C draft only; no A/B adoption observed",
            "boundaries": {"live_llm": False, "native_multi_agent_protocol": False, "native_gm_completion_event": False},
        }
    observation = engine.legacy_start()
    game_id = observation["id"]
    # Context is supplied by this test harness, never decoded from A's turn.
    context_origin = "explicit test harness values; not native engine phase or actor identity"
    actions = []
    samples = (
        ("식당 칸으로 이동한다", "movement", 1, "move", {"kind": "move", "place": 2}),
        ("질문을 정리한다", "interaction", 2, "prepare", {"kind": "prepare"}),
        (f"범인은 {engine.module.SUSPECTS[1]}, 흉기는 {engine.module.WEAPONS[0]}, 범행 장소는 {engine.module.PLACES[2]}",
         "deduction", 3, "accuse", {"kind": "accuse", "suspect": 1, "weapon": 0, "place": 2}),
    )
    for text, phase, epoch, label, expected in samples:
        interpreted = reasoning.interpret(text, observation)
        context = {"game_id": game_id, "agent_id": "probe_player", "action_id": f"language-{label}",
                   "cycle_id": 1, "phase": phase, "phase_epoch": epoch, "task_id": f"{game_id}:{epoch}:probe_player"}
        request = intent_to_request(interpreted["intent"], context)
        # Exercise C's actual decoder after a JSON transport boundary.
        decoded = ActionRequest.from_dict(json.loads(json.dumps(request.to_dict(), ensure_ascii=False)))
        native = request_to_intent(decoded)
        a_validated = engine.module.validate_intent(native, engine._legacy_game)
        if not interpreted["intent"] == native == a_validated == expected:
            raise AssertionError(f"action meaning changed: {label}")
        actions.append({"text": text, "b_native_intent": interpreted["intent"], "c_action_request": decoded.to_dict(),
                        "context_origin": context_origin, "a_validated_intent": a_validated,
                        "command_body_roundtrip_equal": True,
                        "scope": "command fields only; phase enforcement and accusation consequences are not equivalent"})
    # Obtain real A-acquired hints, then consume their shared Information shape in C.
    trace = []
    with closing(Store()) as store:
        observer = LegacyGMObserver(store)
        for text in ("식당 칸으로 이동한다", "질문을 정리한다", "이곳의 기록을 조사한다", "굴린다"):
            intent = reasoning.interpret(text, engine.legacy_observation())["intent"]
            native_result = engine.legacy_execute(intent, text, rng=FixedSixes())
            receipt = observer.observe(native_result["observation"], actor_id="probe_player")
            trace.append({"b_intent": intent, "a_pending": native_result["observation"]["pending"],
                          "new_clue_ids": [clue["id"] for clue in native_result["acquired_clues"]],
                          "c_normalized_information": receipt["normalized_information"],
                          "native_engine_completion_event": False})
        final = engine.legacy_observation()
        clues = []
        for clue in final["clues"]:
            projection = normalize_acquired_clue(clue)
            restored = denormalize_acquired_clue(json.loads(json.dumps(projection, ensure_ascii=False)))
            if restored != clue:
                raise AssertionError("acquired clue identity/meaning/metadata changed")
            clues.append({"a_acquired_clue": clue, "c_projection": projection,
                          "legacy_roundtrip_equal": True,
                          "meaning": "excluded from the indicated candidate category; not identified as culprit"})
        saved = store.read("legacy_integration_observer", game_id)
        if not clues or saved["normalized_information"] != [item["c_projection"]["information"] for item in clues]:
            raise AssertionError("C did not retain the same acquired exclusion information")
    return {
        "schema_version": VERSION, "report_kind": "message_semantics_evidence",
        "repositories": [{"role": role, "commit": git(repo, "rev-parse", "HEAD")}
                         for role, repo in (("A", engine_repo), ("B", agent_repo))],
        "team_adoption": "C draft only; no A/B agreement or multi-agent protocol adoption observed",
        "catalog_identity": catalogs, "all_catalog_ids_and_labels_match": all(row["same_ids_and_labels"] for row in catalogs),
        "action_body_roundtrips": actions, "acquired_information_roundtrips": clues,
        "native_information_flow": trace,
        "boundaries": {"context_origin": context_origin, "dice": "fixed sixes supplied to real A function",
                       "live_llm": False, "native_multi_agent_protocol": False,
                       "native_http_accepts_structured_intent": False, "native_gm_completion_event": False,
                       "hidden_truth_or_undiscovered_clues_in_output": False},
        "requires_explicit_agreement": [
            "game_id / agent_id / action_id / task_id correlation and ID namespaces",
            "cycle_id / phase / phase_epoch versus legacy turn cost",
            "question/explore/examine_corpse as one completed action versus check + roll",
            "same private result to requesting player and supervisory GM; explicit publication separate",
            "actor mode active/npc/corpse, technical failure, deduction death and end reason",
            "authoritative catalog skill/stat/personality/optional-goal definitions",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-repo")
    parser.add_argument("--agent-repo")
    parser.add_argument("--out", default=str(Path(__file__).with_name("examples") / "shared_language_current.json"))
    args = parser.parse_args()
    report = run(args.engine_repo, args.agent_repo)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "catalog_ids_and_labels_match": report["all_catalog_ids_and_labels_match"],
                      "action_body_roundtrips": len(report["action_body_roundtrips"]),
                      "acquired_information_roundtrips": len(report["acquired_information_roundtrips"]),
                      "team_adoption": report["team_adoption"]}, ensure_ascii=False))
    return 0 if report["all_catalog_ids_and_labels_match"] and report.get("status") != "blocked_by_B_API_change" else 1


if __name__ == "__main__":
    raise SystemExit(main())
