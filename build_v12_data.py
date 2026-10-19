"""Extract the v1.2 character sheets and sub-mission list into `v12_data.json`.

python -m gm_coordinator.build_v12_data

Sources are the spec (A1 table) and the sub-mission design (2.5/2.6 tables), so the
GM policy runs on the agreed data before the engine catalog (request R2) exists.
When the engine catalog ships these fields, the GM reads the catalog instead.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Engine order (Game-engine/engine.py PLACES, JOBS); character i holds job i.
PLACES = ["라운지 칸", "침대 칸", "식당 칸", "주방 칸", "수하물 칸", "금고 칸", "전망 칸", "기관실"]
JOBS = ["탐정", "배우", "사업가", "사기꾼", "교수", "대령", "의사", "상속인", "기술자", "요리사", "차장", "기자"]
MBTI = [a + b + c + d for a in "EI" for b in "SN" for c in "TF" for d in "JP"]
TIERS = ["하", "중", "상"]
STAT_TOKENS = {"관찰5": "observe", "화술5": "speech", "위압5": "pressure"}
DESIGNATE = {"캐릭터": "character", "플레이어": "player", "NPC": "npc", "간접 단서": "indirect_clues", "장소": "place"}


def _rows(path, pattern):
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(pattern, line):
            yield [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tokens(cell):
    return [] if cell in ("", "—") else [token.strip() for token in cell.split(",")]


def characters(spec):
    rows = [cells for cells in _rows(spec, r"^\| \d+ \| ") if len(cells) == 9 and cells[2] in JOBS]
    return [{"id": int(cells[0]), "name": cells[1], "job": cells[2], "job_id": JOBS.index(cells[2]),
             "start_room": PLACES.index(cells[3]),
             "stats": {"observe": int(cells[4]), "speech": int(cells[5]), "pressure": int(cells[6])},
             "skill": {"name": cells[7], "effect": cells[8]}} for cells in rows]


def missions(design):
    result = []
    for cells in _rows(design, r"^\| (O\d|M\d\d) \| "):
        mission_id, name, condition, base, easier, harder, interference = cells
        designate = re.search(r"GM이 지정한 (캐릭터|플레이어|NPC|간접 단서|장소)", condition)
        count = re.search(r"간접 단서 (\d+)개", condition) if designate and designate.group(1) == "간접 단서" else None
        mission = {"id": mission_id, "name": name, "condition": condition,
                   "base": base if base in TIERS else None,
                   "easier": _tokens(easier), "harder": _tokens(harder),
                   "interference": "" if interference == "—" else interference,
                   "designate": DESIGNATE[designate.group(1)] if designate else None,
                   "designate_count": int(count.group(1)) if count else (1 if designate else 0)}
        quantity = re.findall(r"N=(\d+) (하|중|상)", condition)
        if quantity:
            mission["quantity"] = {n: tier for n, tier in quantity}
        result.append(mission)
    return result


def build():
    spec, design = HERE / "GAME_SPEC_v1.2.md", HERE / "SUBMISSION_INDIRECT_CLUE_v1.0.md"
    return {"schema_version": "gm-v12-data-0.1",
            "provenance": {"characters": "GAME_SPEC_v1.2.md A1", "missions": "SUBMISSION_INDIRECT_CLUE_v1.0.md 2.5-2.6",
                           "ids": "Game-engine/engine.py PLACES/JOBS order", "license": "Hunka Dunka (2024), CC BY-SA 4.0"},
            "places": PLACES, "jobs": JOBS, "mbti": MBTI, "tiers": TIERS, "stat_tokens": STAT_TOKENS,
            "characters": characters(spec), "missions": missions(design)}


def main():
    data = build()
    (HERE / "v12_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print({"characters": len(data["characters"]), "missions": len(data["missions"])})


if __name__ == "__main__":
    main()
