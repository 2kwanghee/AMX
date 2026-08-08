# AMX 전송 보안(TLS) 배포 가이드

AMX 제어 평면은 AMA(에이전트) → AMS(서버) 방향의 장수명 gRPC 스트림 하나로
동작한다(§5.4). 이 채널은 credential 세트(재주입 시)와 KEK(SessionSetup)를
운반하므로 **프로덕션에서는 TLS가 필수**다(설계 §7 In-transit). 이 문서는
인증서 발급, AMS·AMA 환경변수, 로테이션 절차, insecure 옵트인의 위치를 정리한다.

## 1. 두 개의 인증 계층 — TLS와 server_credential은 별개

| 계층 | 무엇을 인증하나 | 메커니즘 | 필수 여부 |
|---|---|---|---|
| 전송(TLS) | **연결(wire)**을 암호화하고 서버(선택적으로 클라이언트) 신원 검증 | X.509 인증서 | 프로덕션 필수(§7) |
| 앱(server_credential) | **에이전트**를 식별하고 tenant에 바인딩 | enroll-token → 장수명 server credential (§AMA 인증) | 항상 필수 |

핵심: **TLS를 켜도 앱 인증은 사라지지 않는다.** AMA는 매 세션 Register에
server_credential(최초엔 enroll-token)을 제시하고, AMS는 이를 해시 비교해
tenant로 바인딩한다. 따라서:

- **one-way TLS**(서버 인증서만) = 채널 암호화 + 서버 신원 검증. 여기에 앱 계층
  server_credential이 이미 에이전트를 인증하므로, **§7 In-transit 요건을 충족하는
  기본 구성**이다.
- **mTLS**(클라이언트 인증서 추가) = 전송 계층에서도 상호 신원 검증. 앱 인증
  위에 얹는 **defense-in-depth 옵션**이며 필수는 아니다. TLS 종단 자체를
  화이트리스트로 좁히고 싶은 규제/폐쇄망 배포에서 켠다.

## 2. 환경변수 매트릭스

### AMS(서버, `ams-server/app/grpc/server.py::configure_port`)

| 변수 | 의미 |
|---|---|
| `AMX_GRPC_TLS_CERT` | 서버 인증서 PEM 경로 |
| `AMX_GRPC_TLS_KEY` | 서버 개인키 PEM 경로 |
| `AMX_GRPC_TLS_CA` | 설정 시 **mTLS 활성화** — 클라이언트 인증서를 이 CA로 검증하고 요구(`require_client_auth=True`) |
| `AMX_GRPC_ALLOW_INSECURE=1` | CERT/KEY가 없을 때만 평문 기동 허용(개발 전용, 경고 로그) |

- CERT+KEY만: **one-way TLS**.
- CERT+KEY+CA: **mTLS**(클라이언트 인증서 필수).
- 셋 다 없고 옵트인도 없으면 서버는 **기동 거부**(fail-closed).

### AMA(에이전트, `ama-agent/internal/transport/transport.go::SecurityDialOption`)

| 변수 | 의미 |
|---|---|
| `AMX_AMS_TLS_CA` | 설정 시 **TLS 활성화** — 이 CA 번들로 AMS 서버 인증서 검증 |
| `AMX_AMS_TLS_SERVER_NAME` | (선택) 검증할 SNI/인증서 이름 override. 미설정 시 다이얼 호스트 사용 |
| `AMX_AMS_TLS_CLIENT_CERT` | (mTLS) 제시할 클라이언트 인증서 PEM 경로 |
| `AMX_AMS_TLS_CLIENT_KEY` | (mTLS) 클라이언트 개인키 PEM 경로 |
| `AMX_GRPC_ALLOW_INSECURE=1` | CA가 없을 때만 평문 다이얼 허용(개발 전용) |

- CA만: **one-way TLS**(서버 검증, 클라이언트 익명).
- CA + CLIENT_CERT + CLIENT_KEY: **mTLS**(클라이언트 인증서 제시).
- CLIENT_CERT/KEY 중 **한쪽만** 설정하면 오류로 **기동 거부**(익명 폴백으로
  조용히 약화되지 않도록 fail-closed).
- CA도 옵트인도 없으면 다이얼 **거부**(fail-closed).

### 구성 조합 요약

| 배포 형태 | AMS | AMA |
|---|---|---|
| one-way TLS(권장 기본) | `TLS_CERT`+`TLS_KEY` | `TLS_CA` |
| mTLS(defense-in-depth) | `TLS_CERT`+`TLS_KEY`+`TLS_CA` | `TLS_CA`+`CLIENT_CERT`+`CLIENT_KEY` |
| 평문(개발 전용) | `ALLOW_INSECURE=1` | `ALLOW_INSECURE=1` |

