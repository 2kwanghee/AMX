# AMX 운영(PROD) 가이드 — 서버·에이전트 설치부터 제거까지

> **누구를 위한 문서인가**: AMX를 실제 장비에 올려 운영하려는 사람.
> 명령을 그대로 복사해 실행할 수 있도록, 순서대로·빠짐없이 쓰는 것이 목표다.
> 개발·시험용 절차는 `docs/DEV-TEST-GUIDE.md`, TLS 심화는 `docs/DEPLOYMENT-TLS.md`,
> 러너(claude 실행) 보호는 `docs/DEPLOYMENT-RUNNER.md`가 원본이다. 이 문서는
> 그 내용을 "운영 순서" 하나로 엮은 것이며, 충돌 시 각 원본 문서가 우선한다.

---

## 0. 전체 그림 — 무엇이 어디에 설치되는가

AMX는 **두 종류의 장비**로 구성된다.

```
┌─ 중앙 서버 (1대) ─────────────────┐        ┌─ 에이전트 서버 (N대) ──────────────┐
│  db      PostgreSQL   :55432      │        │  ama      에이전트 (Go 바이너리)   │
│  server  REST API     :8080       │◀─gRPC──│  tsamx    계정 넣고 빼는 CLI       │
│  server  gRPC 제어면  :50051      │ :50051 │  claude   실제 작업하는 러너       │
│  web     관리자 화면  :3000       │        │  설정 홈  ~/.claude-amx            │
└───────────────────────────────────┘        └────────────────────────────────────┘
```

- **중앙 서버**: 관리자가 계정·서버·할당을 관리하는 곳. `deploy/fullstack-run.sh`로 설치.
- **에이전트 서버**: Claude 계정을 **받아서 쓰는** 곳. `deploy/agent-setup.sh`로 설치.
  에이전트(ama)가 중앙 서버의 gRPC(:50051)에 **밖→안 방향으로** 접속하므로,
  방화벽을 열어야 하는 쪽은 **중앙 서버뿐**이다. 에이전트 서버는 인바운드 개방이 필요 없다.

### 운영 모드에서 반드시 지킬 것 (요약)

| 항목 | 규칙 | 이유 |
|---|---|---|
| gRPC 전송 | **TLS 필수** (`--insecure`/`--insecure-grpc` 금지) | 이 채널로 계정 자격증명과 KEK가 지나간다 |
| `AMX_ALLOW_RAW_KEK` | **절대 설정 금지** | KEK 평문 노출 폴백. 기동 로그에 SECURITY 경고가 보이면 운영 구성이 아니다 |
| `down all` | **금지** (§7 참고) | DB 컨테이너까지 삭제되어 테넌트·계정·할당이 전부 사라진다 (실제 발생 이력 있음) |
| 방화벽 | 50051(과 필요 시 3000)만 개방 | 8080(REST)·55432(DB)는 외부에 열지 않는다 |
| 웹 | 운영 빌드로만 (`npm run dev` 금지) | CSP가 dev 모드의 unsafe-eval을 막아 화면이 동작하지 않는다 |
| claude 실행 | `CLAUDE_CONFIG_DIR` 지정 + `amx-claude` 래퍼 경유 | §8 참고 — 잘못 띄우면 엉뚱한 계정으로 과금될 수 있다 |

> ⚠ **알려진 한계(솔직하게)**: 현재 `fullstack-run.sh`가 띄우는 PostgreSQL 컨테이너에는
> **데이터 볼륨이 붙어 있지 않다.** 컨테이너가 삭제되면 데이터도 사라진다.
> 운영에서는 §7의 "끄는 법"을 반드시 지키고, 장기적으로는 별도 관리 DB를 쓰고
> `AMX_DATABASE_URL`만 바꿔 연결하는 방식을 권장한다 (`.amx-dev/dev.env`에서 수정).

---

## 1. 중앙 서버 설치 — 사전 준비

중앙 서버 호스트(리눅스 또는 Windows의 WSL2)에서:

| 도구 | 확인 명령 | 용도 |
|---|---|---|
| docker | `docker ps` | PostgreSQL 컨테이너 |
| uv | `uv --version` | 파이썬 서버(ams-server) 실행 |
| node + npm | `node -v` | 관리자 화면(ams-web) 빌드·실행 |
| git | `git --version` | 저장소 clone |

없으면:

```sh
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh   # 설치 후 셸 재시작
# docker / node 는 배포판 패키지 매니저 또는 공식 설치 문서를 따른다
```

저장소를 받는다:

```sh
git clone <저장소 주소> ~/AMX && cd ~/AMX
```

---

## 2. 중앙 서버 설치 — TLS 인증서 발급 (운영 필수)

**시험 단계가 아니라면 평문(insecure) 기동은 쓰지 않는다.** 서버를 켜기 전에
인증서부터 만든다. openssl만 있으면 되고 root 권한·외부 네트워크는 불필요하다.

