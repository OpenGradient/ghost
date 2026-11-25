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

# Run as `irm | iex` there is no checkout: get one, and carry on from there. That is your own session, so nothing is
# set in it, and nothing exits.
if (-not $PSScriptRoot -or -not (Test-Path "$PSScriptRoot\profile\config.yaml")) {
  & {
    param($Options)
    $ErrorActionPreference = "Stop"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "ghost needs git: install Git for Windows (https://git-scm.com/download/win), then run this again." }
    $saved = @{}; foreach ($v in "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "GIT_DIR", "GIT_WORK_TREE") { $saved[$v] = [Environment]::GetEnvironmentVariable($v) }
    try {
      Remove-Item Env:GIT_DIR, Env:GIT_WORK_TREE -ErrorAction SilentlyContinue   # some other repository's
      # git and the engine's installer read only the environment; this also follows a company's PAC script.
      if (-not $env:HTTPS_PROXY) {
        try {
          $to = [Uri]"https://github.com/"; $by = [Net.WebRequest]::GetSystemWebProxy().GetProxy($to)
          if ($by -and $by.AbsoluteUri -ne $to.AbsoluteUri) { $env:HTTPS_PROXY = $env:HTTP_PROXY = $by.AbsoluteUri.TrimEnd("/"); $env:NO_PROXY = "127.0.0.1,localhost" }
        } catch {}
      }
      # A network that inspects TLS has a certificate only Windows knows, and git may be using a list of its own.
      function Git-Net { & git @args; if ($LASTEXITCODE) { & git -c http.sslBackend=schannel -c http.schannelCheckRevoke=false @args } }
      $src = "$env:USERPROFILE\.ghost-src"
      if (Test-Path "$src\.git") {   # always the released branch, whatever was checked out before
        Git-Net -C $src fetch --quiet origin main
        if ($LASTEXITCODE) { throw "couldn't get the latest ghost from github.com. Check your connection, then run this again." }
        git -C $src checkout --quiet main; if (-not $LASTEXITCODE) { git -C $src merge --quiet --ff-only origin/main }
        if ($LASTEXITCODE) { throw "Your copy of ghost in $src has changes of yours in it. Set them aside (git -C $src stash) and run this again." }
      } else {
        if (Test-Path $src) { Remove-Item -Recurse -Force $src }
        Git-Net clone --quiet https://github.com/OpenGradient/ghost.git $src
        if ($LASTEXITCODE) { throw "couldn't get ghost from github.com. Check your connection, then run this again." }
      }
      & powershell -ExecutionPolicy Bypass -NoProfile -File "$src\install.ps1" @Options
      if ($LASTEXITCODE) { throw "ghost was not installed; the reason is above." }
    } finally { foreach ($v in $saved.Keys) { [Environment]::SetEnvironmentVariable($v, $saved[$v]) } }
  } (@(if ($Local) { "-Local" }; if ($NoBrowser) { "-NoBrowser" }))
  return
}
$Repo = $PSScriptRoot
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
foreach ($v in "GIT_DIR", "GIT_WORK_TREE", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }

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

