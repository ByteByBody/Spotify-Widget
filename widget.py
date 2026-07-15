#!/usr/bin/env python3
"""
Music Mode Widget v20
Fixed:
  1. _dragging_ref crash, double-fork daemonize, inverted state lock
  2. Repeat mode flipping on track skip — lock held for 3 ticks after
     next/prev so playerctl lag during transition can't overwrite state

Added:
  3. Smooth progress interpolation, volume scroll, shuffle/repeat toggles
  4. Fade-in/out animation, HSV color extraction, screen resolution detect
  5. Song title marquee scroll for long titles
  6. Album art crossfade when track changes
  7. Single-click seek on progress bar (no drag required)
  8. Keyboard shortcuts: Space, Left/Right, Up/Down (active while hovered)
"""

import gi
gi.require_version("Gtk",        "3.0")
gi.require_version("Gdk",        "3.0")
gi.require_version("GdkPixbuf",  "2.0")
gi.require_version("Pango",      "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango, PangoCairo

import subprocess, os, sys, threading, time, math, tempfile, cairo, shutil, urllib.parse, re

# ── Logging ───────────────────────────────────────────────────────────────────
HOME    = os.path.expanduser("~")
LOGFILE = os.path.join(HOME, "music-mode", "widget.log")
os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Config ────────────────────────────────────────────────────────────────────
import json
CONFIG_FILE = os.path.join(HOME, ".config", "music-mode", "config.json")

def load_config():
    defaults = {
        "blur_wallpaper": True,
        "overlay_track_info": False,
        "player": "spotify,%any",
        "widget_scale": 1.0
    }
    try:
        with open(CONFIG_FILE, "r") as f:
            user_conf = json.load(f)
            defaults.update(user_conf)
    except:
        pass
    return defaults

CONFIG = load_config()
PLAYER_ARG = f"--player={CONFIG.get('player', 'spotify,%any')}"

# ── Constants ─────────────────────────────────────────────────────────────────
CACHE  = os.path.join(HOME, "music-mode", "cache")
os.makedirs(CACHE, exist_ok=True)

COVER_FILE   = os.path.join(CACHE, "current_cover.txt")
ACCENT_FILE    = os.path.join(CACHE, "color_accent.txt")
VIBRANT_FILE   = os.path.join(CACHE, "color_vibrant.txt")
OUTLINE_FILE   = os.path.join(CACHE, "color_outline.txt")
BRIGHT_FILE    = os.path.join(CACHE, "color_brightness.txt")
BG_CSS_FILE    = os.path.join(HOME, ".config", "waybar", "wallust", "colors-bg.css")
WALLPAPER    = os.path.join(CACHE, "wallpaper_blur.jpg")
COVER_RAW    = os.path.join(CACHE, "cover_raw.jpg")

BARS = 16
W, H = 360, 672   # extra 32px for volume OSD below cava

IMG_W   = 270
IMG_X   = (W - IMG_W) // 2
IMG_Y   = 80
IMG_BOT = IMG_Y + IMG_W   # 350

SONG_Y    = 14
ARTIST_Y  = IMG_BOT + 14
EXTRA_Y   = IMG_BOT + 36
PROG_PAD  = 28
PROG_Y    = IMG_BOT + 68
PROG_W    = W - PROG_PAD*2
CTRL_Y    = PROG_Y + 54
CTRL_PREV = W//2 - 70
CTRL_PLAY = W//2
CTRL_NEXT = W//2 + 70
CAVA_TOP  = CTRL_Y + 50
CAVA_BOT  = H - 36           # leave 36px below cava for volume OSD
CAVA_MAXH = CAVA_BOT - CAVA_TOP
VOL_Y     = CAVA_BOT + 8     # volume bar top edge

SHUF_X   = PROG_PAD + 14
REP_X    = W - PROG_PAD - 14
EXTRA_HR = 12

SONG_CLIP_X = PROG_PAD
SONG_CLIP_W = W - PROG_PAD * 2


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd, timeout=2.0):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode().strip()
    except:
        return ""

def read_file(path, default=""):
    try:
        with open(path) as f: return f.read().strip()
    except: return default

