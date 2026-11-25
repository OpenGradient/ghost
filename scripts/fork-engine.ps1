# Build ghost's engine from the upstream Hermes release pinned in ..\engine.pin (Windows).
#
# The Windows twin of fork-engine.sh: fetch that exact commit, debrand it, seal it, and install its
# runtime under ~\.ghost with the engine's own package manager. A normie `hermes` install is neither
# needed nor touched.
#
#   fork-engine.ps1                   build the engine, unless the one there is already this build
#                                     (the previous one is restored if it fails)
#   fork-engine.ps1 -Check            fail if ghost is open, so nothing changes under a live session
#   fork-engine.ps1 -RefreshGateway   point an installed background gateway at this engine
param(
  [string]$Eng       = $(if ($env:GHOST_ENGINE) { $env:GHOST_ENGINE } else { "$env:USERPROFILE\.ghost-engine" }),
  [string]$GhostHome = $(if ($env:GHOST_HOME)   { $env:GHOST_HOME }   else { "$env:USERPROFILE\.ghost" }),
  [string]$Python    = "python",   # a Python 3.9+ to run debrand.py
  [switch]$Check,
  [switch]$RefreshGateway
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoUrl = "https://github.com/NousResearch/hermes-agent.git"
$New = "$Eng.new"; $Old = "$Eng.old"
$BinDir = Join-Path $Eng ".hermes\bin"
$Restart = "$GhostHome\.restart-gateway"   # present while a gateway this script stopped is owed a start
$Refresh = "$GhostHome\.refresh-gateway"   # present while an installed gateway still points at the previous engine

# The engine's package manager must see only its own toolchain, not ghost's privacy venv.
foreach ($v in "UV_PROJECT_ENVIRONMENT","VIRTUAL_ENV","PYTHONHOME","PYTHONPATH","HERMES_RUNTIME_DIR",
               "HERMES_INSTALL_DIR","HERMES_INSTALL_ROOT","HERMES_REVISION","HERMES_PROFILE") {
  Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}

$pin = @{}
Get-Content (Join-Path $here "..\engine.pin") | ForEach-Object {
  if ($_ -match '^\s*(HERMES_\w+)=(\S+)\s*$') { $pin[$Matches[1]] = $Matches[2] }
}
$Version = $pin["HERMES_VERSION"]; $Commit = $pin["HERMES_COMMIT"]
if (-not $Version -or -not $Commit) { throw "engine.pin is missing HERMES_VERSION / HERMES_COMMIT" }

function Launcher {
  foreach ($name in "hermes.exe", "hermes.cmd") {
    $p = Join-Path $BinDir $name
    if (Test-Path $p) { return $p }
  }
  return $null
}
function Invoke-Engine([string[]]$EngineArgs) {
  $env:HERMES_HOME = "$GhostHome\profiles\uncensored"
  & (Launcher) @EngineArgs
}
# Windows PowerShell turns a native command's redirected stderr into an error; not while one of these runs.
function Invoke-Quietly([scriptblock]$Block) {
  $ErrorActionPreference = "Continue"
  try { & $Block 2>$null } catch {}
}
function Test-Launches {
  if (-not (Launcher)) { return $false }
  return "$(@(Invoke-Quietly { Invoke-Engine @('--version') })[0])".StartsWith("Ghost v")
}
function Get-EngineProcess {
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.ProcessId -ne $PID -and $_.Name -match '^(hermes|python)' -and
    # launched by path, or as `python -c "... '<engine>' ..."`, where Python escapes the backslashes
    ($_.CommandLine.Contains("$Eng\") -or $_.CommandLine.Contains("$Eng'") -or $_.CommandLine.Contains($Eng.Replace('\', '\\'))) })
}
# A process keeps importing from the engine's path for as long as it runs, so swapping the tree
# underneath one would mix two releases in it. The background gateway is ours to stop and restart.
function Assert-Closed {
  if (-not (Test-Path $Eng) -or $env:GHOST_FORCE) { return }
  $open = @(Get-EngineProcess | Where-Object { $_.CommandLine -notmatch 'gateway run' })
  if ($open.Count) { throw "ghost is open (pid $($open.ProcessId -join ', ')). Close those windows and re-run, or set GHOST_FORCE=1." }
}
$Startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway_uncensored.vbs"
function Test-Gateway {   # installed, as a task, or where Task Scheduler wouldn't have it, in the Startup folder
  [bool](Get-ScheduledTask -TaskName "Hermes_Gateway_uncensored" -ErrorAction SilentlyContinue) -or
    ((Test-Path $Startup) -and [IO.File]::ReadAllText($Startup).Contains("\.ghost\"))
}
$env:HERMES_NONINTERACTIVE = "1"
# Antivirus, or a program that inherited the folder, may hold it for a moment.
function Move-Tree($from, $to) {
  for ($i = 1; ; $i++) { try { [IO.Directory]::Move($from, $to); return } catch { if ($i -eq 10) { throw }; Start-Sleep -Milliseconds 500 } }
}
function Restore-Engine {
  if (Test-Path $Eng) { Remove-Item -Recurse -Force $Eng -ErrorAction SilentlyContinue }
  if (Test-Path $Eng) { Move-Tree $Eng "$Eng.broken-$PID" }
  Move-Tree $Old $Eng
}

# What this build is made from. The engine there is current if it was made from the same, and still launches.
$made = (@("..\engine.pin", "debrand.py", "fork-engine.ps1") | ForEach-Object { [IO.File]::ReadAllText((Join-Path $here $_)) }) -join ""
$Recipe = (Get-FileHash -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($made + $env:GHOST_NO_BROWSER)))).Hash
function Test-Current {
  if ($env:GHOST_FORCE -or -not (Test-Path "$Eng\.ghost-recipe")) { return $false }
  return ([IO.File]::ReadAllText("$Eng\.ghost-recipe").Trim() -eq $Recipe) -and (Test-Launches)
}

if ($Check) { Assert-Closed; return }
if ($RefreshGateway) {
  if (-not (Test-Path $Refresh)) { return }
  if (-not (Test-Gateway)) { Remove-Item $Refresh; return }
  if (Launcher) {
    Write-Host "==> moving the background gateway onto the new engine"
    $start = if (Test-Path $Restart) { "--start-now" } else { "--no-start-now" }   # one you had stopped stays stopped
    Invoke-Engine @("gateway","install","--force",$start,"--start-on-login")
    if ($LASTEXITCODE -eq 0) { Remove-Item $Restart, $Refresh -ErrorAction SilentlyContinue }
    else { Write-Host "   (could not refresh it -- run: ghost gateway install --force)" }
  }
  return
}
# A build that was killed part-way left the engine it was replacing set aside: put that one back first.
if (Test-Path $Old) { if (Test-Launches) { Remove-Item -Recurse -Force $Old } else { Restore-Engine } }
if (Test-Current) { Write-Host "==> the engine is already this build: $Eng"; exit 0 }
Assert-Closed

# ---------- 1. fetch ----------
Write-Host "==> fetching Hermes v$Version"
if (Test-Path $New) { Remove-Item -Recurse -Force $New }
New-Item -ItemType Directory -Force -Path $GhostHome | Out-Null
for ($i = 1; $i -le 3; $i++) {
  # core.longpaths: the tree has paths past MAX_PATH; autocrlf would change every byte the engine's lock hashes.
  # (A network that inspects TLS has a certificate only Windows knows, and git may be using a list of its own.)
  $tls = if ($i -gt 1) { @("-c", "http.sslBackend=schannel", "-c", "http.schannelCheckRevoke=false") } else { @() }
  & git -c advice.detachedHead=false -c core.longpaths=true -c core.autocrlf=false @tls clone --quiet --depth 1 --branch "v$Version" $RepoUrl $New
  if ($LASTEXITCODE -eq 0) { break }
  if (Test-Path $New) { Remove-Item -Recurse -Force $New }
  if ($i -eq 3) { throw "could not fetch $RepoUrl" }
  Start-Sleep -Seconds ($i * 3)
}
$got = (& git -C $New rev-parse HEAD).Trim()
if ($got -ne $Commit) {
  Remove-Item -Recurse -Force $New
  throw "tag v$Version is commit $got, but engine.pin expects $Commit -- refusing it"
}
foreach ($d in ".git",".github","tests","tests-js","evals","website","contributors") {
  if (Test-Path "$New\$d") { Remove-Item -Recurse -Force "$New\$d" }
}

# ---------- 2. debrand + seal ----------
Write-Host "==> debranding"
& $Python -Es "$here\debrand.py" $New
if ($LASTEXITCODE -ne 0) { throw "debrand.py failed" }
$stamp = [ordered]@{
  schemaVersion = 2; commit = $Commit; commitDate = $null; branch = $null
  builtAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss+00:00")
  dirty = $false; source = "local"; distribution = "ghost"; updateMechanism = "external"
  baseVersion = $Version; displayVersion = $Version; distance = 0; payload = "bootstrap"; tag = $null
}
[IO.File]::WriteAllText("$New\install-stamp.json", ($stamp | ConvertTo-Json) + "`n")

# ---------- 3. stop the background gateway, then swap the trees ----------
Assert-Closed   # (that took a while)
$oldBin = Launcher
if (-not $oldBin -and (Test-Path "$Eng\venv\Scripts\hermes.exe")) { $oldBin = "$Eng\venv\Scripts\hermes.exe" }
if ($oldBin) {
  $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'gateway run' -and $_.CommandLine -notmatch 'idle-exit' -and
    ($_.CommandLine.Contains("$Eng\") -or $_.CommandLine.Contains("$GhostHome\") -or $_.CommandLine.Contains($Eng.Replace('\', '\\'))) }
  if ((Test-Gateway) -and $running) { New-Item -ItemType File -Force -Path $Restart | Out-Null }
  $env:HERMES_HOME = $GhostHome
  # (--yes: it won't stop in the middle of something, a cron job say, without being told. Old engines don't know the word.)
  Invoke-Quietly { & $oldBin -p uncensored gateway stop --yes; if ($LASTEXITCODE) { & $oldBin -p uncensored gateway stop } } | Out-Null
}

$built = $false
try {
  if (Test-Path $Eng) { Move-Tree $Eng $Old }
  Move-Tree $New $Eng

  # ---------- 4. runtime + launcher ----------
  Write-Host "==> installing the engine runtime (Python, Node, ripgrep, locked dependencies)"
  $env:HERMES_HOME = $GhostHome
  $flags = @("-Stage","python-deps","-InstallDir",$Eng,"-HermesHome",$GhostHome,"-NonInteractive")
  if ($env:GHOST_NO_BROWSER) { $flags += @("-SkipBrowser","-SkipComputerUse") }
  & powershell -ExecutionPolicy Bypass -NoProfile -File "$Eng\scripts\install.ps1" @flags
  if ($LASTEXITCODE -ne 0) { throw "engine runtime install failed" }

  $storePy = Get-ChildItem "$GhostHome\tools" -Directory -Filter "python-*" -ErrorAction SilentlyContinue |
    Sort-Object Name | Select-Object -Last 1 | ForEach-Object { Join-Path $_.FullName "python.exe" }
  if (-not $storePy -or -not (Test-Path $storePy)) { throw "the engine's Python was not installed under $GhostHome\tools" }

  Push-Location $Eng
  try {
    # Web search is on by default in ghost, so its backend ships rather than installing mid-conversation.
    & $storePy -m pm.cli install --extra ddgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not install the web-search backend" }
    # It remembers that you did without these, until they're asked for by name.
    $declined = Get-ChildItem "$GhostHome\installs\*\declined-packages.json" -ErrorAction SilentlyContinue | Where-Object { [IO.File]::ReadAllText($_.FullName).Contains("chromium") }
    if (-not $env:GHOST_NO_BROWSER -and $declined) { & $storePy -m pm.cli install chromium cua-driver | Out-Null; if ($LASTEXITCODE -ne 0) { throw "could not install the browser" } }
  } finally { Pop-Location }

  $mint = Join-Path ([IO.Path]::GetTempPath()) "ghost-mint-$PID.py"
  [IO.File]::WriteAllText($mint, @'
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from hermes_cli._launchers import ensure_install_launchers
sys.exit(0 if ensure_install_launchers(root, root / ".hermes" / "bin") else "could not write the engine launcher")
'@)
  & $storePy -I $mint $Eng
  $code = $LASTEXITCODE
  Remove-Item $mint -ErrorAction SilentlyContinue
  if ($code -ne 0 -or -not (Launcher)) { throw "could not write the engine launcher" }

  Write-Host "==> verifying the engine launches"
  $first = @(Invoke-Engine @("--version"))[0]   # collected whole: Select-Object -First would cut the process off
  Write-Host "    $first"
  if (-not "$first".StartsWith("Ghost v$Version")) { throw "expected 'Ghost v$Version'" }
  $built = $true
} finally {   # (which Ctrl-C reaches too)
  if (-not $built) {
    if (Test-Path $New) { Remove-Item -Recurse -Force $New -ErrorAction SilentlyContinue }
    if (Test-Path $Old) {
      Restore-Engine
      Write-Host "!! engine build failed -- restored the previous engine"
      if (Test-Path $Restart) {
        $env:HERMES_HOME = $GhostHome
        Invoke-Quietly { & $oldBin -p uncensored gateway start } | Out-Null
        Remove-Item $Restart -ErrorAction SilentlyContinue
      }
    }
  }
}

New-Item -ItemType File -Force -Path $Refresh | Out-Null
[IO.File]::WriteAllText("$Eng\.ghost-recipe", $Recipe)
if (Test-Path $Old) { Remove-Item -Recurse -Force $Old -ErrorAction SilentlyContinue }
Push-Location $Eng; $env:HERMES_HOME = $GhostHome
Invoke-Quietly { & $storePy -m pm.cli gc } | Out-Null   # drop dependency generations nothing uses any more
Pop-Location
Write-Host "==> engine ready: $Eng"
exit 0
