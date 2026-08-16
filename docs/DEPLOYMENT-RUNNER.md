# 러너 무중단 배포 (O5 / B1b)

deliver가 credential을 교체하는 동안 러너(Claude Code)가 신규 계정으로 과금되지
않도록 하는 배포 가이드. AMA 측 B1a(창 최소화)와 B1b(flock 조율)의 2층 방어를
설명하고, 참조 래퍼 `deploy/amx-claude`의 설치법을 다룬다(§1~§7). 이어 러너 세션을
셀프호스팅 Langfuse로 관측하는 Stop 훅과 함대 일괄 배포(§8), 개인 작업과 러너를 한
대에서 겸용하는 PC의 프로필 부트스트랩·`amx` 명령(§9)까지 다룬다.

## 1. 러너 모델 (두 경로)

같은 서버에서 Claude Code(=러너)가 두 경로로 기동되며, **둘 다 기동 시점에
`$CLAUDE_CONFIG_DIR/.credentials.json`을 읽어** 그 계정으로 과금한다.

1. **사용자 터미널**: 사용자가 직접 `claude`를 실행(대화형, 장수명).
2. **ClickEye webhook**: 터널로 들어온 요청이 진입점에서 `claude -p <명령>`을
   실행(배치, 단수명).

두 경로 모두 **임의 시점에** 기동될 수 있다 — AMA의 deliver와 시간적으로 겹칠 수
있다는 것이 문제의 핵심이다.

> tsamx는 에이전트 설치·자기갱신과 함께 갱신된다. `deploy/agent-setup.sh install`은
> `uv tool install --force`로 체크아웃 기준 재설치하고, 에이전트 self_update도 ama
> 바이너리 스왑 뒤 같은 방식으로 tsamx를 재설치한다. 그래서 저장소의 tsamx 변경이
> 러너에 자동 반영되며(BACKLOG G48 해소), 함대(fleet) 재배포 때도 별도 tsamx 갱신
> 절차가 필요 없다.

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

- **사용자 터미널**: 대화형 셸에서 alias 권장(러너 **전용** 서버 전제).
  ```sh
  alias claude=amx-claude    # ~/.bashrc, ~/.zshrc 등
  ```
  개인 작업과 러너를 한 대에서 겸용하는 PC라면 `claude`를 래퍼에 alias하지 말고 §9의
  `amx` 명령을 쓴다 — alias를 걸면 개인 세션까지 deliver 락에 묶인다.
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

### 위험명령 감지 훅 (선택, 기본 off)
같은 Stop 훅 설치 흐름에 위험명령 감지 훅을 얹을 수 있다. Claude Code가 Bash를
실행하기 직전(PreToolUse) `deploy/langfuse/danger_hook.py`가 돌면서 명령을 보수적인
위험 패턴 목록과 대조하고, 걸리면 AMS로 통보한다. AMS는 이를 `dangerous_command`
경보로 올리고 웹훅으로 흘린다.

핵심은 이 훅이 **명령을 막지 않는다**는 점이다. 감지와 통보만 하고 언제나 `exit 0`으로
끝나므로 Claude의 동작은 그대로다. 통보 HTTP는 2초 타임아웃이고 실패는 조용히
삼키기 때문에 세션을 느리게 하거나 실패시키지도 않는다. 원문 명령도 나가지 않는다 —
sha256 다이제스트와, 패턴에 걸린 키워드만 남기고 나머지를 별표로 가린 축약본(200자
이내)만 보낸다.

