"""Run one competitive v1.3 scenario with the three parts joined over HTTP.

A: the engine runs as its own process (`Game-engine/app.py`, temporary DB by default).
B: each pool member's player runtime (`--player package.module:factory`; default is
   C's scripted stand-in `ScriptedPlayerRuntime`, no LLM).
C: the GM is whichever pool member the engine makes GM (`CompetitiveGMWorker`).
Every pool member is stepped on its own thread; engine requests still go one at a time.

    python -m gm_coordinator.run_scenario --seed 7 --progress
    python -m gm_coordinator.run_scenario --player agent_reasoning_v13:factory --out run.json
"""
import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .agent_runtime import AgentWorker, load_factory
from .engine_v1 import EngineV1Client, EngineV1Error

ROOT = Path(__file__).resolve().parent
DEFAULT_ENGINE = ROOT.parent / "Game-engine"
# Each pool member runs on its own thread. Stepping them one after another let a few slow LLM calls add up
# past the engine's 40 s player/GM lease, turning players into NPCs and failing the GM over.
STEP_PAUSE = 0.05  # seconds between one member's steps
POLL_SECONDS = 0.2
JOIN_SECONDS = 5
UNSERIALIZED = ("llm/invoke",)  # A's engine-hosted LLM call: held under the lock it would stall every member


class SerialClient(EngineV1Client):
    """Sends one engine request at a time, as the sequential runner did. Thirteen members writing at once
    exceeded the engine's 10 s SQLite lock wait (HTTP 500 "database is locked"). Only the waiting overlaps."""

    def __init__(self, base_url="http://127.0.0.1:8765", credentials=None, timeout=10, lock=None):
        super().__init__(base_url, credentials, timeout)
        self.lock = lock or threading.Lock()

    def bind(self, **credentials):
        return SerialClient(self.base_url, credentials, self.timeout, self.lock)

    def call(self, route, **data):
        if route in UNSERIALIZED:
            return super().call(route, **data)
        with self.lock:
            return super().call(route, **data)


def git_head(path):
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, encoding="utf-8", timeout=10)
    except OSError:
        return "unavailable"
    return out.stdout.strip() if out.returncode == 0 else "unavailable"


