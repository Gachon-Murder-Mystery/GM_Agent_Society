"""Offline parallel integration driver. python -m gm_coordinator.demo --help"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .contracts import ContractError, Control
from .coordinator import Coordinator
from .mock_engine import MockEngine
from .mock_player import MockPlayer
from .store import Store

SCENARIOS = ("normal", "player-fault", "gm-fault", "all-fault", "all-dead", "death")


def run(scenario="normal", agents=3, seed=42, max_cycles=80, db=":memory:", share=True, decay=1):
    if type(agents) is not int or agents < 1 or scenario not in SCENARIOS or max_cycles < 1:
        raise ValueError("invalid scenario, roster size or cycle budget")
    store = Store(db)
    try:
        engine = MockEngine(store)
        game_id = f"demo-{scenario}-{agents}-{seed}"
        existing = store.read("engine", game_id)
        if existing:
            gm = Coordinator(engine, store, game_id, Control(**existing["control"]))
            gm.sync()
        else:
            gm = Coordinator.start(engine, store, game_id, [f"player_{i + 1}" for i in range(agents)], seed,
                                   {"corpse_decay_per_cycle": decay})
        players = {f"player_{i + 1}": MockPlayer(f"player_{i + 1}", i) for i in range(agents)}
        initial_notices = gm.phase_notices()
        samples = {"provenance": "offline mock engine and scripted players; provisional contract",
                   "catalog_input": engine.catalog(), "initial_agent_outputs": initial_notices,
                   "example_action_request": None, "example_agent_result": None,
                   "example_gm_event": None, "interaction_agent_outputs": None}
        while True:
            snapshot = gm.sync()
            if snapshot["ended"]:
                break
            if snapshot["cycle_id"] > max_cycles:
                break  # Driver stop, not a game ending or evidence of success.
            if snapshot["phase"] == "deduction":
                if scenario in ("player-fault", "all-fault"):
                    targets = snapshot["active_agents"] if scenario == "all-fault" else ["player_1"]
                    for ident in list(targets):
                        if ident in gm.sync()["active_agents"]:
                            gm.report_player_fault(ident, "injected_player_runtime_failure")
                if scenario == "gm-fault" and gm.control.term == 1:
                    gm = Coordinator.replace_failed_gm(engine, store, game_id, gm.control)
            if gm.tick() == "ended":
                break
            notices = gm.phase_notices()
            if notices and next(iter(notices.values()))["phase"] == "interaction" and samples["interaction_agent_outputs"] is None:
                samples["interaction_agent_outputs"] = notices

            def play(ident, notice):
                observation = engine.agent_snapshot(game_id, ident)
                force = scenario == "all-dead" or (scenario == "death" and ident == "player_1")
                request = players[ident].choose_action(notice, observation, force_death=force)
                result = engine.submit_action(request)  # Direct B -> A, no GM action approval.
                players[ident].receive_result(result)
                if share and result["actor_mode"] == "active" and not result["resolution"] and result["held_information_ids"]:
                    try:
                        engine.share_information(game_id, ident, result["held_information_ids"], request.action_id + ":share")
                    except ContractError:
                        if not engine.gm_snapshot(game_id)["resolution"]:
                            raise
                return request, result

            with ThreadPoolExecutor(max_workers=agents) as executor:
                futures = {executor.submit(play, ident, notice): ident for ident, notice in notices.items()}
                for future in as_completed(futures):
                    try:
                        request, result = future.result()
                    except ContractError:
                        if engine.gm_snapshot(game_id)["resolution"]:
                            continue  # Another parallel player has already solved the case.
                        raise
                    if result["phase"] == "interaction" and samples["example_agent_result"] is None:
                        samples["example_action_request"] = request.to_dict()
                        samples["example_agent_result"] = result
                        samples["example_gm_event"] = next(event for event in engine.events_after(game_id, 0)
                                                          if event["type"] == "action_completed" and
                                                          event["payload"]["result"]["action_id"] == request.action_id)
            gm.tick()
        snapshot = gm.sync()
        events = engine.events_after(game_id, 0)
        type_counts = {}
        for event in events:
            type_counts[event["type"]] = type_counts.get(event["type"], 0) + 1
        return {
            "schema_version": engine.catalog()["schema_version"], "scenario": scenario, "seed": seed,
            "agents_initial": agents, "agents_remaining": len(snapshot["active_agents"]),
            "cycle_id": snapshot["cycle_id"], "phase": snapshot["phase"],
            "ended": snapshot["ended"], "end_reason": snapshot["end_reason"],
            "driver_stopped": not snapshot["ended"], "control": snapshot["control"],
            "actions_recorded": len(snapshot["action_ledger"]), "event_counts": type_counts,
            "notes": ["Local mock execution only; no A/B production integration or live LLM verification.",
                      "Parallel scheduling can change outcomes for the same seed; retain event order for replay."],
            "samples": samples, "gm_final_snapshot": snapshot,
        }
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--agents", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-cycles", type=int, default=80)
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--withhold", action="store_true", help="Scripted players do not voluntarily share")
    parser.add_argument("--corpse-decay", type=int, default=1, help="0 disables provisional information decay")
    args = parser.parse_args()
    result = run(args.scenario, args.agents, args.seed, args.max_cycles, args.db, not args.withhold, args.corpse_decay)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("samples", "gm_final_snapshot")}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