```sh
cd ~/AMX

# ① 사설 CA 생성 (최초 1회. 이미 있으면 스크립트가 덮어쓰지 않고 실패한다 — 정상)
bash deploy/tls/make-ca.sh --cn amx-internal-ca --out deploy/tls/ca

# ② 서버 인증서 발급 — SAN에 "에이전트가 접속할 주소"를 반드시 넣는다.
#    IP로 접속하게 할 거면 --ip, 도메인이면 --dns. (SAN에 없는 주소로 접속하면 검증 실패)
bash deploy/tls/issue-cert.sh --cn ams \
  --ip <이 서버의 LAN IP> \
  --ca-cert deploy/tls/ca/ca.crt --ca-key deploy/tls/ca/ca.key \
  --out deploy/tls/srv
# → deploy/tls/srv/server.crt, server.key 생성

# ③ 발급물 검증 (핸드셰이크 성립 + fail-closed 확인)
bash deploy/tls/verify-tls.sh --ca deploy/tls/ca/ca.crt \
  --cert deploy/tls/srv/server.crt --key deploy/tls/srv/server.key
```

- `ca.key`(CA 개인키)는 **이 서버 밖으로 절대 내보내지 않는다.**
- 에이전트 서버에는 나중에 **`ca.crt` 하나만** 복사해 간다 (§5).
- Windows PC의 WSL이 중앙 서버라면 `--ip`에는 **Windows의 LAN IP**(에이전트가
  실제로 접속하는 주소)를 넣는다. WSL 내부 IP가 아니다.
- 인증서 갱신·CA 교체 절차는 `docs/DEPLOYMENT-TLS.md` §4 (요지: 새 cert 배치 후
  서버 재시작. CA 교체는 "구·신 CA 동시 신뢰 → 서버 cert 교체 → 구 CA 제거" 순서).

---

## 3. 중앙 서버 기동

### 3-1. 최초 기동

```sh
cd ~/AMX

# 최초 up 은 시크릿을 자동 생성해 .amx-dev/dev.env(0600)에 저장한다.
# TLS cert 경로를 dev.env에 먼저 넣기 위해, 한 번은 실패해도 괜찮다:
bash deploy/fullstack-run.sh up all --lan || true

# TLS 경로 배선 (dev.env에 추가)
printf '\nAMX_GRPC_TLS_CERT=%s\nAMX_GRPC_TLS_KEY=%s\n' \
  "$PWD/deploy/tls/srv/server.crt" "$PWD/deploy/tls/srv/server.key" >> .amx-dev/dev.env

# TLS로 재기동
bash deploy/fullstack-run.sh restart all --lan
```

- `--lan`: REST와 웹을 LAN에 노출하고 감지된 LAN IP를 출력한다.
  (gRPC는 플래그와 무관하게 항상 모든 인터페이스에 바인딩된다.)
- TLS cert가 없고 `--insecure-grpc`도 없으면 **기동을 거부**한다(fail-closed).
  이 거부가 뜬다면 §2를 건너뛴 것이다.
- 끝나면 상태 요약이 출력된다. 전부 `✔`이면 성공:

```
✔ db        pg_isready (:55432)
✔ server    REST /healthz (:8080)
✔ server    gRPC 리슨 (:50051)
✔ web       /login 200 (:3000)
```

**안 되면**: `bash deploy/fullstack-run.sh logs server|web|db` 로 원인 확인.

### 3-2. 관리자 계정 만들기 + 로그인

```sh
bash deploy/fullstack-run.sh bootstrap-admin admin@example.com '강한비밀번호'
```

- `amx.local` 같은 예약 도메인은 422로 거부된다 — 실제/일반 도메인을 쓴다.
- 브라우저에서 `http://<서버 IP>:3000/login` 접속 → 위 이메일/비밀번호로 로그인.
- 로그인 후 최소 준비: 사이드바 → **새 테넌트** 생성 (에이전트 설치 전 필수).

### 3-3. 일상 운영 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 확인 | `bash deploy/fullstack-run.sh status` |
| 로그 보기 | `bash deploy/fullstack-run.sh logs server` (또는 `web`/`db`/`all`) |
| 서버만 재시작 | `bash deploy/fullstack-run.sh restart server --lan` |
| 전체 재시작 | `bash deploy/fullstack-run.sh restart all --lan` |

재시작할 때도 dev.env에 TLS 경로가 있으므로 플래그는 `--lan`만 주면 된다.
**`--insecure-grpc`는 운영에서 다시 쓰지 않는다.**

---

## 4. 방화벽·네트워크 개방 (중앙 서버에서만)

### 4-1. 어떤 포트를 여는가

| 포트 | 용도 | 개방 여부 |
|---|---|---|
| **50051** | gRPC 제어면 — 에이전트가 접속 | **연다 (필수)** |
| **3000** | 관리자 화면 | 다른 기기에서 관리할 때만 연다 |
| 8080 | REST API | **열지 않는다** (웹이 내부에서 호출) |
| 55432 | PostgreSQL | **열지 않는다** ⚠ docker `-p`가 모든 인터페이스에 노출하므로 방화벽에서 반드시 막혀 있는지 확인 |

