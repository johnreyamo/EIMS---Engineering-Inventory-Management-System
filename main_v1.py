import network
import time
import json
import os
import uasyncio as asyncio
import machine
import neopixel
import sys
import uhashlib
import ubinascii
import gc
from machine import Pin
from mfrc522 import MFRC522

# ==========================================
# 1. CONFIG & HARDWARE DEFINITIONS
# ==========================================
SESSION_TIMEOUT           = 10
ADMIN_SESSION_TIMEOUT     = 60
SWITCH_DEBOUNCE_MS        = 50
DASHBOARD_WIDTH           = 72
MAX_TOOLS_PER_PAGE        = 10
BUZZER_PIN                = 6
SWITCH_PIN                = 4

SWITCH_CONFIRM_WINDOW_MS  = 5000   
TAG_MISS_THRESHOLD        = 2      
PENDING_TAG_MISS_THRESHOLD = 1      

NORMAL_SCAN_INTERVAL_MS   = 40     
FAST_SCAN_INTERVAL_MS     = 15     

led = machine.Pin(38, machine.Pin.OUT)
px = neopixel.NeoPixel(led, 1)
buzzer = machine.Pin(BUZZER_PIN, machine.Pin.OUT)
mode_switch = machine.Pin(SWITCH_PIN, machine.Pin.IN, machine.Pin.PULL_UP)

# --- HARDWARE RESET SEQUENCE ---
reader = None
def init_scanner():
    print("[HARDWARE] Initializing MFRC522 Scanner...")
    try:
        for pin_num in [14, 13, 12, 10, 9]:
            try:
                machine.Pin(pin_num, machine.Pin.IN)
            except:
                pass
        time.sleep_ms(50)

        rfid_rst = machine.Pin(9, machine.Pin.OUT)
        rfid_rst.value(0)
        time.sleep_ms(150)  
        rfid_rst.value(1)
        time.sleep_ms(150)

        rdr = MFRC522(sck=14, mosi=13, miso=12, rst=9, cs=10)
        
        if hasattr(rdr, 'init'):
            rdr.init()
        if hasattr(rdr, 'stop_crypto1'):
            rdr.stop_crypto1()
            
        print("[HARDWARE] MFRC522 Scanner online.")
        return rdr
    except Exception as e:
        print(f"[CRITICAL] MFRC522 driver failed to init: {e}")
        return None

reader = init_scanner()

_switch_last_reading = mode_switch.value()
_switch_stable_value = _switch_last_reading
_switch_last_change_ms = time.ticks_ms()

def get_stable_switch():
    global _switch_last_reading, _switch_stable_value, _switch_last_change_ms
    current = mode_switch.value()
    if current != _switch_last_reading:
        _switch_last_reading = current
        _switch_last_change_ms = time.ticks_ms()
    elif time.ticks_diff(time.ticks_ms(), _switch_last_change_ms) > SWITCH_DEBOUNCE_MS:
        _switch_stable_value = current
    return _switch_stable_value

# ==========================================
# 2. DATABASE & GLOBAL STATE
# ==========================================
def _load_backup_or_empty(filename):
    backup_name = filename + ".bak"
    try:
        with open(backup_name, "r") as file:
            return json.load(file)
    except (OSError, ValueError):
        return {}

def load_database(filename):
    try:
        with open(filename, "r") as file:
            return json.load(file)
    except OSError:
        return _load_backup_or_empty(filename)
    except ValueError:
        return _load_backup_or_empty(filename)

def save_database(filename, data):
    tmp_name = filename + ".tmp"
    bak_name = filename + ".bak"
    try:
        with open(tmp_name, "w") as file:
            json.dump(data, file)
        try:
            os.rename(filename, bak_name)
        except OSError:
            pass
        os.rename(tmp_name, filename)
    except OSError as e:
        print(f"[ERROR] Failed to save {filename}: {e}")

DB_FLUSH_IDLE_DELAY_MS = 2000
_dirty_files = {}  
_db_flush_timer = 0

def mark_dirty(filename, data):
    global _db_flush_timer
    _dirty_files[filename] = data
    _db_flush_timer = time.ticks_ms() + DB_FLUSH_IDLE_DELAY_MS

async def db_flush_task():
    global _dirty_files, _db_flush_timer
    while True:
        await asyncio.sleep_ms(300)
        if _dirty_files and time.ticks_diff(time.ticks_ms(), _db_flush_timer) > 0:
            pending = _dirty_files
            _dirty_files = {}
            for fname, data in pending.items():
                save_database(fname, data)
                await asyncio.sleep_ms(10) 
            gc.collect()

def hash_password(password):
    digest = uhashlib.sha256(password.encode('utf-8')).digest()
    return ubinascii.hexlify(digest).decode()

_session_token_seq = 0
def generate_session_token():
    global _session_token_seq
    _session_token_seq += 1
    seed = "{}-{}-{}".format(time.ticks_ms(), _session_token_seq, gc.mem_free())
    digest = uhashlib.sha256(seed.encode('utf-8')).digest()
    return ubinascii.hexlify(digest).decode()

def _migrate_users_db(users):
    changed = False
    for uid, val in list(users.items()):
        if isinstance(val, str):
            users[uid] = {"name": val, "status": "Registered"}
            changed = True
    return users, changed

def _migrate_inventory_active(tools):
    changed = False
    for tid, info in tools.items():
        if isinstance(info, dict) and "active" not in info:
            info["active"] = True
            changed = True
    return changed

def _migrate_cabinets(inv):
    changed = False
    if "cabinets" not in inv:
        inv["cabinets"] = [
            {"id": "1", "name": "Cabinet 1"},
            {"id": "2", "name": "Cabinet 2"},
            {"id": "3", "name": "Cabinet 3"}
        ]
        changed = True
    return changed

inventory_db = load_database("inventory.json")
users_db = load_database("users.json")
admins_db = load_database("admins.json")

system_settings = load_database("settings.json")
if not system_settings:
    # Default settings if the file doesn't exist yet
    system_settings = {"session_timeout": 10, "admin_timeout": 60}
    save_database("settings.json", system_settings)
    
SESSION_TIMEOUT = system_settings.get("session_timeout", 10)
ADMIN_SESSION_TIMEOUT = system_settings.get("admin_timeout", 60)
BUZZER_ENABLED = system_settings.get("buzzer_enabled", True)

# Overwrite the global timeout variables with the saved settings
SESSION_TIMEOUT = system_settings.get("session_timeout", 10)
ADMIN_SESSION_TIMEOUT = system_settings.get("admin_timeout", 60)

