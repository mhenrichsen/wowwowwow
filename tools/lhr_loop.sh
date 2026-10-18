#!/bin/bash
# keep a localhost.run tunnel to the monitor up; the current URL is always in lhr_url.txt
D=/c/Users/MadsG/Desktop/wowbot/tools
while true; do
  ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
      -R 80:127.0.0.1:8765 nokey@localhost.run 2>&1 | while IFS= read -r line; do
        u=$(echo "$line" | grep -ao "https://[a-z0-9]*\.lhr\.life" | head -1)
        [ -n "$u" ] && { echo "$u" > $D/lhr_url.txt; echo "$(date +%H:%M:%S) $u" >> $D/lhr_urls.log; }
      done
  sleep 3
done
