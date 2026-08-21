# 개발·시험 안내서 — 운영(PROD)과의 차이분

이 문서는 **개발·시험 환경에서만 다른 부분**만 모은 얇은 차이분이다. 설치·기동·계정
운영의 **기준 절차는 전부 `docs/PROD-GUIDE.md`**에 있고, 여기서는 그와 갈라지는 지점만
"기준 절차는 PROD-GUIDE §N" 형태로 가리키며 덧붙인다.

핵심 차이는 하나다. 시험 환경은 **gRPC를 평문으로 띄운다**(`--insecure-grpc` /
에이전트 `--insecure`). 운영은 TLS가 필수라 이 플래그를 쓰지 않는다. 그 외 시크릿
자동 생성, 예약 도메인 회피 같은 소소한 차이가 있다.

```
┌─ PC (서버 트랙) ──────────────┐        ┌─ 노트북 (에이전트 트랙) ─────┐
│ ams-server  REST :8080        │  LAN   │ ama-agent ──→ PC:50051      │
│             gRPC :50051       │◀──────▶│ tsamx (계정 넣고 빼기)       │
│ ams-web     관리자 화면 :3000 │        │ claude (실제 사용)           │
└───────────────────────────────┘        └─────────────────────────────┘
```

---

## 1. 서버(PC) 트랙 — 기동만 평문으로

준비물(docker·uv·node)과 이후 절차는 **PROD-GUIDE §1, §3~§4**가 기준이다. 시험에서는
TLS 인증서 발급(PROD §2)을 건너뛰고 평문으로 한 줄에 띄운다:

```sh
bash deploy/fullstack-run.sh up all --insecure-grpc --lan   # 최초 1회
bash deploy/fullstack-run.sh up                              # 이후 재기동은 이것만
```

- 플래그는 `.amx-dev/run.env`에 기억되므로 두 번째부터는 `up`만 치면 같은 플래그(`--insecure-grpc --lan`)로 뜬다. 플래그를 다시 주면 그 값으로 덮어쓴다.
- 첫 실행 때 비밀값(암호화 키·관리자 토큰 등)을 자동 생성해 `.amx-dev/dev.env`(0600)에 보관한다.
- `--insecure-grpc` = gRPC 평문. **첫 시험 전용**이라 기동 로그에 경고가 뜬다. 운영은 이 플래그 없이 TLS로 띄운다(PROD §2~§3).
- `--lan` = 웹·REST를 모든 인터페이스에 바인딩. **브라우저나 노트북이 다른 기기라면 필수**다(빼먹으면 다른 기기에서 `ERR_CONNECTION_RESET` — 실측).

성공 판정 4줄(db/REST/gRPC/web ✔)·로그 확인은 PROD §3-1과 같다.

- **WSL2 portproxy**: 기준 절차는 PROD-GUIDE §4-3. 시험이라고 다르지 않다 — WSL 내부 IP는 재부팅 시 바뀌니 노트북이 갑자기 안 붙으면 이것부터 의심한다.
- **관리자 생성·로그인**: 기준 절차는 PROD-GUIDE §3-2. 시험에서는 `example.com`류 일반 도메인을 쓴다(`amx.local` 같은 예약 도메인은 422).
- **화면 기본 동작(L1 스모크)**: 로그인 후 테넌트 생성 → 서버 등록(에이전트 없으면 오프라인이 정상) → 계정/할당/알림 패널이 오류 없이 열리면 통과. 클릭 위치는 PROD §3-2 이후 온보딩과 같다.

---

## 2. 에이전트(노트북) 트랙 — 접속만 평문으로

원클릭 설치·수동 설치·제거의 기준 절차는 **PROD-GUIDE §5~§6**이다. 시험에서 다른 점은
TLS 재료(`--ca ./ca.crt`) 대신 **평문 플래그**를 쓴다는 것뿐이다.

- **설치 명령 생성(PC)**: PROD §5-2와 같되 `--tls` 없이 `bash deploy/agent-install-cmd.sh --server-name "이광희 노트북"`. 서버 행 생성·토큰 발급·IP/공개키 수집·portproxy 점검을 자동으로 하고 붙여넣을 명령을 완성해 준다.
- **설치(노트북)**: 출력된 명령을 붙여넣되 TLS 인자 자리에 `--insecure`가 들어간다(예: `--ams 10.60.1.15:50051 --token <발급됨> --pubkey <채워짐> --insecure`). 저장소는 개발 트리와 섞이지 않게 별도 클론(예: `~/AMX-agent`)을 쓴다.
- **성공 판정(L2)**: 스크립트 끝 판정 + 관리자 화면 `서버` 메뉴에서 **온라인**.
- **제거**: 기준 절차는 PROD-GUIDE §6.

대표 실패 증상(connection refused=portproxy 어긋남, `AMS public key not configured`=`--pubkey` 누락, TLS/평문 짝 불일치, 등록 거부=토큰 만료)은 PROD §10 문제 해결 표와 같다.

---

## 3. 실계정 왕복 (L3) — 기준 절차 + 회수 잔재 정리

