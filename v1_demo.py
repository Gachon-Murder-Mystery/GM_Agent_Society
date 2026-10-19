"""Run C's v1 GM against A's real engine process. python -m gm_coordinator.v1_demo --help

The engine is started from the sibling Game-engine checkout with a temporary
DB. Players are explicit scripted test doubles (not B's LLM agent); the GM
policy is engine_v1.V1Coordinator. No LLM call or teammate import is made.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .engine_v1 import EngineV1Client, EngineV1Error, GMWorker
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("normal", "player-fault", "gm-fault", "human")


@contextmanager
def engine_process(repository=ROOT / "Game-engine", timeout=20):
    repository = Path(repository).resolve()
    if not (repository / "integration.py").exists():
        raise FileNotFoundError(f"A integration engine not found: {repository}")
    with tempfile.TemporaryDirectory() as directory:
        data = str(Path(directory) / "engine.sqlite3")
        env = os.environ | {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        credentials = subprocess.run([sys.executable, "-B", "integration_cli.py", "--data", data, "credentials"],
                                     cwd=repository, capture_output=True, text=True, encoding="utf-8",
                                     env=env, timeout=timeout, check=True)
        operator_token = json.loads(credentials.stdout)["operator_token"]
        process = subprocess.Popen([sys.executable, "-B", "app.py", "--no-browser", "--port", "0", "--data", data],
                                   cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", env=env)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        try:
            try:
                line = lines.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("engine did not start") from None
            url = next((part for part in line.split() if part.startswith("http://")), None)
            if url is None:
                raise RuntimeError(f"engine did not report its URL: {line!r} {process.stderr.read()[:500]}")
            yield EngineV1Client(url), operator_token
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            for stream in (process.stdout, process.stderr):
                stream.close()


class ScriptedPlayer:
    """Test double for B's runtime: follows its own observation and assigned tasks."""

    def __init__(self, identity, share=True):
        self.identity, self.share, self.agent_id = identity, share, identity.credentials["agent_id"]
        self.context_key, self.role = None, None
        self.actions = 0

    def _role(self):
        session = self.identity.call("pool/session")
        if session["role"] != "player":
            self.context_key = self.role = None
            return None
        if session["context_key"] != self.context_key:
            self.context_key, self.role = session["context_key"], self.identity.bind(**session["credentials"])
        return self.role

    def step(self):
        role = self._role()
        if role is None:
            return
        try:
            role.call("agent/heartbeat")
            obs = role.call("agent/observe")
            for question in role.call("questions/inbox")["questions"]:
                refuse = obs["self"]["personality"] == "competitive"
                offers = [] if refuse else [x["id"] for x in obs["information"] if "excluded_id" in x]
                role.call("questions/respond", question_id=question["question_id"], response_id=uuid.uuid4().hex,
                          offer_ids=offers, refused=refuse)
            if not obs["allowed_actions"]:
                return
            self._act(role, obs)
        except EngineV1Error as exc:
            if exc.code not in ("unauthorized", "expired", "not_running", "conflict"):
                raise

    def _act(self, role, obs):
        me, phase = obs["self"], obs["phase"]
        task = next((t for t in obs["tasks"] if t["status"] == "assigned"), None)
        action, payload, task_id = "pass", {}, None
        if phase == "movement":
            action, payload = ("move", {"room": task["room"]}) if task and task["room"] != me["room"] else ("stay", {})
        elif phase == "interaction":
            approach = "brutality" if me["stat"] >= 4 else "compassion"
            peers = [x["id"] for x in obs["occupants"] if x["mode"] == "active"]
            if task and task["room"] == me["room"] and task["kind"] == "question" and peers:
                action, payload, task_id = "question", {"target_id": peers[0], "approach": approach,
                                                        "topic": "미공유 자료 요청"}, task["task_id"]
            else:
                action, payload = "explore", {"approach": approach}
                if task and task["room"] == me["room"] and task["kind"] == "explore":
                    task_id = task["task_id"]
        else:
            remaining = {key: [c["id"] for c in values if not c["excluded"]] for key, values in obs["board"].items()}
            if all(len(values) == 1 for values in remaining.values()):
                action, payload = "accuse", {key: values[0] for key, values in remaining.items()}
        request = {key: obs[key] for key in ("game_id", "cycle_id", "phase", "phase_epoch")}
        request.update(agent_id=me["id"], action_id=uuid.uuid4().hex, action=action, payload=payload)
        if task_id:
            request.update(task_id=task_id, generation=task["generation"])
        result = role.call("agent/action", request=request)
        self.actions += 1
        new = [x["id"] for x in result.get("acquired_information", []) if "excluded_id" in x]
        if self.share and new and me["personality"] != "competitive":
            role.call("agent/share", message_id=uuid.uuid4().hex, information_ids=new)


