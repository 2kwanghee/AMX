# tsamx 업스트림 동기화 절차 (B3 / O6)

tsamx는 claude-swap(MIT)을 rename 포크한 것이라 업스트림 diff 병합이 **수동**이다
(`TSAMX-GUIDE.md` §4). 원본 스냅샷은 `vendor/claude-swap-upstream`(현재
claude-swap v0.25.0b1)에 보존되어 있고, 포크 트리는 `tsamx/`다. 새 업스트림이
나오면 `vendor/`의 옛 스냅샷을 기준으로 **3-way 비교**(옛 업스트림 ↔ 새 업스트림
↔ 우리 `tsamx/`)해 수동 병합한다. 아래 체크리스트를 순서대로 따른다.

## 1. 업스트림 fetch·diff 확인

```sh
# 새 업스트림을 임시로 가져온다(현재 vendor는 0.25.0b1 스냅샷).
git clone --depth 1 https://github.com/realiti4/claude-swap /tmp/cswap-new

# (a) 업스트림 자체 변화: 우리가 보존한 스냅샷 ↔ 새 업스트림
diff -ru vendor/claude-swap-upstream/src /tmp/cswap-new/src

# (b) 우리 포크가 이미 바꾼 것: 옛 업스트림 ↔ tsamx (rename 잡음 포함)
diff -ru vendor/claude-swap-upstream/src tsamx/src
```

(a)가 이번에 병합할 **순수 업스트림 변경분**이다. (b)는 rename(`claude_swap`→
`tsamx`, `cswap`→`tsamx`)·커스텀(§3 인터페이스, TSAMX-GUIDE §3)을 담고 있어,
(a)의 각 hunk를 (b)의 대응 파일에 손으로 옮길 때 rename을 반영해야 한다.

## 2. 병합 판단 기준

- **버그·보안 수정**: 우선 병합. 해당 파일이 (b)에서 rename만 됐으면 기계적.
- **Claude Code 계약 추종**(`~/.claude.json`·credential 위치·OAuth): 최우선.
  Claude Code 업데이트 대응이 최대 유지비 항목(TSAMX-GUIDE §4).
- **rename 금지 영역 충돌**: `CLAUDE_CONFIG_DIR`, `~/.claude.json`,
  `.credentials.json`, keyring 서비스 `claude-code`, OAuth client_id는 원형 유지
  (TSAMX-GUIDE §1). 업스트림이 이 이름을 바꿔도 **따라가지 않는다**.
- **제거 후보 영역**(`tui/`, `menubar.py`, macOS keychain 등, TSAMX-GUIDE §3-c):
  업스트림 변경을 병합할지 스킵할지 소유자 판단(§5).
- **§3 호환성에 닿는 변경**: AMA가 의존하는 계약을 깨면 병합 보류하고 §5로.

## 3. CLI/JSON 호환성 체크리스트 (AMA ↔ tsamx 계약)

병합 후 아래가 **모두 유지**되는지 확인한다. 출처는
`ama-agent/internal/tsamx/exec.go`·`bridge.go`. 하나라도 깨지면 AMA가 회귀한다.

**AMA가 호출하는 명령(인자 포함):**
- [ ] `tsamx add` (무인자 — 현재 config home 계정 캡처) · `exec.go` Add
- [ ] `tsamx remove <account>` · `tsamx enable <account>` · `tsamx disable <account>`
- [ ] `tsamx switch <target>`
- [ ] `tsamx switch --strategy <best|next-available>` · SwitchStrategy
      (cli.py choices `{best,next-available}` — 유지 확인)
- [ ] `tsamx config set autoswitch.threshold <pct>`
- [ ] `tsamx config set autoswitch.<cooldown_seconds|hysteresis_pct> <value>`
- [ ] `tsamx auto --once` — exit code 계약: **0 switched · 2 no-action · 3 blocked
      · 1 error**(exec.go AutoOnce가 이 코드에 의존)
- [ ] `tsamx list --json` · `tsamx status --json`(현재 status는 list에서 파생)

**`tsamx list --json` 스키마 (schema v1, `json_output.py` `SCHEMA_VERSION=1`):**
- [ ] 최상위 `schemaVersion`(=1) · `activeAccountNumber` · `accounts[]`
- [ ] `accounts[]` 필드: `number` `email` `organizationName` `organizationUuid`
      `active` `disabled` `usageStatus` `alias`
- [ ] `usage.fiveHour` / `usage.sevenDay` 각각 `{pct, resetsAt}`

**config 키 (settings.py, `autoswitch.` 네임스페이스):**
- [ ] `autoswitch.threshold` · `autoswitch.cooldown_seconds` · `autoswitch.hysteresis_pct`

**파일·경로·env 계약:**
- [ ] 스테이징 파일 `.claude.json`의 `oauthAccount`: `emailAddress` `accountUuid`
      `organizationUuid` `organizationName`(exec.go가 write, tsamx가 read)
- [ ] `.credentials.json` 스테이징 후 `add` 캡처
- [ ] 백업 루트 `$XDG_DATA_HOME/tsamx`(기본 `~/.local/share/tsamx`)의
      `autoswitch_state.json` → `quarantine{slot:{email}}`(ReadQuarantine)
- [ ] env 계약 `CLAUDE_CONFIG_DIR` · `XDG_DATA_HOME` 존중

## 4. 병합 후 검증

```sh
cd /mnt/c/workspace/AMX/tsamx && uv run pytest -q      # 1824 passed / 3 skipped 기준(TSAMX-GUIDE §2)
cd /mnt/c/workspace/AMX/ama-agent && go test ./internal/tsamx/...   # 브리지 계약 테스트
```

- 9p 마운트에서 tsamx 테스트가 느리면 ext4 사본에서 실행(TSAMX-GUIDE §2).
- (확인 필요) AMA E2E 게이트(실제 tsamx 구동)는 exec 경로를 검증한다 —
  §3 계약 변경이 의심되면 E2E까지 돌린다. G24 규칙: 각 병합 시 전체 e2e 게이트 필수.
- 병합 완료 후 `vendor/claude-swap-upstream`을 새 업스트림 스냅샷으로 갱신해
  다음 3-way의 기준점을 최신화한다.

## 5. 소유자 지정

- **동기화 소유자**: (사용자 결정 대기 — 업스트림 추적·병합 착수 권한자)
- **§2 병합 판단(제거 후보·계약 충돌) 승인자**: (사용자 결정 대기)