### 4-2. 리눅스 서버 (ufw)

```sh
sudo ufw allow 50051/tcp comment 'AMX gRPC'
sudo ufw allow 3000/tcp  comment 'AMX web'    # 원격 관리 시에만
sudo ufw status
```

### 4-3. Windows + WSL2 서버 — portproxy까지 (중요)

WSL2는 NAT 뒤에 있어서 **외부 장비가 WSL 내부 IP로 직접 못 들어온다.**
Windows가 대신 받아 WSL로 넘겨주는 portproxy가 필요하다.

**관리자 PowerShell**에서:

```powershell
# <WSL-IP>는 WSL 셸에서 `hostname -I` 로 확인 (예: 172.22.x.x)
netsh interface portproxy add v4tov4 listenport=50051 listenaddress=0.0.0.0 connectport=50051 connectaddress=<WSL-IP>
netsh advfirewall firewall add rule name="AMX gRPC" dir=in action=allow protocol=TCP localport=50051

# 웹(3000)을 다른 기기에서 열려면 같은 방식으로:
netsh interface portproxy add v4tov4 listenport=3000 listenaddress=0.0.0.0 connectport=3000 connectaddress=<WSL-IP>
netsh advfirewall firewall add rule name="AMX web" dir=in action=allow protocol=TCP localport=3000
```

에이전트에게 알려줄 접속 주소는 **Windows의 실제 LAN IP**다 (`ipconfig`에서
기본 게이트웨이가 있는 어댑터의 IPv4). 가상 어댑터 IP(192.168.56.x,
172.x WSL 대역 등)는 쓰지 않는다.

> ⚠ **WSL 내부 IP는 Windows 재부팅 시 바뀐다.** 그러면 portproxy가 옛 IP를
> 가리켜 에이전트 연결이 전부 끊긴다. **에이전트가 갑자기 오프라인이면 이것부터
> 의심**하라. 점검·수정:
>
> ```powershell
> netsh interface portproxy show v4tov4          # 현재 매핑 확인
> netsh interface portproxy delete v4tov4 listenport=50051 listenaddress=0.0.0.0
> netsh interface portproxy add v4tov4 listenport=50051 listenaddress=0.0.0.0 connectport=50051 connectaddress=<새 WSL-IP>
> ```
>
> §5의 `agent-install-cmd.sh`를 실행하면 이 어긋남을 자동으로 점검해 고칠 명령을 출력해 준다.

### 4-4. 방화벽 규칙 제거 (서버를 내릴 때)

```powershell
# Windows
netsh interface portproxy delete v4tov4 listenport=50051 listenaddress=0.0.0.0
netsh interface portproxy delete v4tov4 listenport=3000 listenaddress=0.0.0.0
netsh advfirewall firewall delete rule name="AMX gRPC"
netsh advfirewall firewall delete rule name="AMX web"
```

```sh
# 리눅스
sudo ufw delete allow 50051/tcp
sudo ufw delete allow 3000/tcp
```

---

## 5. 에이전트 설치 (계정을 받아 쓸 각 서버에서)

### 5-1. 사전 준비 (에이전트 서버)

| 도구 | 확인 명령 | 용도 |
|---|---|---|
| Go 1.24+ | `go version` | 에이전트(ama) 빌드 |
| uv | `uv --version` | tsamx 설치 |
| git | `git --version` | 저장소 clone |

```sh
git clone <저장소 주소> ~/AMX
```

**TLS 재료 복사**: 중앙 서버의 `deploy/tls/ca/ca.crt` **한 파일만** 이 서버로
복사한다 (예: `scp` 또는 USB → `~/AMX/ca.crt`). 개인키·서버 cert는 절대 가져오지 않는다.

### 5-2. 원클릭 설치 (권장)

**① 중앙 서버에서** 설치 명령을 생성한다. 테넌트 확인 → 서버 행 생성 →
등록 토큰 발급 → IP·공개키 수집 → (WSL이면) portproxy 점검까지 자동으로 하고,
붙여넣을 명령을 완성해 준다:

```sh
bash deploy/agent-install-cmd.sh --server-name "빌드서버-01" --tls
```

- `--server-name`: 관리자 화면에 표시될 이 서버의 이름. 같은 이름이 이미 있으면 그 행을 재사용한다.
- `--tls`: TLS용 명령을 출력한다. **운영에서는 항상 붙인다.**
- 출력된 토큰은 **이 출력에만 한 번 표시**되고 만료 시각이 있다. 만료 전에 쓰고,
  놓쳤으면 그냥 다시 실행한다 (새 토큰 발급).

**② 에이전트 서버에서** ①이 출력한 명령을 그대로 붙여넣는다. 형태 예시:

