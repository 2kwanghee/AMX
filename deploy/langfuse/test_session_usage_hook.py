"""session_usage_hook.py 단위 테스트 — 표준 라이브러리만, AMS 없이 독립 실행.

    python3 -m pytest deploy/langfuse/test_session_usage_hook.py
    python3 deploy/langfuse/test_session_usage_hook.py   # 직접 실행도 같은 스위트를 돈다

검증: 모델별 집계, message.id 중복 제거, 잘린/깨진 줄 내성, 원문 미유출,
설정 미지정 no-op, 깨진 stdin에도 exit 0, tsamx 실패 시 계정 없이 전송.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import pathlib

import pytest

_HOOK_PATH = pathlib.Path(__file__).with_name("session_usage_hook.py")

# 트랜스크립트에 섞여 있어야 하는 "원문". 어떤 페이로드에도 나타나서는 안 된다.
_SECRET_PROMPT = "PROMPT-DO-NOT-SEND-4f2b1a"


def _load_module():
    spec = importlib.util.spec_from_file_location("session_usage_hook_under_test", _HOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hook(monkeypatch, tmp_path):
    mod = _load_module()
    # 실패 기록은 tmp로 격리.
    monkeypatch.setenv("CC_SESSION_USAGE_STATE_FILE", str(tmp_path / "state"))
    # tsamx가 실제로 설치된 호스트에서도 테스트가 네트워크/외부 상태에 매이지 않게.
    monkeypatch.delenv("LANGFUSE_USER_ID", raising=False)
    monkeypatch.setattr(mod, "active_account_email", lambda: None)
    # 기존 테스트는 모두 "자식(DEFERRED=1)"의 집계·전송 로직만 검증한다 — 분리
    # 프로세스 재실행 자체는 아래 지연 실행 전용 테스트에서 따로 다룬다. 최대 대기를
    # 0으로 둬 트랜스크립트가 비어 있는 케이스(no-op 계열)가 폴링으로 느려지지 않게 한다.
    monkeypatch.setenv("AMX_SESSION_USAGE_DEFERRED", "1")
    monkeypatch.setattr(mod, "_DEFER_MAX_SECONDS", 0.0)
    return mod


def _assistant(
    *,
    mid: str,
    model: str = "claude-opus-5",
    in_tokens: int = 10,
    out_tokens: int = 100,
    cache_read: int = 1000,
    c1h: int = 500,
    c5m: int = 0,
    thinking: int = 40,
    web_search: int = 0,
    web_fetch: int = 0,
    tier: str | None = "standard",
    stop: str | None = "end_turn",
    ts: str = "2026-08-19T10:00:00.000Z",
    block: str = "thinking",
) -> str:
    usage = {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": c1h + c5m,
        "cache_creation": {
            "ephemeral_1h_input_tokens": c1h,
            "ephemeral_5m_input_tokens": c5m,
        },
        "output_tokens_details": {"thinking_tokens": thinking},
        "server_tool_use": {
            "web_search_requests": web_search,
            "web_fetch_requests": web_fetch,
        },
    }
    if tier is not None:
        usage["service_tier"] = tier
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": mid,
                "model": model,
                "role": "assistant",
                "stop_reason": stop,
                "usage": usage,
                # 원문 — 훅은 이 필드를 읽지 않는다.
                "content": [{"type": block, "text": _SECRET_PROMPT}],
            },
        }
    )


def _transcript() -> list[str]:
    return [
        json.dumps({"type": "user", "message": {"content": _SECRET_PROMPT}}),
        _assistant(mid="msg_a"),
        # 같은 응답의 두 번째 content 블록 — 같은 message.id라 집계에 다시 들어가면 안 된다.
        _assistant(mid="msg_a", block="tool_use"),
        _assistant(mid="msg_b", out_tokens=50, c1h=0, c5m=700, stop="max_tokens",
                   ts="2026-08-19T11:00:00.000Z"),
        # 서브에이전트 모델.
        _assistant(mid="msg_c", model="claude-sonnet-5", in_tokens=1, out_tokens=7,
                   cache_read=20, c1h=30, c5m=0, thinking=3, web_search=2, web_fetch=1,
                   tier="priority", stop="tool_use", ts="2026-08-19T09:00:00.000Z"),
        "{not json at all",  # 깨진 줄
        "",                   # 빈 줄
        '{"type":"assistant","message":{"id":"msg_d","model":"m","usage"',  # 잘린 마지막 줄
    ]


def test_aggregate_per_model_and_dedupe(hook):
    agg = hook.aggregate(_transcript())
    assert set(agg) == {"claude-opus-5", "claude-sonnet-5"}

    opus = agg["claude-opus-5"]
    # msg_a는 두 줄이지만 한 번만 센다(중복 제거 없으면 messageCount 3, 토큰이 1.5배).
    assert opus["message_count"] == 2
    assert opus["input_tokens"] == 20
    assert opus["output_tokens"] == 150
    assert opus["cache_read_tokens"] == 2000
    # 1시간/5분 캐시 쓰기가 합쳐지지 않고 각각 남는다 — 이 훅의 존재 이유.
    assert opus["cache_create_1h_tokens"] == 500
    assert opus["cache_create_5m_tokens"] == 700
    assert opus["thinking_tokens"] == 80
    assert opus["service_tier_counts"] == {"standard": 2}
    assert opus["stop_reason_counts"] == {"end_turn": 1, "max_tokens": 1}
    assert opus["started_at"].isoformat() == "2026-08-19T10:00:00+00:00"
    assert opus["ended_at"].isoformat() == "2026-08-19T11:00:00+00:00"

    sonnet = agg["claude-sonnet-5"]
    assert sonnet["message_count"] == 1
    assert sonnet["web_search_requests"] == 2
    assert sonnet["web_fetch_requests"] == 1
    assert sonnet["service_tier_counts"] == {"priority": 1}


def test_build_models_orders_by_tokens_and_serialises_times(hook):
    models = hook.build_models(hook.aggregate(_transcript()))
    assert [m["model"] for m in models] == ["claude-opus-5", "claude-sonnet-5"]
    first = models[0]
    assert first["cacheCreate1HTokens"] == 500
    assert first["cacheCreate5MTokens"] == 700
    assert first["startedAt"] == "2026-08-19T10:00:00+00:00"
    assert first["endedAt"] == "2026-08-19T11:00:00+00:00"


def test_aggregate_survives_only_garbage(hook):
    # 한 줄도 쓸 수 없는 파일이어도 예외 없이 빈 집계.
    assert hook.aggregate(["", "{", "null", "[]", '{"type":"user"}']) == {}


def _run(hook, monkeypatch, payload, *, url="http://ams.test/ingest", token="tok"):
    """훅을 1회 실행하고 전송 payload(있으면)를 반환한다. 없으면 None."""
    sent: list[dict] = []
    monkeypatch.setattr(hook, "_notify", lambda u, t, p: sent.append({"url": u, "token": t, "payload": p}))
    if url is None:
        monkeypatch.delenv("AMX_SESSION_INGEST_URL", raising=False)
    else:
        monkeypatch.setenv("AMX_SESSION_INGEST_URL", url)
    if token is None:
        monkeypatch.delenv("AMX_SESSION_INGEST_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AMX_SESSION_INGEST_TOKEN", token)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if payload is not None else "!"))
    assert hook.main() == 0
    return sent[0] if sent else None


@pytest.fixture()
def transcript_file(tmp_path):
    path = tmp_path / "sess-1.jsonl"
    path.write_text("\n".join(_transcript()) + "\n", encoding="utf-8")
    return path


def test_sends_expected_payload(hook, monkeypatch, transcript_file):
    out = _run(
        hook,
        monkeypatch,
        {"session_id": "sess-1", "transcript_path": str(transcript_file), "cwd": "/work"},
    )
    assert out is not None
    body = out["payload"]
    assert body["sessionId"] == "sess-1"
    assert body["cwd"] == "/work"
    assert "accountEmail" not in body  # tsamx 조회 실패 → 계정 없이 보낸다.
    assert [m["model"] for m in body["models"]] == ["claude-opus-5", "claude-sonnet-5"]
    assert out["token"] == "tok"


def test_payload_carries_no_transcript_text(hook, monkeypatch, transcript_file):
    out = _run(
        hook,
        monkeypatch,
        {"session_id": "sess-1", "transcript_path": str(transcript_file)},
    )
    serialised = json.dumps(out["payload"])
    # 원문이 어떤 필드에도, 어떤 경로로도 섞이지 않는다.
    assert _SECRET_PROMPT not in serialised
    assert "content" not in serialised


def test_account_email_from_tsamx(hook, monkeypatch, transcript_file):
    monkeypatch.setattr(hook, "active_account_email", lambda: "khee@tscorp.ai")
    out = _run(
        hook, monkeypatch, {"session_id": "s", "transcript_path": str(transcript_file)}
    )
    assert out["payload"]["accountEmail"] == "khee@tscorp.ai"


def test_session_id_falls_back_to_filename(hook, monkeypatch, transcript_file):
    out = _run(hook, monkeypatch, {"transcript_path": str(transcript_file)})
    assert out["payload"]["sessionId"] == "sess-1"


def test_noop_without_config(hook, monkeypatch, transcript_file):
    payload = {"session_id": "s", "transcript_path": str(transcript_file)}
    assert _run(hook, monkeypatch, payload, url=None) is None
    assert _run(hook, monkeypatch, payload, token=None) is None
    assert _run(hook, monkeypatch, payload, url="", token="") is None


def test_noop_on_broken_stdin_and_missing_transcript(hook, monkeypatch, tmp_path):
    assert _run(hook, monkeypatch, None) is None  # stdin이 JSON이 아님
    assert _run(hook, monkeypatch, {}) is None  # transcript_path 없음
    assert _run(hook, monkeypatch, {"transcript_path": str(tmp_path / "nope.jsonl")}) is None


def test_noop_when_no_assistant_records(hook, monkeypatch, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    assert _run(hook, monkeypatch, {"transcript_path": str(path)}) is None


def test_active_account_email_swallows_tsamx_failure(monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.setenv("CC_SESSION_USAGE_STATE_FILE", str(tmp_path / "state"))
    monkeypatch.delenv("LANGFUSE_USER_ID", raising=False)

    def _boom(*a, **kw):
        raise FileNotFoundError("tsamx")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert mod.active_account_email() is None

    class _Proc:
        returncode = 0
        stdout = b'{"active": {"email": "a@b.c"}}'

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _Proc())
    assert mod.active_account_email() == "a@b.c"

    class _ProcList:
        returncode = 0
        stdout = b'{"accounts": [{"email": "x@y.z"}, {"email": "on@y.z", "active": true}]}'

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _ProcList())
    assert mod.active_account_email() == "on@y.z"

    class _Junk:
        returncode = 0
        stdout = b"not json"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _Junk())
    assert mod.active_account_email() is None


# -- iterations 귀속 ----------------------------------------------------------
# 아래 픽스처는 기본 프로필 실측 레코드의 형태를 그대로 옮긴 것이다(값만 축약).
# 최상위 message.model 과 flat 카운터는 **마지막** iteration을 반영하는데 최상위
# cache_creation 분리값만 iteration[0]의 것이 새어 있다 — 실측 47건 중 25건이 이 모양이고,
# 최상위만 읽으면 fable-5 가 쓴 캐시 생성이 opus-5 이름표로 저장된다.
def _multi_iteration_record() -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-08-19T10:00:00.000Z",
            "message": {
                "id": "msg_multi",
                "model": "claude-opus-5",
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 1000,
                    # flat 은 0인데 split 만 iteration[0]의 6079가 새어 있다.
                    "cache_creation_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 6079,
                        "ephemeral_5m_input_tokens": 0,
                    },
                    "output_tokens_details": {"thinking_tokens": 30},
                    "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0},
                    "service_tier": "standard",
                    "iterations": [
                        {
                            "type": "message",
                            "model": "claude-fable-5",
                            "input_tokens": 2,
                            "output_tokens": 80,
                            "cache_read_input_tokens": 400,
                            "cache_creation_input_tokens": 6079,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 6079,
                                "ephemeral_5m_input_tokens": 0,
                            },
                        },
                        {
                            "type": "message",
                            "model": "claude-opus-5",
                            "input_tokens": 2,
                            "output_tokens": 120,
                            "cache_read_input_tokens": 600,
                            "cache_creation_input_tokens": 0,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 0,
                                "ephemeral_5m_input_tokens": 0,
                            },
                        },
                    ],
                },
                "content": [{"type": "text", "text": _SECRET_PROMPT}],
            },
        }
    )


def test_multi_iteration_attributes_tokens_to_own_model(hook):
    agg = hook.aggregate([_multi_iteration_record()])
    assert set(agg) == {"claude-opus-5", "claude-fable-5"}
    fable, opus = agg["claude-fable-5"], agg["claude-opus-5"]
    # 캐시 생성 6079는 그것을 쓴 fable-5 에 붙는다. opus-5 에 새면 이 PR의 목적이 깨진다.
    assert fable["cache_create_1h_tokens"] == 6079
    assert opus["cache_create_1h_tokens"] == 0
    assert fable["output_tokens"] == 80
    assert opus["output_tokens"] == 120
    assert fable["cache_read_tokens"] == 400
    assert opus["cache_read_tokens"] == 600
    # iteration에 없는 항목(thinking·서버툴·티어·stop_reason)과 메시지 수·시각은
    # 최상위 모델 한 곳에만 귀속된다.
    assert opus["thinking_tokens"] == 30
    assert fable["thinking_tokens"] == 0
    assert opus["web_search_requests"] == 1
    assert opus["message_count"] == 1
    assert fable["message_count"] == 0
    assert opus["service_tier_counts"] == {"standard": 1}
    assert fable["service_tier_counts"] == {}
    # 시각은 두 모델 모두에 있어야 한다 — 조회 창이 ended_at 기준이라, NULL이면
    # iteration에만 등장하는 모델이 콘솔에서 사라진다.
    assert fable["started_at"] is not None and fable["ended_at"] is not None
    assert fable["ended_at"] == opus["ended_at"]


def test_single_iteration_without_model_falls_back_to_top(hook):
    # 실측 27,094건이 이 모양이다: iteration이 1개이고 model 키가 없다.
    line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-08-19T10:00:00.000Z",
            "message": {
                "id": "msg_single",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 90,
                    "cache_read_input_tokens": 900,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 700,
                        "ephemeral_5m_input_tokens": 5,
                    },
                    "iterations": [
                        {
                            "type": "message",
                            "input_tokens": 9,
                            "output_tokens": 90,
                            "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 705,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 700,
                                "ephemeral_5m_input_tokens": 5,
                            },
                        }
                    ],
                },
            },
        }
    )
    agg = hook.aggregate([line])
    assert set(agg) == {"claude-opus-5"}
    assert agg["claude-opus-5"]["cache_create_1h_tokens"] == 700
    assert agg["claude-opus-5"]["cache_create_5m_tokens"] == 5
    assert agg["claude-opus-5"]["output_tokens"] == 90


def test_absent_iterations_uses_top_level(hook):
    # 실측 157건. 지금까지의 동작을 그대로 유지한다.
    agg = hook.aggregate([_assistant(mid="m1", c1h=500, c5m=7)])
    assert agg["claude-opus-5"]["cache_create_1h_tokens"] == 500
    assert agg["claude-opus-5"]["cache_create_5m_tokens"] == 7
    assert agg["claude-opus-5"]["message_count"] == 1


def test_empty_iterations_list_uses_top_level(hook):
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m-empty",
                "model": "claude-opus-5",
                "usage": {
                    "output_tokens": 5,
                    "iterations": [],
                    "cache_creation": {"ephemeral_1h_input_tokens": 11},
                },
            },
        }
    )
    agg = hook.aggregate([line])
    assert agg["claude-opus-5"]["output_tokens"] == 5
    assert agg["claude-opus-5"]["cache_create_1h_tokens"] == 11


def test_record_without_message_id_is_skipped(hook):
    # 접을 키가 없으면 같은 응답이 여러 줄로 적혀 있을 때 배수로 계산된다 —
    # 과대집계를 피하려고 버린다.
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"model": "claude-opus-5", "usage": {"output_tokens": 100}},
        }
    )
    assert hook.aggregate([line, line, line]) == {}


# -- 라벨 검증(임의 텍스트 반출 차단) -----------------------------------------


def test_arbitrary_model_name_folds_into_other_bucket(hook):
    evil = "x" * 200
    agg = hook.aggregate([_assistant(mid="m1", model=evil)])
    assert list(agg) == ["<other>"]  # 절단본이 아니라 단일 버킷.
    assert evil[:40] not in json.dumps(hook.build_models(agg))


def test_model_name_with_prose_folds(hook):
    agg = hook.aggregate([_assistant(mid="m1", model="leak me: secret words here")])
    assert list(agg) == ["<other>"]


def test_real_model_names_pass_unchanged(hook):
    for name in ("claude-opus-5", "claude-haiku-4-5-20251001", "<synthetic>", "claude-fable-5"):
        agg = hook.aggregate([_assistant(mid="m-" + name, model=name)])
        assert list(agg) == [name]


def test_count_map_keys_are_validated_not_truncated(hook):
    agg = hook.aggregate(
        [_assistant(mid="m1", tier="a" * 100, stop="exfiltrate this sentence")]
    )
    bucket = agg["claude-opus-5"]
    assert bucket["service_tier_counts"] == {"<other>": 1}
    assert bucket["stop_reason_counts"] == {"<other>": 1}
    assert "exfiltrate" not in json.dumps(hook.build_models(agg))


def test_count_map_key_cap_folds_overflow(hook):
    # 서버 상한(20키)과 짝을 맞춘다: 21번째 키는 버려지지 않고 _OTHER 로 접힌다.
    lines = [_assistant(mid=f"m{i}", stop=f"reason-{i}") for i in range(25)]
    counts = hook.aggregate(lines)["claude-opus-5"]["stop_reason_counts"]
    assert len(counts) <= 20
    # 고유 키 19개(reason-0..18)를 채운 뒤 남은 6건이 _OTHER 로 접혀 총 20키가 된다.
    assert len(counts) == 20
    assert counts["<other>"] == 6
    assert sum(counts.values()) == 25


def test_real_labels_pass_through(hook):
    agg = hook.aggregate([_assistant(mid="m1", tier="standard", stop="max_tokens")])
    bucket = agg["claude-opus-5"]
    assert bucket["service_tier_counts"] == {"standard": 1}
    assert bucket["stop_reason_counts"] == {"max_tokens": 1}


# -- _int 이상값 흡수 ---------------------------------------------------------


def test_int_absorbs_every_anomaly(hook):
    assert hook._int(float("inf")) == 0  # OverflowError 를 던지면 세션 전체가 사라진다.
    assert hook._int(float("-inf")) == 0
    assert hook._int(float("nan")) == 0
    assert hook._int(10**30) == 0  # 서버 상한(2**53) 초과.
    assert hook._int(-5) == 0
    assert hook._int("100") == 0
    assert hook._int(True) == 0
    assert hook._int(None) == 0
    assert hook._int(435) == 435
    assert hook._int(2**53) == 2**53


def test_infinity_in_transcript_does_not_lose_the_session(hook, monkeypatch, tmp_path):
    # json 은 기본으로 Infinity 를 허용한다. 그 값 하나가 세션 전체를 삼키면 안 된다.
    path = tmp_path / "inf.jsonl"
    path.write_text(
        '{"type":"assistant","message":{"id":"m1","model":"claude-opus-5","usage":'
        '{"output_tokens":Infinity,"cache_creation":{"ephemeral_1h_input_tokens":50}}}}\n'
        + _assistant(mid="m2", out_tokens=7)
        + "\n",
        encoding="utf-8",
    )
    out = _run(hook, monkeypatch, {"transcript_path": str(path)})
    assert out is not None  # 세션이 사라지지 않는다.
    model = out["payload"]["models"][0]
    assert model["outputTokens"] == 7  # Infinity 는 0으로 흡수되고 나머지는 살아 있다.
    # m1 의 50 + _assistant 기본값 500.
    assert model["cacheCreate1HTokens"] == 550


# -- 읽기 바이트 상한 ---------------------------------------------------------


def test_giant_single_line_is_bounded(hook, monkeypatch, tmp_path):
    # 개행 없는 거대 스트림(/dev/zero 심볼릭 링크 상황)을 파일로 재현한다.
    path = tmp_path / "huge.jsonl"
    with open(path, "wb") as fh:
        fh.write(b"0" * (3 << 20))  # 개행 없는 3MB
    lines = list(hook._read_lines(str(path)))
    assert lines == []  # 한 줄 상한(1MB)에서 멈춘다.
    state = pathlib.Path(hook._state_file()).read_text(encoding="utf-8")
    assert "line exceeds" in state
    # main 은 여전히 exit 0 이고 보낼 것이 없으므로 전송하지 않는다.
    assert _run(hook, monkeypatch, {"transcript_path": str(path)}) is None


def test_lines_before_the_giant_line_are_kept(hook, tmp_path):
    path = tmp_path / "mixed.jsonl"
    with open(path, "wb") as fh:
        fh.write((_assistant(mid="m1") + "\n").encode("utf-8"))
        fh.write(b"0" * (2 << 20))
    agg = hook.aggregate(hook._read_lines(str(path)))
    assert agg["claude-opus-5"]["message_count"] == 1


def test_lines_after_the_giant_line_are_kept(hook, tmp_path):
    # 초과한 줄만 버리고 계속 읽는다 — 실측 최대 줄이 상한의 93%라 평범한 세션도 넘을 수 있고,
    # 그때 파일 나머지가 통째로 사라지면 조용한 과소집계가 된다.
    path = tmp_path / "mixed-after.jsonl"
    with open(path, "wb") as fh:
        fh.write(b"0" * (2 << 20) + b"\n")
        fh.write((_assistant(mid="m-after") + "\n").encode("utf-8"))
    state = hook._new_state()
    agg = hook.aggregate(hook._read_lines(str(path), state), state)
    assert agg["claude-opus-5"]["message_count"] == 1
    assert state["truncated"] is True  # 한 줄을 버렸으므로 부분 집계 표시는 남긴다.


def test_total_byte_budget_stops_reading(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_MAX_TOTAL_BYTES", 4096)
    # 예산 검사는 청크 하나를 읽은 뒤에 돌므로, 청크도 예산보다 작게 줄여야 중간에서 멈춘다
    # (실제 값은 청크 64KB / 예산 64MB 라 이 문제가 없다).
    monkeypatch.setattr(hook, "_READ_CHUNK_BYTES", 512)
    path = tmp_path / "many.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(400):
            fh.write(_assistant(mid=f"m{i}") + "\n")
    lines = list(hook._read_lines(str(path)))
    assert 0 < len(lines) < 400
    state = pathlib.Path(hook._state_file()).read_text(encoding="utf-8")
    assert "byte budget" in state


# -- 라벨 총량 상한(base64url 반출 차단) ---------------------------------------


def _b64ish(prefix: str, n: int) -> str:
    """문자셋 검증을 통과하는 base64url 문자열. -와 _가 곧 base64url 알파벳이다."""
    raw = base64.urlsafe_b64encode((prefix * 64).encode()).decode().rstrip("=")
    return (raw + "-_")[:n]


# 고정 리터럴은 채널이 아니다(입력이 고를 수 없는 상수). 예산이 묶는 것은 **입력에서 온**
# 라벨 문자이므로 측정에서도 리터럴을 뺀다 — 훅의 _spend_labels 회계와 같은 기준이다.
_FIXED_LABELS = ("<other>", "unknown")


def _label_chars(models: list[dict]) -> int:
    def cost(label: str) -> int:
        return 0 if label in _FIXED_LABELS else len(label)

    return sum(
        cost(m["model"])
        + sum(cost(k) for k in m["serviceTierCounts"])
        + sum(cost(k) for k in m["stopReasonCounts"])
        for m in models
    )


def test_base64url_passes_charset_but_total_budget_caps_the_channel(hook):
    # 개별 값은 문자셋을 통과한다 — 그래서 총량 상한이 필요하다.
    sample = _b64ish("x", 40)
    assert hook._label(sample, 48) == sample
    lines = [
        _assistant(
            mid=f"m{mi}-{ki}",
            model=_b64ish(f"M{mi}", 48),
            tier=_b64ish(f"T{mi}{ki}", 32),
            stop=_b64ish(f"S{mi}{ki}", 32),
        )
        for mi in range(50)
        for ki in range(19)
    ]
    models = hook.build_models(hook.aggregate(lines))
    total = _label_chars(models)
    assert total <= hook._MAX_LABEL_CHARS, total
    body = json.dumps({"sessionId": "s", "hostname": "h", "truncated": False, "models": models})
    assert len(body) < 4096, len(body)
    # 예산을 넘긴 라벨은 _OTHER 로 접히므로 임의 문자열이 페이로드에 남지 않는다.
    assert _b64ish("M40", 48) not in body
    assert "<other>" in body


def test_label_budget_does_not_disturb_normal_payloads(hook):
    models = hook.build_models(hook.aggregate(_transcript()))
    assert [m["model"] for m in models] == ["claude-opus-5", "claude-sonnet-5"]
    assert _label_chars(models) < hook._MAX_LABEL_CHARS
    assert all("<other>" not in json.dumps(m) for m in models)


def test_label_budget_reuses_existing_buckets_after_exhaustion(hook):
    # 예산이 바닥나도 이미 만든 버킷은 계속 쓴다(키가 늘지 않는다).
    lines = [_assistant(mid=f"m{i}", model=_b64ish(f"K{i}", 48)) for i in range(30)]
    lines += [_assistant(mid="tail", model="claude-opus-5")]
    agg = hook.aggregate(lines)
    assert len(agg) <= 50  # 리터럴 상한. 이 시나리오는 라벨 예산이 먼저 걸린다 — 모델 수 상한의 뮤테이션 검증은 test_model_count_cap_binds_before_label_budget.
    assert agg["<other>"]["message_count"] > 1


# -- 집계 중 자료구조 상한 -----------------------------------------------------


def test_huge_iterations_list_is_capped(hook):
    iters = [
        {
            "type": "message",
            "model": f"m{i}",
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 1,
            "cache_creation": {"ephemeral_1h_input_tokens": 1},
        }
        for i in range(100_000)
    ]
    line = json.dumps(
        {"type": "assistant", "message": {"id": "big", "model": "top", "usage": {"iterations": iters}}}
    )
    state = hook._new_state()
    agg = hook.aggregate([line], state)
    # 상한이 없으면 키가 10만 개가 된다.
    assert len(agg) <= 50  # 리터럴 상한. 이 시나리오는 라벨 예산이 먼저 걸린다 — 모델 수 상한의 뮤테이션 검증은 test_model_count_cap_binds_before_label_budget.
    assert state["truncated"] is True  # 버린 항목이 있으므로 과소집계 표시.


def test_unique_model_flood_is_capped_at_insertion(hook):
    lines = [
        _assistant(mid=f"u{i}", model=f"model-{i}", tier=f"tier-{i}", stop=f"stop-{i}")
        for i in range(20_000)
    ]
    agg = hook.aggregate(lines)
    assert len(agg) <= 50  # 리터럴 상한. 이 시나리오는 라벨 예산이 먼저 걸린다 — 모델 수 상한의 뮤테이션 검증은 test_model_count_cap_binds_before_label_budget.
    for bucket in agg.values():
        assert len(bucket["service_tier_counts"]) <= 20
        assert len(bucket["stop_reason_counts"]) <= 20
    # 접기는 합계를 보존한다: 메시지 수 총합이 입력 건수와 같다.
    assert sum(b["message_count"] for b in agg.values()) == 20_000


def test_model_count_cap_binds_before_label_budget(hook):
    # 위 시나리오들은 라벨 예산(256자)이 먼저 마르므로 모델 수 상한 자체는 시험하지 못한다.
    # 여기서는 이름을 2~3자로 줄이고 tier/stop을 비워(라벨 지출 배제) 상한에 실제로 닿게 한다:
    # m0~m48 라벨 합 137자 < 256. _MAX_MODELS 를 완화하면 60개 버킷이 생겨 여기서 잡힌다.
    lines = [_assistant(mid=f"c{i}", model=f"m{i}", tier=None, stop=None) for i in range(60)]
    agg = hook.aggregate(lines)
    assert len(agg) == 50  # 고유 49 + <other>. 리터럴 50 — 서버 스키마 max_length=50과 짝.
    assert "<other>" in agg
    assert sum(b["message_count"] for b in agg.values()) == 60


# -- 잘림 플래그 --------------------------------------------------------------


def test_truncated_flag_not_set_on_normal_input(hook, monkeypatch, transcript_file):
    out = _run(hook, monkeypatch, {"transcript_path": str(transcript_file)})
    assert out["payload"]["truncated"] is False


def test_truncated_flag_set_on_line_count_limit(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_MAX_LINES", 3)
    path = tmp_path / "many.jsonl"
    path.write_text(
        "\n".join(_assistant(mid=f"m{i}") for i in range(10)) + "\n", encoding="utf-8"
    )
    out = _run(hook, monkeypatch, {"transcript_path": str(path)})
    assert out["payload"]["truncated"] is True
    assert out["payload"]["models"][0]["messageCount"] == 3  # 부분 집계도 보낸다.


def test_truncated_flag_set_on_line_byte_limit(hook, monkeypatch, tmp_path):
    path = tmp_path / "mixed.jsonl"
    with open(path, "wb") as fh:
        fh.write((_assistant(mid="m1") + "\n").encode("utf-8"))
        fh.write(b"0" * (2 << 20))
    out = _run(hook, monkeypatch, {"transcript_path": str(path)})
    assert out["payload"]["truncated"] is True
    assert out["payload"]["models"][0]["messageCount"] == 1


def test_truncated_flag_set_on_total_byte_budget(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "_MAX_TOTAL_BYTES", 4096)
    monkeypatch.setattr(hook, "_READ_CHUNK_BYTES", 512)
    path = tmp_path / "long.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(400):
            fh.write(_assistant(mid=f"m{i}") + "\n")
    out = _run(hook, monkeypatch, {"transcript_path": str(path)})
    assert out["payload"]["truncated"] is True


def test_label_folding_alone_is_not_truncation(hook):
    # 접기는 합계를 보존하므로 잘림이 아니다 — 둘을 구별한다.
    state = hook._new_state()
    hook.aggregate([_assistant(mid="m1", model="not a valid label!!")], state)
    assert state["truncated"] is False


# -- 지연 실행(자기 자신을 분리 프로세스로 재실행) ---------------------------
# Claude Code가 Stop 훅 발화 **뒤에** 트랜스크립트에 assistant 레코드를 쓰기 때문에,
# 1차 호출은 기다리지 않고 자식(DEFERRED=1)에게 넘긴다. 위 `hook` 픽스처는 이미
# DEFERRED=1·_DEFER_MAX_SECONDS=0을 깔아 두므로, 아래 테스트는 필요한 만큼만 되돌린다.


def test_first_call_spawns_detached_child_and_returns_immediately(hook, monkeypatch, transcript_file):
    # 1차 호출(DEFERRED 미설정)이면 집계는 전혀 하지 않고 자식만 띄운다.
    monkeypatch.delenv("AMX_SESSION_USAGE_DEFERRED", raising=False)
    monkeypatch.setenv("AMX_SESSION_INGEST_URL", "http://ams.test/ingest")
    monkeypatch.setenv("AMX_SESSION_INGEST_TOKEN", "tok")
    monkeypatch.setattr(hook, "aggregate", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("1차 호출은 집계하면 안 된다")))

    calls: list[dict] = []
    written: list[bytes] = []

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            written.append(data)

        def close(self) -> None:
            pass

    class _FakeProc:
        def __init__(self):
            self.stdin = _FakeStdin()

    def _fake_popen(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(hook.subprocess, "Popen", _fake_popen)
    payload = {"session_id": "sess-1", "transcript_path": str(transcript_file)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert hook.main() == 0
    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["env"]["AMX_SESSION_USAGE_DEFERRED"] == "1"
    assert kwargs["start_new_session"] is True
    assert written and json.loads(written[0].decode("utf-8")) == payload


def test_first_call_swallows_popen_failure(hook, monkeypatch, transcript_file):
    monkeypatch.delenv("AMX_SESSION_USAGE_DEFERRED", raising=False)
    monkeypatch.setenv("AMX_SESSION_INGEST_URL", "http://ams.test/ingest")
    monkeypatch.setenv("AMX_SESSION_INGEST_TOKEN", "tok")

    def _boom(*a, **kw):
        raise OSError("no fork")

    monkeypatch.setattr(hook.subprocess, "Popen", _boom)
    payload = {"session_id": "sess-1", "transcript_path": str(transcript_file)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0  # Popen 실패도 exit 0 불변식을 지킨다.


def test_deferred_child_waits_for_assistant_records_to_appear(hook, monkeypatch, tmp_path):
    # 처음엔 assistant 레코드가 없는 트랜스크립트다가, 폴링 도중(=time.sleep 호출 시점에)
    # 줄이 추가되면 다음 시도에서 잡아 전송한다.
    path = tmp_path / "sess-live.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_DEFER_MAX_SECONDS", 5.0)

    def _fake_sleep(seconds):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_assistant(mid="m1") + "\n")

    monkeypatch.setattr(hook.time, "sleep", _fake_sleep)
    out = _run(hook, monkeypatch, {"session_id": "sess-live", "transcript_path": str(path)})
    assert out is not None
    assert [m["model"] for m in out["payload"]["models"]] == ["claude-opus-5"]


def test_deferred_child_gives_up_after_max_wait_without_sending(hook, monkeypatch, tmp_path):
    # 끝까지(=deadline까지) assistant 레코드가 없으면 아무 것도 보내지 않는다.
    path = tmp_path / "sess-empty.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_DEFER_MAX_SECONDS", 0.05)
    monkeypatch.setattr(hook, "_DEFER_POLL_INTERVAL_SECONDS", 0.01)
    out = _run(hook, monkeypatch, {"session_id": "sess-empty", "transcript_path": str(path)})
    assert out is None


if __name__ == "__main__":
    # 직접 실행(python3 <file>)도 pytest 스위트를 돌린다 — 훅과 마찬가지로 이 파일만
    # 있으면 검증이 되게 한다.
    import sys

    sys.exit(pytest.main([__file__, "-q"]))


def test_home_cwd_is_not_reported(hook, monkeypatch, transcript_file, tmp_path):
    """홈에서 띄운 세션은 cwd 를 빼고 보낸다(프로젝트 이름이 곧 계정명이 된다)."""
    home = tmp_path / "someuser"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    out = _run(
        hook,
        monkeypatch,
        {
            "session_id": "sess-home",
            "transcript_path": str(transcript_file),
            "cwd": str(home),
        },
    )
    assert out is not None
    assert "cwd" not in out["payload"]


def test_subdirectory_of_home_is_reported(hook, monkeypatch, transcript_file, tmp_path):
    home = tmp_path / "someuser"
    project = home / "work" / "AMX"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    out = _run(
        hook,
        monkeypatch,
        {
            "session_id": "sess-proj",
            "transcript_path": str(transcript_file),
            "cwd": str(project),
        },
    )
    assert out is not None
    assert out["payload"]["cwd"] == str(project)
