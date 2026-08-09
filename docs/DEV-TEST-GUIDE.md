# 개발·시험 안내서 (사람용)

이 문서는 AMX를 **직접 켜서 손으로 시험**하는 방법을 쉬운 말로 안내합니다.
목표는 두 가지 상황을 반복해서 시험하는 것입니다.

- **(a) PC 한 대**에서 웹 화면과 서버를 띄워 화면·기능을 확인
- **(b) 노트북 한 대를 "서버 역할"로** 붙여 실제로 에이전트가 연결되고 사용량을 보고하는지 확인

용어 최소 설명 (자세한 그림은 `docs/OVERVIEW.md`):

| 이름 | 한 줄 설명 |
|---|---|
| ams-server | 중앙 서버. 웹이 쓰는 REST(:8080)와 에이전트가 붙는 gRPC(:50051) 두 개를 띄웁니다 |
| ams-web | 관리자 화면(:3000). 브라우저로 접속 |
| ama-agent | "서버 역할"을 하는 컴퓨터(노트북)에 상주하는 프로그램. 중앙 서버에 붙습니다 |
| tsamx | 에이전트가 실제로 조작하는 계정 전환 도구(노트북에 설치 필요) |

---

## 0. 준비물

**PC(중앙 서버·웹을 띄우는 쪽)** 에 필요한 것:

| 도구 | 확인 명령 | 비고 |
|---|---|---|
| Docker | `docker --version` | 시험용 DB를 컨테이너로 자동으로 띄웠다 지웁니다 |
| uv | `uv --version` | 파이썬 실행기(0.11 이상에서 확인). 파이썬 3.11+는 uv가 알아서 준비 |
| Node.js | `node --version` | 20 이상(22에서 확인). 웹 빌드/구동용 |

**노트북(서버 역할, 에이전트를 띄우는 쪽)** 에 필요한 것:

| 도구 | 확인 명령 | 비고 |
|---|---|---|
| Go | `go version` | 1.24 이상. 에이전트를 빌드합니다 |
| tsamx | `tsamx --version` | 계정 하달(L3)까지 시험하려면 필요. 설치법은 `docs/TSAMX-GUIDE.md` |

> 두 컴퓨터 모두 이 저장소를 `git clone` 해 두어야 합니다. 같은 PC 한 대에서 (a)만
> 먼저 해 보고, 노트북은 나중에 붙여도 됩니다.

---

## 1. PC에서 풀스택 켜기

저장소 루트에서:

```sh
bash deploy/fullstack-run.sh up all --insecure-grpc
```

- 처음 실행하면 필요한 비밀값(암호화 키·관리자 토큰 등)을 **자동으로 만들어** 저장소 루트의
  `.amx-dev/dev.env`(권한 0600)에 보관합니다. 이 파일은 `.gitignore`로 제외되니 커밋되지 않습니다.
- `--insecure-grpc`는 "gRPC를 암호화 없이(TLS 없이) 띄운다"는 뜻입니다. **첫 시험 전용**이며,
  실행할 때 경고가 뜹니다. TLS로 하는 방법은 아래 L2에 있습니다.
- 웹은 항상 **운영 빌드**로 뜹니다(`build` 후 `start`). 개발 모드(`next dev`)는 화면이 죽어서 쓰지 않습니다.

끝나면 상태표가 뜹니다. **성공 판정** — 네 줄 모두 초록 `✔` 여야 합니다:

```
✔ db        pg_isready (:55432)
✔ server    REST /healthz (:8080)
✔ server    gRPC 리슨 (:50051)
✔ web       /login 200 (:3000)
```

> 웹은 켜진 직후 몇 초간 준비 중이라 `/login=000`으로 보일 수 있습니다. 잠깐 뒤
> `bash deploy/fullstack-run.sh status`를 다시 실행하면 `200`이 됩니다.

**안 될 때 볼 로그**

