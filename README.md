# AMX

여러 대의 서버에서 쓰는 Claude 계정을 중앙에서 관리하는 시스템이다. 계정을 어느
서버에 배정할지 정하고, 로그인 정보를 안전하게 하달하고, 한도가 차면 자동으로 다음
계정으로 전환하고, 다 쓴 계정을 회수해 재배정한다. 중앙 서버(ams-server)·서버 상주
에이전트(ama-agent)·계정 전환 도구(tsamx)·관리자 웹 콘솔(ams-web) 네 구성요소로
이뤄진다.

## 문서 지도

무엇을 찾는지에 따라 아래에서 시작한다.

| 문서 | 무엇인가 |
|---|---|
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | 전문용어 없이 프로젝트 전체를 그리는 사람용 안내서. 처음 왔거나 길을 잃었을 때 |
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 모듈별 기동 방법. 한 명령 풀스택부터 서버·웹·에이전트·tsamx 개별 기동, 시험 실행, 문서 지도 |
| [docs/AMX-DESIGN.md](docs/AMX-DESIGN.md) | 설계의 원본이자 현행 동작의 **최종 기준(SSOT)**. 정확한 사양이 필요할 때 |
| [docs/PROD-GUIDE.md](docs/PROD-GUIDE.md) | 실제 장비에 올려 운영하는 절차. 중앙 서버·에이전트 설치부터 제거까지 |
| [docs/DEV-TEST-GUIDE.md](docs/DEV-TEST-GUIDE.md) | 개발·시험 환경이 운영과 다른 부분(평문 기동 등)만 모은 차이분 |
| [docs/DEPLOYMENT-RUNNER.md](docs/DEPLOYMENT-RUNNER.md) | 러너(claude 실행) 보호와 Langfuse 사용량 관측 훅 설치 |
| [docs/DEPLOYMENT-TLS.md](docs/DEPLOYMENT-TLS.md) | 제어면 gRPC의 TLS/mTLS 인증서 발급·교체 심화 |
| [docs/TSAMX-GUIDE.md](docs/TSAMX-GUIDE.md) | tsamx 사용법·개조 내역과 사설 저장소 설치 인증(§6) |
| [docs/UPSTREAM-SYNC.md](docs/UPSTREAM-SYNC.md) | tsamx 원본(claude-swap) 업데이트를 개조판에 반영하는 절차 |
| [docs/BACKLOG.md](docs/BACKLOG.md) | 이월·미해결 항목의 현행 원장. 번호(G1, B4 …)로 추적 |
| [docs/design-notes/](docs/design-notes/) | 각 단계를 만들기 전의 설계 메모(as-designed 기록). 현행 기준은 AMX-DESIGN.md |
| [docs/archive/](docs/archive/) | 완료된 실행 계획(todo)·종료된 인수인계 등 이력 스냅샷 |

구성요소별 세부는 각 디렉터리의 README를 본다: `ams-server/`, `ams-web/`,
`tsamx/`, 그리고 종합 시험은 `e2e/`.
