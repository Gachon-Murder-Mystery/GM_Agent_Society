"""Offline review of teammate source, without editing it or calling an LLM.

Run from the workspace: python -B -m gm_coordinator.team_update_probe
Native entrypoints are checked separately from diagnostic import binding.
The latter changes dependency resolution only, never teammate method bodies.
"""
import builtins
import json
import os
import subprocess
import types
from pathlib import Path

from .coordinator import Coordinator
from .mock_engine import MockEngine
from .player_adapter import ReasoningPlayerAdapter
from .store import Store
from .team_adapters import load_legacy_module


ROOT = Path(__file__).resolve().parent.parent
B = ROOT / "Agent_Reasoning"
TEMP = ROOT / "gm_coordinator" / ".test_tmp" / "team_update_20261008"
BASELINES = {
    "Game-engine": "dca35b989249bf1fc7e52ca404805b73e0a52648",
    "Agent_Reasoning": "24eeeb1e6154de3d1a9cd3d891f3479112a9d2c1",
}


def command(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return {"command": args, "exit_code": result.returncode,
            "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def diagnostic_agent(schema, llm):
    """Explicitly bind the two broken imports for component-level diagnosis."""
    module = types.ModuleType("_diagnostic_agent")
    original = builtins.__import__

    def local_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "schema" and level == 0:
            return schema
        if name == "llm" and level == 2:
            return llm
        return original(name, globals, locals, fromlist, level)

    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": local_import}
    path = B / "deploy" / "agent.py"
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


class StubClient:
    def __init__(self, response=None, error_type=None):
        self.response, self.error_type, self.calls = response, error_type, 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.error_type:
            raise self.error_type("offline injected failure")
        return json.dumps(self.response), {"input": 0, "output": 0, "total": 0}


def captured(call):
    try:
        return {"returned": call()}
    except Exception as exc:
        return {"error_type": type(exc).__name__, "error": str(exc)}


def run():
    TEMP.mkdir(parents=True, exist_ok=True)
    report = {"review_date": "2026-10-08", "live_llm_called": False,
              "teammate_source_modified": False, "full_gm_integration_proven": False}
    report["repositories"] = {}
    for name in ("Game-engine", "Agent_Reasoning"):
        repo = ROOT / name
        report["repositories"][name] = {
            "head": command(["git", "rev-parse", "HEAD"], repo)["stdout"],
            "origin_main": command(["git", "rev-parse", "origin/main"], repo)["stdout"],
            "status": command(["git", "status", "--short"], repo)["stdout"],
            "previous_review_head": BASELINES[name],
            "new_commits": command(["git", "log", "--format=%H %aI %s",
                                    BASELINES[name] + "..HEAD"], repo)["stdout"].splitlines(),
            "diff_stat": command(["git", "diff", "--stat", BASELINES[name] + "..HEAD"], repo)["stdout"],
        }
    report["native_checks"] = {
        "schema_selftest": command(["python", "-B", "deploy/schema.py"], B),
        "fake_engine_smoke": command(["python", "-B", "deploy/fake_engine.py"], B),
        "runner": command(["python", "-B", "deploy/runner.py", "--quiet", "--log",
                           str(TEMP / "runner.jsonl")], B),
        "package_import": command(["python", "-B", "-c", "import deploy.agent"], B),
        "flat_import": command(["python", "-B", "-c", "import agent"], B / "deploy"),
        "previous_native_bridge": command(["python", "-B", "-m", "unittest",
            "gm_coordinator.test_team_adapters.TeamAdapterTests.test_native_B_flow_or_explicit_API_change_block"], ROOT),
    }
    report["native_checks"]["previous_native_bridge"]["qualification"] = "Regression check of explicit B API-change blocking, not a successful native B-to-A flow."
    fake = load_legacy_module(B / "deploy" / "fake_engine.py")
    schema = load_legacy_module(B / "deploy" / "schema.py", {"fake_engine": fake})
    llm = load_legacy_module(B / "llm.py")
    agent_module = diagnostic_agent(schema, llm)
    adapter_module = load_legacy_module(B / "deploy" / "adapter.py")
    a_engine = load_legacy_module(ROOT / "Game-engine" / "engine.py")
    report["catalog_comparison"] = {
        "A_places": a_engine.PLACES, "B_fake_locations": fake.LOCATIONS,
        "same_place_labels_in_same_order": list(a_engine.PLACES) == list(fake.LOCATIONS),
        "same_suspect_labels_in_same_order": list(a_engine.SUSPECTS) == list(fake.SUSPECTS),
        "same_weapon_labels_in_same_order": list(a_engine.WEAPONS) == list(fake.WEAPONS),
    }
    store = Store()
    try:
        engine = MockEngine(store)
        gm = Coordinator.start(engine, store, "offline-review", ["p1", "p2"], seed=42)
        notice = gm.phase_notices()["p1"]
        obs = engine.agent_snapshot("offline-review", "p1")
        clue = {"id": "probe-clue", "category": "suspect", "excluded_id": 3, "text": "probe"}

        def make_agent(client=None, belief=False):
            return agent_module.LLMAgent("p1", client or StubClient({}), belief_mode=belief,
                                         log_path=TEMP / "component.jsonl")

        agent = make_agent()
        agent.sync(notice, obs)
        cases = {}
        cases["B_proposed_adapter_with_LLMAgent"] = captured(
            lambda: adapter_module.PlayerAdapter(agent).choose_action(notice, obs))
        shape = types.SimpleNamespace(observe=lambda *args: None,
                                      decide_game=lambda: ("stay", {}))
        cases["B_proposed_adapter_after_old_agent_interface_supplied"] = captured(
            lambda: adapter_module.PlayerAdapter(shape).choose_action(notice, obs))
        cases["belief_sync_first_clue"] = captured(
            lambda: make_agent(belief=True).sync(notice, {**obs, "information": [clue]}))
        cases["C_candidate_keys_valid_move"] = {
            "candidate_keys": list(obs["candidates"]),
            "validation": agent._validate({"action": {"type": "move", "payload": {"room": 0}}}, notice),
        }
        belief = schema.BeliefState("p1")
        cases["C_clue_without_translation"] = {
            "distribution_changed": belief.apply_clue(clue),
            "eliminated_suspects": sorted(belief.eliminated["suspect"]),
        }
        cases["B_clue_native_format_control"] = {
            "distribution_changed": belief.apply_clue(
                {"id": "native-probe", "kind": "suspect", "excludes": 3}),
            "eliminated_suspects": sorted(belief.eliminated["suspect"]),
        }
        failed_client = StubClient(error_type=llm.GatewayError)
        fallback_agent = make_agent(failed_client)
        fallback_agent.sync(notice, obs)
        cases["movement_on_client_failure"] = {
            "decision": fallback_agent.decide(notice, obs), "requests": failed_client.calls,
        }
        response_agent = make_agent(StubClient({"refuse": False, "offer": [clue["id"], "unowned"]}))
        response_agent.sync(notice, {**obs, "information": [clue]})
        cases["question_response_owned_ids_only"] = captured(
            lambda: response_agent.respond_to_question("p2"))
        response_agent.client = StubClient({"refuse": True, "offer": [clue["id"]]})
        cases["question_response_refusal"] = captured(
            lambda: response_agent.respond_to_question("p2"))
        cases["result_ingest_without_belief"] = captured(lambda: response_agent.ingest_result(
            {"outcome": "success", "acquired_information": [{**clue, "id": "result-clue"}]}))
        cases["result_ingest_without_belief"]["held_ids"] = sorted(response_agent._held_ids())
        response_agent.client = StubClient({"action": {"type": "pass", "payload": {}},
            "information_policy": {"type": "SHARE_FULL", "disclose": [clue["id"]]}})
        cases["disclosure_before_engine_ack"] = {
            "decision": response_agent.decide({**notice, "allowed_actions": ["pass"]}, obs),
            "already_disclosed": response_agent.disclosed,
            "engine_ack_received": False,
        }
        cases["legacy_Zen_interface"] = {
            "intent_exists": hasattr(llm.Zen, "intent"), "narrate_exists": hasattr(llm.Zen, "narrate")}
        projected_agent = make_agent(StubClient({"action": {"type": "move", "payload": {"room": 0}}}))
        cases["C_adapter_with_explicit_B_belief_off"] = captured(
            lambda: ReasoningPlayerAdapter(projected_agent).choose_action(notice, obs).to_dict())
        cases["C_adapter_preserves_B_default_belief_error"] = captured(
            lambda: ReasoningPlayerAdapter(make_agent(belief=True)).choose_action(notice, {**obs, "information": [clue]}))
        report["diagnostic_components"] = {
            "qualification": "Imports bound locally; teammate methods unchanged; client stubbed; C mock observations, not A live integration.",
            "cases": cases,
        }
    finally:
        store.close()
    output = ROOT / "gm_coordinator" / "examples" / "team_update_current.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "native_exit_codes": {
        k: v["exit_code"] for k, v in report["native_checks"].items()},
        "diagnostic_components": report["diagnostic_components"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
