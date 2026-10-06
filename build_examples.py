"""Capture actual offline scenario executions and representative wire messages."""
import json
from pathlib import Path

from .demo import run


def main():
    folder = Path(__file__).with_name("examples")
    folder.mkdir(exist_ok=True)
    summaries = []
    scenarios = [("normal", 3), ("player-fault", 3), ("gm-fault", 3),
                 ("death", 3), ("all-fault", 3), ("all-dead", 3), ("gm-fault", 1)]
    for scenario, agents in scenarios:
        result = run(scenario, agents, max_cycles=100)
        summary = {key: value for key, value in result.items() if key not in ("samples", "gm_final_snapshot")}
        summary["actors"] = result["gm_final_snapshot"]["actors"]
        summary["information_owners"] = result["gm_final_snapshot"]["inventories"]
        summaries.append(summary)
        print(json.dumps({key: summary[key] for key in ("scenario", "agents_initial", "agents_remaining", "ended", "end_reason", "control")}, ensure_ascii=True))
        if scenario == "normal":
            (folder / "normal.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (folder / "io_samples.json").write_text(json.dumps(result["samples"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (folder / "scenario_results.json").write_text(json.dumps({"scope": "local mock execution", "runs": summaries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