```sh
cd ~/AMX && git pull && bash deploy/agent-setup.sh install \
  --ams <중앙서버 IP>:50051 \
  --token <자동 발급된 토큰> \
  --pubkey <자동 채워진 서명 공개키> \
  --ca ./ca.crt
```

`install`이 하는 일: 사전점검(go·uv) → tsamx 설치·갱신(`uv tool install --force --reinstall`로
체크아웃 기준 재설치 — 이미 있어도 최신으로 맞춘다) → 에이전트 빌드·기동
(Claude 설정 홈 `~/.claude-amx` 자동 연결) → 성공 판정 출력.

> tsamx는 에이전트 설치·자기갱신(self_update)과 함께 갱신된다 — self_update가 ama
> 바이너리를 스왑한 뒤 체크아웃의 tsamx도 `uv tool install --force --reinstall`로 재설치하므로,
> 저장소의 tsamx 변경이 러너에 자동 반영된다(BACKLOG G48 해소).

**성공 판정**: 스크립트 끝의 판정 + 관리자 화면 `서버` 메뉴에서 이 서버가 **온라인**.

### 5-3. 설치 옵션 (필요할 때만)

```sh
bash deploy/agent-setup.sh install ... \
  --config-dir ~/.claude-amx   # tsamx가 계정을 넣고 뺄 Claude 설정 홈 (기본값)
  --agent-id build-01          # 에이전트 식별자 (기본 ama_dev — 다중 서버면 구분 권장)
  --tsamx-bin /path/to/tsamx   # tsamx 경로 직접 지정 (기본: 자동 탐지)
```

mTLS(전송 계층에서 에이전트 신원까지 검증 — 폐쇄망/규제 환경 옵션)는
`docs/DEPLOYMENT-TLS.md` §3.5-(4): 중앙 서버에서 `issue-cert.sh --client`로
클라이언트 cert를 발급하고 양쪽 환경변수를 배선한다.

### 5-4. 수동 설치 (원클릭 생성기를 못 쓸 때)

1. 관리자 화면: `서버` 메뉴 → `서버 등록` → 행의 `등록 토큰` 버튼 → 토큰 복사 (한 번만 표시).
2. 중앙 서버 `.amx-dev/dev.env`에서 `AMX_AMS_PUBKEY=` 값 복사.
3. 에이전트 서버에서:

```sh
bash deploy/agent-setup.sh install \
  --ams <중앙서버 IP>:50051 --token <토큰> --pubkey <값> --ca ./ca.crt
```

### 5-5. 에이전트 일상 운영

```sh
bash deploy/agent-setup.sh status     # 실행 여부 + 최근 로그
bash deploy/agent-run.sh logs         # 로그 follow
bash deploy/agent-run.sh down         # 종료
bash deploy/agent-run.sh up --ca ./ca.crt   # 재기동 (등록은 이미 됐으므로 토큰 불필요 —
                                            # 저장된 자격증명(.amx-agent/state)으로 재접속)
```

코드 업데이트 반영: `cd ~/AMX && git pull` 후 `agent-run.sh down` → `up ...`
(up이 매번 다시 빌드한다).

---

## 6. 에이전트 제거

```sh
cd ~/AMX

# 기본: 에이전트 종료 + 상태(.amx-agent) 삭제. tsamx와 계정 자격증명은 남긴다.
bash deploy/agent-setup.sh uninstall

# 전부 제거 (tsamx + Claude 설정 홈 = 계정 자격증명까지!)
bash deploy/agent-setup.sh uninstall --purge-tsamx --purge-config
```

- `--purge-config`는 **하달받은 계정 자격증명이 지워지므로** `yes` 입력 확인을
  요구한다 (자동화 시 `--yes`).
- 제거 전 예의: 관리자 화면에서 이 서버에 걸린 할당을 먼저 **회수**하고 지우는 것이
  깨끗하다 (계정이 화면 상태와 어긋나지 않게).
- 관리자 화면의 서버 행은 자동 삭제되지 않는다 — 더 안 쓸 서버면 화면에서 정리.

---

## 7. 중앙 서버 중지·제거 — ⚠ 읽고 나서 실행할 것

### 7-1. 데이터를 보존하며 껐다 켜기 (평소엔 이것만)

```sh
bash deploy/fullstack-run.sh down server web    # 앱만 종료, DB 컨테이너는 유지
bash deploy/fullstack-run.sh up all --lan       # 다시 켜기
# 또는 그냥:
bash deploy/fullstack-run.sh restart all --lan
```

### 7-2. `down all`은 초기화 명령이다

```sh
bash deploy/fullstack-run.sh down all    # ⚠ DB 컨테이너 삭제 = 데이터 전소
```

**`down all`은 DB 컨테이너를 삭제한다 = 테넌트·계정·할당이 전부 사라진다.**
(볼륨 미부착 — 실제 사고 이력 있음.) 의도적 초기화일 때만 쓴다.

