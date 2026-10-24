# Hapus service ClaudeWatch + aturan firewall + auto-buka browser. config.json (PIN, pengaturan) TIDAK dihapus.
param([string]$Python)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\claude-watch-open.vbs'
    Remove-Item $startup -ErrorAction SilentlyContinue            # milik profil user ini, hapus sebelum naik ke admin
    $py0 = $null
    $pyl = Get-Command py -ErrorAction SilentlyContinue
    if ($pyl) { $py0 = (& py -3 -c "import sys;print(sys.executable)" 2>$null) }
    if (-not $py0) { $p = Get-Command python -ErrorAction SilentlyContinue; if ($p) { $py0 = $p.Source } }
    $elevArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($py0) { $elevArgs += @('-Python', "`"$py0`"") }
    Start-Process powershell -Verb RunAs -Wait -ArgumentList $elevArgs
    return
}
Stop-Service ClaudeWatch -Force -ErrorAction SilentlyContinue
if ($Python) { & $Python (Join-Path $PSScriptRoot 'service.py') remove } else { sc.exe delete ClaudeWatch | Out-Null }
Get-NetFirewallRule -DisplayName 'Claude Watch' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
'Dihapus.'