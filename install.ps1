# Pasang service "ClaudeWatch" (start otomatis tiap boot) + aturan firewall subnet lokal + auto-buka browser saat login.
# Jalankan dari PowerShell biasa: .\install.ps1   (akan meminta UAC satu kali). Log: install.log
param([string]$Python, [string]$ClaudeHome, [string]$UserSid, [switch]$NoAutoOpen)
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$log = Join-Path $dir 'install.log'
$port = 64000

function Find-Python {
    $cands = @()
    $pyl = Get-Command py -ErrorAction SilentlyContinue
    if ($pyl) { $cands += (& py -3 -c "import sys;print(sys.executable)" 2>$null) }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { $cands += $p.Source }
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c) -and $c -notmatch '\\WindowsApps\\') {     # stub Microsoft Store tidak bisa jadi service
            if ((& $c -c "import sys;print(int(sys.version_info >= (3,8)))") -eq '1') { return $c }
        }
    }
    throw 'Python 3.8+ (instalasi penuh, bukan stub Microsoft Store) tidak ditemukan. Pasang dari python.org lalu ulangi.'
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    # tentukan Python & folder data Claude milik USER ini sebelum naik ke admin (service berjalan sebagai SYSTEM)
    $py0 = Find-Python
    $ch0 = Join-Path $env:USERPROFILE '.claude'
    $elevArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Python', "`"$py0`"", '-ClaudeHome', "`"$ch0`"", '-UserSid', [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    if ($NoAutoOpen) { $elevArgs += '-NoAutoOpen' }
    Start-Process powershell -Verb RunAs -Wait -ArgumentList $elevArgs
    if (Test-Path $log) { Get-Content $log }
    return
}

Start-Transcript -Path $log -Force | Out-Null
try {
    if (-not $Python) { $Python = Find-Python }
    if (-not $ClaudeHome) { $ClaudeHome = Join-Path $env:USERPROFILE '.claude' }
    "Python     : $Python"
    "Claude home: $ClaudeHome"

    # config.json lokal (tidak masuk repo): salin dari contoh, isi claudeHome
    $cfgPath = Join-Path $dir 'config.json'
    if (-not (Test-Path $cfgPath)) { Copy-Item (Join-Path $dir 'config.example.json') $cfgPath }
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfg | Add-Member -NotePropertyName claudeHome -NotePropertyValue $ClaudeHome -Force
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding($false)))

    # LocalSystem hanya melihat site-packages Python sistem -> pywin32 harus terpasang di sana
    $ErrorActionPreference = 'Continue'
    & $Python -I -m pip install --disable-pip-version-check "pywin32>=306" 2>&1 | Out-String | Write-Output
    $ok = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $ok) { throw 'pip install pywin32 gagal' }
    $purelib = (& $Python -I -c "import sysconfig;print(sysconfig.get_paths()['purelib'])")
    if (-not (Test-Path (Join-Path $purelib 'win32\servicemanager.pyd'))) { throw "pywin32 tidak ada di site-packages sistem ($purelib)" }

    if (Get-Service ClaudeWatch -ErrorAction SilentlyContinue) {
        Stop-Service ClaudeWatch -Force -ErrorAction SilentlyContinue
        & $Python (Join-Path $dir 'service.py') remove
    }
    & $Python (Join-Path $dir 'service.py') --startup auto install
    sc.exe config ClaudeWatch start= delayed-auto | Out-Null         # tunggu jaringan/profil siap
    sc.exe failure ClaudeWatch reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null

    # beri user yang memasang hak Start/Stop/Restart service TANPA UAC (bukan hak ubah/hapus konfigurasi)
    if ($UserSid) {
        $sd = (sc.exe sdshow ClaudeWatch | Where-Object { $_.Trim() }) -join ''
        if (-not $sd.Contains(";;;$UserSid)")) {
            sc.exe sdset ClaudeWatch ([regex]::Replace($sd, '^(D:(?:P|AI|AR)*)', "`$1(A;;LCRPWPDTLOCRRC;;;$UserSid)")) | Out-Null
        }
    }

    # firewall: hanya subnet lokal
    Get-NetFirewallRule -DisplayName 'Claude Watch' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Claude Watch' -Direction Inbound -Protocol TCP -LocalPort $port `
        -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null

    # auto-buka browser saat user ini login (service tidak bisa membuka jendela di desktop)
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\claude-watch-open.vbs'
    if (-not $NoAutoOpen) {
        $vbs = @"
' Buka Claude Watch di browser default tiap login Windows (tunggu service siap maks 2 menit).
Set sh = CreateObject("WScript.Shell")
ps = "`$d=(Get-Date).AddMinutes(2); while((Get-Date) -lt `$d){ try{ `$c=New-Object Net.Sockets.TcpClient('127.0.0.1',$port); `$c.Close(); break }catch{ Start-Sleep 2 } }; Start-Process 'http://127.0.0.1:$port/'"
sh.Run "powershell -NoProfile -WindowStyle Hidden -Command """ & ps & """", 0, False
"@
        [IO.File]::WriteAllText($startup, $vbs, (New-Object Text.UTF8Encoding($false)))
    }

    Start-Service ClaudeWatch
    Start-Sleep 3
    Get-Service ClaudeWatch | Format-Table Name, Status, StartType -Auto
    (Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing).Content
    'INSTALL OK'
    "Langkah berikut: set PIN akses remote ->  python setpin.py <PIN>   lalu buka http://127.0.0.1:$port"
} catch {
    "GAGAL: $($_ | Out-String)"
} finally {
    Stop-Transcript | Out-Null
}