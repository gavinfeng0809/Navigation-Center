# notice-hub 「点卡片用本地 Winbox 打开」安装器
#
# 由 nh-setup.cmd 调用（也可直接用 powershell -File 运行）。
#
#   nh-setup.cmd                      交互式安装：自动探测 + 弹窗让你指定 winbox.exe
#   nh-setup.cmd "C:\Tools\winbox.exe"  直接指定路径（适合批量部署）
#   nh-setup.cmd -Show                只看当前配置
#   nh-setup.cmd -Uninstall           卸载（删协议注册与配置）

param(
    [string]$WinboxPath = '',
    [string]$BaseUrl = 'http://10.0.0.10:8091',
    [switch]$Offline,
    [switch]$Uninstall,
    [switch]$Show
)

$ErrorActionPreference = 'Stop'
$AppDir  = Join-Path $env:LOCALAPPDATA 'notice-hub'
$CfgPath = Join-Path $AppDir 'config.json'
$Handler = Join-Path $AppDir 'nh-open.ps1'
$Here    = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$RegKey  = 'HKCU:\Software\Classes\nh'

function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }

# 处理程序是**拷贝**到 %LOCALAPPDATA% 的，不会自己跟着服务端更新 —— 曾经因此出过
# 「服务端修好了，同事机器上还是旧的」的坑。所以这里优先从门户拉最新一份，
# 拉不到（断网 / 门户没开 / 被墙）就回退本地文件，保证安装永远能走完，绝不卡死。
function Get-HandlerVersion([string]$Path) {
    try {
        $t = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
        $m = [regex]::Match($t, "\`$NhHandlerVersion\s*=\s*'([^']+)'")
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return ''
}

function Test-HandlerFile([string]$Path) {
    # 逐条判断，不用续行符 —— 反引号续行一旦尾随空格就断，这种错很难查
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $t = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    } catch { return $false }
    if (-not $t) { return $false }
    if (-not $t.Contains('param('))              { return $false }   # 有参数声明
    if (-not $t.Contains('[regex]::Match'))      { return $false }   # 确实是处理程序
    if (-not $t.Contains('$NhHandlerVersion'))   { return $false }   # 带版本号（新版标志）
    if (-not $t.Contains('nh://winbox/?'))       { return $false }   # 认浏览器规范化后的链接
    if (-not $t.Contains('Start-Process'))       { return $false }   # 用 Start-Process 启动
    if ($t.Contains('Invoke-Expression'))        { return $false }   # 绝不接受拼 shell 的版本
    return $true
}

function Save-Handler([string]$Src, [string]$Dest) {
    # 自己拷自己会抛 "source and destination are the same"，必须先挡掉
    if ($Src -ne $Dest) {
        Copy-Item -LiteralPath $Src -Destination $Dest -Force
    }
    # 顺带在原文件夹留一份，方便下次离线重装
    $side = Join-Path $Here 'nh-open.ps1'
    if ($Src -ne $side) {
        try { Copy-Item -LiteralPath $Src -Destination $side -Force } catch { }
    }
}

Say ''
Say '============================================' 'Cyan'
Say ' notice-hub  Winbox 本地联动 安装程序' 'Cyan'
Say '============================================' 'Cyan'
Say ''

# ---------------------------------------------------------------- 卸载
if ($Uninstall) {
    Say '  [1/2] 删除协议注册...'
    if (Test-Path $RegKey) { Remove-Item -Path $RegKey -Recurse -Force }
    Say '        已删除 HKCU\Software\Classes\nh' 'Green'
    Say '  [2/2] 删除本机配置...'
    if (Test-Path -LiteralPath $CfgPath) { Remove-Item -LiteralPath $CfgPath -Force }
    Say "        已删除 $CfgPath" 'Green'
    Say ''
    Say "  卸载完成（处理程序脚本保留在：$Handler）" 'Gray'
    Say ''
    exit 0
}

