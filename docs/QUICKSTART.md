# AMX 빠른 시작 — 모듈별 기동 가이드

> 처음 온 사람이 "일단 켜보고 싶다"일 때 읽는 문서다. 각 명령은 저장소의 실제
> 스크립트·README와 대조해 작성했다(2026-08-16 기준). 개념 설명은 `OVERVIEW.md`,
> 운영 배포의 기준 절차는 `PROD-GUIDE.md`가 맡고, 여기서는 기동 명령과 그 앞뒤
> 확인 방법만 다룬다.

---

## 1. 무엇을 켜야 하나 — 구성요소와 포트

AMX는 네 개의 실행 단위로 이뤄진다. 중앙 쪽 세 개(DB·서버·웹)는 관리 PC 한 대에
같이 뜨고, 에이전트는 계정을 받아 쓸 서버마다 하나씩 뜬다.

| 실행 단위 | 언어 | 기본 포트 | 역할 |
|---|---|---|---|
| PostgreSQL (개발용 컨테이너) | — | 55432 | 중앙 데이터 저장 |
| ams-server REST | Python | 8080 | 관리 API. 웹 화면이 여기에 붙는다 |
| ams-server gRPC | Python | 50051 | 에이전트와의 보안 통신 채널 |
| ams-web | TypeScript | 3000 | 관리자 화면 (운영 빌드로만 뜬다) |
| ama-agent | Go | — | 각 작업 서버 상주. gRPC로 중앙에 접속 |
| tsamx | Python | — | 서버 안에서 계정을 갈아끼우는 CLI |

준비물: docker, uv, node/npm(웹), go(에이전트). 전부 PC에 있어야 하는 건 아니고,
중앙 트랙은 docker·uv·node, 에이전트 트랙은 go·uv만 있으면 된다.

---

## 2. 가장 빠른 길 — 한 명령으로 전부 켜기 (개발·시험)

저장소 루트에서:

```sh
bash deploy/fullstack-run.sh up all --insecure-grpc --lan
```

이 한 줄이 DB 컨테이너 → 마이그레이션 → REST → gRPC → 웹 빌드·기동을 순서대로
처리하고, 끝나면 db/server/gRPC/web 네 줄의 ✔ 판정을 찍어 준다. 첫 실행 때는
암호화 키·관리자 토큰 같은 비밀값을 자동 생성해 `.amx-dev/dev.env`(0600)에 넣는다.

플래그 두 개의 뜻:

- `--insecure-grpc` — gRPC를 평문으로 띄운다. 시험 전용이라 경고가 뜨며, 운영은
  TLS 인증서를 발급해 이 플래그 없이 띄운다(`PROD-GUIDE.md` §2~§3).
- `--lan` — 웹·REST를 모든 인터페이스에 바인딩한다. 브라우저나 에이전트가 다른
  기기라면 필수다. 빼먹으면 다른 기기에서 `ERR_CONNECTION_RESET`이 난다.

켠 다음 관리자 계정을 만들어 로그인한다:

```sh
bash deploy/fullstack-run.sh bootstrap-admin admin@example.com <비밀번호>
# → http://localhost:3000 (LAN이면 http://<PC IP>:3000) 에서 이 계정으로 로그인
```

`amx.local` 같은 예약 도메인은 422로 거부되니 `example.com`류 일반 도메인을 쓴다.

일상 조작은 전부 같은 스크립트다:

```sh
bash deploy/fullstack-run.sh status            # 상태 4줄 확인
bash deploy/fullstack-run.sh logs server       # 로그 추적 (db|server|web|all)
bash deploy/fullstack-run.sh restart web       # 일부만 재기동
bash deploy/fullstack-run.sh down server web   # DB는 살려둔 채 종료
bash deploy/fullstack-run.sh down all          # 전부 종료 + DB 컨테이너 삭제
```

`down all`을 해도 데이터 자체는 도커 네임드 볼륨 `amx-dev-pgdata`에 남아 다음
`up` 때 다시 붙는다. 진짜 초기화가 목적일 때만 볼륨까지 지운다:
`docker volume rm amx-dev-pgdata`. 비밀값을 포함한 완전 초기화는 `.amx-dev/`
디렉터리 삭제다.

---

## 3. 모듈별로 따로 켜기

풀스택 스크립트가 내부에서 하는 일을 모듈 단위로 직접 하고 싶을 때의 절차다.
디버깅하거나 일부만 새로 띄울 때 쓴다.

### 3-1. ams-server (중앙 서버)

REST와 gRPC 두 프로세스로 나뉘고, 둘은 데이터베이스로만 연결된다.

```sh
cd ams-server
uv venv && uv pip install -e ".[dev]"      # 처음 한 번

# 필수 환경변수 — 없으면 기동을 거부한다
export AMX_DATABASE_URL="postgresql+psycopg://amx:<pw>@127.0.0.1:55432/amx"
export AMX_ENCRYPTION_KEY="<Fernet 키>"     # 아래 한 줄로 생성
export AMX_ADMIN_TOKEN="<16자 이상 토큰>"
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

uv run alembic upgrade head                                          # 스키마 생성·갱신
uv run python -m uvicorn app.main:create_app --factory --port 8080   # REST
uv run python -m app.grpc.server                                     # gRPC (별도 터미널)
```

