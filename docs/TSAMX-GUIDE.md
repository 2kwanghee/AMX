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
