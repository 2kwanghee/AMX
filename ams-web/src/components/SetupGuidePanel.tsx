'use client';

import { useMemo, useState } from 'react';
import { Icon } from './common';

// 설치·운영 가이드는 정적 콘텐츠다. API·SWR 없이 아래 데이터를 렌더한다.
// 이틀간 PC(fullstack)·노트북(에이전트) 셋업에서 실제로 겪은 이슈를 증상→원인→
// 해결로 정리했다. 새 이슈가 재발하면 이 배열에 항목을 추가한다.
type Fix = {
  symptom: string;
  cause: string;
  fix: string;
  code?: string;
  warn?: boolean;
};
type Section = { title: string; hint: string; items: Fix[] };

const GUIDE: Section[] = [
  {
    title: '원격 에이전트 업데이트',
    hint: '콘솔의 "에이전트 업데이트" 버튼 관련',
    items: [
      {
        symptom: '업데이트 버튼을 누르면 "unknown_command"로 실패한다',
        cause:
          '그 서버의 에이전트가 아직 SelfUpdate 기능이 없는 구버전이다. 콘솔 버튼은 에이전트에 이미 그 기능이 깔려 있어야 동작한다 — 없는 버전에 원격 업데이트를 걸면 명령 자체를 못 알아듣는다.',
        fix: '해당 머신에서 최초 한 번만 손으로 받아 재빌드한다. 이 부트스트랩이 끝나면 그때부터 버튼이 동작한다. 대시보드 서버 표의 버전이 "p3"에서 "p3+커밋해시"로 바뀌면 성공이다.',
        code: 'cd ~/AMX-agent\nbash deploy/agent-run.sh down\ngit pull\nbash deploy/agent-run.sh up --insecure \\\n  --ams 10.60.1.15:50051 \\\n  --pubkey "<PC .amx-dev/dev.env 의 AMX_AMS_PUBKEY>" \\\n  --config-dir ~/.claude-amx --tsamx-bin ~/.local/bin/tsamx',
      },
      {
        symptom: '버튼을 눌러도 버전이 그대로다 (계속 "p3")',
        cause:
          'git pull이 인증 실패로 안 됐거나, 옛 프로세스가 살아 있어 재빌드가 걸러진 것이다. 대시보드 버전에 커밋 해시가 안 붙어 있으면 새 바이너리가 안 뜬 것이다.',
        fix: '아래 "git pull 인증 실패"와 "재기동했는데 새 코드가 안 뜬다" 항목을 차례로 확인한다.',
      },
    ],
  },
  {
    title: 'git 내려받기와 빌드',
    hint: '에이전트 코드 갱신·재빌드에서 막힐 때',
    items: [
      {
        symptom:
          'git pull이 "Password authentication is not supported" 또는 403 "Write access not granted"로 막힌다',
        cause:
          'GitHub는 비밀번호 인증을 없앴다. 비밀번호 자리에는 계정 암호가 아니라 토큰(PAT)을 넣어야 한다. 403은 Fine-grained 토큰이 이 저장소 권한을 못 받았을 때 나온다.',
        fix: 'Classic 토큰을 repo 스코프로 발급한다(Settings → Developer settings → Tokens classic). 원격 업데이트도 노트북에서 git pull을 돌리므로, 자격증명을 저장해두지 않으면 콘솔 버튼도 같은 지점에서 막힌다. 그래서 store에 한 번 저장해둔다.',
        code: 'cd ~/AMX-agent\nrm -f ~/.git-credentials\ngit config --global credential.helper store\ngit pull\n#   Username: <GitHub 계정>\n#   Password: ghp_...  (classic, repo 스코프)',
      },
      {
        symptom: '"go가 없습니다 (1.24+ 필요)"로 빌드가 멈춘다',
        cause: 'Go 툴체인이 PATH에 안 잡혀 있다.',
        fix: '노트북은 /usr/local/go/bin, PC는 /home/lkh/go-toolchain/go/bin을 PATH에 넣는다. 원격 업데이트가 스스로 재빌드할 때도 같은 PATH를 쓰므로 ~/.bashrc에 넣어 영구히 잡아둔다.',
        code: "echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc\nsource ~/.bashrc\ngo version   # go1.24.5 확인",
      },
      {
        symptom: '재기동했는데 새 코드가 안 뜬다 (버전 그대로)',
        cause:
          'down이 옛 프로세스를 못 죽여서, up이 "이미 실행 중"으로 판단해 재빌드를 건너뛴 것이다.',
        fix: '프로세스와 옛 바이너리를 강제로 정리하고 다시 세운다. up이 "빌드: go build" 줄을 새로 출력해야 재빌드가 된 것이다.',
        code: 'cd ~/AMX-agent\nbash deploy/agent-run.sh down\npkill -f ".amx-agent/ama"\nrm -f .amx-agent/ama .amx-agent/ama.pid\nbash deploy/agent-run.sh up --insecure --ams 10.60.1.15:50051 \\\n  --pubkey "<AMX_AMS_PUBKEY>" \\\n  --config-dir ~/.claude-amx --tsamx-bin ~/.local/bin/tsamx',
      },
    ],
  },
  {
    title: '에이전트 실행과 설정',
    hint: '에이전트가 뜨긴 하는데 동작이 이상할 때',
    items: [
      {
        symptom: '로그에 tsamx not found / watch 디렉터리 없음이 반복된다',
        cause: '--config-dir·--tsamx-bin 없이 기동돼 계정 하달·감시 경로를 못 찾는 것이다.',
        fix: '두 플래그를 붙여 재기동한다. 등록·상태만 볼 때는 없어도 되지만, 계정 전달까지 하려면 필요하다.',
        code: 'bash deploy/agent-run.sh up ... \\\n  --config-dir ~/.claude-amx \\\n  --tsamx-bin ~/.local/bin/tsamx',
      },
      {
        symptom: '서버 카드에 계속 "메트릭 미보고"로 뜬다',
        cause: '그 에이전트가 CPU/MEM/DISK 수집 기능 이전의 구버전이다.',
        fix: '위 업데이트로 최신화하면 게이지가 뜬다. 최신 에이전트가 붙은 서버는 정상 표시된다.',
      },
      {
        symptom: '재설치했더니 서버 등록이 무한히 거부된다',
        cause: 'DB 초기화나 재등록 뒤에도 옛 서버 자격증명이 새 토큰보다 우선 적용되는 것이다.',
        fix: '상태 디렉터리를 지우고 새 토큰으로 다시 설치한다.',
        code: 'rm -rf ~/AMX-agent/.amx-agent/state\n# 이후 새 enroll 토큰으로 재설치',
      },
      {
        symptom: '에이전트를 어느 디렉터리에서 돌려야 하나',
        warn: true,
        cause:
          '원격 업데이트는 에이전트가 있는 폴더에서 git pull --ff-only를 실행한다. 개발 트리(/mnt/c/workspace/AMX)에는 커밋하지 않은 변경이 자주 있어 pull이 거부되고, 반대로 pull이 성공하면 개발 중이던 코드가 덮인다.',
        fix: '에이전트는 전용 클론 ~/AMX-agent에서만 돌린다. 개발 트리와 분리된 깨끗한 클론을 쓴다. WSL 홈은 리눅스 디스크라 /mnt/c보다 빌드도 빠르다.',
        code: 'git clone https://github.com/2kwanghee/AMX.git ~/AMX-agent\ncd ~/AMX-agent && bash deploy/agent-run.sh up ...',
      },
    ],
  },
  {
    title: 'PC 서버(fullstack) 운영',
    hint: 'PC 쪽 스택·네트워크·git',
    items: [
      {
        symptom: '노트북이 PC에 연결되지 않는다 (등록 실패·오프라인)',
        cause:
          'PC의 WSL 내부 IP는 재부팅 때 바뀐다. 인바운드를 넘기는 portproxy가 옛 IP를 가리키면 연결이 끊긴다.',
        fix: 'PC에서 현재 WSL IP를 확인하고, 관리자 PowerShell에서 portproxy를 그 IP로 갱신한다. 노트북이 안 붙을 때 가장 먼저 볼 곳이다. 50051(gRPC)만이 아니라 8080(REST)·3000(웹)도 같은 방식이라 함께 갱신해야 한다.',
        code: "hostname -I        # WSL에서 현재 IP 확인\n# 관리자 PowerShell에서 — 각 포트를 지우고 새 IP로 다시 추가\nnetsh interface portproxy show v4tov4   # 현재 매핑 확인\nnetsh interface portproxy delete v4tov4 listenport=50051 listenaddress=0.0.0.0\nnetsh interface portproxy add v4tov4 listenport=50051 listenaddress=0.0.0.0 connectport=50051 connectaddress=<새 WSL IP>\n# 8080·3000도 동일하게 반복",
      },
      {
        symptom: '설치 원라이너(curl … install.sh | bash)가 아무 출력 없이 끝난다',
        cause:
          '10.60.1.15:8080(REST)이 밖으로 안 나가 있는 것이다. WSL은 NAT 모드라 포트마다 portproxy를 손으로 넣어줘야 하는데, gRPC(50051)·웹(3000)만 넣고 8080을 빠뜨리면 설치 스크립트 다운로드부터 막힌다. curl -fsSL은 실패해도 아무 말 없이 죽기 때문에 "무응답"으로 보인다. (2026-08-14 실제 발생 — Windows 네이티브 명령도 같은 이유로 함께 막혔다.)',
        fix: '먼저 WSL 안에서 curl http://127.0.0.1:8080/install.sh 로 서버 자체가 사는지 확인한다(200이면 서버는 정상, 네트워크 문제). 그다음 관리자 PowerShell에서 8080 portproxy와 방화벽 인바운드 규칙을 추가한다. connectaddress는 hostname -I로 확인한 현재 WSL IP다.',
        code: "# WSL에서 — 서버 생존 확인\ncurl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/install.sh   # 200이어야 함\nhostname -I   # 첫 IP가 현재 WSL IP (예: 172.22.118.30)\n\n# 관리자 PowerShell에서 — 8080 노출 (두 listenaddress 모두)\nnetsh interface portproxy add v4tov4 listenport=8080 listenaddress=0.0.0.0 connectport=8080 connectaddress=<WSL IP>\nnetsh interface portproxy add v4tov4 listenport=8080 listenaddress=10.60.1.15 connectport=8080 connectaddress=<WSL IP>\nNew-NetFirewallRule -DisplayName 'AMX REST 8080' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080\n\n# 확인\nnetsh interface portproxy show v4tov4   # 8080 행이 보여야 함",
      },
      {
        symptom: '같은 포트가 Windows·노트북에서는 되는데 WSL 안에서만 timeout 난다',
        cause:
          'WSL에서 10.60.1.15로 나갔다 되돌아오는 트래픽은 일반 Windows 방화벽이 아니라 별도의 Hyper-V 방화벽을 거친다. 일반 방화벽에 허용 규칙이 있어도 Hyper-V 쪽에 같은 포트 규칙이 없으면 WSL발 접속만 조용히 막힌다. (2026-08-14 실제 발생 — 8080이 Windows에서는 200, WSL에서만 timeout이었다.)',
        fix: '관리자 PowerShell에서 Get-NetFirewallHyperVRule로 해당 포트 규칙이 있는지 보고, 없으면 추가한다. VMCreatorId는 WSL 고정값이다. 이 조치 스크립트는 .amx-dev/open-8080.ps1로 저장해 뒀다.',
        code: "# 관리자 PowerShell에서\nGet-NetFirewallHyperVRule | Where-Object { $_.LocalPorts -eq '8080' }   # 없으면 아래 실행\nNew-NetFirewallHyperVRule -DisplayName 'AMX REST 8080' -Direction Inbound -Action Allow -Protocol TCP -LocalPorts 8080 -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'\n\n# WSL에서 확인\ncurl -s -o /dev/null -w '%{http_code}' http://10.60.1.15:8080/install.sh   # 200이면 해결",
      },
      {
        symptom: '대시보드가 갑자기 죽거나 /mnt/c 접근이 I/O error로 막힌다',
        cause: 'WSL의 Windows 드라이브 마운트(9p)가 끊긴 것이다. 코드나 디스크 문제가 아니다.',
        fix: 'Windows PowerShell에서 WSL을 완전히 내렸다 다시 올린 뒤, 스택을 재기동한다.',
        code: 'wsl --shutdown          # PowerShell\n# 다시 WSL 진입 후\nbash deploy/fullstack-run.sh up all --insecure-grpc --lan',
      },
      {
        symptom: 'git 명령이 "index.lock: File exists"로 실패한다',
        cause: '이전 git 프로세스가 남긴 스테일 락 파일이다.',
        fix: '실행 중인 git이 없는지 확인하고 락을 지운다.',
        code: 'pgrep -x git    # 아무것도 안 나오면\nrm -f .git/index.lock',
      },
      {
        symptom: 'down all을 쓰기 전 알아둘 것',
        warn: true,
        cause:
          'down all은 DB 컨테이너까지 삭제한다. 볼륨을 안 붙여둬서 테넌트·계정·할당이 전부 사라진다.',
        fix: '정말 초기화할 때만 쓴다. 코드 반영으로 서버만 다시 띄우려면 restart를 쓴다. 웹은 실행 중 재빌드가 막히므로 내렸다 올린다.',
        code: 'bash deploy/fullstack-run.sh restart server --insecure-grpc --lan\nbash deploy/fullstack-run.sh down web\nbash deploy/fullstack-run.sh up web --lan',
      },
    ],
  },
];

