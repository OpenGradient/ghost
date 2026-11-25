# ghost-uninstall -- remove ghost from this machine (`ghost uninstall`).
#
#   ghost uninstall             ask first, and keep your chats, skills and settings unless you say otherwise
#   ghost uninstall --yes       don't ask (your data is kept)
#   ghost uninstall --purge     delete your data too
param([switch]$y, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Flags)
$ErrorActionPreference = "SilentlyContinue"
$GhostHome = "__GHOST_HOME__"
$Eng = "__ENG__"
$BinDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Flags = @($Flags | Where-Object { $_ -and $_ -ne "uninstall" })
foreach ($flag in $Flags) {
  if ($flag -notin "--yes", "--purge") { Write-Host "usage: ghost uninstall [--yes] [--purge]"; exit 2 }
}
$purge = $Flags -contains "--purge"

# Only ever delete directories that are unmistakably ghost's.
function Test-OurEngine($dir) {
  $stamp = "$dir\install-stamp.json"
  return (Test-Path $stamp) -and [IO.File]::ReadAllText($stamp) -match '"distribution":\s*"ghost"'
}
if (-not ((Test-Path "$GhostHome\.src") -and (Test-Path "$GhostHome\privacy"))) { Write-Host "ghost isn't installed here ($GhostHome)."; exit 0 }

$open = @(Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and $_.CommandLine -and $_.Name -match '^(python|hermes)' -and $_.CommandLine -notmatch 'gateway run' -and
  ($_.CommandLine.Contains("$Eng\") -or $_.CommandLine.Contains($Eng.Replace('\', '\\'))) })
if ($open.Count -and -not $env:GHOST_FORCE) {
  Write-Host "ghost is open (pid $($open.ProcessId -join ', ')). Close those windows and run this again."; exit 1
}

Write-Host "This removes ghost: its engine ($Eng), background service and commands."
if (-not ($y -or $Flags -contains "--yes")) {
  if ((Read-Host "Remove ghost? [y/N]") -notmatch '^[Yy]') { Write-Host "Nothing was changed."; exit 0 }
  if (-not $purge) { $purge = (Read-Host "Also delete your chats, skills, settings and login? [y/N]") -match '^[Yy]' }
}

Write-Host "Stopping ghost"
$gateway = Get-ScheduledTask -TaskName "Hermes_Gateway_uncensored"
$engine = @("$Eng\.hermes\bin\hermes.exe", "$Eng\.hermes\bin\hermes.cmd") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($engine) {
  $env:HERMES_HOME = $GhostHome
  & $engine -p uncensored gateway stop 2>&1 | Out-Null
  if ($gateway) { & $engine -p uncensored gateway uninstall 2>&1 | Out-Null }
}
foreach ($task in "ghost-bridge", "ghost-scrubber", "ghost-veil") {
  Stop-ScheduledTask -TaskName $task
  Unregister-ScheduledTask -TaskName $task -Confirm:$false
}
Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $PID -and $_.CommandLine -and $_.Name -match '^(python|hermes)' -and
  ($_.CommandLine.Contains("$GhostHome\") -or $_.CommandLine.Contains("$Eng\") -or $_.CommandLine.Contains($Eng.Replace('\', '\\')))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1

Write-Host "Removing files"
foreach ($dir in $Eng, "$Eng.old", "$Eng.new") {
  if (-not (Test-Path $dir)) { continue }
  if (Test-OurEngine $dir) { Remove-Item -Recurse -Force $dir } else { Write-Host "   left $dir (not an engine ghost built)" }
}
$src = [IO.File]::ReadAllText("$GhostHome\.src").Trim()
if ($src -eq "$env:USERPROFILE\.ghost-src" -and (Test-Path "$src\.git")) {
  if (git -C $src status --porcelain) { Write-Host "   left $src (it has changes of yours)" } else { Remove-Item -Recurse -Force $src }
}
if ($purge) {
  Remove-Item "$env:USERPROFILE\.opengradient\veil\session.json"
  Remove-Item -Recurse -Force $GhostHome
  Write-Host "ghost and all of its data are gone."
} else {
  foreach ($name in "venv", "tools", "installs", "cache", "python", "uv", "privacy\__pycache__", "privacy\*.py", "privacy\uncensored_prefill.json",
                    "privacy\bridge.key", "privacy\bridge.log*", ".src", ".install-env", ".installing") { Remove-Item -Recurse -Force "$GhostHome\$name" }
  Write-Host "ghost is gone. Your chats, skills, settings and login are still in $GhostHome (delete it to remove them too)."
}
foreach ($name in "ghost.cmd", "ghost-login.cmd", "ghost-update.cmd", "ghost-update.ps1", "ghost-uninstall.cmd", "ghost-uninstall.ps1") {
  Remove-Item -Force "$BinDir\$name"
}
if (-not (Get-ChildItem -Force $BinDir)) {   # nothing else of yours in there
  Remove-Item -Force $BinDir
  $path = [Environment]::GetEnvironmentVariable("Path", "User")
  [Environment]::SetEnvironmentVariable("Path", (($path -split ";" | Where-Object { $_ -and $_ -ne $BinDir }) -join ";"), "User")
}
exit 0