```sh
bash deploy/fullstack-run.sh logs server   # REST·gRPC 로그
bash deploy/fullstack-run.sh logs web       # 웹 로그
bash deploy/fullstack-run.sh logs db        # DB 컨테이너 로그
```

부분만 켜고 끌 수도 있습니다: `up db` / `up server` / `up web`, `restart server` 등.

---

## 2. 관리자 만들기 (첫 로그인 계정)

로그인은 **이메일+비밀번호**입니다. 첫 관리자는 명령으로 만듭니다:

```sh
bash deploy/fullstack-run.sh bootstrap-admin admin@example.com 'DevPass123!'
```

- **성공 판정**: `✔ 관리자 생성 완료` 가 뜹니다.
- **이메일 도메인 규칙이 엄격합니다.** `admin@amx.local` 같은 예약/특수 도메인은 **거부(422)**
  됩니다. `example.com` 처럼 일반 도메인을 쓰세요. (거부되면 스크립트가 그 이유를 알려 줍니다.)

---

## 3. 브라우저로 접속

`http://127.0.0.1:3000/login` 에 접속해 위에서 만든 이메일/비밀번호로 로그인합니다.

---

# 테스트 시나리오

아래 L1 → L2 → L3 순서로 난이도가 올라갑니다. L1은 PC만으로, L2·L3은 노트북이 필요합니다.

## L1 — PC만으로 화면·기능 확인

로그인한 상태에서 대시보드를 다음 순서로 눌러 봅니다.

체크리스트:

1. **테넌트 만들기** — 왼쪽 위 `+ Tenant` → 이름 입력 → 생성. 목록에 생기고 선택되면 성공.
2. **서버 등록** — 서버 패널의 `+ Register server` → 이름/호스트명/전환 모드 입력 → 생성.
   목록에 새 서버 행이 보이면 성공. (아직 "오프라인/미등록" 상태가 정상입니다 — 에이전트가 없으니까요.)
3. **등록 토큰 발급** — 그 서버 행의 `Enroll token` 버튼 → **토큰이 한 번만** 화면에 표시됩니다.
   L2에서 쓸 것이니 복사해 둡니다. (창을 닫으면 다시 못 봅니다. 다시 누르면 새 토큰이 나오고 이전 것은 무효가 됩니다.)
4. **패널 둘러보기** — Accounts / Assignments / Alerts 패널이 오류 없이 열리면 성공.

**성공 판정 기준**: 위 네 가지가 화면 오류 없이 모두 동작하고, 서버 행과 등록 토큰이 만들어짐.

**안 될 때 볼 로그**: 화면이 이상하면 브라우저 개발자도구 콘솔 + `logs web`. 저장(생성) 버튼이
먹통이면 대개 REST 문제이니 `logs server`.

---

## L2 — 노트북 에이전트 연결

노트북의 ama-agent가 PC의 gRPC(:50051)에 붙어 "서버 온라인"이 되고 사용량을 보고하는지 봅니다.
**평문(빠른 확인) 경로**와 **TLS(권장) 경로** 둘 다 안내합니다.

### 2-0. PC를 LAN에 노출

PC에서 `--lan`을 붙여(재)기동하면 REST·웹이 모든 인터페이스에 열리고 LAN 주소가 출력됩니다.
(gRPC는 소스상 항상 모든 인터페이스에 열려 있어 별도 설정이 필요 없습니다.)

```sh
bash deploy/fullstack-run.sh restart all --insecure-grpc --lan
```

출력 예:

```
LAN 접속 주소 (노트북 에이전트용):
  gRPC : 192.168.0.10:50051   → 노트북 AMX_AMS_ADDR
  web  : http://192.168.0.10:3000
```

- **방화벽**에서 위 포트(gRPC 50051, 필요 시 웹 3000)를 **인바운드 허용** 하세요.
  - Windows: "고급 보안이 포함된 Windows Defender 방화벽" → 인바운드 규칙 → 새 규칙(포트 50051 TCP 허용).
  - Linux(ufw): `sudo ufw allow 50051/tcp`
