# 개발·시험 안내서 (사람용)

AMX를 직접 켜서 시험하는 방법을 **서버(PC) 트랙**과 **에이전트(노트북) 트랙**으로 나눠 안내합니다.
각 단계는 "실행할 것 → 성공 판정 → 안 되면"의 같은 꼴로 되어 있어 위에서부터 순서대로 따라가면 됩니다.

```
┌─ PC (서버 트랙) ──────────────┐        ┌─ 노트북 (에이전트 트랙) ─────┐
│ ams-server  REST :8080        │  LAN   │ ama-agent ──→ PC:50051      │
│             gRPC :50051       │◀──────▶│ tsamx (계정 넣고 빼기)       │
│ ams-web     관리자 화면 :3000 │        │ claude (실제 사용)           │
└───────────────────────────────┘        └─────────────────────────────┘
```

| 이름 | 한 줄 설명 |
|---|---|
| ams-server | 중앙 서버. 웹이 쓰는 REST(:8080)와 에이전트가 붙는 gRPC(:50051) |
| ams-web | 관리자 화면(:3000). 브라우저로 접속 |
| ama-agent | 노트북에 상주하며 중앙 서버에 붙는 프로그램 |
| tsamx | 에이전트가 실제로 조작하는 계정 전환 도구 |

---

# A트랙 — 서버(PC) 설치 시나리오

## A-1. 준비물

| 도구 | 확인 명령 | 비고 |
|---|---|---|
| Docker | `docker --version` | 시험용 DB 컨테이너 |
| uv | `uv --version` | 파이썬 실행기. 파이썬 3.11+는 uv가 알아서 준비 |
| Node.js | `node --version` | 20 이상. 웹 빌드·구동 |

저장소를 `git clone` 해 둡니다.

## A-2. 풀스택 켜기

```sh
bash deploy/fullstack-run.sh up all --insecure-grpc --lan
```

- 첫 실행 때 비밀값(암호화 키·관리자 토큰 등)을 자동 생성해 `.amx-dev/dev.env`(0600)에 보관합니다.
- `--insecure-grpc` = gRPC 평문(첫 시험 전용, 경고 뜸). TLS는 부록 참고.
- `--lan` = 웹·REST를 모든 인터페이스에 바인딩. **브라우저나 노트북이 다른 기기라면 필수**입니다.
  (빼먹으면 다른 기기에서 `ERR_CONNECTION_RESET`이 납니다 — 실측.)

**성공 판정** — 네 줄 모두 `✔` (기동 직후 예열을 기다렸다가 출력해 줍니다):

```
✔ db        pg_isready (:55432)
✔ server    REST /healthz (:8080)
✔ server    gRPC 리슨 (:50051)
✔ web       /login 200 (:3000)
```

**안 되면**: `bash deploy/fullstack-run.sh logs server|web|db`

## A-3. (WSL2인 경우만) Windows portproxy

PC가 WSL2면 노트북은 WSL 내부 IP로 직접 못 들어옵니다. **관리자 PowerShell**에서 1회:

```powershell
# <WSL-IP>는 WSL 셸에서 hostname -I 로 확인
netsh interface portproxy add v4tov4 listenport=50051 listenaddress=0.0.0.0 connectport=50051 connectaddress=<WSL-IP>
netsh advfirewall firewall add rule name="AMX gRPC" dir=in action=allow protocol=TCP localport=50051
```

웹(3000)도 노트북에서 열려면 같은 방식으로 추가합니다.

> ⚠ **WSL 내부 IP는 재부팅하면 바뀝니다.** 노트북 연결이 갑자기 안 되면 이것부터 의심하세요.
> B트랙의 `agent-install-cmd.sh`가 매번 자동 점검해 틀어져 있으면 고치는 명령을 알려 줍니다.

## A-4. 관리자 만들기 + 로그인

```sh
bash deploy/fullstack-run.sh bootstrap-admin admin@example.com 'DevPass123!'
```

- **성공 판정**: `✔ 관리자 생성 완료`.
- 이메일 도메인 규칙이 엄격합니다 — `amx.local` 같은 예약 도메인은 422로 거부되니 `example.com`류를 쓰세요.

브라우저에서 `http://127.0.0.1:3000/login` (다른 기기는 `http://<PC IP>:3000/login`) 접속 → 로그인.

## A-5. 화면 기본 동작 확인 (L1)

관리자 화면은 좌측 사이드바(대시보드·서버·계정·할당·알림)로 이동합니다.