등록 → 할당 → 전달 → 사용 확인 → 회수의 한 바퀴는 **PROD-GUIDE §8-3**이 기준이다.
성공 판정에 쓰는 `tsclaude list`(= `CLAUDE_CONFIG_DIR=~/.claude-amx tsamx list`)도 거기 설명이 있다.

### 회수 잔재 수동 정리 (INACTIVE가 안 사라질 때)

회수는 항상 purge라(O2, 2026-08-14) 정상 경로면 계정이 풀에서 즉시 사라진다. 그런데
`tsclaude list`에 **INACTIVE로 남아 있는** 계정이 보이면 두 경우 중 하나다.

1. **구버전 에이전트가 남긴 disable 잔재** — 예전 disable-회수 시절의 흔적이거나, 회수가
   DIVERGED/REJECTED로 실패한 상태. reconcile-on-report가 자동으로 purge를 재발행해
   정리한다. 서버가 온라인이고 보고가 도는지 확인하고 잠시 기다린다.
2. **재시도 상한(3회) 소진** — 자동 재발행이 3회 모두 실패하면 loop guard가 걸려
   (`reconcile.py`의 재발행 상한) 더는 자동으로 청소되지 않는다. 이때 경보는 열리지
   않고 로그 warning과 drift 기록만 남으므로, 잔재는 **조용히** 남는다. 수동 정리
   방법은 둘뿐이다(할당이 이미 detached라 `:recall`은 force여도 409가 난다):
   - 노트북에서 직접: `tsclaude remove <email>` (그 서버의 풀에서 제거).
   - 또는 관리자 화면에서 그 계정을 **다시 전달 → 다시 회수**해 정상 purge 경로를 태운다.

> INACTIVE는 비용 배분에서 **제외되지 않는다**(자격증명 점유 중인 deactivate와 구분 불가하고,
> 제외하면 구독료가 증발해 전액 배분 불변식이 깨진다). 그래서 잔재는 위처럼 실제로 풀에서
> 제거해야 배분에서 빠진다.

### 계정 분리 규칙 — 개인용과 배정용은 섞지 않는다

한 OAuth 계정을 개인 프로필(`~/.claude`)과 AMX 배정 양쪽에서 쓰면 refresh token
회전이 맞부딪쳐 **양쪽이 다 깨진다**(2026-08-17 실사고 — 나중에 갱신한 쪽이 이미
무효화된 토큰을 쥐게 되고, 빈 자격증명이 중앙까지 역동기화됐다). 등록 전에 한 가지만
자문하면 된다: *이 계정을 사람이 자기 PC에서 직접 로그인해 쓰는가?*

- **그렇다** → 등록 화면(OAuth·API키 공통)의 **배정 제외** 체크박스를 켠다. 신규
  배정이 409로 거부된다. 등록해 두는 것 자체는 무방하다(사용량 관찰 등).
- **아니다(AMX 전용)** → 그대로 등록하고, 이후에도 개인 프로필에서 그 계정으로
  로그인하지 않는다.

이미 배정된 계정에서 병용이 발견되면 순서가 중요하다: 계정 편집에서 **배정 제외를
먼저 켜고**(신규 유입 차단), **기존 배정을 회수까지** 해야 사고가 멈춘다 — 표시만
세우면 남은 배정에 reconcile이 자격증명을 계속 재푸시한다(§5.2). 병용 사고의 1차
신호는 `credential_unusable` 경보이고, 중앙 보관본이 이미 오염됐는지는
`ams-server/scripts/scan_credential_material.py`(읽기 전용, 토큰 미출력)로 전수
확인할 수 있다. 오염된 행의 자동 복구 경로는 없다 — 회수 → 계정 삭제 → OAuth
재등록이 유일한 조치다.

### 계정 풀 자동 배분 시험 시

기준 사양·운영 절차는 **PROD-GUIDE §3-5**, 설계는 `AMX-DESIGN.md` §5.8이다. 시험에서만
챙길 점 셋.

- **에이전트를 먼저 새 코드로.** 사용량 미측정 계정 제외·`relogin_required` 격리·
  `usage_fetched_at`·`deliver_lock_timeout` 같은 새 입력은 재빌드·재설치한 에이전트라야
  올라온다(§4 self-update 또는 §2 재설치). 구버전 에이전트로는 풀 판정 입력이 비어 관측이
  덜 찬다.
- **자동 모드는 관측 뒤에.** 콘솔 "계정 풀" 탭에서 서버를 수동으로 두고 배급처·대여중·
  충전소 분포를 며칠 본 뒤 그 서버만 자동으로 올린다. 마이그레이션 0028~0031은 기동 시
  자동 적용된다.
- **체인 중 수동 배정은 409.** 교체 체인이 도는 서버에 전달·회수·즉시전환을 걸면
  `409 pool.chain_active`가 정상이다. force recall(global-admin)만 체인을 무효화하고 통과한다
  (위 §3 회수 잔재 정리와 같은 force 경로).

---

## 4. 에이전트 자기 업데이트 (self-update) 시험