def write_file(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: f.write(text)
    os.replace(tmp, path)

def hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 6:
        return tuple(int(h[i:i+2], 16)/255 for i in (0,2,4))
    return (1.0, 1.0, 1.0)

def extract_cover_bg(src):
    """Perceptually-weighted linear-RGB average of src, darkened for backgrounds.
    Returns hex color string like '#554433'.
    """
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB").resize((100, 100))
        pixels = list(img.getdata())

        def to_linear(c):
            c = c / 255.0
            return c ** 2.2 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.2

        def to_srgb(c):
            c = max(0.0, min(1.0, c))
            return int((12.92 * c if c <= 0.0031308 else 1.055 * c ** (1/2.4) - 0.055) * 255 + 0.5)

        def perceive(r, g, b):
            return r * 0.299 + g * 0.587 + b * 0.114

        r_sum = g_sum = b_sum = 0.0
        count = 0
        for pr, pg, pb in pixels:
            br = perceive(pr, pg, pb)
            if br < 15 or br > 240:
                continue
            r_sum += to_linear(pr)
            g_sum += to_linear(pg)
            b_sum += to_linear(pb)
            count += 1

        if count == 0:
            return "#2a2a30"

        r_ = to_srgb(r_sum / count)
        g_ = to_srgb(g_sum / count)
        b_ = to_srgb(b_sum / count)

        # Darken to 50% for background use
        r = int(r_ * 0.5)
        g = int(g_ * 0.5)
        b = int(b_ * 0.5)

        # Enforce max brightness of 85 so white text is always readable
        bri = r * 0.299 + g * 0.587 + b * 0.114
        if bri > 85:
            scale = 85.0 / bri
            r = min(255, int(r * scale))
            g = min(255, int(g * scale))
            b = min(255, int(b * scale))

        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#2a2a30"



def fmt_time(s):
    try:
        s = max(0, int(s))
        return f"{s//60:02d}:{s%60:02d}"
    except: return "00:00"


def extract_accent_from_cover(src):
    """Find the true dominant palette of the cover using K-Means (quantize),
    then pick the best colorful shade. Gently clamps lightness for readability.
    Returns clean accent + vibrant colors that respect the album's mood.
    """
    try:
        from PIL import Image
        import colorsys
        img = Image.open(src).convert("RGB")
        img.thumbnail((150, 150), Image.Resampling.LANCZOS)
        
        # Extract a 10-color palette
        q_img = img.quantize(colors=10, method=2)
        colors = q_img.convert("RGB").getcolors(150 * 150)
        
        # Sort by frequency
        colors.sort(key=lambda x: x[0], reverse=True)
        
        best_color = None
        best_score = -1
        total_pixels = sum(c[0] for c in colors)
        
        for count, (r, g, b) in colors:
            rn, gn, bn = r / 255.0, g / 255.0, b / 255.0
            h, l, s = colorsys.rgb_to_hls(rn, gn, bn)
            
            freq_score = count / total_pixels
            
            # Penalize pure blacks, pure whites, and muddy grays heavily
            if l < 0.15 or l > 0.85 or s < 0.15:
                score = freq_score * 0.1
            else:
                # Balance frequency with saturation to find a prominent accent
                score = (freq_score * 0.4) + (s * 0.6)
                
            if score > best_score:
                best_score = score
                best_color = (h, l, s)
                
        if not best_color:
            return "#5588cc", "#77aaff"
            
        h, l, s = best_color
        
        # Adaptive clamping: keep the color's natural mood but make it UI-friendly
        l_accent = max(0.45, min(0.70, l))
        s_accent = max(0.25, min(0.85, s))
        
        l_vibrant = max(0.55, min(0.80, l + 0.10))
        s_vibrant = max(0.40, min(0.95, s + 0.15))
        
        r1, g1, b1 = colorsys.hls_to_rgb(h, l_accent, s_accent)
        accent = f"#{int(r1*255+0.5):02x}{int(g1*255+0.5):02x}{int(b1*255+0.5):02x}"

        r2, g2, b2 = colorsys.hls_to_rgb(h, l_vibrant, s_vibrant)
        vibrant = f"#{int(r2*255+0.5):02x}{int(g2*255+0.5):02x}{int(b2*255+0.5):02x}"
        
        return accent, vibrant
    except Exception as e:
        return "#5588cc", "#77aaff"


# ── Screen resolution ─────────────────────────────────────────────────────────

def get_screen_resolution():
    """Return (width, height) of the primary monitor.

    Strategy:
      1. hyprctl monitors -j  — Hyprland/Wayland (reads width/height fields and
         applies the transform/scale so we get logical pixels, not physical ones)
      2. xrandr --current     — X11 / Cinnamon fallback
      3. Hard default 1920×1080
    """
    # ── 1. Hyprland ──────────────────────────────────────────────────────────
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            import json
            out = subprocess.check_output(
                ["hyprctl", "monitors", "-j"],
                stderr=subprocess.DEVNULL, timeout=2).decode()
            monitors = json.loads(out)
            if monitors:
                m = monitors[0]
                # Hyprland reports physical pixels; divide by scale for logical px
                scale = float(m.get("scale", 1.0)) or 1.0
                # width/height in the JSON are already physical; transform may
                # swap axes on rotated displays — use the "transform" field.
                transform = int(m.get("transform", 0))
                pw, ph = int(m["width"]), int(m["height"])
                if transform in (1, 3, 5, 7):   # 90° / 270° rotations swap axes
                    pw, ph = ph, pw
                lw, lh = int(pw / scale), int(ph / scale)
                log(f"hyprctl resolution: {lw}x{lh} (scale={scale})")
                return lw, lh
        except Exception as e:
            log(f"get_screen_resolution hyprctl error: {e}")

    # ── 2. xrandr (X11) ───────────────────────────────────────────────────────
    try:
        out = subprocess.check_output(
            ["xrandr", "--current"], stderr=subprocess.DEVNULL, timeout=2).decode()
        import re
        for line in out.splitlines():
            if " connected" in line:
                m = re.search(r"(\d+)x(\d+)\+", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception as e:
        log(f"get_screen_resolution xrandr error: {e}")

    # ── 3. Safe default ───────────────────────────────────────────────────────
    return 1920, 1080


# ── Wallpaper ─────────────────────────────────────────────────────────────────

def _ensure_swww_daemon():
    """Start swww-daemon if it isn't running yet.  Safe to call repeatedly."""
    try:
        r = subprocess.run(["swww", "query"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
        if r.returncode == 0:
            return True          # already running
        subprocess.Popen(["swww-daemon"],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(0.8)         # give it a moment to bind the socket
        return True
    except Exception as e:
        log(f"swww daemon start error: {e}")
        return False

def make_and_set_wallpaper(src):
    """Generate wallpaper from *src* according to CONFIG and push it to the
    compositor.  Tries swww (Hyprland/Wayland) first; falls back to gsettings.

    Safe to call from a background thread.
    """
    TW, TH = get_screen_resolution()
    try:
        from PIL import Image, ImageFilter, ImageDraw, ImageFont
        img = Image.open(src).convert("RGB")
        iw, ih = img.size
        
        # Read config values with defaults
        blur_mode = CONFIG.get("blur_wallpaper", True)
        overlay_info = CONFIG.get("overlay_track_info", False)

        if blur_mode:
            scale  = max(TW / iw, TH / ih)
            nw, nh = int(iw * scale) + 1, int(ih * scale) + 1
            bg   = img.resize((nw, nh), Image.LANCZOS)
            bg   = bg.crop(((nw - TW) // 2, (nh - TH) // 2,
                              (nw - TW) // 2 + TW, (nh - TH) // 2 + TH))
            bg   = bg.filter(ImageFilter.GaussianBlur(radius=16))
            bg   = bg.point(lambda p: int(p * 0.70))
        else:
            # Crisp mode: heavily blurred background + crisp cover in center
            scale  = max(TW / iw, TH / ih)
            nw, nh = int(iw * scale) + 1, int(ih * scale) + 1
            bg   = img.resize((nw, nh), Image.LANCZOS)
            bg   = bg.crop(((nw - TW) // 2, (nh - TH) // 2,
                              (nw - TW) // 2 + TW, (nh - TH) // 2 + TH))
            bg   = bg.filter(ImageFilter.GaussianBlur(radius=32))
            bg   = bg.point(lambda p: int(p * 0.50)) # darker
            
            # Resize cover to be reasonable size (e.g. max 50% of screen height)
            cover_size = int(TH * 0.5)
            # Ensure it fits width-wise too (unlikely to be an issue, but safe)
            cover_size = min(cover_size, int(TW * 0.5))
            cover = img.resize((cover_size, cover_size), Image.LANCZOS)
            
            y_offset = (TH - cover_size) // 2
            if overlay_info:
                y_offset -= 80 # shift up to make room for text
            
            # Draw subtle drop shadow
            shadow = Image.new("RGBA", (cover_size, cover_size), (0,0,0, 100))
            bg.paste(shadow, ((TW - cover_size) // 2 + 10, y_offset + 10), shadow)
            # Paste cover
            bg.paste(cover, ((TW - cover_size) // 2, y_offset))

        if overlay_info:
            title = (run(f"playerctl {PLAYER_ARG} metadata xesam:title") or "Unknown Title").strip()
            artist = (run(f"playerctl {PLAYER_ARG} metadata xesam:artist") or "Unknown Artist").strip()
            
            draw = ImageDraw.Draw(bg)
            
            # Attempt to load fonts
            font_title, font_artist = None, None
            font_paths = [
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
            ]
            for path in font_paths:
                try:
                    font_title = ImageFont.truetype(path, 64)
                    break
                except: pass
                
            artist_paths = [
                "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf"
            ]
            for path in artist_paths:
                try:
                    font_artist = ImageFont.truetype(path, 48)
                    break
                except: pass
                
            if not font_title: font_title = ImageFont.load_default()
            if not font_artist: font_artist = ImageFont.load_default()
            
            if not blur_mode:
                text_y = y_offset + cover_size + 40
            else:
                text_y = TH // 2 + 100
                
            try:
                tw = draw.textlength(title, font=font_title)
                aw = draw.textlength(artist, font=font_artist)
            except:
                tw, aw = 400, 300 # Fallback sizes for load_default
                
            # Outline/shadow for text for readability
            for ox, oy in [(-2,-2),(-2,2),(2,-2),(2,2)]:
                draw.text(((TW - tw) // 2 + ox, text_y + oy), title, font=font_title, fill="black")
                draw.text(((TW - aw) // 2 + ox, text_y + 80 + oy), artist, font=font_artist, fill="black")
                
            draw.text(((TW - tw) // 2, text_y), title, font=font_title, fill="white")
            draw.text(((TW - aw) // 2, text_y + 80), artist, font=font_artist, fill="#dddddd")

        bg.save(WALLPAPER, "JPEG", quality=88)
    except ImportError:
        subprocess.run(["convert", src,
                        "-resize", f"{TW}x{TH}^", "-gravity", "center",
                        "-extent", f"{TW}x{TH}", "-blur", "0x10",
                        "-modulate", "70", WALLPAPER],
                       stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"wallpaper render error: {e}"); return

    uri = f"file://{WALLPAPER}"

    # ── 1. swww (Wayland / Hyprland) ─────────────────────────────────────────
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or \
       os.environ.get("WAYLAND_DISPLAY"):
        if _ensure_swww_daemon():
            try:
                r = subprocess.run(
                    ["swww", "img", WALLPAPER,
                     "--transition-type", "fade",
                     "--transition-duration", "1.5"],
                    stderr=subprocess.PIPE, timeout=6)
                if r.returncode == 0:
                    log(f"wallpaper set via swww: {WALLPAPER}")
                    # Trigger system color sync via JaKooLit's script
                    sync_script = os.path.expanduser("~/.config/hypr/scripts/WallustSwww.sh")
                    if os.path.exists(sync_script):
                        # Pass COVER_RAW instead of WALLPAPER because heavily blurred
                        # images cause wallust to fail with "Not enough colors!"
                        subprocess.run([sync_script, COVER_RAW], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        # Extract clean accent from cover via HSL (hue from image,
                        # fixed saturation/lightness for consistent quality)
                        accent_hex, vibrant_hex = extract_accent_from_cover(COVER_RAW)
                        write_file(ACCENT_FILE, accent_hex)
                        write_file(VIBRANT_FILE, vibrant_hex)
                        ri = int(accent_hex[1:3], 16); gi = int(accent_hex[3:5], 16); bi = int(accent_hex[5:7], 16)
                        bri = (ri*299+gi*587+bi*114)//1000
                        write_file(OUTLINE_FILE, "#000000" if bri>145 else "#ffffff")
                        write_file(BRIGHT_FILE, str(bri))
                        log(f"accent: {accent_hex} bri={bri} (vibrant: {vibrant_hex})")
                    # Full waybar restart to refresh window/workspace tracking.
                    # SIGUSR2 (used by WallustSwww.sh) doesn't always reload
                    # the hyprland/window and hyprland/workspaces modules.
                    subprocess.run(
                        ["bash", "-c", "pkill -f 'playerctl -a metadata' 2>/dev/null; killall waybar 2>/dev/null; nohup waybar >/dev/null 2>&1 & disown"],
                        stderr=subprocess.DEVNULL, timeout=5)
                    return
                else:
                    log(f"swww non-zero exit: {r.stderr.decode().strip()}")
            except Exception as e:
                log(f"swww error: {e}")

    # ── 2. gsettings (Cinnamon / X11 fallback) ───────────────────────────────
    try:
        subprocess.run(["gsettings", "set",
                        "org.cinnamon.desktop.background",
                        "picture-uri", uri],
                       stderr=subprocess.DEVNULL, timeout=3)
        subprocess.run(["gsettings", "set",
                        "org.cinnamon.desktop.background",
                        "picture-options", "zoom"],
                       stderr=subprocess.DEVNULL, timeout=3)
        log(f"wallpaper set via gsettings: {uri}")
    except Exception as e:
        log(f"gsettings error: {e}")


# ── Art processor ─────────────────────────────────────────────────────────────

class ArtProcessor(threading.Thread):
    def __init__(self, on_update):
        super().__init__(daemon=True, name="art")
        self.on_update = on_update
        self._last_url = ""
        self._last_accent_mtime = 0

    def run(self):
        log("ArtProcessor started")
        while True:
            try: self._check()
            except Exception as e: log(f"art error: {e}")
            time.sleep(1)

    def _reload_accent(self):
        """Re-read accent/outline/brightness files and schedule redraw."""
        GLib.idle_add(self.on_update)

    def _check(self):
        status = run(f"playerctl {PLAYER_ARG} status")
        if status not in ("Playing","Paused"): return

        # Poll for wallust accent updates even when cover hasn't changed
        try:
            mt = max(os.path.getmtime(ACCENT_FILE), os.path.getmtime(VIBRANT_FILE))
            if mt > self._last_accent_mtime:
                self._last_accent_mtime = mt
                self._reload_accent()
        except Exception:
            pass

        # Check track ID to see if song changed even if art is missing
        track_id = run(f"playerctl {PLAYER_ARG} metadata mpris:trackid")
        raw_url = run(f"playerctl {PLAYER_ARG} metadata mpris:artUrl")
        
        track_changed = False
        if not hasattr(self, '_last_track_id'):
            track_changed = True
        elif self._last_track_id != track_id:
            track_changed = True
        self._last_track_id = track_id
        
        # If no art URL is provided (common for local Spotify files), try to
        # extract embedded cover art from the actual music file.
        LOCAL_DIR = os.path.join(HOME, "Music", "Spotify Local")
        if not raw_url:
            clean = "fallback:" + track_id
            if clean == self._last_url:
                if track_changed and CONFIG.get("overlay_track_info", False):
                    # Redraw wallpaper for new track info
                    raw_copy = COVER_RAW+".wp.jpg"
                    if os.path.exists(COVER_RAW):
                        import shutil; shutil.copy2(COVER_RAW, raw_copy)
                        import threading; threading.Thread(target=make_and_set_wallpaper, args=(raw_copy,), daemon=True, name="wp").start()
                return
            self._last_url = clean
            log(f"using fallback art for: {track_id}")

            r_code = 1
            # Try to extract embedded cover from matching local file
            try:
                title  = (run(f"playerctl {PLAYER_ARG} metadata xesam:title") or "").strip()
                artist = (run(f"playerctl {PLAYER_ARG} metadata xesam:artist") or "").strip()
                if title and artist and os.path.isdir(LOCAL_DIR):
                    best_match = None
                    best_score = 0
                    title_lower = title.lower()
                    # Build set of significant words from title (≥3 chars, not common words)
                    skip_words = {"the","and","remix","feat","ft","vs","the","a","an","of","in","on","for","with","from"}
                    title_words = {w for w in title_lower.replace("(","").replace(")","").split()
                                   if len(w) >= 3 and w not in skip_words}
                    for fname in os.listdir(LOCAL_DIR):
                        fn_lower = fname.lower()
                        # Score: exact title substring gives 10 bonus, each word match gives 1
                        score = 0
                        if title_lower in fn_lower:
                            score += 10
                        if artist.lower() in fn_lower:
                            score += 5
                        for w in title_words:
                            if w in fn_lower:
                                score += 1
                        if score > best_score:
                            best_score = score
                            best_match = fname
                    if best_match and best_score >= 1:
                        fpath = os.path.join(LOCAL_DIR, best_match)
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", fpath, "-an", "-vcodec", "copy", COVER_RAW],
                            stderr=subprocess.DEVNULL, timeout=10)
                        if os.path.exists(COVER_RAW) and os.path.getsize(COVER_RAW) > 100:
                            r_code = 0
                            log(f"extracted cover from: {best_match}")
            except Exception as e:
                log(f"cover extraction error: {e}")

            if r_code != 0:
                # Create a default cover image as fallback
                fallback_img = os.path.join(CACHE, "default_cover.jpg")
                if not os.path.exists(fallback_img):
                    subprocess.run(["convert", "-size", f"{IMG_W}x{IMG_W}",
                                    "gradient:#2e3440-#4c566a", fallback_img],
                                   stderr=subprocess.DEVNULL)
                shutil.copy2(fallback_img, COVER_RAW)
            
        else:
            # Only accept http(s) and file URLs.
            if not (raw_url.startswith("http://") or 
                    raw_url.startswith("https://") or 
                    raw_url.startswith("file://")):
                # If Spotify returns spotify:image:... convert to scdn URL
                if raw_url.startswith("spotify:image:"):
                    raw_url = "https://i.scdn.co/image/" + raw_url.split(":")[-1]
                else:
                    return
                    
            clean = raw_url.split("?")[0]
            if clean == self._last_url:
                if track_changed and CONFIG.get("overlay_track_info", False):
                    # Redraw wallpaper for new track info
                    raw_copy = COVER_RAW+".wp.jpg"
                    if os.path.exists(COVER_RAW):
                        import shutil; shutil.copy2(COVER_RAW, raw_copy)
                        import threading; threading.Thread(target=make_and_set_wallpaper, args=(raw_copy,), daemon=True, name="wp").start()
                return
            self._last_url = clean
            log(f"new art URL: {clean}")
            
            if clean.startswith("file://"):
                local_path = urllib.parse.unquote(clean[7:])
                try:
                    shutil.copy2(local_path, COVER_RAW)
                    r_code = 0
                except Exception as e:
                    log(f"file copy failed: {e}")
                    r_code = 1
            else:
                r = subprocess.run(["curl","-sL",clean,"-o",COVER_RAW],
                                   stderr=subprocess.DEVNULL)
                r_code = r.returncode
                
        if r_code != 0 or not os.path.exists(COVER_RAW):
            log("cover download failed"); return

        ts    = int(time.time())
        cover = os.path.join(CACHE, f"cover_{ts}.jpg")
        for f in os.listdir(CACHE):
            if f.startswith("cover_") and f[6:-4].isdigit():
                try: os.remove(os.path.join(CACHE, f))
                except: pass
        try:
            from PIL import Image
            try:
                Image.open(COVER_RAW).convert("RGB").resize(
                    (IMG_W, IMG_W), Image.BILINEAR).save(cover, "JPEG", quality=88)
            except Exception:
                # If PIL fails, it might be an audio file with embedded cover.
                # Try extracting with ffmpeg.
                tmp_cover = COVER_RAW + ".ext.jpg"
                subprocess.run(["ffmpeg", "-y", "-i", COVER_RAW, "-an", "-vcodec", "copy", tmp_cover],
                               stderr=subprocess.DEVNULL)
                if os.path.exists(tmp_cover):
                    Image.open(tmp_cover).convert("RGB").resize(
                        (IMG_W, IMG_W), Image.BILINEAR).save(cover, "JPEG", quality=88)
                    os.remove(tmp_cover)
                else:
                    raise
        except ImportError:
            subprocess.run(["convert",COVER_RAW,
                            "-resize",f"{IMG_W}x{IMG_W}",cover],
                           stderr=subprocess.DEVNULL)
        
        if not os.path.exists(cover):
            log("failed to process cover")
            return
            
        write_file(COVER_FILE, cover)
        log(f"cover ready: {cover}")

        # Extract cover's overall mood for bar background
        bg_col = extract_cover_bg(COVER_RAW)
        write_file(BG_CSS_FILE, f"@define-color cover-bg {bg_col};\n")
        log(f"cover bg: {bg_col}")

        GLib.idle_add(self.on_update)

        raw_copy = COVER_RAW+".wp.jpg"
        shutil.copy2(COVER_RAW, raw_copy)
        threading.Thread(target=make_and_set_wallpaper,
                         args=(raw_copy,), daemon=True, name="wp").start()


# ── Cava ──────────────────────────────────────────────────────────────────────

class CavaReader(threading.Thread):
    """
    Reads cava audio-visualizer output in a background thread.

    - Buffered line reading (one syscall per frame, not per byte)
    - Thread-safe value updates (atomic list replacement under GIL)
    - Clean subprocess lifecycle (kill + wait/reap on restart/shutdown)
    - Stale process cleanup on startup (survives previous widget crashes)
    - Exponential backoff on repeated cava failures
    - Automatically kills cava when Spotify is not playing to save CPU
    - Detects fullscreen windows and lowers cava activity accordingly
    """
    def __init__(self, sp, n=BARS):
        super().__init__(daemon=True, name="cava")
        self.sp     = sp
        self.n      = n
        self.values = [0.0] * n
        self._peak  = [0.0] * n
        self._lock  = threading.Lock()
        self._proc  = None
        self._stop  = threading.Event()
        self._desktop_visible = True
        self._last_visible_check = 0.0
        self._last_playing_state = False
        self._kill_stale()
        self._cfg   = self._write_cfg()
        self.start()

    def _kill_stale(self):
        """Kill ALL leftover cava processes so only we run one."""
        try:
            subprocess.run(
                ["pkill", "-9", "cava"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3)
            time.sleep(0.3)
        except Exception:
            pass

    def _write_cfg(self, framerate=60, sensitivity=200):
        cfg_path = os.path.join(CACHE, "cava.cfg")
        with open(cfg_path, "w") as f:
            f.write("[general]\n")
            f.write(f"bars = {self.n}\n")
            f.write(f"framerate = {framerate}\n")
            f.write("sleep_timer = 0\n")
            f.write("noise_reduction = 0.15\n")
            f.write(f"sensitivity = {sensitivity}\n")
            f.write("monstercat = 1\n")
            f.write("autosens = 0\n")
            f.write("low_cut = 50\n")
            f.write("\n[input]\n")
            f.write("method = pulse\n")
            f.write("source = auto\n")
            f.write("\n[output]\n")
            f.write("method = raw\n")
            f.write("raw_target = /dev/stdout\n")
            f.write("data_format = ascii\n")
            f.write("ascii_max_range = 1000\n")
            f.write("bar_delimiter = 59\n")
        return cfg_path

    def _check_desktop_visible(self):
        """Check if desktop is actually visible (no fullscreen window covering it)."""
        try:
            import json
            out = subprocess.check_output(
                ["hyprctl", "activewindow", "-j"],
                stderr=subprocess.DEVNULL, timeout=1)
            data = json.loads(out)
            self._desktop_visible = (data.get("fullscreen", 0) == 0)
        except Exception:
            pass

    def _kill_proc(self):
        """Kill and reap the current cava subprocess (thread-safe)."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    def run(self):
        log("CavaReader started")
        backoff = 2.0
        while not self._stop.is_set():
            # Check desktop visibility every 2 seconds
            now = time.monotonic()
            if now - self._last_visible_check > 2.0:
                self._check_desktop_visible()
                self._last_visible_check = now

            is_playing = (self.sp.status == "Playing")
            state_changed = (is_playing != self._last_playing_state)
            self._last_playing_state = is_playing

            if not is_playing or not self._desktop_visible:
                self._kill_proc()
                self.values = [0.0] * self.n
                self._peak = [0.0] * self.n
                if self._stop.wait(0.5):
                    break
                continue

            try:
                self._kill_proc()
                # Default bufsize (-1) gives a BufferedReader on stdout,
                # so `for line in proc.stdout` reads full lines efficiently
                # (~60 syscalls/sec instead of ~4800 with read(1)).
                proc = subprocess.Popen(
                    ["cava", "-p", self._cfg],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)
                with self._lock:
                    self._proc = proc

                for raw_line in proc.stdout:
                    line = raw_line.decode(errors="ignore").strip().rstrip(";")
                    if not line:
                        continue
                    parts = line.split(";")
                    if len(parts) < self.n:
                        continue
                    try:
                        new = [max(0.0, min(1.0, int(p.strip() or "0") / 1000.0))
                               for p in parts[:self.n]]
                        old_v = self.values
                        old_p = self._peak
                        # Atomic list replacement — safe for the draw thread
                        self.values = [old_v[i] * 0.35 + new[i] * 0.65
                                       for i in range(self.n)]
                        self._peak = [
                            new[i] if new[i] >= old_p[i]
                            else max(0.0, old_p[i] - 0.016)
                            for i in range(self.n)
                        ]
                        backoff = 2.0
                    except Exception:
                        pass

                # stdout EOF — cava exited; capture diagnostics
                rc = proc.poll()
                try:
                    err = proc.stderr.read(4096).decode(errors="ignore").strip()
                except Exception:
                    err = ""
                if rc and rc != 0:
                    log(f"cava exited (rc={rc}){': ' + err[:200] if err else ''}")
                try:
                    proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.stderr.close()
                except Exception:
                    pass

            except FileNotFoundError:
                log("cava: binary not found in PATH")
                backoff = 60.0
            except Exception as e:
                log(f"cava: {e}")

            # Zero out bars so the UI doesn't show stale frozen values
            self.values = [0.0] * self.n
            self._peak  = [0.0] * self.n
            self._kill_proc()

            # Backoff sleep — also serves as the clean shutdown check
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def stop(self):
        self._stop.set()
        self._kill_proc()
        try:
            os.unlink(self._cfg)
        except Exception:
            pass


# ── Window sinker ─────────────────────────────────────────────────────────────

class WindowSinker(threading.Thread):
    """
    Positions and pins the widget window so it never covers other windows.

    Hyprland / Wayland
    ──────────────────
    Uses `hyprctl dispatch` to:
      • setfloating  — make it a floating window
      • pin          — stick it to every workspace
      • movewindowpixel exact 20 820 — bottom-left position
      • focuswindow  is deliberately NOT called so we never steal focus

    Input passthrough (no blocking clicks to windows behind):
      The widget window has accept-focus=False so focus can never be
      stolen passively.  We also run `hyprctl keyword` to mark the
      address as `nofocus` in Hyprland's window rules so Hyprland
      itself won't grant focus to it on map.  Keyboard grab/ungrab
      (done in on_enter/on_leave) still works for shortcuts while
      the cursor is physically inside the widget.

    X11 / Cinnamon fallback
    ───────────────────────
    Uses wmctrl to add below/skip_taskbar/skip_pager hints.
    """

    # How many pixels from the left / bottom edge
    WIN_X = 20
    WIN_Y = 820

    def __init__(self):
        super().__init__(daemon=True, name="sinker")
        self._wid     = None
        self._addr    = None
        self._pinned  = False        # True once all three dispatches succeed
        self._is_hypr = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

    # ── Hyprland ──────────────────────────────────────────────────────────────

    def _find_hypr_address(self):
        """Return hyprctl address of our window, or None."""
        import json
        try:
            out = subprocess.check_output(
                ["hyprctl", "clients", "-j"],
                stderr=subprocess.DEVNULL, timeout=2).decode()
            for c in json.loads(out):
                title = c.get("title", "")
                cls   = c.get("class", "")
                if title == "music-mode" or "widget.py" in cls or cls == "music-mode":
                    return c["address"]
        except Exception as e:
            log(f"hypr find-addr error: {e}")
        return None

    def _hyprctl(self, *args):
        """Run `hyprctl <args>` and return True on success."""
        try:
            r = subprocess.run(["hyprctl"] + list(args),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2)
            return r.returncode == 0
        except Exception as e:
            log(f"hyprctl {args} error: {e}")
            return False

    def _hyprland_sink(self):
        addr = self._find_hypr_address()
        if not addr:
            return False

        if addr != self._addr:
            log(f"hypr-sinker: found window {addr}")
            self._addr   = addr
            self._pinned = False   # force re-apply on new address

        if not self._pinned:
            # Float the window
            self._hyprctl("dispatch", "setfloating", f"address:{addr}")

            # Pin to all workspaces
            self._hyprctl("dispatch", "pin", f"address:{addr}")

            # Position bottom-left (exact pixel coords)
            self._hyprctl("dispatch", "movewindowpixel",
                          f"exact {self.WIN_X} {self.WIN_Y},address:{addr}")

            # Tell Hyprland to never grant focus to this window.
            # setprop is cleaner than windowrulev2 — it's a per-window
            # property that doesn't accumulate global rules.
            self._hyprctl("setprop", f"address:{addr}", "nofocus", "1")

            log(f"hypr-sinker: pinned & positioned {addr}")
            self._pinned = True

        return True

    # ── X11 fallback ──────────────────────────────────────────────────────────

    def _x11_sink(self):
        try:
            out = subprocess.run(["wmctrl", "-l"],
                                 capture_output=True, text=True, timeout=1).stdout
            for line in out.splitlines():
                if "music-mode" in line:
                    wid = line.split()[0]
                    if wid != self._wid:
                        log(f"x11-sinker: found {wid}")
                        self._wid = wid
                    subprocess.run(
                        ["wmctrl", "-i", "-r", wid, "-b",
                         "add,below,skip_taskbar,skip_pager"],
                        stderr=subprocess.DEVNULL, timeout=1)
                    return True
        except Exception as e:
            log(f"x11-sinker error: {e}")
        return False

    # ── Thread loop ───────────────────────────────────────────────────────────

    def run(self):
        time.sleep(3)          # wait for GTK window to map
        stable_count = 0
        while True:
            if self._is_hypr:
                ok = self._hyprland_sink()
            else:
                ok = self._x11_sink()
            if ok:
                stable_count += 1
            # Once pinned and stable for 5 cycles back off to 5 s polling.
            # This catches workspace switches or compositor restarts.
            sleep_t = 5.0 if stable_count >= 5 else 0.8
            time.sleep(sleep_t)


# ── Spotify state ─────────────────────────────────────────────────────────────

class SpotifyTracker:
    def __init__(self):
        self.status  = "Stopped"
        self.song    = ""
        self.artist  = ""
        self.pos     = 0
        self.dur     = 1
        self.shuffle = False
        self.repeat  = "None"   # "None" | "Track" | "Playlist"
        self.volume  = 100
        self._prev_status  = ""
        self._dragging_ref = [False]

        # ── Repeat / shuffle intent tracking ──────────────────────────────────
        # The user's intended repeat/shuffle state. Once set by a user action,
        # refresh() always writes this back to self.repeat / self.shuffle so
        # playerctl lag during track transitions can never overwrite it.
        # Set to None until the first successful playerctl read.
        self._desired_repeat  = None   # None | "None" | "Track" | "Playlist"
        self._desired_shuffle = None   # None | bool
        # Last value playerctl reported — used to detect Spotify-side changes
        # (e.g. user changes repeat in the Spotify app itself).
        self._pc_repeat  = None
        self._pc_shuffle = None

    def set_repeat(self, mode):
        """Called by widget when user clicks repeat toggle."""
        self.repeat          = mode
        self._desired_repeat = mode
        self._pc_repeat      = mode   # treat as confirmed so next read won't flip

    def set_shuffle(self, state):
        """Called by widget when user clicks shuffle toggle."""
        self.shuffle          = state
        self._desired_shuffle = state
        self._pc_shuffle      = state

    def refresh(self):
        # Runs on a background thread — never called from GTK main loop directly.
        new_status = run(f"playerctl {PLAYER_ARG} status") or "Stopped"
        became_active = (new_status in ("Playing","Paused") and
                         self._prev_status not in ("Playing","Paused"))
        self.status       = new_status
        self._prev_status = new_status

        if new_status in ("Playing","Paused"):
            self.song   = run(f"playerctl {PLAYER_ARG} metadata xesam:title")  or ""
            self.artist = run(f"playerctl {PLAYER_ARG} metadata xesam:artist") or ""
            try:
                us = int(run(f"playerctl {PLAYER_ARG} metadata mpris:length") or "0")
                self.dur = max(1, us//1_000_000)
            except:
                self.dur = 1

            # Read repeat from playerctl
            pc_rp = run(f"playerctl {PLAYER_ARG} loop")
            pc_rp = pc_rp if pc_rp in ("None","Track","Playlist") else None

            if pc_rp is not None:
                if self._desired_repeat is None:
                    # First read ever — accept playerctl as truth
                    self._desired_repeat = pc_rp
                    self._pc_repeat      = pc_rp
                elif pc_rp != self._pc_repeat:
                    # playerctl value changed — only accept if it differs from
                    # what we last sent, meaning Spotify itself changed it
                    # (e.g. user used the Spotify app or another client).
                    self._desired_repeat = pc_rp
                    self._pc_repeat      = pc_rp
                # Always enforce desired state regardless of what playerctl said
                self.repeat      = self._desired_repeat
                self._pc_repeat  = pc_rp

            # Same logic for shuffle
            pc_sh_raw = run(f"playerctl {PLAYER_ARG} shuffle")
            if pc_sh_raw in ("On","Off","on","off"):
                pc_sh = pc_sh_raw.lower() == "on"
                if self._desired_shuffle is None:
                    self._desired_shuffle = pc_sh
                    self._pc_shuffle      = pc_sh
                elif pc_sh != self._pc_shuffle:
                    self._desired_shuffle = pc_sh
                    self._pc_shuffle      = pc_sh
                self.shuffle     = self._desired_shuffle
                self._pc_shuffle = pc_sh

            # Volume (no user-intent tracking needed — continuous value)
            try:
                v = run(f"playerctl {PLAYER_ARG} volume")
                self.volume = max(0, min(100, int(float(v)*100)))
            except: pass

        if new_status in ("Playing","Paused") and not self._dragging_ref[0]:
            try:
                self.pos = int(float(run(f"playerctl {PLAYER_ARG} position") or "0"))
            except: pass

        return became_active


# ── Widget ────────────────────────────────────────────────────────────────────

class MusicWidget(Gtk.Window):
    def __init__(self):
        super().__init__(Gtk.WindowType.TOPLEVEL)
        self.set_title("music-mode")
        self.set_default_size(W, H)
        self.set_resizable(False)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_focus_on_map(False)
        self.set_app_paintable(True)

        _on_wayland = (os.environ.get("WAYLAND_DISPLAY") or
                       os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

        if _on_wayland:
            # gtk-layer-shell: stick widget to the BOTTOM layer permanently
            try:
                import gi
                gi.require_version("GtkLayerShell", "0.1")
                from gi.repository import GtkLayerShell
                GtkLayerShell.init_for_window(self)
                GtkLayerShell.set_layer(self, GtkLayerShell.Layer.BOTTOM)
                GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
                GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT,   False)
                GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT,  False)
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, 20)
                GtkLayerShell.set_exclusive_zone(self, -1)
                GtkLayerShell.set_keyboard_mode(
                    self, GtkLayerShell.KeyboardMode.ON_DEMAND)
                log("gtk-layer-shell: BOTTOM layer active")
            except Exception as e:
                log(f"gtk-layer-shell not available: {e} — falling back to sinker")
        else:
            # X11/Cinnamon fallback
            self.set_type_hint(Gdk.WindowTypeHint.DOCK)
            self.set_keep_below(True)
            self.stick()

        # accept_focus=False means the WM/compositor will never passively give
        # this window focus (e.g. on workspace switch or click-through).
        # Keyboard grab in on_enter/on_leave still works for shortcuts.
        self.set_accept_focus(False)

        screen = self.get_screen()
        v = screen.get_rgba_visual()
        if v: self.set_visual(v)

        # Initial placement: centre of screen.  hyprctl will move it to
        # WIN_X/WIN_Y (bottom-left) once the window is mapped.
        sw, sh = screen.get_width(), screen.get_height()
        self.move((sw - W) // 2, (sh - H) // 2)
        log(f"window at ({(sw-W)//2},{(sh-H)//2}) screen {sw}x{sh}")

        # Drawing state
        self.cover_pixbuf     = None   # current cover
        self.cover_pixbuf_old = None   # previous cover (for crossfade)
        self.cover_path       = ""
        self._cover_fade      = 1.0    # 0=old, 1=new
        self.accent           = (0.2, 0.7, 1.0)
        self.vibrant          = (0.2, 0.7, 1.0)
        self.secondary        = (0.6, 0.4, 1.0)
        self.outline          = (0.0, 0.0, 0.0)
        self._static_surface  = None
        self._static_dirty    = True
        self._tick_interval   = 50
        self.text_main        = (1.0, 1.0, 1.0, 0.95)
        self.text_sub         = (1.0, 1.0, 1.0, 0.50)
        self._dragging        = False
        self._drag_frac       = 0.0
        self._hovered         = False
        self._mouse_x         = 0.0
        self._mouse_y         = 0.0

        # Fade animation
        self._fade_alpha  = 0.0
        self._fade_target = 0.0
        self._FADE_STEP   = 0.05

        # Smooth position interpolation
        self._last_tick_time = time.monotonic()

        # Volume OSD
        self._vol_show_until = 0.0

        # Song title marquee
        self._marquee_offset = 0.0
        self._marquee_dir    = 1
        self._marquee_pause  = 0.0
        self._marquee_speed  = 28.0
        self._marquee_song   = ""

        # Idle dim — disabled (always full opacity)
        self._last_interaction = time.monotonic()
        self._IDLE_SECS        = 999999.0
        self._IDLE_ALPHA       = 1.0
        self._dim_alpha        = 1.0

        # Right-click context menu
        self._menu_open   = False
        self._menu_items  = ["Open Spotify", "Copy song info", "Hide widget"]
        self._menu_hover  = -1   # which item is highlighted

        # Progress bar hover preview
        self._prog_hovered = False   # mouse is over the progress bar

        self.sp     = SpotifyTracker()
        self.cava   = CavaReader(self.sp)
        self.art    = ArtProcessor(self._on_art_update)
        self.sinker = WindowSinker()
        self.art.start()
        self.sinker.start()

        # Background poller: runs sp.refresh() off the GTK thread so the
        # 7 playerctl subprocess calls never block drawing or input.
        self._sp_poller = threading.Thread(
            target=self._sp_poll_loop, daemon=True, name="sp-poll")
        self._sp_poller.start()
        log("all workers started")

        self.da = Gtk.DrawingArea()
        self.da.set_size_request(W, H)
        self.da.connect("draw", self.on_draw)
        self.da.set_events(
            Gdk.EventMask.BUTTON_PRESS_MASK   |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.SCROLL_MASK         |
            Gdk.EventMask.ENTER_NOTIFY_MASK   |
            Gdk.EventMask.LEAVE_NOTIFY_MASK   |
            Gdk.EventMask.KEY_PRESS_MASK)
        self.da.connect("button-press-event",   self.on_press)
        self.da.connect("button-release-event", self.on_release)
        self.da.connect("motion-notify-event",  self.on_motion)
        self.da.connect("scroll-event",         self.on_scroll)
        self.da.connect("enter-notify-event",   self.on_enter)
        self.da.connect("leave-notify-event",   self.on_leave)
        self.da.set_can_focus(True)
        self.connect("key-press-event", self.on_key)
        self.add(self.da)
        self.connect("destroy", lambda *_: (self.cava.stop(), Gtk.main_quit()))
        self.connect("window-state-event", self._on_window_state)

        # tick_draw drives all animation with adaptive framerate.
        self._tick_interval = 50
        GLib.timeout_add(50, self.tick_draw)

        self._on_art_update()

    # ── Art / color update ────────────────────────────────────────────────────

    def _on_art_update(self):
        self._static_dirty = True
        path = read_file(COVER_FILE,"").strip()
        if path and path != self.cover_path and os.path.exists(path):
            try:
                new_pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, IMG_W, IMG_W, False)
                # Keep old cover for crossfade
                self.cover_pixbuf_old = self.cover_pixbuf
                self.cover_pixbuf     = new_pb
                self.cover_path       = path
                self._cover_fade      = 0.0   # start crossfade from old
                log(f"cover loaded: {path}")
            except Exception as e:
                log(f"cover load error: {e}")
        self.accent  = hex_to_rgb(read_file(ACCENT_FILE,  "#33aaff"))
        self.vibrant = hex_to_rgb(read_file(VIBRANT_FILE, "#33aaff"))
        self.outline = hex_to_rgb(read_file(OUTLINE_FILE, "#000000"))
        try:    bri = int(read_file(BRIGHT_FILE,"40"))
        except: bri = 40
        if bri > 145:
            self.text_main = (0.05,0.05,0.05,0.95)
            self.text_sub  = (0.05,0.05,0.05,0.50)
        else:
            self.text_main = (1.0,1.0,1.0,0.95)
            self.text_sub  = (1.0,1.0,1.0,0.50)
        self.da.queue_draw()
        return False

    def _on_window_state(self, w, event):
        if event.new_window_state & Gdk.WindowState.ICONIFIED:
            GLib.idle_add(self._restore)
        return False

    def _restore(self):
        gdk_win = self.get_window()
        if gdk_win: gdk_win.show()
        self.deiconify()
        # keep_below only valid on X11; on Hyprland the sinker re-pins us.
        if not (os.environ.get("WAYLAND_DISPLAY") or
                os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
            self.set_keep_below(True)
        return False

    # ── Background Spotify poller ─────────────────────────────────────────────
    # All playerctl subprocess calls run in a daemon thread so the GTK main
    # loop is never stalled waiting for subprocess output (~30-80ms per call).

    def _sp_poll_loop(self):
        """Polls Spotify state every 800 ms off the GTK thread."""
        while True:
            try:
                became_active = self.sp.refresh()
                new_target    = 1.0 if self.sp.status in ("Playing","Paused") else 0.0
                GLib.idle_add(self._apply_track_update, new_target, became_active)
            except Exception as e:
                log(f"sp-poll error: {e}")
            time.sleep(0.8)

    def _apply_track_update(self, fade_target, became_active):
        """Called on GTK main thread after background poll completes."""
        self._fade_target    = fade_target
        self._static_dirty   = True
        return False

    # ── Draw tick ─────────────────────────────────────────────────────────────

    def tick_draw(self):
        now     = time.monotonic()
        elapsed = now - self._last_tick_time
        self._last_tick_time = now

        # Smooth position interpolation
        if self.sp.status == "Playing" and not self._dragging:
            self.sp.pos = min(self.sp.dur, self.sp.pos + elapsed)

        # Fade animation
        if self._fade_alpha < self._fade_target:
            self._fade_alpha = min(1.0, self._fade_alpha + self._FADE_STEP)
        elif self._fade_alpha > self._fade_target:
            self._fade_alpha = max(0.0, self._fade_alpha - self._FADE_STEP)

        # Cover crossfade
        if self._cover_fade < 1.0:
            self._cover_fade = min(1.0, self._cover_fade + 0.06)

        # Marquee scroll
        song = self.sp.song or ""
        if song != self._marquee_song:
            self._marquee_song   = song
            self._marquee_offset = 0.0
            self._marquee_dir    = 1
            self._marquee_pause  = 1.2

        # Idle dim
        idle       = now - self._last_interaction
        dim_target = self._IDLE_ALPHA if idle > self._IDLE_SECS else 1.0
        if self._dim_alpha < dim_target:
            self._dim_alpha = min(dim_target, self._dim_alpha + 0.008)
        elif self._dim_alpha > dim_target:
            self._dim_alpha = max(dim_target, self._dim_alpha - 0.008)

        # ── Idle-skip optimisation ────────────────────────────────────────────
        # Skip queue_draw() entirely when nothing is animating.
        # Drops idle CPU from ~5-8% to <0.5%.
        playing   = self.sp.status == "Playing"
        fading    = abs(self._fade_alpha - self._fade_target) > 0.001
        dimming   = abs(self._dim_alpha - dim_target) > 0.001
        crossfade = self._cover_fade < 1.0
        vol_osd   = time.monotonic() < self._vol_show_until
        marquee   = (self._marquee_offset != 0.0 or
                     self._marquee_dir != 1 or
                     self._marquee_pause > 0)

        needs_draw = (playing or fading or dimming or crossfade or
                      vol_osd or marquee or self._hovered or
                      self._fade_alpha > 0.01)
        if needs_draw:
            self.da.queue_draw()

        # ── Adaptive framerate ────────────────────────────────────────────
        # Full 50ms (20fps) when cava/marquee is animating.
        # Drop to 200ms (5fps) when only static content is showing.
        # Drop to 1000ms (1fps) when widget is fully idle/transparent.
        bars_active = any(v > 0.01 for v in self.cava.values)
        if self._fade_alpha <= 0.01:
            next_ms = 1000
        elif playing or bars_active or fading or dimming or crossfade or marquee or self._hovered:
            next_ms = 50    # 20fps — always smooth when anything is animating
        else:
            next_ms = 200   # 5fps — truly idle

        if next_ms != self._tick_interval:
            self._tick_interval = next_ms
            GLib.timeout_add(next_ms, self.tick_draw)
            return False    # cancel current timer, new one registered
        return True

    # ── Input ─────────────────────────────────────────────────────────────────

    def on_enter(self, w, event):
        self._hovered = True
        self._last_interaction = time.monotonic()
        # Gdk.keyboard_grab is X11-only; on Wayland key events reach us via
        # the normal GTK key-press-event signal as long as the window has focus,
        # which we re-request here via present_with_time only while hovered.
        gdk_win = self.get_window()
        if gdk_win:
            if not (os.environ.get("WAYLAND_DISPLAY") or
                    os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
                Gdk.keyboard_grab(gdk_win, False, Gdk.CURRENT_TIME)
            # On Wayland: request focus only while mouse is inside so keyboard
            # shortcuts work, but don't raise the window above others.
            else:
                self.present_with_time(Gdk.CURRENT_TIME)
        return False

    def on_leave(self, w, event):
        self._hovered      = False
        self._prog_hovered = False
        self._menu_hover   = -1
        if not (os.environ.get("WAYLAND_DISPLAY") or
                os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
            Gdk.keyboard_ungrab(Gdk.CURRENT_TIME)
        return False

    def on_key(self, w, event):
        if self.sp.status not in ("Playing","Paused"): return False
        self._last_interaction = time.monotonic()
        key = event.keyval
        if key == Gdk.KEY_space:
            run(f"playerctl {PLAYER_ARG} play-pause")
            self.sp.status = "Paused" if self.sp.status=="Playing" else "Playing"
        elif key == Gdk.KEY_Left:
            run(f"playerctl {PLAYER_ARG} previous")
            self.sp.pos = 0
        elif key == Gdk.KEY_Right:
            run(f"playerctl {PLAYER_ARG} next")
            self.sp.pos = 0
        elif key == Gdk.KEY_Up:
            self.sp.volume = min(100, self.sp.volume + 5)
            run(f"playerctl {PLAYER_ARG} volume {self.sp.volume/100:.2f}")
            self._vol_show_until = time.monotonic() + 1.5
        elif key == Gdk.KEY_Down:
            self.sp.volume = max(0, self.sp.volume - 5)
            run(f"playerctl {PLAYER_ARG} volume {self.sp.volume/100:.2f}")
            self._vol_show_until = time.monotonic() + 1.5
        elif key in (Gdk.KEY_s, Gdk.KEY_S):
            # S = toggle shuffle
            run(f"playerctl {PLAYER_ARG} shuffle toggle")
            self.sp.set_shuffle(not self.sp.shuffle)
        elif key in (Gdk.KEY_r, Gdk.KEY_R):
            # R = cycle repeat
            cycle    = {"None":"Playlist","Playlist":"Track","Track":"None"}
            new_mode = cycle.get(self.sp.repeat, "None")
            run(f"playerctl {PLAYER_ARG} loop {new_mode}")
            self.sp.set_repeat(new_mode)
        else:
            return False
        self.da.queue_draw()
        return True

    def on_scroll(self, w, event):
        if self.sp.status not in ("Playing","Paused"): return True
        self._last_interaction = time.monotonic()
        if event.direction == Gdk.ScrollDirection.UP:
            self.sp.volume = min(100, self.sp.volume + 5)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.sp.volume = max(0, self.sp.volume - 5)
        else:
            return True
        run(f"playerctl {PLAYER_ARG} volume {self.sp.volume/100:.2f}")
        self._vol_show_until = time.monotonic() + 1.5
        self.da.queue_draw()
        return True

    def _prog_frac(self, x):
        return max(0.0, min(1.0, (x-PROG_PAD)/PROG_W))

    def _menu_item_rect(self, i):
        """Return (x, y, w, h) for context menu item i."""
        mw, mh = 160, 28
        mx = W//2 - mw//2
        my = H//2 - (len(self._menu_items)*mh)//2 + i*mh
        return mx, my, mw, mh

    def on_press(self, w, event):
        x, y = event.x, event.y
        self._last_interaction = time.monotonic()

        # ── Context menu handling ──────────────────────────────────────────────
        if self._menu_open:
            for i, label in enumerate(self._menu_items):
                mx, my, mw, mh = self._menu_item_rect(i)
                if mx <= x <= mx+mw and my <= y <= my+mh:
                    self._menu_open = False
                    self._handle_menu(i)
                else:
                    self._menu_open = False
                self.da.queue_draw()
            return True

        # Right-click → open menu
        if event.button == 3:
            self._menu_open  = True
            self._menu_hover = -1
            self.da.queue_draw()
            return True

        # Progress bar — click OR drag start
        if PROG_Y-14 <= y <= PROG_Y+22 and PROG_PAD <= x <= PROG_PAD+PROG_W:
            self._dragging = True
            self.sp._dragging_ref[0] = True
            self._drag_frac = self._prog_frac(x)
            self.sp.pos = int(self._drag_frac * self.sp.dur)
            self.da.queue_draw()
            return True

        # Shuffle toggle
        if abs(x-SHUF_X) < EXTRA_HR and abs(y-EXTRA_Y) < EXTRA_HR:
            run(f"playerctl {PLAYER_ARG} shuffle toggle")
            self.sp.set_shuffle(not self.sp.shuffle)
            self.da.queue_draw()
            return True

        # Repeat cycle: None → Playlist → Track → None
        if abs(x-REP_X) < EXTRA_HR and abs(y-EXTRA_Y) < EXTRA_HR:
            cycle    = {"None":"Playlist","Playlist":"Track","Track":"None"}
            new_mode = cycle.get(self.sp.repeat, "None")
            run(f"playerctl {PLAYER_ARG} loop {new_mode}")
            self.sp.set_repeat(new_mode)
            self.da.queue_draw()
            return True

        # Transport controls
        if abs(y-CTRL_Y) < 34:
            if abs(x-CTRL_PREV) < 34:
                if self.sp.pos > 3:
                    run(f"playerctl {PLAYER_ARG} position 0")
                    self.sp.pos = 0
                else:
                    run(f"playerctl {PLAYER_ARG} previous")
                    self.sp.pos = 0
            elif abs(x-CTRL_PLAY) < 34:
                run(f"playerctl {PLAYER_ARG} play-pause")
                self.sp.status = "Paused" if self.sp.status=="Playing" else "Playing"
            elif abs(x-CTRL_NEXT) < 34:
                run(f"playerctl {PLAYER_ARG} next")
                self.sp.pos = 0
            self.da.queue_draw()
        return True

    def _handle_menu(self, index):
        if index == 0:   # Open Spotify
            subprocess.Popen(["xdg-open", "spotify:"], stderr=subprocess.DEVNULL)
        elif index == 1: # Copy song info
            text = f"{self.sp.song} — {self.sp.artist}"
            copied = False
            # wl-copy (Wayland)
            if not copied and (os.environ.get("WAYLAND_DISPLAY") or
                               os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")):
                try:
                    proc = subprocess.Popen(["wl-copy"],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    proc.communicate(text.encode())
                    copied = True
                except FileNotFoundError:
                    pass
            # xclip (X11)
            if not copied:
                try:
                    proc = subprocess.Popen(["xclip", "-selection", "clipboard"],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    proc.communicate(text.encode())
                    copied = True
                except FileNotFoundError:
                    pass
            # xsel (X11 fallback)
            if not copied:
                try:
                    proc = subprocess.Popen(["xsel", "--clipboard", "--input"],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    proc.communicate(text.encode())
                except FileNotFoundError:
                    pass
        elif index == 2: # Hide widget
            self._fade_target = 0.0

    def on_release(self, w, event):
        if self._dragging:
            seek = int(self._prog_frac(event.x) * self.sp.dur)
            run(f"playerctl {PLAYER_ARG} position {seek}")
            self.sp.pos = seek
            self._dragging = False
            self.sp._dragging_ref[0] = False
        return True

    def on_motion(self, w, event):
        self._mouse_x = event.x
        self._mouse_y = event.y
        self._last_interaction = time.monotonic()

        prev_prog_hovered = self._prog_hovered
        self._prog_hovered = (
            PROG_Y-14 <= event.y <= PROG_Y+22 and
            PROG_PAD  <= event.x <= PROG_PAD+PROG_W
        )

        prev_menu_hover = self._menu_hover
        if self._menu_open:
            self._menu_hover = -1
            for i in range(len(self._menu_items)):
                mx, my, mw, mh = self._menu_item_rect(i)
                if mx <= event.x <= mx+mw and my <= event.y <= my+mh:
                    self._menu_hover = i
                    break

        if self._dragging:
            self._drag_frac = self._prog_frac(event.x)
            self.sp.pos = int(self._drag_frac * self.sp.dur)
            self._static_dirty = True
            self.da.queue_draw()
        elif (self._prog_hovered != prev_prog_hovered or
              self._menu_hover != prev_menu_hover or
              self._prog_hovered):
            # Only redraw when hover state actually changed or cursor is over
            # the progress bar (tooltip needs live position tracking).
            self.da.queue_draw()
        return True

    # ── Drawing ───────────────────────────────────────────────────────────────

    def on_draw(self, w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0,0,0,0)
        cr.paint()
        if self._fade_alpha <= 0.01:
            return
        cr.set_operator(cairo.OPERATOR_OVER)
        effective_alpha = self._fade_alpha * self._dim_alpha

        # ── Cached static surface ─────────────────────────────────────────
        # Redraw the static content (cover, text, controls, progress) onto a
        # cached ImageSurface only when something actually changed.
        # Cava and fade alpha are drawn directly every frame since they change
        # every frame anyway.
        need_static = (
            self._static_dirty or
            self._cover_fade < 1.0 or
            self._static_surface is None
        )
        if need_static:
            if (self._static_surface is None or
                    self._static_surface.get_width() != W or
                    self._static_surface.get_height() != H):
                self._static_surface = cairo.ImageSurface(
                    cairo.FORMAT_ARGB32, W, H)
            sc = cairo.Context(self._static_surface)
            sc.set_operator(cairo.OPERATOR_SOURCE)
            sc.set_source_rgba(0, 0, 0, 0)
            sc.paint()
            sc.set_operator(cairo.OPERATOR_OVER)
            self._draw_cover(sc)
            self._draw_song(sc)
            self._draw_artist(sc)
            self._draw_extras(sc)
            self._draw_progress(sc)      # progress bar fill + knob (no labels)
            self._draw_controls(sc)
            self._draw_cava(sc)
            self._static_dirty = False

        # Keep redrawing while playing OR while bars are still decaying
        now = time.monotonic()
        bars_active = any(v > 0.01 for v in self.cava.values)
        if self.sp.status == "Playing" or bars_active:
            self._static_dirty = True

        # Paint cached surface (includes cava)
        cr.set_source_surface(self._static_surface, 0, 0)
        cr.paint_with_alpha(effective_alpha)

        # Draw time labels directly every frame so they always show live position
        self._draw_time_labels(cr)

        # Context menu at full opacity on top
        if self._menu_open:
            self._draw_context_menu(cr)

    def _label(self, cr, text, cx, y, size_pt, color,
               bold=False, italic=False, halign="center"):
        layout = PangoCairo.create_layout(cr)
        weight = "Bold" if bold else "Regular"
        fd     = Pango.FontDescription.from_string(f"Cantarell {weight} {size_pt}")
        if italic: fd.set_style(Pango.Style.ITALIC)
        layout.set_font_description(fd)
        layout.set_text(text, -1)
        lw, lh = layout.get_pixel_size()
        tx = (cx-lw/2 if halign=="center" else
              cx-lw   if halign=="right"  else cx)
        # 2-pass shadow (down-right only) instead of 8-pass outline.
        # Visually equivalent at reading sizes; 4x cheaper.
        cr.save(); cr.translate(tx+1, y+1)
        cr.set_source_rgba(*self.outline, 0.45)
        PangoCairo.show_layout(cr, layout); cr.restore()
        cr.save(); cr.translate(tx, y)
        cr.set_source_rgba(*color)
        PangoCairo.show_layout(cr, layout); cr.restore()

    def _rrect(self, cr, x, y, w, h, r):
        cr.arc(x+r,   y+r,   r, math.pi,     3*math.pi/2)
        cr.arc(x+w-r, y+r,   r, 3*math.pi/2, 0)
        cr.arc(x+w-r, y+h-r, r, 0,           math.pi/2)
        cr.arc(x+r,   y+h-r, r, math.pi/2,   math.pi)
        cr.close_path()

    def _draw_tooltip(self, cr, text, cx, y):
        """Small pill-shaped tooltip centered at cx, vertically at y."""
        cr.select_font_face("Cantarell",
            cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(10)
        ext = cr.text_extents(text)
        pw  = ext.width + 14
        ph  = ext.height + 8
        px  = min(max(cx - pw/2, 4), W - pw - 4)
        cr.new_path()
        self._rrect(cr, px, y - ph/2, pw, ph, ph/2)
        cr.set_source_rgba(0, 0, 0, 0.72); cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.90)
        cr.move_to(px + 7, y + ext.height/2 - 1)
        cr.show_text(text)


    def _draw_song(self, cr):
        t = self.sp.song
        if not t: return

        # Measure text width to decide if marquee is needed
        layout = PangoCairo.create_layout(cr)
        fd     = Pango.FontDescription.from_string("Cantarell Bold 17")
        layout.set_font_description(fd)
        layout.set_text(t, -1)
        tw, th = layout.get_pixel_size()

        clip_w = SONG_CLIP_W
        if tw <= clip_w:
            # Short enough — draw centered, no scroll
            self._marquee_offset = 0.0
            self._label(cr, t, W//2, SONG_Y, 17, (*self.accent,1.0), bold=True)
            return

        # Marquee scroll — time-based ping-pong
        # Uses real elapsed time so speed is consistent regardless of FPS.
        now = time.monotonic()
        if not hasattr(self, '_marquee_last_t'):
            self._marquee_last_t = now
        dt = min(now - self._marquee_last_t, 0.1)  # cap dt to avoid jumps
        self._marquee_last_t = now

        max_offset = tw - clip_w + 20
        if max_offset <= 0:
            self._marquee_offset = 0.0
        elif self._marquee_pause > 0:
            self._marquee_pause -= dt
        else:
            self._marquee_offset += self._marquee_dir * 55.0 * dt
            if self._marquee_offset >= max_offset:
                self._marquee_offset = max_offset
                self._marquee_dir    = -1
                self._marquee_pause  = 1.2
            elif self._marquee_offset <= 0.0:
                self._marquee_offset = 0.0
                self._marquee_dir    = 1
                self._marquee_pause  = 1.2

        # Clip and draw scrolled text (2-pass shadow, same as _label)
        cr.save()
        cr.rectangle(SONG_CLIP_X, SONG_Y - 4, clip_w, th + 8)
        cr.clip()
        x = SONG_CLIP_X - self._marquee_offset
        cr.save(); cr.translate(x+1, SONG_Y+1)
        cr.set_source_rgba(*self.outline, 0.45)
        PangoCairo.show_layout(cr, layout); cr.restore()
        cr.save(); cr.translate(x, SONG_Y)
        cr.set_source_rgba(*self.accent, 1.0)
        PangoCairo.show_layout(cr, layout); cr.restore()
        cr.restore()

    def _draw_cover(self, cr):
        ax,ay,aw,r = IMG_X,IMG_Y,IMG_W,14
        # Shadow
        cr.new_path(); cr.set_source_rgba(0,0,0,0.38)
        self._rrect(cr,ax+4,ay+6,aw,aw,r); cr.fill()
        # Old cover underneath during crossfade
        if self.cover_pixbuf_old and self._cover_fade < 1.0:
            cr.save()
            cr.new_path(); self._rrect(cr,ax,ay,aw,aw,r); cr.clip()
            Gdk.cairo_set_source_pixbuf(cr,self.cover_pixbuf_old,ax,ay)
            cr.paint()
            cr.restore()
        # New cover on top, fading in
        if self.cover_pixbuf:
            cr.save()
            cr.new_path(); self._rrect(cr,ax,ay,aw,aw,r); cr.clip()
            Gdk.cairo_set_source_pixbuf(cr,self.cover_pixbuf,ax,ay)
            cr.paint_with_alpha(self._cover_fade)
            cr.restore()
        else:
            cr.new_path(); cr.set_source_rgba(1,1,1,0.04)
            self._rrect(cr,ax,ay,aw,aw,r); cr.fill()
        # Border
        cr.new_path(); cr.set_source_rgba(*self.accent,0.20)
        cr.set_line_width(1.0); self._rrect(cr,ax,ay,aw,aw,r); cr.stroke()

    def _draw_artist(self, cr):
        t = self.sp.artist
        if not t: return
        if len(t)>30: t=t[:29]+"…"
        self._label(cr, t, W//2, ARTIST_Y, 15, (*self.accent,0.90), italic=True)

    def _draw_extras(self, cr):
        cy = EXTRA_Y

        # Shuffle icon
        sh_alpha = 1.0 if self.sp.shuffle else 0.35
        cr.save(); cr.translate(SHUF_X, cy)
        cr.set_source_rgba(*self.accent, sh_alpha)
        cr.set_line_width(1.5); cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(-10,-4); cr.line_to(4,-4); cr.line_to(10,4); cr.stroke()
        cr.move_to(7,1);    cr.line_to(10,4); cr.line_to(7,7);  cr.stroke()
        cr.move_to(-10,4);  cr.line_to(4,4);  cr.line_to(10,-4);cr.stroke()
        cr.move_to(7,-7);   cr.line_to(10,-4);cr.line_to(7,-1); cr.stroke()
        if self.sp.shuffle:
            cr.arc(0,8,2,0,2*math.pi)
            cr.set_source_rgba(*self.accent,1.0); cr.fill()
        cr.restore()

        # Repeat icon
        rp       = self.sp.repeat
        rp_alpha = 1.0 if rp != "None" else 0.35
        cr.save(); cr.translate(REP_X, cy)
        cr.set_source_rgba(*self.accent, rp_alpha)
        cr.set_line_width(1.5); cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.arc(0,0,8,math.pi*0.3,math.pi*2.3); cr.stroke()
        ang = math.pi*2.3
        ax2 = 8*math.cos(ang); ay2 = 8*math.sin(ang)
        cr.move_to(ax2-3,ay2-3); cr.line_to(ax2,ay2)
        cr.line_to(ax2+4,ay2-1); cr.stroke()
        if rp == "Track":
            cr.set_font_size(8)
            cr.select_font_face("Cantarell",
                cairo.FONT_SLANT_NORMAL,cairo.FONT_WEIGHT_BOLD)
            ext = cr.text_extents("1")
            cr.move_to(-ext.width/2, ext.height/2); cr.show_text("1")
        if rp != "None":
            cr.arc(0,9,2,0,2*math.pi)
            cr.set_source_rgba(*self.accent,1.0); cr.fill()
        cr.restore()

        # Hover tooltips for shuffle / repeat
        if self._hovered:
            mx, my = self._mouse_x, self._mouse_y
            tip = None
            if abs(mx-SHUF_X) < EXTRA_HR and abs(my-cy) < EXTRA_HR:
                tip = "Shuffle: On" if self.sp.shuffle else "Shuffle: Off"
            elif abs(mx-REP_X) < EXTRA_HR and abs(my-cy) < EXTRA_HR:
                tip = {"None":"Repeat: Off","Playlist":"Repeat: All",
                       "Track":"Repeat: Track"}.get(rp,"Repeat")
            if tip:
                self._draw_tooltip(cr, tip, W//2, cy - 20)

    def _draw_context_menu(self, cr):
        mw, mh = 160, 28
        n  = len(self._menu_items)
        mx = W//2 - mw//2
        my = H//2 - (n*mh)//2
        # Background panel
        cr.new_path()
        self._rrect(cr, mx-2, my-2, mw+4, n*mh+4, 10)
        cr.set_source_rgba(0.10, 0.10, 0.10, 0.92)
        cr.fill()
        cr.select_font_face("Cantarell",
            cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(13)
        for i, label in enumerate(self._menu_items):
            ix, iy = mx, my + i*mh
            # Hover highlight
            if i == self._menu_hover:
                cr.new_path()
                self._rrect(cr, ix, iy, mw, mh, 6)
                cr.set_source_rgba(*self.accent, 0.25)
                cr.fill()
            # Separator line (except after last)
            if i < n-1:
                cr.set_source_rgba(1,1,1,0.08)
                cr.move_to(ix+10, iy+mh)
                cr.line_to(ix+mw-10, iy+mh)
                cr.set_line_width(0.5); cr.stroke()
            # Label
            ext = cr.text_extents(label)
            cr.set_source_rgba(1,1,1, 0.95 if i==self._menu_hover else 0.75)
            cr.move_to(ix + mw/2 - ext.width/2, iy + mh/2 + ext.height/2)
            cr.show_text(label)

    def _draw_progress(self, cr):
        bary = PROG_Y; barh = 3
        prog = max(0.0,min(1.0,self.sp.pos/max(self.sp.dur,1)))
        cr.new_path(); cr.set_source_rgba(1,1,1,0.12)
        cr.rectangle(PROG_PAD,bary,PROG_W,barh); cr.fill()
        if prog > 0:
            cr.new_path(); cr.set_source_rgba(*self.accent,1.0)
            cr.rectangle(PROG_PAD,bary,PROG_W*prog,barh); cr.fill()
        hx   = PROG_PAD+PROG_W*prog
        dotr = 6.0 if self._dragging else 4.0
        cr.new_path(); cr.set_source_rgba(*self.accent,1.0)
        cr.arc(hx,bary+barh/2,dotr,0,2*math.pi); cr.fill()

        # Hover preview — show time at cursor position
        if (self._prog_hovered or self._dragging) and self._hovered:
            frac     = self._prog_frac(self._mouse_x)
            prev_sec = int(frac * self.sp.dur)
            prev_t   = fmt_time(prev_sec)
            px       = PROG_PAD + PROG_W * frac
            # Ghost knob at hover position
            if not self._dragging:
                cr.new_path(); cr.set_source_rgba(*self.accent, 0.45)
                cr.arc(px, bary+barh/2, 4, 0, 2*math.pi); cr.fill()
            self._draw_tooltip(cr, prev_t, px, bary - 12)

    def _draw_time_labels(self, cr):
        bary = PROG_Y
        cr.select_font_face("Monospace",
            cairo.FONT_SLANT_NORMAL,cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11)
        ty      = bary+18
        pos_t   = fmt_time(self.sp.pos)
        dur_t   = fmt_time(self.sp.dur)
        ext_d   = cr.text_extents(dur_t)
        cr.set_source_rgba(*self.outline,0.25)
        cr.move_to(PROG_PAD+1,ty+1);               cr.show_text(pos_t)
        cr.move_to(PROG_PAD+PROG_W-ext_d.width+1,ty+1); cr.show_text(dur_t)
        cr.set_source_rgba(*self.text_sub)
        cr.move_to(PROG_PAD,ty);                   cr.show_text(pos_t)
        cr.move_to(PROG_PAD+PROG_W-ext_d.width,ty);cr.show_text(dur_t)


    def _fo(self, cr, fa, sa):
        cr.set_source_rgba(*self.accent,fa); cr.fill_preserve()
        cr.set_source_rgba(*self.outline,sa); cr.set_line_width(0.8); cr.stroke()

    def _draw_controls(self, cr):
        cy=CTRL_Y; px=CTRL_PREV; nx=CTRL_NEXT
        cr.new_path(); cr.rectangle(px-10,cy-11,3,22);       self._fo(cr,0.85,0.25)
        cr.new_path()
        cr.move_to(px-4,cy); cr.line_to(px+8,cy-11)
        cr.line_to(px+8,cy+11); cr.close_path();              self._fo(cr,0.85,0.25)
        cr.new_path()
        cr.move_to(nx-8,cy-11); cr.line_to(nx-8,cy+11)
        cr.line_to(nx+4,cy); cr.close_path();                 self._fo(cr,0.85,0.25)
        cr.new_path(); cr.rectangle(nx+7,cy-11,3,22);         self._fo(cr,0.85,0.25)
        r=23
        cr.new_path(); cr.arc(CTRL_PLAY,cy,r,0,2*math.pi)
        cr.set_source_rgba(*self.accent,0.10); cr.fill_preserve()
        cr.set_source_rgba(*self.accent,0.50); cr.set_line_width(1.5); cr.stroke()
        cr.new_path(); cr.arc(CTRL_PLAY,cy,r+0.5,0,2*math.pi)
        cr.set_source_rgba(*self.outline,0.20); cr.set_line_width(0.8); cr.stroke()
        if self.sp.status=="Playing":
            bw=5
            cr.new_path(); cr.rectangle(CTRL_PLAY-bw-2,cy-10,bw,20); self._fo(cr,1.0,0.30)
            cr.new_path(); cr.rectangle(CTRL_PLAY+2,    cy-10,bw,20); self._fo(cr,1.0,0.30)
        else:
            cr.new_path()
            cr.move_to(CTRL_PLAY-7,cy-11); cr.line_to(CTRL_PLAY-7,cy+11)
            cr.line_to(CTRL_PLAY+12,cy); cr.close_path(); self._fo(cr,1.0,0.30)

    def _draw_cava(self, cr):
        vals=self.cava.values; peaks=self.cava._peak
        n=len(vals); gap=3
        bw=(PROG_W-gap*(n-1))/n
        half=n//2
        display=list(range(half-1,-1,-1))+list(range(half))
        ar, ag, ab = self.accent
        for slot,bi in enumerate(display):
            val=min(1.0,vals[bi] ** 0.45)
            pk=min(1.0,peaks[bi] ** 0.45)
            bh=max(2.0,val*CAVA_MAXH)
            bx=PROG_PAD+slot*(bw+gap)
            by=CAVA_BOT-bh
            grad=cairo.LinearGradient(0,by,0,CAVA_BOT)
            grad.add_color_stop_rgba(0.0, ar, ag, ab, 0.35)
            grad.add_color_stop_rgba(0.5, ar, ag, ab, 0.65)
            grad.add_color_stop_rgba(1.0, ar, ag, ab, 0.92)
            cr.set_source(grad)
            cr.new_path(); cr.rectangle(bx,by,bw,bh); cr.fill()
            if pk>0.04:
                py=CAVA_BOT-pk*CAVA_MAXH
                cr.new_path(); cr.set_source_rgba(ar, ag, ab, 0.90)
                cr.rectangle(bx,py-2.0,bw,2.5); cr.fill()
        # Volume OSD below cava
        now = time.monotonic()
        if now < self._vol_show_until:
            remaining = self._vol_show_until - now
            alpha     = min(1.0, remaining / 0.3)
            vol_frac  = max(0.0, min(1.0, self.sp.volume / 100))
            by = VOL_Y; bh = 3
            cr.new_path(); cr.set_source_rgba(1,1,1,0.12*alpha)
            cr.rectangle(PROG_PAD,by,PROG_W,bh); cr.fill()
            cr.new_path(); cr.set_source_rgba(*self.accent,0.9*alpha)
            cr.rectangle(PROG_PAD,by,PROG_W*vol_frac,bh); cr.fill()
            kx = PROG_PAD + PROG_W*vol_frac
            cr.new_path(); cr.set_source_rgba(*self.accent,alpha)
            cr.arc(kx, by+bh/2, 5, 0, 2*math.pi); cr.fill()
            cr.select_font_face("Cantarell",
                cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10)
            label = f"Volume  {self.sp.volume}%"
            ext   = cr.text_extents(label)
            cr.set_source_rgba(*self.text_sub[:3], self.text_sub[3]*alpha)
            cr.move_to(W//2-ext.width/2, by+bh+13); cr.show_text(label)


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("=== widget.py starting ===")
    os.makedirs(os.path.dirname(BG_CSS_FILE), exist_ok=True)
    if not os.path.exists(BG_CSS_FILE):
        write_file(BG_CSS_FILE, "@define-color cover-bg #2a2a30;\n")
    win = MusicWidget()
    win.show_all()
    log("GTK main loop starting")
    Gtk.main()
    log("GTK main loop ended")
