# F2 봉투암호화 설계서 (P5 S3)

> 이 문서는 설계 시점 기록(as-designed)이며, 현행 동작의 기준은 `docs/AMX-DESIGN.md`다.

> REASONER 산출(2026-08-09). KMS 벤더 미정 → KEK provider 추상화(로컬 MVP + KMS 어댑터 자리).
> SSOT는 `docs/AMX-DESIGN.md`. proto 무변경(at-rest 전용, 와이어 세션 KEK는 별개).

## 핵심 결정
1. **단일 초크포인트가 이중암호 붕괴 방지**: 모든 at-rest 접근이 `crypto.encrypt_secret`/`decrypt_secret` 두 함수·5 호출부만 통과(전수 확인). 두 함수에 `tenant_id` 추가 → O9 포함 전 경로 자동 DEK 경유. **독립 ciphertext 생성 경로 부재 = 정확성 불변식.**
2. KEK provider 추상화 + 로컬 provider(MVP) + KMS 어댑터 자리. `AMX_KEK_PROVIDER=local|aws-kms|vault`.
3. `tenant_deks` 테이블(컬럼 아님) — lazy 재암호로 구/신 DEK 버전 공존.
4. AES-256-GCM + 버전태그(v2) 신규, 기존 Fernet은 태그 부재로 식별해 레거시 복호. 쓰기 플래그로 롤백 경계.

## 1. at-rest 접근점 전수 (5곳, 전부 두 함수 경유)
| # | 경로 | 파일 | R/W | tenant_id |
|---|---|---|---|---|
| 1 | 생성+oauth 등록 | inventory.create_account / accounts.py | W | 인자 |
| 2 | secret 수정 | inventory.update_account | W | 인자 |
| 3 | **O9 역동기화** | grpc/server.py _apply_cred_update | W | WHERE 스코프 |
| 4 | deliver 복호→세션KEK 재봉인 | grpc/server.py _build_deliver | R | account.tenant_id |
| 5 | ops 검증 | scripts/verify_credential.py | R | --tenant |
5곳 모두 tenant_id 스코프에 있음 → 시그니처 확장만, 로직 재작성 불요.

## 2. KEK provider 인터페이스
```python
class KekProvider(Protocol):
    def wrap_dek(self, dek, *, tenant_id) -> tuple[bytes, str]  # (wrapped, key_id)
    def unwrap_dek(self, wrapped, *, tenant_id, key_id) -> bytes
    provider_id: str
```
- 로컬 provider: `AMX_KEK`(신규 env; 전환기 `AMX_ENCRYPTION_KEY` 재사용 허용) 32B로 DEK를 **AES-256-GCM 래핑, AAD=tenant_id** → A wrapped-DEK를 B로 unwrap 불가.
- KMS 어댑터: 추상 베이스 + KMS Encrypt/Decrypt(EncryptionContext={tenant}). 벤더 정해지면 채움. key_id=KMS ARN/version.
- config.py `kek_provider` 팩토리(R0 설정).

## 3. DEK 스키마 (migration 0008)
`tenant_deks(id, tenant_id FK RESTRICT, version int, wrapped_dek bytea, kek_provider, kek_key_id, algorithm default 'AES-256-GCM', created_at, retired_at NULL)`, `UNIQUE(tenant_id, version)`. 활성 = retired_at NULL 중 max version.
- DEK 생성 = 테넌트 생성 시(create_tenant) + 마이그레이션 백필. 첫-쓰기 레이스 제거.

## 3′. ciphertext 포맷
텍스트 컬럼 유지. 신규 `v2:{dek_version}:{b64(nonce)}:{b64(ct)}`. 복호: `v2:` → DEK-GCM(tenant+version으로 wrapped_dek 조회→unwrap→open, AAD=tenant_id); 접두 없음 → 레거시 Fernet. 암호화는 항상 활성 DEK v2.

## 4. 마이그레이션·롤백 (무중단)
- **A(0008)**: tenant_deks 생성 + 전 테넌트 DEK 백필(로컬 KEK 래핑, v1). accounts 무접촉.
- **B(코드)**: 초크포인트 두 함수 tenant_id 추가. 복호 v2/레거시 자동 분기. **v2 쓰기는 `AMX_ENVELOPE_WRITE=1` 게이트** → 코드 먼저 배포·검증 후 flip = 깨끗한 롤백 경계.
- **C(lazy)**: 쓰기 경로(#1 O9-push·#2 수정·재등록)가 다음 기록 시 v2 승격. 냉로우용 배치 rewrap 스크립트.
- deliver(#4) 트리거 재암호 **기각**(읽기에 쓰기 추가→경합·observed_at 단조성 상호작용). deliver 읽기전용 유지.

## 5. DEK 캐시·가용성
unwrap DEK를 (tenant_id, version) 키로 in-process 캐시(TTL+max). 로테이션 시 무효화. 미스+KMS 장애 → 재시도·백오프. 평문 DEK 상주는 현 AMX_ENCRYPTION_KEY 신뢰수준과 동일.

## 6. 테스트
테넌트 DEK 격리(A ct를 B DEK로 open 실패, AAD), 5 접근점 왕복, 로컬 provider 왕복, 레거시 Fernet 읽기 유지, lazy 승격(다음 쓰기 v2), O9 push DEK 경유, fake-KMS 교체, 백필. 기존 172 유지.

## 7. 단계 분할·R
- **S3a (R2)**: provider iface + tenant_deks + 로컬 provider + 백필(0008), 플래그 off.
- **S3b (R3, 2인+ADVERSARY)**: 초크포인트 재작성(tenant_id·v2·레거시) + 5 호출부 + **O9 조율**.
- **S3c (R2)**: lazy 재암호 + 배치 rewrap.

## 8. 위험·미해결
- KMS 벤더 미정: 어댑터 스텁, provider_id/key_id 배관 완비.
- O9 경합: 단일 초크포인트 + 단조 WHERE 불변으로 해소(DEK가 단조성 미접촉).
- 로컬 KEK 한계(정직): 봉투구조·테넌트 DEK 격리·KMS-ready 획득하나 MVP 로컬 KEK는 단일 env 시크릿이라 기밀 강화는 실 KMS 도입 시. 격리·구조가 이득.
- **롤백 이중성(리뷰 반영)**: 플래그 on→off는 **신 코드 유지 시에만** 안전(v2 자동판별 읽기). **코드 롤백**(F2 이전)이나 **`0008` downgrade**는 v2 판독 불가 → **v2→Fernet 역-rewrap(`rewrap_secrets.py --reverse`) 선행 필수**; downgrade는 v2 존재 시 refuse 가드. **rewrap↔O9 경합**: 배치 rewrap은 CAS UPDATE(`WHERE encrypted_secret=:old`)로 O9 lost-update(stale 부활) 방지. DEK 로테이션 배치: 후속(lazy 기본).
- **provider 디스패치**: `_unwrap_cached`가 `tenant_deks.kek_provider` 존중. local→KMS 혼재는 기존 DEK 재래핑(후속 스크립트) 선행 — §2 "provider swap"은 신규 DEK부터.
- [발견물] mask_secret 미솔트 — 테넌트 DEK에서 salt 파생 가능하나 범위 밖.

## proto/SSOT
- proto 무변경. §5.1 tenant_deks 추가, §7 At-rest SaaS 행을 테넌트별 DEK+KEK provider 봉투암호화(local MVP, KMS-ready)로.