설치는 `--with-danger-hook` 한 플래그를 붙이면 된다.
```sh
LANGFUSE_BASE_URL=http://<langfuse-host>:3100 \
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... \
  sh deploy/install-langfuse-hook.sh --with-danger-hook
```
훅을 `<설정 홈>/hooks/`로 복사하고 `settings.json`의 PreToolUse에 `Bash` matcher 항목을
멱등 병합한다. 이 훅은 의존성이 없어 `python3`로 바로 돌고 uv나 체크섬 검증을 타지
않는다(vendored `langfuse_hook.py`와는 별개 파일). 다만 복사만으로는 아무 일도 하지
않는다. 통보 대상은 `amx-langfuse.env`의 두 값으로 정하며, 둘 다 없으면 훅은 무동작이다.
```sh
AMX_DANGER_INGEST_URL='http://<ams-host>:8080/api/v1/ingest/danger-command'
AMX_DANGER_INGEST_TOKEN='<AMS의 danger_ingest_token과 동일>'
```
기본 패턴은 재귀 강제 삭제(`rm -rf` 계열), 권한 상승(`sudo`), 파일시스템 포맷(`mkfs`),
블록 디바이스로의 `dd`, `chmod -R 777`, 네트워크에서 받아 셸로 바로 실행하는
`curl|sh`, main/master로의 강제 push를 잡는다. `CC_DANGER_PATTERNS_FILE`(줄당 정규식,
파일 권한 0600 필수)로 늘릴 수 있다.

오탐 한계는 분명히 해 둔다. 정규식은 셸을 파싱하지 않으므로 인용 문자열이나 주석 안에
든 위험 문자열을 잘못 잡을 수 있고, 난독화한 명령은 놓칠 수 있다. 단어 경계와 명령
구분자로 오탐을 줄이되 완벽히 막지는 못한다. 이 훅은 조기경보이지 방어선이 아니다.

끄려면 `--uninstall`을 돌린다. 플래그 없이 돌려도 Stop 훅과 danger 훅 항목을 함께
걷어낸다.

### 함대(fleet) 일괄 배포
호스트가 몇 대 넘어가면 서버마다 손으로 설치 명령을 치는 방식은 오래 못 간다. 신규
에이전트는 설치 때 자동으로 켜지게 하고, 이미 떠 있는 호스트는 목록 한 장으로 한꺼번에
켜고 끄는 두 갈래를 둔다.

**신규 에이전트 자동 적용.** `deploy/agent-setup.sh install`은 `AMX_LANGFUSE_BASE_URL`,
`AMX_LANGFUSE_PUBLIC_KEY`, `AMX_LANGFUSE_SECRET_KEY` 세 환경변수가 **모두** 있을 때만
설치 끝에 `install-langfuse-hook.sh`를 같은 설정 홈에 심는다. 하나라도 비면 훅에 손을
대지 않아 기존 설치 흐름이 그대로다. 훅 설치가 실패해도 경고만 남기고 에이전트 설치는
계속 진행한다.

```sh
AMX_LANGFUSE_BASE_URL=http://<langfuse-host>:3100 \
AMX_LANGFUSE_PUBLIC_KEY=pk-... \
AMX_LANGFUSE_SECRET_KEY=sk-... \
  deploy/agent-setup.sh install --ams HOST:PORT --token T --pubkey K --insecure
```

**기존 호스트 일괄 on/off/status.** `deploy/fleet-langfuse.sh`가 호스트 목록을 받아 ssh로
각 서버에서 설치(`on`)·회수(`off`)·조회(`status`)를 돌린다. 목록은 한 줄에 `user@host`
하나씩 적고, 빈 줄과 `#` 주석은 건너뛴다. 기본 경로는 `deploy/fleet-hosts.txt`이며
`--hosts`로 바꿀 수 있다. 실호스트가 든 파일은 커밋하지 말고, `deploy/fleet-hosts.txt.example`을
복사해 채운다.

시크릿은 명령줄에 직접 타이핑하지 말고 0600 env 파일에 담아 소싱한다. `on`은 값을
원격 명령의 stdin으로만 흘려 로컬·원격 어느 쪽 `ps`(argv)에도 시크릿을 남기지 않지만,
그건 어디까지나 프로세스 인자 이야기다. `LANGFUSE_SECRET_KEY=sk-... sh fleet-langfuse.sh on`
처럼 셸에 직접 치면 그 한 줄이 통째로 `~/.bash_history`에 남는다. env 파일로 우회하면
그 누출 경로가 사라진다.

