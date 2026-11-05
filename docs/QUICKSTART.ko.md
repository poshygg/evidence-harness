# 제공 환경에서 설치하고 연결하기

공개 소스를 제공 작업 환경으로 가져와 **로컬 stdio MCP**로 실행합니다. 별도 AI API 키나 외부 MCP 호스팅은 필요하지 않습니다. Git, Python 3.11 이상, 가상환경·패키지 설치 기능이 필요합니다. 설치는 하네스 전용 `.venv`만 사용하고 Codex 자체를 업데이트하지 않습니다.

## 1. 공개 저장소 가져오기

제공 환경 **내부 터미널**에서 실행합니다.

```bash
git clone https://github.com/poshygg/evidence-harness.git /tmp/evidence-harness
```

`/tmp/evidence-harness`가 이미 있으면 clone을 반복하거나 폴더를 지우지 말고 저장소와 커밋을 먼저 확인합니다.

```bash
git -C /tmp/evidence-harness remote get-url origin
git -C /tmp/evidence-harness rev-parse HEAD
```

사용할 공개 커밋이 지정되어 있다면 그 커밋으로 고정합니다. 임시 디렉터리는 환경 초기화 시 사라질 수 있으므로 연결 전 파일 존재를 확인합니다.

## 2. 실제 과제 작업폴더에서 설치 한 번

현재 경로가 **실제 과제 작업폴더**인지 확인합니다. 공개 하네스 저장소 폴더 안에서 실행하지 않습니다.

```bash
pwd
python3 --version
python3 /tmp/evidence-harness/scripts/setup.py --workspace "$PWD"
```

스크립트는 전용 가상환경과 고정 의존성(MCP SDK `1.26.0`)을 준비하고, 과제 작업폴더의 `.evidence-harness/`에 다음 파일을 만듭니다.

- `workspace.json`: 실제 작업공간과 비공개 기록 경로.
- `mcp-servers.json`: 실제 절대 경로가 들어 있는 MCP 설정 JSON.
- `codex-config.toml`: 일반 Codex 설정용 서버 항목.

이어 `doctor`와 표준 MCP 클라이언트의 **initialize → tools/list → evidence_status 실제 호출**을 검사하고, 복사할 JSON과 파일 경로를 출력합니다. 같은 설정으로 재실행할 수 있으며 내용이 다른 기존 파일은 덮어쓰지 않습니다. `--skip-install`은 이미 설치된 `.venv`를 검사할 때만 사용합니다.

**이 검사 통과는 플랫폼의 MCP 탭을 통한 연결 성공과 다릅니다.** 다음 단계를 진행합니다. 스크립트는 Codex 모델·권한·설정을 변경하지 않습니다.

## 3. MCP 탭에 생성 설정 추가

```bash
cat .evidence-harness/mcp-servers.json
```

플랫폼의 MCP 탭에서 기존 서버 항목을 보존한 채 `evidence_harness`를 추가하고 저장합니다. 파일의 `command`와 `args`는 현재 환경의 실제 경로이므로 수동으로 `/workspace` 같은 경로를 추측해 넣지 않습니다. `.venv/bin/python`을 시스템 Python으로 바꾸지 않습니다.

아래는 **모양 설명용이며 그대로 복사할 설정이 아닙니다.** 플랫폼이 `mcpServers` 최상위 키를 받는지 등은 기존 JSON과 안내를 기준으로 맞춰야 합니다.

```json
{
  "mcpServers": {
    "evidence_harness": {
      "command": "/actual/repository/.venv/bin/python",
      "args": ["-m", "evidence_harness", "serve", "--workspace", "/actual/task/.evidence-harness/workspace.json"]
    }
  }
}
```

저장 후 플랫폼이 제공하는 재연결 절차를 따릅니다. 호스트에서 `/mcp`를 지원하면 서버 목록을 확인합니다. 재시작이 필요하면 플랫폼이 허용하는 절차만 사용하고 모델·권한·감독 설정을 바꾸지 않습니다.

## 4. 호스트에서 실제 도구 호출 확인

제공된 Codex에 다음처럼 요청합니다.

> evidence_harness의 evidence_status를 max_items=3으로 실제 호출해줘. 도구가 보이지 않으면 호출했다고 가정하지 말고 연결 오류를 알려줘.

실제 도구 호출이 표시되고 해당 작업공간의 상태가 반환되어야 합니다. 서버 목록에 이름만 있는 상태와 실제 호출 성공은 다릅니다. 연결 후에는 `init_workspace`로 목표·시간 예산을 기록하고 요구사항·결정·검증을 기록합니다.

## 5. 허용된 로컬 CLI로 사용하는 대안

플랫폼이 CLI 사용을 허용하는 경우 같은 코어를 직접 호출할 수 있습니다. 아래는 Linux/POSIX 셸 예시입니다.

```bash
/tmp/evidence-harness/.venv/bin/python -m evidence_harness call \
  --workspace "$PWD/.evidence-harness/workspace.json" \
  --tool init_workspace \
  --arguments '{"request_id":"goal-1","goal":"필수 기능 구현과 검증","time_budget_minutes":180}'

/tmp/evidence-harness/.venv/bin/python -m evidence_harness call \
  --workspace "$PWD/.evidence-harness/workspace.json" \
  --tool evidence_status --arguments '{"max_items":5}'
```

CLI의 `run_check`는 완료까지 프로세스를 유지합니다. MCP의 `run_check`는 `job_id`를 즉시 반환하므로 `evidence_status(job_id=...)`로 완료를 확인합니다.

일반 Codex CLI는 아래 방식의 등록도 지원합니다. 플랫폼에 MCP 탭이 있으면 탭을 우선하며, 이 명령으로 플랫폼 정책이나 제한을 우회하지 않습니다.

```bash
codex mcp add evidence_harness -- \
  /tmp/evidence-harness/.venv/bin/python -m evidence_harness serve \
  --workspace "$PWD/.evidence-harness/workspace.json"
```

## 오류 확인

| 증상 | 확인 |
|---|---|
| Python·venv·의존성 설치 실패 | 제공 환경의 설치·네트워크 지원 범위를 확인. 호스트 업데이트나 차단 우회 금지 |
| 기존 설정이 다르다는 오류 | 기존 파일을 확인. 무조건 덮어쓰거나 삭제하지 않음 |
| 모듈을 찾지 못함 | 생성 JSON의 전용 `.venv` Python과 절대 경로를 사용했는지 확인 |
| 설치 smoke는 통과하나 호스트에 안 보임 | MCP 탭 JSON 형식·저장·호스트 재연결 절차 확인 |
| 서버는 보이나 호출 실패 | 실제 오류와 시각을 보존하고 제공 환경의 문의 기능 사용 |

로컬 실행도 보안 샌드박스는 아닙니다. 등록한 코드와 환경의 허용 범위를 확인하고 실제 기록·과제 파일은 제공 작업공간 안에 둡니다. 외부 HTTP 서버로 과제 데이터를 보내는 방식으로 자동 전환하지 않습니다. AI가 금지되는 후속 단계에서는 AI·MCP를 사용하지 않으며 기록 문서 열람 허용도 임의로 가정하지 않습니다.

일반 연결 기능은 [OpenAI 공식 문서](https://learn.chatgpt.com/docs/extend/mcp?surface=app)를 참고했습니다. 특정 플랫폼의 JSON 형식이나 호스트 연결 성공을 검증했다는 뜻은 아닙니다.
