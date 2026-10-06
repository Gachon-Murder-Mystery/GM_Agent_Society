"""Actual team code probes, kept separate from mock integration results."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .coordinator import Coordinator
from .store import Store
from .team_adapters import IntegrationUnavailable, LegacyGMObserver, NativeEngineBridge, NativeReasoningBridge


class FixedSixes:
    def randint(self, minimum, maximum):
        return 6


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args], text=True, encoding="utf-8").strip()


def native_component_probe(engine, reasoning, store):
    engine.legacy_start()
    observer = LegacyGMObserver(store)
    trace = []
    for text in ("식당 칸으로 이동한다", "질문을 정리한다", "이곳의 기록을 조사한다", "굴린다"):
        before = engine.legacy_observation()
        interpreted = reasoning.interpret(text, before)
        result = engine.legacy_execute(interpreted["intent"], text, rng=FixedSixes())
        received = observer.observe(result["observation"])
        trace.append({"text": text, "b_interpretation": interpreted,
                      "a_native_result": result, "c_observer_record": received})
    snapshot = engine.legacy_observation()
    return {
        "status": "single_actor_component_flow_executed", "trace": trace,
        "flow": "B native keyless intent -> A native validate/apply -> A public observation -> C receipt store",
        "new_information_received_by_observer": bool(snapshot["clues"]),
        "dice_provider": "fixed sixes injected into native A rng argument for deterministic smoke test",
        "autonomous_player": False, "native_engine_events": False, "three_phase_gameplay": False,
        "multiple_agent_game": False, "live_llm": False,
    }


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _request(base, endpoint, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + endpoint, data=data,
                                     headers={"Content-Type": "application/json", "X-GM-Client": "local"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return {"http_status": response.status, "body": json.loads(response.read().decode("utf-8"))}


def native_http_probe(engine_repo, reasoning, work_dir):
    """Launch the unmodified A app against an isolated local database."""
    work_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    database = work_dir / "native_engine.sqlite3"
    log_path = work_dir / "server.log"
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = {"base_url": base, "database": str(database), "source": str(engine_repo / "app.py"),
              "database_scope": "isolated probe data; original demo/user game database is not used",
              "calls": [], "live_llm": False, "server_stopped": False, "port_closed": False}
    process = None
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                [sys.executable, str(engine_repo / "app.py"), "--no-browser", "--port", str(port), "--data", str(database)],
                cwd=str(engine_repo), stdout=log, stderr=log, env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise RuntimeError("native engine server exited before readiness")
                try:
                    catalog = _request(base, "/catalog")
                    break
                except (urllib.error.URLError, OSError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("native engine server readiness timed out")
                    time.sleep(0.05)
            result["calls"].append({"endpoint": "/catalog", "response": catalog})
            start = _request(base, "/start", {"name": "HTTP 연결 점검", "role": 0, "stat": 2, "goal": "비밀 지키기"})
            result["calls"].append({"endpoint": "/start", "response": start})
            game_id = start["body"]["id"]
            state = _request(base, "/state", {"id": game_id})["body"]
            interpreted = reasoning.interpret("식당 칸으로 이동한다", state)
            # Caller-supplied phase fields are not recognized by the old server.
            moved = _request(base, "/chat", {"id": game_id, "text": "식당 칸으로 이동한다", "key": "",
                                              "cycle_id": 1, "phase": "interaction", "phase_epoch": 2,
                                              "action_id": "move-during-interaction"})
            result["calls"].append({"endpoint": "/chat", "b_interpretation": interpreted,
                                    "submitted_phase": "interaction", "response": moved})
            result["caller_phase_field_did_not_block_movement"] = moved["body"]["room"] == 2
            duplicate = {"id": game_id, "text": "질문을 정리한다", "key": "", "action_id": "same-action-id"}
            first = _request(base, "/chat", duplicate)
            second = _request(base, "/chat", duplicate)
            result["calls"].append({"endpoint": "/chat", "probe": "same action_id twice", "first": first, "second": second})
            result["same_action_id_repeated_side_effect"] = second["body"]["turn"] > first["body"]["turn"]
            checked = _request(base, "/chat", {"id": game_id, "text": "이곳의 기록을 조사한다", "key": ""})
            rolled = _request(base, "/roll", {"id": game_id, "key": ""})
            result["calls"].append({"endpoint": "/chat -> /roll", "check": checked, "roll": rolled})
            result["observation_returned"] = "clues" in rolled["body"]
            result["native_http_has_structured_action_result"] = "action_id" in rolled["body"]
            result["status"] = "native_http_executed_with_contract_gaps"
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                result["server_stopped"] = process.poll() is not None
            with socket.socket() as check:
                check.settimeout(0.25)
                result["port_closed"] = check.connect_ex(("127.0.0.1", port)) != 0
    return result


def run(engine_repo=None, agent_repo=None, include_http=True):
    root = Path(__file__).resolve().parent.parent
    engine_repo = Path(engine_repo or root / "Game-engine").resolve()
    agent_repo = Path(agent_repo or root / "Agent_Reasoning").resolve()
    engine = NativeEngineBridge(engine_repo)
    reasoning = NativeReasoningBridge(agent_repo)
    repositories = [{"role": role, "path": str(path), "branch": git(path, "branch", "--show-current"),
                     "commit": git(path, "rev-parse", "HEAD"), "tracked_worktree_clean": git(path, "status", "--short") == ""}
                    for role, path in (("A_engine", engine_repo), ("B_reasoning", agent_repo))]
    store = Store()
    try:
        try:
            Coordinator.start(engine, store, "real-team-attach", ["player_1", "player_2", "player_3"])
        except IntegrationUnavailable as error:
            attachment = {"attempted": True, "status": "blocked", "blocked_call": "create_game",
                          "reason": str(error), "missing_native_methods": error.missing_methods,
                          "game_created_by_full_attach": False, "mock_substitution": False}
        else:
            raise RuntimeError("unexpected full attachment; re-review the native adapter contract")
        component = native_component_probe(engine, reasoning, store)
        work_dir = Path(__file__).with_name(".test_tmp") / ("native_http_" + uuid.uuid4().hex)
        http = native_http_probe(engine_repo, reasoning, work_dir) if include_http else {"status": "not_requested"}
        return {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(), "repositories": repositories,
            "engine_capabilities": engine.capabilities(), "reasoning_capabilities": reasoning.capabilities(),
            "full_gm_attachment": attachment, "native_component_probe": component, "native_http_probe": http,
            "summary": "Native single-actor components and HTTP can be used; agreed multi-agent GM contract cannot yet attach.",
            "team_source_modified": False, "remote_writes": False,
        }
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-repo", type=Path)
    parser.add_argument("--agent-repo", type=Path)
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("examples") / "integration_attempt_20261006.json")
    args = parser.parse_args()
    report = run(args.engine_repo, args.agent_repo, not args.skip_http)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {"full_gm_attachment": report["full_gm_attachment"]["status"],
               "native_components": report["native_component_probe"]["status"],
               "information_received_by_C": report["native_component_probe"]["new_information_received_by_observer"],
               "native_http": report["native_http_probe"]["status"],
               "server_stopped": report["native_http_probe"].get("server_stopped"),
               "port_closed": report["native_http_probe"].get("port_closed")}
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
