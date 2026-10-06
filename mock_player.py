"""Temporary B adapter: scripted investigation, not an LLM reasoning agent."""
from .contracts import ActionRequest


class MockPlayer:
    def __init__(self, agent_id, index=0):
        self.agent_id, self.index = agent_id, index
        self.results = []

    def choose_action(self, phase_notice, observation, force_death=False):
        phase = phase_notice["phase"]
        action, payload = "pass", {}
        if phase == "movement":
            action, payload = "move", {"room": (phase_notice["cycle_id"] - 1 + self.index + 2) % 8}
        elif phase == "interaction":
            action = "explore"
            payload = {"approach": "compassion" if observation["self"]["stat"] <= 3 else "brutality"}
        else:
            known = observation["information"] + observation["shared_information"]
            candidates = {
                category: [item["id"] for item in items if item["id"] not in
                           {clue["excluded_id"] for clue in known if clue["category"] == category}]
                for category, items in observation["candidates"].items()
            }
            if force_death and known:
                # Uses an already excluded candidate; never reads the hidden answer.
                clue = known[0]
                payload = {key: values[0] for key, values in candidates.items()}
                payload[clue["category"]] = clue["excluded_id"]
                action = "accuse"
            elif all(len(values) == 1 for values in candidates.values()):
                action, payload = "accuse", {key: values[0] for key, values in candidates.items()}
        return ActionRequest(
            game_id=phase_notice["game_id"], agent_id=self.agent_id,
            action_id=f"{phase_notice['game_id']}:{phase_notice['phase_epoch']}:{self.agent_id}:action",
            cycle_id=phase_notice["cycle_id"], phase=phase, phase_epoch=phase_notice["phase_epoch"],
            action=action, payload=payload, task_id=phase_notice["task_id"],
        )

    def receive_result(self, result):
        self.results.append(result)
