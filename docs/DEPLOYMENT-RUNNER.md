# 러너 무중단 배포 (O5 / B1b)

deliver가 credential을 교체하는 동안 러너(Claude Code)가 신규 계정으로 과금되지
않도록 하는 배포 가이드. AMA 측 B1a(창 최소화)와 B1b(flock 조율)의 2층 방어를
설명하고, 참조 래퍼 `deploy/amx-claude`의 설치법을 다룬다.

## 1. 러너 모델 (두 경로)

같은 서버에서 Claude Code(=러너)가 두 경로로 기동되며, **둘 다 기동 시점에
`$CLAUDE_CONFIG_DIR/.credentials.json`을 읽어** 그 계정으로 과금한다.

1. **사용자 터미널**: 사용자가 직접 `claude`를 실행(대화형, 장수명).
2. **ClickEye webhook**: 터널로 들어온 요청이 진입점에서 `claude -p <명령>`을
   실행(배치, 단수명).

두 경로 모두 **임의 시점에** 기동될 수 있다 — AMA의 deliver와 시간적으로 겹칠 수
있다는 것이 문제의 핵심이다.

## 2. 과금 창 (왜 위험한가)

deliver 크리티컬 섹션(SSOT §6.3, AMA `handleDeliver`)은 다음을 한 구간에서 수행한다.

```
[이전 활성 기록] → [.credentials.json + .claude.json 원자적 기록] → [tsamx add]
                 → [tsamx switch <이전 활성> 복귀]
```

`tsamx add`는 신규 슬롯을 **활성**으로 만든다. 그래서 add 직후 ~ 복귀 직전의 짧은
구간 동안 `.credentials.json`이 가리키는 활성 계정이 잠시 신규 계정이 된다. 이
구간에 러너가 **새로 기동**되면 신규 계정 credential을 읽어 오과금된다.

- **이미 실행 중인** 러너는 영향 없음 — 기동 시 읽은 credential을 계속 쓴다.
  위험한 것은 이 창 안에서 **새로 시작하는** 러너의 startup read뿐이다.

## 3. 2층 방어

### 층 1 — B1a: 창 최소화 (래퍼 없이도 적용)
- deliver 전에 활성 계정을 기록하고, add 후 **이전 활성으로 복귀**(`tsamx switch`).
  desired가 ACTIVE/INACTIVE든 러너 활성은 이전 상태로 유지된다.
- credential 파일 쓰기는 **원자적**(같은 디렉터리 temp + `rename`) — 러너가
  찢긴/부분 파일을 읽지 않는다.
- 결과: 활성이 신규로 바뀌어 있는 창이 수백 ms 이하로 좁아진다. 래퍼를 설치하지
  않아도 노출은 이 수준으로 제한된다.

### 층 2 — B1b: flock 조율 (래퍼 설치 시 "진행 중 deliver 위 기동" 차단)
- **AMA**: deliver 크리티컬 섹션(기록~add~복귀) 동안 `$CLAUDE_CONFIG_DIR/.amx-deliver.lock`에
  **배타 락**(`flock LOCK_EX`, `internal/tsamx/exec.go` `DeliverLock`)을 잡는다.
  - **획득은 논블록·상한 재시도**(`LOCK_EX|LOCK_NB`, 50ms 간격, 상한 기본 5s) 이고
    **`handleDeliver`가 엔진 락을 잡기 _전에_ 호출**한다. 러너가 공유 락을 오래
    쥐고 있어 상한 내 획득에 실패하면 **flock 없이 진행**(fail-open) — deliver가
    **무한 블록되지 않으며 엔진 락을 점유하지 않는다.** 따라서 장수명 러너가 떠
    있어도 스케줄러 틱·다른 명령은 계속 진행(엔진 프리즈 없음). fail-open 시 노출은
    층 1(B1a)이 sub-second로 방어.
  - 엔진 락(AMA 내부 직렬화)과 별개로, 이 flock은 **러너 프로세스와의 조율** 전용.
- **러너 래퍼(`deploy/amx-claude`)**: `claude` 기동 **전에** 같은 파일에 **공유 락**을
  `flock -w <상한> -s <lock> -c true`로 잡아 **진행 중 deliver가 끝날 때까지 대기한 뒤
  즉시 놓고** `exec claude`. **claude 수명 동안 락을 유지하지 않는다** — 유지하면
  AMA의 배타 deliver가 러너 종료까지 대기해(장시간 대화형 → deliver·스위칭 엔진 스톨)
  가용성을 해친다.
