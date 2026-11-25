# ghost -- the installer and updater for Windows (PowerShell). On macOS, Linux and WSL2: install.sh. Safe to re-run.
#
#   irm https://raw.githubusercontent.com/OpenGradient/ghost/main/install.ps1 | iex
#
# From a clone:  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Local] [-NoBrowser]
#
# Sets up the engine (the Hermes release pinned in engine.pin, debranded, with its own runtime), the local
# bridge that seals requests to the TEE gateway, and the `ghost` commands. It contacts no model and needs no
# login: ghost asks you to log in the first time you run it. No administrator rights needed.
#
# Options: -Local (an offline model too; needs Ollama), -NoBrowser (skip Chromium and the computer-use driver).
# `ghost update` remembers them.
param([switch]$Local, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$GhostHome  = "$env:USERPROFILE\.ghost"
$Eng        = "$env:USERPROFILE\.ghost-engine"
$ProfileDir = "$GhostHome\profiles\uncensored"
$Priv       = "$GhostHome\privacy"
$Venv       = "$GhostHome\venv"
$Py         = "$Venv\Scripts\python.exe"
$BinDir     = "$env:USERPROFILE\.local\bin"
function Say($m) { Write-Host "`n==> $m" -ForegroundColor Yellow }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Write-Text($path, $text) { [IO.File]::WriteAllText($path, $text) }   # no byte-order mark, unlike Set-Content
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

# Run as `irm | iex` there is no checkout: get one, and carry on from there.
$Repo = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if (-not $Repo -or -not (Test-Path "$Repo\profile\config.yaml")) {
  if (-not (Have git)) { throw "ghost needs git: install Git for Windows (https://git-scm.com/download/win), then run this again." }
  $Src = "$env:USERPROFILE\.ghost-src"
  if (Test-Path "$Src\.git") {   # always the released branch, whatever was checked out before
    git -C $Src fetch --quiet origin main; Check "git fetch"
    git -C $Src checkout --quiet main; Check "git checkout (does $Src have changes of yours in it?)"
    git -C $Src merge --quiet --ff-only origin/main; Check "git merge (does $Src have changes of yours in it?)"
  } else {
    if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
    git clone --quiet https://github.com/OpenGradient/ghost.git $Src; Check "git clone"
  }
  & powershell -ExecutionPolicy Bypass -NoProfile -File "$Src\install.ps1" @PSBoundParameters
  exit $LASTEXITCODE
}

# --- options: what was asked for now, or last time -------------------------------------------
$saved = if (Test-Path "$GhostHome\.install-env") { [IO.File]::ReadAllText("$GhostHome\.install-env") } else { "" }
function Option($switch, $name) {
  if ($switch) { return $true }
  $value = [Environment]::GetEnvironmentVariable($name)
  if ($null -ne $value) { return $value -notin "", "0" }
  return $saved -match "(?m)^$name=1"
}
$Local = Option $Local "GHOST_LOCAL"; $NoBrowser = Option $NoBrowser "GHOST_NO_BROWSER"
if ($NoBrowser) { $env:GHOST_NO_BROWSER = "1" } else { Remove-Item Env:GHOST_NO_BROWSER -ErrorAction SilentlyContinue }

# --- 1. this machine -------------------------------------------------------------------------
Say "Checking this machine"
& "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -Check   # nothing changes under an open ghost
if (-not (Have uv)) { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression; $env:Path = "$BinDir;$env:Path" }
if (-not (Have uv)) { throw "couldn't install uv. Install it from https://docs.astral.sh/uv/, then run this again." }
if ($Local -and -not (Have ollama)) { throw "-Local needs Ollama: install it from https://ollama.com, then run this again." }
New-Item -ItemType Directory -Force -Path $GhostHome, $Priv, $BinDir | Out-Null
Write-Text "$GhostHome\.src" $Repo
$chosen = @(); if ($Local) { $chosen += "GHOST_LOCAL=1" }; if ($NoBrowser) { $chosen += "GHOST_NO_BROWSER=1" }
Write-Text "$GhostHome\.install-env" (($chosen | ForEach-Object { "$_`n" }) -join "")

# --- 2. the bridge ---------------------------------------------------------------------------
Say "Installing the privacy bridge"
# Windows won't replace the files of a program that is running.
foreach ($task in "ghost-bridge", "ghost-scrubber", "ghost-veil") { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue | Where-Object {
  $_.CommandLine -and ($_.CommandLine.Contains("$Priv\bridge.py") -or $_.CommandLine.Contains("$Priv\scrubbing_proxy.py") -or $_.CommandLine.Contains("-m veil serve --foreground --skip-setup --port 11435"))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
foreach ($task in "ghost-scrubber", "ghost-veil") { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue }   # older releases'

$env:UV_PROJECT_ENVIRONMENT = $Venv
Push-Location $Repo
try {
  & uv sync --quiet --python 3.11 --frozen
  if ($LASTEXITCODE -ne 0) {   # again with the system's certificates, for a network that has one of its own
    $env:UV_SYSTEM_CERTS = "1"; $env:UV_NATIVE_TLS = "1"
    & uv sync --quiet --python 3.11 --frozen; Check "uv sync"
  }
} finally { Pop-Location }
Remove-Item Env:UV_PROJECT_ENVIRONMENT
if (Test-Path "$Priv\.scrub") {   # someone relied on it: say so rather than let them find out
  Write-Host "   Redaction was removed. What you send is sealed to a TEE enclave, but no longer redacted first."
}
foreach ($old in "scrubbing_proxy.py", "presidio_scrub.py", "scrub_patterns.py", "ensure_scrubber_route.py", ".scrub", ".no_scrub",
                 ".full_redaction", ".presidio", ".presidio_failed", ".pass_paths", ".proxy") { Remove-Item "$Priv\$old" -ErrorAction SilentlyContinue }
Copy-Item "$Repo\privacy\*.py" $Priv -Force
Copy-Item "$Repo\profile\uncensored_prefill.json" $Priv -Force

# --- 3. the engine ---------------------------------------------------------------------------
Say "Setting up the engine (a few minutes the first time)"
& powershell -ExecutionPolicy Bypass -NoProfile -File "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome
Check "building the engine"

# --- 4. the local model (-Local) -------------------------------------------------------------
$LocalModel = ""
if ($Local) {
  Say "Getting the local model"
  foreach ($line in [IO.File]::ReadAllLines("$Repo\models.txt")) {
    if ($line -match '^\s*(#|$)') { continue }
    $source, $alias, $need = $line -split "`t"
    if ($need -eq "optional") { continue }
    & ollama pull $source; Check "ollama pull"
    # The engine refuses a window under 64K for tool use, and Ollama's own default is far smaller.
    $modelfile = Join-Path ([IO.Path]::GetTempPath()) "ghost-modelfile-$PID"
    Write-Text $modelfile "FROM $source`nPARAMETER num_ctx 65536`n"
    & ollama create $alias -f $modelfile; Check "ollama create"
    Remove-Item $modelfile
    $LocalModel = "${alias}:latest"
  }
}

# --- 5. the profile --------------------------------------------------------------------------
Say "Writing your profile"
$flags = @(); if ($LocalModel) { $flags += @("--local-model", $LocalModel) }
& $Py "$Repo\scripts\write_profile.py" $ProfileDir --ghost-home $GhostHome @flags; Check "writing the profile"
& $Py "$Repo\scripts\migrate_skills.py" $GhostHome $ProfileDir; Check "moving your skills"

# --- 6. the background service ---------------------------------------------------------------
Say "Starting the background service"
$Pyw = "$Venv\Scripts\pythonw.exe"; if (-not (Test-Path $Pyw)) { $Pyw = $Py }
try {
  $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  $task = @{
    TaskName  = "ghost-bridge"
    Action    = New-ScheduledTaskAction -Execute $Pyw -Argument "-Es `"$Priv\bridge.py`""
    Trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me   # for anyone's logon it would need an administrator
    Principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
    Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 `
      -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  }
  Register-ScheduledTask @task -Force | Out-Null
} catch { Write-Host "   Task Scheduler refused it ($($_.Exception.Message.Trim())), so ghost starts it when needed." }
& $Py -Es -c "import sys; sys.path.insert(0, sys.argv[1]); import launch; sys.exit(not launch.ensure_bridge())" $Priv
if ($LASTEXITCODE -ne 0) { throw "the background service didn't start. Its log is $Priv\bridge.log" }

# --- 7. the commands -------------------------------------------------------------------------
Say "Installing the ghost command"
foreach ($name in "ghost.cmd", "ghost-login.cmd", "ghost-update.cmd", "ghost-update.ps1", "ghost-uninstall.cmd", "ghost-uninstall.ps1") {
  $text = [IO.File]::ReadAllText("$Repo\bin\windows\$name").Replace("__PYTHON__", $Py).Replace("__GHOST_HOME__", $GhostHome).Replace("__ENG__", $Eng)
  Write-Text "$BinDir\$name" ($text -replace "`r?`n", "`r`n")   # cmd misreads labels in files with bare newlines
}
& "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -RefreshGateway
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ("$userPath" -notlike "*$BinDir*") { [Environment]::SetEnvironmentVariable("Path", "$BinDir;$userPath", "User") }

$who = & $Py -Es "$Priv\ghost_login.py" --status
if ($LASTEXITCODE -eq 0) { Say "ghost is ready. $who." } else { Say "ghost is ready. It asks you to log in the first time." }
Write-Host "   Open a new terminal and run:  ghost"
exit 0