export function SetupGuidePanel() {
  const [q, setQ] = useState('');
  const query = q.trim().toLowerCase();

  const sections = useMemo(() => {
    if (!query) return GUIDE;
    return GUIDE.map((s) => ({
      ...s,
      items: s.items.filter((it) =>
        `${it.symptom} ${it.cause} ${it.fix} ${it.code ?? ''}`.toLowerCase().includes(query),
      ),
    })).filter((s) => s.items.length > 0);
  }, [query]);

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>설치·운영 가이드</h2>
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        PC와 노트북 설치·연결에서 자주 나오는 문제를 증상으로 찾아 해결합니다. 각 항목을 눌러 펼치세요.
      </p>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="증상·명령으로 검색 (예: unknown_command, go, portproxy)"
        style={{ marginBottom: 8 }}
      />

      {sections.length === 0 && <p className="muted">검색 결과가 없습니다.</p>}

      {sections.map((s) => (
        <section key={s.title} className="guide-section">
          <h3 className="guide-h">
            {s.title}
            <span className="muted guide-hint">{s.hint}</span>
          </h3>
          {s.items.map((it) => (
            <details key={it.symptom} className="guide-item">
              <summary>
                <span className={`guide-mark ${it.warn ? 'warn' : ''}`}>
                  <Icon name={it.warn ? 'alert' : 'help'} size={15} />
                </span>
                <span className="guide-symptom">{it.symptom}</span>
              </summary>
              <div className="guide-body">
                <p>
                  <b>원인</b> — {it.cause}
                </p>
                <p>
                  <b>해결</b> — {it.fix}
                </p>
                {it.code && <pre className="guide-cmd">{it.code}</pre>}
              </div>
            </details>
          ))}
        </section>
      ))}
    </div>
  );
}