- **락이 창을 닫는 근거(보조)**: 래퍼가 공유 락을 얻는 시점에 deliver가 배타 락을
  쥐고 있으면 래퍼는 상한까지 대기 → 러너 기동이 진행 중 deliver 위에서 시작하지
  않는다. 락을 놓은 뒤 claude read까지의 짧은 틈은 층 1(B1a 원자적·sub-second)이
  방어하는 **best-effort** 보조 방어다(완벽 무중단은 claude가 credential read 시점을
  노출해야 가능하나 불가).

lock 파일은 credential 파일과 **분리**되어 있어 claude가 절대 건드리지 않는다. AMA·래퍼가
`CLAUDE_CONFIG_DIR` 미설정이면 **양쪽 다 `~/.claude`로 해석**해 같은 lock 파일을 잡는다.

## 4. 래퍼 설치

`deploy/amx-claude`는 `claude`의 드롭인 대체다. `CLAUDE_CONFIG_DIR`를 존중하고
(미설정 시 `~/.claude`), `-p` 포함 **모든 인자를 그대로 패스스루**한다.

- **사용자 터미널**: 대화형 셸에서 alias 권장.
  ```sh
  alias claude=amx-claude    # ~/.bashrc, ~/.zshrc 등
  ```
- **ClickEye webhook**: 진입점에서 `claude` 대신 `amx-claude`를 호출.
  ```sh
  amx-claude -p "<명령>"
  ```
- PATH에 `deploy/amx-claude`를 설치하되, 래퍼가 실제 `claude`를 찾도록 한다.
  래퍼가 `claude`라는 이름으로 PATH를 가릴 경우 `AMX_CLAUDE_BIN`에 실제 바이너리
  경로를 지정한다(자기 재귀 방지).
- flock(1)이 없거나 config 디렉터리를 만들 수 없으면 래퍼는 **차단 대신** claude를
  직접 실행한다(가용성 우선; 이때도 층 1의 sub-second 창 방어는 유효).

## 5. lock 파일 위치·정리

- 경로: `$CLAUDE_CONFIG_DIR/.amx-deliver.lock` (기본 `~/.claude/.amx-deliver.lock`).
- 내용 없음(0바이트). 존재만으로 의미가 있으며 삭제해도 다음 deliver/기동 시 재생성.
- flock은 **프로세스 연관** 락이라 AMA/러너가 죽으면 커널이 자동 해제한다 — 스테일
  락이 남지 않는다. 파일 자체는 남아도 무해하다.
- AMA와 러너가 **같은 `CLAUDE_CONFIG_DIR`**를 보도록 배포에서 보장해야 한다(§1의
  공유 config 전제). 경로가 어긋나면 두 flock이 다른 파일을 잡아 조율이 무효가 된다.

## 6. 트레이드오프 (운영 주의) — 가용성 우선 설계

핵심 원칙: **flock이 가용성을 해치지 않는다.** 초기 설계(래퍼가 claude 수명 동안
락 유지 + AMA가 블로킹 배타 락)는 장수명 대화형 러너 하나가 deliver의 배타 락을
무한 대기시키고, 그 대기가 **엔진 락을 점유해 스케줄러 틱·자동 스위칭(§6.4 무인운영)을
정지**시키는 치명 결함이 있었다. 현재 설계는 이를 제거했다:

- **AMA**: 배타 락 획득이 **논블록·상한(기본 5s)·fail-open**이고 **엔진 락 밖**에서
  일어난다. 상한 초과 시 flock 없이 deliver를 진행한다. → deliver도 엔진도 절대
  무한 블록되지 않는다. fail-open 시 방어는 층 1(B1a sub-second)로 축소되지만
  **무인 운영(자동 스위칭)은 계속 돈다.**
- **래퍼**: 공유 락을 **잠깐 확인 후 놓고** claude를 기동한다(수명 유지 안 함). →
  실행 중 러너가 deliver를 지연시키지 않는다. 다중 러너 동시 기동 OK.
- **잔여 노출**: 래퍼가 락을 놓은 뒤 claude read까지의 짧은 틈, 그리고 AMA fail-open
  구간. 둘 다 층 1(B1a: 이전 활성 복귀 + 원자적 쓰기, sub-second·torn-free)이 방어하는
  **best-effort** 구간이다. 완벽한 무중단은 claude가 credential read 시점을 노출해야
  가능하나 불가하므로, **B1a가 주 방어, B1b flock은 "진행 중 deliver 위 기동 방지"
  보조**라는 역할 분담이 설계 결론이다.