또한 서버 DB를 초기화하면 **모든 에이전트의 저장 자격증명이 무효**가 되는데,
에이전트는 옛 자격증명을 새 토큰보다 우선 사용하므로 **무한 등록 거부**에 빠진다.
각 에이전트 서버에서:

```sh
rm -rf ~/AMX/.amx-agent/state    # 옛 서버 자격증명 폐기
# 이후 §5-2 원클릭 설치를 새로 수행 (새 토큰으로 재등록)
```

### 7-3. 완전 제거

```sh
bash deploy/fullstack-run.sh down all   # (위 경고 숙지 후)
rm -rf ~/AMX/.amx-dev                   # 시크릿·pidfile·로그 삭제
# 방화벽·portproxy 규칙 제거는 §4-4
# 저장소 자체를 지우려면: rm -rf ~/AMX
```

---

## 8. claude(러너) 실행법 — 일반적인 방법과 다르다

에이전트 서버에서 실제 작업자는 `claude`를 그냥 실행하면 **안 된다.** 두 가지가 다르다.

### 8-1. 설정 홈 지정 — `CLAUDE_CONFIG_DIR`

AMX가 하달한 계정은 기본 `~/.claude`가 아니라 **`~/.claude-amx`**(설치 시
`--config-dir`로 지정한 곳)에 들어간다. 따라서:

```sh
CLAUDE_CONFIG_DIR=~/.claude-amx claude
```

이걸 빼먹으면 개인 계정(~/.claude)으로 실행된다 — 로그인을 요구하거나
**엉뚱한 계정으로 과금**된다.

풀 조회·점검용 `tsamx`는 매번 `CLAUDE_CONFIG_DIR`를 앞에 붙이는 대신
**`tsclaude`** 래퍼를 쓴다. `deploy/agent-setup.sh install`이 설치 시점의
`--config-dir` 값을 각인해 `~/.local/bin/tsclaude`를 만들어 두므로,
`tsclaude list`는 `CLAUDE_CONFIG_DIR=~/.claude-amx tsamx list`와 같다.

### 8-2. 래퍼 경유 — `deploy/amx-claude`

에이전트가 계정을 교체(deliver)하는 순간에 claude가 새로 뜨면, 찰나의 창에서
**교체 중인 다른 계정을 읽어 그 계정으로 과금**될 수 있다. `deploy/amx-claude`
래퍼는 교체가 끝날 때까지 기다렸다가 claude를 띄워 이 창을 닫는다.
사용법은 `claude`와 완전히 동일하다 (인자 그대로 전달):

```sh
# 권장 설정 (에이전트 서버의 ~/.bashrc 등에):
export CLAUDE_CONFIG_DIR=~/.claude-amx
alias claude="$HOME/AMX/deploy/amx-claude"

# 이후 평소처럼:
claude                # 대화형
claude -p "작업 내용"  # 배치
```

이 전역 `export`·`alias`는 러너 **전용** 서버 전제다. 개인 `~/.claude`와 러너를 한
대에서 겸용하는 PC라면 이 방식을 쓰지 말고(개인 `claude`까지 러너로 끌려간다)
`docs/DEPLOYMENT-RUNNER.md` §9의 `amx` 명령을 쓴다.

자동화(웹훅·cron·systemd) 진입점도 `amx-claude`를 호출하게 한다.
래퍼를 우회한 직접 실행까지 차단하려면 `deploy/install-runner-guard.sh`
(검증: `verify-runner-guard.sh`) — 상세는 `docs/DEPLOYMENT-RUNNER.md`.

### 8-3. 계정 운영 한 바퀴 (관리자 화면 기준)

1. **계정 등록** — `계정` → `OAuth 계정 등록` → `인증 페이지 열기 ↗` →
   Claude 로그인·승인 → `코드#상태` 값을 인증 코드 칸에 붙여넣기 → 등록 완료.
2. **할당** — `할당` → `계정 할당` → 계정·서버 선택 → 생성 (**대기 상태가 정상**).
3. **전달** — 할당 행의 `전달` → 몇 초 안에 `활성`. 에이전트 서버에서
   `tsclaude list`에 계정이 보이면 성공.
4. **사용** — §8-2대로 claude 실행. 로그인 요구 없이 동작하면 끝까지 통한 것.
5. **회수** — 할당 행의 `회수` → 에이전트가 계정을 그 서버에서 완전히 뺀다
   (자격증명·매니페스트 삭제, `tsclaude list`에서 사라짐). 이력은 감사용으로
   `detached` 배정 행에 남고, 재사용하려면 다시 전달한다.

---

## 9. 사용량 관측(Langfuse) 설치 — 선택

러너 세션의 토큰 사용량을 계정별·모델별로 실측하려면 Langfuse 관측 층을 켠다. **선택
기능**이라 켜지 않아도 나머지 운영은 완전히 동일하다. **내부망·신뢰 경계 안 전용**이며,
프롬프트·응답을 마스킹 없이 전량 수집하므로 인터넷에 노출된 Langfuse에는 붙이지 않는다.
원본 절차는 `deploy/langfuse/README.md`(스택)와 `docs/DEPLOYMENT-RUNNER.md` §8(훅)이며,
아래는 운영 순서로 엮은 요약이다.