1. **테넌트** — 사이드바의 `새 테넌트` → 이름 입력 → 생성. 상단 선택 상자에 뜨면 성공.
2. **서버 등록** — `서버` 메뉴 → `서버 등록` → 이름 입력 → 생성. 행이 생기면 성공
   (에이전트가 아직 없으니 "오프라인"이 정상). ※ B트랙의 원클릭 스크립트를 쓰면 이 단계는 자동입니다.
3. **패널 확인** — 계정/할당/알림 메뉴가 오류 없이 열리면 성공.

---

# B트랙 — 에이전트(노트북) 설치 시나리오

## B-1. 준비물 (노트북)

| 도구 | 확인 명령 | 비고 |
|---|---|---|
| Go | `go version` | 1.24+. 에이전트 빌드 |
| uv | `uv --version` | tsamx 설치에 사용 (없으면 설치 스크립트가 안내) |

저장소를 `git clone` 해 둡니다 (예: `~/AMX`).

## B-2. 원클릭 설치 (권장)

**① PC에서** 설치 명령 생성 — 서버 행 생성·토큰 발급·IP/공개키 수집·portproxy 점검을 전부 자동으로 하고, 붙여넣을 명령을 완성해 줍니다:

```sh
bash deploy/agent-install-cmd.sh --server-name "이광희 노트북"
```

**② 노트북에서** ①이 출력한 명령을 그대로 붙여넣기 (형태 예시):

```sh
cd ~/AMX && git pull && bash deploy/agent-setup.sh install \
  --ams 10.60.1.15:50051 \
  --token <자동 발급됨> \
  --pubkey <자동 채워짐> \
  --insecure
```

`agent-setup.sh install`이 하는 일: 사전점검(go·uv) → tsamx 설치(없으면) → 에이전트 빌드·기동
(tsamx 경로·Claude 설정 홈 `~/.claude-amx` 자동 연결) → 성공 판정 출력.

**성공 판정**: 스크립트 끝의 판정 + 관리자 화면 `서버` 메뉴에서 해당 서버가 **온라인**. (이게 L2 통과입니다.)

**안 되면** (노트북 `deploy/agent-run.sh logs` 기준 대표 증상):

| 로그 증상 | 원인·조치 |
|---|---|
| `connection refused`/타임아웃 | PC 방화벽(50051) 또는 **WSL portproxy 어긋남**(A-3) — PC에서 `agent-install-cmd.sh` 재실행하면 점검해 줌 |
| `AMS public key not configured` | `--pubkey` 누락 — 생성기 출력 명령을 그대로 쓰면 발생하지 않음 |
| TLS/인증서 오류 | 평문·TLS 짝 불일치 — 양쪽 모드 확인(부록 TLS) |
| 등록 거부 | 토큰 만료/재발급됨 — PC에서 생성기 재실행(새 토큰 발급) |

## B-3. 제거

```sh
bash deploy/agent-setup.sh uninstall                # 에이전트만 (tsamx·계정 보존)
bash deploy/agent-setup.sh uninstall --purge-tsamx --purge-config   # 전부 (계정 자격증명까지!)
```

`--purge-config`는 계정 자격증명이 지워지므로 `yes` 입력 확인을 요구합니다. `--yes`로 생략 가능.

---

# C — 실계정 왕복 시나리오 (L3: 등록 → 할당 → 전달 → 확인 → 회수)

A·B트랙이 끝난 상태(서버 온라인)에서, 실제 Claude 계정을 한 바퀴 돌립니다.

1. **계정 등록** — `계정` 메뉴 → `OAuth 계정 등록` → `인증 페이지 열기 ↗` → 브라우저에서 Claude
   로그인·**승인** → 표시되는 `코드#상태` 값을 복사해 **인증 코드** 칸에 붙여넣기 → `등록 완료`.
   - **성공 판정**: 계정 목록에 이메일이 뜨고 상태가 `사용 가능`.
   - 개인(Pro/Max)·조직 계정 모두 동일하게 됩니다.
2. **할당 생성** — `할당` 메뉴 → `계정 할당` → 계정·서버 선택 → 생성. **대기 상태로 만들어지는 것이 정상**입니다.
3. **전달** — 할당 행의 `전달` 버튼 → 상태가 `전달 중` → 몇 초 안에 **`활성`**(에이전트가 수신·적용 확인).
   - **성공 판정(노트북)**: `tsamx list` 에 그 계정이 보임.
   - **성공 판정(화면)**: 할당 행이 "이메일 → 서버명" 파이프라인으로 `활성` 표시.
