# A/B/C가 같은 자료를 같은 의미로 주고받는지 확인하는 기준

2026-10-06 기준. A = Game-engine, B = Agent_Reasoning, C = GM_Agent_Society.
현재 확인 커밋: A `dca35b989249bf1fc7e52ca404805b73e0a52648`, B `24eeeb1e6154de3d1a9cd3d891f3479112a9d2c1`.
공통 형식 후보는 기존 C의 `gm-provisional-v0.1`을 사용한다. **C에서 준비한 기준안이며 A/B가 채택한 팀 합의로 간주하지 않는다.**

## 현재 확인된 결론

세 저장소는 동일한 데모의 카탈로그를 기준으로 한다. 직업 12개, 용의자 12개, 흉기 8개, 장소 8개의 ID와 이름이 일치한다. A/B의 행동 사전도 서로 통한다. C와는 일부 필드명을 변환하면 같은 행동 의도와 단서 의미를 유지할 수 있다.

그러나 ‘누가 어느 턴에 한 행동이며, 어떤 정보를 얻었고, 그 결과가 언제 완료되었는가’라는 공통 메시지는 아직 A/B에 없다. 같은 데모를 출발점으로 삼는다는 근거는 있지만, 세 사람이 같은 다중 플레이어 계약을 구현하고 있다는 근거는 아직 부족하다. 파일명이나 JSON 사용 여부보다 아래의 생산자·소비자·필드 의미가 연결 기준이다.

## 공통 데이터 사전과 실제 차이

| 의미 | A/B 현재 데모 | C 현재 기준안 | 처리 및 주의점 |
|---|---|---|---|
| 게임 식별자 | 게임 상태의 `id` | `game_id` | 대응 가능. NPC·단서의 `id`와 구별한다. |
| 행위자 식별자 | 별도 player ID 없음, 플레이어 한 명 | `agent_id` | A의 공식 식별자 필요. `role`이나 이름으로 대체하지 않는다. |
| 직업 | 내부 `role` = 직업 인덱스, 공개 `job` = 이름 | `job.id/name`, 별도 `stat`, 직업의 스킬 | 인덱스·이름만 대응. 스킬 정의까지 일치한다고 볼 수 없다. |
| 위치 | `room` = 장소 번호, `location` = 표시 이름 | `room` = 장소 번호 | 조인은 번호로 하고 이름은 표시용으로 쓴다. |
| 이동 목적지 | `kind=move`, `place` | `action=move`, `payload.room` | 행동 의도는 왕복 변환 가능. 이동 허가는 엔진이 별도로 검사해야 한다. |
| 준비 | `kind=prepare` | `action=prepare`, 빈 payload | 다음 판정의 준비 의도는 대응. 현재 A에는 턴 소비 제한이 없다. |
| 최종 지목 | `kind=accuse`, `suspect/weapon/place` | `action=accuse`, 같은 3개 후보 ID | 여기서 `place`는 범행 장소다. 이동의 `room`으로 바꾸면 의미가 깨진다. 오답 결과 규칙은 현재 서로 다르다. |
| 상호작용 상대 | `target` = 기존 NPC 후보 번호; 공개 상태에서는 이름 | `target_id` = 객체 ID | 기존 NPC만 `10 ↔ npc:10` 대응. 다른 플레이어·장애 NPC·시체까지 후보 번호로 환산하지 않는다. |
| 탐문·탐색 | `check`로 pending 생성, `roll`로 판정 | `question/explore/examine_corpse` 한 행동의 완료 결과 | 임의의 단순 이름 변경 금지. pending과 완료의 관계를 합의한다. `talk`는 정보 획득을 보장하지 않는다. |
| 복합 이동 | `find`가 이동하고 조건에 따라 질문도 시작 | 이동 턴과 상호작용 턴 분리 | 같은 상호작용 한 번으로 변환 불가. 추가 이동을 허용해서는 안 된다. |
| 단서의 뜻 | `category`, `index` = 제외되는 후보 | `category`, `excluded_id` | 동일 의미. ‘범인으로 지목’하는 양성 증거가 아니라 해당 후보를 제외하는 정보다. |
| 단서 정체성 | `suspect:0`, `weapon:0`, `place:0` | 같은 정보 ID | `suspect:0` 단서와 `npc:0` 객체는 다르다. 동일 정보는 같은 ID를 유지한다. |
| 단서 출처·시점 | `source` = 이름, `turn` = 경과 비용 | Information은 핵심 4개 필드 | 변환 시 별도 `legacy_metadata`에 보존. 비용을 사이클/턴 번호로 추정하지 않는다. |
| 게임 진행 시점 | `turn` = 이동·준비·판정·추가 대가의 비용 누적 | `cycle_id`, `phase`, `phase_epoch` | 동일 개념 아님. `turn / 3` 등으로 변환하지 않는다. |
| 처리 추적 | action/task/event ID 없음 | `action_id`, `task_id`, `event_id/sequence` | A의 결과에 요청 ID를 유지해야 한다. C 관찰용 별칭은 공식 ID가 아니다. |
| 성향 | NPC별 자연어 설명 | 임시 성향 객체·공개량 정책 | 설명을 warm/guarded 같은 정책 값으로 자동 분류하지 않는다. 엔진 정의 필요. |
| 개인 목표 | 자유 문자열 `goal` | 구조화된 선택 목표, `required=false` | 텍스트만으로 목표 ID·달성·점수를 추정하지 않는다. |
| 정보 소유·공개 | 단일 플레이어의 `clues` | 플레이어별 소유 원장과 명시적 공유 분리 | GM 감독 수신과 다른 플레이어 공개는 서로 다른 전달 범위다. |
| 장애·사망·종료 | `wrong/ended/won`, 두 번 오답 후 전체 종료 | `active/npc/corpse`, 마지막 제외 원인별 종료 | `wrong=1`을 사망으로, `ended=true`를 장애로 변환할 수 없다. 사용자 확정 규칙으로 A/B도 맞춰야 한다. |

