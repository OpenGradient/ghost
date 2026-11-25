# ghost-uninstall -- remove ghost from this machine (`ghost uninstall`).
#
#   ghost uninstall             ask first, and keep your chats, skills and settings unless you say otherwise
#   ghost uninstall --yes       don't ask (your data is kept)
#   ghost uninstall --purge     delete your data too
param([switch]$y, [Parameter(ValueFromRemainingArguments = $true)][string[]]$Flags)
$ErrorActionPreference = "SilentlyContinue"
$env:PSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Machine")   # PowerShell 7's, if that's where you are
Remove-Item Env:GIT_DIR, Env:GIT_WORK_TREE                                             # some other repository's
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
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway_uncensored.vbs"   # (or it's a task)
$gateway = (Get-ScheduledTask -TaskName "Hermes_Gateway_uncensored") -or (Test-Path $startup)
$env:HERMES_NONINTERACTIVE = "1"
$engine = @("$Eng\.hermes\bin\hermes.exe", "$Eng\.hermes\bin\hermes.cmd") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($engine) {
  $env:HERMES_HOME = $GhostHome
  & $engine -p uncensored gateway stop --yes 2>&1 | Out-Null
  if ($gateway) { & $engine -p uncensored gateway uninstall 2>&1 | Out-Null }
}
if ((Test-Path $startup) -and [IO.File]::ReadAllText($startup).Contains("\.ghost\")) { Remove-Item -Force $startup }   # (the engine couldn't)
foreach ($task in "ghost-bridge", "ghost-scrubber", "ghost-veil") {
  Stop-ScheduledTask -TaskName $task
  Unregister-ScheduledTask -TaskName $task -Confirm:$false
}
Get-CimInstance Win32_Process | Where-Object {   # whatever runs from ghost's folders: Python, Node, its browser
  $_.ProcessId -ne $PID -and ("$($_.ExecutablePath)".StartsWith("$GhostHome\") -or "$($_.ExecutablePath)".StartsWith("$Eng\") -or
   ($_.Name -match '^(python|hermes)' -and $_.CommandLine -and
    ($_.CommandLine.Contains("$GhostHome\") -or $_.CommandLine.Contains("$Eng\") -or $_.CommandLine.Contains($Eng.Replace('\', '\\')))))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 1
# Antivirus may hold a file for a moment.
function Remove-Hard($path) { for ($i = 0; $i -lt 5 -and (Test-Path $path); $i++) { if ($i) { Start-Sleep -Seconds 1 }; Remove-Item -Recurse -Force $path } }

Write-Host "Removing files"
foreach ($dir in $Eng, "$Eng.old", "$Eng.new") {
  if (-not (Test-Path $dir)) { continue }
  if (Test-OurEngine $dir) { Remove-Hard $dir } else { Write-Host "   left $dir (not an engine ghost built)" }
}
$src = [IO.File]::ReadAllText("$GhostHome\.src").Trim()
if ($src -eq "$env:USERPROFILE\.ghost-src" -and (Test-Path "$src\.git")) {
  if ((git -C $src status --porcelain) -or (git -C $src stash list) -or (git -C $src log --oneline --branches --not --remotes)) { Write-Host "   left $src (it has changes of yours)" } else { Remove-Item -Recurse -Force $src }
}
if ($purge) {
  Remove-Item "$env:USERPROFILE\.opengradient\veil\session.json"
  Get-ChildItem -Force $GhostHome | Where-Object { $_.Name -ne "config" } | ForEach-Object { Remove-Hard $_.FullName }   # (the Ghost blog's command keeps its own file there)
  if (-not (Get-ChildItem -Force $GhostHome)) { Remove-Item -Force $GhostHome }
} else {
  foreach ($name in "venv", "tools", "installs", "cache", "python", "uv", "privacy\__pycache__", "privacy\*.py", "privacy\uncensored_prefill.json",
                    "privacy\bridge.key", "privacy\.made", "privacy\proxies.json", "privacy\bridge.log*", ".src", ".install-env", ".installing") { Remove-Hard "$GhostHome\$name" }
  Write-Host "ghost is gone. Your chats, skills, settings and login are still in $GhostHome (delete it to remove them too)."
}
foreach ($name in "ghost.cmd", "ghost-login.cmd", "ghost-update.cmd", "ghost-update.ps1", "ghost-uninstall.cmd", "ghost-uninstall.ps1") {
  Remove-Item -Force "$BinDir\$name"
}
if (-not (Get-ChildItem -Force $BinDir)) {   # nothing else of yours in there
  Remove-Item -Force $BinDir
  if (-not (Get-ChildItem -Force (Split-Path $BinDir))) { Remove-Item -Force (Split-Path $BinDir) }
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
  $path = $key.GetValue("Path", "", "DoNotExpandEnvironmentNames")
  $key.SetValue("Path", (($path -split ";" | Where-Object { $_ -and $_ -ne $BinDir }) -join ";"), "ExpandString"); $key.Close()
}
$left = @($Eng, "$env:USERPROFILE\.ghost-src", $(if ($purge) { $GhostHome }), "$GhostHome\venv", "$GhostHome\tools") | Where-Object { $_ -and (Test-Path $_) -and -not ($_ -eq $GhostHome -and (Test-Path "$GhostHome\config")) }
if ($left) { Write-Host "Something is still using these, so delete them yourself once it's closed: $($left -join ', ')"; exit 1 }
if ($purge) { Write-Host "ghost and all of its data are gone." }
exit 0
