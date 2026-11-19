"""Run one competitive v1.3 scenario with the three parts joined over HTTP.

A: the engine runs as its own process (`Game-engine/app.py`, temporary DB by default).
B: each pool member's player runtime (`--player package.module:factory`; default is
   C's scripted stand-in `ScriptedPlayerRuntime`, no LLM).
C: the GM is whichever pool member the engine makes GM (`CompetitiveGMWorker`).

    python -m GM_Agent_Society.run_scenario --seed 7 --progress
    python -m GM_Agent_Society.run_scenario --player agent_reasoning_v13:factory --out run.json
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .agent_runtime import AgentWorker, load_factory
from .engine_v1 import EngineV1Client

ROOT = Path(__file__).resolve().parent
DEFAULT_ENGINE = ROOT.parent / "Game-engine"


POLL_PAUSE_SECONDS = 0.1



ENGINE_CLIENT_TIMEOUT = 120   # [FIX-I13] seconds; above the engine's 60 s DB lock wait

def _merge(counters):
    """Sum dicts of counts (say_do_mismatches per player, FIX-B13)."""
    total = {}
    for counter in counters:
        for key, value in (counter or {}).items():
            total[key] = total.get(key, 0) + value
    return total


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
    # [FIX-C13] keep the engine's tracebacks (they used to go to DEVNULL, so a 500 had no cause)
    engine_log = open(Path(str(data) + ".engine.log"), "a", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-B", "app.py", "--port", "0", "--no-browser", "--data", str(data)],
                            cwd=engine_dir, env=env, stdout=subprocess.PIPE, stderr=engine_log,
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
        engine_log.close()


def run(client, operator_token, seed=7, rules=None, player_factory=None, provenance=None,
        time_limit=3600, progress=False, parallel=False, on_created=None):
    """Bootstrap the pool, create one game and step every pool member until the GM has reported.

    time_limit: wall-clock seconds, 0 = off (the game then ends only by cycles / a winner).
    parallel:   [FIX-C6] step all pool members concurrently, one round at a time. Each round
                is a barrier, so a member is never stepped by two threads at once; the
                engine serialises requests per game (ThreadingHTTPServer + per-game lock).
                LLM calls of different agents then overlap instead of queueing.
    """
    operator = client.bind(operator_token=operator_token)
    # [FIX-I12] record the model that really plays (the engine default label would say gpt-6-luna)
    model = os.environ.get("CODEX_MODEL") if os.environ.get("GAME_LLM_BACKEND") == "codex" else None
    pool = operator.call("pool/bootstrap", pool_id="society",   # reuses the persistent pool if the DB has one
                         **({"model_version": model} if model else {}))
    # [FIX-C2] The engine (and through it every player) learns the wall-clock budget.
    rules = dict(rules or {}, time_limit_seconds=int(time_limit))
    created = operator.call("games/create", pool_id="society", seed=seed, rules=rules,
                            provenance=provenance or {})
    game_id = created["game_id"]
    # [FIX-I11] the runner may hook the game (model-outage PAUSE/RESUME); it returns a callable
    # giving the seconds the game was paused, which do not count against the wall-clock limit
    paused = on_created(operator, game_id) if on_created else None
    workers = {a["agent_id"]: AgentWorker(client.bind(**operator.call("pool/credentials", agent_id=a["agent_id"])),
                                          player_factory)
               for a in pool["agents"]}
    started, last_cycle = time.monotonic(), None
    timeout_requested = None
    executor = ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix="agent") if parallel else None

    def step_all(fn):
        if executor is None:
            for worker in workers.values():
                fn(worker)
            return
        futures = [executor.submit(fn, worker) for worker in workers.values()]
        for future in futures:
            future.result()  # re-raise the first failure (incl. BaseException) in the driver

    try:
        while True:
            if (timeout_requested is None and time_limit
                    and time.monotonic() - started - (paused() if paused else 0) >= time_limit
                    and operator.call("games/observe", game_id=game_id)["status"] != "awaiting_setup"):
                operator.call("games/operator", game_id=game_id, operation="TIMEOUT")
                timeout_requested = time.monotonic()
            if timeout_requested is None:
                step_all(lambda worker: worker.step())
            else:
                # No new player decisions after timeout; allow GM publication/report.
                for worker in workers.values():
                    worker.gm.step()
            state = operator.call("games/observe", game_id=game_id)
            if state["ended"] and state["gm_report"] is not None:
                break
            if progress and state["cycle_id"] != last_cycle and (state["cycle_id"] == 1 or state["cycle_id"] % 10 == 0):
                print(f"cycle {state['cycle_id']} · active {len(state['active_agents'])}", file=sys.stderr, flush=True)
            last_cycle = state["cycle_id"]
            # [FIX-C10] The loop polled the engine without pause; while every worker only waits
            # (e.g. for trades and answers to settle) that opened hundreds of HTTP connections
            # a second and exhausted Windows sockets (WinError 10055). A short pause per round
            # costs nothing next to model calls.
            time.sleep(POLL_PAUSE_SECONDS)
            if timeout_requested is not None and time.monotonic() - timeout_requested > 60:
                raise RuntimeError("official timeout recorded, but GM finalization failed within 60s")
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    gm_worker = next(w.gm for w in workers.values() if w.gm.gm is not None and w.gm.gm.reported)
    record = gm_worker.gm.report()
    players = [p for w in workers.values() for p in w.runtimes]
    # [FIX-C3] Game-end hook. Players never observe the ended game themselves (after a
    # time_out only the GM is stepped), so the runner tells each runtime the game is over.
    # B uses it to read its official feedback and store what it learned for the next game.
    # [FIX-A40/B55] a first-ranked player may now give part of its score of this game (its own LLM call);
    # then the runner closes the sharing and the engine publishes the cumulative standings to everyone.
    for p in players:
        hook = getattr(p, "share_after_game", None)
        if callable(hook):
            try:
                hook(state)
            except Exception as exc:   # sharing is optional and must never break the official record
                print(f"share_after_game failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        sharing = operator.call("games/sharing-close", game_id=game_id)
    except Exception as exc:
        sharing = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        print(f"sharing-close failed: {sharing['error']}", file=sys.stderr)
    end_hooks = []
    for p in players:
        hook = getattr(p, "on_game_end", None)
        if callable(hook):
            try:
                end_hooks.append(hook(state) or "ok")
            except Exception as exc:  # learning must never break the official record
                end_hooks.append(f"failed: {type(exc).__name__}: {exc}"[:200])
                print(f"on_game_end failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    verdicts = [v for a in state["announcements"] if a["kind"] == "deduction_result" for v in a["verdicts"]]
    return {"game_id": game_id, "seed": seed, "resolution": state["resolution"], "cycles": state["cycle_id"],
            "players": len(state["role_assignments"]), "winners": [v for v in verdicts if v["winner"]],
            "deaths": [v["agent_id"] for v in verdicts if not v["winner"]],
            "gm": {"controller_id": gm_worker.gm.controller_id, "term": gm_worker.gm.term,
                   "failovers": state["failovers"]},
            "gm_report": state["gm_report"], "engine_metrics": state["metrics"],
            "sharing": sharing.get("sharing"), "standings": sharing.get("standings"),   # [FIX-A40]
            "scenario": [line["text"] for line in record["scenario"]],
            "gm_memory": record.get("gm_memory"),   # [FIX-C15] incidents, remedies, learned thresholds
            "player_runtime": sorted({type(p).__name__ for p in players}) or None,
            "player_fallbacks": sum(getattr(p, "fallbacks", 0) for p in players),
            # [FIX-C5] model-level stand-ins inside the player (B counts them, see FIX-B9)
            "model_calls": sum(getattr(p, "model_calls", 0) for p in players),
            "model_fallbacks": sum(getattr(p, "model_fallbacks", 0) for p in players),
            "say_do_mismatches": _merge(getattr(p, "say_do_mismatches", {}) for p in players),
            "answer_repairs": sum(getattr(p, "answer_repairs", 0) for p in players),
            "trade_activity": _merge(dict(getattr(p, "trade_activity", {})) for p in players),
            "live_llm": any(getattr(p, "live_llm", False) for p in players),
            "end_hooks": end_hooks,
            "elapsed_seconds": round(time.monotonic() - started, 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engine", default=str(DEFAULT_ENGINE), help="A's Game-engine checkout")
    parser.add_argument("--player", help="B player factory as package.module:callable (default: scripted stand-in)")
    parser.add_argument("--player-num", type=int, choices=range(3, 13), default=12, help="playable participants (3..12); GM excluded")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--deadline", type=int, help="override deadline_cycles (default 120)")
    parser.add_argument("--data", help="persistent SQLite path; default is a temporary database")
    parser.add_argument("--out", help="write the run summary JSON here")
    parser.add_argument("--time-limit", type=int, default=3600)
    parser.add_argument("--parallel", action="store_true",
                        help="step pool members concurrently (FIX-C6): LLM calls overlap")
    parser.add_argument("--interaction-order", choices=("immediate", "simultaneous"), default=None,
                        help="engine rule interaction_order (FIX-A17)")
    parser.add_argument("--clue-flow", choices=("v13", "boosted"), default=None,
                        help="engine rule clue_flow (FIX-A19)")
    parser.add_argument("--question-timeout", type=int, default=None,
                        help="engine rule question_timeout in seconds (default 30)")
    parser.add_argument("--unsolved-score", type=int, default=None,
                        help="engine rule unsolved_score (FIX-A3); omitted = engine default -10 (FIX-A24)")
    parser.add_argument("--progress", action="store_true")
    # [FIX-I19] user decisions 2026-11-12: the v10 rules in one place (engine defaults keep v9 for comparison runs)
    parser.add_argument("--profile", choices=("v10", "v9"), default="v10",
                        help="v10 = relationships from reactions (A52), ordered trades (A56), no talk rooms (A55)")
    parser.add_argument("--fault-plan", default=None,
                        help="JSON list of disconnection scenarios (engine A57), e.g. [{\"kind\": \"player\", \"cycle\": 8}]")
    parser.add_argument("--assignment", default=None,
                        help="[FIX-C24] JSON {agent_id: character_id}: this game's participants and characters")
    args = parser.parse_args()
    from .role_policy import REQUESTED_PLAYERS
    from . import role_policy
    REQUESTED_PLAYERS.set(args.player_num)
    if args.assignment:
        role_policy.ASSIGNMENT = json.loads(args.assignment)
    engine_dir = Path(args.engine).resolve()
    if not (engine_dir / "competitive.py").is_file():
        parser.error(f"A engine not found at {engine_dir}")
    rules = {"deadline_cycles": args.deadline} if args.deadline else {}
    if args.profile == "v10":
        rules.update(relationship_source="reactions", trade_resolution="ordered", talk_rounds=0,
                     lease_expiry="link",   # [FIX-A60] user 2026-11-16: unplanned faults are the GM's to learn
                     cut_player_sits_out=True,   # [FIX-A61] user 2026-11-17: an NPC-made player sits the game out
                     gm_stand_in_credit=True,    # [FIX-A62] user 2026-11-17: a GM stand-in is credited at its average
                     gm_absent_wait_cost=1)      # [FIX-A63] user 2026-11-18: each round of waiting costs a deadline cycle
    if args.fault_plan:
        rules["fault_plan"] = json.loads(args.fault_plan)
    if args.unsolved_score is not None:
        rules["unsolved_score"] = args.unsolved_score
    if args.interaction_order:
        rules["interaction_order"] = args.interaction_order
    if args.clue_flow:
        rules["clue_flow"] = args.clue_flow
    if args.question_timeout is not None:
        rules["question_timeout"] = args.question_timeout
        # [FIX-B17] a trade answer is another model call, so it gets the same response window
        rules["trade_timeout"] = args.question_timeout
    factory = load_factory(args.player)
    sources = [engine_dir / p for p in ("integration.py", "competitive.py", "competitive_rules.py", "competitive_catalog.json")]
    provenance = {"engine_commit": git_head(engine_dir), "gm_commit": git_head(ROOT),
                  "engine_tree_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sources)).hexdigest()[:16],
                  "player_num": str(args.player_num),
                  "player": args.player or "GM_Agent_Society.agent_runtime:ScriptedPlayerRuntime",
                  "command": " ".join(["run_scenario"] + sys.argv[1:])[:500]}
    with tempfile.TemporaryDirectory() as temporary:
        # [FIX-C4] Resolve now: the engine runs with cwd=Game-engine, so a relative path
        # used to create the DB under Game-engine/ instead of where the user asked.
        data = Path(args.data).resolve() if args.data else Path(temporary) / "scenario.sqlite3"
        data.parent.mkdir(parents=True, exist_ok=True)
        with engine_process(engine_dir, data) as (url, token):
            # [FIX-I13] the engine waits up to 60 s for its DB lock (integration.py); a 30 s client gave up first
            result = run(EngineV1Client(url, timeout=ENGINE_CLIENT_TIMEOUT), token, args.seed, rules, factory, provenance,
                         args.time_limit, args.progress, parallel=args.parallel)
    result["provenance"] = provenance
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
