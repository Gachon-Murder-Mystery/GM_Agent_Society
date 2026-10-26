# 임시 GM 조정 구현 v0.1

2026-10-06 대화에서 확정한 GM 역할을 실행 가능한 형태로 만든 독립 패키지다. A의 엔진과 B의 플레이어 코드가 개발 중이므로, 기존 `ornate_gm_demo/engine.py`와 제공된 영문·한국어 룰북에서 구성한 임시 엔진·스크립트 플레이어로 연결했다.

GM은 캐릭터 조합 배분, 이동→탐문·탐색→추리 턴 진행, 상황 안내, 정보 소유 원장, 장애 처리와 대리 GM 전환을 담당한다. 플레이어 행동은 GM을 경유하지 않고 엔진에 직접 요청한다. 공식 상태·주사위·권한·중복 행동 방지는 엔진이 처리한다.

사용자의 직접 개발 범위는 GM 및 GM↔agent의 안내·수신·조정이다. 엔진과 agent 내부 구현은 각 담당자가 제공하며 GM 측 어댑터로 연결한다. `ROLE_TASKS_20261006.md`는 역할별 선결 조건과 이 개발 경계를 기록한다. 에이전트 간 상호작용은 해당 문서의 기존 권고 흐름을 작업 전제로 사용한다.

2026-10-08 최신 확인: A는 변경이 없고 B의 새 커밋 3개를 로컬에 반영했다. B의 `sync/decide/ingest_result`에 대응하는 [GM 어댑터 변경](GM_ADAPTER_UPDATE_20261008.md)을 추가했다. 자발적 공유는 선택 기능으로 분리하고 기본 실행에서는 끈다. 기존 점검 도구는 제거된 `Zen.intent`를 명시적 차단 상태로 보고한다. B 실행 오류와 A 공식 접점 부족으로 전체 연결은 아직 불가능하다. 변경 전 상태는 [업데이트 검토](TEAM_UPDATE_REVIEW_20261008.md)와 [당시 실행 기록](examples/team_update_review_20261008.json)에 보존했다.

2026-10-19 최신 확인: A가 외부 에이전트 전용 [연결 계약 v1](../Game-engine/INTEGRATION_CONTRACT.md)(`/integration/v1/`, `b477cb0`)을 추가했다. GM은 이 API에 맞춘 [`engine_v1.py`](engine_v1.py)로 갱신했다: 엔진이 선발·승계한 GM 자격으로만 동작하고, 활성화·직업·성향·task·대안·선택 이유를 PLAN으로 제출하며, 플레이어 장애 시 재배정과 승계 후 빈 맥락 재구성을 수행한다. 실제 A 엔진 프로세스와 스크립트 플레이어로 정상·플레이어 장애·GM 장애·인간 개입 시나리오를 해결까지 확인했다. B의 최신 2개 반환 `decide()`에 맞춰 어댑터도 고쳤다. 상세와 남은 차단 사항은 [GM_ENGINE_V1_UPDATE_20261019.md](GM_ENGINE_V1_UPDATE_20261019.md). 아래 `demo.py`/`mock_engine.py`는 v1 이전의 오프라인 모의 실행으로 유지한다.

2026-10-26 최신 확인: A가 경쟁형 v1.3 엔진(`919d34d`)을 기본 모드로 올렸다. 설정값은 [`competitive_settings.json`](competitive_settings.json) 하나로 통일했고 GM은 시작할 때 엔진 카탈로그와 대조한다(`settings.verify_engine`). 경쟁형 GM은 [`competitive_gm.py`](competitive_gm.py)다: Role·서브 미션 배정, 엔진이 굴린 주사위 결과 수신, 응답 대기 확인 뒤 진행, 공지 즉시 게시, 종료·성과 보고, 승계 후 엔진 기록만으로 재구성. 협력형 `engine_v1.py`는 `mode=legacy` 회귀용으로 남긴다. 대조 결과·B 인계 계약·남은 작업은 [V13_ALIGNMENT_20261026.md](V13_ALIGNMENT_20261026.md).

3자 통합 실행: A 엔진을 별도 프로세스로 띄우고, 풀 멤버마다 `AgentWorker`(`agent_runtime.py`)가 현재 역할(플레이어 또는 GM)로 실제 HTTP에 붙어 한 판을 공식 종료까지 진행한다. 플레이어 자리는 B 런타임 슬롯이다(`--player 모듈:팩토리`). 기본값은 C 대역 `ScriptedPlayerRuntime`이고 LLM은 쓰지 않는다.

```powershell
python -B -m gm_coordinator.run_scenario --seed 7 --progress --out run.json
python -B -m unittest gm_coordinator.test_competitive_gm gm_coordinator.test_agent_runtime -v   # 실제 A 엔진(../Game-engine), LLM 없음
```

## 실행

워크스페이스 루트에서 Python 3.10 이상으로 실행한다. 실행과 테스트에는 외부 패키지나 LLM 연결이 필요 없다.