- **튜닝**: 래퍼 대기 상한은 `AMX_DELIVER_WAIT`(초, 기본 5), AMA 측 상한은
  `ExecBridge.LockMaxWait`(기본 5s)로 조정한다. 0이면 기본값.

## 7. 진입점 강제 (B1) — 래퍼 미경유 직접 실행 차단

alias(§4)는 대화형 셸에서만 확장된다. 비대화형 셸·`sh -c`·cron·systemd
`ExecStart`은 alias를 보지 못하므로 ClickEye webhook 같은 배치 진입점이 래퍼를
조용히 우회한다. 그래서 배포에서는 **PATH 셰도잉**으로 강제한다: 실제 `claude`보다
PATH에서 앞서는 bin 디렉터리(기본 `/usr/local/bin`)에 `claude`라는 이름의 **shim**을
설치해, 셸 상호작용성과 무관하게 `execvp`/`command -v`가 shim을 먼저 고르게 한다.
shim은 `AMX_CLAUDE_BIN=<실제 claude>`를 export하고 `amx-claude`를 exec한다(래퍼가
같은 이름의 shim으로 재귀하지 않게). 실제 바이너리는 건드리지 않아(rename 안 함)
Claude Code 자체 업데이트에 투명하다.

### 설치
```sh
bash deploy/install-runner-guard.sh          # 기본 /usr/local/bin
GUARD_BIN_DIR=/opt/bin bash deploy/install-runner-guard.sh   # 설치 위치 변경
```
멱등(재실행 안전)·fail-loud. 실제 `claude` 미발견, guard 디렉터리가 PATH에서 실제
claude보다 앞서지 않음(강제가 무효가 됨), 쓰기 불가 시 비0으로 중단한다. PATH 순서
경고를 무시해야 할 특수 상황은 `GUARD_ALLOW_UNORDERED=1`로 우회한다.

한 번의 실행이 `claude`와 `codex` 두 진입점을 함께 처리한다. codex는 설치돼 있을
때만 shim을 깔고, 없으면 `skip codex: not installed on this host`를 찍고 넘어간다.
codex를 쓰는 호스트인데도 가드를 붙이고 싶지 않다면 `GUARD_SKIP_CODEX=1`을 준다.

### 검증 (설치 후 1회 + 주기 점검 겸용)
```sh
AMA_USER=ama bash deploy/verify-runner-guard.sh
# 또는 명시 경로:
AMA_CLAUDE_CONFIG_DIR=/home/ama/.claude bash deploy/verify-runner-guard.sh
```
(a) `claude`가 래퍼(shim/amx-claude)로 해석되는지, (b) 러너 계정과 AMA 서비스
계정이 **같은 `~/.claude`**(`CLAUDE_CONFIG_DIR`)를 보는지를 판정한다. 둘 다 성립해야
종료코드 0, 아니면 비0과 `[FAIL]` 진단. (b)는 AMA 계정 정보가 필요하므로
`AMA_USER` 또는 `AMA_CLAUDE_CONFIG_DIR` 중 하나를 반드시 준다. cron/systemd 타이머로
주기 실행해 드리프트(다른 HOME·컨테이너 마운트·stray `CLAUDE_CONFIG_DIR`, PATH 앞
다른 claude 재등장)를 감시한다.

### codex 러너

codex도 credential이 config home의 `auth.json` 한 장에 들어가므로 §2의 과금 창이
똑같이 열린다. 래퍼는 `deploy/amx-codex`, config home은 `CODEX_HOME`(없으면
`AMX_CODEX_HOME`, 그것도 없으면 `~/.codex`)이고, AMA와 러너가 같은 디렉터리를
가리켜야 flock이 성립하는 것도 claude와 같다.

판정 기준만 한 가지 다르다. claude는 이 호스트에 반드시 있어야 하지만 codex는
선택이라, PATH에 codex가 없으면 `[SKIP]`으로 넘기고 종료코드에 반영하지 않는다.
codex가 설치돼 있는데 AMA 쪽 디렉터리를 알려주지 않은 경우도 `[WARN]`에 그친다 —
codex 바이너리가 깔려 있다는 사실만으로 그 호스트가 Codex 계정을 받았다고 볼 수는
없기 때문이다. 반면 codex가 래퍼를 건너뛰어 해석되거나 두 디렉터리가 실제로 어긋나면
claude와 똑같이 `[FAIL]`이다. Codex 계정을 운영하는 호스트라면 아래처럼 codex 쪽
경로까지 넘겨 판정을 확정지어야 한다.

