# tsamx — cswap 내재화 결과 및 사용 가이드

---
title: tsamx 내재화 가이드
origin: claude-swap v0.25.0b1 (MIT, https://github.com/realiti4/claude-swap) 포크
status: 내재화 완료 (2026-08-07, 테스트 1824/1824 통과)
location: /mnt/c/workspace/AMX/tsamx
---

## 1. 무엇이 되었나

- 패키지 `claude_swap` → `tsamx`, CLI `cswap`/`claude-swap` → `tsamx` 전면 rename (72개 파일).
- tsamx 소유 데이터 경로도 rename: 백업 루트 `$XDG_DATA_HOME/tsamx`, `.tsamx-backup`, 로그 `tsamx.log`, 세션 마커 `.tsamx-shared.json` 등.
- **Claude Code 자체 계약은 원형 유지** (rename 금지 영역): `CLAUDE_CONFIG_DIR`, `~/.claude.json`, `~/.claude/.credentials.json`, keyring 서비스 `claude-code`, OAuth client_id. 독립 리뷰어가 오치환 0건 확인.
- 업스트림 자체 업데이트 체크 비활성화 (`update_check.py` — tsamx는 PyPI에 없으므로).
- 라이선스: MIT 원본 LICENSE 유지 + NOTICE.md 포크 고지. 법적으로 rename·수정·사내 사용 모두 허용.

## 2. 설치·사용

```bash
# 개발 설치 (소스 수정이 즉시 반영됨)
uv tool install --editable /mnt/c/workspace/AMX/tsamx

# 배포 설치 (D11 확정: 모노레포 서브디렉터리 git 설치 — 별도 레포 분리 안 함)
# AMX 레포를 git에 올린 뒤, 운영은 반드시 태그 핀:
uv tool install "git+https://<host>/<org>/AMX.git@<태그>#subdirectory=tsamx"
# 프라이빗 레포는 SSH: git+ssh://git@<host>/<org>/AMX.git@<태그>#subdirectory=tsamx
# 업데이트: 태그 상향 후 각 서버에서 uv tool upgrade tsamx (또는 재설치)

tsamx --help
tsamx add            # 현재 로그인된 credential을 슬롯으로 캡처
tsamx list
tsamx 2              # 2번 계정으로 전환
tsamx auto on        # 자동 전환
```

기존 `cswap`과 병행 설치 가능하나 **동시 사용은 금지** — 둘 다 같은 `~/.claude` 라이브 파일을 조작한다. AMX 에이전트 서버에서는 tsamx만 설치할 것. 데이터 디렉터리가 다르므로(`claude-swap` vs `tsamx`) 기존 cswap 계정 슬롯은 자동 승계되지 않는다 — 필요 시 각 계정을 활성화한 상태에서 `tsamx add`로 재캡처.

테스트/수정 루프:

```bash
cd /mnt/c/workspace/AMX/tsamx && uv run pytest -q   # 1824 passed / 3 skipped 기준
```

주의: 9p 마운트(/mnt/c)에서 테스트가 느리면 ext4로 복사해 실행 (`~/amx-tsamx-build/tsamx`에 동일 사본 있음).

## 3. 커스텀 지점 (분석 결과)

### 3-a. 명령어 구조 변경
`cli.py`는 서브커맨드 파서가 아니라 **단일 ArgumentParser + 플래그 변환 레이어**다:
- `_SUBCOMMAND_FLAGS`(cli.py:50) + `_translate_subcommand`(cli.py:72)가 `tsamx list` 같은 동사를 `--list` 플래그로 재작성.
- `run/map/alias/auto/config`는 별도 사전 디스패치 파서.

소폭 변경(명령 추가·이름 변경)은 변환 테이블+플래그 그룹+하드코딩된 usage 문자열을 함께 수정. 명령 체계를 크게 바꾸려면 정식 `add_subparsers` 구조로 재작성이 안전 (cli.py에 국소적 T4, 단 기존 테스트가 `--flag` 인터페이스를 직접 구동하므로 테스트 동반 갱신 필요).

### 3-b. 테넌트 구조 도입
저장 포맷은 단일 `sequence.json`: `{activeAccountNumber, sequence[], accounts:{번호: AccountInfo}}`.
최소 변경 세트:
1. `models.py:78 AccountInfo`에 `tenant_id` 필드 + to_dict/from_dict, `AccountSnapshot`(models.py:123) 확장
2. `switcher.py`의 `list_accounts`(≈3921) / `switch`(≈4208) / `add_account`(≈2200)에 테넌트 필터
3. `transfer.py` export/import 스키마 확장
4. `autoswitch.py` 후보 선정에 테넌트 필터 반영

⚠ **선행 권장**: `switcher.py`(5.7k 라인)가 `sequence.json`을 40여 곳에서 직접 I/O한다. 테넌트 필터를 일관 적용하려면 스토어 접근 계층을 먼저 분리(T4, 사용자 승인 필요)하는 편이 누락 위험이 낮다.

### 3-c. 제거 후보 (AMX 에이전트 서버 기준)
`tui/` 전체(Textual 의존), `menubar.py`(rumps), `macos_keychain.py` + credentials.py의 Keychain 분기, `appearance.py`, `update_check.py`. `autoswitch`/`session`은 독립성이 좋아 유지 권장.

## 4. 유지비 (fork의 대가)

- **업스트림 추적 단절**: rename으로 향후 upstream diff 병합은 수동 작업. 원본 스냅샷을 `vendor/claude-swap-upstream`에 보존했으므로 업스트림 갱신 시 3-way 비교 가능.
- **Claude Code 비공개 계약 의존**: `~/.claude.json` 구조, credential 파일 위치, OAuth refresh 엔드포인트 등은 Claude Code 업데이트 시 자체 추적·수리 필요 (최대 유지비 항목).
- `autoswitch`(2.3k 라인)의 cooldown/quarantine 로직은 타이밍 민감 — 테넌트 필터 삽입 시 회귀 주의.

## 5. AMX 설계 문서와의 관계

AMX-DESIGN.md의 `cswap` 참조(§2, §3, §6.3 등)는 이제 `tsamx`로 치환 대상. 명령 인터페이스는 현재 1:1 호환(`add`, `switch`, `list`)이므로 설계 의미는 불변 — 명칭 일괄 갱신은 별도 문서 수정 턴에서 수행 권장.

## 6. 프라이빗 레포 설치 인증 (배포, B2 / O10)

tsamx는 프라이빗 AMX 모노레포의 서브디렉터리(`tsamx/`)에서 git으로 설치된다(§2, D11 확정 — 별도 레포 분리 안 함). 프라이빗 레포이므로 각 AMA 서버는 git이 인증돼야 설치·업데이트할 수 있다. 여기서는 **내재화 1차**(소수 서버) 기준으로 읽기 전용 deploy key를 공용하는 방식을 정리한다.

### 6-1. 왜 읽기 전용 deploy key 공용인가

| 방식 | 대상 규모 | 특성 |
|---|---|---|
| **읽기 전용 deploy key 공용** | 내재화 1차(서버 소수) | 레포당 1개 키, 읽기 전용. 서버들이 같은 공개키를 공유 |
| 서버별 deploy key / machine user | 서버 N대 초과 | 키 회수 단위가 서버. machine user는 다중 레포 접근 |
| AMS wheel 아티팩트 서빙 | 조직 이전·규모화 | GitHub 의존 제거, AMS가 빌드 산출물 배포 |

1차는 최소 구성을 택한다. 레포에 읽기 전용 deploy key 1개를 등록하고 그 개인키를 소수 서버가 공유한다. 쓰기 권한이 없어 유출돼도 손해가 읽기로 한정되고, 코드 push 경로가 서버에 존재하지 않는다. 확장 조건은 §6-4.

### 6-2. GitHub deploy key 발급·등록 (읽기 전용)

1. 배포 워크스테이션에서 전용 키쌍 생성(패스프레이즈 없음 — 무인 설치용):
   ```sh
   ssh-keygen -t ed25519 -N "" -C "amx-tsamx-deploy" -f ./amx_tsamx_deploy
   ```
2. GitHub 레포 → **Settings → Deploy keys → Add deploy key**.
   - Title: `amx-tsamx-deploy (read-only)`
   - Key: `amx_tsamx_deploy.pub` 내용 붙여넣기
   - **"Allow write access" 는 체크하지 않는다**(읽기 전용 강제).
3. 개인키 `amx_tsamx_deploy`(공개키 아님)를 각 AMA 서버로 안전 채널로 전달한다(예: `scp`, 비밀 관리 도구). 저장소 히스토리·로그에 남기지 않는다.

### 6-3. 서버측 git 설정

키는 `ama` 서비스 계정 홈에 배치하고 권한을 조인다:

```sh
install -d -m 0700 ~/.ssh
install -m 0600 amx_tsamx_deploy ~/.ssh/amx_tsamx_deploy   # 개인키 0600 필수
```

`~/.ssh/config`에 이 레포 전용 Host 별칭을 만들어 키를 바인딩한다(다른 GitHub 접근과 격리):

```
Host amx-github
    HostName github.com
    User git
    IdentityFile ~/.ssh/amx_tsamx_deploy
    IdentitiesOnly yes
```

`known_hosts`를 미리 고정해 최초 연결 시 대화형 프롬프트(무인 설치 실패 원인)를 없앤다:

```sh
ssh-keyscan github.com >> ~/.ssh/known_hosts
```

(확인 필요) GitHub SSH 호스트키 지문은 GitHub 공식 문서의 값과 대조해 MITM을 배제하는 것이 안전하다.

설치·업데이트는 §2의 태그 핀 방식을 그대로 쓰되 SSH URL의 호스트에 위 별칭(`amx-github`)을 넣는다:

```sh
uv tool install "git+ssh://git@amx-github/<org>/AMX.git@<태그>#subdirectory=tsamx"
```

설치 검증: `tsamx --help`, 그리고 AMA가 소비하는 스키마 확인용 `tsamx list --json`(UPSTREAM-SYNC.md §3).

(확인 필요) AMA 데몬은 tsamx를 PATH에서 찾거나 `AMX_TSAMX_BIN`으로 지정된 경로에서 찾는다(`ama-agent/internal/tsamx/exec.go` `EnvBinary`). uv tool 설치 경로(`~/.local/bin`)가 데몬 PATH에 없으면 `AMX_TSAMX_BIN`을 명시한다.

### 6-4. 확장 트리거 (1차 이후, 보류 아님 — 조건 기록)

아래 조건에 **도달하면** 다음 단계로 이행한다(BACKLOG B2 서술과 일치):

- **서버 N대 초과 또는 조직 이전 시** → 공용 키를 폐기하고 **서버별 deploy key 또는 machine user**로 전환한다. 키 회수·감사 단위가 서버가 되어 한 서버 유출이 전체 설치 경로를 오염시키지 않는다. machine user는 다중 레포 접근이 필요할 때 택한다.
- **그 다음 단계** → **AMS wheel 아티팩트 서빙**으로 GitHub 의존을 제거한다. AMS가 tsamx 빌드 산출물(wheel)을 배포하고 서버는 내부 인덱스에서 설치한다.

각 전환은 별도 배포 설계 턴에서 다룬다(현 문서 범위 밖).
