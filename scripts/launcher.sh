#!/bin/bash
# launcher.sh — called by ~/.config/autostart/music-mode.desktop

LOGFILE="$HOME/music-mode/widget.log"
mkdir -p "$HOME/music-mode"
exec >> "$LOGFILE" 2>&1
echo "=== launcher.sh started $(date) ==="

# Wait for X display
for i in $(seq 1 30); do
    if xdpyinfo -display "${DISPLAY:-:0}" &>/dev/null; then
        echo "X display ready (attempt $i)"
        break
    fi
    sleep 1
done

sleep 5
echo "launcher.sh handing off to autostart.sh"
exec bash "$HOME/music-mode/scripts/autostart.sh"