```sh
# secrets.env (chmod 600) — 커밋 금지
cat > secrets.env <<'ENV'
export LANGFUSE_BASE_URL=http://<langfuse-host>:3100
export LANGFUSE_PUBLIC_KEY=pk-...
export LANGFUSE_SECRET_KEY=sk-...
ENV
chmod 600 secrets.env

. ./secrets.env && sh deploy/fleet-langfuse.sh on   # 히스토리에 키가 남지 않음
. ./secrets.env && sh deploy/fleet-langfuse.sh on --with-danger-hook  # 위험명령 훅까지 함께

sh deploy/fleet-langfuse.sh off       # 전 호스트에서 추적 회수
sh deploy/fleet-langfuse.sh status    # 호스트별 amx-langfuse.env 존재 여부
```

`on`에 `--with-danger-hook`을 붙이면 원격 `install-langfuse-hook.sh`에 그대로 전달돼
위험명령 감지 PreToolUse 훅까지 함께 깐다(기본은 off, Stop 훅만). 훅을 실제로 무장하려면
각 호스트의 `amx-langfuse.env`에 `AMX_DANGER_INGEST_URL`/`AMX_DANGER_INGEST_TOKEN`을
채워야 한다. `off`는 각 호스트에서 `install-langfuse-hook.sh --uninstall`을 돌려 env
파일과 Stop·danger 훅 항목을 걷어낸다. `status`는 자격증명이 필요 없고, `--config-dir`을 주지 않으면
`~/.claude-amx` → `~/.claude` 순으로 첫 env 파일을 찾아 보고한다. 다만 `status`는
**`amx-langfuse.env` 파일이 있는지만** 본다 — `settings.json`의 Stop 훅 등록 여부나 키가
실제로 유효한지는 확인하지 않으므로, 켜짐 표시가 곧 추적이 도는 증거는 아니다.

원격 호스트의 AMX 체크아웃 경로는 기본 `$HOME/AMX`로 잡고, 다르면 `--remote-repo`로
지정한다(`on`·`off`에만 쓰인다. `status`는 원격 저장소 없이도 돈다). 개발용으로
`~/AMX-agent`에 따로 체크아웃한 dev 호스트는 `--remote-repo ~/AMX-agent`를 붙여야 한다. ssh는
`BatchMode=yes ConnectTimeout=5`로 붙어 암호 프롬프트에 걸려 멈추지 않는다. 한 호스트가
실패해도 나머지는 계속 돌고, 마지막에 성공·실패 수와 실패 호스트를 집계한 뒤 실패가 하나라도
있으면 종료코드 1로 끝난다.

## 9. 겸용 PC 프로필 가이드

한 대의 PC를 개인 작업과 AMX 러너가 나눠 쓰는 경우가 있다. 개인 세션은 Claude 기본
프로필(`~/.claude`)을, 러너는 별도 프로필(`~/.claude-amx`)을 쓰는데, tsamx가 계정을
넣고 빼는 대상이 후자다. 문제는 개인 프로필에 쌓아 둔 전역 지침(`CLAUDE.md`)·스킬·
키바인딩·에이전트·명령·프로젝트 메모리가 러너 프로필에서는 보이지 않는다는 점이다.
같은 사람이 같은 PC에서 일하는데 amx 세션만 맨몸으로 뜨는 셈이다.

`deploy/bootstrap-profile.sh`가 이 간극을 메운다. 개인 프로필의 유저 스코프 자산을
러너 프로필에서 심볼릭 링크로 참조하게 걸어, 두 프로필이 같은 지침·스킬·메모리를
공유하도록 한다. 멱등하게 짜여 있어 여러 번 돌려도 같은 상태로 수렴한다.

### 무엇이 공유되고 무엇이 분리되나

공유되는 것은 유저 스코프 자산이다. `CLAUDE.md`, `skills/`, `keybindings.json`,
`agents/`, `commands/`, 그리고 개인 프로필의 프로젝트 메모리(`projects/<slug>/memory`)를
러너 쪽에서 링크로 건다. 개인 프로필에서 규칙을 고치면 amx 세션에도 그대로 반영된다.