if "tools" not in inventory_db:
    inventory_db = {"tools": inventory_db}

mig1 = _migrate_inventory_active(inventory_db["tools"])
mig2 = _migrate_cabinets(inventory_db)
if mig1 or mig2:
    save_database("inventory.json", inventory_db)

users_db, _users_changed = _migrate_users_db(users_db)
if _users_changed:
    save_database("users.json", users_db)

if not admins_db:
    admins_db = {"admin": hash_password("123")}
    save_database("admins.json", admins_db)

def get_history_filename():
    y, m, d, h, mi, s, wday, yday = time.localtime()
    week_num = yday // 7
    return f"history_{y}_W{week_num:02d}.json"

current_history_filename = get_history_filename()
history_db = load_database(current_history_filename)
if not isinstance(history_db, list):
    history_db = []

current_session_filename = get_history_filename().replace("history_", "sessions_")
session_history_db = load_database(current_session_filename)
if not isinstance(session_history_db, list):
    session_history_db = []

current_session_entry = None

def check_week_rollover():
    global history_db, current_history_filename
    global session_history_db, current_session_filename
    new_filename = get_history_filename()
    if new_filename != current_history_filename:
        current_history_filename = new_filename
        history_db = load_database(new_filename)
        if not isinstance(history_db, list):
            history_db = []
    new_session_filename = new_filename.replace("history_", "sessions_")
    if new_session_filename != current_session_filename:
        current_session_filename = new_session_filename
        session_history_db = load_database(new_session_filename)
        if not isinstance(session_history_db, list):
            session_history_db = []

def log_borrow(tool_id, tool_name, borrower_name):
    global history_db, current_history_filename
    check_week_rollover()
    y, m, d, h, mi, s, _, _ = time.localtime()
    date_str = f"{m:02d}/{d:02d}/{y}"
    time_str = f"{h:02d}:{mi:02d}"
    control_no = f"TX{y}{m:02d}{d:02d}{h:02d}{mi:02d}{s:02d}"

    entry = {
        "control_no": control_no, "tool_id": tool_id, "tool_name": tool_name,
        "borrowed_by": borrower_name, "date": date_str, "time_borrow": time_str,
        "time_return": "", "status_note": ""
    }
    history_db.append(entry)
    mark_dirty(current_history_filename, history_db)

def log_return(tool_id):
    global history_db, current_history_filename
    check_week_rollover()
    y, m, d, h, mi, s, _, _ = time.localtime()
    time_str = f"{h:02d}:{mi:02d}"

    for entry in reversed(history_db):
        if entry.get("tool_id") == tool_id and not entry.get("time_return"):
            entry["time_return"] = time_str
            mark_dirty(current_history_filename, history_db)
            break

def log_transfer(tool_id, tool_name, previous_holder, new_holder):
    global history_db, current_history_filename
    check_week_rollover()
    y, m, d, h, mi, s, _, _ = time.localtime()
    date_str = f"{m:02d}/{d:02d}/{y}"
    time_str = f"{h:02d}:{mi:02d}"

    for entry in reversed(history_db):
        if entry.get("tool_id") == tool_id and not entry.get("time_return"):
            entry["time_return"] = time_str
            entry["status_note"] = f"Transferred to {new_holder}"
            break

    control_no = f"TX{y}{m:02d}{d:02d}{h:02d}{mi:02d}{s:02d}"
    entry = {
        "control_no": control_no, "tool_id": tool_id, "tool_name": tool_name,
        "borrowed_by": new_holder, "date": date_str, "time_borrow": time_str,
        "time_return": "", "status_note": f"Transferred from {previous_holder}"
    }
    history_db.append(entry)
    mark_dirty(current_history_filename, history_db)

def log_session_start(borrower_id, borrower_name):
    global session_history_db, current_session_filename, current_session_entry
    check_week_rollover()
    y, m, d, h, mi, s, _, _ = time.localtime()
    date_str = f"{m:02d}/{d:02d}/{y}"
    time_str = f"{h:02d}:{mi:02d}"
    control_no = f"SX{y}{m:02d}{d:02d}{h:02d}{mi:02d}{s:02d}"

    entry = {
        "control_no": control_no, "borrower_id": borrower_id, "borrowed_by": borrower_name,
        "date": date_str, "login_time": time_str, "logout_time": "", "items": []
    }
    session_history_db.append(entry)
    current_session_entry = entry
    mark_dirty(current_session_filename, session_history_db)

def log_session_item(tool_id, tool_name, status):
    global current_session_entry
    if current_session_entry is None:
        return
    check_week_rollover()
    y, m, d, h, mi, s, _, _ = time.localtime()
    time_str = f"{h:02d}:{mi:02d}"
    current_session_entry["items"].append({
        "tool_id": tool_id, "name": tool_name, "time": time_str, "status": status
    })
    mark_dirty(current_session_filename, session_history_db)

def log_session_end():
    global current_session_entry
    if current_session_entry is None:
        return
    y, m, d, h, mi, s, _, _ = time.localtime()
    time_str = f"{h:02d}:{mi:02d}"
    current_session_entry["logout_time"] = time_str
    mark_dirty(current_session_filename, session_history_db)
    current_session_entry = None

admin_session_active = False
admin_session_token = None   # NEW: identifies which device actually holds the admin session
admin_last_activity_time = time.ticks_ms()
pending_registration = None
last_registration_result = None

active_borrower_id = None
active_borrower_name = None
last_activity_time = time.ticks_ms()
current_cabinet_id = "1"
terminal_message = "Please tap your ID card to begin..."

scan_event_seq = 0
scan_event_time = 0
pending_switch_id = None
pending_switch_time = 0

def get_short_timestamp():
    y, m, d, h, mi, s, _, _ = time.localtime()
    return f"{m:02d}/{d:02d} {h:02d}:{mi:02d}"

def natural_sort_key(s):
    key = []
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            if num:
                key.append((1, int(num)))
                num = ""
            key.append((0, ch.lower()))
    if num:
        key.append((1, int(num)))
    return key

def sort_tool_entries(entries):
    return sorted(
        entries,
        key=lambda pair: (pair[1].get("status", "Available") == "Available", natural_sort_key(pair[0]))
    )

def emit_terminal_message(msg):
    global terminal_message, scan_event_seq, scan_event_time
    terminal_message = msg
    scan_event_seq += 1
    scan_event_time = time.ticks_ms()
    request_dashboard_render()

# ==========================================
# 3. ASYNC VISUAL CUES (NON-BLOCKING)
# ==========================================
_active_feedback_task = None

def clear_led():
    px[0] = (0, 0, 0)
    px.write()

