# GM 임시 입출력 계약

버전: `gm-provisional-v0.1`. JSON 형식은 `io_schema.json`, Python 호출 형식은 `contracts.py`에 있다. A/B의 실제 구현을 연결할 때 모의 구현을 포트 어댑터로 교체한다. 이번 버전의 transport는 프로세스 내 메서드 호출이며 HTTP 경로나 메시지 브로커는 아직 정하지 않았다.

## 호출 경로와 책임

```text
GM -- 캐릭터 조합 등록·턴 전환·NPC 전환·종료 요청 --> 엔진
GM -- 턴 시작·상황 안내 --> 플레이어
플레이어 -- 행동 요청 --> 엔진 -- 개인별 결과 --> 플레이어
엔진 -- 완료·정보·상태 이벤트 --> GM
플레이어 -- 자발적 공유 --> 엔진 공유 기록 --> GM 및 허용된 플레이어
```

GM이 매 행동을 중계하거나 승인하지 않는다. GM은 전체 턴을 열고 닫으며 엔진은 현재 턴과 행동 소비 기록을 검사한다. 엔진이 행동 실행·차감·결과 영속 저장·완료 이벤트 저장을 같은 트랜잭션으로 처리한다.

## GM 입력

| 입력 | 최소 내용 | 임시 구현 |
|---|---|---|
| 엔진 카탈로그 | 직업·스테이터스·스킬, 성향, 개인 목표, 장소·후보 목록, 규칙 | `catalog()` |
| 감독 상태 | game/cycle/phase/epoch, 제어자·term, 능동 명단, 위치, 소비 기록, 플레이어 유래 캐릭터별 정보 ID, 관측된 정보, 공유 정보, 종료 상태 | `gm_snapshot(game_id)` |
| 완료·상태 이벤트 | event_id, game_id, sequence, cycle_id, phase, phase_epoch, type, payload | `events_after(game_id, sequence)` |
| 플레이어 장애 신호 | agent_id, 명시적인 장애 사유 | 런타임이 `report_player_fault` 호출 |
| GM 장애 신호 | 기존 제어자·term과 현재 능동 명단 | 생존 런타임이 `replace_failed_gm` 호출 |

획득 결과 이벤트는 다른 플레이어에게 보류된 정보도 GM에 제공한다. 이 이벤트는 감독용이며 전체 플레이어에게 방송하지 않는다. 원본 NPC의 미발견 보유 정보 디렉터리와 숨겨진 정답은 감독 상태에 포함하지 않는다. 새롭게 획득한 정보에는 고정 ID, 제외되는 후보의 종류·ID, 설명이 붙는다.

`gm_snapshot.event_sequence`는 상태와 대응하는 이벤트의 마지막 순번이다. GM은 해당 순번까지 처리하고, 이후 이벤트는 다음 동기화에서 처리한다. 이벤트 저장은 엔진 담당이고, 수신 cursor·완료 원장은 GM 담당이다. 실제 연동에서 이벤트 누락을 발견하면 해당 cursor 뒤를 재조회할 수 있어야 한다.

## GM 출력

| 대상 | 출력 | 필수 효과 |
|---|---|---|
| 엔진 | 시작 배분 `assignments[agent_id]` | job/stat/personality/personal_goal을 공식 상태로 등록 |
| 플레이어 | 턴 안내 `phase_notices()[agent_id]` | 현재 턴·토큰·task_id·허용 행동·잔여 횟수·자기 캐릭터·같은 공간 대상·공유 정보 전달 |
| 플레이어: 이동 시작 | `positions` | 모든 캐릭터의 id/name/mode/room/job 이름만 제공 |
| 엔진 | `advance_phase(game_id, control, phase_epoch)` | 능동 참여자의 행동 소비를 재검사하고 다음 턴으로 전환 |
| 엔진 | `convert_fault(game_id, agent_id, reason, control)` | 기존 정보·캐릭터를 보존하고 능동 플레이어를 NPC로 전환 |
| 엔진 | `take_over(game_id, successor_id, previous_control)` | 제어권 교체와 선택 캐릭터의 NPC 전환을 원자적으로 수행 |
| 엔진 | `finish(game_id, reason, control)` | GM이 요청한 종료를 공식 기록 |

GM 안내에 전체 `inventories`, 전체 `observed_information`, 다른 캐릭터의 개인 목표·능력치·성향을 복사하지 않는다. 본인의 상세 관찰은 엔진의 `agent_snapshot`으로 받는다. 원장 내 작업 ID는 턴·캐릭터 단위 조정 ID이며, 사건 단서 획득 성공 여부와 별개다.

