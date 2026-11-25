# ghost-update -- get the latest ghost and install it, with the options you chose the first time (`ghost update`).
$ErrorActionPreference = "Stop"
$env:PSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Machine")   # PowerShell 7's, if that's where you are
Remove-Item Env:GIT_DIR, Env:GIT_WORK_TREE -ErrorAction SilentlyContinue               # some other repository's
# git and the engine's installer read only the environment; this also follows a company's PAC script.
if (-not $env:HTTPS_PROXY) {
  try {
    $to = [Uri]"https://github.com/"; $by = [Net.WebRequest]::GetSystemWebProxy().GetProxy($to)
    if ($by -and $by.AbsoluteUri -ne $to.AbsoluteUri) { $env:HTTPS_PROXY = $env:HTTP_PROXY = $by.AbsoluteUri.TrimEnd("/"); $env:NO_PROXY = "127.0.0.1,localhost" }
  } catch {}
}
# A network that inspects TLS has a certificate only Windows knows, and git may be using a list of its own.
function Git-Net { & git @args; if ($LASTEXITCODE) { & git -c http.sslBackend=schannel -c http.schannelCheckRevoke=false @args } }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

# The source you installed from, or ghost's own copy.
$managed = "$env:USERPROFILE\.ghost-src"
$src = if (Test-Path "__GHOST_HOME__\.src") { [IO.File]::ReadAllText("__GHOST_HOME__\.src").Trim() } else { $managed }
if (-not (Test-Path "$src\.git")) { $src = $managed }

if (-not (Test-Path "$src\.git")) {
  if (Test-Path $src) { Remove-Item -Recurse -Force $src }
  Git-Net clone --quiet https://github.com/OpenGradient/ghost.git $src; Check "git clone"
} elseif ($src -eq $managed) {   # always the released branch, whatever was checked out before
  Git-Net -C $src fetch --quiet origin main; Check "git fetch"
  git -C $src checkout --quiet main; Check "git checkout (does $src have changes of yours in it?)"
  git -C $src merge --quiet --ff-only origin/main; Check "git merge (does $src have changes of yours in it?)"
} else {
  Git-Net -C $src pull --quiet --ff-only; Check "git pull"
}
& powershell -ExecutionPolicy Bypass -NoProfile -File "$src\install.ps1"
exit $LASTEXITCODE