async def blink_success():
    for _ in range(1):
        px[0] = (0, 255, 0)
        px.write()
        if BUZZER_ENABLED: buzzer.value(1)
        await asyncio.sleep_ms(60)
        clear_led()
        buzzer.value(0)
        await asyncio.sleep_ms(60)

async def blink_error():
    for _ in range(1):
        px[0] = (255, 0, 0)
        px.write()
        if BUZZER_ENABLED: buzzer.value(1)
        await asyncio.sleep_ms(60)
        clear_led()
        buzzer.value(0)
        await asyncio.sleep_ms(60)

async def hold_led_blue():
    px[0] = (0, 0, 255)
    px.write()
    if BUZZER_ENABLED: buzzer.value(1)
    await asyncio.sleep_ms(100)
    buzzer.value(0)

def start_feedback(coro):
    global _active_feedback_task
    if _active_feedback_task is not None and not _active_feedback_task.done():
        try:
            _active_feedback_task.cancel()
        except Exception:
            pass
        clear_led()
        buzzer.value(0)
    _active_feedback_task = asyncio.create_task(coro)
    return _active_feedback_task

async def switch_pending_watchdog(expected_id):
    global pending_switch_id
    await asyncio.sleep_ms(SWITCH_CONFIRM_WINDOW_MS)
    if pending_switch_id == expected_id:
        pending_switch_id = None
        clear_led()
        emit_terminal_message("[ERROR] Session switch timed out.")

# ==========================================
# 4. TERMINAL DASHBOARD LOGIC
# ==========================================
DASHBOARD_RENDER_INTERVAL_MS = 120
DASHBOARD_WRITE_CHUNK_SIZE = 256
_dashboard_dirty = True  

def request_dashboard_render():
    global _dashboard_dirty
    _dashboard_dirty = True

async def dashboard_render_task():
    global _dashboard_dirty
    while True:
        if _dashboard_dirty:
            _dashboard_dirty = False
            await write_terminal_dashboard()
        await asyncio.sleep_ms(DASHBOARD_RENDER_INTERVAL_MS)

def pad_center(text, width):
    text = str(text)
    if len(text) >= width: return text[:width]
    total_pad = width - len(text)
    left = total_pad // 2
    return " " * left + text + " " * (total_pad - left)

def build_terminal_dashboard():
    global current_cabinet_id, terminal_message, active_borrower_name, admin_session_active, pending_registration

    lines = []
    bar = "=" * DASHBOARD_WIDTH
    lines.append(bar)

    if get_stable_switch() == 0:
        lines.append(pad_center("ADMIN MODE - TAG/CARD REGISTRATION", DASHBOARD_WIDTH))
        lines.append(bar)
        if admin_session_active:
            if pending_registration:
                lines.append("")
                lines.append(pad_center("> READY TO WRITE <", DASHBOARD_WIDTH))
                lines.append(pad_center(f"ID to burn: {pending_registration['id']}", DASHBOARD_WIDTH))
                lines.append(pad_center("Tap the new card or tag now...", DASHBOARD_WIDTH))
                lines.append("")
            else:
                lines.append("")
                lines.append(pad_center("SYSTEM STANDBY", DASHBOARD_WIDTH))
                lines.append(pad_center("Enter details on the web dashboard and click Add.", DASHBOARD_WIDTH))
                lines.append("")
                lines.append("")
        else:
            lines.append("")
            lines.append(pad_center("NO ADMIN LOGGED IN", DASHBOARD_WIDTH))
            lines.append(pad_center("Log in on the web dashboard to enable writing.", DASHBOARD_WIDTH))
            lines.append("")
            lines.append("")
        lines.append("-" * DASHBOARD_WIDTH)
        lines.append(f" > {terminal_message}")

    else:
        cabs = inventory_db.get("cabinets", [])
        cab_name = "Unknown Cabinet"
        for c in cabs:
            if str(c["id"]) == str(current_cabinet_id):
                cab_name = c["name"]
                break
                
        lines.append(pad_center(f"WORKSHOP INVENTORY - {cab_name.upper()}", DASHBOARD_WIDTH))
        lines.append(bar)

        user_disp = f"[{active_borrower_name}]" if active_borrower_name else "[LOCKED]"
        lines.append(pad_center(user_disp, DASHBOARD_WIDTH))
        lines.append("-" * DASHBOARD_WIDTH)

        tools = inventory_db.get("tools", {})
        cab_tools = sort_tool_entries(
            [(tid, info) for tid, info in tools.items()
             if str(info.get("cabinet", "1")) == str(current_cabinet_id) and info.get("active", True)]
        )

        for i in range(MAX_TOOLS_PER_PAGE):
            if i < len(cab_tools):
                t_id, info = cab_tools[i]
                name = info.get("name", "?")[:20]
                status = info.get("status", "Available")
                lines.append(f" {t_id:<8} | {name:<20} | {status}")
            else:
                lines.append(" ")

        lines.append("-" * DASHBOARD_WIDTH)
        lines.append(f" > {terminal_message}")
    lines.append(bar)

    return "\x1b[2J\x1b[H" + "\n".join(lines) + "\n"

async def write_terminal_dashboard():
    output = build_terminal_dashboard()
    for i in range(0, len(output), DASHBOARD_WRITE_CHUNK_SIZE):
        sys.stdout.write(output[i:i + DASHBOARD_WRITE_CHUNK_SIZE])
        await asyncio.sleep_ms(0)

async def dashboard_carousel_task():
    global current_cabinet_id, active_borrower_id, active_borrower_name, last_activity_time
   global admin_session_active, admin_session_token, pending_registration, last_registration_result
    global pending_switch_id

    while True:
        now = time.ticks_ms()

        if active_borrower_id and get_stable_switch() == 1 and time.ticks_diff(now, last_activity_time) >= SESSION_TIMEOUT * 1000:
            log_session_end()
            active_borrower_id = None
            active_borrower_name = None
            pending_switch_id = None
            emit_terminal_message("Session ended due to inactivity.")
            mark_dirty("inventory.json", inventory_db)

         if admin_session_active and time.ticks_diff(now, admin_last_activity_time) >= ADMIN_SESSION_TIMEOUT * 1000:
            admin_session_active = False
            admin_session_token = None
            pending_registration = None
            last_registration_result = None
            print("[ADMIN] Session auto-locked (inactivity timeout).")

        cabs = inventory_db.get("cabinets", [])
        if cabs:
            idx = 0
            for i, c in enumerate(cabs):
                if str(c["id"]) == str(current_cabinet_id):
                    idx = i
                    break
            next_idx = (idx + 1) % len(cabs)
            current_cabinet_id = str(cabs[next_idx]["id"])

        request_dashboard_render()
        gc.collect()
        await asyncio.sleep(4)

