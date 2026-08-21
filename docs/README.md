# docs/ 문서 색인

AMX 문서의 지도다. 어떤 문서가 무엇을 담고, 누가 언제 열어야 하는지를 한 장에 모았다.
각 항목은 역할·읽는 시점·이어 볼 문서 순으로 적었다. 정확한 현행 사양이 필요하면 언제나
[AMX-DESIGN.md](AMX-DESIGN.md)가 최종 기준(SSOT)이고, 나머지는 그 사양을 상황별로 풀어 쓴
안내서다.

## 최상위 문서

**[OVERVIEW.md](OVERVIEW.md)** — 전문용어 없이 프로젝트 전체를 그려 주는 사람용 안내서.
처음 왔거나 큰 그림을 잃었을 때 먼저 읽는다. 세부 사양은 AMX-DESIGN, 설치는 PROD-GUIDE로
넘어간다.

**[QUICKSTART.md](QUICKSTART.md)** — "일단 켜보고 싶다"일 때 읽는 모듈별 기동 가이드. 한 명령
풀스택부터 서버·웹·에이전트·tsamx 개별 기동, 시험 실행, 앞뒤 확인법까지 담는다. 개념은
OVERVIEW, 운영 배포의 기준 절차는 PROD-GUIDE로 넘어간다.

**[AMX-DESIGN.md](AMX-DESIGN.md)** — 설계 원본이자 현행 동작의 최종 기준(SSOT). 계정 상태기계·
제어면 프로토콜·스윕/락 배정표·경보 kind·과금·RBAC·봉투암호화까지 정확한 사양을 확인할 때
본다. 개발자·리뷰어가 구현과 대조하는 참조점이다. 단계별 설계 배경은 design-notes/에 있다.

**[PROD-GUIDE.md](PROD-GUIDE.md)** — 실제 장비에 올려 운영하는 절차서. 중앙 서버·에이전트
설치부터 방화벽·계정 운영·제거까지, 그리고 운영용 환경변수 전량을 담는다. 서버를 세우거나
운영하는 사람이 손에 들고 따라가는 문서다. 러너 보호·관측은 DEPLOYMENT-RUNNER로 이어진다.

**[DEV-TEST-GUIDE.md](DEV-TEST-GUIDE.md)** — 개발·시험 환경이 운영과 다른 부분(평문 기동,
자동 시크릿 등)만 모은 차이분. 로컬에서 띄우거나 테스트를 돌릴 때 PROD-GUIDE와 함께 본다.

**[DEPLOYMENT-RUNNER.md](DEPLOYMENT-RUNNER.md)** — 러너(claude 실행)를 과금 사고에서 지키는
2층 방어(래퍼·진입점 강제), Langfuse 사용량 훅 설치, 겸용 PC 프로필, 그리고 셸 alias 총정리를
담는다. 에이전트 호스트를 세팅하는 사람이 §9 alias 절과 함께 읽는다. PROD-GUIDE §8이 여기를
참조한다.

**[DEPLOYMENT-TLS.md](DEPLOYMENT-TLS.md)** — 제어면 gRPC의 TLS/mTLS 인증서 발급·교체 심화.
PROD-GUIDE의 TLS 절로 부족할 때, 인증서를 직접 다루는 사람이 본다.

**[TSAMX-GUIDE.md](TSAMX-GUIDE.md)** — tsamx(계정 전환 도구)의 내재화 내역·사용법과 사설 레포
설치 인증(§6, deploy key). tsamx를 설치·개조하거나 배포 인증을 세팅할 때 본다. 원본 갱신
반영은 UPSTREAM-SYNC로 넘어간다.

**[UPSTREAM-SYNC.md](UPSTREAM-SYNC.md)** — tsamx 원본(claude-swap) 업데이트를 개조판에 반영하는
절차(B3/O6). 업스트림을 병합할 때만 펴 보는 문서다. 배경은 TSAMX-GUIDE §4.

**[BACKLOG.md](BACKLOG.md)** — 이월·미해결 항목의 현행 원장. 번호(G1, B4 …)로 출처·이력을
추적한다. "이 문제 어떻게 됐더라"를 되짚거나 다음 작업을 고를 때 본다. 실행 순서의 이력
스냅샷은 archive/todo/에 있다.

## design-notes/ — 단계별 설계 메모 (as-designed 기록)

