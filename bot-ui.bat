@echo off
REM One click: make sure the control page is up, then open it.
REM The key lives in web_key.txt and every request must carry it.
cd /d "%~dp0"
powershell -NoProfile -Command ^
  "$k = (Get-Content web_key.txt).Trim();" ^
  "if (-not (Get-NetTCPConnection -LocalPort 8767 -State Listen -EA SilentlyContinue)) {" ^
  "  Start-Process -WindowStyle Hidden python '-u','web.py','--port','8767','--control';" ^
  "  Start-Sleep -Seconds 2 }" ^
  "Start-Process ('http://127.0.0.1:8767/?key=' + $k)"