function Read-Cfg {
    if (-not (Test-Path -LiteralPath $CfgPath)) { return $null }
    try { return (Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

# ---------------------------------------------------------------- 只看配置
if ($Show) {
    $cfg = Read-Cfg
    $reg = (Get-ItemProperty -Path (Join-Path $RegKey 'shell\open\command') -ErrorAction SilentlyContinue).'(default)'
    if ($reg) {
        Say '  协议注册 : 已注册' 'Green'
        Say "             $reg" 'DarkGray'
    } else {
        Say '  协议注册 : 未注册（请运行 nh-setup.cmd 安装）' 'Red'
    }
    if ($cfg -and $cfg.winbox) {
        $ok = Test-Path -LiteralPath $cfg.winbox
        if ($ok) {
            Say "  winbox   : $($cfg.winbox)" 'Green'
        } else {
            Say "  winbox   : $($cfg.winbox)" 'Red'
            Say '             [警告] 这个路径已经不存在了，请重新运行安装' 'Yellow'
        }
    } else {
        Say '  winbox   : (未配置，将自动探测)' 'Yellow'
    }
    # 处理程序是拷贝到本机的，装的是哪一版只能靠版本号看出来
    if (Test-Path -LiteralPath $Handler) {
        $hv = Get-HandlerVersion $Handler
        $norm = $false
        try { $norm = (Get-Content -LiteralPath $Handler -Raw -Encoding UTF8).Contains('nh://winbox/?') } catch { }
        if ($hv) {
            Say "  处理程序 : $hv" 'Green'
        } else {
            Say '  处理程序 : 旧版（没有版本号）——建议重新运行 nh-setup.cmd 更新' 'Yellow'
        }
        if (-not $norm) {
            Say '             [警告] 这一版可能不认浏览器发来的链接（winbox 后多一个斜杠），' 'Red'
            Say '                    请重新运行 nh-setup.cmd 拉取最新处理程序。' 'Red'
        }
    } else {
        Say '  处理程序 : 未安装' 'Red'
    }
    Say ''
    exit 0
}

# ---------------------------------------------------------------- 1. 确定 winbox.exe
Say '  [1/3] 确定 winbox.exe 的位置...'

function Get-WinboxCandidates {
    $out = @()
    foreach ($n in @('winbox.exe', 'winbox64.exe')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { $out += $c.Source }
    }
    $out += @(
        "$env:LOCALAPPDATA\Winbox\winbox.exe",
        "$env:LOCALAPPDATA\Programs\Winbox\winbox.exe",
        "$env:ProgramFiles\Winbox\winbox.exe",
        "${env:ProgramFiles(x86)}\Winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\Winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\Winbox4\winbox.exe",
        "$env:USERPROFILE\Desktop\winbox.exe",
        "$env:USERPROFILE\Downloads\winbox.exe",
        "C:\Winbox\winbox.exe", "C:\Tools\Winbox\winbox.exe",
        "D:\Winbox\winbox.exe", "D:\Tools\Winbox\winbox.exe"
    )
    foreach ($d in @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
                     "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
                     "$env:USERPROFILE\Desktop")) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        foreach ($l in (Get-ChildItem -LiteralPath $d -Recurse -Filter '*winbox*.lnk' -ErrorAction SilentlyContinue)) {
            try {
                $t = (New-Object -ComObject WScript.Shell).CreateShortcut($l.FullName).TargetPath
                if ($t -and (Test-Path -LiteralPath $t)) { $out += $t }
            } catch { }
        }
    }
    return ($out | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique)
}

function Pick-Winbox {
    Add-Type -AssemblyName System.Windows.Forms
    Say '        请在弹出的窗口里选择 winbox.exe（通常在你解压 Winbox 的那个文件夹里）' 'Yellow'
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title  = '请选择 winbox.exe'
    $dlg.Filter = 'Winbox (winbox.exe)|winbox.exe;winbox64.exe|所有程序 (*.exe)|*.exe|所有文件 (*.*)|*.*'
    $dlg.CheckFileExists = $true
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.FileName }
    return $null
}

$winbox = $null
$old = (Read-Cfg)
if ($old -and $old.winbox -and (Test-Path -LiteralPath $old.winbox)) { $winbox = $old.winbox }

if (-not $WinboxPath) {
    $cands = @(Get-WinboxCandidates)
    if ($winbox) {
        Say "        当前配置：$winbox" 'Green'
        $ans = Read-Host '        沿用这个路径？(Y=沿用 / N=重新选择)'
        if ($ans -match '^[Nn]') { $winbox = $null }
    }
    if (-not $winbox) {
        if ($cands.Count -ge 1) {
            Say '        自动探测到这些候选：'
            for ($i = 0; $i -lt $cands.Count; $i++) { Say ("          [{0}] {1}" -f ($i + 1), $cands[$i]) }
            $ans = Read-Host "        用哪个？回车=第 1 个，或输入 0 手动选择"
            if ($ans -eq '0') { $winbox = Pick-Winbox }
            elseif ($ans -match '^\d+$' -and [int]$ans -ge 1 -and [int]$ans -le $cands.Count) { $winbox = $cands[[int]$ans - 1] }
            else { $winbox = $cands[0] }
        } else {
            Say '        没有自动找到 winbox.exe。' 'Yellow'
            $winbox = Pick-Winbox
        }
    }
} else {
    $winbox = $WinboxPath
}

if (-not $winbox -or -not (Test-Path -LiteralPath $winbox)) {
    Say ''
    Say '  [中止] 没有确定 winbox.exe 的位置。' 'Red'
    Say '         请先安装/解压 Winbox，或再次运行本安装并手动选择它的位置。' 'Red'
    Say ''
    exit 1
}
Say "        使用：$winbox" 'Green'
Say ''

# ---------------------------------------------------------------- 2. 写配置与处理程序
Say '  [2/3] 写入本机配置与处理程序...'
if (-not (Test-Path -LiteralPath $AppDir)) { New-Item -ItemType Directory -Path $AppDir -Force | Out-Null }

