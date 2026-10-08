# GM 연결 계층 변경 — 2026-10-08

GM의 배분·3단계 진행·소유 원장·장애/대리 조정은 유지하고, B의 새 `sync → decide → ingest_result` 인터페이스에 대응하는 C 어댑터를 추가했다. A/B 저장소는 수정하지 않았다. 자발적 공유는 최초 연결의 필수 조건에서 제외했다.

## 변경 사항

| 변경 | 동작 | 담당 경계 |
|---|---|---|
| `ReasoningPlayerAdapter` | 실행 가능한 B 인스턴스를 받아 PlayerPort의 choose_action/receive_result로 연결 | B 내부 import·신념·판단을 수정하지 않음 |
| 개인 입력 투영 | 본인 관찰·허용 공유·존재/위치만 B에 전달. 후보 키는 복수형으로, 단서는 kind/excludes 별칭을 추가 | 후보·단서 ID는 공식 관찰 그대로 유지. B fake 카탈로그를 대응표로 사용하지 않음 |
| 행동 출력 변환 | B question의 target을 target_id로 변환하고 요청 상관 ID 부여 | 행동을 대신 선택하거나 잘못된 move를 stay로 교체하지 않음 |
| 재호출·결과 수신 | 같은 미확정 task는 같은 action_id·요청을 반환. 완료·소비·상관 ID를 확인하고 동일 결과 중복 수신은 한 번만 전달 | 실제 제출·동일 ID 재전송·공식 소비는 B 실행기와 A의 책임 |
| 질문 응답 접점 | answer_question은 B의 공개 후보/거절을 상관 ID와 함께 반환. 능동 행동 요청을 만들지 않음 | A가 위치·소유·판정·전달을 확정. 현재 A의 라우팅은 미제공 |
| 선택 공유 분리 | share_information은 InformationSharingPort에 분리. 모의 demo 기본 공유 off, --share로 활성화 | 감독용 획득 결과 수신은 계속 필수. B의 disclose는 제안으로만 보관하고 자동 전송하지 않음 |
| GM 이벤트 누락 처리 | 이벤트 사이 또는 snapshot까지의 누락이 있으면 cursor·원장 갱신을 롤백하고 재조회를 요구 | 순번 누락을 플레이어 장애로 간주하지 않음. A의 재조회 접점 필요 |
| 기존 점검 도구 | 제거된 Zen.intent를 호출하는 대신 blocked_by_B_API_change로 명시 | 차단 상태를 실제 연결 성공으로 취급하지 않음 |

## 사용 접점

`b_agent`는 B 담당자가 제공하는 실행 가능한 객체다. C 어댑터는 B 소스를 import 방식 변경이나 메서드 교체로 고쳐서 로딩하지 않는다.

```python
from gm_coordinator.player_adapter import ReasoningPlayerAdapter

player = ReasoningPlayerAdapter(b_agent)
notice = gm.phase_notices()[b_agent.agent_id]
observation = engine.agent_snapshot(gm.game_id, b_agent.agent_id)
request = player.choose_action(notice, observation)

# B 실행기/연결 드라이버가 A에 직접 제출한다.
result = engine.submit_action(request)
player.receive_result(result)
gm.tick()  # A의 완료 기록을 감독 수신한 뒤 전원 소비 시 전환 요청
```

호출 중 결과를 받지 못하면 같은 task로 choose_action을 다시 호출해 같은 요청을 얻는다. 네트워크 오류를 이유로 새 ID의 행동을 만들지 않는다. 요청·수신 캐시는 현재 인스턴스 메모리에 있으며 프로세스 재시작 뒤 동일 ID 복원은 아직 제공하지 않는다. 실제 제출자의 지속 저장·결과 조회 명세가 필요하다.

정상 상대 응답은 A의 향후 질문 라우터가 `answer_question({game_id, question_id, asker_id, target_id, topic})`를 호출하는 접점이다. 이는 엔진 판정 전 공개 의사다. NPC/시체의 응답은 A의 수동 정책으로 처리하며 이 접점을 사용하지 않는다. 현재 동기 메서드 연결이며 비동기 수신기·주소 관리·인증·자동 장애 감시는 구현되지 않았다.

