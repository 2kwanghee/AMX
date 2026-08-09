# tsamx 설치 인증 배포 가이드 (B2 / O10)

tsamx는 프라이빗 AMX 모노레포의 서브디렉터리(`tsamx/`)에서 git으로 설치된다
(`TSAMX-GUIDE.md` §2, D11 확정 — 별도 레포 분리 안 함). 프라이빗 레포이므로
각 AMA 서버는 **git이 인증**되어야 설치·업데이트할 수 있다. 이 문서는
**내재화 1차** 규모(소수 서버)에서의 인증 방식 — 읽기 전용 deploy key 공용 —
과 서버측 git 설정, 설치·업데이트 흐름, 그리고 확장 트리거를 정리한다.

## 1. 인증 계층 선택 — 왜 읽기 전용 deploy key 공용인가

| 방식 | 대상 규모 | 특성 |
|---|---|---|
| **읽기 전용 deploy key 공용** | 내재화 1차(서버 소수) | 레포당 1개 키, 읽기 전용. 서버들이 같은 공개키를 공유 |
| 서버별 deploy key / machine user | 서버 N대 초과 | 키 회수 단위가 서버. machine user는 다중 레포 접근 |
| AMS wheel 아티팩트 서빙 | 조직 이전·규모화 | GitHub 의존 제거, AMS가 빌드 산출물 배포 |

1차는 최소 구성을 택한다: **레포에 읽기 전용 deploy key 1개를 등록**하고,
그 개인키를 소수 서버가 공유한다. 쓰기 권한이 없어 유출 시 손해가 읽기로
한정되고, 코드 push 경로가 서버에 존재하지 않는다. 확장 트리거는 §4.

## 2. GitHub deploy key 발급·등록 (읽기 전용)

1. 배포 워크스테이션에서 전용 키쌍 생성(패스프레이즈 없음 — 무인 설치용):
   ```sh
   ssh-keygen -t ed25519 -N "" -C "amx-tsamx-deploy" -f ./amx_tsamx_deploy
   ```
2. GitHub 레포 → **Settings → Deploy keys → Add deploy key**.
   - Title: `amx-tsamx-deploy (read-only)`
   - Key: `amx_tsamx_deploy.pub` 내용 붙여넣기
   - **"Allow write access" 는 체크하지 않는다**(읽기 전용 강제).
3. 개인키 `amx_tsamx_deploy`(공개키 아님)를 각 AMA 서버로 안전 채널로 전달한다
   (예: `scp`, 비밀 관리 도구). 저장소 히스토리·로그에 남기지 않는다.

## 3. 서버측 git 설정

키는 `ama` 서비스 계정 홈에 배치하고 권한을 조인다:

```sh
install -d -m 0700 ~/.ssh
install -m 0600 amx_tsamx_deploy ~/.ssh/amx_tsamx_deploy   # 개인키 0600 필수
```

`~/.ssh/config`에 이 레포 전용 Host 별칭을 만들어 키를 바인딩한다(다른 GitHub
접근과 격리):

```
Host amx-github
    HostName github.com
    User git
    IdentityFile ~/.ssh/amx_tsamx_deploy
    IdentitiesOnly yes
```

`known_hosts`를 미리 고정해 최초 연결 시 대화형 프롬프트(무인 설치 실패 원인)를
없앤다:

```sh
ssh-keyscan github.com >> ~/.ssh/known_hosts
```

(확인 필요) GitHub SSH 호스트키 지문은 GitHub 공식 문서의 값과 대조해 MITM을
배제하는 것이 안전하다.

## 4. tsamx 설치·업데이트 흐름

설치 방식은 `TSAMX-GUIDE.md` §2와 일치시킨다 — 운영은 반드시 **태그 핀**:

```sh
# §3의 Host 별칭(amx-github)을 SSH URL에 사용한다.
uv tool install "git+ssh://git@amx-github/<org>/AMX.git@<태그>#subdirectory=tsamx"

# 업데이트: 레포 태그 상향 후 각 서버에서
uv tool upgrade tsamx        # 또는 위 install 명령을 새 태그로 재실행
```

설치 검증:

```sh
tsamx --help
tsamx list --json            # AMA가 소비하는 스키마 확인(UPSTREAM-SYNC.md §3)
```

(확인 필요) AMA 데몬은 tsamx를 PATH에서 찾거나 `AMX_TSAMX_BIN`으로 지정된
경로에서 찾는다(`ama-agent/internal/tsamx/exec.go` `EnvBinary`). uv tool 설치
경로(`~/.local/bin`)가 데몬 PATH에 없으면 `AMX_TSAMX_BIN`을 명시한다.

> 주의(TSAMX-GUIDE.md §2): 기존 `cswap`과 **동시 사용 금지** — 같은 `~/.claude`
> 라이브 파일을 조작한다. AMA 서버에는 tsamx만 설치한다.

## 5. 확장 트리거 조건 (1차 이후, 보류 아님 — 조건 기록)

아래 조건에 **도달하면** 다음 단계로 이행한다(BACKLOG B2 서술과 일치):

- **서버 N대 초과 또는 조직 이전 시** → 공용 키를 폐기하고 **서버별 deploy key
  또는 machine user**로 전환한다. 키 회수·감사 단위가 서버가 되어, 한 서버
  유출이 전체 설치 경로를 오염시키지 않는다. machine user는 다중 레포 접근이
  필요할 때 택한다.
- **그 다음 단계** → **AMS wheel 아티팩트 서빙**으로 GitHub 의존을 제거한다.
  AMS가 tsamx 빌드 산출물(wheel)을 배포하고 서버는 내부 인덱스에서 설치한다.

각 전환은 별도 배포 설계 턴에서 다룬다(현 문서 범위 밖).
