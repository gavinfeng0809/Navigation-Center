# notice-hub 本地协议处理程序（Winbox 联动）
#
# 由 nh:// 协议调用，形如：
#     nh://winbox?host=10.94.6.9
#     nh://winbox?host=10.94.6.9:8291
#
# winbox.exe 的位置来自本机配置：
#     %LOCALAPPDATA%\notice-hub\config.json      { "winbox": "C:\\Tools\\winbox.exe" }
# 配置由 nh-setup.cmd 写入（它会弹出文件选择框让你指定路径）。
# 配置缺失或路径失效时会自动再探测一轮 PATH / 常见目录 / 开始菜单快捷方式。
#
# ⚠️ 安全要点：nh:// 可以被**任意网页**触发（不止 notice-hub），
# 所以这里必须严格校验参数，绝不能把原文拼进命令行执行。

param([Parameter(Mandatory = $true)][string]$Uri)

# 版本号：改动本文件时请一并更新，安装器会用它告诉你本机装的是不是最新一份。
# 处理程序是拷贝到 %LOCALAPPDATA% 的，不会自动跟随服务端更新，只能靠版本号比对。
$NhHandlerVersion = 'nh-open 2026-09-29'

$ErrorActionPreference = 'Stop'
$AppDir = Join-Path $env:LOCALAPPDATA 'notice-hub'
$CfgPath = Join-Path $AppDir 'config.json'

function Show-Msg([string]$text, [string]$title = 'notice-hub') {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [void][System.Windows.Forms.MessageBox]::Show($text, $title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
    } catch {
        Write-Host "$title : $text"
    }
}

# ---- 1. 解析并**严格校验**参数 -------------------------------------------
$target = $null

# 形式一：nh://winbox?host=xxx
# 关键：浏览器会把自定义协议规范化 —— `nh://winbox?host=X` 里的 winbox 被当成**主机名**，
# 序列化时会补出一个空路径，实际传进来的是 `nh://winbox/?host=X`（winbox 后面多一个斜杠）。
# 所以正则必须容忍可选的 "/"，否则从网页点会报「无法识别的链接」，
# 而安装器里直接调用却能成功（那条路没经过浏览器）。
$m = [regex]::Match($Uri, '^nh://winbox/?\?(.+)$', 'IgnoreCase')
if ($m.Success) {
    $hm = [regex]::Match($m.Groups[1].Value, '(?:^|&)host=([^&]+)', 'IgnoreCase')
    if ($hm.Success) { $target = [System.Uri]::UnescapeDataString($hm.Groups[1].Value) }
}

# 形式二：nh://winbox/10.94.6.9 （把地址放在路径里，省掉 query 规范化的麻烦）
if (-not $target) {
    $m2 = [regex]::Match($Uri, '^nh://winbox/([^/?]+)/?$', 'IgnoreCase')
    if ($m2.Success) { $target = [System.Uri]::UnescapeDataString($m2.Groups[1].Value) }
}

# 形式三：winbox://10.94.6.9
if (-not $target) {
    $m3 = [regex]::Match($Uri, '^winbox://(.+)$', 'IgnoreCase')
    if ($m3.Success) { $target = $m3.Groups[1].Value.TrimEnd('/') }
}

# 形式四：nh://10.94.6.9 （地址直接放在主机位）
if (-not $target) {
    $m4 = [regex]::Match($Uri, '^nh://([^/?]+)/?$', 'IgnoreCase')
    if ($m4.Success) { $target = $m4.Groups[1].Value }
}
if (-not $target) {
    Show-Msg "无法识别的链接：`n$Uri`n`n支持这些形式：`n  nh://winbox?host=10.94.6.9`n  nh://winbox/?host=10.94.6.9`n  nh://winbox/10.94.6.9"
    exit 1
}
# 只允许 IPv4 / 主机名 + 可选端口；这一条同时挡掉了命令注入
if ($target -notmatch '^[A-Za-z0-9]([A-Za-z0-9\.\-]{0,253})(:\d{1,5})?$') {
    Show-Msg "设备地址格式不合法，已拒绝：`n$target"
    exit 1
}

# ---- 2. 找 winbox.exe：先用配置，再自动探测 -------------------------------
function Find-Winbox {
    # a) 本机配置里的路径优先
    if (Test-Path -LiteralPath $CfgPath) {
        try {
            $cfg = Get-Content -LiteralPath $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.winbox -and (Test-Path -LiteralPath $cfg.winbox)) { return $cfg.winbox }
            if ($cfg.winbox) { $script:cfgStale = $cfg.winbox }
        } catch { }
    }
    # b) PATH
    foreach ($n in @('winbox.exe', 'winbox64.exe')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    # c) 常见安装位置
    $cands = @(
        "$env:LOCALAPPDATA\Winbox\winbox.exe",
        "$env:LOCALAPPDATA\Programs\Winbox\winbox.exe",
        "$env:ProgramFiles\Winbox\winbox.exe",
        "${env:ProgramFiles(x86)}\Winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\Winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\winbox\winbox.exe",
        "$env:ProgramFiles\MikroTik\Winbox4\winbox.exe",
        "$env:USERPROFILE\Desktop\winbox.exe",
        "$env:USERPROFILE\Downloads\winbox.exe",
        "C:\Winbox\winbox.exe",
        "C:\Tools\Winbox\winbox.exe",
        "D:\Winbox\winbox.exe",
        "D:\Tools\Winbox\winbox.exe"
    )
    foreach ($p in $cands) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    # d) 开始菜单 / 桌面快捷方式指向的目标
    $dirs = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
              "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
              "$env:USERPROFILE\Desktop")
    foreach ($d in $dirs) {
        if (-not (Test-Path -LiteralPath $d)) { continue }
        foreach ($l in (Get-ChildItem -LiteralPath $d -Recurse -Filter '*winbox*.lnk' -ErrorAction SilentlyContinue)) {
            try {
                $t = (New-Object -ComObject WScript.Shell).CreateShortcut($l.FullName).TargetPath
                if ($t -and (Test-Path -LiteralPath $t)) { return $t }
            } catch { }
        }
    }
    return $null
}

$script:cfgStale = $null
$winbox = Find-Winbox
if (-not $winbox) {
    $extra = ''
    if ($script:cfgStale) {
        $extra = "`n`n配置里记的路径已经不存在了：`n$($script:cfgStale)`n（可能 Winbox 被移动或卸载了）"
    }
    Show-Msg "没有找到 winbox.exe。$extra`n`n请重新运行一次 nh-setup.cmd，在弹出的小窗里**指定 winbox.exe 的位置**。`n`n（本次要打开的目标：$target）"
    exit 2
}

# ---- 3. 启动（地址作为**独立参数**传入，不经过 shell） ---------------------
try {
    Start-Process -FilePath $winbox -ArgumentList $target
} catch {
    Show-Msg "启动 Winbox 失败：`n$($_.Exception.Message)`n`n路径：$winbox"
    exit 3
}
exit 0
