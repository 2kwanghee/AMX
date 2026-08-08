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

### 층 2 — B1b: flock 조율 (래퍼 설치 시 창 완전 차단)
- **AMA**: deliver 크리티컬 섹션 전체 동안 `$CLAUDE_CONFIG_DIR/.amx-deliver.lock`에
  **배타 락**(`flock LOCK_EX`, `internal/tsamx/exec.go` `DeliverLock`,
  `handleDeliver`에서 획득·해제)을 잡는다. 엔진 락(AMA 내부 직렬화)과 별개로,
  이 flock은 **러너 프로세스와의 프로세스 간 조율** 전용이다.
- **러너 래퍼(`deploy/amx-claude`)**: `claude`를 exec 하기 전에 같은 파일에
  **공유 락**(`flock -s`)을 잡는다. 공유 락은 진행 중인 deliver(배타)가 끝날
  때까지 대기하므로, 러너의 startup read가 크리티컬 섹션과 **겹치지 않는다**.
- **락이 창을 닫는 근거**: AMA가 `LOCK_EX`를 쥔 구간(기록~add~복귀) 동안 래퍼의
  `LOCK_SH` 요청은 커널에서 블록된다 → 러너의 credential read는 반드시 복귀 완료
  **후**에 일어난다. 공유끼리는 블록하지 않으므로 러너 다중 기동은 직렬화되지 않는다.

lock 파일은 credential 파일과 **분리**되어 있어 claude가 절대 건드리지 않는다.

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

## 6. 트레이드오프 (운영 주의)

참조 래퍼는 공유 락을 **claude 프로세스 수명 동안** 유지한다(가장 단순하고
레이스 없는 형태). 함의:

- 다중 러너는 공유 락이라 서로 직렬화되지 않는다(동시 기동 OK).
- 반면 AMA의 **deliver(배타)는 실행 중인 러너가 모두 락을 놓을 때까지 대기**한다.
  `-p` 배치는 단수명이라 대기가 짧다. 장수명 대화형 세션이 상시 떠 있으면 deliver
  지연이 길어질 수 있다.
- 완화: deliver는 운영자/AMS 발행으로 드물고 크리티컬 섹션은 sub-second다. 상시
  대화형 부하에서 deliver 지연을 없애려면 층 1(B1a)만으로도 노출은 sub-second이므로
  래퍼를 배치 경로에만 적용하는 선택이 가능하다. startup 구간에만 락을 한정하는
  정교화(러너 기동 후 조기 해제)는 후속 배포 설계 과제로 둔다.