카탈로그의 번호가 같아도 플레이어와 용의자의 관계에는 차이가 있다. 현재 데모는 `role` 위치의 용의자를 플레이어 이름으로 치환하고 그 NPC를 목록에서 뺀다. C의 모의 구현은 플레이어 객체와 기존 용의자 NPC를 분리한다. 앞으로 플레이어의 직업을 무작위 배분할 때 **직업 ID = 플레이어 ID = 용의자 ID**라는 가정을 이어가면 안 된다. 이는 카탈로그 일치 검사와 별도로 맞춰야 하는 게임 모델 항목이다.

## 세 사람이 공유할 최소 메시지

상세 필드와 검토용 JSON Schema는 [IO_CONTRACT.md](IO_CONTRACT.md), [io_schema.json](io_schema.json)에 있다. 새 형식을 추가로 경쟁시키지 않고 이 기준안을 A/B의 메시지와 대조한다. 전송 방식이 함수 호출인지 HTTP인지는 별도 선택이며, 데이터 의미는 동일해야 한다.

| 메시지 | 생산 → 소비 | 같아야 하는 핵심 의미 |
|---|---|---|
| 카탈로그·배분 | A → C → A 및 해당 B | A가 정의한 job/stat/skills/personality/선택 목표를 C가 조합해 공식 캐릭터에 등록 |
| `PhaseNotice` | C → 해당 B | 동일 game/agent/cycle/phase/epoch, 허용 행동, 남은 횟수, 위치와 대상; 이동 전 전체 위치·이동 후 같은 공간의 대상 |
| `ActionRequest` | B → A | 행동 소유자와 요청 ID, 현재 턴, 대상과 의도. C는 행동별 중계자가 아니다. |
| `ActionResult` | A → 요청한 B | 같은 요청 ID, 처리 완료·성공/실패·소비 여부, 획득 정보와 본인 보유 정보 ID |
| `EngineEvent(action_completed)` | A → C | 위 ActionResult와 같은 내용, 감독용 전달 범위, 재수신용 event ID/sequence |
| `GMSnapshot`·공유 이벤트 | A → C; 명시적으로 공유된 부분만 다른 B에게 | 캐릭터별 소유 목록, 상태/위치/소비 기록. 감독 수신이 자동 공개를 뜻하지 않음 |

예를 들어 같은 질문을 다음처럼 추적할 수 있어야 한다.

```json
{
  "game_id": "g", "agent_id": "player_1", "action_id": "ask-001",
  "cycle_id": 1, "phase": "interaction", "phase_epoch": 2,
  "task_id": "g:2:player_1", "action": "question",
  "payload": {"target_id": "npc:10", "approach": "compassion"}
}
```

A가 돌려준 `acquired_information`에 `{"id":"weapon:0","category":"weapon","excluded_id":0,"text":"…"}`가 있으면, B의 기억과 C의 소유 원장은 **player_1이 weapon:0을 획득했다**는 같은 사실을 기록한다. A가 보낸 GM 이벤트의 `payload.result`는 B가 받은 결과와 같아야 한다. 다른 B의 관찰에는 명시적 공유가 일어나기 전 이 단서를 넣지 않는다. 이 예시는 기준안이며 현재 A의 HTTP 응답이 이 형식이라는 뜻은 아니다.