# ==========================================
# 5. ASYNC RFID SMART SCANNER
# ==========================================
async def read_rfid_smart(rdr, block_num=8):
    REQ_ALL = 0x52 
    OK_STAT = rdr.OK if hasattr(rdr, 'OK') else 0

    (stat, tag_type) = rdr.request(REQ_ALL)
    if stat != OK_STAT: return None

    (stat, raw_uid) = rdr.anticoll()
    if stat != OK_STAT: return None

    hex_uid = "".join([f"{x:02X}" for x in raw_uid])
    select_result = rdr.select_tag(raw_uid)
    if select_result != OK_STAT: return (hex_uid, None)

    key = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
    auth_cmd = rdr.AUTHENT1A if hasattr(rdr, 'AUTHENT1A') else 0x60
    auth_result = rdr.auth(auth_cmd, block_num, key, raw_uid)
    if auth_result != OK_STAT:
        return (hex_uid, None)

    raw_data = rdr.read(block_num)
    rdr.stop_crypto1()
    await asyncio.sleep_ms(0) 

    if raw_data:
        text_data = "".join([chr(byte) for byte in raw_data if 32 <= byte <= 126]).strip()
        if text_data:
            return (hex_uid, text_data)

    return (hex_uid, None)

async def write_rfid_smart(rdr, block_num, text_to_write):
    REQ_ALL = 0x52
    OK_STAT = rdr.OK if hasattr(rdr, 'OK') else 0

    (stat, tag_type) = rdr.request(REQ_ALL)
    await asyncio.sleep_ms(0)
    if stat != OK_STAT: return False

    (stat, raw_uid) = rdr.anticoll()
    await asyncio.sleep_ms(0)
    if stat != OK_STAT: return False

    select_result = rdr.select_tag(raw_uid)
    await asyncio.sleep_ms(0)
    if select_result != OK_STAT: return False

    key = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
    auth_cmd = rdr.AUTHENT1A if hasattr(rdr, 'AUTHENT1A') else 0x60

    auth_result = rdr.auth(auth_cmd, block_num, key, raw_uid)
    await asyncio.sleep_ms(0)
    if auth_result != OK_STAT: return False

    data = [0x20] * 16
    text_bytes = str(text_to_write).encode('utf-8')
    for i in range(min(16, len(text_bytes))):
        data[i] = text_bytes[i]

    write_result = rdr.write(block_num, data)
    await asyncio.sleep_ms(0)
    rdr.stop_crypto1()

    return write_result == OK_STAT

def process_borrow_tap(scanned_id):
    global active_borrower_id, active_borrower_name, last_activity_time
    global pending_switch_id, pending_switch_time
    tools = inventory_db.get("tools", {})
    now = time.ticks_ms()

    if pending_switch_id and time.ticks_diff(now, pending_switch_time) > SWITCH_CONFIRM_WINDOW_MS:
        pending_switch_id = None

    if active_borrower_id is None:
        pending_switch_id = None
        if scanned_id in users_db:
            if users_db[scanned_id].get("status") == "Inactive":
                candidate_name = users_db[scanned_id].get("name", scanned_id)
                emit_terminal_message(f"[ERROR] {candidate_name} is inactive, please reachout the admin for details")
                start_feedback(blink_error())
                return
            active_borrower_id = scanned_id
            active_borrower_name = users_db[scanned_id].get("name", scanned_id)
            last_activity_time = now
            log_session_start(active_borrower_id, active_borrower_name)
            emit_terminal_message(f"Welcome, {active_borrower_name}!")
            start_feedback(blink_success())
        else:
            emit_terminal_message(f"[ERROR] Unrecognized ID: {scanned_id}")
            start_feedback(blink_error())

    elif scanned_id == active_borrower_id:
        pending_switch_id = None
        log_session_end()
        active_borrower_id = None
        active_borrower_name = None
        mark_dirty("inventory.json", inventory_db)
        emit_terminal_message("Session ended manually.")
        start_feedback(blink_success())

    elif scanned_id in tools:
        tool = tools[scanned_id]

        if not tool.get("active", True) or tool.get("cabinet") == "archived":
            emit_terminal_message(f"[ERROR] {tool.get('name', scanned_id)} is unavailable.")
            start_feedback(blink_error())
            return

        if pending_switch_id:
            pending_switch_id = None
            emit_terminal_message("[ERROR] Session switch cancelled.")
            start_feedback(blink_error())
            return

        last_activity_time = now
        current_status = tool.get("status", "Available")

        if current_status == "Available":
            tool["status"] = f"Borrowed by {active_borrower_name}"
            tool["last_update"] = get_short_timestamp()
            mark_dirty("inventory.json", inventory_db)
            log_borrow(scanned_id, tool['name'], active_borrower_name)
            log_session_item(scanned_id, tool['name'], "Borrowed")
            emit_terminal_message(f"Borrowed {tool['name']}")
            start_feedback(blink_success())

        elif current_status == f"Borrowed by {active_borrower_name}":
            tool["status"] = "Available"
            tool["last_update"] = get_short_timestamp()
            mark_dirty("inventory.json", inventory_db)
            log_return(scanned_id)
            log_session_item(scanned_id, tool['name'], "Returned")
            emit_terminal_message(f"Returned {tool['name']}")
            start_feedback(blink_success())

        elif current_status.startswith("Borrowed by "):
            previous_holder = current_status[len("Borrowed by "):]
            tool["status"] = f"Borrowed by {active_borrower_name}"
            tool["last_update"] = get_short_timestamp()
            mark_dirty("inventory.json", inventory_db)
            log_transfer(scanned_id, tool['name'], previous_holder, active_borrower_name)
            log_session_item(scanned_id, tool['name'], f"Transferred from {previous_holder}")
            emit_terminal_message(f"Transferred {tool['name']} from {previous_holder}")
            start_feedback(blink_success())
        else:
            emit_terminal_message("[ERROR] Tool already borrowed.")
            start_feedback(blink_error())

    elif scanned_id in users_db:
        if users_db[scanned_id].get("status") == "Inactive":
            candidate_name = users_db[scanned_id].get("name", scanned_id)
            emit_terminal_message(f"[ERROR] {candidate_name} is inactive, please reachout the admin for details")
            start_feedback(blink_error())
            return
        candidate_name = users_db[scanned_id].get("name", scanned_id)
        if pending_switch_id == scanned_id:
            pending_switch_id = None
            log_session_end()
            active_borrower_id = scanned_id
            active_borrower_name = candidate_name
            last_activity_time = now
            log_session_start(active_borrower_id, active_borrower_name)
            emit_terminal_message(f"Session switched to {active_borrower_name}")
            start_feedback(blink_success())
        else:
            pending_switch_id = scanned_id
            pending_switch_time = now
            emit_terminal_message(f"Tap again within 5s to switch to {candidate_name}...")
            start_feedback(hold_led_blue())
            asyncio.create_task(switch_pending_watchdog(scanned_id))

    else:
        pending_switch_id = None
        emit_terminal_message(f"[ERROR] Unrecognized Tag: {scanned_id}")
        start_feedback(blink_error())