단, 프로젝트 메모리는 선택제다. `bootstrap-profile.sh`에 `--no-memory`를 주면
메모리 링크 단계 전체를 건너뛴다. 무인 러너 자동 경로(§ "자동 실행과 안전 규약")는
이 옵션을 기본으로 걸어 메모리를 아예 링크하지 않으며, 나머지 자산만 공유한다.

분리되는 것은 세 가지다. 계정 자격증명(`.credentials.json`)은 tsamx가 러너 프로필에서
독립으로 관리하므로 개인 로그인과 섞이지 않는다. 대화 이력도 프로필별로 따로 쌓인다.
`settings.json`은 링크하지 않고 러너 쪽에 없을 때만 한 번 복사한다. AMX Langfuse 훅이
이 파일에 Stop 훅 항목을 병합 기록하는데, 링크로 걸어 두면 그 기록이 개인 원본을
오염시키기 때문이다. 러너 쪽에 이미 `settings.json`이 있으면 손대지 않고 넘어간다.

### 공유의 대가 — 격리가 아니라 연결이다

"공유"는 편의지만 격리를 무너뜨린다. 링크를 걸기 전에 세 가지를 감수할지 판단해야
한다. 첫째, 프로젝트 메모리 링크는 한 방향이 아니라 **양방향 write 공유**다. 러너의
자동화 세션이 `projects/<slug>/memory`에 뭔가를 쓰면 그 내용이 개인 프로필의 실제
메모리 파일에 그대로 반영된다. 러너가 개인 메모리를 오염시킬 수 있다는 뜻이다.
개인 메모리를 통째로 빼려면 `--no-memory`로 부트스트랩해 메모리 링크 단계를 아예
건너뛰고(무인 러너 자동 경로의 기본값), 특정 프로젝트만 격리하려면 그 slug만
링크하지 말고, 이미 걸렸으면 `--uninstall`로 끊는다. 둘째, 링크된 개인 지침(`CLAUDE.md`)과 스킬은 amx 세션의
실제 동작을 바꾼다 — 개인 규칙이 러너 자동화에 그대로 적용된다. 셋째, 개인 메모리
내용은 러너 세션이 읽어 §8의 Langfuse 트레이스로 나갈 수 있다. 민감한 개인 메모리가
추적 서버에 남는 게 곤란하면 해당 slug를 공유에서 제외한다.

### 러너 전용 서버와 겸용 PC는 설정이 다르다

`docs/PROD-GUIDE.md` §8-2는 러너 **전용** 서버를 전제로 `export CLAUDE_CONFIG_DIR=~/.claude-amx`를
셸 전역에 걸고 `alias claude="$HOME/AMX/deploy/amx-claude"`로 `claude`를 래퍼에
묶으라고 권한다. 그 서버에는 개인 프로필이 없으니 `claude`가 곧 러너다.

겸용 PC에서는 이 두 설정을 쓰면 안 된다. 전역 `export CLAUDE_CONFIG_DIR`을 걸면
개인 `claude`까지 러너 프로필로 끌려가 개인 작업이 러너 계정으로 뜨고, `claude`를
래퍼에 alias하면 개인 세션이 deliver 락에 묶인다. 겸용 PC의 규칙은 반대다. 전역
`export`를 걸지 않아 `claude`는 개인 프로필로 그대로 두고, 러너로 띄울 때만 아래
`amx` 명령을 쓴다. 즉 두 방식은 상호 배타다 — 한 대에서 섞지 않는다.

### amx 명령

전환은 별도 명령 `amx`로 한다. 기존 `claude`는 그대로 두고 개인 프로필로 뜬다.

```sh
amx                     # 대화형, 러너 프로필(~/.claude-amx)로 기동
amx -p "작업 지시"       # 배치, claude -p 와 동일
```

`amx`는 `CLAUDE_CONFIG_DIR`을 러너 프로필로 잡은 뒤 `amx-claude` 래퍼로 넘긴다. 즉
deliver 락 조율(B1b)과 Langfuse 추적은 §4·§8에서 설명한 래퍼가 그대로 담당하고,
`amx`는 프로필만 갈아 끼운다. 프로필 경로를 바꾸려면 `AMX_CONFIG_DIR`을 준다.
`bootstrap-profile.sh`는 `~/.local/bin`이 있으면 여기에 `amx`를 복사하며, 이 디렉터리가
PATH에 없으면 안내를 출력한다.