## 이번에 실제 확인한 메시지 이동

1. 실제 B의 오프라인 파서가 이동·준비·지목 명령을 만든다.
2. C의 ActionRequest 형식으로 변환하고 JSON 직렬화 후 C의 실제 디코더로 읽는다.
3. 원래 명령으로 돌려 실제 A의 validator에 넣었을 때 3개 명령 본문이 모두 같다. 지목 실행 결과의 동등성은 주장하지 않는다.
4. 별도 실제 B→A 실행에서 이동→준비→check→roll을 수행한다. 고정 6 주사위를 주입해 획득 단서 2개를 받는다.
5. C의 관찰 저장기가 단서 ID·종류·제외 후보·텍스트를 현재 Information 형식으로 저장한다. 원본의 출처와 비용도 별도로 보존한 왕복 변환이 일치한다.

행위자와 phase/epoch/action ID는 이 검사의 호출자가 명시적으로 제공한 시험 문맥이다. A가 제공하거나 검증한 값으로 표현하지 않는다. `check` 단계에는 pending만 있고 새 단서는 없으며, 실제 단서는 `roll` 이후 전달된다. 이 연결은 함수 수준의 명령/정보 의미 검사이며 B의 자율 판단이나 A의 다중 플레이어 프로토콜 채택을 증명하지 않는다.

실행 근거: [shared_language_20261006.json](examples/shared_language_20261006.json).
공통 형식 예시: [common_message_fixture.json](examples/common_message_fixture.json) — C 모의 실행에서 추출한 검토용 메시지 묶음.

검증 기록: [shared_language_test_verification.json](examples/shared_language_test_verification.json) — 의미 검사 9개, 실제 팀 어댑터 검사 6개, 모의 GM 검사 22개, 총 37개 통과. [shared_language_format_verification.json](examples/shared_language_format_verification.json)은 예시 9개가 기존 JSON Schema 형식에 맞음을 확인한 별도 기록이다. 이 개수는 A/B의 계약 채택이나 전체 연결 성공을 뜻하지 않는다.

## 다음 연결의 완료 기준

세 사람이 같은 계약을 구현한다는 근거는 아래 항목을 각자의 실제 구현으로 통과시키는 것이다. 현재 A/B 개발이 진행 중이어도 데이터 형식과 의미는 먼저 공유할 수 있다.

- A는 동일 fixture의 요청을 받아 대상·phase·소비 횟수를 검사하고, B 결과와 GM 이벤트에 같은 요청 ID와 같은 획득 정보를 돌려준다. 동일 action ID의 재전송은 같은 결과이며 추가 소비가 없다.
- B는 같은 PhaseNotice를 읽고 허용된 행동과 같은 객체 ID를 사용한다. 완료 결과를 받으면 정보 ID와 제외 의미를 유지해서 기억한다. 명령 파싱만으로 이 수신 동작을 검증했다고 보지 않는다.
- C는 같은 완료 결과로 소유 원장을 갱신하고 전원 완료 후 턴을 넘긴다. 다른 B에게는 명시적으로 공유한 정보만 보낸다.
- 탐문 실패는 ‘완료된 실패’이며 기술 장애와 구별한다. 장애 NPC·게임 사망 시체·GM 대리·0명 종료의 상태 값과 의미도 같은 사전으로 사용한다.
- 세 저장소의 실제 수신/실행 결과와 커밋을 함께 남긴다. fixture의 형식 검사만 통과한 상태, C 모의 실행, A/B 실제 채택을 각각 구별한다.

추가 자료를 받기 전에도 진행 가능한 작업은 공통 사전·예시·변환기를 준비하는 것이다. 현재 준비를 완료했다. 다음 업데이트에서 우선 볼 것은 메서드 이름이 아니라 **A/B가 이 메시지를 읽고 같은 의미의 결과를 내보내는지**다. 최신 main이 개발 중이라는 사용자 설명을 유지하고, 새 업데이트를 알려주면 같은 검사로 비교한다.

```powershell
python -m gm_coordinator.language_probe
python -m unittest gm_coordinator.test_message_language -v
```

검사기는 로컬에 받은 A/B 저장소를 읽는다. 최신 업데이트를 로컬에 반영한 뒤 재실행한다. 카탈로그 번호/이름이 바뀌면 불일치를 보고하고 종료 코드 1을 반환한다. 의미를 보존할 수 없는 행동·정보나 알 수 없는 필드는 자동 추정 대신 오류로 드러낸다.