codex 쪽 AMA 경로는 `AMA_USER`로 유도하지 않는다. 에이전트는 `AMX_CODEX_HOME`에만
스테이징하고 `~/.codex` 폴백이 없어서, 양쪽 다 `~/.codex`로 추정하면 아무도 쓰지 않는
디렉터리끼리 비교해 정작 잡아야 할 어긋남을 통과시킨다. 그래서 codex는 명시 경로를
받을 때만 판정한다.

```sh
AMX_CODEX_HOME=/srv/codex-home \
AMA_CODEX_CONFIG_DIR=/srv/codex-home \
AMA_USER=ama \
  bash deploy/verify-runner-guard.sh
```

러너 쪽 `CODEX_HOME`(또는 `AMX_CODEX_HOME`)과 AMA 쪽 `AMA_CODEX_CONFIG_DIR`을 같은
값으로 주면 `[ OK ]`, 다르면 `[FAIL]`, 한쪽이라도 비어 있으면 판정 불가라 `[WARN]`으로
남기고 종료코드는 건드리지 않는다.

서버 한 대는 Codex 계정을 하나만 받는다. auth.json이 한 장뿐이라 두 번째 배정은
첫 계정을 덮어쓰기 때문이고, AMS가 배정 생성 시점에 409
(`assignment.server_codex_capacity`)로 막는다. 회수는 claude와 달리 항상 완전 삭제로
나간다(`purge_local_copy=true`). Codex 브리지가 계정 신원을 사이드카 파일에 남기는데,
비활성화만 하면 그 파일이 남아 다음 다른 계정 배달을 `codex_single_account`로 거부해
호스트가 첫 계정에 영구히 묶인다.

### 셀프테스트
```sh
bash deploy/test-runner-guard.sh   # docker·root 불필요
```
임시 HOME·가짜 claude로 강제 성립/우회 검출을 각각 확인한다.

### cron/systemd 주의
guard 디렉터리가 해당 실행 환경의 PATH에 있어야 shim이 선택된다. systemd 기본
PATH에는 `/usr/local/bin`이 포함되지만, cron 기본 PATH(`/usr/bin:/bin`)에는 없으므로
crontab에 `PATH=/usr/local/bin:/usr/bin:/bin` 줄을 추가하거나 guard를 cron PATH에
있는 디렉터리(`/usr/bin` 등, `GUARD_BIN_DIR`)에 설치한다. 설치 후 그 환경에서
`verify-runner-guard.sh`로 실제 강제를 확인한다.

### 잔여 우회 경로 (설계상 한계)
shim은 `claude`라는 **이름 해석**만 강제한다. 실제 바이너리를 절대경로로 직접
호출하거나(`/path/to/real/claude`), guard보다 앞에 다른 `claude`를 심거나, PATH에서
guard 디렉터리를 제거하면 우회된다. 이는 이름 기반 강제의 본질적 한계이며,
주기 `verify`가 (a)(b) 드리프트를 감시하는 이유다. 이 창의 잔여 노출은 층 1(B1a
sub-second·torn-free)이 방어한다.

## 8. Langfuse 추적 (P3, 선택)

러너 세션을 셀프호스팅 Langfuse로 흘려보내 세션 단위로 관찰한다. `amx-claude`
**래퍼를 거쳐 뜬 세션만** 추적되고, 설정 파일이 없는 서버는 동작이 이전과 완전히
같다 — 추적을 붙이지 않은 호스트에는 아무 부작용이 없다.

### 어떻게 래퍼 경유만 추적되나
추적은 두 조각으로 나뉜다. Stop 훅(`settings.json`에 등록, `deploy/langfuse/`에서
복사)은 세션이 끝날 때마다 돌지만, 환경에 Langfuse 키가 없으면 아무 일도 하지 않고
빠진다. 키를 환경에 넣어 주는 것은 오직 `amx-claude` 래퍼뿐이다. 래퍼는 실행 직전
`$CLAUDE_CONFIG_DIR/amx-langfuse.env`가 있으면 읽어들이고, 그 안에
`TRACE_TO_LANGFUSE=true`가 켜져 있을 때만 키를 export한다. 그래서 래퍼를 거치지
않고 뜬 세션(실바이너리 직접 호출 등)은 키를 못 받아 훅이 그대로 무동작이다.
파일 파싱 오류·`tsamx` 부재·조회 타임아웃 어느 것도 claude 기동을 막지 않는다(래퍼
기존 fail-open 유지).