4. **사용 확인(선택)** — 노트북에서 `CLAUDE_CONFIG_DIR=~/.claude-amx claude` 실행 → 로그인 요구 없이 동작하면 끝까지 통한 것.
5. **회수** — 할당 행의 `회수` → 에이전트가 계정을 뺌.
   - **성공 판정**: `tsamx list` 에서 사라지고 화면 상태가 해제됨.

**안 되면**: 노트북 `agent-run.sh logs`에 `tsamx ...` 오류가 보이면 tsamx 설치/설정 홈 문제
(원클릭 설치를 썼다면 자동 구성됨). 전달이 `전달 중`에서 멈추면 서버가 온라인인지 먼저 확인.

---

# C-2 — 에이전트 자기 업데이트 (self-update)

C트랙이 끝난 상태에서, 원격으로 에이전트를 최신 커밋으로 올리는 경로를 확인합니다.
에이전트는 **자기 노트북의 클론**(`~/AMX`)만 당겨서 자기를 다시 빌드합니다. 명령에는
저장소 주소도 브랜치도 실리지 않으니, PC에서 코드를 밀어 넣는 게 아니라 노트북이 스스로
`git pull`을 하는 것으로 이해하면 됩니다.

호출은 REST입니다(화면 버튼은 별도 트랙). `<TEN>`·`<SRV>`는 테넌트·서버 UUID:

```sh
curl -X POST -H "Authorization: Bearer $AMX_ADMIN_TOKEN" \
  http://localhost:8080/api/v1/tenants/<TEN>/servers/<SRV>:self-update    # 202
```

**시험 1 — 정상 왕복.** 노트북 `~/AMX`가 최신보다 뒤처진 상태를 만들고(`git -C ~/AMX reset
--hard HEAD~1` 후 `deploy/agent-run.sh up`) 위 명령을 부릅니다.

- **성공 판정**: 30초~2분 뒤 `agent-run.sh logs`에 재기동 흔적이 남고, 화면의 서버 상세
  `agent_version`이 `p3+<새 커밋 앞 12자>`로 바뀝니다. **버전 문자열이 바뀌는 것이 유일한
  성공 판정입니다** — ack(CONVERGED)은 "바이너리를 교체하고 재기동을 요청했다"까지만 뜻하고,
  새 버전이 실제로 떴다는 보장이 아닙니다.
- 빌드가 도는 동안 계정 전달·스위칭은 평소대로 동작해야 합니다(교체 직전까지 락을 잡지 않음).

**시험 2 — 핀 불일치로 거부.** 있지도 않은 커밋을 못으로 박아 보냅니다:

```sh
curl -X POST -H "Authorization: Bearer $AMX_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expectedCommit":"aaaaaaa"}' \
  http://localhost:8080/api/v1/tenants/<TEN>/servers/<SRV>:self-update
```

- **성공 판정**: 화면 `알림`에 `self_update_failed`(사유 `commit_mismatch`)가 뜨고, 노트북에서
  `git -C ~/AMX rev-parse HEAD`가 **호출 전과 같습니다**. 핀 대조는 pull 앞에서 하므로 거부된
  요청은 작업 트리를 건드리지 않습니다. 에이전트는 계속 온라인이어야 합니다.
- 같은 서버에 self-update가 이미 queued/sent면 두 번째 호출은 409
  `self_update_already_pending`으로 막힙니다(연타 방지). 앞 건이 ack되거나 실패해야 다시 됩니다.

**시험 3 — `ama.bak` 수동 복구.** 새 바이너리가 떠서 죽는 상황을 흉내 냅니다. 노트북에서:

```sh
bash deploy/agent-run.sh down
cp ~/AMX/.amx-agent/ama.bak ~/AMX/.amx-agent/ama   # 직전 바이너리로 되돌리기
bash deploy/agent-run.sh up
```

교체 직전 바이너리는 항상 `ama.bak`에 남습니다. 저장소까지 되돌려야 하면
`git -C ~/AMX reset --hard origin/main && bash deploy/agent-run.sh up`.

- **성공 판정**: 서버가 다시 온라인이 되고 `agent_version`이 되돌린 커밋을 가리킵니다.

> ⚠ 새 커밋이 지금 돌고 있는 AMS보다 앞설 수 있습니다(핀 없이 보내면 upstream tip으로 갑니다).
> PC 서버를 먼저 올리거나 `expectedCommit`으로 못을 박으세요. 그리고 플릿에 걸 때는 **1대 먼저
> 걸어 `agent_version`을 확인한 뒤** 나머지에 겁니다.