gRPC 프로세스에는 명령 서명용 `AMX_SIGNING_KEY`도 필요하다. 손으로 만들기 번거로운
값이라, 개발에서는 fullstack-run.sh가 생성해 둔 `.amx-dev/dev.env`를 source해서
쓰는 편이 편하다. 환경변수 전체 목록과 의미는 `ams-server/app/config.py`에 주석으로
정리돼 있다.

동작 확인: `curl http://127.0.0.1:8080/healthz` 가 응답하면 REST는 정상이다.

### 3-2. ams-web (관리자 화면)

```sh
cd ams-web
cp .env.example .env.local    # AMX_API_BASE, AMX_ADMIN_TOKEN, AMX_SESSION_SECRET 기입
npm install
npm run build && npm start    # http://localhost:3000 → /login
```

주의할 것이 하나 있다. `npm run dev`(개발 모드)는 동작하지 않는다. 운영 CSP가
Next 개발 서버가 요구하는 `unsafe-eval`을 막아 화면이 하얗게 죽는다. 항상 운영
빌드(`build` + `start`)로 띄운다. 웹이 뜬 상태에서 재빌드가 겹쳐 `.next`가 깨지면
`down web` 후 `up web`, 그래도 안 되면 `ams-web/.next`를 지우고 다시 띄운다.

브라우저는 같은 출처의 `/bff/*`만 호출하고, 관리자 토큰은 Next 서버 프로세스
환경변수에만 있다. 토큰이 브라우저로 새지 않는 구조 설명은 `ams-web/README.md`.

### 3-3. ama-agent (서버 상주 에이전트)

에이전트를 설치할 기기(노트북 등)에서 띄운다. 두 가지 길이 있다.

**원클릭 (권장).** 관리 PC에서 설치 명령을 생성한다:

```sh
bash deploy/agent-install-cmd.sh --server-name "이광희 노트북"
```

서버 행 생성, 등록 토큰 발급, IP·공개키 수집까지 자동으로 하고 에이전트 기기에
붙여넣을 완성 명령을 출력한다. 그 명령이 실행하는 것이 `deploy/agent-setup.sh
install`이며, 사전점검(go·uv) → tsamx 설치 → 에이전트 빌드·기동 → 성공 판정까지
한 번에 간다. 성공 판정은 관리자 화면 서버 메뉴에서 그 서버가 온라인으로 보이는 것.

**수동.** 저장소를 별도로 클론한 뒤(개발 트리와 섞지 않는다 — 예: `~/AMX-agent`):

```sh
bash deploy/agent-run.sh up \
  --ams <PC IP>:50051 \
  --token <웹에서 발급한 등록 토큰> \
  --pubkey <AMS 서명 공개키> \
  --insecure          # 시험용 평문. 운영은 --ca ./ca.crt (TLS)
```

`--pubkey` 값은 PC의 `.amx-dev/dev.env`에 있는 `AMX_AMS_PUBKEY`를 복사한다.
PC가 `--insecure-grpc`로 떠 있으면 에이전트도 `--insecure`, TLS면 `--ca`로 맞춘다.
짝이 어긋나면 접속이 안 된다. 일상 조작은 `agent-run.sh status | logs | down`.

에이전트는 내장 가짜 없이 항상 실제 tsamx CLI를 부르므로, 계정 하달까지
시험하려면 그 기기에 tsamx가 설치돼 있어야 한다(원클릭 설치는 이것도 해 준다).

### 3-4. tsamx (계정 전환 CLI)

```sh
uv tool install --editable /mnt/c/workspace/AMX/tsamx   # 개발 설치
# 운영 배포는 git 태그 핀 설치 — docs/TSAMX-GUIDE.md §2, §6

tsamx list        # 슬롯 목록
tsamx add         # 현재 로그인된 자격증명을 슬롯으로 캡처
tsamx 2           # 2번 계정으로 전환
tsamx auto on     # 한도 도달 시 자동 전환
```

같은 `~/.claude` 파일을 조작하는 원본 cswap과의 동시 사용은 금지다. AMX 프로필로
격리해 쓸 때는 `CLAUDE_CONFIG_DIR=~/.claude-amx tsamx list`처럼 설정 홈을 지정한다.

### 3-5. 러너(claude) 실행 — 직접 실행하지 않는다

계정을 받은 서버에서 Claude Code는 `claude`를 직접 부르지 않고 래퍼
`deploy/amx-claude`(겸용 PC에서는 `amx`)를 거친다. 계정 교체 중에 실행되면 요금이
엉뚱한 계정에 찍히는 것을 막는 장치다. 래퍼 설치와 우회 차단, Langfuse 사용량
훅은 `DEPLOYMENT-RUNNER.md`에 절차가 있다.