구성은 세 조각이다: ① 데이터를 받는 **Langfuse 서버**(셀프호스팅), ② 중앙 서버가
Langfuse를 폴링해 집계하는 **AMS 스윕**, ③ 각 러너 세션이 기록을 보내는 **Stop 훅**.

### 9-1. Langfuse 서버 기동 (관측 데이터를 받을 호스트에서)

```sh
cd ~/AMX/deploy/langfuse
cp env.example .env       # CHANGEME 전부 교체:
#  - NEXTAUTH_SECRET / SALT : openssl rand -base64 32
#  - ENCRYPTION_KEY         : openssl rand -hex 32  (정확히 64 hex)
docker compose config     # 병합·문법 검증
docker compose up -d
curl -fsS http://localhost:3100/api/public/health   # {"status":"OK"} 류면 성공
```

- 웹 포트는 기본 **3100**(`LANGFUSE_WEB_PORT`). 웹(3100)과 minio(9090)를 뺀 포트는
  `127.0.0.1`에만 바인딩된다. 다른 기기에서 대시보드를 열려면 §4 방식으로 3100을 연다.
- 웹이 스키마 마이그레이션을 끝낸 뒤에만 워커가 뜨도록 기동 순서가 고정돼 있어, 첫
  `up`부터 재시작 없이 정상화된다.
- **`docker compose down -v`는 관측 데이터를 전소**시키므로 쓰지 않는다(볼륨 백업 후 업그레이드).
- 대시보드에서 조직·프로젝트를 만들고 **API 키(pk-…/sk-…)**를 발급해 둔다. 아래 두 곳(AMS, 훅)이 이 키를 쓴다.

### 9-2. 중앙 서버(AMS) 집계 켜기

집계 스윕은 `.amx-dev/dev.env`에 아래 4종이 **모두** 있을 때만 활성화된다(하나라도 비면 비활성).

```sh
cat >> ~/AMX/.amx-dev/dev.env <<'ENV'
AMX_LANGFUSE_BASE_URL=http://<langfuse-host>:3100
AMX_LANGFUSE_PUBLIC_KEY=pk-...
AMX_LANGFUSE_SECRET_KEY=sk-...
AMX_LANGFUSE_TENANT_ID=<집계 대상 테넌트 UUID>
ENV
bash deploy/fullstack-run.sh restart server --lan
```

- 선택 변수: `AMX_LANGFUSE_UI_URL`(패널 딥링크용 대시보드 주소), `AMX_LANGFUSE_POLL_SECONDS`
  (기본 300, 최소 60), `AMX_LANGFUSE_METRICS_WINDOW_DAYS`(기본 3, 최소 2로 클램프),
  `AMX_LANGFUSE_MAX_ACCOUNTS`(기본 100).
- 현재 롤업은 **전역 단일 테넌트**(`AMX_LANGFUSE_TENANT_ID`)에 귀속된다 — 그 테넌트의
  사용량 탭에서만 실측 패널이 채워지고, 다른 테넌트 조회는 빈 결과다(§AMX-DESIGN 5.6.1).
- **마이그레이션은 자동**이다. `fullstack-run.sh`의 up/restart가 `alembic upgrade head`를
  돌려 이 트랙이 추가한 **0019**(watermark_future 경보)·**0020**(스냅샷 보존 부분 인덱스)·
  **0021**(langfuse_usage_rollup)·**0022**(alert_webhook_outbox + Langfuse 임계값 경보
  kind 3종)까지 적용한다. 이 마이그레이션들이 밀려 있던 구버전 AMS를 올리는 경우에도
  restart 한 번이면 반영된다.

### 9-3. 러너에 Stop 훅 배포

각 에이전트 서버 러너에 훅을 심으면 그 서버의 세션이 Langfuse로 흘러간다. `amx-claude`
래퍼를 거쳐 뜬 세션만 추적되고, 훅·키가 없는 서버는 동작이 이전과 같다.

- **호스트 한 대**: `docs/DEPLOYMENT-RUNNER.md` §8-"설치"의 `install-langfuse-hook.sh`.
- **신규 에이전트 자동 적용**: `agent-setup.sh install`에 `AMX_LANGFUSE_BASE_URL`/
  `PUBLIC_KEY`/`SECRET_KEY` 3종을 함께 주면 설치 끝에 훅까지 심는다(§DEPLOYMENT-RUNNER 8).
- **기존 함대 일괄**: `deploy/fleet-langfuse.sh on|off|status`. 실호스트 목록은
  `fleet-hosts.txt`(커밋 금지), 시크릿은 0600 env 파일로 소싱한다. dev 호스트가
  `~/AMX-agent`에 체크아웃돼 있으면 `--remote-repo ~/AMX-agent`를 붙인다.
  상세·주의는 `docs/DEPLOYMENT-RUNNER.md` §8-"함대(fleet) 일괄 배포".