---

# D — 끄기·초기화

**PC:**

```sh
bash deploy/fullstack-run.sh down all
```

> ⚠ **`down all`은 DB 컨테이너를 삭제합니다 = 테넌트·계정·할당 데이터가 전부 사라집니다** (실측).
> 데이터를 유지한 채 껐다 켜려면 **`down server web`처럼 부분 종료**를 쓰거나, 그냥 `restart`를 쓰세요.
> 초기화가 목적일 때만 `down all`을 쓰고, 이후 A-4(관리자)부터 다시 만듭니다.
> 비밀값 `.amx-dev/dev.env`는 남습니다. 완전 초기화는 `.amx-dev/` 삭제.

**노트북:** `bash deploy/agent-setup.sh uninstall` (B-3 참고)

---

# 부록

## 자주 쓰는 명령

| 하고 싶은 것 | 어디서 | 명령 |
|---|---|---|
| 전부 켜기(평문+LAN) | PC | `deploy/fullstack-run.sh up all --insecure-grpc --lan` |
| 상태/로그 | PC | `deploy/fullstack-run.sh status` / `logs server\|web\|db` |
| 관리자 만들기 | PC | `deploy/fullstack-run.sh bootstrap-admin <email> <pw>` |
| **에이전트 설치 명령 생성** | PC | `deploy/agent-install-cmd.sh --server-name <이름>` |
| **에이전트 설치/제거/상태** | 노트북 | `deploy/agent-setup.sh install …` / `uninstall` / `status` |
| 에이전트 로그 | 노트북 | `deploy/agent-run.sh logs` |
| 서버만 재시작 | PC | `deploy/fullstack-run.sh restart server --insecure-grpc --lan` |

## 수동 에이전트 설치 (원클릭을 못 쓸 때)

1. 화면에서: `서버` 메뉴 → `서버 등록` → 그 행의 `등록 토큰` 버튼 → 토큰 복사(한 번만 표시).
2. PC의 `.amx-dev/dev.env`에서 `AMX_AMS_PUBKEY=` 값 복사.
3. 노트북에서 tsamx 설치: `uv tool install --editable ~/AMX/tsamx` (상세: `docs/TSAMX-GUIDE.md`)
4. 노트북에서:

```sh
bash deploy/agent-run.sh up \
  --ams <PC IP>:50051 --token <토큰> --pubkey <값> --insecure \
  --config-dir "$HOME/.claude-amx"
```

## TLS 경로 (권장, 시험 후 전환)

1. **PC에서 인증서 발급** (LAN IP를 SAN에 포함):

```sh
cd deploy/tls
bash make-ca.sh --out ./ca
bash issue-cert.sh --cn ams --ip <PC IP> --ca-cert ca/ca.crt --ca-key ca/ca.key --out ./srv
```

2. **PC를 TLS로 기동** — dev.env에 경로를 넣고 `--insecure-grpc` 없이:

```sh
printf '\nAMX_GRPC_TLS_CERT=%s\nAMX_GRPC_TLS_KEY=%s\n' \
  "$PWD/deploy/tls/srv/server.crt" "$PWD/deploy/tls/srv/server.key" >> .amx-dev/dev.env
bash deploy/fullstack-run.sh restart server --lan
```

(둘 다 없으면 "보안 미설정"으로 기동을 거부합니다 — 실수로 평문이 새지 않도록.)

3. **노트북**: `ca/ca.crt`를 복사해 온 뒤, 설치 명령에서 `--insecure` 대신 `--ca ./ca.crt`.
   PC 생성기도 `deploy/agent-install-cmd.sh --tls`로 실행하면 TLS용 명령을 출력합니다.

## 알려진 문제·이력

- **OAuth "Invalid request format"**: 승인 클릭 시점에 나던 오류. 원인은 authorize `state`가 16바이트였던 것
  (claude.ai는 32바이트 요구). 2026-08-10 수정 완료 — 재발하면 `claude` 바이너리 상수와
  `ams-server/app/services/oauth_enroll.py`를 대조.
- **할당 즉시 전달**: P1에서는 지원되지 않아 화면에서 제거됨. 생성(대기) 후 `전달` 버튼을 쓰는 것이 정상 절차.
- **웹 빌드 충돌**: 웹이 떠 있는 상태에서 재빌드가 겹치면 `.next`가 깨질 수 있음 —
  `down web` 후 `up web`, 그래도 안 되면 `ams-web/.next` 삭제 후 재기동.
