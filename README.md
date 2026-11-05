# Evidence Harness

요구사항, 기술 선택, 실제 검사, AI 제안에 대한 판단을 연결하는 **실행 가능한 로컬 stdio MCP**입니다. 별도 모델 API·계정 키·원격 MCP 서버가 필요하지 않습니다.

Python 3.11 이상, MCP SDK `1.26.0`, SQLite를 사용합니다. 제공된 AI 호스트의 버전·모델·권한 설정을 변경하지 않습니다.

## 설치와 연결

**실제 과제 작업 디렉터리**의 터미널에서 실행합니다. 아래는 Linux 계열 셸 예시입니다. 로컬 설치와 공개 자료 사용이 허용된 환경에서만 진행합니다.

```bash
git clone https://github.com/poshygg/evidence-harness.git /tmp/evidence-harness
python3 /tmp/evidence-harness/scripts/setup.py --workspace "$PWD"
```

이미 `/tmp/evidence-harness`가 있으면 다시 clone하지 말고 기존 저장소와 커밋을 확인합니다. 재현 가능한 사용에는 테스트한 공개 커밋을 checkout한 뒤 setup을 실행합니다.

설치 스크립트는 전용 `.venv`, 고정 의존성, 작업공간 설정을 만들고 **실제 MCP initialize → tools/list → tools/call**을 시험합니다. 끝에 현재 환경의 실제 경로를 넣은 MCP JSON을 출력합니다.

1. 출력된 `<작업폴더>/.evidence-harness/mcp-servers.json`을 확인합니다.
2. 플랫폼의 MCP 설정 탭에 해당 서버 항목을 추가하고 저장합니다. 기존 서버와 플랫폼의 최상위 JSON 형식을 보존합니다.
3. 호스트에서 서버 연결 상태를 확인한 뒤 `evidence_status`를 실제 호출합니다.

일반적인 `mcpServers` JSON을 생성하지만 특정 플랫폼이 같은 wrapper를 받는다고 보장하지 않습니다. 설치 smoke 성공과 해당 플랫폼에서의 연결 성공은 별도입니다. [자세한 연결 절차](docs/QUICKSTART.ko.md).

## 도구 9개

| 도구 | 목적 |
|---|---|
| `init_workspace` | 목표·제약·시간 예산 기록 |
| `record_requirement` | 요구사항·근거·수용 기준 기록 |
| `record_decision` | 대안·선택·트레이드오프 기록 |
| `register_check` | 로컬 검사 manifest 등록 |
| `run_check` | 실제 검사 비동기 실행 |
| `compare_runs` | 정확성·조건 동등성을 확인한 비교 |
| `record_ai_assessment` | AI 제안의 채택·수정·거절 근거 기록 |
| `evidence_status` | 상태·미검증·실패·stale와 job 조회 |
| `export_review_packet` | 이미 기록된 사실과 한계를 로컬 문서로 내보내기 |

서버는 작업공간에 바인딩됩니다. 도구 인자에 별도 `workspace_id`를 넣지 않습니다. 반환값에는 workspace ID가 포함됩니다. [현재 도구 계약](docs/TOOL-CONTRACTS.ko.md).

## 기록과 검증

- AI 주장, 보고한 판단, 실행 관찰을 구분합니다. 증거를 연결했다고 자연어 주장이 자동으로 검증되는 것은 아닙니다.
- 종료 코드 0만으로 정답 통과를 선언하지 않습니다. 검사 프로그램의 oracle 결과를 별도로 확인합니다.
- 동일 입력·검사·의존성·환경·실행 인자로 baseline과 candidate를 비교합니다. 명시적인 `--variant` 값 차이만 허용합니다.
- 원본 입력·검사·코드는 내용 해시와 로컬 blob으로 보존합니다. 변경 후에는 `stale`, 실행 중 변경은 `contaminated`로 표시합니다.
- runner는 전체 subprocess의 실제 wall time을 측정합니다. 사용자 정의 지표는 검사 프로그램이 보고한 값이며 전체 메모리는 측정하지 않습니다.
- 작업은 동시 1개, 전체 deadline 기본 30초(최대 120초), 로그 합계 1MiB, 추적 입력 최대 32MiB/2048파일, blob 저장소 256MiB로 제한합니다.

실행 데이터는 **공개 소스 디렉터리 밖의 작업공간 `.evidence-harness/`**에 저장합니다. 자동 push·업로드·텔레메트리·외부 AI 호출을 구현하지 않습니다. 등록한 검사 코드 자체는 신뢰해야 합니다. subprocess 실행은 보안 샌드박스가 아닙니다.

## 작은 합성 예제로 연습

실제 과제와 무관한 정수 중복 제거 예제가 있습니다. 별도 임시 작업공간을 만들어 수동 정답 7개와 반복 실행·비교·기록 내보내기를 수행합니다.

```bash
/tmp/evidence-harness/.venv/bin/python /tmp/evidence-harness/examples/synthetic_demo/demo.py
```

새 환경 검증:

```bash
python -m unittest discover -s tests -v
```

[검증 기록과 범위](docs/VALIDATION.ko.md), [실행·비교 기준](docs/VERIFICATION.ko.md).

## 작업 중의 흐름

요구사항과 불확실한 가정 → 작은 baseline → 핵심 기능 → 정확성 검사 → 조건을 맞춘 개선 비교 → 최종 코드 재검증과 한계 정리 순으로 사용합니다. 알고리즘·앱 스택은 실제 과제에 맞춰 선택합니다. [작업 지침 예시](templates/AGENTS.exam.example.md).

AI가 금지된 후속 평가에서는 AI·MCP를 사용하지 않습니다. 이 도구는 제한된 단계의 답안을 대신 작성하거나 기록 열람 허용을 보장하지 않습니다.

[구조 설명](docs/ARCHITECTURE.ko.md) · [공개·자료 경계](docs/PUBLICATION.ko.md) · [OpenAI MCP 문서](https://learn.chatgpt.com/docs/extend/mcp?surface=app) · [고정한 MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)