> 확인: 켜진 뒤 러너로 세션을 한 번 돌리고, 5분(폴 주기) 안팎 뒤 관리자 화면 **사용량**
> 탭의 Langfuse 패널에 계정·모델이 뜨면 세 조각이 끝까지 이어진 것이다. `fleet-langfuse.sh
> status`는 env 파일 존재만 보므로 "켜짐"이 곧 추적 성립 증거는 아니다(§DEPLOYMENT-RUNNER 8).

### 9-4. 경보 웹훅 + Langfuse 임계값 경보 (선택)

AMS의 모든 경보(all_exhausted·server_offline·drift·quarantine·recall_failed·
command_send_failed·self_update_failed·billing_watermark_future + 아래 임계값 3종)를
외부 수신 엔드포인트로 내보낸다. URL과 시크릿이 **둘 다** 설정될 때만 켜지고(하나라도
비면 완전 무부작용), 발송은 전용 배경 스위퍼가 아웃박스를 드레인해 처리한다.

```sh
cat >> ~/AMX/.amx-dev/dev.env <<'ENV'
AMX_ALERT_WEBHOOK_URL=https://<수신-엔드포인트>/ams-alerts
AMX_ALERT_WEBHOOK_SECRET=<32바이트+ 무작위 시크릿>
ENV
bash deploy/fullstack-run.sh restart server --lan
```

- 수신 측은 본문을 **받은 바이트 그대로** 두고 서명을 재계산해 검증한다:
  `expected = "sha256=" + HMAC_SHA256(시크릿, X-AMS-Timestamp 헤더 + 원문 본문)`을
  `X-AMS-Signature`와 상수시간 비교하고, `X-AMS-Timestamp`(유닉스 초)가 허용 시차(예:
  ±5분) 안인지 확인해 리플레이를 거른다. 페이로드는
  `{alertId, kind, status(open|resolved), tenantId, serverId, detail, occurredAt}`이다.
- **전달 의미론**: **at-least-once**(정확히 1회 아님)이고 **순서 미보장**이다 — 재시도·다중
  인스턴스로 같은 전이가 중복 도착하거나 open/resolved가 뒤바뀐 순서로 올 수 있다. 수신자는
  `(alertId, status, occurredAt)`를 멱등 키로 삼아 중복을 흡수하고, 더 이른 `occurredAt`이
  나중에 도착해도 최신 상태를 되돌리지 않도록 처리한다.
- 실패는 지수 백오프로 재시도하고 상한 초과 시 폐기한다(무한 적재 없음). 폐기 시에는
  관측용 셀프 경보 `alert_webhook_dropped`가 열린다(이 경보 자체는 웹훅으로 내보내지 않아
  재귀하지 않는다). 시크릿은 서명 계산에만 쓰이고 로그에 남지 않는다.
- 드레인은 오프라인 탐지 루프와 **분리된 전용 태스크**로 돌아 불량 수신자가 다른 배경
  작업을 지연시키지 않는다. 선택 변수: `AMX_ALERT_WEBHOOK_DRAIN_SECONDS`(기본 30, 최소 5),
  `AMX_ALERT_WEBHOOK_TIMEOUT_SECONDS`(발송 POST 타임아웃, 기본 5).
- **Langfuse 임계값 경보 3종**은 §9-2의 Langfuse 집계가 켜져 있을 때만 동작하며(활성
  게이트·폴 주기 공유), 임계값은 아래 변수로 조정한다(전부 선택, 기본값 존재). 이 경보들도
  위 웹훅으로 함께 나간다.
  - `AMX_ALERT_SPIKE_FACTOR`(기본 3.0) / `AMX_ALERT_SPIKE_MIN_TOKENS`(기본 1000000) —
    당일 총 토큰이 전일 대비 배수를 넘으면 `langfuse_usage_spike`. 전일이 0이면 절대 하한
    초과 시에만.
  - `AMX_ALERT_STALE_MINUTES`(기본 60) — 롤업이 이 분(minute)만큼 갱신되지 않으면
    `langfuse_stale`.
  - `AMX_ALERT_LATENCY_P95_MS`(기본 60000) — Metrics API latency p95(최근 1시간)가 이
    밀리초를 넘으면 `langfuse_latency`.

### 9-5. 위험명령 경보 (선택)

러너에 위험명령 감지 훅(§9-3의 `--with-danger-hook`)을 깔았다면, 그 통보를 받을 창구를
중앙 서버에 열어야 한다. 훅이 Bash 위험 패턴을 잡으면 마스킹한 통보를
`POST /api/v1/ingest/danger-command`로 보내고, AMS는 이를 `dangerous_command` 경보로
올려 §9-4 웹훅으로 흘린다.

