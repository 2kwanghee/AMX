# F1 테넌트 RBAC 상세 설계 (P5 S2)

> REASONER 산출(2026-08-09). E3(Principal 반환형) 위. 사용자 확정: 자체 admins+bcrypt, 2-role.
> SSOT는 `docs/AMX-DESIGN.md`. proto/gRPC 무변경(RBAC은 REST 평면).

## 핵심 결정
- **집행 = 라우터 공통 의존성**. accounts/servers/assignments/alerts는 prefix `/tenants/{tenant_id}` → `require_tenant_scope`(tenant_id 경로 + Principal) 하나를 라우터 `dependencies`에 → 엔드포인트 누락 불가. 스코프 dep + 서비스층 재검증 + 복합 FK + 메타테스트 = 4중.
- **부트스트랩 = AMX_ADMIN_TOKEN 유지**. 루트 Bearer는 항상 `global-admin/all_tenants`(하위호환·M2M·break-glass), admins 비어도 잠금 불가.
- **세션 = DB opaque 토큰**(`admin_sessions`, 해시 저장). 로그인 email+password(bcrypt)→토큰. require_admin이 Bearer를 (1)AMX_ADMIN_TOKEN 상수비교→(2)세션 해시조회 순 검증.
- **교차 테넌트=404**(은닉, 서비스층 일치), **역량 거부=403**(tenant create/관리). 스코프(404) 먼저→역량(403).
- **BFF는 per-admin 세션토큰 상류 전달**(공유 루트 토큰 아님) → 스코핑을 ams-server가 실집행.
- **Principal 타입 무변경**(S1의 role/all_tenants/tenant_ids가 두 역할 표현).

## 1. 스키마 (§5.1 확장, alembic 0007)
```
admins:
  id uuid pk; email text UNIQUE(lower(email)); password_hash text(bcrypt);
  role text('global-admin'|'tenant-admin');
  tenant_id uuid NULL FK->tenants(id) ON DELETE RESTRICT;   -- 격리 앵커
  disabled bool default false; created_at, updated_at
  CHECK ((role='global-admin' AND tenant_id IS NULL) OR (role='tenant-admin' AND tenant_id IS NOT NULL))
admin_sessions:
  id uuid pk; admin_id uuid FK->admins ON DELETE CASCADE;
  token_hash text UNIQUE(sha256(raw)); created_at, expires_at
```
- tenant-admin 다수/테넌트 허용. crypto.new_token()/hash_token() 재사용(enroll 패턴).

## 2. 부트스트랩 (잠금 방지)
- require_admin 우선순위1 = compare_digest(token, admin_token) → Principal(global-admin, all_tenants=True). admins 무관 상시 유효 → 구조적 잠금 불가.
- 첫 인간 global-admin: 루트 토큰 `POST /admins` 또는 CLI `python -m app.admin_cli create-admin`(DB write, 토큰 노출 없이) 권장.
- 루트 토큰 = M2M/break-glass 영구 유지.

## 3. 세션 인증
```
POST /auth/login {email,password} → admin 조회(정규화 email), disabled 401,
  bcrypt.checkpw(sha256_prehash(pw), hash) → admin_sessions INSERT(hash, exp=now+TTL)
  → {session_token, role, tenant_ids, expires_at}
POST /auth/logout → 세션행 삭제
require_admin(Bearer X):
  if compare_digest(X, admin_token): Principal(global-admin, all_tenants=True)  # 부트스트랩
  row = admin_sessions where token_hash=sha256(X) and exp>now; none/disabled → 401
  return admin→Principal  # global: all_tenants / tenant: tenant_ids={str(tenant_id)}
```
- 세션 DB(revoke·만료). TTL 8h. bcrypt 직접(passlib 지양), 72B 절단은 sha256 프리해시.

## 4. 스코핑 집행 (핵심)
```python
def require_tenant_scope(tenant_id: UUID, principal=Depends(require_admin)) -> Principal:
    if principal.all_tenants or str(tenant_id) in principal.tenant_ids: return principal
    raise not_found("tenant")            # 404 은닉
def require_global_admin(principal=Depends(require_admin)) -> Principal:
    if principal.role != "global-admin": raise ApiError(403, ...)
    return principal
```
- accounts/servers/assignments/alerts: `dependencies=[AdminAuth]`→`[Depends(require_tenant_scope)]`(각 1줄). tenant_id는 prefix서 주입, require_admin 캐시 1회.
- tenants.py(prefix `/tenants`, tid 없는 collection): GET list=스코프 필터(all→전체/아니면 tenant_ids만), POST=require_global_admin(403), GET/{tid}=스코프(404), PATCH/DELETE/{tid}=스코프(404) 먼저→역량(403, tenant-admin은 자기것도 rename/delete 불가).
- tid 없는 것=tenants collection + 신규 `/auth/*`(무인증 login), `/admins`(require_global_admin).
- **메타 테스트**: 전 라우트 순회, `/tenants/{tenant_id}` 계열이 require_tenant_scope 라우터에 속하는지 assert(미래 누락 방어).

## 5. BFF 연동 (S2c)
- POST /bff/session: password-only→email+password → 상류 /auth/login → per-admin 세션토큰.
- upstream.ts가 상류 Bearer를 공유 env.adminToken→해당 admin 세션토큰 교체.
- 세션토큰 브라우저 미노출: 현 쿠키는 서명(가독)뿐 → **AES-GCM 암호화 승격**(시크릿 담으므로). role/tenant_ids도 쿠키(서명)로 nav 필터(UI 편의, 집행은 ams-server). 루트 토큰 BFF env 잔존(break-glass).

## 6. 테스트
- bcrypt roundtrip/오답·disabled 401/세션 발급·인증·만료·logout 401.
- 루트토큰→global-admin, 기존 인증 테스트 그린(무회귀).
- global-admin 임의 테넌트 4리소스 200; tenant-admin 자기 200/타 4리소스 404(라우터별 parametrize)/GET tenants 자기만/POST 403/자기 PATCH·DELETE 403/타 404.
- /admins tenant-admin 403. 서비스층 격리 단위(스코프 우회 가정에도 404+복합 FK).

## 7. 단계 분할·R
- **S2a (R3, 2인+ADVERSARY)**: admins+admin_sessions·bcrypt·/auth/login|logout·require_admin 세션확장·require_tenant_scope 전 라우터·tenants.py 스코프/역량·부트스트랩(루트+CLI)·메타테스트. 보안 완결 단위.
- **S2b (R2)**: /admins 관리 API(global-admin가 tenant-admin CRUD·disable) + delete_tenant admins 잔존 검사(409). S2a 의존.
- **S2c (R2, 쿠키암호화만 R3 lean)**: BFF(email+password·per-admin 토큰 상류·쿠키 AES-GCM·nav 필터). S2a 의존, S2b 병렬.

## 8. 위험
- 스코핑 누락→4중(공통 dep+서비스층+복합 FK+메타테스트). 부트스트랩 잠금→루트토큰 독립. 세션 탈취→httpOnly+strict+secure·짧은 TTL·revoke·해시·AES-GCM. M2M 회귀→루트 우선순위1 무변경·기존 테스트 그린. 404/403 누출→순서 고정·테스트. bcrypt 72B→sha256 프리해시. proto/gRPC 무변경.

## SSOT 수정 (오케스트레이터 반영)
- §5.1: admins·admin_sessions 추가. §7 REST 인증 행: 다중관리자·세션·2-role·공통 집행·교차404·부트스트랩. openapi: /auth/*·/admins·403·404. proto 무변경.
