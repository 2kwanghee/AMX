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
