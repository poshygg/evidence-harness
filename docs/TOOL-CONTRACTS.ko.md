# 현재 도구 계약: v0.1.0

실행형 구현의 기준 문서입니다. 정확한 입력 타입은 MCP `tools/list`와 `src/evidence_harness/server.py`, 실제 동작은 `core.py`·`runner.py`를 기준으로 확인합니다. 초기 설계의 입력을 그대로 복사하지 않습니다.

## 공통

서버 실행 시 `--workspace CONFIG`로 작업공간을 바인딩합니다. 각 도구에 `workspace_id`, 임의 workspace_root 또는 state_dir를 전달하지 않습니다. 변경 도구는 `request_id`를 받습니다. 동일 키·동일 입력은 기존 결과를 반환하고 다른 입력은 `IDEMPOTENCY_CONFLICT`입니다. 서버가 UUID와 UTC 시각을 생성합니다.

기록 envelope는 `schema_version`, `workspace_id`, `record_id`, `kind`, `created_at`, `status`, `data`, `limitations`, `reused`입니다. 조회·job·비교 응답은 각 용도의 구조를 반환합니다. 실패는 `error_code`와 설명을 포함합니다. 실제 입력 예시는 `templates/record-inputs.example.json`에 있습니다.

원장 기록은 추가 방식입니다. 정정은 선택적 `supersedes_id`를 씁니다. `reported_by`는 `candidate` 또는 `codex`입니다. 코드가 자동으로 후보자의 이해·신뢰 이유를 만들어내지 않습니다. 참조 ID 존재 검사는 자연어 주장의 진실성 검증과 다릅니다.

## 입력

| 도구 | 필수 입력 | 주요 선택 입력 |
|---|---|---|
| init_workspace | request_id, goal | constraints, time_budget_minutes=180 |
| record_requirement | request_id, title, acceptance_criteria | source_ref, source_excerpt, interpretation, interpretation_status, priority, check_ids, reported_by, supersedes_id |
| record_decision | request_id, question, alternatives, selected, reason | tradeoffs, evidence_ids, unverified_assumptions, reported_by, supersedes_id |
| record_ai_assessment | request_id, claim, action, reason | conversation_ref, paraphrase, evidence_ids, reported_by, remaining_uncertainties, supersedes_id |
| register_check | request_id, manifest_path | requirement_ids |
| run_check | request_id, check_id | 없음 |
| compare_runs | baseline_run_id, candidate_run_id | metric_names, scope=current 또는 historical |
| evidence_status | 없음 | max_items=20, job_id |
| export_review_packet | request_id, output_path | max_items=30 |

source_ref는 작업공간 안의 파일 참조입니다. source가 없으면 불확실한 해석으로 남깁니다. evidence_ids가 빈 판단은 잠정 또는 미검증으로 기록할 수 있습니다. action은 adopted/modified/rejected/deferred입니다.

## 검사의 입력과 출력

manifest의 형식은 `schemas/check-manifest.schema.json`과 실행형 합성 예제에서 확인합니다. 임의 셸 문자열 대신 `argv` 배열을 사용하며 셸 실행기와 inline code 옵션을 거절합니다. 실행은 등록한 로컬 check_id로만 시작합니다. manifest 내용이 바뀌면 새로 등록합니다.

필수 구성은 argv, cwd, 소스·fixture·검사·lockfile 경로, oracle 설명, seed, warmups, repeats, timeout_seconds, result_artifact, metric_definitions입니다. 의존성·캐시 디렉터리 전체는 추적하지 않습니다. task의 의미와 실행 권한은 실제 환경의 허용 범위를 따릅니다.

검사 프로그램은 매 실행마다 새 UTF-8 JSON을 result_artifact에 씁니다.

```json
{"correctness":{"status":"passed","checks":7},"metrics":{}}
```

status는 passed/failed/not_checked이고 통과에는 양의 정수 checks가 필요합니다. 위 수치가 정답 판정의 독립성이나 충분함을 보장하지는 않습니다. 실제 oracle 코드를 검토해야 합니다. 기존의 도구 소유가 아닌 출력 파일은 `OUTPUT_EXISTS`로 보존하고 실행을 중단합니다. 프로그램이 새 결과를 만들지 않으면 이전 결과 파일로 통과할 수 없습니다.

runner가 기록하는 wall_time_ms는 unit=ms, direction=lower, measurement_scope=whole_subprocess입니다. 다른 지표는 manifest에서 정의하고 metrics에 이름:유한숫자로 보고합니다. 이 값들은 check_reported로 구분합니다. 현재 전체 메모리 측정기는 없습니다.

## 비동기 작업

run_check는 job_id를 즉시 반환합니다. evidence_status(job_id=...)로 조회합니다.

```text
queued → running → completed | failed | interrupted
```

최종 응답의 run은 실제 run 기록 envelope입니다. execution_status와 correctness_status를 분리합니다. completed job도 정답 검사에서 실패할 수 있습니다. CLI call의 run_check는 worker를 유지하기 위해 완료까지 기다립니다.

timeout_seconds는 준비·워밍업·모든 반복·수집을 합친 전체 job의 예산입니다. final integrity 확인과 프로세스 종료에는 짧은 정리 시간이 추가될 수 있습니다. 실행은 동시 1개, 로그 합계 1MiB입니다. 큰 입력은 추적 범위를 좁혀야 합니다.

정상 종료와 시간 초과에서는 POSIX process group 또는 Windows Job Object로 자식 프로세스 정리를 시도합니다. Windows에서는 프로세스 시작 후 Job Object에 배정하는 짧은 구간이 있습니다. 하드 종료·전원 손실 때의 완전한 정리·정확한 측정 완료를 보장하지 않습니다. 소유 프로세스가 사라지면 interrupted로 표시하며 자동 재실행하지 않습니다. 실제 샌드박스의 역할을 대신하지 않습니다.

## 비교

양쪽이 실제 완료 run이고, correctness=passed, 반복 수 충족, 오염 없음이어야 합니다. 현재 비교는 stale도 거절합니다. historical은 보존된 과거 코드의 결과 비교일 뿐 현재 구현 검증이 아닙니다.

fixture/check/lockfile 지문, 실효 환경, cwd, seed, 반복 정책, oracle 설명과 metric 정의를 확인합니다. 명령 인자는 명시적인 --variant/--variant-id의 값 차이만 정규화합니다. 다른 인자가 달라지면 COMPARISON_BLOCKED입니다. 입력 크기를 바꾸고 같은 문제의 성능 개선이라고 표현할 수 없습니다.

결과는 원시 samples, n, 중앙값·최소·최대, 절대 차이와 관측 개선율입니다. 작은 표본에서 일반적 우월성이나 최적성을 주장하지 않습니다. baseline 중앙값이 0이면 비율을 계산하지 않습니다.

## 상태와 내보내기

현재 입력·코드와 해시가 달라지면 stale, 실행 전후 다르면 contaminated로 표시합니다. 연결된 검사들이 통과해도 요구사항의 모든 의미가 독립적으로 검증되었다고 선언하지 않습니다. 내보내기는 실제 원장에 있는 사실과 한계를 로컬 Markdown으로 정리하며, 기존의 다른 파일을 덮어쓰지 않습니다.

설치 및 표준 MCP 호출 검증과 특정 플랫폼의 등록·호출 검증은 별개입니다.
