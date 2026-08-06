$ErrorActionPreference = "Stop"

$taskName = "TaiwanCompany-StartBackend"
$wslArguments = '-d Ubuntu -u jacktsai --exec /bin/bash -lc "systemctl --user start taiwan-company.service"'

$action = New-ScheduledTaskAction `
    -Execute "$env:WINDIR\System32\wsl.exe" `
    -Argument $wslArguments

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT10S"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName $taskName `
    -Description "Start Taiwan Company backend in WSL2 at Windows logon" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force | Out-Null

Write-Output "Scheduled task '$taskName' installed for Windows user '$env:USERNAME'."
