# 연결 설정 생성과 적용

실행 서버는 로컬 `stdio` 방식입니다. 실제 설치 절차는 [빠른 시작](../docs/QUICKSTART.ko.md)을 따릅니다.

```bash
python3 /tmp/evidence-harness/scripts/setup.py --workspace "$PWD"
```

위 명령은 실제 과제 작업폴더에서 실행합니다. 하네스 전용 가상환경을 준비하고 작업폴더의 `.evidence-harness/`에 `workspace.json`, 실제 절대 경로가 담긴 `mcp-servers.json`, 일반 Codex용 `codex-config.toml`을 생성합니다. Codex 자체를 설치·업데이트하거나 호스트 설정을 자동 변경하지 않습니다. 내용이 다른 기존 설정은 덮어쓰지 않습니다.

## 플랫폼의 MCP 탭

1. 생성된 `mcp-servers.json`을 확인합니다.
2. 플랫폼의 기존 JSON 형식과 안내를 기준으로 `evidence_harness` 항목을 추가합니다. 기존 다른 서버를 보존합니다.
3. 저장 후 제공된 재연결 절차를 따릅니다. `/mcp`를 지원하는 호스트에서는 서버 목록을 확인합니다.
4. Codex에서 `evidence_status`를 **실제 호출**합니다.

`mcpServers` 최상위 키를 특정 플랫폼이 그대로 받는지는 검증되지 않았습니다. 생성 파일의 서버 `command`·`args`를 사용하되 플랫폼이 요구하는 감싸는 구조는 보존합니다. 개인 PC 경로나 임의의 `/workspace` 경로를 넣지 않습니다.

설치 스크립트는 표준 MCP 클라이언트로 initialize/list/call을 검증합니다. 이 성공은 대상 플랫폼의 호스트에서 호출한 성공과 구분됩니다.

## 이 디렉터리의 예시 파일

`codex.config.example.toml`과 `mcp-servers.example.json`은 **형태를 설명하는 수동 예시**입니다. 현재 환경의 유효한 경로가 아니므로 그대로 복사하지 않습니다. 설치 스크립트가 생성한 파일을 우선 사용합니다.

일반 Codex CLI의 `codex mcp add`도 로컬 서버를 등록할 수 있습니다. 플랫폼에 MCP 탭이 있으면 탭을 우선하며 CLI로 정책이나 제한을 우회하지 않습니다. 허용된 일반 CLI의 명령 예시는 빠른 시작 문서에 있습니다.

로컬 실행이나 MCP 등록 자체가 지원되지 않으면 오류를 보존하고 제공 환경의 문의 기능 또는 허용된 기본 도구를 사용합니다. 외부 HTTP 서버로 과제 데이터를 보내는 방식으로 자동 전환하지 않습니다.

일반 설정 문법은 [OpenAI 공식 문서](https://learn.chatgpt.com/docs/extend/mcp?surface=app)를 참고했습니다.
