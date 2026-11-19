<#
.SYNOPSIS
  tailclip client for Windows: send the clipboard to your tailclip relay, or
  put the latest clip on the clipboard.

.EXAMPLE
  .\tailclip.ps1 push      # send text, an image or copied files
  .\tailclip.ps1 pull      # fetch the newest clip into the clipboard

  The relay URL is read from %USERPROFILE%\.tailclip\config.json (written by
  install.ps1), or pass -Url. Needs Windows PowerShell 5.1 (powershell.exe).
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)][ValidateSet('push', 'pull')][string]$Action,
  [string]$Url,
  [string]$Source,
  [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Net.Http
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$configPath = Join-Path $env:USERPROFILE '.tailclip\config.json'
if (Test-Path $configPath) {
  $config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if (-not $Url) { $Url = $config.url }
  if (-not $Source) { $Source = $config.source }
}
if (-not $Source) { $Source = 'Windows' }

function Show-Note([string]$Text, [bool]$IsError = $false) {
  if ($Quiet) { Write-Host $Text; return }
  $icon = New-Object System.Windows.Forms.NotifyIcon
  $icon.Icon = [System.Drawing.SystemIcons]::Information
  $icon.Visible = $true
  $tip = if ($IsError) { [System.Windows.Forms.ToolTipIcon]::Error } else { [System.Windows.Forms.ToolTipIcon]::Info }
  $icon.ShowBalloonTip(3000, 'tailclip', $Text, $tip)
  Start-Sleep -Milliseconds 3200
  $icon.Dispose()
}

function Get-Preview([string]$Text) {
  $one = ($Text -replace '\s+', ' ').Trim()
  if ($one.Length -gt 60) { $one = $one.Substring(0, 60) + '...' }
  return $one
}

function Get-DownloadsDir {
  try {
    $path = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path
    if ($path) { return $path }
  } catch { }
  return (Join-Path $env:USERPROFILE 'Downloads')
}

$client = New-Object System.Net.Http.HttpClient
$client.Timeout = [TimeSpan]::FromSeconds(120)
$client.DefaultRequestHeaders.Add('X-Source', $Source)

function Send-Clip([byte[]]$Bytes, [string]$Type, [string]$Name) {
  $content = New-Object System.Net.Http.ByteArrayContent -ArgumentList (, $Bytes)
  $content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse($Type)
  $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList ([System.Net.Http.HttpMethod]::Post), "$Url/api/clips"
  $request.Content = $content
  if ($Name) { [void]$request.Headers.TryAddWithoutValidation('X-Filename', [Uri]::EscapeDataString($Name)) }
  $response = $client.SendAsync($request).GetAwaiter().GetResult()
  if (-not $response.IsSuccessStatusCode) {
    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    throw "Relay answered $([int]$response.StatusCode): $body"
  }
}

function Invoke-Push {
  $cb = [System.Windows.Forms.Clipboard]
  if ($cb::ContainsImage()) {
    $image = $cb::GetImage()
    $stream = New-Object IO.MemoryStream
    $image.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    Send-Clip $stream.ToArray() 'image/png' $null
    Show-Note "Sent image ($($image.Width) x $($image.Height))"
  } elseif ($cb::ContainsFileDropList()) {
    $files = @($cb::GetFileDropList() | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($files.Count -eq 0) { throw 'Only files can be sent, not folders.' }
    foreach ($file in $files) {
      Send-Clip ([IO.File]::ReadAllBytes($file)) 'application/octet-stream' ([IO.Path]::GetFileName($file))
    }
    if ($files.Count -eq 1) { Show-Note "Sent $([IO.Path]::GetFileName($files[0]))" } else { Show-Note "Sent $($files.Count) files" }
  } elseif ($cb::ContainsText()) {
    $text = $cb::GetText()
    Send-Clip ([Text.Encoding]::UTF8.GetBytes($text)) 'text/plain; charset=utf-8' $null
    Show-Note "Sent: $(Get-Preview $text)"
  } else {
    Show-Note 'The clipboard is empty.' $true
  }
}

function Invoke-Pull {
  $cb = [System.Windows.Forms.Clipboard]
  $response = $client.GetAsync("$Url/api/latest").GetAwaiter().GetResult()
  if ([int]$response.StatusCode -eq 404) { Show-Note 'Nothing on tailclip yet.'; return }
  if (-not $response.IsSuccessStatusCode) { throw "Relay answered $([int]$response.StatusCode)" }

  $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
  $type = $response.Content.Headers.ContentType.MediaType
  $kind = ($response.Headers.GetValues('X-Tailclip-Kind') | Select-Object -First 1)
  $name = $null
  $names = $null
  if ($response.Headers.TryGetValues('X-Tailclip-Name', [ref]$names)) { $name = [Uri]::UnescapeDataString(@($names)[0]) }

  if ($kind -eq 'text') {
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    $cb::SetText($text)
    Show-Note "Copied: $(Get-Preview $text)"
  } elseif ($kind -eq 'image' -and @('image/png', 'image/jpeg', 'image/gif', 'image/bmp') -contains $type) {
    $image = [System.Drawing.Image]::FromStream((New-Object IO.MemoryStream -ArgumentList (, $bytes)))
    $data = New-Object System.Windows.Forms.DataObject
    $data.SetImage($image)
    if ($type -eq 'image/png') { $data.SetData('PNG', $false, (New-Object IO.MemoryStream -ArgumentList (, $bytes))) }
    $cb::SetDataObject($data, $true)
    Show-Note "Copied image ($($image.Width) x $($image.Height))"
  } else {
    # Anything Windows can't hold as clipboard data (files, HEIC photos...):
    # save it and put the file itself on the clipboard, ready to paste in Explorer.
    if (-not $name) {
      $ext = switch ($type) { 'image/heic' { '.heic' } 'image/webp' { '.webp' } default { '.bin' } }
      $name = 'tailclip-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + $ext
    }
    $name = ($name -replace '[\\/:*?"<>|]', '_')
    $dir = Join-Path (Get-DownloadsDir) 'tailclip'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $path = Join-Path $dir $name
    $stem = [IO.Path]::GetFileNameWithoutExtension($name); $ext = [IO.Path]::GetExtension($name); $i = 1
    while (Test-Path -LiteralPath $path) { $path = Join-Path $dir "$stem ($i)$ext"; $i++ }
    [IO.File]::WriteAllBytes($path, $bytes)
    $list = New-Object System.Collections.Specialized.StringCollection
    [void]$list.Add($path)
    $cb::SetFileDropList($list)
    Show-Note "Saved $([IO.Path]::GetFileName($path)) to Downloads\tailclip (paste it into any folder)"
  }
}

try {
  if (-not $Url) { throw "No relay URL. Run install.ps1 -Url https://<server>.<tailnet>.ts.net, or pass -Url." }
  $Url = $Url.TrimEnd('/')
  if ($Action -eq 'push') { Invoke-Push } else { Invoke-Pull }
} catch {
  $e = $_.Exception
  $message = $e.Message
  $network = $false
  while ($e) {
    if ($e -is [System.Net.Http.HttpRequestException] -or $e -is [System.Net.WebException] -or
        $e -is [System.Net.Sockets.SocketException] -or $e -is [System.Threading.Tasks.TaskCanceledException]) { $network = $true }
    $message = $e.Message
    $e = $e.InnerException
  }
  if ($network) { $message = "Can't reach the relay. Is Tailscale connected? ($message)" }
  Show-Note $message $true
  exit 1
} finally {
  $client.Dispose()
}
