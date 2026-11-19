# Remove the tailclip Windows client (shortcuts, script and config).
$programs = [Environment]::GetFolderPath('Programs')
Remove-Item -ErrorAction SilentlyContinue (Join-Path $programs 'tailclip - send clipboard.lnk'), (Join-Path $programs 'tailclip - get latest clip.lnk')
Remove-Item -Recurse -ErrorAction SilentlyContinue (Join-Path $env:USERPROFILE '.tailclip')
Write-Host 'tailclip client removed. Files you pulled stay in Downloads\tailclip.'
