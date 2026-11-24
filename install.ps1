# ============================================================================
# ghost -- native Windows installer (PowerShell). The macOS/Linux path is install.sh.
# ============================================================================
# One command, deterministic, no LLM:
#   irm https://raw.githubusercontent.com/OpenGradient/ghost/main/install.ps1 | iex
# From a clone:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# EXPERIMENTAL: CI builds the engine on Windows, but the privacy services below (Task Scheduler) have
# only been authored, not run on a real Windows box. Please report failures. The mac/linux install.sh
# is unaffected.
#
# Options (env vars or switches): -Local (offline model), -Scrub (outbound PII/secret redaction).
# ============================================================================
param([switch]$Local, [switch]$Scrub)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$GhostHome = "$env:USERPROFILE\.ghost"
$ProfileDir = "$GhostHome\profiles\uncensored"
$Priv      = "$GhostHome\privacy"
$Eng       = "$env:USERPROFILE\.ghost-engine"
$BinDir    = "$env:USERPROFILE\.local\bin"
$Venv      = "$GhostHome\venv"
function Say($m) { Write-Host "`n==> $m" -ForegroundColor Yellow }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

# --- self-bootstrap: run via `irm | iex` (no checkout) -> clone + re-exec ------------------
$RepoRoot = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if (-not $RepoRoot -or -not (Test-Path "$RepoRoot\profile\config.yaml")) {
  if (-not (Have git)) { throw "ghost needs git. Install Git for Windows, then re-run." }
  $Src = "$env:USERPROFILE\.ghost-src"
  if (Test-Path "$Src\.git") { Say "Updating ghost source ($Src)"; git -C $Src pull --ff-only }
  else { Say "Fetching ghost into $Src"; if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }; git clone https://github.com/OpenGradient/ghost.git $Src }
  & powershell -ExecutionPolicy Bypass -NoProfile -File "$Src\install.ps1" @PSBoundParameters
  exit $LASTEXITCODE
}

# Record source + options so `ghost update` can re-run the same way.
New-Item -ItemType Directory -Force -Path $GhostHome | Out-Null
Set-Content -Path "$GhostHome\.src" -Value $RepoRoot -Encoding UTF8
$envLines = @(); if ($Local) { $envLines += "GHOST_LOCAL=1" }; if ($Scrub) { $envLines += "GHOST_SCRUB=1" }
Set-Content -Path "$GhostHome\.install-env" -Value ($envLines -join "`n") -Encoding UTF8

# Nothing below runs while a ghost window is open: it would be left on a half-replaced engine.
& "$RepoRoot\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -Check