# (what the machine is, not this PowerShell)
$arch = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -ErrorAction SilentlyContinue).PROCESSOR_ARCHITECTURE
if ($arch -eq "ARM64") { throw "ghost doesn't run on Windows on ARM yet. It does in WSL2 there (https://learn.microsoft.com/windows/wsl/install): use the Linux command." }
if (-not (Have git)) { throw "ghost needs git: install Git for Windows (https://git-scm.com/download/win), then run this again." }
if (-not $env:CI -and ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "   This is an administrator window. ghost needs none: use it from an ordinary one, where its background process is yours." -ForegroundColor Yellow
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
New-Item -ItemType Directory -Force -Path $GhostHome | Out-Null
try { $lock = [IO.File]::Open("$GhostHome\.installing", "OpenOrCreate", "ReadWrite", "None") }
catch { throw "ghost is already being installed or updated. Wait for that to finish." }
trap { if ($lock) { $lock.Dispose() }; break }
& "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -Check   # nothing changes under an open ghost
# What ghost brings stays in its own folder: uv, the Pythons it fetches, and their cache. uv is ours, because one you
# have may be too old; it trusts the certificates the system trusts, as a company network may have one of its own.
$env:UV_CACHE_DIR = "$GhostHome\cache\uv"; $env:UV_PYTHON_INSTALL_DIR = "$GhostHome\python"
$env:UV_SYSTEM_CERTS = "1"; $env:UV_NATIVE_TLS = "1"
# git and the engine's installer read only the environment; this also follows a company's PAC script.
if (-not $env:HTTPS_PROXY) {
  try {
    $to = [Uri]"https://github.com/"; $by = [Net.WebRequest]::GetSystemWebProxy().GetProxy($to)
    if ($by -and $by.AbsoluteUri -ne $to.AbsoluteUri) { $env:HTTPS_PROXY = $env:HTTP_PROXY = $by.AbsoluteUri.TrimEnd("/"); $env:NO_PROXY = "127.0.0.1,localhost" }
  } catch {}
}
$UvVersion = "0.13.10"; $Uv = "$GhostHome\uv\uv.exe"
if (-not ((Test-Path $Uv) -and "$(& $Uv --version)".StartsWith("uv $UvVersion"))) {
  $env:UV_UNMANAGED_INSTALL = "$GhostHome\uv"
  try { Invoke-RestMethod "https://astral.sh/uv/$UvVersion/install.ps1" | Invoke-Expression } catch { Write-Host "   $_" }
  Remove-Item Env:UV_UNMANAGED_INSTALL
  if (-not (Test-Path $Uv)) {
    if (-not (Have uv)) { throw "couldn't download uv, which brings ghost's Python (from astral.sh and github.com). Check your connection, then run this again." }
    $Uv = "uv"
  }
}
if ($Local -and -not (Have ollama)) { throw "-Local needs Ollama: install it from https://ollama.com, then run this again." }
New-Item -ItemType Directory -Force -Path $Priv, $BinDir | Out-Null
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

$env:UV_PROJECT_ENVIRONMENT = $Venv
Push-Location $Repo
try {
  & $Uv sync --quiet --managed-python --python 3.11 --frozen; Check "uv sync"
} finally { Pop-Location }
Remove-Item Env:UV_PROJECT_ENVIRONMENT
if (Test-Path "$Priv\.scrub") {   # someone relied on it: say so rather than let them find out
  Write-Host "   Redaction was removed. What you send is sealed to a TEE enclave, but no longer redacted first."
}
Copy-Item "$Repo\privacy\*.py" $Priv -Force
Copy-Item "$Repo\profile\uncensored_prefill.json" $Priv -Force
function Install-Command($name) {
  # Your folder by its variable: these files are read in a code page your name may not be in.
  $you = if ($name.EndsWith(".cmd")) { "%USERPROFILE%" } else { '$env:USERPROFILE' }
  $text = [IO.File]::ReadAllText("$Repo\bin\windows\$name")
  foreach ($path in @(("__PYTHON__", $Py), ("__GHOST_HOME__", $GhostHome), ("__ENG__", $Eng))) {
    $text = $text.Replace($path[0], $path[1].Replace($env:USERPROFILE, $you))
  }
  Write-Text "$BinDir\$name" ($text -replace "`r?`n", "`r`n")   # cmd misreads labels in files with bare newlines
}
"ghost-uninstall.cmd", "ghost-uninstall.ps1" | ForEach-Object { Install-Command $_ }   # the way out, should a later step fail

# --- 3. the engine ---------------------------------------------------------------------------
Say "Setting up the engine (a few minutes the first time)"
& powershell -ExecutionPolicy Bypass -NoProfile -File "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -Python $Py
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
$why = & $Py -Es -c "import sys; sys.path.insert(0, sys.argv[1]); import launch; print('' if launch.ensure_bridge() else launch.bridge_problem())" $Priv
if ("$why".Trim() -or $LASTEXITCODE -ne 0) { throw "ghost's background service isn't running: $why" }
foreach ($task in "ghost-scrubber", "ghost-veil") { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue }   # older releases'
foreach ($old in "scrubbing_proxy.py", "presidio_scrub.py", "scrub_patterns.py", "ensure_scrubber_route.py", ".scrub", ".no_scrub",
                 ".full_redaction", ".presidio", ".presidio_failed", ".pass_paths", ".proxy") { Remove-Item "$Priv\$old" -ErrorAction SilentlyContinue }

# --- 7. the commands -------------------------------------------------------------------------
Say "Installing the ghost command"
"ghost.cmd", "ghost-login.cmd", "ghost-update.cmd", "ghost-update.ps1" | ForEach-Object { Install-Command $_ }
& "$Repo\scripts\fork-engine.ps1" -Eng $Eng -GhostHome $GhostHome -RefreshGateway
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)   # (with its variables left as they are)
$userPath = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
if (($userPath -split ";") -notcontains $BinDir -and ($userPath -split ";") -notcontains "%USERPROFILE%\.local\bin") {
  $key.SetValue("Path", "$BinDir;$userPath", "ExpandString")
  [Environment]::SetEnvironmentVariable("GHOST_PATH_SET", $null, "User")   # tells the open windows that something changed
}
$key.Close()
$other = Get-Command ghost -CommandType Application -ErrorAction SilentlyContinue | Where-Object { (Split-Path $_.Source) -ne $BinDir } | Select-Object -First 1
if ($other) { Write-Host "   Another program called ghost is on your PATH ($($other.Source)). This one is $BinDir\ghost.cmd." }

$who = & $Py -Es "$Priv\ghost_login.py" --status
if ($LASTEXITCODE -eq 0) { Say "ghost is ready. $who." } else { Say "ghost is ready. It asks you to log in the first time." }
Write-Host "   Open a new terminal and run:  ghost"
$lock.Dispose()
exit 0
