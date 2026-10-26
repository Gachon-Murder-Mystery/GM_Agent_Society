"""Unified v1.3 settings (single source: competitive_settings.json) and the engine drift check.

Pure-function policy modules read the same JSON directly so A's launcher can still
load them one file at a time. The GM checks this file against the engine catalog
before it acts, so a changed engine default stops the GM instead of skewing results.
"""
import json
from pathlib import Path

PATH = Path(__file__).with_name("competitive_settings.json")
DATA_PATH = Path(__file__).with_name("v12_data.json")


def load(path=PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


SETTINGS = load()


def verify_engine(catalog, settings=SETTINGS, data_path=DATA_PATH):
    """Mismatches between the engine catalog and the GM's settings/data ([] when unified)."""
    problems = []
    if catalog.get("ruleset") != settings["ruleset"]:
        problems.append(f"ruleset: engine {catalog.get('ruleset')!r}, GM {settings['ruleset']!r}")
    rules = catalog.get("default_rules", {})
    for key, value in settings["engine_rules"].items():
        if rules.get(key) != value:
            problems.append(f"default_rules.{key}: engine {rules.get(key)!r}, GM {value!r}")
    for key in sorted(set(rules) - set(settings["engine_rules"])):
        problems.append(f"default_rules.{key}: engine-only rule {rules[key]!r}")
    if catalog.get("allowed_actions") != settings["allowed_actions"]:
        problems.append("allowed_actions differ")
    if catalog.get("scoring") != settings["scoring"]:
        problems.append(f"scoring: engine {catalog.get('scoring')!r}, GM {settings['scoring']!r}")
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    if catalog.get("mbti") != data["mbti"]:
        problems.append("mbti order differs")
    if catalog.get("missions") != data["missions"]:
        problems.append("sub-mission catalog differs from v12_data.json")
    engine_chars = {c["id"]: c for c in catalog.get("characters", [])}
    for sheet in data["characters"]:
        mine = engine_chars.get(sheet["id"], {})
        if {k: mine.get(k) for k in sheet} != sheet:
            problems.append(f"character {sheet['id']} sheet differs")
    return problems