### 자동 실행과 안전 규약

`deploy/agent-setup.sh install`은 개인 프로필(`~/.claude`)이 있고 그 안에 `CLAUDE.md`·
`skills`·`projects` 중 하나라도 있으면 부트스트랩을 자동으로 돌린다. 개인 프로필이
없으면 경로 전체가 그대로 지나가고, 부트스트랩이 실패해도 경고만 남긴 채 에이전트
설치는 계속된다.

이 자동 경로는 `--no-memory`를 기본으로 붙여 부트스트랩한다. 무인 러너가 개인
프로필의 프로젝트 메모리에 write해 오염시키거나 그 내용을 Langfuse 트레이스로
흘리는 표면(BACKLOG G40)을 없애기 위해서다. 따라서 자동 설치로는 `CLAUDE.md`·스킬·
키바인딩·에이전트·명령만 링크되고 메모리는 링크되지 않는다. 겸용 PC라 개인 메모리까지
공유하고 싶으면 설치 뒤 `deploy/bootstrap-profile.sh`를 옵션 없이 수동으로 한 번 더
돌린다(옵트인). 자동 설치 로그에도 이 안내가 한 줄 남는다.

부트스트랩 스크립트는 러너 홈 안에만 파일(링크)을 만든다. tsamx가 만드는 세션
프로필이나 개인 프로필 내부에는 어떤 파일도 쓰지 않는다 — 개인 프로필은 스크립트
실행 시점 기준으로 읽기 전용이다(설치 뒤 런타임의 메모리 양방향 write는 위
"공유의 대가" 참고). 이유는 tsamx의 history 병합(`session.py`의
`_merge_history_into_source`)이 `shutil.move`로 프로필 `projects/`를 통째로 옮기기
때문이다. 그 경로 안에 심링크가 있으면 원본이 딸려
사라질 위험이 있는데, 러너 홈은 이 이관 경로 밖이라 안전하다. 러너 쪽에 이미 실제
파일이나 비어 있지 않은 메모리 디렉터리가 있으면 사용자 데이터로 보고 건드리지 않고
경고만 낸다. 제거는 `bootstrap-profile.sh --uninstall`로 하며, 이 스크립트가 만든
링크만 걷어내고 복사본·실파일은 남긴다.

### project scope 자산과 알려진 한계

프로젝트 루트의 `.claude/`(project scope) 자산은 프로필과 무관하게 동작한다. Claude
Code가 작업 디렉터리 기준으로 읽으므로, `claude`로 열든 `amx`로 열든 같은 저장소에서는
같은 project scope 설정이 적용된다. 이 가이드가 다루는 링크는 유저 스코프에만
해당한다.

한 가지 한계가 있다(BACKLOG G36). tsamx가 세션마다 만드는 임시 세션 프로필에는 이
부트스트랩이 손대지 않으므로, 그 프로필로 뜬 세션에는 Langfuse Stop 훅이 걸려 있지
않다. 다만 이 공백은 **사람이 훅 설치 호스트에서 `tsamx run`을 직접 칠 때만** 나타난다.
자동 러너(AMA)는 계정 관리 verb만 호출하고 세션은 `amx`/`amx-claude`를 거치므로
해당하지 않고, 러너 홈(`~/.claude-amx`)을 그대로 쓰는 `amx` 경유 세션도 §8의 훅
설치가 그대로 적용된다. 운영 표준은 `amx`이며 `tsamx run`은 이 경로로 채택하지 않는다.

그래서 `tsamx run`은 러너 홈에 `amx-langfuse.env`가 있으면(=이 호스트가 추적 대상이면)
"이 세션은 Langfuse 추적에서 제외됩니다 — 추적하려면 amx 명령을 사용하세요"를 stderr로
한 줄 띄운다. 세션 자체는 그대로 뜨고, 경고는 추적 공백을 조용히 넘기지 않으려는 안내다.