### 수집 방침·전제
프롬프트·응답 페이로드를 **전량 수집**한다(민감정보 마스킹 없음). 내부망·신뢰
경계 안 전용이라는 전제이며, 인터넷에 노출된 Langfuse에는 붙이지 않는다. 페이로드
길이는 `CC_LANGFUSE_MAX_CHARS`(기본 20000자)로 자른다.

### 귀속
- **계정 귀속**(`LANGFUSE_USER_ID`): env 파일에 직접 박지 않으면, 래퍼가
  `tsamx status --json`의 활성 계정 이메일(`.active.email`)로 자동 채운다(상한 2초,
  실패 시 미설정).
- **서버 귀속**(`LANGFUSE_TRACING_ENVIRONMENT`): 비어 있으면 `$(hostname)`.

### 설치
```sh
LANGFUSE_BASE_URL=http://<langfuse-host>:3100 \
LANGFUSE_PUBLIC_KEY=pk-... \
LANGFUSE_SECRET_KEY=sk-... \
  sh deploy/install-langfuse-hook.sh
```
멱등(재실행 시 `settings.json` 불변)이다. 설정 홈은
`--config-dir` > `$CLAUDE_CONFIG_DIR` > `~/.claude-amx`(존재 시) > `~/.claude`
순으로 정해지고, 고른 경로를 설치 로그에 찍는다 — tsamx·`amx-claude`와 같은 홈을
써야 하기 때문이다(러너 홈 관례는 `deploy/agent-setup.sh` 참조). 복사 전 vendored
훅을 `deploy/langfuse/SHA256SUMS`와 대조해 불일치면 중단한다. 이어 훅을
`<설정 홈>/hooks/`로 복사하고, `settings.json`의 Stop 훅에 `uv run --script <훅>`
항목을 병합하며(기존 다른 키·훅 보존), `amx-langfuse.env`(0600)를 만든다. 기존 env
파일이 있으면 `.bak`로 백업한 뒤 덮어쓴다. 훅 실행에 `uv`가 **필수**라 없으면 설치가
오류로 멈춘다(https://docs.astral.sh/uv/). 설치 끝에 훅을 비활성 상태로 1회 돌려 uv
의존성 캐시를 미리 채운다 — 오프라인 호스트에서 첫 Stop 훅이 PyPI 해석에 실패하는 것을
막기 위함이고, 이 워밍 실패는 경고로만 남긴다.

> 시크릿 전달 시 노출 주의: 키를 명령줄 인자(`--secret-key ...`)나 환경변수로 주면
> `ps`·셸 히스토리에 남을 수 있다. 공유 호스트에서는 설치 셸의 히스토리를 끄거나,
> 설치 직후 셸을 정리한다. 기록된 최종 자격증명은 `amx-langfuse.env`(0600) 한 곳에만
> 둔다.

### 끄는 법
```sh
sh deploy/install-langfuse-hook.sh --uninstall
```
`amx-langfuse.env`를 지우고 `settings.json`에서 이 훅 항목만 뺀다. env 파일만 지워도
래퍼가 키를 export하지 않아 추적이 멈춘다(훅은 남아 있어도 무동작).

### 운영 주의
- **세션 도중 계정 전환**: `LANGFUSE_USER_ID`는 래퍼가 **기동 시점**의 활성 계정
  이메일로 한 번 정해 고정한다. 세션이 떠 있는 동안 deliver로 활성 계정이 바뀌어도
  그 세션의 추적 귀속은 기동 시점 계정 그대로다(세션 단위 정확도의 한계). 정확한
  귀속이 필요하면 env 파일에 `LANGFUSE_USER_ID`를 명시적으로 박아 쓴다.
- **키 로테이션**: Langfuse에서 새 키를 발급한 뒤 같은 설치 명령을 다시 돌리면
  `amx-langfuse.env`가 갱신되고 기존 파일은 `.bak`로 남는다. **`.bak`에는 구
  시크릿이 평문으로 남으므로** 로테이션 확인 후 `amx-langfuse.env.bak`을 지운다
  (`shred -u` 권장). 이미 실행 중인 세션은 기동 시 읽은 구 키를 계속 쓴다 —
  로테이션은 다음 기동부터 적용된다.
- **Langfuse 서버 장애**: Stop 훅은 서버가 죽어 있거나 키가 틀려도 예외를 삼켜
  `exit 0`으로 끝나고 실패는 훅 로그 파일에만 남는다(코드 확인: `main()`이
  네트워크·클라이언트 생성 실패를 잡아 `return 0`). 따라서 Langfuse 장애가 Claude
  세션 종료나 동작을 막지 않는다. 추적만 조용히 누락된다.
