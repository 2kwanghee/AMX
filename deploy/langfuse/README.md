# deploy/langfuse

Claude Code 세션을 셀프호스팅 Langfuse로 흘려보내는 Stop 훅과 부속 파일.

## langfuse_hook.py

Langfuse가 공개한 Claude Code observability 플러그인
([langfuse/claude-observability-plugin](https://github.com/langfuse/claude-observability-plugin),
MIT)에서 훅 스크립트만 그대로 가져와 넣었다(vendoring). 상단 uv 인라인
메타데이터로 `langfuse>=4.0,<5`를 자동으로 끌어와 `uv run --script`로 돈다.

받아온 그대로 두고 손대지 않는다. 저장소에 고정해 둔 이유는 두 가지다. 하나는
배포 대상 서버가 인터넷 플러그인 마켓을 거치지 않고 이 저장소 하나만으로 설치를
끝낼 수 있어야 하기 때문이고(내부망·오프라인 전제), 다른 하나는 러너 전 대수가
같은 버전을 봐야 추적 결과가 서로 어긋나지 않기 때문이다. 업스트림을 따라가려면
원본을 다시 복사해 이 파일을 통째로 교체하고, 아래 vendored 날짜만 갱신한다.
훅 동작이 바뀔 수 있으니 교체는 별도 커밋으로 남긴다.

- vendored: 2026-08-15
- 원본 라이선스: `LICENSE-langfuse-hook`

## 설치

`deploy/install-langfuse-hook.sh`가 이 디렉터리의 훅을
`$CLAUDE_CONFIG_DIR/hooks/`로 복사하고 `settings.json`에 Stop 훅을 멱등 병합한다.
자세한 절차는 `docs/DEPLOYMENT-RUNNER.md`의 Langfuse 추적 절을 본다.