---

## 4. 시험 돌리기

```sh
# 중앙 서버 — docker 필요 (시험용 DB를 자동으로 띄웠다 지움)
cd ams-server && uv sync --extra dev && uv run pytest

# 에이전트
cd ama-agent && go test ./...

# 웹 화면 — Vitest (BFF 수명주기·토큰 격리 게이트 포함)
cd ams-web && npm install && npm run build && npm test

# tsamx — 약 1,900개, 병렬 기본
cd tsamx && uv run pytest -q

# 종합(e2e) — 저장소 루트에서. docker + go + uv 필요, P4 콘솔 시험은 node도
uv run --project ams-server pytest e2e/ -q
```

e2e는 DB·REST·gRPC·에이전트 3대·tsamx를 실제 프로세스로 전부 띄워 계정 10개
배정→하달→회수 한 바퀴를 검사한다. 새 기능을 병합할 때마다 돌리는 것이 규칙이다.
상세와 실패 진단은 `e2e/README.md`.

---

## 5. docs/ 문서 지도 — 정리판

25개 파일을 성격별로 묶으면 네 층이다. 위 층일수록 현행이고, 아래로 갈수록 이력이다.

### 시작·이해 (여기부터 읽는다)

| 문서 | 내용 |
|---|---|
| `OVERVIEW.md` | 전문용어 없이 프로젝트 전체를 그리는 안내서. 처음이거나 길을 잃었을 때 |
| `QUICKSTART.md` | 이 문서. 모듈별 기동 명령 모음 |

### 기준 사양 (충돌하면 항상 이쪽이 이긴다)

| 문서 | 내용 |
|---|---|
| `AMX-DESIGN.md` | 설계 원본이자 현행 동작의 최종 기준(SSOT). ERD·API·상태기계·보안 설계 전부 |

### 절차서 (현행 운영·개발 가이드)

| 문서 | 언제 읽나 |
|---|---|
| `PROD-GUIDE.md` | 실장비 운영의 기준 절차. 중앙 서버 설치부터 제거·문제해결 표까지 |
| `DEV-TEST-GUIDE.md` | 개발·시험이 운영과 다른 부분만 모은 차이분(평문 기동 등) |
| `DEPLOYMENT-RUNNER.md` | 러너 래퍼(amx-claude) 강제, Langfuse 훅, 겸용 PC 프로필 |
| `DEPLOYMENT-TLS.md` | gRPC TLS/mTLS 인증서 발급·배선·로테이션 심화 |
| `TSAMX-GUIDE.md` | tsamx 개조 내역·사용법·사설 저장소 설치 인증(§6) |
| `UPSTREAM-SYNC.md` | tsamx 원본(claude-swap) 업데이트를 개조판에 반영하는 절차 |

### 장부·이력 (현재 사양의 근거가 아니라 기록)

| 문서 | 내용 |
|---|---|
| `BACKLOG.md` | 이월·미해결 항목의 현행 원장. 번호(A1, G53 …)로 추적 |
| `design-notes/` | 각 단계 착수 전 설계 메모 9건(p2~p5, f1·f2, recovery, 계정 풀 기획·API 계약). as-designed 기록 |
| `archive/` | 완료된 실행 계획(todo ①~④)·종료된 인수인계. 스냅샷이라 갱신하지 않는다 |
| `presentation/amx-intro.html` | 소개용 발표 자료 |
| `cswapGitRepo.txt` | 원본 claude-swap 저장소 주소 한 줄 메모 |

읽는 순서로 정리하면: 처음이면 OVERVIEW → 이 문서로 켜 보고, 정확한 사양이
필요해지면 AMX-DESIGN, 배포할 때 PROD-GUIDE(시험이면 DEV-TEST-GUIDE 차이분),
"이 문제 알려진 건가" 싶으면 BACKLOG 순이다.

---

## 6. 자주 걸리는 것들

| 증상 | 원인·조치 |
|---|---|
| 다른 기기에서 웹 접속이 리셋됨 | `--lan` 없이 기동함. `restart` 시 `--lan` 포함 |
| 노트북 에이전트가 갑자기 안 붙음 | WSL2 내부 IP 변동 → portproxy 재설정 (`PROD-GUIDE.md` §4-3) |
| `connection refused` | portproxy 어긋남 또는 방화벽. PROD §10 표 참조 |
| `AMS public key not configured` | 에이전트 기동 시 `--pubkey` 누락 |
| TLS/평문 접속 실패 | PC(`--insecure-grpc`)와 에이전트(`--insecure`/`--ca`) 짝 불일치 |
| 관리자 생성 422 | 예약 도메인(amx.local 등). example.com류로 |
| 웹 화면이 하얗게 죽음 | `npm run dev`로 띄움 → 운영 빌드로 재기동 |

로그 위치: 중앙 트랙은 `.amx-dev/logs/`, 에이전트는 `.amx-agent/logs/`. 더 깊은
문제 해결은 `PROD-GUIDE.md` §10의 표가 기준이다.
