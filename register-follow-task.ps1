param(
    [Parameter(Mandatory)][string]$FollowerExe,
    [string]$TaskName = 'CodexQuotaViewer-FollowCodex',
    [string]$Arguments = '--watch-codex',
    [string]$StateDirectory = (Join-Path $env:APPDATA 'CodexQuotaViewer'),
    [switch]$Remove,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
if ($TaskName -notmatch '^CodexQuotaViewer-(FollowCodex|Test-[a-f0-9]{16,32})$') {
    throw '计划任务名称超出本应用范围'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = $identity.User.Value
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $principalId = $existing.Principal.UserId
    if ($principalId -notmatch '^S-1-') {
        $principalId = ([Security.Principal.NTAccount]::new($principalId)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($principalId -ne $userSid) {
        throw '同名任务属于其他用户，未修改'
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
$marker = Join-Path $StateDirectory 'codex-follow-task.json'
if ($Remove) {
    if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
    return
}
$resolvedExe = (Resolve-Path -LiteralPath $FollowerExe).Path
$action = New-ScheduledTaskAction -Execute $resolvedExe -Argument $Arguments -WorkingDirectory (Split-Path -Parent $resolvedExe)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userSid
$principal = New-ScheduledTaskPrincipal -UserId $userSid -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
if ($TaskName -like 'CodexQuotaViewer-Test-*') {
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings `
        -Description '额度查看器隔离启停验证；无自动触发器' -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description '独立于 Codex 进程组，随 Codex 桌面启动及退出额度查看器' -Force | Out-Null
}
New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
[ordered]@{ enabled=$true; task_name=$TaskName; user_sid=$userSid; executable=$resolvedExe; arguments=$Arguments } |
    ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
if (-not $NoLaunch) { Start-ScheduledTask -TaskName $TaskName }
Write-Output "已登记独立联动任务：$TaskName"
