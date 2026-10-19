# GM의 엔진 외부 연결 계약 v1 대응

2026-10-19 기준. A `b477cb0`(외부 연결 계약 v1), B `f916097`, C 직전 `f00bc05`.

## 무엇이 바뀌었나

A가 `/integration/v1/`에 외부 에이전트 전용 API를 열었다. 영속 풀·GM 선발과 승계·NPC 전환·단계 진행 검증·질문 응답 대기는 엔진이 집행한다. GM(C)은 **활성 인원·직업·성향·목표·task·의존성·대안·선택 이유를 PLAN으로 제출하는 역할 정책**만 맡는다. 계약서 217–237행의 C 측 변경 요구를 아래처럼 반영했다.

| 기존 C 임시 접점 | v1 대응 (`engine_v1.py`) |
|---|---|
| `create_game` + `gm-primary` 고정 | 운영자 `games/create`가 선발한 GM이 자기 `pool/session`으로 받은 controller_id/term/token으로만 `V1Coordinator`를 만든다. |
| `gm_snapshot` / `events_after` | `gm/observe` + `events`. 기본 GM은 획득 **ID만** 받으므로 본문 가정을 제거했다. 단서 내용 없이 ID 차집합으로 판단한다. |
| GM이 매 단계 task_id 발급 | 엔진 task(`task_id`, `generation`)를 PLAN으로 생성·재배정한다. 플레이어는 관찰한 generation으로 직접 행동한다. |
| `advance_phase` | `gm/command ADVANCE` (phase_epoch, 멱등 command_id). 전원 소비·대기 질문 0일 때만 보낸다. |
| `take_over` / `reconcile_successor_slot` | 엔진 watchdog·운영자가 승계와 NPC 전환을 원자적으로 처리한다. C는 승계를 일으키지 않는다. `GMWorker`가 세션의 role/context_key 변화를 감지해 새 임기의 GM을 **빈 맥락**에서 시작한다. |
| `convert_fault` 후 재배분 | 엔진은 장애자의 미완료 task 소유권만 해제한다. GM이 PLAN으로 새 담당자를 정하고, 다음 이동 단계에서 예비 풀원을 활성화해 목표 인원을 복구한다. |
| `finish` | `resolution=solved`를 관찰하면 GM FINISH. 중단은 운영자 FINISH. |
| personality dict 가정 | v1의 string trait를 그대로 사용한다. |

## GM 정책 (C 소유)

- **초기 구성:** 풀 카드의 공식 `player` 역량 추정치 순으로 예비 풀원을 고르고, 직업과 성향은 겹치지 않게 배정한다. 기본 목표 인원은 플레이어 2명(GM 포함 3명)이며 `target_players`로 바꾼다.
- **매 사이클 계획:** ADVANCE로 이동 단계가 열리면 같은 tick 안에서 바로 계획한다. 이 처리가 없으면 플레이어가 먼저 이동해 GM이 새 이동 단계를 관찰하지 못하는 경쟁이 실제 엔진에서 재현되었다.
- **대안 비교:** 매 계획마다 `parallel_explore` / `exchange_questions` / `deduce_only` 세 대안의 점수와 근거를 PLAN의 `alternatives`·`reason`에 남긴다.
  - 병렬 조사: 남은 조사 공간 수와 활성 인원 중 작은 값. 직전 사이클에 신규 획득이 없으면(정체) 가중치를 낮춘다.
  - 질문 교환: 각 플레이어가 **다른 활성 플레이어에게서 얻을 수 있는 미보유 단서 ID 수**의 합. 정체 상태이거나 조사 공간이 소진되면 가중치를 높인다. 같은 상대에게 2회 거절당한 질문 쌍은 0으로 둔다. GM은 공개를 강제하지 않으므로 거절을 우회해 계획한다.
- **소진 판단:** 성공 판정의 조사 결과에 새 단서가 없으면 그 공간을 소진으로 기록한다. 엔진 이벤트만으로 재구성되므로 후임 GM도 같은 판단을 복원한다.
- **재지정:** 열린 task가 새 계획과 다르거나 같은 generation에서 3회 이상 실패하면 generation을 맞춰 재지정한다. 초기 구현에서는 상대에게 더 얻을 것이 없는 질문 task가 영원히 열린 채 남는 결함이 있었다. 공유 없는 시연이 66사이클 동안 미해결로 끝나 발견했고, 위 이득 기준과 재지정 규칙으로 고쳤다. 또 플레이어의 `held_information_ids`는 본인 소유만 담으므로 이미 공개된 단서를 '얻을 수 있는 단서'로 잘못 세던 결함도 공개 ID를 빼도록 고쳤다(공유를 켠 시연이 77사이클 미해결로 끝나 발견했다).
- **기록:** 로컬 SQLite 원장(`gm-v1` 네임스페이스, `game_id:context_key` 키)에 이벤트 커서, 공간별 시도, 장애·승계·인간 개입 요청, 거절 쌍, 결정 기록을 저장한다. 결정 기록 항목은 term, map_version, 점수, 선택, 변경 task다.

