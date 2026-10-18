# Start the nb bot for each instance (1 = Shamaa to 20, 2-4 = the new shamans to 10). Skips one already running.
# Several instances start -Gap seconds apart (default 60): bots started together walk the guide's steps in lockstep
# and want the same quest mobs and NPCs at the same moment. restart.ps1 starts one at a time, so it never waits.
# -Rxp: start them in RXP mode (env NB_RXP=1: RestedXP drives the route, nb/rxp_follow.py). -NoRxp: env NB_RXP=0 (the
# old guide driver even when an RXP_MODE file says on). Neither: an NB_RXP already set in this shell, else the bot reads
# nb/instN/RXP_MODE, then nb/RXP_MODE (restart.ps1 passes no switch: put the mode in a file for restarts).
param([int[]]$Insts = @(1, 2, 3, 4), [int]$Gap = 60, [switch]$Rxp, [switch]$NoRxp)
$py = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
$root = "C:\Users\MadsG\Desktop\wowbot"
$running = Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'nb\.bot' } | % { $_.CommandLine }
$started = 0
# Start-Process hands this shell's environment to the bot: set NB_RXP for the starts only, then put it back
$oldRxp = $env:NB_RXP
if ($Rxp) { $env:NB_RXP = "1" } elseif ($NoRxp) { $env:NB_RXP = "0" }
try {
    foreach ($i in $Insts) {
        if ($running | ? { $_ -match "--inst $i(\s|$)" }) { "instance $i already running"; continue }
        if ($started -gt 0 -and $Gap -gt 0) {
            "waiting $Gap s before instance $i"
            Start-Sleep $Gap
        }
        $goal = if ($i -eq 1) { 20 } else { 10 }
        $dir = if ($i -eq 1) { "$root\nb" } else { "$root\nb\inst$i" }
        New-Item -ItemType Directory -Force $dir | Out-Null
        Start-Process -FilePath $py -ArgumentList '-u', '-m', 'nb.bot', "$goal", '--inst', "$i" -WorkingDirectory $root `
            -WindowStyle Hidden -RedirectStandardOutput "$dir\run.out" -RedirectStandardError "$dir\run.err"
        $mode = if ($Rxp) { ", RXP mode" } elseif ($NoRxp) { ", old guide driver" } else { "" }
        "started instance $i (goal $goal$mode)"
        $started++
    }
} finally {
    if ($null -eq $oldRxp) { Remove-Item Env:NB_RXP -ErrorAction SilentlyContinue } else { $env:NB_RXP = $oldRxp }
}