def run(scenario="normal", players=2, reserves=2, seed=7, max_rounds=400, engine_repo=ROOT / "Game-engine",
        share=True):
    if scenario not in SCENARIOS or players < 1 or reserves < 0 or max_rounds < 1:
        raise ValueError("invalid scenario, roster or budget")
    with engine_process(engine_repo) as (client, operator_token):
        operator = client.bind(operator_token=operator_token)
        ids = [f"pool-{i + 1}" for i in range(1 + players + reserves)]
        tokens = {aid: operator.call("pool/register", agent_id=aid, model_version="scripted-double")["pool_token"]
                  for aid in ids}
        created = operator.call("games/create", game_id=f"v1-{scenario}-{seed}", seed=seed)
        game_id = created["game_id"]
        store = Store()
        identities = {aid: client.bind(agent_id=aid, pool_token=tokens[aid]) for aid in ids}
        workers = {aid: GMWorker(identities[aid], store, players) for aid in ids}
        bots = {aid: ScriptedPlayer(identities[aid], share) for aid in ids}
        injected, statuses, trace = [], {}, []
        try:
            for round_no in range(max_rounds):
                state = operator.call("games/observe", game_id=game_id)
                if state["ended"]:
                    break
                cycle = state["cycle_id"]
                if not injected and cycle == 3 and state["phase"] == "interaction":
                    injected.append(_inject(operator, game_id, state, scenario))
                controller = (state["control"] or {}).get("controller_id")
                # Only workers that hold or may have just received GM authority need a step.
                for aid in ids:
                    if aid != controller and workers[aid].coordinator is None:
                        continue
                    status = workers[aid].step()
                    statuses[status] = statuses.get(status, 0) + 1
                    if status in ("waiting_for_human", "fenced", "waiting_for_players"):
                        trace.append({"round": round_no, "cycle_id": cycle, "agent_id": aid, "gm_status": status})
                if state["status"] == "waiting_for_human" and scenario == "human":
                    injected.append(_appoint(operator, game_id, state, ids))
                for aid in operator.call("games/observe", game_id=game_id)["active_agents"]:
                    bots[aid].step()
            final = operator.call("games/observe", game_id=game_id)
            if not final["ended"]:
                operator.call("games/operator", game_id=game_id, operation="FINISH")
            gm = next((w.coordinator for w in workers.values() if w.coordinator), None)
            report = gm.report() if gm else None
            decisions = [record for w in workers.values() if w.coordinator
                         for record in w.coordinator.report()["decisions"]]
            return {"scenario": scenario, "seed": seed, "players_target": players, "reserves": reserves,
                    "provenance": "real A engine process + C V1Coordinator + scripted player doubles; no LLM",
                    "ended": final["ended"], "resolution": final["resolution"],
                    "solved": final["resolution"] == "solved", "cycle_id": final["cycle_id"],
                    "control": final["control"], "failovers": final["failovers"],
                    "metrics": final["metrics"], "gm_status_counts": statuses, "injected": injected,
                    "trace": trace, "plans_recorded_by_engine": len(final["plans"]),
                    "engine_plans": final["plans"], "assignments": [
                        {k: a.get(k) for k in ("agent_id", "role", "term", "status", "personality")}
                        for a in final["assignments"]],
                    "gm_report": report, "current_gm_decisions": len(decisions)}
        finally:
            store.close()


def _inject(operator, game_id, state, scenario):
    if scenario == "player-fault":
        victim = sorted(state["active_agents"])[0]
        operator.call("games/operator", game_id=game_id, operation="FAIL_PLAYER", agent_id=victim)
        return {"operation": "FAIL_PLAYER", "agent_id": victim, "cycle_id": state["cycle_id"]}
    if scenario == "gm-fault":
        operator.call("games/operator", game_id=game_id, operation="FAIL_GM", term=state["control"]["term"])
        return {"operation": "FAIL_GM", "term": state["control"]["term"], "cycle_id": state["cycle_id"]}
    if scenario == "human":
        for aid in sorted(state["active_agents"]):
            operator.call("games/operator", game_id=game_id, operation="FAIL_PLAYER", agent_id=aid)
        operator.call("games/operator", game_id=game_id, operation="FAIL_GM", term=state["control"]["term"])
        return {"operation": "FAIL_ALL_PLAYERS_THEN_GM", "cycle_id": state["cycle_id"]}
    return {"operation": "none"}


def _appoint(operator, game_id, state, ids):
    pool = {a["agent_id"]: a for a in operator.call("pool/list")["agents"]}
    choice = next(aid for aid in ids if pool[aid]["pool_status"] == "available")
    operator.call("games/operator", game_id=game_id, operation="APPOINT_GM", agent_id=choice)
    return {"operation": "APPOINT_GM", "agent_id": choice, "decided_by": "operator (scripted human stand-in)"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--players", type=int, default=2)
    parser.add_argument("--reserves", type=int, default=2)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-rounds", type=int, default=400)
    parser.add_argument("--engine-repo", type=Path, default=ROOT / "Game-engine")
    parser.add_argument("--no-share", action="store_true", help="Scripted players never publish clues")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run(args.scenario, args.players, args.reserves, args.seed, args.max_rounds, args.engine_repo,
                 not args.no_share)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {k: result[k] for k in ("scenario", "solved", "resolution", "cycle_id", "control", "failovers",
                                      "metrics", "gm_status_counts", "injected", "plans_recorded_by_engine")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
