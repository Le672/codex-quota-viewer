param([switch]$AutoStart, [switch]$NoLaunch, [switch]$NoCodexFollow)
$ErrorActionPreference = 'Stop'
$sourceExe = Join-Path $PSScriptRoot 'dist\Codex额度查看器.exe'
$sourceFollower = Join-Path $PSScriptRoot 'dist\Codex额度联动.exe'
if (-not (Test-Path -LiteralPath $sourceExe)) { throw '请先运行 build.bat 生成 EXE' }
if (-not (Test-Path -LiteralPath $sourceFollower)) { throw '请重新构建，缺少 Codex额度联动.exe' }
$installDirectory = Join-Path $env:LOCALAPPDATA 'Programs\CodexQuotaViewer'
New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
$installedExe = Join-Path $installDirectory 'Codex额度查看器.exe'
$installedFollower = Join-Path $installDirectory 'Codex额度联动.exe'
$followTaskName = 'CodexQuotaViewer-FollowCodex'
$followTask = Get-ScheduledTask -TaskName $followTaskName -ErrorAction SilentlyContinue
if ($followTask) { Stop-ScheduledTask -TaskName $followTaskName -ErrorAction SilentlyContinue }
$quotaProcesses = @(Get-Process -Name 'Codex额度查看器','Codex额度联动' -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -in @($installedExe,$installedFollower) } catch { $false }
})
foreach ($quotaProcess in $quotaProcesses) {
    if (-not $quotaProcess.HasExited) { Stop-Process -InputObject $quotaProcess -Force -ErrorAction SilentlyContinue }
    if (-not $quotaProcess.WaitForExit(5000)) { throw '旧版额度查看器未退出，请稍后重试' }
}
foreach ($installFile in @(
    @{ Source=$sourceExe; Destination=$installedExe },
    @{ Source=$sourceFollower; Destination=$installedFollower }
)) {
    for ($copyAttempt = 0; $copyAttempt -lt 10; $copyAttempt++) {
        try {
            Copy-Item -LiteralPath $installFile.Source -Destination $installFile.Destination -Force
            break
        } catch [System.IO.IOException] {
            if ($copyAttempt -eq 9) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
}
$iconVersion = (Get-Item -LiteralPath $installedExe).VersionInfo.FileVersion
$sourceIcon = Join-Path $PSScriptRoot 'icon.ico'
$installedIcon = Join-Path $installDirectory "quota-icon-$iconVersion.ico"
Copy-Item -LiteralPath $sourceIcon -Destination $installedIcon -Force
$changedIconPaths = [System.Collections.Generic.List[string]]::new()
$changedIconPaths.Add($installedExe)
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $folder 'Codex 额度查看器.lnk'))
    $shortcut.TargetPath = $installedExe
    $shortcut.Arguments = ''
    $shortcut.WorkingDirectory = $installDirectory
    $shortcut.IconLocation = "$installedIcon,0"
    $shortcut.Description = '查看 Codex 5 小时、每周额度和重置卡'
    $shortcut.Save()
    $changedIconPaths.Add($shortcut.FullName)
}
$startupDirectory = [Environment]::GetFolderPath('Startup')
if ($NoCodexFollow -and $AutoStart) {
    $shortcut = $shortcutShell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Codex 额度查看器.lnk'))
    $shortcut.TargetPath = $installedExe
    $shortcut.Arguments = '--minimized'
    $shortcut.WorkingDirectory = $installDirectory
    $shortcut.IconLocation = "$installedIcon,0"
    $shortcut.Description = '随 Codex 桌面启动和退出；Codex 未运行时不显示额度窗口'
    $shortcut.Save()
    $changedIconPaths.Add($shortcut.FullName)
}
# 更新本应用图标；任务栏保留参数，登录入口迁移到独立联动器。
$existingShortcutFolders = @(
    (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'),
    [Environment]::GetFolderPath('Startup')
)
foreach ($folder in $existingShortcutFolders) {
    if (-not (Test-Path -LiteralPath $folder)) { continue }
    foreach ($file in Get-ChildItem -LiteralPath $folder -Filter '*.lnk' -File) {
        $shortcut = $shortcutShell.CreateShortcut($file.FullName)
        if ($shortcut.TargetPath -notin @($installedExe,$installedFollower)) { continue }
        if ($folder -eq $startupDirectory) {
            if (-not $NoCodexFollow -or -not $AutoStart) {
                Remove-Item -LiteralPath $file.FullName
                continue
            }
            $shortcut.Arguments = '--minimized'
            $shortcut.TargetPath = $installedExe
        }
        $shortcut.IconLocation = "$installedIcon,0"
        $shortcut.Save()
        $changedIconPaths.Add($file.FullName)
    }
}
if (-not ('QuotaIconNotify' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class QuotaIconNotify {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    public static extern void SHChangeNotify(uint events, uint flags, string path, IntPtr second);
}
'@
}
foreach ($path in $changedIconPaths) {
    [QuotaIconNotify]::SHChangeNotify(0x00002000, 0x00000005, $path, [IntPtr]::Zero)
}
Write-Output "已安装：$installedExe"
# MSIX 子进程会虚拟化 AppData；共享目录必须位于 AppData 之外。
# 偏好由独立任务首次启动时迁移，避免安装命令读取到 Codex 包中的旧副本。
$sharedDataDirectory = Join-Path $env:USERPROFILE '.codex-quota-viewer'
New-Item -ItemType Directory -Path $sharedDataDirectory -Force | Out-Null
$storageMarker = Join-Path $sharedDataDirectory 'storage.json'
if (-not (Test-Path -LiteralPath $storageMarker)) {
    [ordered]@{ schema=1; reason='shared-data-outside-msix-appdata' } |
        ConvertTo-Json | Set-Content -LiteralPath $storageMarker -Encoding UTF8
}
if ($NoCodexFollow) {
    & (Join-Path $PSScriptRoot 'register-follow-task.ps1') -FollowerExe $installedFollower -Remove -StateDirectory $sharedDataDirectory
} else {
    & (Join-Path $PSScriptRoot 'register-follow-task.ps1') -FollowerExe $installedFollower -NoLaunch:$NoLaunch -StateDirectory $sharedDataDirectory
}
if (-not $NoCodexFollow) { Write-Output '已启用：随 Codex 桌面启动和退出' }
if (-not $NoLaunch) {
    if ($NoCodexFollow) { Start-Process -FilePath $installedExe -WindowStyle Hidden }
    # 联动模式已由 Windows 计划任务启动，不能从当前进程派生联动器。
}
