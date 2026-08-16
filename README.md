# AMX

여러 대의 서버에서 쓰는 Claude 계정을 중앙에서 관리하는 시스템이다. 계정을 어느
서버에 배정할지 정하고, 로그인 정보를 안전하게 하달하고, 한도가 차면 자동으로 다음
계정으로 전환하고, 다 쓴 계정을 회수해 재배정한다. 중앙 서버(ams-server)·서버 상주
에이전트(ama-agent)·계정 전환 도구(tsamx)·관리자 웹 콘솔(ams-web) 네 구성요소로
이뤄진다.

## 문서 지도

무엇을 찾는지에 따라 아래에서 시작한다. 각 문서의 역할·읽는 시점·관련 문서까지 담은 전체
색인은 **[docs/README.md](docs/README.md)**에 있다 — 처음이면 여기부터 훑는다.

| 문서 | 무엇인가 |
|---|---|
| [docs/OVERVIEW.md](docs/OVERVIEW.md) | 전문용어 없이 프로젝트 전체를 그리는 사람용 안내서 |
| [docs/AMX-DESIGN.md](docs/AMX-DESIGN.md) | 설계 원본이자 현행 동작의 **최종 기준(SSOT)** |
| [docs/PROD-GUIDE.md](docs/PROD-GUIDE.md) | 실제 장비에 올려 운영하는 절차. 설치부터 제거까지 |
| [docs/DEV-TEST-GUIDE.md](docs/DEV-TEST-GUIDE.md) | 개발·시험이 운영과 다른 부분만 모은 차이분 |
| [docs/DEPLOYMENT-RUNNER.md](docs/DEPLOYMENT-RUNNER.md) | 러너 보호·Langfuse 훅·셸 alias 총정리(§9) |
| [docs/DEPLOYMENT-TLS.md](docs/DEPLOYMENT-TLS.md) | 제어면 gRPC의 TLS/mTLS 인증서 심화 |
| [docs/TSAMX-GUIDE.md](docs/TSAMX-GUIDE.md) | tsamx 사용법·개조 내역과 사설 저장소 설치 인증(§6) |
| [docs/UPSTREAM-SYNC.md](docs/UPSTREAM-SYNC.md) | tsamx 원본(claude-swap) 갱신 반영 절차 |
| [docs/BACKLOG.md](docs/BACKLOG.md) | 이월·미해결 항목의 현행 원장(G1, B4 …) |
| [docs/design-notes/](docs/design-notes/) | 단계별 설계 메모. 현행 기준은 AMX-DESIGN.md |
| [docs/archive/](docs/archive/) | 완료된 실행 계획·종료된 인수인계 등 이력 스냅샷 |

문서별 상세 설명은 반복하지 않는다 — [docs/README.md](docs/README.md) 색인이 맡는다.
구성요소별 세부는 각 디렉터리의 README를 본다: `ams-server/`, `ams-web/`,
`tsamx/`, 그리고 종합 시험은 `e2e/`.
