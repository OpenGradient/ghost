# ghost-update -- get the latest ghost and install it, with the options you chose the first time (`ghost update`).
$ErrorActionPreference = "Stop"
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

# The source you installed from, or ghost's own copy.
$managed = "$env:USERPROFILE\.ghost-src"
$src = if (Test-Path "__GHOST_HOME__\.src") { [IO.File]::ReadAllText("__GHOST_HOME__\.src").Trim() } else { $managed }
if (-not (Test-Path "$src\.git")) { $src = $managed }

if (-not (Test-Path "$src\.git")) {
  if (Test-Path $src) { Remove-Item -Recurse -Force $src }
  git clone --quiet https://github.com/OpenGradient/ghost.git $src; Check "git clone"
} elseif ($src -eq $managed) {   # always the released branch, whatever was checked out before
  git -C $src fetch --quiet origin main; Check "git fetch"
  git -C $src checkout --quiet main; Check "git checkout (does $src have changes of yours in it?)"
  git -C $src merge --quiet --ff-only origin/main; Check "git merge (does $src have changes of yours in it?)"
} else {
  git -C $src pull --quiet --ff-only; Check "git pull"
}
& powershell -ExecutionPolicy Bypass -NoProfile -File "$src\install.ps1"
exit $LASTEXITCODE
