param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

$multipleInstances = switch ([int]$task.Settings.MultipleInstances) {
    0 { 'Parallel' }
    1 { 'Queue' }
    2 { 'IgnoreNew' }
    3 { 'StopExisting' }
    default { "Unknown:$([int]$task.Settings.MultipleInstances)" }
}

$actions = @(
    $task.Actions | ForEach-Object {
        [pscustomobject]@{
            execute = [string]$_.Execute
            arguments = [string]$_.Arguments
            working_directory = [string]$_.WorkingDirectory
        }
    }
)

$triggers = @(
    $task.Triggers | ForEach-Object {
        $type = switch ([string]$_.CimClass.CimSystemProperties.ClassName) {
            'MSFT_TaskLogonTrigger' { 'logon' }
            'MSFT_TaskDailyTrigger' { 'daily' }
            default { [string]$_.CimClass.CimSystemProperties.ClassName }
        }
        $trigger = @{
            type = $type
            enabled = [bool]$_.Enabled
        }
        $repetitionInterval = $_.Repetition.Interval
        $repetitionDuration = $_.Repetition.Duration
        if ($repetitionInterval) {
            $trigger.repetition_interval = [string]$repetitionInterval
        }
        if ($repetitionDuration) {
            $trigger.repetition_duration = [string]$repetitionDuration
        }
        [pscustomobject]$trigger
    }
)

$readback = [pscustomobject]@{
    task_name = [string]$task.TaskName
    enabled = [bool]$task.Settings.Enabled
    actions = $actions
    settings = [pscustomobject]@{
        multiple_instances = [string]$multipleInstances
        run_only_if_network_available = [bool]$task.Settings.RunOnlyIfNetworkAvailable
        start_when_available = [bool]$task.Settings.StartWhenAvailable
        restart_count = $task.Settings.RestartCount
        restart_interval = [string]$task.Settings.RestartInterval
    }
    triggers = $triggers
}

$readback | ConvertTo-Json -Depth 8 -Compress