```powershell
python -m gm_coordinator.demo --scenario normal --agents 3 --out gm_coordinator/examples/normal.json
python -m gm_coordinator.demo --scenario player-fault --agents 3
python -m gm_coordinator.demo --scenario gm-fault --agents 3
python -m gm_coordinator.demo --scenario all-dead --agents 3
python -m unittest gm_coordinator.test_coordinator -v
```

`--agents`는 양의 정수이며 인원수가 고정되지 않는다. `--scenario`는 `normal`, `player-fault`, `gm-fault`, `all-fault`, `all-dead`, `death`를 지원한다. 자발적 공유는 기본적으로 꺼져 있으며 `--share`로 켠다. 기존 `--withhold`도 공유를 끈 상태로 실행한다. `--corpse-decay 0`은 임시 시체 정보 감쇠 규칙을 끈다.

실제 A 엔진(v1)과의 실행은 `Game-engine`이 같은 상위 폴더에 있어야 하며 드라이버가 임시 DB로 엔진을 띄운다.

```powershell
python -m gm_coordinator.v1_demo --scenario normal --out gm_coordinator/examples/v1_normal.json
python -m gm_coordinator.v1_demo --scenario gm-fault
python -m unittest gm_coordinator.test_engine_v1 -v
```

```powershell
python -m gm_coordinator.demo --db gm_coordinator/data/demo.sqlite3 --scenario normal --agents 3
```

같은 DB·시나리오·인원·seed로 다시 실행하면 저장된 게임을 이어받는다. 끝난 게임은 끝난 상태로 읽는다. 새 게임은 다른 seed 또는 별도 DB를 사용한다. `--max-cycles`는 실행 드라이버의 안전한 중단 한도이며 게임의 종료 규칙이 아니다. 한도에 도달하면 `driver_stopped=true`, `ended=false`로 보고한다.

## 파일

| 파일 | 역할 |
|---|---|
| `engine_v1.py` | A `/integration/v1/` HTTP 클라이언트, v1 GM 정책 `V1Coordinator`, 승계 감지 `GMWorker` |
| `v1_demo.py` / `test_engine_v1.py` | 실제 A 엔진 프로세스 + 스크립트 플레이어 시나리오 / 자동 검증 |
| `GM_ENGINE_V1_UPDATE_20261019.md` | v1 대응 내용·검증 결과·남은 차단 사항 |
| `GAME_SPEC_v1.3.md` | **확정 기획서 v1.3 (개발 기준 문서).** 공정성 감시 제외, GM 정책 기본값(C6)·대역 플레이어 기본값(D3) 문서화. `build_spec_docx.py`로 DOCX 생성 |
| `GAME_SPEC_v1.2.md` | 기획서 v1.2 (GM 담당 결정: 풀 구성·중간 참가 없음·Role 배정·인원·간접 단서 검사 주체·LLM) |
| `GAME_SPEC_v1.1.md` | 기획서 v1.1 (Role, 고정 MBTI, GM 유임, 서브 미션, 간접 단서). 부속: `SUBMISSION_INDIRECT_CLUE_v1.0.md` |
| `GM_TASK_STATUS_20261019.md` | GM 담당 작업 현황 (완료 / 선결 작업 대기) |
| `role_policy.py`, `mission_policy.py`, `gm_metrics.py` | v1.3 Role 배정, 서브 미션 선택·지정, 성과 지표(C4). 기본값은 기획서 C6. 순수 함수 |
| `competitive_player.py` | 경쟁형·무작위 플레이어 대역 (LLM 없음, 기본값은 기획서 D3). 엔진 형식은 A-9 전까지 가정 |
| `build_v12_data.py` → `v12_data.json` | 기획서 A1·설계서 2절에서 캐릭터 12명·서브 미션 58종 추출 |
| `test_v12_policy.py` | 위 모듈 단위 테스트 (엔진·LLM 없음) |
| `GM_ENGINE_REQUEST_20261019.md` | GM → 엔진 인터페이스 요청 R1~R10 (v1.2 대응) |
| `GAME_SPEC_v1.0.md` | 기획서 v1.0 (이전 판). 공통 + A·B·C 영역별 확인 사항, 선결 항목 링크. `build_spec_docx.py`로 DOCX 생성 |
| `PLAN_CURRENT_20261019.md` | 현재 구현 기반 기획안 (A/B/C 코드에서 실제 동작하는 규칙·구조) |
| `PLAN_REVISION_20261019.md` | 기획 수정안: 경쟁 게임·캐릭터·관계성·판정 규칙과 결정해야 할 사항(Q1~Q20) |
| `CHARACTER_SHEETS_20261019.md` | 캐릭터 12명 시트 초안: 스테이터스·스킬·MBTI 쓰임·판정 기준선 |
| `REMAINING_WORK_20261019.md` | 게임 완성까지 남은 A/B/C·공동 기능과 진행 순서 |
| `contracts.py` | 교체 가능한 EnginePort / PlayerPort, 행동 요청, GM 제어 식별자 |
| `player_adapter.py` | B sync/decide/ingest_result와 GM 입력/결과 형식의 연결 경계 |
| `GM_ADAPTER_UPDATE_20261008.md` | 새 어댑터·선택 공유·이벤트 누락 처리와 검증 범위 |
| `coordinator.py` | C가 구현하는 GM 조정 로직 |
| `store.py` | GM 기록과 모의 엔진 상태·이벤트의 SQLite 저장 |
| `catalog.json` | 엔진에서 받을 임시 직업·성향·목표·장소·후보 데이터 |
| `mock_engine.py` | A의 실제 엔진으로 교체할 모의 구현 |
| `mock_player.py` | B의 실제 판단 루프로 교체할 스크립트 구현 |
| `demo.py` | 병렬 실행·장애 주입·입출력 예시 생성 |
| `IO_CONTRACT.md` | 다음 연결 시 사용할 호출·메시지 계약 |
| `SOURCE_RULES.md` | 룰북·데모·사용자 결정·임시 가정 구분 |
| `TEAM_REPO_REVIEW_20261006.md` | A/B 저장소의 확인 커밋·기존 테스트·GM 연결 차이 |
| `INTEGRATION_STATUS_20261006.md` | 실제 팀 코드·HTTP 연결 시도와 전체 GM 연결의 차단 사유 |
| `team_adapters.py` / `integration_probe.py` | 실제 A/B 데모 함수 연결 및 native 서버 점검 |
| `SHARED_LANGUAGE_20261006.md` | A/B/C 필드 의미 대조와 팀 공통 메시지의 연결 기준 |
| `ROLE_TASKS_20261006.md` | 역할별 추가 작업·선결 조건·개발 범위·상호작용 전제 |
| `docs/collaboration_20261006/` | 엔진·agent 담당자별 제안서 및 GM 진행/공통 협의 문서, DOCX와 원문 |
| `build_collaboration_documents.py` | 협의 문서 원문에서 전달용 DOCX 재생성 |
| `message_language.py` / `language_probe.py` | 실제 명령·단서의 의미 보존 변환 및 재검사 |
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
- GM 대리 전환은 살아 있는 런타임/감시자가 엔진에 요청한다. 엔진이 안정적인 agent ID 순으로 후임 1명을 선정하는 것은 임시 정책이다. 후임의 기존 플레이어 자리는 먼저 `fault`로 남고, 새 GM이 NPC 전환을 요청한다. 이전 GM의 제어 식별자는 엔진에서 거절된다. 책임 경계는 [GM_ENGINE_BOUNDARY_20261012.md](GM_ENGINE_BOUNDARY_20261012.md)에 정리했다.

