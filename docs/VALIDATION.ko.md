# 실행 검증 기록: v0.1.0

검사일: 2026-11-05. 실제 실행과 형식 검사를 구분한다.

## 로컬 실행 결과

- Windows / Python 3.12.10 / mcp==1.26.0.
- unittest 30개 실행, 29개 통과, 1개 skip, exit 0. skip은 Windows symlink 생성권한이며 직접 경로 이탈 검사는 통과했다.
- 실제 stdio MCP initialize → tools/list(9개) → tools/call 확인.
- SDK의 2025-11-25 협상과 별도의 raw JSON-RPC 2025-06-18 협상·도구 호출 확인.
- setup 기본 설치(전용 venv의 pip 준비, 고정 의존성, editable 설치)와 --skip-install 경로 모두 실제 성공.
- 합성 예제의 양쪽 수동 정답 7개, 워밍업 뒤 실제 subprocess wall time 5회, 공정 비교와 로컬 문서 내보내기 성공. 이 예제의 데이터는 합성이지만 실행·측정은 실제다.

회귀 검사는 동일 request 중복방지·충돌, 동시 기록, 판단 출처, 미검증 상태, 경로 이탈, manifest 변경, 미생성/기존 결과의 잘못된 통과 방지, 실제 반복 수·원본 blob, stale·비교조건 차이, timeout 부모·자식 정리, 출력 상한, 비variant 인자 차이 거절, 기존 파일 보존, manifest 변경 후 완료 job 재조회, 33MiB 입력 등록 거절을 포함한다.

## 실제 Linux 실행 확인

WSL Ubuntu / Python 3.12.3에서 공개 e6fd352 커밋의 코드와 고정된 Linux/공용 wheel 31개로 전용 가상환경을 만들었다. 기본 setup과 --skip-install, 실제 stdio 9개 도구 및 evidence_status, 프로토콜 2025-11-25와 구형 2025-06-18 호출이 성공했다. unittest 30개 모두 통과, skip 0, exit 0이었다. 합성 데모의 반복 측정·comparable=true·문서 내보내기도 확인했다.

WSL의 직접 GitHub HTTPS clone은 90초 타임아웃이었다. 공개 소스와 고정 wheel을 Windows에서 받아 새 Linux 임시 경로에 전달해 검증했으므로, 이를 WSL 온라인 clone·온라인 설치 또는 특정 시험 플랫폼 연결 성공이라고 표현하지 않는다. 시스템·네트워크 설정과 소스 코드는 바꾸지 않았다. Windows의 공개 README 익명 HTTP 조회와 저장소 공개 상태는 확인했다.

## 자동 검사

ci/github-actions.example.yml에 Linux와 Windows의 Python 3.11용 자동 검사 예제를 제공한다. 현재 게시 인증에 workflow 권한이 없어 활성화하지 않았다. Windows 로컬 검증과 위의 별도 Linux 실검증을 수행했다. CI를 활성화하면 해당 커밋의 실제 자동 실행 결과를 별도로 확인한다.

## 구조 검사와 한계

JSON/TOML 문법과 입력·manifest·실제 run envelope 스키마의 구조를 검사한다. 스키마는 ID의 실제 존재, 파일의 정답, 자연어 주장의 진실성이나 완전한 격리를 보장하지 않는다.

특정 시험 플랫폼의 MCP 탭 등록·실제 호출은 수행하지 않았다. 개인 접속 링크나 사전 체험을 열지 않았다. 실제 과제·데이터·비공개 작업물은 공개하지 않았다.

SDK와 일부 현재 의존성 조합에서 forward-reference 경고가 stderr에 표시될 수 있다. 현재 테스트에서는 프로토콜 동작이 통과했으며 stdout의 MCP 메시지와 분리된다.

Windows에서는 실행 후 Job Object에 배정하는 짧은 구간이 있다. 하드 종료·전원 손실 때의 완전한 자식 정리와 강한 변조 방지를 보장하지 않는다. 실제 checker의 oracle·사용자 정의 지표는 별도 검토가 필요하며, 전체 메모리 측정과 알고리즘 최적성은 검증하지 않았다.
