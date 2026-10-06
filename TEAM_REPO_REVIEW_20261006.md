# 팀 저장소 확인과 GM 연결 준비

2026-10-06에 두 저장소를 `gm_coordinator`와 같은 상위 폴더의 별도 디렉터리로 clone했다. 검토 대상은 아래 main 커밋이며, 팀원의 아직 push되지 않은 개발 내용은 포함하지 않는다. 두 clone의 tracked 파일은 변경하지 않았다.

| 구분 | 원격 | 로컬 폴더 | 확인한 main 커밋 |
|---|---|---|---|
| A 엔진 | https://github.com/Gachon-Murder-Mystery/Game-engine | `../Game-engine` | `dca35b989249bf1fc7e52ca404805b73e0a52648` |
| B 플레이어 | https://github.com/Gachon-Murder-Mystery/Agent_Reasoning | `../Agent_Reasoning` | `24eeeb1e6154de3d1a9cd3d891f3479112a9d2c1` |

## 확인 결과

`engine.py`, `app.py`, `llm.py`, `test_game.py`, `README.md`, `index.html`의 내용은 두 저장소가 서로 동일하며 기존 `ornate_gm_demo`와도 줄바꿈을 정규화하면 동일하다. 이 비교는 텍스트 동일성 확인이며 새 다중 에이전트 기능이나 통합 실행의 증거가 아니다.

현재 원격 버전은 모두 1인용 대화형 TRPG 데모 초기 코드다. 저장소 이름은 담당 영역을 나타내지만, 해당 영역으로 분리된 구현은 아직 원격 main에서 확인되지 않는다. GM의 임시 구현은 이 데모를 기반으로 만들었으므로 카탈로그의 이름·장소·후보는 일치한다.

## A 엔진에서 현재 사용할 수 있는 부분

- `new_game`, `apply`, `public`, `llm_context`, `validate_intent`: 사건 생성·기존 행동·주사위·공개 상태 투영.
- `/catalog`, `/start`, `/state`, `/chat`, `/roll`, `/approach`, `/accuse`, `/export`: 기존 로컬 HTTP 요청.
- 기존 후보·직업·장소 ID, 제외 단서와 출처, 준비·전문성 판정.

