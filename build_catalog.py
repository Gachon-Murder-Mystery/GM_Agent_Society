"""One-time extraction from the supplied demo, with explicit provenance."""
import importlib.util
import json
from pathlib import Path


def build():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("ornate_source", root / "ornate_gm_demo" / "engine.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    # These skill bindings are temporary engine data, not official rulebook skills.
    scopes = {
        0: ["explore", "question", "examine_corpse"], 1: ["question"],
        2: ["explore@5"], 3: ["question"], 4: [], 5: [],
        6: ["examine_corpse"], 7: [], 8: ["explore@7"],
        9: ["explore@3"], 10: [], 11: ["question"],
    }
    goals = [
        ("steal_vault", "금고에서 물건 훔치기"), ("protect_secret", "비밀 지키기"),
        ("stop_train", "열차를 멈추게 하기"), ("prove_innocence", "자신의 무죄 증명하기"),
        ("find_love", "사랑 찾기"), ("uncover_secret", "더 거대한 비밀 밝혀내기"),
        ("keep_awesome", "그냥 계속 멋지게 있기"),
    ]
    return {
        "schema_version": "gm-provisional-v0.1",
        "provenance": {
            "rulebook_en": "Murder_Mystery on_the_Ornate_Express_V1.0.pdf, pp. 1-2",
            "rulebook_ko": "Murder_Mystery_on_the_Ornate_Express_KO.pdf, pp. 1-3",
            "demo": "ornate_gm_demo/engine.py",
            "author": "Hunka Dunka (2024)", "license": "CC BY-SA 4.0",
            "temporary": ["named skill scopes", "disclosure caps and job bonuses", "job stat defaults", "goal scores"],
        },
        "places": [{"id": i, "name": name, "description": demo.ROOMS[i]} for i, name in enumerate(demo.PLACES)],
        "suspects": [{"id": i, "name": name, "job_id": i, "initial_room": demo.NPC_ROOMS[i],
                      "description": demo.PERSONALITIES[i], "background": demo.BACKGROUNDS[i]} for i, name in enumerate(demo.SUSPECTS)],
        "weapons": [{"id": i, "name": name} for i, name in enumerate(demo.WEAPONS)],
        "jobs": [{"id": i, "name": name, "stat_options": [2, 3, 4, 5],
                  "default_stat": 2 + i % 4,
                  "disclosure_bonus": int(i in (0, 1, 3, 11)),
                  "skills": [{"id": f"expert_{i}", "dice_bonus": 1, "scopes": scopes[i], "provisional": True}]}
                 for i, name in enumerate(demo.JOBS)],
        "personalities": [
            {"id": "warm", "name": "친화적", "disclosure_cap": 2, "reasoning_hint": "협력과 정보 교환을 선호한다", "provisional": True},
            {"id": "neutral", "name": "신중함", "disclosure_cap": 1, "reasoning_hint": "근거를 확인한 뒤 선택적으로 공유한다", "provisional": True},
            {"id": "guarded", "name": "경계심", "disclosure_cap": 1, "reasoning_hint": "개인 목표와 비밀 보호를 우선 검토한다", "provisional": True},
        ],
        "personal_goals": [{"id": ident, "name": name, "required": False, "bonus_score": None,
                            "evaluation": "not_implemented"} for ident, name in goals],
        "rules": {
            "phase_order": ["movement", "interaction", "deduction"],
            "actions_per_phase": 1, "npc_random_movement": True,
            "corpse_decay_per_cycle": 1, "job_duplicates": True,
            "prepared_bonus_dice": 1, "base_dice": 1,
            "wrong_accusation": "death", "zero_active_players": "gm_termination",
        },
    }


if __name__ == "__main__":
    path = Path(__file__).with_name("catalog.json")
    path.write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