## 실행

엔진 체크아웃이 `../Game-engine`에 있어야 한다. 드라이버가 엔진을 임시 DB로 직접 띄우고 종료한다. LLM 호출은 없다.

```powershell
python -m gm_coordinator.v1_demo --scenario normal --out gm_coordinator/examples/v1_normal.json
python -m gm_coordinator.v1_demo --scenario normal --no-share
python -m gm_coordinator.v1_demo --scenario player-fault
python -m gm_coordinator.v1_demo --scenario gm-fault
python -m gm_coordinator.v1_demo --scenario human
python -m unittest gm_coordinator.test_engine_v1 -v
```

`human`은 모든 플레이어 장애 후 GM 장애를 주입한다. 엔진이 `waiting_for_human`으로 멈추면 운영자 대역이 `APPOINT_GM`으로 예비 풀원을 지정한다. 새 GM은 `awaiting_setup`에서 플레이어를 다시 모집한다.

## 검증 결과 (2026-10-19, seed 7, 실제 A 엔진 프로세스 + 스크립트 플레이어 대역)

결과 원본은 `examples/v1_*.json`이다.

| 시나리오 | 결과 | 사이클 | 비고 |
|---|---|---|---|
| normal (공유 켬) | solved, GM FINISH | 30 | 플레이어가 자발적으로 공유 |
| normal --no-share | solved | 54 | 공유 0건, GM의 질문 교환 배정으로 해결 |
| player-fault | solved | 37 | 사이클 3 FAIL_PLAYER → GM 재배정·예비 활성화 |
| gm-fault | solved | 36 | 사이클 3 FAIL_GM → 엔진 승계, 이전 GM `fenced`, 후임이 빈 맥락에서 재계획 |
| human | solved | 53 | 후보 없음 → 운영자 APPOINT_GM → 새 GM이 재모집 |

단일 seed의 사이클 수는 비교 근거가 아니다. 공유 여부나 GM 정책의 효과를 주장하려면 여러 seed의 분포와 기준선이 필요하다.

자동 테스트 `test_engine_v1.py`는 실제 엔진을 테스트마다 새로 띄워 아래 항목을 확인한다. 엔진 체크아웃이 없으면 건너뛴다.

- 초기 활성화 시 직업과 성향이 서로 다르고, 대안 2개 이상·선택·이유가 기록되며, 공간을 병렬로 분할하는지
- 매 사이클 계획과 ADVANCE 조건
- 플레이어 장애 시 엔진은 소유권만 해제하고 GM이 재배정하는지(generation 증가)
- 승계 시 이전 임기 차단, 새 context_key와 term 사용, 개인 목표 미유입
- 후보 없음 → 운영자 지정 → 재모집
- 역할 자격 없이는 생성되지 않는지
- 이득 계산과 거절 반영

## B 어댑터 변경 (`player_adapter.py`)

- 최신 B의 `decide()` 2개 반환 `(action, payload)`를 받는다. 이전 3개 반환(공개 제안 포함)도 계속 받는다.
- question은 `target_id`(최신 B)와 `target`(이전 B) 중 정확히 하나만 허용한다.
- v1 부분 공개 단서 `{id, category, redacted:true, text}`를 받아들이되 `excludes`를 만들지 않아 배제 근거로 쓰이지 않게 한다.

## 남은 일과 차단 사항

- **B:** 최신 `agent.py`의 `respond_to_question()`이 `return True, ""`로 끝난다. 보유 단서가 있으면 `(offer_ids, refused)` 대신 잘못된 형식을 반환하므로 v1 `questions/respond`가 거부한다. 이 상태로는 실제 B로 질문 응답을 할 수 없다. 실제 B 플레이어 연결은 엔진의 `reasoning_inputs()`/`EngineClient.action_request()`를 쓰는 B 런타임 측 작업이며, 이번 C 변경에 포함하지 않았다.
- **A에 제안:** task에 질문 대상(`target_id`)을 둘 필드가 없다. 그래서 GM은 질문 task를 '만날 공간'으로만 지정한다. ADVANCE 직후 계획 이전에 플레이어 행동이 들어오는 경쟁은 C가 즉시 계획으로 줄였지만, 엔진에 계획 대기 창을 둘지는 합의가 필요하다.
- **기존 탐침:** B 저장소 구조 변경(루트로 이동, `engine.py`·`deploy/` 제거)으로 `team_adapters`/`message_language`의 구 B 레이아웃 탐침 7개는 이제 건너뛴다. 이전 기록은 `examples/team_update_review_20261008.json` 등에 보존되어 있다.
- 검증 범위: 실제 A 엔진 + C GM + 스크립트 플레이어. 실제 B LLM, 유료 모델, 학습 효과, H1–H3는 검증하지 않았다.