입력은 C의 임시 관찰 형식에서 시작한다. `suspect/weapon/place` 후보 풀을 B의 `suspects/weapons/places`로 투영한다. 단서 `id/category/excluded_id/text`는 보존하고 B schema용 `kind/excludes` 별칭을 추가한다. `place` 카테고리의 별칭만 `location`이다. 카탈로그 ID 의미를 임의로 바꾸지 않으며, 공식 A 접점이 다른 형식을 제공한다면 합의 후 변환을 수정해야 한다.

## 검증 결과

워크스페이스 루트에서 다음 명령으로 **45개 테스트가 통과**했다.

[검증 기록](examples/gm_adapter_verification_20261008.json)과 [최신 팀 코드 점검](examples/team_update_current.json)에 범위를 기록했다. 기본 CLI 공유 이벤트는 0건, `--share`를 켠 별도 모의 실행에서는 공유 이벤트가 발생하는 것도 확인했다.

```powershell
python -B -m unittest gm_coordinator.test_coordinator gm_coordinator.test_message_language gm_coordinator.test_team_adapters gm_coordinator.test_player_adapter -v
```

검증에는 기존 모의 게임 규칙, 개인정보 입력 제한, 요청 재호출·완료 중복/오접수 차단, 선택 공유 미사용, 이벤트 누락 롤백 및 기존 A HTTP 계약 차이 확인이 포함된다. 기존 B 인터프리터가 없다는 것을 올바르게 보고하는 테스트도 포함되므로 숫자를 전체 연결의 성공 증거로 해석하면 안 된다.

B 실제 LLMAgent 메서드는 **진단용 import 바인딩·고정 LLM 대역·명시적 belief_mode=False·C MockEngine** 조건에서 stay→explore→pass 한 사이클을 수행했다. B의 메서드 본문은 그대로다. 기본 belief 모드의 없는 apply 호출은 그대로 오류를 내며 어댑터가 이를 명시적으로 보고하는 것도 확인했다. 이는 B 패키지 원래 실행, 실제 LLM 판단이나 A/B/C 전체 연결의 검증이 아니다.

## 팀원에게 협의할 남은 조건

| 담당 | 필요한 제공 조건 | 이유 |
|---|---|---|
| B | 정상 패키지 import, 실행 가능한 LLMAgent 생성 방식, sync/decide/ingest_result API | 현재 runner/import 오류는 B 내부 수정 사항 |
| B | BeliefState 생성자·apply_clue 갱신 정렬, 이동 실패의 유효한 대체 행동 | C는 신념을 수선하거나 행동을 대신 고르지 않음 |
| B | 실제 엔진 제출/조회·지속 요청 ID, 판단 중 질문 수신과 공식 상태 전환 | 현재 C 어댑터는 호출 경계와 메모리 캐시만 제공 |
| A | 같은 사건의 n명 상태, 공식 3단계·원자적 소비·요청 결과·GM 이벤트·관찰·제어 | 현 A는 기존 단일 플레이어 데모 |
| A/B/C | 공통 카탈로그 의미와 요청/응답 형식 | B fake 카탈로그와 A의 숫자 ID를 그대로 연결할 수 없음 |

자발적 공유는 선택 기능이며 위 초기 연결의 선결 조건이 아니다. 이후 사용한다면 B의 공개 의도와 A가 확정한 공유 완료를 구분해야 한다. B 현재 코드의 공개 의사 직후 disclosed 갱신은 C가 수정하지 않았다.

10월 6일 제안서와 10월 8일 최초 검토 기록은 당시 상태를 보존한다. 최신 어댑터의 상태는 이 문서가 기준이다. 실제 전체 연결을 위해 위 조건을 제공하는 방식이 가능한지, 다른 접점이 선호된다면 어떤 규격에 맞추면 좋을지 담당자와 협의하면 된다.
