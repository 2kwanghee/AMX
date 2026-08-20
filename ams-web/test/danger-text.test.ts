// dangerText는 dangerous_command 경보의 detail(훅 자기 신고 페이로드)을 사유 문장으로
// 조립한다. 핵심 계약은 둘이다: patternName이 없으면 undefined를 돌려 호출부의 원시
// JSON 폴백이 살아야 하고(옛 페이로드 호환), 있으면 항상 비어 있지 않은 문장을 돌려
// 렌더 지점의 `(kind === 'dangerous_command' && dangerText(...)) || KR_ALERT_REASON[...]`
// falsy 평가에 걸리지 않아야 한다.
import { describe, expect, it } from 'vitest';
import { dangerText } from '@/lib/danger-text';

// dev DB 실측 페이로드 형태 (2026-08-20 sudo 테스트 건)
const FULL = {
  ts: '2026-08-20T13:08:05.997484+00:00',
  cwd: '/mnt/c/workspace/AMX',
  userId: 'khee@tscorp.ai',
  hostname: 'DESKTOP-PN6P9SC',
  sessionId: '4070ab70-9dd8-472f-b5bb-4a6d876fc471',
  patternName: 'sudo',
  commandMasked: 'sudo*****************************',
  commandSha256: 'c3589d0290dd5390cff7f352d80d17306b4a906a5fbc0d9064ecaef81cb21d53',
};

describe('dangerText', () => {
  it('전체 페이로드: 패턴·호스트·사용자·마스킹본이 모두 문장에 담긴다', () => {
    const t = dangerText(FULL);
    expect(t).toBeDefined();
    expect(t).toContain('위험 명령 패턴(sudo)');
    expect(t).toContain('DESKTOP-PN6P9SC의 khee@tscorp.ai 세션에서');
    expect(t).toContain('「sudo*****************************」');
    // 훅의 성격(감지 전용·원문 비전송)을 사실대로 알린다
    expect(t).toContain('차단하지 않으므로');
    expect(t).toContain('원문은 전송되지 않아');
  });

  it('patternName이 없으면 undefined — 원시 JSON 폴백 경로 보존', () => {
    expect(dangerText(undefined)).toBeUndefined();
    expect(dangerText({})).toBeUndefined();
    const { patternName: _omit, ...rest } = FULL;
    expect(dangerText(rest)).toBeUndefined();
    // 공백뿐이거나 문자열이 아닌 값도 "없음"으로 취급
    expect(dangerText({ ...FULL, patternName: '   ' })).toBeUndefined();
    expect(dangerText({ ...FULL, patternName: 42 })).toBeUndefined();
  });

  it('patternName만 있어도 비어 있지 않은 문장을 돌린다 (falsy 트랩 없음)', () => {
    const t = dangerText({ patternName: 'mkfs' });
    expect(t).toBeTruthy();
    expect(t).toContain('위험 명령 패턴(mkfs)');
  });

  it('마스킹본 80자 초과는 앞 80자 + 말줄임, 정확히 80자는 그대로', () => {
    const long = 'curl'.padEnd(200, '*');
    const over = dangerText({ patternName: 'curl_pipe_shell', commandMasked: long });
    expect(over).toContain(`「${long.slice(0, 80)}…」`);
    expect(over).not.toContain(long);
    const exact = 'x'.repeat(80);
    expect(dangerText({ patternName: 'p', commandMasked: exact })).toContain(`「${exact}」`);
  });

  it('호스트/사용자 조합 4경우의 발생 위치 문구', () => {
    const both = dangerText(FULL)!;
    expect(both).toContain('DESKTOP-PN6P9SC의 khee@tscorp.ai 세션에서');
    const hostOnly = dangerText({ patternName: 'sudo', hostname: 'H1' })!;
    expect(hostOnly).toContain('H1에서 실행이 감지됐습니다');
    const userOnly = dangerText({ patternName: 'sudo', userId: 'a@b.io' })!;
    expect(userOnly).toContain('a@b.io 세션에서 실행이 감지됐습니다');
    const neither = dangerText({ patternName: 'sudo' })!;
    expect(neither).toContain('. 실행이 감지됐습니다');
  });

  it('문자열이 아닌 부속 필드는 조용히 무시된다', () => {
    const t = dangerText({
      patternName: 'dd_of_device',
      commandMasked: null,
      hostname: 123,
      userId: { nested: true },
      truncated: true,
    });
    expect(t).toBeTruthy();
    expect(t).toContain('위험 명령 패턴(dd_of_device)');
    expect(t).not.toContain('123');
  });
});
