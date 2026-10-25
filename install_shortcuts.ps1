<#
.SYNOPSIS
    安装 / 刷新 MicroBench 的桌面与开始菜单快捷方式。

.DESCRIPTION
    主入口「MicroBench 工作台」= **独立应用窗口**（pywebview / Edge WebView2），
    双击后弹出没有地址栏、没有标签页的原生窗口，和普通软件一样。

    另外两个是辅助入口：
      · 「MicroBench 网页版」→ start_workbench.bat（起服务 + 用默认浏览器打开）
      · 「MicroBench 项目目录」→ 资源管理器打开项目文件夹

.NOTES
    这个文件名**不能带前导下划线**：仓库根目录的 /_*.ps1 被 .gitignore 吃掉，
    脚本就传不到另一台设备（30461）上。两台设备各自跑一次本脚本即可。
    用法:
        pwsh -NoProfile -File install_shortcuts.ps1
        pwsh -NoProfile -File install_shortcuts.ps1 -ProjectRoot 'D:\somewhere\MicroBench'
#>
param(
    # 项目根目录，默认取脚本所在目录；项目搬家后用 -ProjectRoot 指定新路径重跑即可
    [string]$ProjectRoot = $PSScriptRoot
)

$ErrorActionPreference = 'Stop'

$project = $ProjectRoot
$appBat  = Join-Path $project 'MicroBench Desktop.bat'
$webBat  = Join-Path $project 'start_workbench.bat'
$icon    = Join-Path $project 'assets\microbench.ico'
$desktop = [Environment]::GetFolderPath('Desktop')

if (-not (Test-Path $appBat)) { throw "桌面启动脚本不存在: $appBat" }
if (-not (Test-Path $webBat)) { throw "网页版启动脚本不存在: $webBat" }
if (-not (Test-Path $icon))    { throw "图标不存在: $icon" }

$shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param(
        [string]$Path,
        [string]$Target,
        [string]$Arguments,
        [string]$WorkDir,
        [string]$IconLocation,
        [string]$Description,
        [int]$WindowStyle = 1
    )
    $sc = $shell.CreateShortcut($Path)
    $sc.TargetPath       = $Target
    if ($Arguments)   { $sc.Arguments   = $Arguments }
    $sc.WorkingDirectory = $WorkDir
    $sc.IconLocation     = $IconLocation
    $sc.Description      = $Description
    $sc.WindowStyle      = $WindowStyle
    $sc.Save()
    Write-Output "created: $Path"
}

# 1) 主入口：独立应用窗口（不经过浏览器）
New-Shortcut -Path (Join-Path $desktop 'MicroBench 工作台.lnk') `
              -Target $appBat `
              -Arguments '' `
              -WorkDir $project `
              -IconLocation $icon `
              -Description '双击启动 MicroBench 桌面应用（独立窗口，不走浏览器）'

# 2) 网页版：起服务 + 用默认浏览器打开（需要浏览器插件/多标签时用）
New-Shortcut -Path (Join-Path $desktop 'MicroBench 网页版.lnk') `
              -Target $webBat `
              -Arguments '' `
              -WorkDir $project `
              -IconLocation $icon `
              -Description '启动 MicroBench 服务并用默认浏览器打开 (http://127.0.0.1:5000)'

# 3) 打开项目目录
New-Shortcut -Path (Join-Path $desktop 'MicroBench 项目目录.lnk') `
              -Target "$env:WINDIR\explorer.exe" `
              -Arguments "`"$project`"" `
              -WorkDir $project `
              -IconLocation $icon `
              -Description '打开 MicroBench 项目文件夹'

# 4) 开始菜单副本（Win 搜索可直达）
$startMenu = [Environment]::GetFolderPath('Programs')
if ($startMenu -and (Test-Path $startMenu)) {
    New-Shortcut -Path (Join-Path $startMenu 'MicroBench 工作台.lnk') `
                  -Target $appBat `
                  -Arguments '' `
                  -WorkDir $project `
                  -IconLocation $icon `
                  -Description '双击启动 MicroBench 桌面应用（独立窗口，不走浏览器）'
    New-Shortcut -Path (Join-Path $startMenu 'MicroBench 网页版.lnk') `
                  -Target $webBat `
                  -Arguments '' `
                  -WorkDir $project `
                  -IconLocation $icon `
                  -Description '启动 MicroBench 服务并用默认浏览器打开 (http://127.0.0.1:5000)'
}

Write-Output ''
Write-Output '--- verify ---'
Get-ChildItem -Path $desktop -Filter 'MicroBench *.lnk' | ForEach-Object {
    $l = $shell.CreateShortcut($_.FullName)
    [PSCustomObject]@{
        Name = $_.Name
        Target = $l.TargetPath
        WorkDir = $l.WorkingDirectory
        Icon = $l.IconLocation
    }
} | Format-List