admin_tap_notice_seq = 0  
def notify_admin_mode_tap():
    global admin_tap_notice_seq
    msg = "[NOTICE] Admin mode active. Toggle the switch to activate user mode." if admin_session_active else "[NOTICE] The system is in admin mode, toggle switch to activate user mode."
    admin_tap_notice_seq += 1
    emit_terminal_message(msg)
    start_feedback(blink_error())

_tag_present_id = None
_tag_miss_count = 0

async def rfid_scanner_task():
    global reader, admin_last_activity_time, admin_session_active
    global pending_registration, last_registration_result
    global _tag_present_id, _tag_miss_count

    while reader is None:
        await asyncio.sleep(2) 
        try:
            rfid_rst = machine.Pin(9, machine.Pin.OUT)
            rfid_rst.value(0)
            await asyncio.sleep_ms(100)
            rfid_rst.value(1)
            await asyncio.sleep_ms(100)
            reader = MFRC522(sck=14, mosi=13, miso=12, rst=9, cs=10)
            if hasattr(reader, 'init'): reader.init()
            if hasattr(reader, 'stop_crypto1'): reader.stop_crypto1()
        except Exception:
            reader = None

    while True:
        try:
            is_admin_mode = (get_stable_switch() == 0)

            if is_admin_mode and admin_session_active and pending_registration:
                success = await write_rfid_smart(reader, 8, pending_registration['id'])
                if success:
                    if pending_registration["type"] == "tool":
                        inventory_db["tools"][pending_registration["id"]] = {
                            "name": pending_registration["name"],
                            "status": "Available",
                            "active": True,
                            "last_update": get_short_timestamp(),
                            "cabinet": str(pending_registration.get("cabinet", "1")),
                            "layer": pending_registration.get("layer", 1)
                        }
                        mark_dirty("inventory.json", inventory_db)
                        last_registration_result = {"status": "success", "message": f"[SUCCESS], {pending_registration['name']} has been added.", "type": "tool"}

                    elif pending_registration["type"] == "engineer":
                        users_db[pending_registration["id"]] = {
                            "name": pending_registration["name"],
                            "status": "Active"
                        }
                        mark_dirty("users.json", users_db)
                        last_registration_result = {"status": "success", "message": f"[SUCCESS], Engr. {pending_registration['name']} has been added.", "type": "engineer"}

                    pending_registration = None
                    admin_last_activity_time = time.ticks_ms()
                    start_feedback(blink_success())
                    request_dashboard_render()

                if hasattr(reader, 'init'): reader.init()
                _tag_present_id = None
                _tag_miss_count = 0
                await asyncio.sleep(0.5)

            else:
                result = await read_rfid_smart(reader, 8)
                if result:
                    hex_uid, text_data = result
                    _tag_miss_count = 0
                    if hex_uid != _tag_present_id:
                        _tag_present_id = hex_uid
                        logical_id = text_data if text_data else hex_uid
                        if is_admin_mode:
                            notify_admin_mode_tap()
                        else:
                            process_borrow_tap(logical_id)
                else:
                    _tag_miss_count += 1
                    active_miss_threshold = PENDING_TAG_MISS_THRESHOLD if pending_switch_id else TAG_MISS_THRESHOLD
                    if _tag_miss_count >= active_miss_threshold:
                        _tag_present_id = None

        except Exception:
            try:
                if hasattr(reader, 'init'): reader.init()
            except Exception:
                pass

        if pending_switch_id:
            await asyncio.sleep_ms(FAST_SCAN_INTERVAL_MS)
        else:
            await asyncio.sleep_ms(NORMAL_SCAN_INTERVAL_MS)