[공식 상태 생성 코드](https://github.com/Gachon-Murder-Mystery/Game-engine/blob/dca35b989249bf1fc7e52ca404805b73e0a52648/engine.py#L27)는 사건 하나에 플레이어 상태 하나를 저장한다. `room`, `pending`, `prepared`, `clues`, `goal` 등이 에이전트별로 분리되어 있지 않다. 각 플레이어마다 `new_game()`을 따로 호출하면 서로 다른 사건이 생성되므로 다중 플레이어 연동의 대안이 아니다.

[서버 처리](https://github.com/Gachon-Murder-Mystery/Game-engine/blob/dca35b989249bf1fc7e52ca404805b73e0a52648/app.py#L51)는 사건 잠금 안에서 LLM 해석과 서술을 기다린다. ThreadingHTTPServer를 사용하더라도 같은 사건의 LLM 처리 동안 다른 행동이 기다리게 된다.

## B 플레이어에서 현재 사용할 수 있는 부분

[`Zen.intent(g, text)`](https://github.com/Gachon-Murder-Mystery/Agent_Reasoning/blob/24eeeb1e6154de3d1a9cd3d891f3479112a9d2c1/llm.py#L78)는 사람이 제공한 자연어를 행동으로 해석한다. `Zen.narrate`는 엔진 결과를 장면으로 서술한다. 현재 원격에는 관찰·신념·목표를 바탕으로 스스로 다음 행동을 선택하는 독립 플레이어 루프가 없다.

LLM 요청·오류 처리 코드는 B의 구현 재료로 사용할 수 있지만, 그 자체를 `PlayerPort.choose_action` 구현으로 간주할 수 없다. 플레이어 판단이 엔진의 원본 정답이나 다른 플레이어의 개인 정보에 접근하지 않도록 관찰 입력부터 분리해야 한다.

## GM 임시 계약과 실제 코드의 차이

| 항목 | 현재 팀 저장소 | 연결에 필요한 처리 | 책임 |
|---|---|---|---|
| 한 사건·n개 플레이어 | 한 사건·한 플레이어 상태 | 같은 공식 사건 안에 에이전트별 위치·정보·캐릭터 상태 분리 | A |
| 직업·성향·목표 카탈로그 | 직업 문자열과 NPC용 성격 설명 | GM 배분에 사용할 구조화된 stat/skill/personality/goal 정의 | A 제공, C 배분 |
| 3단계 사이클 | 자유 행동과 비용용 turn 카운터 | cycle_id/phase/phase_epoch, 턴별 소비 기록, 현재 턴 행동 제한 | C 진행, A 검증 |
| 행동 요청·결과 | 자연어 chat, kind 기반 intent, 문자열 결과 | agent/action/task ID와 구조화된 완료 결과 | B 요청, A 실행 |
| check와 roll | 판정 요청과 굴리기가 별도 HTTP 요청 | 한 조사 행동의 준비·판정·완료를 묶고 외부 요청 재전송 시 중복 소비 방지 | A |
| 정보 소유 | 사건 전체 단서 목록 하나 | 플레이어별 보유·공유 정보와 감독용 결과 구분 | A |
| GM 완료 이벤트 | 없음 | 원자적으로 저장되는 이벤트와 sequence/cursor 기반 재조회 | A 발행, C 수신 |
| 플레이어 판단 | 사람 입력 해석 | GM 턴 안내·개인 관찰을 받고 행동을 선택하는 독립 loop | B |
| 플레이어 간 탐문 | 원본 NPC와의 대화 | 같은 장소의 다른 플레이어·장애 NPC와 상호작용 | A 규칙, B 선택 |
| 장애 NPC·시체 | NPC 위치 상수, 별도 mode 없음 | active/npc/corpse 전환, 정보 보존·주사위 공개·감쇠 | C 장애 조정, A 공식 객체 |
| 추리 실패 | 첫 오답 재시도, 두 번째 오답 사건 종료 | 오답 플레이어 사망, 잔여 능동 플레이어 0명일 때 GM 종료 | A 사망, C 종료 요청 |
| GM 대리 전환 | 없음 | 캐릭터의 NPC 전환과 controller/term 교체, 진행 기록 복원 | C 선정, A 원자적 기록 |
| 개인 목표 점수 | 역할극용 문자열 | 선택적 서브 퀘스트 달성·추가 점수 판정 | A |

`move.room` → 기존 `kind=move/place`, `explore` → `kind=check/target=None` 등 이름 일부는 매핑할 수 있다. 그러나 공식 다중 플레이어 상태·턴 소비·정보 권한·종료 규칙은 이름 매핑만으로 구현되지 않는다. 기존 `find`는 이동과 심문을 묶을 수 있으므로 탐문·탐색 턴에 그대로 허용하면 이동 금지 규칙을 위반한다.

## 기존 테스트 실행과 저장 연결 진단

Windows, Python 3.14 환경에서 두 저장소의 기존 테스트를 각각 실행했다. LLM 요청 테스트는 기존 mock을 사용했으며 실제 API 호출은 하지 않았다.

| 저장소 | 실행 | 통과 | 오류 |
|---|---:|---:|---:|
| Game-engine | 14 | 13 | 1 |
| Agent_Reasoning | 14 | 13 | 1 |

양쪽 모두 `test_persistence_roundtrip`의 TemporaryDirectory 정리 과정에서 `state.sqlite` 삭제가 WinError 32로 실패했다. 별도 저장·복원 진단에서는 읽어온 상태가 저장한 상태와 일치했다. 전체 테스트 통과로 보고하지 않는다.

[`Store`](https://github.com/Gachon-Murder-Mystery/Game-engine/blob/dca35b989249bf1fc7e52ca404805b73e0a52648/app.py#L16)는 `with sqlite3.connect(...)`로 트랜잭션을 처리하지만 명시적으로 연결을 닫지 않는다. 진단에서 연결 참조를 보존하자 생성·저장·읽기의 연결 3개가 context 종료 뒤에도 사용할 수 있었다. 이는 명시적 close가 없는 근거이며 장기적인 메모리 누수나 OOM을 입증한 것은 아니다. A/B에서 `contextlib.closing` 또는 `try/finally: close()`로 연결 수명을 명시하는 수정이 필요하다. 이번 검토에서는 팀원 파일을 수정하지 않았다.

## 다음 연결 순서

1. A/B에 `contracts.py`와 `IO_CONTRACT.md`의 임시 형식을 기준으로 실제 제공 형식을 대조한다. 이 문서가 팀원이 해당 계약을 이미 채택했다는 의미는 아니다.
2. A가 같은 사건의 다중 플레이어 관찰·행동·완료 이벤트를 제공하면 `EnginePort` 어댑터를 연결한다. C가 엔진 판정이나 공식 상태를 대신 구현하는 우회는 하지 않는다.
3. B가 턴 안내·관찰 입력에서 구조화된 행동을 선택하면 `PlayerPort`를 연결한다. C가 B의 판단 알고리즘을 대신 작성하지 않는다.
4. 정상 3단계 진행 → 플레이어 장애 → 추리 오답 사망 → GM 대리 전환 → 0명 종료 순으로 실제 연결 동작을 확인한다.

현재 결론: 저장소 확보와 코드·테스트 확인은 완료했다. 실제 A/B/C 연동은 아직 수행하지 않았으며, GM은 기존 모의 어댑터를 유지한다. 상세 비교·진단 결과는 `examples/team_repo_review.json`, 기존 테스트 로그는 워크스페이스 `.repo_review_work/engine_tests.txt`와 `agent_tests.txt`에 있다.
