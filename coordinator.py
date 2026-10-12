"""C-owned GM logic. Does not execute player moves or determine dice results."""
import random

from .contracts import ALLOWED_ACTIONS, ContractError, Control, EnginePort
from .store import Store


class Coordinator:
    def __init__(self, engine: EnginePort, store: Store, game_id: str, control: Control):
        self.engine, self.store, self.game_id, self.control = engine, store, game_id, control

    @classmethod
    def start(cls, engine, store, game_id, agent_ids, seed=42, rules=None):
        if not agent_ids or len(set(agent_ids)) != len(agent_ids):
            raise ContractError("unique nonempty player roster required")
        catalog = engine.catalog()
        rng = random.Random(seed)
        assignments = {}
        for ident in agent_ids:
            job = rng.choice(catalog["jobs"])
            assignments[ident] = {
                "job": job, "stat": job["default_stat"],
                "personality": rng.choice(catalog["personalities"]),
                "personal_goal": rng.choice(catalog["personal_goals"]),
            }
        engine.create_game(game_id, assignments, seed, rules)
        gm = cls(engine, store, game_id, Control("gm-primary", 1))
        gm.sync()
        return gm

    def _authorize(self, snapshot):
        if snapshot["control"] != vars(self.control):
            raise ContractError("this GM has lost authority; resume as the current controller")

    def sync(self):
        snapshot = self.engine.gm_snapshot(self.game_id)
        self._authorize(snapshot)
        # Read events outside the GM transaction: production A/C stores may differ.
        previous = self.store.read("gm", self.game_id) or {"event_cursor": 0, "action_ledger": {}, "event_log": []}
        events = self.engine.events_after(self.game_id, previous["event_cursor"])
        with self.store.transaction() as db:
            ledger = self.store.get(db, "gm", self.game_id) or previous
            if ledger["event_cursor"] > snapshot["event_sequence"] or ledger.get("control", {}).get("term", 0) > self.control.term:
                return ledger  # A newer synchronization has already committed.
            for event in events:
                if event["sequence"] > snapshot["event_sequence"]:
                    break  # Process a consistent engine snapshot; defer later events.
                if event["sequence"] <= ledger["event_cursor"]:
                    continue
                if event["sequence"] != ledger["event_cursor"] + 1:
                    raise ContractError(f"missing engine event {ledger['event_cursor'] + 1}; re-query before advancing GM state")
                ledger["event_cursor"] = event["sequence"]
                ledger["event_log"].append({"event_id": event["event_id"], "type": event["type"], "sequence": event["sequence"]})
                if event["type"] == "action_completed":
                    result = event["payload"]["result"]
                    ledger["action_ledger"][result["action_id"]] = result
            if ledger["event_cursor"] != snapshot["event_sequence"]:
                raise ContractError("engine events do not cover the snapshot; re-query before advancing GM state")
            ledger.update(snapshot)
            self.store.put(db, "gm", self.game_id, ledger)
        return ledger

    def phase_notices(self):
        snapshot = self.sync()
        if snapshot["ended"] or snapshot["resolution"]:
            return {}
        notices = {}
        for ident in snapshot["active_agents"]:
            if ident in snapshot["spent"]:
                continue
            observation = self.engine.agent_snapshot(self.game_id, ident)
            notice = {
                "game_id": self.game_id, "agent_id": ident, "cycle_id": snapshot["cycle_id"],
                "phase": snapshot["phase"], "phase_epoch": snapshot["phase_epoch"],
                "task_id": f"{self.game_id}:{snapshot['phase_epoch']}:{ident}",
                "allowed_actions": list(ALLOWED_ACTIONS[snapshot["phase"]]), "remaining_actions": 1,
                "self": observation["self"], "occupants": observation["occupants"],
                "shared_information": snapshot["shared_information"],
            }
            if snapshot["phase"] == "movement":
                notice["positions"] = snapshot["actors"]  # Presence only, never global inventories.
            notices[ident] = notice
        return notices

    def tick(self):
        snapshot = self.sync()
        if snapshot["ended"]:
            return "ended"
        if snapshot["resolution"] == "solved":
            self.engine.finish(self.game_id, "solved", self.control)
            self.sync()
            return "ended"
        if not snapshot["active_agents"]:
            self.engine.finish(self.game_id, snapshot["last_removal_reason"], self.control)
            self.sync()
            return "ended"
        if all(ident in snapshot["spent"] for ident in snapshot["active_agents"]):
            self.engine.advance_phase(self.game_id, self.control, snapshot["phase_epoch"])
            self.sync()
            return "advanced"
        return "waiting"

    def report_player_fault(self, agent_id, reason):
        self.engine.convert_fault(self.game_id, agent_id, reason, self.control)
        return self.sync()

    @classmethod
    def replace_failed_gm(cls, engine, store, game_id, previous_control):
        """Called by a surviving runtime/watchdog, not by the failed GM process.

        The engine chooses and fences the successor. The new GM then converts
        their vacated player character into an NPC through the normal GM command.
        """
        control = engine.take_over(game_id, previous_control)
        gm = cls(engine, store, game_id, control)
        gm.sync()
        gm.reconcile_successor_slot()
        return gm

    def reconcile_successor_slot(self):
        """Finish NPC conversion if a newly promoted GM stopped after takeover."""
        snapshot = self.sync()
        actor = next((a for a in snapshot["actors"] if a["id"] == self.control.controller_id), None)
        if actor is not None and actor["mode"] == "fault":
            self.engine.convert_fault(self.game_id, actor["id"], "promoted_to_gm", self.control)
            return self.sync()
        return snapshot