C트랙이 끝난 상태에서, 원격으로 에이전트를 최신 커밋으로 올리는 경로를 확인한다.
에이전트는 **자기 노트북의 클론**(`~/AMX-agent`)만 당겨서 자기를 다시 빌드한다. 명령에는
저장소 주소도 브랜치도 실리지 않으니, PC에서 코드를 밀어 넣는 게 아니라 노트북이 스스로
`git pull`을 하는 것으로 이해하면 된다.

호출은 REST다(화면 버튼은 별도 트랙). `<TEN>`·`<SRV>`는 테넌트·서버 UUID:

```sh
curl -X POST -H "Authorization: Bearer $AMX_ADMIN_TOKEN" \
  http://localhost:8080/api/v1/tenants/<TEN>/servers/<SRV>:self-update    # 202
```

**시험 1 — 정상 왕복.** 노트북 `~/AMX-agent`가 최신보다 뒤처진 상태를 만들고(`git -C ~/AMX-agent reset
--hard HEAD~1` 후 `deploy/agent-run.sh up`) 위 명령을 부른다.

- **성공 판정**: 30초~2분 뒤 `agent-run.sh logs`에 재기동 흔적이 남고, 화면의 서버 상세
  `agent_version`이 `p3+<새 커밋 앞 12자>`로 바뀐다. **버전 문자열이 바뀌는 것이 유일한
  성공 판정이다** — ack(CONVERGED)은 "바이너리를 교체하고 재기동을 요청했다"까지만 뜻하고,
  새 버전이 실제로 떴다는 보장이 아니다.
- 빌드가 도는 동안 계정 전달·스위칭은 평소대로 동작해야 한다(교체 직전까지 락을 잡지 않음).

**시험 2 — 핀 불일치로 거부.** 있지도 않은 커밋을 못으로 박아 보낸다:

```sh
curl -X POST -H "Authorization: Bearer $AMX_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"expectedCommit":"aaaaaaa"}' \
  http://localhost:8080/api/v1/tenants/<TEN>/servers/<SRV>:self-update
```

- **성공 판정**: 화면 `알림`에 `self_update_failed`(사유 `commit_mismatch`)가 뜨고, 노트북에서
  `git -C ~/AMX-agent rev-parse HEAD`가 **호출 전과 같다**. 핀 대조는 pull 앞에서 하므로 거부된
  요청은 작업 트리를 건드리지 않는다. 에이전트는 계속 온라인이어야 한다.
- 같은 서버에 self-update가 이미 queued/sent면 두 번째 호출은 409
  `self_update_already_pending`으로 막힌다(연타 방지). 앞 건이 ack되거나 실패해야 다시 된다.

**시험 3 — `ama.bak` 수동 복구.** 새 바이너리가 떠서 죽는 상황을 흉내 낸다. 노트북에서:

```sh
bash deploy/agent-run.sh down
cp ~/AMX-agent/.amx-agent/ama.bak ~/AMX-agent/.amx-agent/ama   # 직전 바이너리로 되돌리기
bash deploy/agent-run.sh up
```

교체 직전 바이너리는 항상 `ama.bak`에 남는다. 저장소까지 되돌려야 하면
`git -C ~/AMX-agent reset --hard origin/main && bash deploy/agent-run.sh up`.

- **성공 판정**: 서버가 다시 온라인이 되고 `agent_version`이 되돌린 커밋을 가리킨다.

> ⚠ 새 커밋이 지금 돌고 있는 AMS보다 앞설 수 있다(핀 없이 보내면 upstream tip으로 간다).
> PC 서버를 먼저 올리거나 `expectedCommit`으로 못을 박는다. 그리고 플릿에 걸 때는 **1대 먼저
> 걸어 `agent_version`을 확인한 뒤** 나머지에 건다.

---

## 5. 끄기·초기화

기준 절차는 **PROD-GUIDE §7**이다. 시험에서도 경고는 동일하다:

> ⚠ **`down all`은 DB 컨테이너를 삭제한다 = 테넌트·계정·할당 데이터가 전부 사라진다**(실측).
> 데이터를 유지한 채 껐다 켜려면 `down server web` 같은 부분 종료나 `restart`를 쓰고,
> `down all`은 초기화가 목적일 때만 쓴다. 비밀값 `.amx-dev/dev.env`는 남는다(완전 초기화는 `.amx-dev/` 삭제).

노트북 쪽 제거는 PROD §6.

---

## 6. 알려진 문제·이력

- **OAuth "Invalid request format"**: 승인 클릭 시점에 나던 오류. 원인은 authorize `state`가 16바이트였던 것
  (claude.ai는 32바이트 요구). 2026-08-10 수정 완료 — 재발하면 `claude` 바이너리 상수와
  `ams-server/app/services/oauth_enroll.py`를 대조.
- **할당 즉시 전달**: P1에서는 지원되지 않아 화면에서 제거됨. 생성(대기) 후 `전달` 버튼을 쓰는 것이 정상 절차.
- **웹 빌드 충돌**: 웹이 떠 있는 상태에서 재빌드가 겹치면 `.next`가 깨질 수 있음 —
  `down web` 후 `up web`, 그래도 안 되면 `ams-web/.next` 삭제 후 재기동.