```sh
cat >> ~/AMX/.amx-dev/dev.env <<'ENV'
AMX_DANGER_INGEST_TOKEN=<32바이트+ 무작위 토큰, 러너 훅과 동일 값>
AMX_DANGER_TENANT_ID=<경보를 매달 테넌트 UUID; 생략 시 AMX_LANGFUSE_TENANT_ID 사용>
ENV
bash deploy/fullstack-run.sh restart server --lan
```

- 이 엔드포인트는 관리자 인증이나 테넌트 범위를 타지 않는다. 사람이 아니라 무인 훅이
  호출하므로 정적 토큰 하나(`X-AMX-Ingest-Token`)로만 막는다. **토큰과 귀속 테넌트가 둘 다
  있어야** 켜진다. 하나라도 없으면 경로가 비활성(404)이라 안 켠 서버에서는 이 경로가 없는
  것처럼 보인다. 토큰이 틀리면 401이다.
- 경보는 서버에 매이지 않는 시스템 범위지만 **실 테넌트**(`AMX_DANGER_TENANT_ID`, 없으면
  `AMX_LANGFUSE_TENANT_ID`)에 귀속돼 콘솔 경보 목록·ack 동선에 정상 노출된다. 중복은
  `(tenant, hostname, patternName, commandSha256)`로 흡수하므로 같은 호스트의 같은 위험
  명령이 반복돼도 새 경보로 쌓이지 않고 기존 경보만 갱신된다. 자동 해소는 없다 — 관리자가
  확인하고 ack/resolve 한다.
- 저장되는 건 마스킹본·해시·세션·호스트뿐이고 **원문 명령은 남지 않는다**.
- 폭주 방어로 전역 분당 상한을 둔다. `AMX_DANGER_RATE_LIMIT_PER_MIN`(기본 120)을 넘는
  통보는 429로 떨어뜨리고 로그만 남긴다. 프로세스 로컬 카운터라 다중 인스턴스에서는
  인스턴스당 상한이다. 본문은 읽기 전에 Content-Length 64KB로 걸러(초과 413) 값싸게 막는다.

---

## 10. 문제 해결 표

| 증상 | 1순위 점검 | 조치 |
|---|---|---|
| 에이전트 로그 `connection refused`/타임아웃 | 중앙 서버가 WSL이면 **portproxy 어긋남** (재부팅으로 WSL IP 변동) | §4-3 박스. 중앙 서버에서 `agent-install-cmd.sh` 재실행하면 자동 점검·수정 명령 출력 |
| 에이전트 TLS/인증서 오류 | SAN에 접속 주소가 없거나, ca.crt 불일치·만료 | §2 — 접속 주소를 `--ip`/`--dns`로 넣어 재발급 후 서버 재시작 |
| 등록 거부 (registration denied) | 토큰 만료 또는 **DB 초기화 후 옛 자격증명 잔존** | 토큰은 생성기 재실행으로 재발급. DB 초기화였다면 `rm -rf ~/AMX/.amx-agent/state` 후 재설치 (§7-2) |
| 서버 기동 거부 "gRPC 보안이 설정되지 않았습니다" | dev.env에 TLS cert 경로 누락 | §3-1의 printf 배선 후 restart |
| 관리자 생성 422 | 예약 도메인 (amx.local 등) | 일반 도메인 이메일 사용 |
| 웹 화면 깨짐/빌드 충돌 | 웹 실행 중 재빌드 겹침 | `down web` → `up web`, 안 되면 `ams-web/.next` 삭제 후 재기동 |
| claude가 로그인 요구 | `CLAUDE_CONFIG_DIR` 미지정 | §8-1 |
| 전달이 `전달 중`에서 멈춤 | 서버 행이 온라인인지 | 오프라인이면 에이전트부터 살린다 (`agent-run.sh status/logs`) |
| 기동 로그에 `SECURITY: AMX_ALLOW_RAW_KEK` 경고 | 운영 금지 플래그가 켜짐 | 즉시 해당 env 제거 후 재기동 |

---

## 11. 설치 완료 체크리스트

**중앙 서버:**
- [ ] `fullstack-run.sh status` 전부 ✔ (db/REST/gRPC/web)
- [ ] 기동 로그에 평문(insecure)·RAW_KEK 경고가 **없다**
- [ ] 방화벽: 50051 개방, 8080·55432는 외부에서 닫혀 있다
- [ ] (WSL) portproxy가 현재 WSL IP를 가리킨다
- [ ] 관리자 로그인 성공, 테넌트 생성됨
- [ ] `deploy/tls/ca/ca.key`가 이 서버 밖으로 나간 적 없다

**에이전트 서버 (각각):**
- [ ] 관리자 화면에서 서버가 **온라인**
- [ ] `agent-run.sh logs`에 error/refused/denied 없음
- [ ] 계정 전달 → `tsamx list` 확인 → claude 실행 → 회수, 한 바퀴 성공
- [ ] `CLAUDE_CONFIG_DIR` export + `amx-claude` alias 설정됨
