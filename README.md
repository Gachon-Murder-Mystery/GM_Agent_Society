# 임시 GM 조정 구현 v0.1

2026-10-06 대화에서 확정한 GM 역할을 실행 가능한 형태로 만든 독립 패키지다. A의 엔진과 B의 플레이어 코드가 개발 중이므로, 기존 `ornate_gm_demo/engine.py`와 제공된 영문·한국어 룰북에서 구성한 임시 엔진·스크립트 플레이어로 연결했다.

GM은 캐릭터 조합 배분, 이동→탐문·탐색→추리 턴 진행, 상황 안내, 정보 소유 원장, 장애 처리와 대리 GM 전환을 담당한다. 플레이어 행동은 GM을 경유하지 않고 엔진에 직접 요청한다. 공식 상태·주사위·권한·중복 행동 방지는 엔진이 처리한다.

## 실행

워크스페이스 루트에서 Python 3.10 이상으로 실행한다. 실행과 테스트에는 외부 패키지나 LLM 연결이 필요 없다.

```powershell
python -m gm_coordinator.demo --scenario normal --agents 3 --out gm_coordinator/examples/normal.json
python -m gm_coordinator.demo --scenario player-fault --agents 3
python -m gm_coordinator.demo --scenario gm-fault --agents 3
python -m gm_coordinator.demo --scenario all-dead --agents 3
python -m unittest gm_coordinator.test_coordinator -v
```

`--agents`는 양의 정수이며 인원수가 고정되지 않는다. `--scenario`는 `normal`, `player-fault`, `gm-fault`, `all-fault`, `all-dead`, `death`를 지원한다. `--withhold`는 모의 플레이어의 자발적 공유를 끈다. `--corpse-decay 0`은 임시 시체 정보 감쇠 규칙을 끈다.

```powershell
python -m gm_coordinator.demo --db gm_coordinator/data/demo.sqlite3 --scenario normal --agents 3
```

같은 DB·시나리오·인원·seed로 다시 실행하면 저장된 게임을 이어받는다. 끝난 게임은 끝난 상태로 읽는다. 새 게임은 다른 seed 또는 별도 DB를 사용한다. `--max-cycles`는 실행 드라이버의 안전한 중단 한도이며 게임의 종료 규칙이 아니다. 한도에 도달하면 `driver_stopped=true`, `ended=false`로 보고한다.

## 파일

| 파일 | 역할 |
|---|---|
| `contracts.py` | 교체 가능한 EnginePort / PlayerPort, 행동 요청, GM 제어 식별자 |
| `coordinator.py` | C가 구현하는 GM 조정 로직 |
| `store.py` | GM 기록과 모의 엔진 상태·이벤트의 SQLite 저장 |
| `catalog.json` | 엔진에서 받을 임시 직업·성향·목표·장소·후보 데이터 |
| `mock_engine.py` | A의 실제 엔진으로 교체할 모의 구현 |
| `mock_player.py` | B의 실제 판단 루프로 교체할 스크립트 구현 |
| `demo.py` | 병렬 실행·장애 주입·입출력 예시 생성 |
| `IO_CONTRACT.md` | 다음 연결 시 사용할 호출·메시지 계약 |
| `SOURCE_RULES.md` | 룰북·데모·사용자 결정·임시 가정 구분 |
| `io_schema.json` | JSON 입출력의 형식 명세 |
| `examples/` | 실제 모의 실행에서 생성한 입출력·결과 |

`build_catalog.py`는 기존 데모의 공개 상수만 읽어 카탈로그를 재생성한다. 기존 데모 코드·키·플레이 DB를 변경하거나 읽지 않는다.

## 핵심 동작

- 매 턴 능동 플레이어당 행동 1회를 임시 기본값으로 사용한다. 추리는 `accuse` 또는 `pass`다. 이동 턴의 `stay`, 탐문·탐색 턴의 `pass`도 해당 턴의 행동을 소비한다.
- 이동 전에는 모든 캐릭터의 위치·유형을 안내한다. 이동 후에는 각 플레이어가 있는 공간의 상호작용 대상을 안내한다.
- 탐문·탐색 턴의 `question`에는 같은 공간의 다른 플레이어 또는 NPC만 지정할 수 있다. 시체는 `examine_corpse`로 조사한다. 이 단계에서 이동은 허용되지 않는다.
- GM은 획득 결과 전체와 플레이어 유래 캐릭터의 정보 소유 현황을 받는다. 플레이어 출력에는 본인의 정보와 명시적으로 공유된 정보만 들어간다. 미발견 정보와 정답은 GM에 전달하지 않는다.
- 장애 NPC는 정보·직업·스테이터스·스킬·성향·개인 목표 기록을 유지한다. 매 사이클 이동 단계 시작에 무작위 이동하고, 탐문·탐색·추리를 스스로 수행하지 않는다. 개인 목표 달성을 능동적으로 시도하지 않는다.
- 시체는 움직이지 않는다. 조사 결과는 엔진 주사위로 결정하며, 기본 임시 정책은 다음 사이클이 시작될 때 잔존 정보 1개를 무작위 소실시키는 것이다. 이미 다른 캐릭터가 획득하거나 공유한 정보는 지우지 않는다.
- 능동 플레이어 0명이면 GM이 엔진에 종료를 요청한다. 마지막 제외 원인에 따라 `technical_failure`와 `deduction_failure`를 구분한다. 사건 해결 역시 엔진 확인 후 GM이 종료를 기록한다.
- GM 대리 전환은 살아 있는 런타임/감시자가 호출한다. 안정적인 agent ID 순으로 1명을 선정하는 것은 임시 정책이다. 선정된 캐릭터는 NPC가 되고 런타임은 GM을 수행한다. 이전 GM의 제어 식별자는 엔진에서 거절된다.

## 검증의 범위

테스트는 턴 제한, 동시 중복 소비, 잘못된 요청의 원자적 거절, 정보 경계, 사망·장애 구분, NPC·시체 상호작용, 감쇠, GM 대리 전환, 재시작과 이벤트 재수신, 저장 실패 시 롤백을 확인한다.

이 결과는 로컬 모의 연동의 동작 확인이다. 실제 A/B 코드, 네트워크 장애, 운영체제의 프로세스 감시, 실시간 LLM 판단은 검증하지 않았다. `replace_failed_gm`과 `report_player_fault`는 연결 지점이며, 현재 시연에서는 장애를 명시적으로 주입한다. 카탈로그의 개인 목표는 배분만 구현했으며 서브 퀘스트의 달성·점수 판정은 엔진 연동 사항이다.

동일 seed는 초기 사건·캐릭터 조합을 재현하지만, 병렬 행동의 도착 순서가 달라지면 주사위와 진행 결과도 달라질 수 있다. 비교·재현에는 seed뿐 아니라 이벤트 순서와 실행 조건도 보존해야 한다.

## 출처와 라이선스

원작: *Murder Mystery on the Ornate Express V1.0*, Hunka Dunka (2024), CC BY-SA 4.0. 제공된 비공식 한국어 번역과 기존 데모에서 설정을 사용했다. 이 파생 모의 구현과 설정도 CC BY-SA 4.0으로 제공한다. 출처별 페이지와 변경 규칙은 `SOURCE_RULES.md`에 기록했다.