- PC의 gRPC 서명 공개키를 노트북이 알아야 합니다. PC의 `.amx-dev/dev.env` 에서 **`AMX_AMS_PUBKEY=`**
  줄의 값을 복사해 두세요(노트북에서 `--pubkey`로 넘깁니다).

### 2-A. 평문 경로 (첫 확인용, 빠름)

노트북(저장소 clone 됨)에서:

```sh
bash deploy/agent-run.sh up \
  --ams 192.168.0.10:50051 \
  --token <L1에서 복사한 등록 토큰> \
  --pubkey <PC dev.env의 AMX_AMS_PUBKEY 값> \
  --insecure
```

> `--insecure`는 PC도 `--insecure-grpc`로 떠 있을 때만 맞습니다(양쪽이 같아야 함).

### 2-B. TLS 경로 (권장)

1. **PC에서 인증서 발급** (`deploy/tls/` 스크립트, LAN IP를 SAN에 포함):

   ```sh
   cd deploy/tls
   bash make-ca.sh --out ./ca                         # ca/ca.crt, ca/ca.key 생성
   bash issue-cert.sh --cn ams --ip 192.168.0.10 \
        --ca-cert ca/ca.crt --ca-key ca/ca.key --out ./srv   # srv/server.crt, srv/server.key
   ```

2. **PC를 TLS로 기동** — `dev.env`에 인증서 경로를 넣고 `--insecure-grpc` 없이 띄웁니다:

   ```sh
   printf '\nAMX_GRPC_TLS_CERT=%s\nAMX_GRPC_TLS_KEY=%s\n' \
     "$PWD/deploy/tls/srv/server.crt" "$PWD/deploy/tls/srv/server.key" >> .amx-dev/dev.env
   bash deploy/fullstack-run.sh restart server --lan
   ```

   (`--insecure-grpc`를 빼면 스크립트가 dev.env의 인증서를 자동으로 씁니다. 둘 다 없으면
   "보안 미설정"이라며 기동을 거부합니다 — 실수로 평문이 새지 않도록.)

3. **노트북으로 CA를 복사**(`ca/ca.crt`)한 뒤 에이전트를 TLS로 붙입니다:

   ```sh
   bash deploy/agent-run.sh up \
     --ams 192.168.0.10:50051 \
     --token <등록 토큰> \
     --pubkey <AMX_AMS_PUBKEY 값> \
     --ca ./ca.crt
   ```

### L2 성공 판정 기준

- 노트북: `bash deploy/agent-run.sh status` 가 **실행 중**이고 최근 로그에 연결 오류가 없음.
- 웹(대시보드 → 서버 행): 그 서버가 **online** 으로 바뀜. **이것이 L2의 핵심 판정**입니다.
- **사용량 값**은 노트북에 tsamx가 설치되고 계정이 들어와 있어야 채워집니다(즉 L3까지 해야 의미 있는 숫자가 나옵니다).
  tsamx가 없으면 서버는 online이 되지만 사용량은 비어 있고, 로그에 `tsamx ...` 오류가 남습니다 — L2에서는 정상입니다.
  (tsamx가 있는 상태라면 서버 행의 `Refresh usage`(사용량 새로고침)로 보고를 강제할 수 있습니다. 자동 보고 주기는 약 5분.)

### L2 안 될 때 볼 로그

- 노트북: `bash deploy/agent-run.sh logs` — 대표 증상:
  - `AMS public key not configured` → `--pubkey`/`--pubkey-file` 빠짐.
  - `connection refused` / 타임아웃 → 방화벽 미개방 또는 `--ams` 주소 오타.
  - `no TLS configured` / 인증서 검증 실패 → 평문/ TLS 짝이 안 맞음(양쪽 모드·CA·SAN 확인).
  - 등록 거부 → 토큰 만료/재발급됨. 웹에서 `Enroll token`을 다시 눌러 새로 발급.