$cfgObj = [ordered]@{ winbox = $winbox; updated = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') }
($cfgObj | ConvertTo-Json) | Set-Content -LiteralPath $CfgPath -Encoding UTF8
Say "        配置：$CfgPath" 'Green'

$srcHandler = Join-Path $Here 'nh-open.ps1'
$staged     = ''          # 最终要装的那一份
$origin     = ''

# (1) 先试门户 —— 保证拿到的是最新版
if (-not $Offline) {
    $url = ($BaseUrl.TrimEnd('/')) + '/tools/nh-open.ps1'
    $tmp = Join-Path $env:TEMP ('nh-open-' + [guid]::NewGuid().ToString('N') + '.ps1')
    Say "        正在从门户获取最新处理程序..." 'Gray'
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 8 | Out-Null
        if (Test-HandlerFile $tmp) {
            $staged = $tmp
            $origin = "门户 $url"
        } else {
            Say '        门户返回的内容不像处理程序，改用本地文件' 'Yellow'
        }
    } catch {
        Say "        门户取不到（$($_.Exception.Message.Trim())），改用本地文件" 'Yellow'
    }
}

# (2) 门户不可用 → 回退到同目录的文件
if (-not $staged -and (Test-HandlerFile $srcHandler)) {
    $staged = $srcHandler
    $origin = '本地同目录文件'
}

# (3) 再退一步 → 用本机已装的那份，至少别让安装失败
if (-not $staged -and (Test-HandlerFile $Handler)) {
    $staged = $Handler
    $origin = '本机已安装的旧文件'
}

if (-not $staged) {
    Say '        [错误] 拿不到可用的 nh-open.ps1。' 'Red'
    Say '               请确认能访问门户，或把 nh-open.ps1 与 nh-setup.cmd 放在同一文件夹。' 'Red'
    exit 1
}

$oldVer = Get-HandlerVersion $Handler
$newVer = Get-HandlerVersion $staged
Save-Handler $staged $Handler
if ($staged -ne $Handler -and $staged -like (Join-Path $env:TEMP '*')) {
    Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue   # 临时文件不留在 %TEMP%
}
Say "        处理程序：$Handler" 'Green'
Say "        来源      ：$origin" 'Gray'
if ($newVer) { Say "        版本      ：$newVer" 'Gray' }
if ($oldVer -and $newVer -and $oldVer -ne $newVer) {
    Say "        （已从 $oldVer 更新到 $newVer）" 'Green'
} elseif ($oldVer -and $oldVer -eq $newVer) {
    Say '        （已是最新，无需更新）' 'Gray'
}
Say ''

# ---------------------------------------------------------------- 3. 注册 nh://
Say '  [3/3] 注册 nh:// 协议（仅当前用户，不需要管理员权限）...'
New-Item -Path $RegKey -Force | Out-Null
Set-ItemProperty -Path $RegKey -Name '(default)'    -Value 'URL:notice-hub Protocol'
Set-ItemProperty -Path $RegKey -Name 'URL Protocol' -Value ''
New-Item -Path (Join-Path $RegKey 'DefaultIcon') -Force | Out-Null
Set-ItemProperty -Path (Join-Path $RegKey 'DefaultIcon') -Name '(default)' -Value 'powershell.exe,0'
$cmdKey = Join-Path $RegKey 'shell\open\command'
New-Item -Path $cmdKey -Force | Out-Null
$cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" "%1"' -f $Handler
Set-ItemProperty -Path $cmdKey -Name '(default)' -Value $cmd
Say '        已注册 HKCU\Software\Classes\nh' 'Green'
Say ''

# ---------------------------------------------------------------- 自测
Say '  ============================================' 'Cyan'
Say '   安装完成' 'Green'
Say '  ============================================' 'Cyan'
Say ''
Say "   winbox.exe : $winbox"
Say '   协议       : nh://  →  已注册'
Say ''
$ans = Read-Host '   现在就用 10.0.0.9 试一下？(Y/N)'
if ($ans -match '^[Yy]' -or $ans -eq '') {
    # 自测必须走浏览器**实际会传进来**的形式，否则自测通过、网页点却没反应。
    # 浏览器把 winbox 当主机名，序列化时会补一个空路径：
    #   nh://winbox?host=X   →   实际收到 nh://winbox/?host=X   （winbox 后面多一个斜杠）
    # 两种形式都测，确保浏览器那条路真的通。
    foreach ($t in @('nh://winbox/?host=10.0.0.9', 'nh://winbox?host=10.0.0.9')) {
        Say "   测试 $t" 'Gray'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Handler $t
    }
    Say '   （如果 Winbox 起来了，说明一切就绪）' 'Green'
    Say '   注意：上面第一条是浏览器真实发出的形式，它必须能启动才说明网页可用。' 'Gray'
}
Say ''
Say '   改路径：再运行一次 nh-setup.cmd' 'Gray'
Say '   看配置：nh-setup.cmd -Show' 'Gray'
Say '   卸载  ：nh-setup.cmd -Uninstall' 'Gray'
Say ''
