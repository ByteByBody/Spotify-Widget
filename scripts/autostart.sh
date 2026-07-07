#!/bin/bash
# autostart.sh — Music Mode (fixed for Linux Mint 22)

LOGFILE="$HOME/music-mode/widget.log"
ARTLOG="$HOME/music-mode/art.log"
mkdir -p "$HOME/music-mode"

exec >> "$LOGFILE" 2>&1
echo "=== autostart $(date) ==="

# ── 1. Display ────────────────────────────────────────────────────────────────
export DISPLAY="${DISPLAY:-:0}"
echo "Display: $DISPLAY"

# ── 2. D-Bus session bus ──────────────────────────────────────────────────────
BUS_PATH="/run/user/$(id -u)/bus"
for i in $(seq 1 30); do
    if [ -S "$BUS_PATH" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS_PATH"
        echo "D-Bus ready (attempt $i)"
        break
    fi
    sleep 1
done

if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    echo "ERROR: D-Bus not found — aborting"
    exit 1
fi

# ── 3. Wait for Cinnamon process only — NO gsettings in the loop ─────────────
# CRITICAL FIX: gsettings can hang indefinitely if the session bus schema
# isn't loaded yet. This was causing the script to freeze here and never
# reach the python3 launch. Use pgrep only, with a timeout guard on gsettings.
for i in $(seq 1 30); do
    pgrep -x cinnamon > /dev/null && { echo "Cinnamon up (attempt $i)"; break; }
    sleep 1
done
sleep 2
echo "Proceeding"

# ── 4. Compositor setup ───────────────────────────────────────────────────────
if [ -n "$HYPRLAND_INSTANCE_SIGNATURE" ]; then
    # Hyprland manages its own compositor; nothing to toggle.
    echo "Hyprland detected — skipping gsettings compositing"
    # Pre-start swww-daemon so wallpaper changes are instant later.
    if command -v swww-daemon &>/dev/null; then
        if ! swww query &>/dev/null 2>&1; then
            setsid nohup swww-daemon >> "$ARTLOG" 2>&1 &
            echo "swww-daemon launched (pid $!)"
            sleep 1
        else
            echo "swww-daemon already running"
        fi
    fi
else
    timeout 3 gsettings set org.cinnamon.muffin compositing-manager true 2>/dev/null \
        && echo "compositing enabled" || echo "compositing: skipped"
fi

# ── 5. Kill stale instances ───────────────────────────────────────────────────
pkill -f "python3.*widget\.py" 2>/dev/null && echo "killed old widget.py" || true
pkill -f "bash.*art\.sh"       2>/dev/null && echo "killed old art.sh"    || true
sleep 1

# ── 6. Export vars ────────────────────────────────────────────────────────────
export PYTHONUNBUFFERED=1
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# ── 7. Launch ─────────────────────────────────────────────────────────────────
setsid nohup python3 "$HOME/music-mode/widget.py" >> "$LOGFILE" 2>&1 &
WPY_PID=$!
echo "widget.py launched (pid $WPY_PID)"

setsid nohup bash "$HOME/music-mode/scripts/art.sh" >> "$ARTLOG" 2>&1 &
echo "art.sh launched (pid $!)"

sleep 3
kill -0 "$WPY_PID" 2>/dev/null \
    && echo "widget.py alive" \
    || echo "WARNING: widget.py exited early — check log above"

echo "=== autostart done ==="
