<#
install.ps1 — 패키지형 설치 (Windows). install.sh 의 등가물.

  irm http://HOST:8080/install.ps1 | iex   형태로는 인자를 못 넘기므로, 실제
  설치 한 줄은 스크립트를 내려받아 파라미터와 함께 실행한다:
    $s = irm http://HOST:8080/install.ps1;
    & ([scriptblock]::Create($s)) -Ams HOST:50051 -Token <enroll> -Pubkey <B64> -Insecure

하는 일: 매니페스트(Ed25519 서명) 검증 → windows-amd64 바이너리·wheel 다운로드 →
         sha256 대조 → ama 배치 + uv 부트스트랩 + tsamx 설치 → enroll·기동.

v1 골격: 다운로드·sha256 대조·기동은 동작한다. Ed25519 서명 검증은 .NET 8+ 의
System.Security.Cryptography 를 쓰되, 구버전에서는 경고 후 진행 여부를 막는다
(sha256 대조는 항상 수행되므로 매니페스트 자체 위조가 없다는 전제에서만 완화).
신뢰 LAN 한정·평문 MITM 감수는 install.sh 와 동일하다.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Ams,
  [string]$Token = "",
  [Parameter(Mandatory=$true)][string]$Pubkey,
  [string]$AmsUrl = "",
  [switch]$Insecure,
  [switch]$DryRun,
  [string]$ConfigDir = "$HOME\.claude-amx",
  [string]$AgentId = "ama_dev",
  [string]$InstallRoot = "$HOME\.amx"
)
$ErrorActionPreference = "Stop"
function Die($m) { Write-Error $m; exit 1 }
function Step($m) { Write-Host "· $m" -ForegroundColor DarkGray }
function Ok($m)   { Write-Host "✔ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }

# ── 다운로드 베이스 ────────────────────────────────────────────────────────────
if (-not $AmsUrl) {
  $amsHost = ($Ams -split ":")[0]
  if (-not $amsHost) { Die "--Ams 에서 호스트를 추출할 수 없습니다: $Ams" }
  $scheme = if ($Insecure) { "http" } else { "https" }
  $AmsUrl = "${scheme}://${amsHost}:8080"
}
$AmsUrl = $AmsUrl.TrimEnd("/")
if (-not $Token) { Warn "-Token 이 없습니다 — 최초 enroll 이라면 기동이 실패합니다" }

# ── os/arch ────────────────────────────────────────────────────────────────────
$binName = "ama-windows-amd64.exe"
Ok "대상: windows-amd64 → 바이너리 $binName"

$work = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ("amx-install-" + [guid]::NewGuid())) -Force
try {
  # ── 1) 매니페스트 획득 + 서명 검증 ───────────────────────────────────────────
  Step "매니페스트 다운로드: $AmsUrl/download/manifest.json"
  $envelope = Invoke-RestMethod -Uri "$AmsUrl/download/manifest.json"
  if (-not ($envelope.manifest -and $envelope.signature -and $envelope.algorithm)) {
    Die "매니페스트 봉투 형식이 올바르지 않습니다"
  }
  if ($envelope.algorithm -ne "ed25519:amx-manifest-v1") { Die "알 수 없는 서명 알고리즘: $($envelope.algorithm)" }

  # 원문 바이트 보존: manifest 문자열을 UTF-8 로 그대로 인코딩(재직렬화 금지).
  $manifestBytes = [Text.Encoding]::UTF8.GetBytes($envelope.manifest)
  $domain = [Text.Encoding]::ASCII.GetBytes("amx-manifest-v1") + [byte]0
  $msg = $domain + $manifestBytes
  $sig = [Convert]::FromBase64String($envelope.signature)
  $pub = [Convert]::FromBase64String($Pubkey)
  if ($pub.Length -ne 32) { Die "-Pubkey 가 32바이트 raw Ed25519 키가 아닙니다 ($($pub.Length)B)" }

  Step "Ed25519 서명 검증"
  $verified = $false
  # .NET 8+ 는 System.Security.Cryptography 에 Ed25519 가 없다(현재). BouncyCastle
  # 없이 순수 .NET 으로는 어렵다 → v1 은 openssl 이 있으면 그걸 쓰고, 없으면 막는다.
  $openssl = Get-Command openssl -ErrorAction SilentlyContinue
  if ($openssl) {
    $hdr = [byte[]](0x30,0x2a,0x30,0x05,0x06,0x03,0x2b,0x65,0x70,0x03,0x21,0x00)
    [IO.File]::WriteAllBytes("$work\pub.der", [byte[]]($hdr + $pub))
    [IO.File]::WriteAllBytes("$work\msg.bin", [byte[]]$msg)
    [IO.File]::WriteAllBytes("$work\sig.bin", [byte[]]$sig)
    & openssl pkeyutl -verify -pubin -inkey "$work\pub.der" -keyform DER -rawin -in "$work\msg.bin" -sigfile "$work\sig.bin" 2>$null
    if ($LASTEXITCODE -eq 0) { $verified = $true }
  }
  if (-not $verified) {
    Die "매니페스트 Ed25519 서명 검증 실패 또는 검증 도구 없음. Windows 에서는 openssl(3.0+) 을 PATH 에 두세요 (v1 제약)."
  }
  Ok "매니페스트 서명 검증 통과"

  # ── 2) 매니페스트 파싱 ────────────────────────────────────────────────────────
  $manifest = $envelope.manifest | ConvertFrom-Json
  $wheelName = $manifest.version.wheel
  if (-not $wheelName) { Die "매니페스트에 version.wheel 이 없습니다" }
  $binSha   = $manifest.artifacts.$binName.sha256
  $wheelSha = $manifest.artifacts.$wheelName.sha256
  if (-not $binSha)   { Die "매니페스트에 $binName 항목이 없습니다" }
  if (-not $wheelSha) { Die "매니페스트에 wheel($wheelName) 항목이 없습니다" }
  Ok "매니페스트 커밋 $($manifest.version.commit) · wheel $wheelName"

  # ── 3) 다운로드 + sha256 대조 ─────────────────────────────────────────────────
  function Fetch-Verify($name, $want) {
    Step "다운로드: $name"
    Invoke-WebRequest -Uri "$AmsUrl/download/$name" -OutFile "$work\$name"
    $got = (Get-FileHash "$work\$name" -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $want.ToLower()) { Die "sha256 불일치: $name (manifest=$want actual=$got)" }
    Ok "$name  sha256 일치"
  }
  Fetch-Verify $binName $binSha
  Fetch-Verify $wheelName $wheelSha

  if ($DryRun) {
    Ok "-DryRun: 서명 검증·다운로드·sha256 대조까지 통과. 설치·enroll 은 생략합니다."
    return
  }

  # ── 4) 배치 ───────────────────────────────────────────────────────────────────
  $stateDir = "$InstallRoot\state"; $logDir = "$InstallRoot\logs"
  New-Item -ItemType Directory -Force -Path $InstallRoot,$stateDir,$logDir,$ConfigDir | Out-Null
  $bin = "$InstallRoot\ama.exe"
  Copy-Item "$work\$binName" $bin -Force
  Ok "ama 배치 → $bin"

  # ── 5) uv 부트스트랩 + tsamx ──────────────────────────────────────────────────
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "uv 부트스트랩"
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$HOME\.local\bin;$env:Path"
  }
  Step "tsamx 설치 (uv tool install $wheelName)"
  & uv tool install --python 3.12 "$work\$wheelName" | Out-Null
  if ($LASTEXITCODE -ne 0) { Die "tsamx 설치 실패" }
  $tsamx = (Get-Command tsamx -ErrorAction SilentlyContinue).Source
  if (-not $tsamx) { Die "tsamx 가 PATH 에 없습니다 (~\.local\bin 확인)" }
  Ok "tsamx 설치 완료 ($tsamx)"

  # ── 6) 설치 마커 ──────────────────────────────────────────────────────────────
  @(
    "# install.ps1(패키지 설치) 마커. 토큰 제외."
    "AMX_INSTALL_METHOD=package"
    "AMX_INSTALL_ROOT=$InstallRoot"
    "AMX_AMS_ADDR=$Ams"
    "AMX_AMS_URL=$AmsUrl"
    "AMX_AMS_PUBKEY=$Pubkey"
    "AMX_AGENT_ID=$AgentId"
    "AMX_CONFIG_DIR=$ConfigDir"
    "AMX_INSTALLED_COMMIT=$($manifest.version.commit)"
    "AMX_INSECURE=$([int][bool]$Insecure)"
  ) | Set-Content -Path "$InstallRoot\install.env" -Encoding UTF8
  Ok "설치 마커 기록 → $InstallRoot\install.env (install_method=package)"

  # ── 7) enroll·기동 ────────────────────────────────────────────────────────────
  # TODO(v1): 서비스 등록(nssm/Register-ScheduledTask)으로 부팅 지속성 부여.
  #           지금은 현재 세션 백그라운드 프로세스로만 기동한다.
  # 재설치 시 옛 인스턴스를 먼저 정리해 같은 state dir 에 두 데몬이 뜨지 않게 한다(H1).
  $pidFile = "$InstallRoot\ama.pid"
  if (Test-Path $pidFile) {
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid) { Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue; Warn "기존 ama 종료 (pid $oldPid)" }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
  }
  # PR4 self_update 분기용 키를 데몬 env 에도 싣는다(마커와 통일).
  $envVars = @{
    AMX_INSTALL_METHOD = "package"; AMX_INSTALL_ROOT = $InstallRoot; AMX_AMS_URL = $AmsUrl
    AMX_AMS_ADDR = $Ams; AMX_AGENT_ID = $AgentId; AMX_STATE_DIR = $stateDir
    AMX_AMS_PUBKEY = $Pubkey; CLAUDE_CONFIG_DIR = $ConfigDir; AMX_TSAMX_BIN = $tsamx
  }
  if ($Token) { $envVars["AMX_ENROLL_TOKEN"] = $Token }
  if ($Insecure) { $envVars["AMX_GRPC_ALLOW_INSECURE"] = "1" } else { Warn "Windows TLS(--ca) 경로는 v1 미구현" }
  foreach ($k in $envVars.Keys) { Set-Item -Path "Env:$k" -Value $envVars[$k] }
  Step "ama 기동"
  $p = Start-Process -FilePath $bin -RedirectStandardOutput "$logDir\ama.log" -RedirectStandardError "$logDir\ama.err.log" -PassThru -WindowStyle Hidden
  Set-Content -Path $pidFile -Value $p.Id
  Ok "ama 기동 (pid $($p.Id)) → AMS $Ams, 로그 $logDir\ama.log"
  Ok "설치 끝. 관리자 화면에서 이 서버가 '온라인'인지 확인하세요."
}
finally {
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