- PC: `bash deploy/fullstack-run.sh logs server` (gRPC 쪽 등록·인증 메시지).

---

## L3 — 실계정 왕복 (등록 → 배정 → 하달 → 확인 → 회수)

실제 계정을 한 바퀴 돌려 봅니다. **노트북에 tsamx가 설치되어 있어야 하며**(배포 에이전트는 항상
실제 tsamx를 호출합니다 — 시험용 가짜 모드는 배포 바이너리에 없습니다), 노트북에서 에이전트를
띄울 때 tsamx가 쓰는 Claude 설정 홈을 알려 줘야 합니다:

```sh
bash deploy/agent-run.sh up \
  --ams 192.168.0.10:50051 --token <토큰> --pubkey <값> --ca ./ca.crt \
  --config-dir "$HOME/.claude-amx"      # tsamx가 계정을 넣고 뺄 홈. --tsamx-bin 으로 경로 지정도 가능
```

절차와 성공 판정:

1. **계정 등록** (웹 Accounts 패널의 OAuth 등록 플로우) → 계정이 목록에 뜨면 성공.
2. **배정 생성** (Assignments 패널) — 그 계정을 위 노트북 서버에 배정 → 배정 행이 생기면 성공.
3. **하달 확인** — 중앙이 명령을 내려 에이전트가 tsamx에 계정을 넣습니다.
   - **성공 판정**: 노트북에서 `tsamx list` 에 해당 계정이 보임(활성 상태). 웹의 배정 상태가
     "적용됨/수렴"으로 바뀜.
4. **회수** — 웹에서 배정을 제거/회수 → 에이전트가 tsamx에서 계정을 뺍니다.
   - **성공 판정**: `tsamx list` 에서 그 계정이 사라짐. 웹 배정 상태가 "회수됨/없음"으로 바뀜.

**안 될 때 볼 로그**

- 노트북 `agent-run.sh logs` 에 `tsamx ...` 오류가 보이면 tsamx 설치/`--config-dir` 확인.
- 하달이 계속 반복되면(계정이 붙었다 떨어졌다) 보고에 계정 식별자가 안 실린 경우일 수 있으니
  로그의 `usage report` / reconcile 관련 줄을 확인하고 PC의 `logs server` 와 대조.

---

## 4. 정리 (끄기)

**PC:**

```sh
bash deploy/fullstack-run.sh down all
```

- 웹·REST·gRPC 프로세스를 pidfile로 정확히 종료하고, DB 컨테이너를 지웁니다.
- **성공 판정**: `ss -ltn` 에 8080/50051/3000/55432 포트가 남지 않고, `docker ps -a` 에
  `amx-dev-pg` 컨테이너가 없습니다. 다시 `up` 하면 그대로 재현됩니다(멱등).
- 비밀값 파일 `.amx-dev/dev.env` 는 남습니다(다음 실행에 재사용). 완전 초기화하려면 `.amx-dev/` 폴더를 지우세요.

**노트북:**

```sh
bash deploy/agent-run.sh down
```

---

## 부록 — 자주 쓰는 명령 요약

| 하고 싶은 것 | 명령 |
|---|---|
| 전부 켜기(평문) | `deploy/fullstack-run.sh up all --insecure-grpc` |
| LAN 노출해서 켜기 | `deploy/fullstack-run.sh restart all --insecure-grpc --lan` |
| 상태 보기 | `deploy/fullstack-run.sh status` |
| 로그 보기 | `deploy/fullstack-run.sh logs server\|web\|db` |
| 관리자 만들기 | `deploy/fullstack-run.sh bootstrap-admin <email> <pw>` |
| 전부 끄기 | `deploy/fullstack-run.sh down all` |
| 노트북 에이전트 켜기 | `deploy/agent-run.sh up --ams IP:50051 --token T --pubkey K (--ca ca.crt \| --insecure)` |
| 에이전트 상태/로그/끄기 | `deploy/agent-run.sh status\|logs\|down` |