## 플레이어 → 엔진 행동 요청

```json
{
  "game_id": "g", "agent_id": "player_1", "action_id": "unique-action-id",
  "cycle_id": 1, "phase": "interaction", "phase_epoch": 2,
  "task_id": "g:2:player_1", "action": "question",
  "payload": {"target_id": "npc:10", "approach": "compassion"}
}
```

| 턴 | 행동 | payload |
|---|---|---|
| movement | move / stay | move: `room`; stay: 빈 객체 |
| interaction | explore / question / examine_corpse / prepare / pass | 탐색: 선택적 approach; 질문·시체 조사: target_id와 선택적 approach |
| deduction | accuse / pass | 지목: suspect·weapon·place ID 모두 필요 |

`phase_epoch`는 턴이 넘어갈 때마다 증가한다. 이전 턴의 새로운 요청은 거절한다. 이미 완료된 동일 `action_id`와 동일 요청을 재전송하면 원래 결과를 반환한다. 같은 ID에 다른 요청을 넣으면 거절한다. 새 ID라도 해당 캐릭터가 이미 행동을 소비했으면 실행하지 않는다. 잘못된 위치·대상·지목 형식은 소비·상태 변경·완료 이벤트 없이 거절한다. 현재 로컬 구현은 `ContractError`를 사용하며 transport의 오류 응답 형식은 실제 어댑터에서 맞춘다.

결과에는 `execution_status=completed`, `outcome=succeeded/failed/skipped`, `consumed=true`, 주사위 결과, 획득 정보, 본인의 전체 보유 정보 ID가 들어간다. 탐색 실패도 완료된 행동이며 기술적 장애가 아니다. GM용 `action_completed` 이벤트에는 같은 결과가 포함된다.

`share_information(game_id, agent_id, ids, message_id)`는 능동 소유자만 사용한다. 현재 임시 정책에서는 공유 메시지가 게임 행동을 추가 소비하지 않는다. 전송 횟수를 행동 예산으로 제한할지는 추후 엔진 규칙으로 교체할 수 있다. 공유를 해도 엔진의 턴별 게임 행동 소비 제한은 바뀌지 않는다.

## 장애·사망·복원

- 기술적 장애는 런타임의 명시적 실패 신호로만 주입한다. 주사위 실패나 추리 생략을 장애로 해석하지 않는다. heartbeat·timeout의 값은 현재 자동 판정하지 않는다.
- NPC 전환 전 이미 커밋된 행동과 정보는 보존한다. 늦게 온 동일 요청의 재전송은 기존 결과를 반환할 뿐, NPC가 새 행동을 수행하지 않는다.
- 추리 오답은 엔진이 `actor_died`를 발생시키며 캐릭터를 시체로 바꾼다. 살아 있는 플레이어에게 개인 상태를 자동 이전하지 않는다.
- NPC는 능동 참여자 명단에서 제외한다. NPC 무작위 이동과 시체 감쇠는 엔진이 새 사이클 시작 시 수행한다.
- 대리 GM은 이전 GM의 공유 원장·감독 기록을 이어받는다. 자신의 과거 개인 정보가 전체 플레이어에게 자동 공유되지 않는다. controller_id와 term이 맞지 않는 GM의 조작은 거절한다.
- 플레이어가 0명이 된 마지막 원인이 기술적 장애·GM 대리 전환이면 `technical_failure`, 추리 오답 사망이면 `deduction_failure`다. 혼합 이력에서도 마지막 제외 원인으로 구분하고 이전 이력은 이벤트에 남긴다.
- 저장소를 다시 열면 현재 턴·소비 기록·주사위 RNG 상태·엔진 결과·GM cursor를 복원한다. 이벤트 재조회로 같은 원장 항목을 중복 추가하지 않는다.

## 첫 A/B 연결 시 제공할 요소

A는 카탈로그·개인 관찰·감독 상태·행동 실행·순서 있는 이벤트 재조회·GM 제어 메서드를 제공한다. B는 캐릭터 배분과 턴 안내를 받아 현재 허용 행동을 선택하고 직접 엔진에 제출한다. C는 기존 GM 모듈을 유지하고 A/B 포트의 어댑터만 연결한다.

인증, 네트워크 전달, 자동 프로세스 감시, 개인 목표 점수 판정은 이 로컬 모의 계약에서 구현되지 않았다. 다음 연결에서 실제 구현의 책임 범위와 맞춰야 한다.
