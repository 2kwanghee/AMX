'use client';

import type { EnrollTokenResponse } from '@/lib/api-client/types';
import { CopyButton, Modal, fmtTime } from '../common';

// 등록 토큰 모달 — 토큰만 보여주는 대신, 대상 머신에 그대로 붙여넣을 설치 명령까지
// 조립해 준다. 기본은 패키지형 설치(AMS가 서빙하는 install.sh를 curl로 받아 실행)이며,
// 대상 머신에 git·go·python이 없어도 된다. amsEndpoint/amsPubkey는 발급 응답에서
// 오며(app.config의 AMX_ADVERTISE_HOST·서명키에서 파생), 광고 host 미설정이면
// endpoint가 null이라 자리표시자로 대체한다. 인자는 셸 메타문자 대비로 큰따옴표로
// 감싸고, 자리표시자도 꺾쇠 없이 둔다. 소스 설치(git clone + agent-setup.sh)는 개발용
// 으로 접어 둔다 — 한 머신에 두 방식을 같이 두면 tsamx 슬롯이 충돌한다.
const AGENT_DIR = '~/AMX-agent';
const AGENT_REPO = 'https://github.com/2kwanghee/AMX.git';
const HOST_PLACEHOLDER = 'SERVER_IP';
const GRPC_PORT = 50051;
const REST_PORT = 8080;

// amsEndpoint에서 호스트만 뽑는다. 서버는 "host:port"로 광고하지만(AMX_ADVERTISE_HOST
// + gRPC 포트), 포트 없는 host나 IPv6가 올 수도 있으므로 형식을 알아볼 수 없으면
// null을 돌려주고 호출부가 자리표시자로 떨어진다.
function amsHostOf(endpoint: string | null | undefined): string | null {
  const raw = (endpoint ?? '').trim();
  if (!raw) return null;
  const bracketed = raw.match(/^(\[[0-9a-fA-F:]+\])(?::\d+)?$/); // [::1] · [::1]:50051
  if (bracketed) return bracketed[1] ?? null;
  const [head, port, ...rest] = raw.split(':');
  if (!head || rest.length > 0) return null;
  if (port === undefined) return head;
  return /^\d+$/.test(port) ? head : null;
}

export function EnrollTokenModal({ token, onClose }: { token: EnrollTokenResponse; onClose: () => void }) {
  const host = amsHostOf(token.amsEndpoint);
  const grpcAddr = host ? `${host}:${GRPC_PORT}` : `${HOST_PLACEHOLDER}:${GRPC_PORT}`;
  const baseUrl = `http://${host ?? HOST_PLACEHOLDER}:${REST_PORT}`;
  const pubkey = token.amsPubkey || 'AMS_PUBKEY';
  const noHost = !host;
  const noPubkey = !token.amsPubkey;

  const bashCmd =
    `curl -fsSL ${baseUrl}/install.sh | bash -s -- ` +
    `--ams "${grpcAddr}" --ams-url "${baseUrl}" ` +
    `--token "${token.token}" --pubkey "${pubkey}" --insecure`;

  // irm | iex 로는 파라미터를 못 넘겨서, 스크립트를 받아 scriptblock으로 실행한다.
  const ps1Cmd =
    `$s = irm ${baseUrl}/install.ps1; ` +
    `& ([scriptblock]::Create($s)) -Ams "${grpcAddr}" -AmsUrl "${baseUrl}" ` +
    `-Token "${token.token}" -Pubkey "${pubkey}" -Insecure`;

  const gitBlock =
    `git clone ${AGENT_REPO} ${AGENT_DIR}   # 최초 1회, 이미 받았으면 생략\n` +
    `cd ${AGENT_DIR} && git pull && bash deploy/agent-setup.sh install ` +
    `--ams "${grpcAddr}" --token "${token.token}" --pubkey "${pubkey}" --insecure`;

  return (
    <Modal title="등록 토큰 (한 번만 표시)" onClose={onClose}>
      <p className="muted">토큰은 지금 한 번만 표시됩니다. 아래 명령을 복사해 대상 머신에서 실행하세요.</p>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 2px' }}>
        <b style={{ fontSize: 13 }}>Linux · WSL · macOS</b>
        <CopyButton text={bashCmd} label="명령 복사" />
      </div>
      <pre className="guide-cmd">{bashCmd}</pre>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '12px 0 2px' }}>
        <b style={{ fontSize: 13 }}>Windows · PowerShell</b>
        <CopyButton text={ps1Cmd} label="명령 복사" />
      </div>
      <pre className="guide-cmd">{ps1Cmd}</pre>

      <p className="muted" style={{ marginTop: 10 }}>
        <b>신뢰 LAN 한정</b> — 이 명령은 평문 HTTP로 스크립트·바이너리를 받고
        <code>--insecure</code>로 gRPC도 평문으로 붙습니다. 매니페스트가 위 공개키로 서명
        검증되고 산출물은 sha256으로 대조되므로 변조된 바이너리는 걸러지지만, 토큰 등
        요청 내용은 그대로 노출됩니다. 사내 신뢰 LAN 밖에서는 TLS(<code>--ca</code>)로
        전환하세요. PC도 <code>--insecure-grpc</code>로 떠 있어야 합니다.
      </p>

      {noHost && (
        <p className="muted">
          광고 주소 미설정 또는 형식 미상 — 명령의 <code>{HOST_PLACEHOLDER}</code>을 실제 AMS
          호스트(IP 또는 도메인)로 바꾸세요. 서버에 <code>AMX_ADVERTISE_HOST</code>를 지정하면
          이 값이 자동으로 채워집니다.
        </p>
      )}
      {noPubkey && (
        <p className="err">
          AMS 서명 공개키를 받지 못했습니다 — <code>AMS_PUBKEY</code> 자리를 실제 키로 바꾸지
          않으면 매니페스트 서명 검증이 불가능해 설치가 중단됩니다.
        </p>
      )}

      <p className="muted">
        <b>portproxy · 방화벽</b> — 대상 머신이 다른 호스트면 PC의 gRPC({GRPC_PORT})·
        REST({REST_PORT}) 포트로 접근 가능해야 합니다. WSL에서 구동 시 관리자 PowerShell에서
        <code>netsh interface portproxy</code>로 두 포트를 전달하고, 방화벽 인바운드를 허용하세요.
      </p>

      <details style={{ marginTop: 10 }}>
        <summary className="muted" style={{ cursor: 'pointer' }}>개발용 — 소스 설치(git clone)</summary>
        <p className="muted" style={{ marginTop: 8 }}>
          저장소를 직접 받아 빌드하는 방식입니다. 대상 머신에 git·go·python이 필요하며,
          위 패키지 설치와 한 머신에 함께 두지 마세요.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '4px 0 2px' }}>
          <CopyButton text={gitBlock} label="명령 복사" />
        </div>
        <pre className="guide-cmd">{gitBlock}</pre>
      </details>

      <p className="muted" style={{ marginTop: 10 }}>만료 {fmtTime(token.expiresAt)}</p>
    </Modal>
  );
}
