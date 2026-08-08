#!/usr/bin/env python3
"""AMX O9 판별: OAuth refresh token이 회전형(rotating)인지 정적(static)인지 확인.

같은 원본 refresh token으로 token 엔드포인트에 refresh grant를 2회 호출한다.
2회차가 거부되면 회전형(원본이 1회 쓰고 소각됨), 성공하면 정적(재사용 가능).

토큰 값은 절대 출력·로깅하지 않는다. 결과(ROTATING/STATIC)만 stdout에 낸다.

실행 (tsamx 상수 재사용):
    export AMX_TEST_REFRESH_TOKEN='<실험 계정의 refreshToken>'
    # 또는:  export AMX_TEST_CREDENTIALS="$HOME/.claude/.credentials.json"
    uv run --project ~/amx-p0/tsamx python ~/amx-p0/tools/o9_refresh_probe.py

주의: 회전형이면 실험한 계정의 원본 refresh token은 실험 후 무효가 된다
      → 그 계정은 재로그인이 필요할 수 있으니 실험용(개인) 계정으로 할 것.
"""
import json
import os
import sys
import urllib.error
import urllib.request

try:
    from tsamx.oauth import OAUTH_CLIENT_ID, OAUTH_TOKEN_URL
except Exception:  # tsamx 미설치 시 공개 상수로 폴백
    OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
    OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def refresh(rt: str):
    """(status, new_refresh_present) 반환. 토큰 값은 담지 않는다."""
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "amx-o9-probe/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode())
        return "ok", bool(d.get("refresh_token"))
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace") if hasattr(e, "read") else ""
        dead = e.code in (400, 401, 403) and ("invalid_grant" in text or "invalid_client" in text)
        return ("invalid_grant" if dead else f"http_{e.code}"), None
    except Exception as e:  # 네트워크 등
        return f"error:{type(e).__name__}", None


def load_refresh_token() -> str | None:
    rt = os.environ.get("AMX_TEST_REFRESH_TOKEN")
    if rt:
        return rt.strip()
    path = os.environ.get("AMX_TEST_CREDENTIALS")
    if path:
        with open(os.path.expanduser(path)) as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or {}
        return oauth.get("refreshToken")
    return None


def main() -> int:
    rt = load_refresh_token()
    if not rt:
        print("refresh token을 못 찾음. 다음 중 하나를 설정하세요:")
        print("  export AMX_TEST_REFRESH_TOKEN='<refreshToken>'")
        print('  export AMX_TEST_CREDENTIALS="$HOME/.claude/.credentials.json"')
        return 1

    print("=== AMX O9 refresh 회전 판별 ===")
    s1, new1 = refresh(rt)
    print(f"1회차 refresh: {s1}  | 응답에 새 refresh_token 포함: {new1}")
    if s1 != "ok":
        print("\n판정불가: 1회차가 실패했습니다.")
        print("  - invalid_grant  → 토큰이 이미 만료/무효 (다시 로그인 후 재시도)")
        print("  - http_4xx/error → 네트워크·형식 문제")
        return 2

    # 2회차: 반드시 같은 '원본' refresh token을 재사용한다 (새로 받은 것 X).
    s2, _ = refresh(rt)
    print(f"2회차 refresh (같은 원본 토큰 재사용): {s2}")

    if s2 == "ok":
        print("\n판정: STATIC (정적) — 같은 refresh token을 재사용할 수 있습니다.")
        print("함의: AMS 보관본이 로컬 refresh 후에도 유효 → 재배정 시 역동기화·재인증 불필요.")
        print("      현재 폴백(재인증)보다 매끄러운 경로로 O9를 확정할 수 있습니다.")
        return 0
    if s2 == "invalid_grant":
        print("\n판정: ROTATING (회전형) — 원본 refresh token이 1회 사용 후 무효화됩니다.")
        print("함의: 계정이 서버에서 자체 refresh하면 AMS 보관본이 구식이 됨")
        print("      → 재배정 시 재인증(현 폴백 유지) 또는 AMA→AMS 역동기화가 필요.")
        print("주의: 방금 실험한 계정의 원본 refresh token은 이제 무효 — 그 계정 재로그인 필요.")
        return 0
    print(f"\n판정불가: 2회차 상태={s2} (네트워크 재시도 권장).")
    return 3


if __name__ == "__main__":
    sys.exit(main())
