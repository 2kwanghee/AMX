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

## docker-compose.yml — Langfuse v4 서버

훅이 데이터를 흘려보낼 대상 서버를 이 저장소 하나로 띄운다. 공식 원본 compose를
기반으로 하되, 운영에서 걸렸던 결함을 기동 순서로 막았다.

- **기동 순서 고정.** PoC에서는 첫 `up` 때 워커(`langfuse-worker`)가 웹
  (`langfuse-web`)의 Postgres 스키마 마이그레이션이 끝나기 전에 떠서
  `relation "..." does not exist` 류 Prisma 오류를 내고, 사람이 워커를 다시
  올려야 했다. 여기서는 웹에 `/api/public/health` 헬스체크를 달고 워커가
  `depends_on: langfuse-web: condition: service_healthy`로 웹이 healthy가 된
  뒤에만 뜨도록 해, 첫 기동부터 재시작 없이 정상화된다.
- **시크릿은 전부 `.env` 참조.** 기본값에는 `# CHANGEME` 표식을 남겨 뒀다.
- **Postgres/ClickHouse는 UTC 고정**(공식 문서 요구). `TZ`로 조정한다.
- 웹 포트는 `${LANGFUSE_WEB_PORT:-3100}`. 웹(3100)과 minio(9090)를 뺀 모든
  포트는 `127.0.0.1`에만 바인딩한다.

### 기동 절차

```bash
cd deploy/langfuse
cp env.example .env            # CHANGEME 값을 전부 교체
#  - NEXTAUTH_SECRET / SALT : openssl rand -base64 32
#  - ENCRYPTION_KEY         : openssl rand -hex 32  (정확히 64 hex)
docker compose config          # 구성 병합·문법 검증
docker compose up -d
```

### 헬스 확인

```bash
docker compose ps                       # web=healthy, worker=running 확인
curl -fsS http://localhost:3100/api/public/health   # {"status":"OK"} 류
docker compose logs -f langfuse-worker  # relation 오류가 없어야 정상
```

웹 헬스체크는 `start_period` 30초 뒤부터 판정한다. 마이그레이션이 오래 걸리면
`healthy` 전환이 그만큼 늦고, 그 사이 워커는 대기한다(의도된 동작).

### 업그레이드 주의 (공식 문서 기준)

- 이미지 태그는 메이저 고정(`langfuse/langfuse:4`, `langfuse/langfuse-worker:4`).
  메이저를 넘길 때는 릴리스 노트의 마이그레이션·파괴적 변경을 먼저 확인한다.
- 업그레이드도 웹이 스키마 마이그레이션을 적용하므로 웹→워커 순서를 유지한다.
  `docker compose pull && docker compose up -d`로 올리면 이 compose의 기동
  조건이 순서를 강제한다.
- 스키마가 바뀌는 업그레이드 전에는 Postgres·ClickHouse 볼륨을 백업한다.
  `docker compose down`은 볼륨을 지우지 않지만 `-v`는 전소시키므로 쓰지 않는다.