# --- 0. dependencies: uv (provisions Python 3.11 for the privacy stack) --------------------
Say "Dependencies"
if (-not (Have uv)) { Say "Installing uv (Astral's Python manager)"; irm https://astral.sh/uv/install.ps1 | iex; $env:Path = "$env:USERPROFILE\.local\bin;$env:Path" }
if (-not (Have uv)) { throw "uv install failed; install it from https://docs.astral.sh/uv/ and re-run." }

# --- 1. the engine (pinned Hermes release, debranded) --------------------------------------
Say "Building the engine -> $Eng"
& powershell -ExecutionPolicy Bypass -NoProfile -File "$RepoRoot\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome
if ($LASTEXITCODE -ne 0) { throw "fork-engine.ps1 failed" }

# --- 2. privacy stack: isolated uv venv (Python 3.11) --------------------------------------
Say "Privacy stack (isolated uv venv, Python 3.11)"
New-Item -ItemType Directory -Force -Path $Priv | Out-Null
$env:UV_PROJECT_ENVIRONMENT = $Venv
$extra = @(); if ($Scrub) { $extra = @("--extra","presidio") }
Push-Location $RepoRoot
try { & uv sync --python 3.11 --frozen @extra } catch { & uv sync --python 3.11 @extra }
finally { Pop-Location }
$Py  = "$Venv\Scripts\python.exe"
$Pyw = "$Venv\Scripts\pythonw.exe"; if (-not (Test-Path $Pyw)) { $Pyw = $Py }
if (-not (Test-Path $Py)) { throw "privacy venv not created at $Py" }
Copy-Item "$RepoRoot\privacy\*.py" $Priv -Force

# --- 3. uncensored profile -----------------------------------------------------------------
Say "Writing the uncensored profile"
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
$LocalModel = "ghost-tool:latest"
$homeFwd = ($env:USERPROFILE -replace '\\','/')
(Get-Content -Raw -Encoding UTF8 "$RepoRoot\profile\config.yaml") -replace '__HOME__',$homeFwd -replace '__LOCAL_MODEL__',$LocalModel |
  Set-Content -NoNewline -Encoding UTF8 "$ProfileDir\config.yaml"
Copy-Item "$RepoRoot\profile\SOUL.md" "$ProfileDir\SOUL.md" -Force
if (-not (Test-Path "$ProfileDir\.env")) { Copy-Item "$RepoRoot\profile\.env.example" "$ProfileDir\.env" -Force }
& $Py "$RepoRoot\scripts\migrate_skills.py" $GhostHome $ProfileDir
if (-not $Local) {
  # hosted-only: route auxiliary + fallback to hosted models (mirrors install.sh)
  @'
import sys, re
p = sys.argv[1]; s = open(p, encoding="utf-8-sig").read()
s = re.sub(r"provider: ollama-local\n(\s*)model: \S+", r"provider: opengradient\n\1model: nous/hermes-4-405b", s)
s = re.sub(r"model: ghost-tool:latest\n(\s*)provider: ollama-local", r"model: nous/hermes-4-405b\n\1provider: opengradient", s)
s = s.replace("provider: ollama-local", "provider: opengradient")
s = re.sub(r"(  compression:\n    provider: opengradient\n    model: )\S+", r"\g<1>deepseek/deepseek-v4-pro", s)
open(p, "w", encoding="utf-8").write(s); print("   hosted-only: fallback + auxiliary -> hermes-4-405b, compression -> deepseek-v4-pro (via og-veil)")
'@ | & $Py - "$ProfileDir\config.yaml"
}
# redaction markers (off by default)
if (-not (Test-Path "$Priv\pii_denylist.txt")) { Copy-Item "$RepoRoot\profile\pii_denylist.example.txt" "$Priv\pii_denylist.txt" -Force }
Copy-Item "$RepoRoot\profile\uncensored_prefill.json" "$Priv\uncensored_prefill.json" -Force
Remove-Item "$Priv\.proxy","$Priv\.no_scrub" -ErrorAction SilentlyContinue
if ($Scrub) {
  Set-Content -Path "$Priv\.scrub" -Value "" -Encoding UTF8
  @'
import sys, re
p = sys.argv[1]; s = open(p, encoding="utf-8-sig").read()
s = re.sub(r"(?m)^  redact_secrets: false$", "  redact_secrets: true", s)
s = re.sub(r"(?m)^  redact_pii: false$", "  redact_pii: true", s)
open(p, "w", encoding="utf-8").write(s)
'@ | & $Py - "$ProfileDir\config.yaml"
  Say "Outbound PII + secret redaction ON (-Scrub)"
} else {
  Remove-Item "$Priv\.scrub" -ErrorAction SilentlyContinue
  Say "Full-fidelity mode (default) -- no outbound redaction. Use -Scrub to enable."
}
if ($Scrub) {
  & $Py -m spacy download en_core_web_md 2>$null
  & $Py -c "import en_core_web_md" 2>$null
  if ($LASTEXITCODE -eq 0) { Set-Content "$Priv\.presidio" "" -Encoding ascii }
}

# --- 4. privacy services via Task Scheduler (scrubber :8788 + og-veil :11435) --------------
Say "Registering privacy services (Task Scheduler, at-logon + auto-restart)"
$svcSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$svcTrigger  = New-ScheduledTaskTrigger -AtLogOn
$svcs = @(
  @{ Name="ghost-scrubber"; Exec=$Pyw; Args="`"$Priv\scrubbing_proxy.py`"" },
  @{ Name="ghost-veil";     Exec=$Pyw; Args="-m veil serve --foreground --skip-setup --port 11435" }
)
foreach ($s in $svcs) {
  $a = New-ScheduledTaskAction -Execute $s.Exec -Argument $s.Args
  Register-ScheduledTask -TaskName $s.Name -Action $a -Trigger $svcTrigger -Settings $svcSettings -Force | Out-Null
  Start-ScheduledTask -TaskName $s.Name
}
Write-Host "   waiting for the scrubber + og-veil"
foreach ($probe in @("http://127.0.0.1:8788/healthz","http://127.0.0.1:11435/health")) {
  for ($i=0; $i -lt 20; $i++) { try { if ((Invoke-WebRequest $probe -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { break } } catch {}; Start-Sleep 1 }
}

# --- 5. the ghost / ghost-login / ghost-update commands ------------------------------------
Say "Installing the ghost commands"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$hermesExe = "$Eng\.hermes\bin\hermes.exe"
if (-not (Test-Path $hermesExe)) { $hermesExe = "$Eng\.hermes\bin\hermes.cmd" }
# ghost.cmd
@"
@echo off
set "HERMES_HOME=%USERPROFILE%\.ghost"
set "ANTHROPIC_API_KEY="
if /I "%~1"=="update" ( call "%~dp0ghost-update.cmd" & exit /b %errorlevel% )
if /I "%~1"=="--scrub" ( type nul > "%USERPROFILE%\.ghost\privacy\.scrub" & shift )
if /I "%~1"=="--no-scrub" ( del /q "%USERPROFILE%\.ghost\privacy\.scrub" 2>nul & shift )
call "$hermesExe" -p uncensored %*
"@ | Set-Content -Encoding ascii "$BinDir\ghost.cmd"
# ghost-login.cmd  (og-veil login through the engine venv)
@"
@echo off
"$Venv\Scripts\python.exe" -m veil %*
"@ | Set-Content -Encoding ascii "$BinDir\ghost-login.cmd"
# ghost-update.cmd  (pull + re-run installer)
@"
@echo off
set "SRC=%USERPROFILE%\.ghost-src"
if exist "%SRC%\.git" ( git -C "%SRC%" pull --ff-only ) else ( git clone https://github.com/OpenGradient/ghost.git "%SRC%" )
powershell -ExecutionPolicy Bypass -NoProfile -File "%SRC%\install.ps1" %*
"@ | Set-Content -Encoding ascii "$BinDir\ghost-update.cmd"

& "$RepoRoot\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -RefreshGateway

# add $BinDir to user PATH
$userPath = [Environment]::GetEnvironmentVariable("Path","User")
if ($userPath -notlike "*$BinDir*") { [Environment]::SetEnvironmentVariable("Path","$BinDir;$userPath","User"); $env:Path = "$BinDir;$env:Path" }

# --- 6. connect + smoke test ---------------------------------------------------------------
Say "Connect your OpenGradient Chat account: run  ghost-login  (browser login)"
Say "Smoke test"
& cmd /c "`"$BinDir\ghost-login.cmd`" status >nul 2>&1"
if ($LASTEXITCODE -eq 0) { & "$BinDir\ghost.cmd" --yolo -z "Reply with one word: hi" }
else { Write-Host "   skipped -- it needs a connected account (run 'ghost-login', then 'ghost')" }

Say "ghost installed -- open a new terminal and run:  ghost"
Write-Host "   Hosted default = abliteration/abliterated-model-large-v2 via the OpenGradient TEE gateway (OHTTP-private)."
Write-Host "   Not connected yet? Run:  ghost-login"
