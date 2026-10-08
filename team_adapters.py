"""Connect the current A/B demo components without inventing missing authority.

The legacy functions are usable for a single actor. They are NOT a complete
EnginePort or PlayerPort. Full GM attachment fails explicitly until A/B expose
the required interfaces; the existing mock implementations remain separate.
"""
import builtins
import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path

from .contracts import ContractError, EnginePort, InformationSharingPort, PlayerPort
from .message_language import normalize_acquired_clue


class IntegrationUnavailable(ContractError):
    def __init__(self, message, missing_methods=()):
        super().__init__(message)
        self.missing_methods = list(missing_methods)


def load_legacy_module(path, dependencies=None):
    """Bind flat `from engine import ...` to the correct repository module.

    Uses module-local import binding; never replaces global sys.modules['engine']
    or modifies sys.path, so the A and B copies can coexist in one process.
    """
    path = Path(path).resolve()
    name = "_team_component_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise IntegrationUnavailable(f"cannot load component: {path}")
    module = importlib.util.module_from_spec(spec)
    imports = dependencies or {}
    original_import = builtins.__import__

    def local_import(import_name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and import_name in imports:
            return imports[import_name]
        return original_import(import_name, globals, locals, fromlist, level)

    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": local_import}
    # Native components have ordinary imports and no entrypoint is invoked here.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def port_methods(port):
    return [name for name, value in vars(port).items() if not name.startswith("_") and callable(value)]


class NativeEngineBridge:
    def __init__(self, repository):
        self.repository = Path(repository).resolve()
        self.module = load_legacy_module(self.repository / "engine.py")
        self._legacy_game = None

    def capabilities(self):
        required = port_methods(EnginePort)
        return {
            "native_functions": [name for name in ("new_game", "apply", "public", "validate_intent") if callable(getattr(self.module, name, None))],
            "required_engine_port_methods": required,
            "native_engine_port_methods_present": [name for name in required if callable(getattr(self.module, name, None))],
            "native_engine_port_methods_missing": [name for name in required if not callable(getattr(self.module, name, None))],
            "optional_engine_methods": {name: callable(getattr(self.module, name, None)) for name in port_methods(InformationSharingPort)},
            "bridge_scope": "single-player native functions and provisional catalog normalization",
            "full_gm_compatible": False,
        }

    def catalog(self):
        """Use real A labels; stat/skill/personality details remain provisional."""
        catalog = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
        if [job["name"] for job in catalog["jobs"]] != list(self.module.JOBS):
            raise IntegrationUnavailable("A job catalog changed; structured job metadata must be mapped first")
        for collection, labels in (("places", self.module.PLACES), ("weapons", self.module.WEAPONS), ("suspects", self.module.SUSPECTS)):
            if len(catalog[collection]) != len(labels):
                raise IntegrationUnavailable(f"A {collection} IDs changed; explicit mapping required")
            for item, label in zip(catalog[collection], labels):
                item["name"] = label
        catalog["native_catalog_source"] = str(self.repository / "engine.py")
        catalog["native_catalog_note"] = "Names and IDs from A; structured skill/stat/personality policies from provisional GM catalog."
        return catalog

    def create_game(self, game_id, assignments, seed, rules=None):
        missing = self.capabilities()["native_engine_port_methods_missing"]
        raise IntegrationUnavailable(
            "A exposes new_game(name, role, stat, goal), not an authoritative multi-player "
            "create_game/phase/observation/event contract. Full GM attachment is blocked before game creation.",
            missing,
        )

    def legacy_start(self, name="연동 점검 플레이어", role=0, stat=2, goal="비밀 지키기"):
        self._legacy_game = self.module.new_game(name, role, stat, goal)
        return self.legacy_observation()

    def legacy_observation(self):
        if self._legacy_game is None:
            raise IntegrationUnavailable("single-player probe not started")
        return copy.deepcopy(self.module.public(self._legacy_game))

    def legacy_execute(self, intent, text="", rng=None):
        if self._legacy_game is None:
            raise IntegrationUnavailable("single-player probe not started")
        before = {clue["id"] for clue in self.legacy_observation()["clues"]}
        validated = self.module.validate_intent(intent, self._legacy_game)
        message = self.module.apply(self._legacy_game, validated, text, rng=rng)
        observation = self.legacy_observation()
        return {
            "record_type": "legacy_client_observation", "source": "actual A apply/public functions",
            "intent": validated, "message": message, "observation": observation,
            "acquired_clues": [clue for clue in observation["clues"] if clue["id"] not in before],
            "engine_completion_event": False,
        }


class NativeReasoningBridge:
    def __init__(self, repository):
        self.repository = Path(repository).resolve()
        self.engine_module = load_legacy_module(self.repository / "engine.py")
        self.module = load_legacy_module(self.repository / "llm.py", {"engine": self.engine_module})
        self.interpreter = self.module.Zen(key="")
        self.last_input = None

    def capabilities(self):
        required = port_methods(PlayerPort)
        return {
            "native_interpreter": "Zen.intent(g, text)", "mode": "offline_rule_parser" if callable(getattr(self.interpreter, "intent", None)) else "legacy_interpreter_unavailable",
            "legacy_interpreter_available": callable(getattr(self.interpreter, "intent", None)),
            "next_player_interface": "ReasoningPlayerAdapter wrapping B sync/decide/ingest_result",
            "native_narrator": "Zen.narrate(g, text, result)",
            "player_port_methods_missing": [name for name in required if not callable(getattr(self.module, name, None))],
            "autonomous_player_loop_verified": False,
            "live_llm_calls": False,
        }

    def private_safe_legacy_input(self, observation):
        """Minimal observed fields required by B's keyless local_intent branch."""
        try:
            role = list(self.engine_module.JOBS).index(observation["job"])
        except (KeyError, ValueError) as error:
            raise IntegrationUnavailable("B job ID mapping does not match A observation") from error
        target = observation.get("target")
        target_id = None
        if target is not None:
            match = [npc["id"] for npc in observation["npcs"] if npc["name"] == target]
            if len(match) != 1:
                raise IntegrationUnavailable("B target ID mapping is ambiguous")
            target_id = match[0]
        return {"name": observation["name"], "role": role,
                "pending": copy.deepcopy(observation["pending"]), "target": target_id}

    def interpret(self, text, observation):
        if not self.capabilities()["legacy_interpreter_available"]:
            raise IntegrationUnavailable("B removed Zen.intent; use an executable B LLMAgent instance with ReasoningPlayerAdapter", ["Zen.intent"])
        self.last_input = self.private_safe_legacy_input(observation)
        intent, notice = self.interpreter.intent(self.last_input, text)
        return {"intent": intent, "notice": notice, "input_keys": list(self.last_input),
                "input_source": "A public observation whitelist", "mode": "offline_rule_parser"}

    def choose_action(self, phase_notice, observation):
        raise IntegrationUnavailable("Legacy Zen bridge cannot provide PlayerPort; wrap an executable B instance with ReasoningPlayerAdapter")


class LegacyGMObserver:
    """C-side receipt recording only. This is not the GM turn state machine."""
    def __init__(self, store):
        self.store = store

    def observe(self, observation, actor_id="legacy_single_player"):
        game_id = observation["id"]
        record = {
            "game_id": game_id, "actor_id": actor_id,
            "actor_id_source": "probe alias; A has no native agent_id",
            "room": observation["room"],
            "information_ids": [clue["id"] for clue in observation["clues"]],
            "information": copy.deepcopy(observation["clues"]),
            "normalized_information": [normalize_acquired_clue(clue)["information"] for clue in observation["clues"]],
            "source": "actual A public observation; polled/client-received",
            "engine_completion_event": False,
            "official_phase_token": None,
        }
        with self.store.transaction() as db:
            self.store.put(db, "legacy_integration_observer", game_id, record)
        return record