## 검증의 범위

테스트는 턴 제한, 동시 중복 소비, 잘못된 요청의 원자적 거절, 정보 경계, 사망·장애 구분, NPC·시체 상호작용, 감쇠, GM 대리 전환, 재시작과 이벤트 재수신, 저장 실패 시 롤백을 확인한다.

이 결과는 로컬 모의 연동의 동작 확인이다. 실제 A/B 코드, 네트워크 장애, 운영체제의 프로세스 감시, 실시간 LLM 판단은 검증하지 않았다. `replace_failed_gm`과 `report_player_fault`는 연결 지점이며, 현재 시연에서는 장애를 명시적으로 주입한다. 카탈로그의 개인 목표는 배분만 구현했으며 서브 퀘스트의 달성·점수 판정은 엔진 연동 사항이다.

동일 seed는 초기 사건·캐릭터 조합을 재현하지만, 병렬 행동의 도착 순서가 달라지면 주사위와 진행 결과도 달라질 수 있다. 비교·재현에는 seed뿐 아니라 이벤트 순서와 실행 조건도 보존해야 한다.

실제 팀 저장소를 점검하려면 워크스페이스 루트에서 `python -m gm_coordinator.integration_probe`를 실행한다. 기본적으로 같은 상위 폴더의 `Game-engine`과 `Agent_Reasoning`을 읽는다. 경로가 다르면 `--engine-repo`와 `--agent-repo`로 지정한다. 기존 데모 함수와 HTTP 서버를 실제 실행하지만, 전체 GM 계약이 없으면 연결을 명시적으로 차단한다. 이 결과는 `demo.py`의 모의 실행 결과와 별도로 저장한다. 실시간 LLM과 자동 장애 감시는 실행하지 않는다.

자료가 같은 의미로 이동하는지 확인하려면 `python -m gm_coordinator.language_probe`를 실행한다. 기존 B 인터프리터가 없으면 차단 상태를 보고한다. 새 코드 자체 점검은 `python -B -m gm_coordinator.team_update_probe`로 실행하며 `examples/team_update_current.json`을 갱신한다. 형식·의미·팀 채택의 차이는 `SHARED_LANGUAGE_20261006.md`, 최신 연결 조건은 `GM_ADAPTER_UPDATE_20261008.md`에 정리했다. 이 기준안의 A/B 채택은 아직 확인되지 않았다.

## 출처와 라이선스

원작: *Murder Mystery on the Ornate Express V1.0*, Hunka Dunka (2024), CC BY-SA 4.0. 제공된 비공식 한국어 번역과 기존 데모에서 설정을 사용했다. 이 파생 모의 구현과 설정도 CC BY-SA 4.0으로 제공한다. 출처별 페이지와 변경 규칙은 `SOURCE_RULES.md`에 기록했다.
