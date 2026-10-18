# Restart the nb bot of the given instances, each only when its character is out of combat (waits up to 60 s).
# -Rxp / -NoRxp are passed on to start_all.ps1 (RXP mode on / off for the restarted bot; neither: the RXP_MODE files)
param([int[]]$Insts = @(1, 2, 3, 4), [switch]$Rxp, [switch]$NoRxp)
$py = "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
$root = "C:\Users\MadsG\Desktop\wowbot"
foreach ($i in $Insts) {
    $ok = $false
    for ($t = 0; $t -lt 30; $t++) {
        $c = & $py -c "import sys; sys.path.insert(0, r'$root'); from nb.eye import Eye; from nb.multi import find_instance_window; s=Eye(find_instance_window($i)).read(); print(1 if (s is None or s.combat or s.dead) else 0)"
        if ($c -eq '0') { $ok = $true; break }
        Start-Sleep 2
    }
    if (-not $ok) { "instance ${i}: still in combat, not restarted"; continue }
    Get-CimInstance Win32_Process | ? { $_.CommandLine -match "nb\.bot \d+ --inst $i(\s|$)" } | % { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 1
    $extra = @(); if ($Rxp) { $extra += '-Rxp' }; if ($NoRxp) { $extra += '-NoRxp' }
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$root\nb\start_all.ps1" -Insts $i @extra
}
