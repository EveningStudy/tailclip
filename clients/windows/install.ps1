<#
.SYNOPSIS
  Install the tailclip Windows client: copies tailclip.ps1 to
  %USERPROFILE%\.tailclip, saves the relay URL, and adds two Start menu
  shortcuts with global hotkeys.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Url https://myserver.tail1234.ts.net
#>
param(
  [Parameter(Mandatory = $true)][string]$Url,
  [string]$Source = 'Windows',
  [string]$PushHotkey = 'CTRL+ALT+SHIFT+C',
  [string]$PullHotkey = 'CTRL+ALT+SHIFT+V'
)
$ErrorActionPreference = 'Stop'

$dir = Join-Path $env:USERPROFILE '.tailclip'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'tailclip.ps1') $dir
$config = [ordered]@{ url = $Url.TrimEnd('/'); source = $Source } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $dir 'config.json'), $config, (New-Object Text.UTF8Encoding $false))

$programs = [Environment]::GetFolderPath('Programs')
$shell = New-Object -ComObject WScript.Shell
function New-TailclipShortcut([string]$Name, [string]$Action, [string]$Hotkey) {
  $link = $shell.CreateShortcut((Join-Path $programs "$Name.lnk"))
  # conhost --headless runs PowerShell without flashing a console window.
  $link.TargetPath = Join-Path $env:WINDIR 'System32\conhost.exe'
  $link.Arguments = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$dir\tailclip.ps1`" $Action"
  $link.WorkingDirectory = $dir
  $link.Hotkey = $Hotkey
  $link.Description = "tailclip: $Action"
  $link.Save()
}
New-TailclipShortcut 'tailclip - send clipboard' 'push' $PushHotkey
New-TailclipShortcut 'tailclip - get latest clip' 'pull' $PullHotkey

Write-Host "Installed to $dir"
Write-Host "  $PushHotkey  send this PC's clipboard"
Write-Host "  $PullHotkey  copy the latest clip to this PC"
try {
  $health = Invoke-RestMethod -UseBasicParsing -TimeoutSec 10 "$($Url.TrimEnd('/'))/healthz"
  Write-Host "Relay reachable (tailclip $($health.version))."
} catch {
  Write-Warning "Could not reach $Url yet: $($_.Exception.Message)"
}
Write-Host 'If a hotkey does not respond at first, sign out and back in once so Windows picks it up.'