@contextmanager
def engine_process(engine_dir, data):
    """Start A's engine server on a free port; yield (base_url, operator_token)."""
    env = os.environ | {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    secret = subprocess.run([sys.executable, "-B", "integration_cli.py", "--data", str(data), "credentials"],
                            cwd=engine_dir, env=env, capture_output=True, text=True, encoding="utf-8",
                            check=True, timeout=30)
    token = json.loads(secret.stdout)["operator_token"]
    proc = subprocess.Popen([sys.executable, "-B", "app.py", "--port", "0", "--no-browser", "--data", str(data)],
                            cwd=engine_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8")
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
    try:
        line = lines.get(timeout=30)
        url = next((x for x in line.split() if x.startswith("http://")), None)
        if url is None:
            raise RuntimeError(f"engine did not announce its address: {line!r}")
        yield url.rstrip("/"), token
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()


def member_loop(agent_id, worker, stop, failures, refusals):
    """Step one pool member until the run stops, so its slow LLM call never holds up the others or the GM.

    A request the engine refuses (4xx, e.g. an action that arrives after the phase moved on or after the
    member became an NPC) is that member's outcome, counted in `refusals`; anything else stops the run."""
    while not stop.is_set():
        try:
            worker.step()
        except EngineV1Error as exc:
            if isinstance(exc.status, int) and 400 <= exc.status < 500:
                refusals.append(exc.code)
            else:
                failures.put((agent_id, exc))
                return
        except Exception as exc:  # noqa: BLE001 - handed to run(), which stops the scenario
            failures.put((agent_id, exc))
            return
        stop.wait(STEP_PAUSE)


def run(client, operator_token, seed=7, rules=None, player_factory=None, provenance=None,
        time_limit=3600, progress=False):
    """Bootstrap the pool, create one game and step every pool member until the GM has reported."""
    client = SerialClient(client.base_url, client.credentials, client.timeout)
    operator = client.bind(operator_token=operator_token)
    pool = operator.call("pool/bootstrap", pool_id="society")
    created = operator.call("games/create", pool_id="society", seed=seed, rules=rules or {},
                            provenance=provenance or {})
    game_id = created["game_id"]
    workers = {a["agent_id"]: AgentWorker(client.bind(**operator.call("pool/credentials", agent_id=a["agent_id"])),
                                          player_factory)
               for a in pool["agents"]}
    stop, failures, refusals = threading.Event(), queue.Queue(), []
    threads = [threading.Thread(target=member_loop, args=(agent_id, worker, stop, failures, refusals), name=agent_id, daemon=True)
               for agent_id, worker in workers.items()]
    started, last_cycle = time.monotonic(), None
    try:
        for thread in threads:
            thread.start()
        while True:
            if not failures.empty():
                agent_id, exc = failures.get()
                raise RuntimeError(f"pool member {agent_id} stopped: {exc!r}") from exc
            state = operator.call("games/observe", game_id=game_id)
            if state["ended"] and state["gm_report"] is not None:
                break
            if progress and state["cycle_id"] != last_cycle and (state["cycle_id"] == 1 or state["cycle_id"] % 10 == 0):
                print(f"cycle {state['cycle_id']} · active {len(state['active_agents'])}", file=sys.stderr, flush=True)
            last_cycle = state["cycle_id"]
            if time.monotonic() - started > time_limit:
                raise RuntimeError(f"no official ending within {time_limit}s (cycle {state['cycle_id']}, phase {state['phase']})")
            time.sleep(POLL_SECONDS)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=JOIN_SECONDS)  # a member still waiting on its LLM is left behind (daemon)
    gm_worker = next(w.gm for w in workers.values() if w.gm.gm is not None and w.gm.gm.reported)
    record = gm_worker.gm.report()
    players = [p for w in workers.values() for p in w.runtimes]
    verdicts = [v for a in state["announcements"] if a["kind"] == "deduction_result" for v in a["verdicts"]]
    return {"game_id": game_id, "seed": seed, "resolution": state["resolution"], "cycles": state["cycle_id"],
            "players": len(state["role_assignments"]), "winners": [v for v in verdicts if v["winner"]],
            "deaths": [v["agent_id"] for v in verdicts if not v["winner"]],
            "gm": {"controller_id": gm_worker.gm.controller_id, "term": gm_worker.gm.term,
                   "failovers": state["failovers"]},
            "gm_report": state["gm_report"], "engine_metrics": state["metrics"],
            "scenario": [line["text"] for line in record["scenario"]],
            "player_runtime": sorted({type(p).__name__ for p in players}) or None,
            "player_fallbacks": sum(getattr(p, "fallbacks", 0) for p in players),
            "engine_refusals": {code: refusals.count(code) for code in sorted(set(refusals))},
            "live_llm": any(getattr(p, "live_llm", False) for p in players),
            "elapsed_seconds": round(time.monotonic() - started, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", default=str(DEFAULT_ENGINE), help="A's Game-engine checkout")
    parser.add_argument("--player", help="B player factory as package.module:callable (default: scripted stand-in)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--deadline", type=int, help="override deadline_cycles (default 120)")
    parser.add_argument("--data", help="persistent SQLite path; default is a temporary database")
    parser.add_argument("--out", help="write the run summary JSON here")
    parser.add_argument("--time-limit", type=int, default=3600)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    engine_dir = Path(args.engine).resolve()
    if not (engine_dir / "competitive.py").is_file():
        parser.error(f"A engine not found at {engine_dir}")
    rules = {"deadline_cycles": args.deadline} if args.deadline else {}
    factory = load_factory(args.player)
    sources = [engine_dir / p for p in ("integration.py", "competitive.py", "competitive_rules.py", "competitive_catalog.json")]
    provenance = {"engine_commit": git_head(engine_dir), "gm_commit": git_head(ROOT),
                  "engine_tree_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()[:16],
                  "player": args.player or "gm_coordinator.agent_runtime:ScriptedPlayerRuntime",
                  "command": " ".join(["run_scenario"] + sys.argv[1:])[:500]}
    with tempfile.TemporaryDirectory() as temporary:
        data = Path(args.data) if args.data else Path(temporary) / "scenario.sqlite3"
        data.parent.mkdir(parents=True, exist_ok=True)
        with engine_process(engine_dir, data) as (url, token):
            result = run(EngineV1Client(url, timeout=30), token, args.seed, rules, factory, provenance,
                         args.time_limit, args.progress)
    result["provenance"] = provenance
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
