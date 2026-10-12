"""Replaceable A/B/C wire contracts. Values and IDs are JSON serializable."""
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

VERSION = "gm-provisional-v0.1"
PHASES = ("movement", "interaction", "deduction")
ALLOWED_ACTIONS = {
    "movement": ("move", "stay"),
    "interaction": ("explore", "question", "examine_corpse", "prepare", "pass"),
    "deduction": ("accuse", "pass"),
}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Control:
    controller_id: str
    term: int


@dataclass(frozen=True)
class ActionRequest:
    game_id: str
    agent_id: str
    action_id: str
    cycle_id: int
    phase: str
    phase_epoch: int
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None

    def __post_init__(self):
        for name in ("game_id", "agent_id", "action_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractError(f"{name} must be a nonempty string")
        for name in ("cycle_id", "phase_epoch"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ContractError(f"{name} must be a positive integer")
        if self.phase not in PHASES or self.action not in ALLOWED_ACTIONS[self.phase]:
            raise ContractError("action not allowed in requested phase")
        if not isinstance(self.payload, dict):
            raise ContractError("payload must be an object")
        if self.task_id is not None and not isinstance(self.task_id, str):
            raise ContractError("task_id must be a string or null")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ContractError("request must be an object")
        try:
            return cls(**value)
        except TypeError as error:
            raise ContractError(str(error)) from error


class EnginePort(Protocol):
    def catalog(self) -> dict: ...
    def create_game(self, game_id: str, assignments: dict, seed: int, rules: dict | None = None) -> None: ...
    def gm_snapshot(self, game_id: str) -> dict: ...
    def agent_snapshot(self, game_id: str, agent_id: str) -> dict: ...
    def events_after(self, game_id: str, sequence: int) -> list[dict]: ...
    def submit_action(self, request: ActionRequest) -> dict: ...
    def advance_phase(self, game_id: str, control: Control, phase_epoch: int) -> None: ...
    def convert_fault(self, game_id: str, agent_id: str, reason: str, control: Control) -> None: ...
    def take_over(self, game_id: str, previous_control: Control) -> Control: ...
    def finish(self, game_id: str, reason: str, control: Control) -> None: ...


class PlayerPort(Protocol):
    def choose_action(self, phase_notice: dict, observation: dict) -> ActionRequest: ...
    def receive_result(self, result: dict) -> None: ...


class InformationSharingPort(Protocol):
    """Optional publication capability, separate from required GM supervision."""
    def share_information(self, game_id: str, agent_id: str, information_ids: list[str], message_id: str) -> dict: ...
