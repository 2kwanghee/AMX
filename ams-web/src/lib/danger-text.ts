// dangerous_command 경보의 사유 문장 조립. detail이 곧 사유라 정적 문구로 담을 수
// 없다(§5.6.3 — 원문은 비전송, 패턴명·마스킹본·발생 위치만 온다). hostname·userId는
// 훅이 자기 신고한 값이고 AMS가 장부와 대조한 참조가 아니므로, 대상 서버·계정 칸으로
// 승격하지 않고 보고값 그대로 문장에 담는다. 필드가 빠진 옛 페이로드면 undefined를
// 돌려 호출부의 detailText(원시 JSON) 폴백으로 내려간다.
export function dangerText(detail?: Record<string, unknown>): string | undefined {
  if (!detail) return undefined;
  const s = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : undefined);
  const pattern = s(detail.patternName);
  if (!pattern) return undefined;
  // 마스킹본은 최대 200자(대부분 '*')라 표에서는 앞부분만 보여준다. 전문은 툴팁(원시 detail)에 남는다.
  const maskedRaw = s(detail.commandMasked);
  const masked = maskedRaw && maskedRaw.length > 80 ? `${maskedRaw.slice(0, 80)}…` : maskedRaw;
  const host = s(detail.hostname);
  const user = s(detail.userId);
  const where =
    host && user ? `${host}의 ${user} 세션에서 ` : host ? `${host}에서 ` : user ? `${user} 세션에서 ` : '';
  const what = masked ? `${where}「${masked}」 실행이 감지됐습니다` : `${where}실행이 감지됐습니다`;
  return `위험 명령 패턴(${pattern})이 잡혔습니다. ${what}. 훅은 감지만 하고 차단하지 않으므로 명령은 실행됐을 수 있습니다 — 의도한 작업인지 확인하세요. 원문은 전송되지 않아 마스킹본과 해시만 남습니다.`;
}