각 Phase를 만들기 전에 남긴 설계 배경이다. **현행 기준은 AMX-DESIGN.md**이고, 여기는 "왜
그렇게 정했는가"를 되짚을 때 참고하는 기록이라 일부는 최신 동작과 어긋날 수 있다.

- **[p2-architecture.md](design-notes/p2-architecture.md)** — P2 "채널"(제어면 gRPC 스트림·명령
  큐) 설계.
- **[p3-architecture.md](design-notes/p3-architecture.md)** — P3 "스위칭 제어"(자동 전환·
  reconcile·드리프트) 설계.
- **[p4-architecture.md](design-notes/p4-architecture.md)** — P4 "콘솔·운영"(웹 콘솔·경보·사용량
  뷰) 설계.
- **[p5-saas-architecture.md](design-notes/p5-saas-architecture.md)** — P5 SaaS 준비(멀티테넌트·
  다중 인스턴스) 방향 설계.
- **[f1-rbac-architecture.md](design-notes/f1-rbac-architecture.md)** — F1 테넌트 RBAC 상세(P5 S2).
- **[f2-envelope-architecture.md](design-notes/f2-envelope-architecture.md)** — F2 봉투암호화(테넌트
  DEK·KEK) 설계(P5 S3).
- **[recovery-architecture.md](design-notes/recovery-architecture.md)** — 운영 안정화 회복 설계
  (D1·D2·C1, 명령 재큐·타임아웃 경보·이벤트 무손실).
- **[account-pool-automation-plan.md](design-notes/account-pool-automation-plan.md)**: 계정 풀
  자동 배분(배급처·대여중·충전소) 기획. 상태 머신·스윕·단계별 강도의 배경. 현행 사양은
  AMX-DESIGN.md §5.8.
- **[account-pool-api.md](design-notes/account-pool-api.md)**: 계정 풀 REST 계약(개요·권고·
  체인·정책·상태 조작 엔드포인트).

## archive/ — 이력 스냅샷

완료된 실행 계획과 종료된 인수인계를 보존한다. 현행 작업 지침이 아니라 "그때 이렇게
정했다"는 기록이라, 새 작업은 BACKLOG.md·AMX-DESIGN.md에서 출발한다.

- **[archive/HANDOFF-langfuse-monitoring.md](archive/HANDOFF-langfuse-monitoring.md)** — Langfuse
  모니터링 트랙 인수인계(2026-08-15). 관측 기능이 어떤 전제로 넘어왔는지 되짚을 때 본다.
- **[archive/todo/](archive/todo/)** — 2026-08-09 확정한 잔여 작업 실행 계획의 스냅샷.
  [README](archive/todo/README.md)가 우선순위 ①~④와 완료 판정을 담고, 딸린
  [01-deploy-critical](archive/todo/01-deploy-critical.md)·[02-billing-hardening](archive/todo/02-billing-hardening.md)·[03-ops-stabilization](archive/todo/03-ops-stabilization.md)·[04-low-cost-misc](archive/todo/04-low-cost-misc.md)가
  각 순위의 항목을, [on-hold-saas](archive/todo/on-hold-saas.md)가 보류한 상용 SaaS 준비 항목을
  적었다. 당시 실행 순서의 SSOT였고 지금은 이력이다.

## 그 밖에

- **[presentation/amx-intro.html](presentation/amx-intro.html)** — 프로젝트 소개용 발표 자료(정적
  HTML). 외부에 개요를 보여줄 때 쓴다.
- **[presentation/amx-process.html](presentation/amx-process.html)** — 프로세스별 소개 발표 자료(정적
  HTML, 오프라인 단일 파일). 구성요소·주요 서비스 5선·장점·도입 메리트·Codex 제약을 14장 컨설팅
  덱으로 담는다. 발표자가 직접 발표하는 10분 브리핑용이다.
- **[presentation/amx-tokenomics.html](presentation/amx-tokenomics.html)** — 토큰노믹스 관점의 소개
  발표 자료(정적 HTML, 오프라인 단일 파일, 16장). 구독 한도가 5시간·7일 롤링 윈도우이고 미사용
  용량이 소멸한다는 착안에서 출발해, 실측·라우팅·귀속이라는 설계 결론과 각 서비스를 잇는다.
  `amx-process.html`의 후속 버전이며 최근 강화(자격증명 무효화 방어 체인·경보 14종)까지 반영한다.
- **cswapGitRepo.txt** — tsamx 원본(claude-swap) 저장소 주소를 적어 둔 한 줄 메모.