# ==========================================
# 6. ASYNC WEB SERVER
# ==========================================
async def send_response(writer, status, content_type, body, extra_headers=None):
    try:
        body_bytes = body.encode('utf-8') if isinstance(body, str) else body
        header_lines = [
            f'HTTP/1.1 {status}',
            f'Content-Type: {content_type}',
            f'Content-Length: {len(body_bytes)}',
            'Connection: close'
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                header_lines.append(f'{k}: {v}')
        
        header_bytes = ('\r\n'.join(header_lines) + '\r\n\r\n').encode('utf-8')
        
        if len(body_bytes) < 2048:
            writer.write(header_bytes + body_bytes)
            await writer.drain()
        else:
            writer.write(header_bytes)
            await writer.drain()
            CHUNK_SIZE = 1024
            for i in range(0, len(body_bytes), CHUNK_SIZE):
                writer.write(body_bytes[i:i + CHUNK_SIZE])
                await writer.drain()
    except OSError:
        pass
    finally:
        await writer.aclose()

async def send_file_response(writer, filename, content_type="text/html"):
    try:
        file_size = os.stat(filename)[6]
        headers = [
            "HTTP/1.1 200 OK",
            f"Content-Type: {content_type}",
            f"Content-Length: {file_size}",
            "Cache-Control: no-store, no-cache, must-revalidate",
            "Pragma: no-cache",
            "Connection: close",
            "\r\n"
        ]
        writer.write("\r\n".join(headers).encode('utf-8'))
        await writer.drain()

        with open(filename, 'rb') as f:
            while True:
                chunk = f.read(1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
    except OSError:
        await send_response(writer, "404 Not Found", "text/plain", "File not found.")
    finally:
        await writer.aclose()
        
def get_header_value(raw_headers, name):
    prefix = name.lower() + ":"
    for line in raw_headers.split('\r\n'):
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None

async def handle_client(reader_stream, writer_stream):
    global inventory_db, users_db, admin_session_active, admin_session_token, admin_last_activity_time

async def handle_client(reader_stream, writer_stream):
    global inventory_db, users_db, admin_session_active, admin_last_activity_time
    global pending_registration, last_registration_result
    global SESSION_TIMEOUT, ADMIN_SESSION_TIMEOUT, BUZZER_ENABLED # <-- Updated! # <-- NEW

    try:
        request_bytes = await reader_stream.read(2048)
        if not request_bytes:
            await writer_stream.aclose()
            return

        parts = request_bytes.split(b'\r\n\r\n', 1)
        header_bytes = parts[0]
        body_bytes = parts[1] if len(parts) > 1 else b""
        try:
            headers = header_bytes.decode('utf-8')
        except UnicodeDecodeError:
            await writer_stream.aclose()
            return
        request_line = headers.split('\r\n')[0]
        method, full_path = request_line.split(' ')[0], request_line.split(' ')[1]
        if '?' in full_path:
            path, query_string = full_path.split('?', 1)
        else:
            path, query_string = full_path, ''
        query_params = {}
        for pair in query_string.split('&'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                query_params[k] = v

        if method == 'POST':
            content_length = 0
            try:
                cl_values = [line.split(":", 1)[1].strip() for line in headers.split('\r\n')
                             if line.lower().startswith("content-length:")]
                if cl_values:
                    content_length = int(cl_values[0])
            except (IndexError, ValueError):
                content_length = 0

            while len(body_bytes) < content_length:
                chunk = await reader_stream.read(content_length - len(body_bytes))
                if not chunk:
                    break
                body_bytes += chunk

        try:
            body = body_bytes.decode('utf-8')
        except UnicodeDecodeError:
            body = ""
        switch_is_admin = (get_stable_switch() == 0)
        
        # is THIS specific device the one that's actually logged in as admin?
        request_admin_token = get_header_value(headers, 'X-Admin-Token')
        is_this_device_admin = bool(admin_session_active and admin_session_token and request_admin_token == admin_session_token)

        # --- ROUTER ---
        if path == '/':
            await send_file_response(writer_stream, 'index.html', 'text/html')
            
        elif path == '/api/update_admin_creds' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                new_user = req.get("username")
                new_pass = req.get("password")
                
                if not new_user or not new_pass:
                    await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "missing fields"}')
                else:
                    # Clear out the old admin database completely and assign the new credentials
                    admins_db.clear() 
                    admins_db[new_user] = hash_password(new_pass)
                    
                    # Save to the JSON file
                    save_database("admins.json", admins_db)
                    
                    # Reset the inactivity timer
                    admin_last_activity_time = time.ticks_ms()
                    
                    await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')
            
        elif path == '/api/get_settings' and is_this_device_admin:
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(system_settings))
            
        elif path == '/api/update_timeouts' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                new_sess = int(req.get("session_timeout", SESSION_TIMEOUT))
                new_admin = int(req.get("admin_timeout", ADMIN_SESSION_TIMEOUT))
                
                # Save to dictionary and JSON file
                system_settings["session_timeout"] = new_sess
                system_settings["admin_timeout"] = new_admin
                save_database("settings.json", system_settings)
                
                # Apply instantly to the running global variables
                SESSION_TIMEOUT = new_sess
                ADMIN_SESSION_TIMEOUT = new_admin
                
                admin_last_activity_time = time.ticks_ms() # reset timer
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid data"}')
                
        elif path == '/api/toggle_buzzer' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                BUZZER_ENABLED = bool(req.get("enabled", True))
                
                system_settings["buzzer_enabled"] = BUZZER_ENABLED
                save_database("settings.json", system_settings)
                
                admin_last_activity_time = time.ticks_ms()
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid data"}')

        elif path == '/api/diagnostics' and is_this_device_admin:
            uptime_ms = time.ticks_ms()
            uptime_sec = uptime_ms // 1000
            
            data = {
                "uptime": uptime_sec,
                "mem_alloc": gc.mem_alloc(),
                "mem_free": gc.mem_free(),
                "buzzer_enabled": BUZZER_ENABLED
            }
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(data))

        elif path == '/api/reboot' and method == 'POST' and is_this_device_admin:
            await send_response(writer_stream, "200 OK", "application/json", '{"status": "rebooting"}')
            await asyncio.sleep(1) # Give it a second to send the HTTP response
            machine.reset() # Hard reset the ESP32
            
        elif path == '/api/backup' and is_this_device_admin:
            # Bundle only the core operating databases to prevent Out-Of-Memory crashes
            backup_data = {
                "inventory": inventory_db,
                "users": users_db,
                "admins": admins_db,
                "settings": system_settings
            }
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(backup_data))

        elif path == '/api/restore' and method == 'POST' and is_this_device_admin:
            try:
                data = json.loads(body)
                # Save whatever tables are provided in the backup
                if "inventory" in data: save_database("inventory.json", data["inventory"])
                if "users" in data: save_database("users.json", data["users"])
                if "admins" in data: save_database("admins.json", data["admins"])
                if "settings" in data: save_database("settings.json", data["settings"])
                
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
                await asyncio.sleep(1)
                machine.reset()
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "Invalid format"}')

        elif path == '/api/purge_history' and method == 'POST' and is_this_device_admin:
            import os
            current_hist = get_history_filename()
            current_sess = current_hist.replace("history_", "sessions_")
            count = 0
            
            for f in os.listdir():
                if (f.startswith("history_") or f.startswith("sessions_")) and f.endswith(".json"):
                    if f != current_hist and f != current_sess:
                        try:
                            os.remove(f)
                            count += 1
                        except OSError:
                            pass
            
            await send_response(writer_stream, "200 OK", "application/json", json.dumps({"purged": count}))

        elif path == '/api/factory_reset' and method == 'POST' and is_this_device_admin:
            import os
            # Wipe absolutely every database and backup file
            for f in os.listdir():
                if f.endswith(".json") or f.endswith(".bak") or f.endswith(".tmp"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            
            await send_response(writer_stream, "200 OK", "application/json", '{"status": "reset"}')
            await asyncio.sleep(1)
            machine.reset()
            
        elif path == '/api/edit_engineer' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                action = req.get("action")
                eng_id = req.get("id")
                
                if eng_id in users_db:
                    if action == "rename":
                        users_db[eng_id]["name"] = req.get("name", users_db[eng_id]["name"])
                    # --- UPDATED: Explicitly set the status from the dropdown ---
                    elif action == "set_status":
                        users_db[eng_id]["status"] = req.get("status", "Active")
                        
                    mark_dirty("users.json", users_db)
                    admin_last_activity_time = time.ticks_ms()
                    await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
                else:
                    await send_response(writer_stream, "404 Not Found", "application/json", '{"error": "engineer not found"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid request"}')

        elif path == '/api/sync_time' and method == 'POST':
            try:
                time_data = json.loads(body)
                machine.RTC().datetime((
                    time_data['year'], time_data['month'], time_data['day'], 0,
                    time_data['hour'], time_data['minute'], time_data['second'], 0
                ))
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "time_synced"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid time"}')

        elif path == '/api/dashboard_status':
            visible_tools = {
                tid: info for tid, info in inventory_db.get("tools", {}).items()
                if info.get("active", True) and info.get("cabinet") != "archived"
            }
            status_payload = {
                "tools": visible_tools,
                "cabinets": inventory_db.get("cabinets", []),
                "active": is_this_device_admin,
                "admin_in_session": admin_session_active,
                "mode": "admin" if switch_is_admin else "borrow",
                "borrower_name": active_borrower_name if active_borrower_name else "",
                "message": terminal_message,
                "event_seq": scan_event_seq,
                "admin_notice_seq": admin_tap_notice_seq,
                "cabinet": current_cabinet_id,
                "reg_result": last_registration_result
            }
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(status_payload),
                                extra_headers={"Cache-Control": "no-store"})

        elif path == '/api/login' and method == 'POST':
            try:
                creds = json.loads(body)
                username = creds.get("username", "")
                password = creds.get("password", "")
                if not username or not password:
                    await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "username and password required"}')
                elif admin_session_active and not is_this_device_admin:
                    await send_response(writer_stream, "409 Conflict", "application/json", '{"error": "admin_in_session"}')
                elif admins_db.get(username) == hash_password(password):
                    admin_session_active = True
                    admin_session_token = generate_session_token()
                    admin_last_activity_time = time.ticks_ms()
                    pending_registration = None
                    last_registration_result = None
                    await send_response(writer_stream, "200 OK", "application/json", json.dumps({"status": "ok", "token": admin_session_token}))
                else:
                    await send_response(writer_stream, "401 Unauthorized", "application/json", '{"error": "invalid credentials"}')

        elelif path == '/api/logout' and method == 'POST' and is_this_device_admin:
            admin_session_active = False
            admin_session_token = None
            pending_registration = None
            last_registration_result = None
            await send_response(writer_stream, "200 OK", "application/json", '{"status": "ok"}')

        elif path == '/api/admin_heartbeat' and method == 'POST':
            if is_this_device_admin:
                admin_last_activity_time = time.ticks_ms()
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "ok"}')
            else:
                await send_response(writer_stream, "401 Unauthorized", "application/json", '{"error": "unauthorized"}')

        elif path == '/api/inventory' and is_this_device_admin:
            admin_last_activity_time = time.ticks_ms()
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(inventory_db.get("tools", {})))

        elif path == '/api/engineers' and is_this_device_admin:
            admin_last_activity_time = time.ticks_ms()
            await send_response(writer_stream, "200 OK", "application/json", json.dumps(users_db))

        elif path == '/api/manage_cabinets' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                action = req.get("action")
                cabs = inventory_db.setdefault("cabinets", [])
                
                if action == "add":
                    new_id = str(int(time.ticks_ms() / 1000))
                    cabs.append({"id": new_id, "name": req.get("name", "New Cabinet")})
                elif action == "edit":
                    cid = str(req.get("id"))
                    for c in cabs:
                        if str(c["id"]) == cid:
                            c["name"] = req.get("name", c["name"])
                            break
                elif action == "delete":
                    cid = str(req.get("id"))
                    inventory_db["cabinets"] = [c for c in cabs if str(c["id"]) != cid]
                    for tid, tool in inventory_db.get("tools", {}).items():
                        if str(tool.get("cabinet")) == cid:
                            tool["cabinet"] = "archived"
                            
                mark_dirty("inventory.json", inventory_db)
                admin_last_activity_time = time.ticks_ms()
                await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid request"}')

        elif path == '/api/stage_registration' and method == 'POST' and is_this_device_admin:
            try:
                data = json.loads(body)
                if not data.get("id") or not data.get("name"):
                    await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "id and name required"}')
                else:
                    pending_registration = data
                    last_registration_result = None
                    admin_last_activity_time = time.ticks_ms()
                    await send_response(writer_stream, "200 OK", "application/json", '{"status": "staged"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')

        elif path == '/api/cancel_registration' and method == 'POST':
            pending_registration = None
            last_registration_result = None
            await send_response(writer_stream, "200 OK", "application/json", '{"status": "cancelled"}')

        elif path == '/api/force_return' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                tool_id = req.get("id")
                if tool_id in inventory_db["tools"]:
                    tool = inventory_db["tools"][tool_id]
                    if tool["status"] != "Available":
                        tool["status"] = "Available"
                        tool["last_update"] = get_short_timestamp()
                        log_return(tool_id)
                        mark_dirty("inventory.json", inventory_db)
                        admin_last_activity_time = time.ticks_ms()
                        await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
                    else:
                        await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "Tool is already available"}')
                else:
                    await send_response(writer_stream, "404 Not Found", "application/json", '{"error": "tool id not found"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')

        elif path == '/api/bulk_action' and method == 'POST' and is_this_device_admin:
            try:
                req = json.loads(body)
                action = req.get("action")
                ids = req.get("ids", [])
                skipped_ids = []

                if action == "delete":
                    for tid in ids:
                        if tid in inventory_db["tools"]:
                            del inventory_db["tools"][tid]
                elif action == "move":
                    cab = str(req.get("cabinet", "1"))
                    for tid in ids:
                        if tid in inventory_db["tools"]:
                            inventory_db["tools"][tid]["cabinet"] = cab
                            inventory_db["tools"][tid]["last_update"] = get_short_timestamp()
                elif action == "set_layer":
                    layer = int(req.get("layer", 1))
                    if layer < 1: layer = 1
                    if layer > 10: layer = 10
                    for tid in ids:
                        if tid in inventory_db["tools"]:
                            inventory_db["tools"][tid]["layer"] = layer
                            inventory_db["tools"][tid]["last_update"] = get_short_timestamp()
                elif action == "deactivate":
                    for tid in ids:
                        if tid in inventory_db["tools"]:
                            t = inventory_db["tools"][tid]
                            if t.get("status", "Available") == "Available":
                                t["active"] = False
                                t["last_update"] = get_short_timestamp()
                            else:
                                skipped_ids.append(tid)
                elif action == "activate":
                    for tid in ids:
                        if tid in inventory_db["tools"]:
                            inventory_db["tools"][tid]["active"] = True
                            inventory_db["tools"][tid]["last_update"] = get_short_timestamp()

                mark_dirty("inventory.json", inventory_db)
                admin_last_activity_time = time.ticks_ms()
                resp_obj = {"status": "success"}
                if skipped_ids:
                    resp_obj["skipped"] = skipped_ids
                await send_response(writer_stream, "200 OK", "application/json", json.dumps(resp_obj))
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')

        elif path == '/api/delete' and method == 'POST' and is_this_device_admin:
            try:
                del_req = json.loads(body)
                del_id = del_req.get("id")
                if not del_id:
                    await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "id required"}')
                elif del_id in inventory_db["tools"]:
                    del inventory_db["tools"][del_id]
                    mark_dirty("inventory.json", inventory_db)
                    admin_last_activity_time = time.ticks_ms()
                    await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
                else:
                    await send_response(writer_stream, "404 Not Found", "application/json", '{"error": "not found"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')

        elif path == '/api/delete_engineer' and method == 'POST' and admin_session_active:
            try:
                del_req = json.loads(body)
                del_id = del_req.get("id")
                if not del_id:
                    await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "id required"}')
                elif del_id in users_db:
                    del users_db[del_id]
                    mark_dirty("users.json", users_db)
                    admin_last_activity_time = time.ticks_ms()
                    await send_response(writer_stream, "200 OK", "application/json", '{"status": "success"}')
                else:
                    await send_response(writer_stream, "404 Not Found", "application/json", '{"error": "not found"}')
            except Exception:
                await send_response(writer_stream, "400 Bad Request", "application/json", '{"error": "invalid json"}')

        elif path == '/api/history':
            check_week_rollover()
            await send_response(writer_stream, "200 OK", "application/json", json.dumps({"month": "Current Week", "entries": history_db}))

        elif path == '/api/session_history':
            check_week_rollover()
            await send_response(writer_stream, "200 OK", "application/json", json.dumps({"month": "Current Week", "entries": session_history_db}))

        elif path == '/api/list_weeks':
            try:
                files = [f for f in os.listdir() if f.startswith('history_') and f.endswith('.json') and not f.endswith('.bak') and not f.endswith('.tmp')]
                weeks = sorted([f[len('history_'):-len('.json')] for f in files], reverse=True)
            except OSError:
                weeks = []
            await send_response(writer_stream, "200 OK", "application/json", json.dumps({"weeks": weeks}))

        elif path == '/api/export_history':
            week_filter = query_params.get('week')
            fname_suffix = f"_{week_filter}" if week_filter else ""
            headers = [
                "HTTP/1.1 200 OK",
                "Content-Type: text/csv",
                f"Content-Disposition: attachment; filename=\"tool_history{fname_suffix}.csv\"",
                "Connection: close",
                "\r\n"
            ]
            writer_stream.write("\r\n".join(headers).encode('utf-8'))
            await writer_stream.drain()

            csv_header = "Control No.,Tool ID,Tool Name,Borrowed By,Date,Time of Borrow,Time of Return,Notes\n"
            writer_stream.write(csv_header.encode('utf-8'))
            await writer_stream.drain()

            try:
                if week_filter:
                    candidate = f"history_{week_filter}.json"
                    files = [candidate] if candidate in os.listdir() else []
                else:
                    files = [f for f in os.listdir() if f.startswith('history_') and f.endswith('.json')]
                files.sort()
                for f in files:
                    try:
                        with open(f, "r") as hf:
                            data = json.load(hf)
                            if isinstance(data, list):
                                for e in data:
                                    row = f"{e.get('control_no','')},{e.get('tool_id','')},{e.get('tool_name','')},{e.get('borrowed_by','')},{e.get('date','')},{e.get('time_borrow','')},{e.get('time_return','')},{e.get('status_note','')}\n"
                                    writer_stream.write(row.encode('utf-8'))
                                    await writer_stream.drain()
                    except:
                        pass
            except OSError:
                pass
            return

        elif path == '/api/export_sessions':
            week_filter = query_params.get('week')
            fname_suffix = f"_{week_filter}" if week_filter else ""
            headers = [
                "HTTP/1.1 200 OK",
                "Content-Type: text/csv",
                f"Content-Disposition: attachment; filename=\"session_history{fname_suffix}.csv\"",
                "Connection: close",
                "\r\n"
            ]
            writer_stream.write("\r\n".join(headers).encode('utf-8'))
            await writer_stream.drain()

            csv_header = "Control No.,Name,Date,Login Time,Item,Item Time,Item Status,Logout Time\n"
            writer_stream.write(csv_header.encode('utf-8'))
            await writer_stream.drain()

            try:
                if week_filter:
                    candidate = f"sessions_{week_filter}.json"
                    files = [candidate] if candidate in os.listdir() else []
                else:
                    files = [f for f in os.listdir() if f.startswith('sessions_') and f.endswith('.json')]
                files.sort()
                for f in files:
                    try:
                        with open(f, "r") as hf:
                            data = json.load(hf)
                            if isinstance(data, list):
                                for s in data:
                                    items = s.get("items") or [{"name": "", "time": "", "status": ""}]
                                    for it in items:
                                        row = f"{s.get('control_no','')},{s.get('borrowed_by','')},{s.get('date','')},{s.get('login_time','')},{it.get('name','')},{it.get('time','')},{it.get('status','')},{s.get('logout_time','')}\n"
                                        writer_stream.write(row.encode('utf-8'))
                                        await writer_stream.drain()
                    except:
                        pass
            except OSError:
                pass
            return

        else:
            await send_response(writer_stream, "401 Unauthorized", "text/plain", "Not Authorized")

    except Exception:
        pass
    finally:
        await writer_stream.aclose()


# ==========================================
# 7. MAIN ENGINE INITIALIZER
# ==========================================
async def main():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid="EIMS - ESP32", password="12345678", authmode=3, channel=6)
    while not ap.active(): pass

    print(f"Network Active. Smart TV URL: http://{ap.ifconfig()[0]}")

    asyncio.create_task(rfid_scanner_task())
    asyncio.create_task(dashboard_carousel_task())
    asyncio.create_task(dashboard_render_task())
    asyncio.create_task(db_flush_task())

    await asyncio.start_server(handle_client, '0.0.0.0', 80)

    while True:
        await asyncio.sleep(1)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\n[SYSTEM] Shutting down gracefully...")
    try:
        if _dirty_files:
            for fname, data in _dirty_files.items():
                save_database(fname, data)
            _dirty_files = {}
    except Exception:
        pass

    if reader:
        if hasattr(reader, 'stop_crypto1'):
            try: reader.stop_crypto1()
            except: pass
        if hasattr(reader, 'spi'):
            try: reader.spi.deinit()
            except: pass
            
    try:
        buzzer.value(0)
        px[0] = (0, 0, 0)
        px.write()
    except:
        pass