**주의:** AMS가 `TLS_CA`를 설정해 mTLS를 요구하면, AMA도 반드시 클라이언트
인증서를 설정해야 한다. 한쪽만 mTLS면 연결이 거부된다.

## 3. 인증서 발급 옵션

세 가지 중 배포 환경에 맞게 선택한다. AMA가 신뢰하는 `AMX_AMS_TLS_CA`에는
**서버 인증서를 서명한 CA**(체인의 루트/중간)를 넣는다.

1. **내부 CA (권장, 다중 호스트)** — 조직 내부 CA(step-ca, Vault PKI, 사내 PKI)로
   서버·클라이언트 인증서를 발급. AMA에는 내부 CA 인증서만 배포. 로테이션·폐기를
   중앙에서 관리. mTLS 클라이언트 인증서도 여기서 발급.
2. **Self-signed (소규모/폐쇄망)** — 단일 자체서명 CA를 만들고 그 CA로 서버(및
   mTLS 시 클라이언트) 인증서를 서명. 서버 인증서 SAN에 AMA가 다이얼하는
   호스트명/IP를 반드시 포함(미포함 시 검증 실패). AMA에 CA 인증서 배포.
   *예시 절차:*
   ```
   # CA
   openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
     -keyout ca.key -out ca.crt -days 3650 -subj "/CN=amx-internal-ca"
   # 서버 CSR + 서명 (SAN에 실제 호스트/IP)
   openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
     -keyout server.key -out server.csr -subj "/CN=ams.internal"
   openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
     -out server.crt -days 365 \
     -extfile <(printf "subjectAltName=DNS:ams.internal,IP:10.0.0.10")
   # (mTLS) 클라이언트 인증서: extendedKeyUsage=clientAuth 로 발급
   ```
3. **공인 CA (인터넷 노출 AMS)** — Let's Encrypt 등으로 서버 인증서 발급. AMA는
   해당 공인 루트를 신뢰(시스템 트러스트 스토어 대신 `AMX_AMS_TLS_CA`에 명시
   고정 권장). mTLS 클라이언트 인증서는 공인 CA로 발급하지 않으므로, mTLS가
   필요하면 클라이언트 측만 내부 CA를 별도로 쓴다.

## 4. 로테이션 절차

**서버 인증서(AMS):** 새 cert/key를 배포 경로에 배치 → AMS 프로세스를
그레이스풀 재시작(새 스트림부터 새 인증서 적용). AMA는 재연결 시 백오프
(1s→30s)로 자동 재접속하므로 무중단에 가깝다. CA를 바꾸는 경우 **CA를 먼저
AMA 측 번들에 추가 배포**(구·신 CA 동시 신뢰)한 뒤 서버 cert를 신 CA로 교체하고,
전환 완료 후 구 CA 제거 — 이 순서를 지키면 검증 실패 창(window)이 없다.

**클라이언트 인증서(AMA, mTLS):** 새 클라이언트 cert/key 배치 후 AMA 재시작.
AMS의 `TLS_CA`가 발급 CA를 이미 신뢰하면 무중단.

**만료 관리:** 만료된 서버 인증서는 AMA가 거부한다(검증 실패). 만료 전 갱신
알람을 CA/모니터링에서 걸어둔다. 인증서 유효기간은 짧게(예: 90일) + 자동 갱신
권장.

## 5. insecure 옵트인은 개발 전용

`AMX_GRPC_ALLOW_INSECURE=1`은 **로컬 개발·E2E 전용**이다. 이 모드에서는 KEK와
credential이 평문으로 와이어에 노출된다(AMS는 기동 시 경고 로그를 남긴다).
**프로덕션에서 절대 설정하지 말 것.** 로컬 E2E 하네스(`e2e/conftest.py`)는 실
네트워크 없이 loopback에서만 이 플래그로 기동한다.

## 6. 검증 체크리스트

- AMS 기동 로그에 평문 경고가 **없어야** 한다(TLS 정상).
- AMA가 잘못된 CA/만료 서버 인증서/평문 서버에 연결 시 **거부**되는지 확인
  (`ama-agent/internal/transport/transport_tls_test.go`가 이 네거티브를 커버).
- mTLS 배포에서 클라이언트 인증서 없는 AMA가 **거부**되는지 확인.
- one-way·mTLS 정상 경로에서 Register→SessionSetup 왕복이 성공하는지 확인
  (동일 테스트가 커버).
