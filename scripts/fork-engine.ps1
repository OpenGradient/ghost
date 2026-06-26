# Fork the Hermes engine into a standalone, debranded ghost engine (Windows).
#
# The bash fork relocates the venv by rewriting paths with sed. That can't work on Windows:
# venvs use Scripts\*.exe launchers with the interpreter path baked into the binary. So here we
# RECREATE the venv with uv (the same way Hermes builds it on Windows: `uv venv` + `uv sync`),
# which produces a Scripts\hermes.exe pointing at the fork -- no relocation needed.
#
# EXPERIMENTAL: authored without a Windows test machine. Validate on Windows and report issues.
param(
  [string]$Src   = $(if ($env:HERMES_SRC)    { $env:HERMES_SRC }    else { "$env:LOCALAPPDATA\hermes\hermes-agent" }),
  [string]$Eng   = $(if ($env:GHOST_ENGINE)  { $env:GHOST_ENGINE }  else { "$env:USERPROFILE\.ghost-engine" }),
  [string]$PyVer = "3.11"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path $Src)) { throw "upstream Hermes engine not found at $Src" }

Write-Host "==> forking engine: $Src -> $Eng"
if (Test-Path $Eng) { Remove-Item -Recurse -Force $Eng }
New-Item -ItemType Directory -Force -Path $Eng | Out-Null
# Copy the source tree, excluding the venv (recreated below), git, caches.
robocopy $Src $Eng /E /XD venv .venv .git node_modules __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed copying the engine ($LASTEXITCODE)" }
$global:LASTEXITCODE = 0  # robocopy uses 0-7 for success

Write-Host "==> recreating the venv with uv (Windows can't relocate a copied venv)"
Push-Location $Eng
try {
  & uv venv venv --python $PyVer
  $env:UV_PROJECT_ENVIRONMENT = "$Eng\venv"
  # Mirror Hermes's own install: sync the project (+ all extras) into the fresh venv.
  & uv sync --extra all --locked 2>$null
  if (-not (Test-Path "$Eng\venv\Scripts\hermes.exe")) { & uv sync --extra all }
  if (-not (Test-Path "$Eng\venv\Scripts\hermes.exe")) { & uv pip install -e ".[all]" }
} finally { Pop-Location }
if (-not (Test-Path "$Eng\venv\Scripts\hermes.exe")) { throw "venv recreate failed: $Eng\venv\Scripts\hermes.exe missing" }

Write-Host "==> debranding the fork"
& "$Eng\venv\Scripts\python.exe" "$here\debrand.py" "$Eng"
if ($LASTEXITCODE -ne 0) { throw "debrand.py failed" }

Write-Host "==> isolating ghost skills -> skills-ghost (separate from a normal hermes)"
$skillFiles = @("tools\skills_hub.py","tools\skills_sync.py","tools\skills_tool.py","tools\skill_manager_tool.py","hermes_cli\skills_hub.py")
foreach ($rel in $skillFiles) {
  $p = Join-Path $Eng $rel
  if (Test-Path $p) {
    (Get-Content -Raw $p) `
      -replace 'SKILLS_DIR = HERMES_HOME / "skills"', 'SKILLS_DIR = HERMES_HOME / "skills-ghost"' `
      -replace '/skills/', '/skills-ghost/' | Set-Content -NoNewline -Encoding UTF8 $p
  }
}

Write-Host "==> verifying the fork launches (expect 'Ghost vX.Y')"
& "$Eng\venv\Scripts\hermes.exe" --version
if ($LASTEXITCODE -ne 0) { throw "fork failed to launch" }
Write-Host "==> fork ready: $Eng"
