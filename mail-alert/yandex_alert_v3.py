# -*- coding: utf-8 -*-
"""
Будильник по почте Яндекса для ночной смены (v3 + окно настроек).

Что умеет:
  * ходит по IMAP и ищет НЕПРОЧИТАННЫЕ письма во всех папках (кроме исключённых);
  * на новое письмо крутит мелодию для инцидентов (петлёй ALARM_SECONDS секунд);
  * если письмо так и висит непрочитанным - напоминает каждые REMIND_EVERY_MIN минут;
  * если сломалась почта / сеть / сам скрипт - крутит мелодию для ошибок;
  * watchdog-поток: если основной цикл завис - играет мелодию ошибки, пишет стеки потоков
    в файл и убивает процесс (run_alert.bat поднимет заново).

Почта, пароль приложения, исключения, интервал проверки и обе мелодии вводятся в окне программы
и хранятся в ~/mail_alert_settings.json (пароль шифруется Windows DPAPI). В коде секретов нет.
"""
import base64
import faulthandler
import imaplib
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import default

try:
    import winsound
except ImportError:  # не Windows - нужно только для тестов
    winsound = None

# ===================== КОНСТАНТЫ (в окне не настраиваются) =====================
IMAP_SERVER = "imap.yandex.com"
IMAP_PORT = 993
TIMEOUT = 30                # таймаут сокета IMAP, сек

ALARM_SECONDS = 30          # сколько играть мелодию инцидента за раз
FAIL_SECONDS = 30           # сколько играть мелодию ошибки за раз

REMIND_EVERY_MIN = 5        # повторять звонок, пока письмо не прочитано (0 - выключить)
REMIND_FOR_HOURS = 3        # ...но не дольше стольких часов после первого звонка

FAIL_ALARM_AFTER = 2        # столько проверок подряд с ошибкой -> мелодия ошибки
FAIL_ALARM_EVERY_MIN = 5    # и повторять не чаще, чем раз в столько минут

HANG_ALARM_AFTER = 300      # watchdog: основной цикл молчит дольше (сек) -> играем мелодию ошибки
HANG_EXIT_AFTER = 600       # ...молчит дольше -> убиваем процесс (код 3), лаунчер перезапустит

CHECK_INBOX = True          # False = INBOX не смотрим. True = смотрим тоже
SINGLE_INSTANCE_PORT = 47653
# ===============================================================================

HOME = os.path.expanduser("~")
SETTINGS_FILE = os.path.join(HOME, "mail_alert_settings.json")
STATE_FILE = os.path.join(HOME, "mail_alert_state.json")
LOG_FILE = os.path.join(HOME, "mail_alert.log")
HANG_DUMP_FILE = os.path.join(HOME, "mail_alert_hang.txt")

SKIP_FLAGS = ('\\noselect', '\\nonexistent', '\\sent', '\\trash', '\\junk', '\\drafts')
SKIP_NAMES = {'sent', 'trash', 'spam', 'junk', 'drafts',
              'отправленные', 'корзина', 'спам', 'черновики'}

log = logging.getLogger("mailalert")
running = True
_heartbeat = time.time()
sound_stop = threading.Event()   # "Стоп звук" в окне
wake = threading.Event()         # "проверь почту прямо сейчас" (после сохранения настроек)
STATUS = {"text": "Запускается...", "level": "warn"}


# ---------------------------------------------------------------- настройки
DEFAULT_SETTINGS = {
    "email": "",
    "password": "",
    "check_interval": 120,
    "excluded_senders": [],
    "excluded_folders": [],
    "incident_sound": "",
    "fail_sound": "",
}
SETTINGS = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
_settings_lock = threading.Lock()


def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    func = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI: операция не удалась")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def encode_password(pw):
    """'dpapi:<b64>' если получилось зашифровать и расшифровать обратно, иначе 'plain:<текст>'."""
    if not pw:
        return ""
    if os.name == "nt":
        try:
            enc = _dpapi(pw.encode("utf-8"), True)
            if _dpapi(enc, False).decode("utf-8") == pw:
                return "dpapi:" + base64.b64encode(enc).decode("ascii")
        except Exception:
            pass
    return "plain:" + pw


def decode_password(stored):
    if not stored:
        return ""
    if stored.startswith("dpapi:"):
        try:
            return _dpapi(base64.b64decode(stored[6:]), False).decode("utf-8")
        except Exception:
            return ""
    if stored.startswith("plain:"):
        return stored[6:]
    return ""


def normalize_settings(raw):
    s = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
    s["email"] = str(raw.get("email", s["email"])).strip()
    s["password"] = str(raw.get("password", s["password"]))
    try:
        interval = int(raw.get("check_interval", s["check_interval"]))
    except (TypeError, ValueError):
        interval = s["check_interval"]
    s["check_interval"] = max(10, min(3600, interval))
    for key in ("excluded_senders", "excluded_folders"):
        val = raw.get(key, s[key])
        if isinstance(val, str):
            val = val.splitlines()
        if isinstance(val, list):
            s[key] = [str(x).strip() for x in val if str(x).strip()]
    for key in ("incident_sound", "fail_sound"):
        s[key] = str(raw.get(key, s[key])).strip()
    return s


def load_settings():
    raw = {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"Файл настроек битый, начинаем с пустых: {e}")
    raw["password"] = decode_password(raw.pop("password_enc", ""))
    s = normalize_settings(raw)
    with _settings_lock:
        SETTINGS.clear()
        SETTINGS.update(s)
    return s


def get_settings():
    with _settings_lock:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in SETTINGS.items()}


def save_settings(new):
    s = normalize_settings(new)
    out = {k: v for k, v in s.items() if k != "password"}
    out["password_enc"] = encode_password(s["password"])
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    with _settings_lock:
        SETTINGS.clear()
        SETTINGS.update(s)
    return s


def is_configured(cfg):
    return bool(cfg["email"] and cfg["password"])


def apply_form(email, password, senders_text, folders_text, interval_text, incident_sound, fail_sound):
    """Логика кнопки 'Сохранить' (без tkinter, чтобы тестировалась). Возвращает (ok, сообщение)."""
    try:
        interval = int(str(interval_text).strip())
    except ValueError:
        return False, "Интервал проверки должен быть числом (секунд)"
    if interval < 10:
        return False, "Интервал проверки не меньше 10 секунд"
    current = get_settings()
    new = {
        "email": email,
        "password": password if password else current["password"],  # пусто = не менять
        "check_interval": interval,
        "excluded_senders": senders_text,
        "excluded_folders": folders_text,
        "incident_sound": incident_sound,
        "fail_sound": fail_sound,
    }
    for key in ("incident_sound", "fail_sound"):
        if new[key] and not os.path.exists(new[key]):
            return False, f"Файл мелодии не найден: {new[key]}"
    save_settings(new)
    wake.set()  # worker проверит почту сразу с новыми настройками
    return True, "Сохранено, применяется сразу"


def set_status(text, level="ok"):
    STATUS["text"] = text
    STATUS["level"] = level


# ---------------------------------------------------------------- служебное
def setup_logging():
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S')
    log.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000,
                                              backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)


def disable_quickedit():
    """Клик мышкой по окну cmd ставит вывод на паузу -> скрипт 'зависает' до Enter.
    Выключаем QuickEdit программно (в Windows Terminal этой проблемы нет)."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            new_mode = (mode.value | 0x80) & ~0x40  # EXTENDED_FLAGS вкл, QUICK_EDIT выкл
            k32.SetConsoleMode(handle, new_mode)
    except Exception as e:
        log.warning(f"Не получилось отключить QuickEdit: {e}")


_lock_sock = None


def acquire_single_instance():
    """Вторая копия дублировала бы звонки. Занимаем порт на localhost как 'замок'."""
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        s.close()
        return False
    _lock_sock = s
    return True


def beat():
    global _heartbeat
    _heartbeat = time.time()


def sleep_alive(seconds):
    """Спим по секундке и отмечаем, что основной поток жив."""
    end = time.time() + seconds
    while running and time.time() < end:
        beat()
        time.sleep(max(0.0, min(1.0, end - time.time())))


def wait_next_check(seconds):
    """Ждём до следующей проверки, но просыпаемся сразу, если нажали 'Сохранить'."""
    end = time.time() + seconds
    while running and time.time() < end:
        beat()
        if wake.wait(1.0):
            wake.clear()
            return


def _stop(sig, frame):
    global running
    running = False


# ---------------------------------------------------------------- звук
def play_melody(path, seconds, alive=True):
    """Крутит WAV петлёй seconds секунд. Прерывается кнопкой 'Стоп звук'.
    Нет файла / нет звука - запасная сирена из бипов, лишь бы не молчать.
    alive=True: отмечаем heartbeat (основной поток). Из watchdog вызываем с alive=False,
    иначе сторож сам себе обновит heartbeat и не доведёт дело до перезапуска."""
    sound_stop.clear()
    started = False
    end = time.time() + seconds
    try:
        if winsound is None:
            raise RuntimeError("winsound недоступен")
        if not path:
            raise FileNotFoundError("мелодия не выбрана в окне настроек")
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        started = True
        while running and not sound_stop.is_set() and time.time() < end:
            if alive:
                beat()
            time.sleep(0.2)
    except Exception as e:
        log.error(f"Мелодия не сыграла ({e}), включаю сирену из бипов")
        try:
            if winsound is None:
                raise RuntimeError("winsound недоступен")
            while running and not sound_stop.is_set() and time.time() < end:
                if alive:
                    beat()
                winsound.Beep(900, 250)
                winsound.Beep(1400, 250)
        except Exception as e2:
            log.error(f"И бипы не сработали: {e2}")
    finally:
        if started and winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass


def ring_incident(cfg, alive=True):
    play_melody(cfg["incident_sound"], ALARM_SECONDS, alive)


def ring_failure(cfg, alive=True):
    """Мелодия ошибки; если для неё ничего не выбрано - играем мелодию инцидента."""
    play_melody(cfg["fail_sound"] or cfg["incident_sound"], FAIL_SECONDS, alive)


# ---------------------------------------------------------------- IMAP
def imap_utf7_decode(s):
    """Имена папок в IMAP приходят в modified UTF-7: '&BCIEQAQ1BDoENQRA-' = 'Трекер'."""
    def repl(m):
        chunk = m.group(1)
        if not chunk:
            return '&'
        b64 = chunk.replace(',', '/')
        b64 += '=' * (-len(b64) % 4)
        return base64.b64decode(b64).decode('utf-16-be')
    try:
        return re.sub(r'&([^-]*)-', repl, s)
    except Exception:
        return s


def quote_mailbox(raw):
    return '"' + raw.replace('\\', '\\\\').replace('"', '\\"') + '"'


LIST_RE = re.compile(
    rb'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>(?:[^"\\]|\\.)*)"|NIL)\s+(?P<name>.+)$')


def list_folders(mail):
    """Возвращает [(сырое_имя_для_SELECT, читаемое_имя, флаги_в_нижнем_регистре)]."""
    typ, data = mail.list()
    if typ != 'OK':
        raise imaplib.IMAP4.error(f"LIST вернул {typ}")
    result = []
    for item in data or []:
        if not isinstance(item, bytes):
            continue
        m = LIST_RE.match(item.strip())
        if not m:
            continue
        flags = m.group('flags').decode('ascii', 'ignore').lower()
        raw = m.group('name').strip()
        if raw.startswith(b'"') and raw.endswith(b'"') and len(raw) >= 2:
            raw = raw[1:-1].replace(b'\\"', b'"').replace(b'\\\\', b'\\')
        raw_s = raw.decode('ascii', 'ignore')
        result.append((raw_s, imap_utf7_decode(raw_s), flags))
    return result


def folder_skipped(name, flags, cfg):
    low = name.casefold()
    if any(f in flags for f in SKIP_FLAGS):
        return True
    if low == 'inbox' and not CHECK_INBOX:
        return True
    if low in SKIP_NAMES:
        return True
    return any(ex.casefold() in low for ex in cfg["excluded_folders"])


def sender_of(raw_header):
    try:
        return str(BytesParser(policy=default).parsebytes(raw_header).get('From', ''))
    except Exception:
        return ''


def is_excluded_sender(sender, cfg):
    if not sender:
        return False
    low = sender.lower()
    return any(ex.lower() in low for ex in cfg["excluded_senders"])


def uidvalidity(mail):
    try:
        _, d = mail.response('UIDVALIDITY')
        return d[0].decode() if d and d[0] else '0'
    except Exception:
        return '0'


def create_connection(cfg):
    """Подключение с повторами: 3 попытки (сразу, +5 с, +15 с)."""
    last = None
    for pause in (0, 5, 15):
        if pause:
            sleep_alive(pause)
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=TIMEOUT)
            mail.login(cfg["email"], cfg["password"])
            return mail
        except Exception as e:
            last = e
            log.warning(f"Не удалось подключиться/залогиниться: {e!r}")
            if mail is not None:
                try:
                    mail.shutdown()
                except Exception:
                    pass
    raise last


def fetch_pending(mail, excluded_cache, cfg):
    """Ключи всех НЕПРОЧИТАННЫХ важных писем: 'папка|UIDVALIDITY|UID'.
    UID уникален только внутри папки, поэтому папка входит в ключ.
    Возвращает (pending, complete); complete=False, если какая-то папка не отработала."""
    pending = set()
    unseen_all = set()
    complete = True

    for raw, name, flags in list_folders(mail):
        if folder_skipped(name, flags, cfg):
            continue
        beat()
        try:
            typ, info = mail.select(quote_mailbox(raw), readonly=True)  # EXAMINE: ничего не помечаем
            if typ != 'OK':
                log.warning(f"Папка '{name}' не открылась: {info}")
                complete = False
                continue
            validity = uidvalidity(mail)
            typ, data = mail.uid('SEARCH', None, 'UNSEEN')
            if typ != 'OK' or not data or not data[0]:
                continue
            for uid in data[0].split():
                beat()
                key = f"{name}|{validity}|{uid.decode()}"
                unseen_all.add(key)
                if key not in excluded_cache:
                    typ, md = mail.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (FROM)])')
                    if typ == 'OK' and md and isinstance(md[0], tuple):
                        excluded_cache[key] = is_excluded_sender(sender_of(md[0][1]), cfg)
                    else:
                        # не смогли прочитать отправителя: лучше лишний раз позвонить, чем промолчать
                        complete = False
                        pending.add(key)
                        continue
                if not excluded_cache[key]:
                    pending.add(key)
        except (imaplib.IMAP4.abort, OSError):
            raise  # соединение умерло (в т.ч. таймаут) - весь проход в мусор, пусть решает вызывающий
        except Exception as e:
            log.warning(f"Ошибка в папке '{name}': {e!r}")
            complete = False

    if complete:
        for k in list(excluded_cache):
            if k not in unseen_all:
                del excluded_cache[k]
    return pending, complete


def check_once(excluded_cache, cfg):
    mail = create_connection(cfg)
    try:
        return fetch_pending(mail, excluded_cache, cfg)
    finally:
        try:
            mail.logout()
        except Exception:
            try:
                mail.shutdown()
            except Exception:
                pass


# ---------------------------------------------------------------- состояние
def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return {str(k): float(v) for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning(f"Файл состояния битый, начинаем с нуля: {e}")
        return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.warning(f"Не удалось сохранить состояние: {e}")


# ---------------------------------------------------------------- watchdog
def _wd_note(text):
    """Пишем напрямую в файл, мимо logging: если консоль залипла, logging может встать колом."""
    try:
        with open(HANG_DUMP_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {text}\n")
    except Exception:
        pass


def watchdog():
    global _heartbeat
    last_tick = time.time()
    last_alarm = 0.0
    dumped = False
    while True:
        time.sleep(10)
        now = time.time()
        if now - last_tick > 60:
            # Мы сами проспали больше минуты - значит, спал/замирал весь комп
            _heartbeat = now
            _wd_note(f"Разрыв времени {int(now - last_tick)} с: комп спал или замирал "
                     f"(проверь схему электропитания / обновления Windows)")
            last_tick = now
            dumped = False
            continue
        last_tick = now
        stale = now - _heartbeat
        if stale < HANG_ALARM_AFTER:
            dumped = False
            continue
        if not dumped:
            _wd_note(f"Основной цикл молчит {int(stale)} с. Стеки потоков:")
            try:
                with open(HANG_DUMP_FILE, 'a', encoding='utf-8') as f:
                    faulthandler.dump_traceback(file=f, all_threads=True)
            except Exception:
                pass
            dumped = True
        if now - last_alarm >= 60:
            last_alarm = now
            threading.Thread(target=ring_failure, args=(get_settings(), False), daemon=True).start()
        if stale >= HANG_EXIT_AFTER:
            _wd_note("Убиваю процесс (код 3), лаунчер должен поднять заново")
            os._exit(3)


# ---------------------------------------------------------------- мониторинг (рабочий поток)
def monitor_loop():
    state = load_state()
    excluded_cache = {}
    fails = 0
    last_ring = 0.0
    last_fail_alarm = 0.0

    while running:
        beat()
        cfg = get_settings()
        if not is_configured(cfg):
            set_status("Впиши почту и пароль приложения и нажми «Сохранить»", "warn")
            wait_next_check(3)
            continue
        try:
            pending, complete = check_once(excluded_cache, cfg)
            fails = 0
            now = time.time()

            new = [k for k in pending if k not in state]
            for k in new:
                state[k] = now
            if complete:  # прочитанные письма вычищаем, чтобы файл не рос
                state = {k: v for k, v in state.items() if k in pending}
            save_state(state)

            reason = None
            if new:
                reason = f"новых писем: {len(new)}"
            elif (REMIND_EVERY_MIN and now - last_ring >= REMIND_EVERY_MIN * 60
                  and any(now - state[k] < REMIND_FOR_HOURS * 3600 for k in pending)):
                reason = "напоминание: письма так и висят непрочитанными"

            stamp = time.strftime("%H:%M:%S")
            if reason:
                log.info(f"ЗВОНИМ: {reason}")
                set_status(f"{stamp}  ЗВОНОК: {reason}", "bad")
                ring_incident(cfg)
                last_ring = time.time()
            else:
                log.info(f"Тихо. Непрочитанных важных: {len(pending)}")
                set_status(f"{stamp}  Всё тихо. Непрочитанных важных: {len(pending)}", "ok")
        except Exception as e:
            fails += 1
            log.error(f"Сбой проверки #{fails} подряд: {e!r}")
            set_status(f"{time.strftime('%H:%M:%S')}  ОШИБКА #{fails} подряд: {e!r}", "bad")
            if fails >= FAIL_ALARM_AFTER and time.time() - last_fail_alarm >= FAIL_ALARM_EVERY_MIN * 60:
                log.error("Почта недоступна несколько проверок подряд - играем мелодию ошибки")
                last_fail_alarm = time.time()
                ring_failure(cfg)
        wait_next_check(cfg["check_interval"])


# ---------------------------------------------------------------- окно настроек (tkinter)
def build_gui(root):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    cfg = get_settings()
    root.title("MailAlarm - настройки")
    root.columnconfigure(0, weight=1)
    root.protocol("WM_DELETE_WINDOW", root.iconify)  # крестик сворачивает, мониторинг не останавливается

    main = ttk.Frame(root, padding=12)
    main.grid(row=0, column=0, sticky="nsew")
    main.columnconfigure(0, weight=1)

    # --- почта
    box = ttk.LabelFrame(main, text="Почта", padding=8)
    box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    box.columnconfigure(1, weight=1)
    ttk.Label(box, text="Адрес ящика").grid(row=0, column=0, sticky="w")
    e_email = ttk.Entry(box, width=46)
    e_email.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)
    e_email.insert(0, cfg["email"])
    ttk.Label(box, text="Пароль приложения").grid(row=1, column=0, sticky="w")
    e_pass = ttk.Entry(box, width=46, show="•")
    e_pass.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
    pw_hint = ttk.Label(box, text="", foreground="gray")
    pw_hint.grid(row=2, column=1, sticky="w", padx=(8, 0))

    def refresh_pw_hint():
        pw_hint.configure(text="сохранён (оставь поле пустым, чтобы не менять)"
                          if get_settings()["password"] else "не задан")
    refresh_pw_hint()

    # --- исключения
    box = ttk.LabelFrame(main, text="Не звонить", padding=8)
    box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    box.columnconfigure(0, weight=1)
    ttk.Label(box, text="От отправителей (по одному в строке, достаточно куска адреса):").grid(
        row=0, column=0, sticky="w")
    t_senders = tk.Text(box, height=4, width=52)
    t_senders.grid(row=1, column=0, sticky="ew", pady=(2, 6))
    t_senders.insert("1.0", "\n".join(cfg["excluded_senders"]))
    ttk.Label(box, text="Не смотреть папки (по одной в строке):").grid(row=2, column=0, sticky="w")
    t_folders = tk.Text(box, height=3, width=52)
    t_folders.grid(row=3, column=0, sticky="ew", pady=(2, 0))
    t_folders.insert("1.0", "\n".join(cfg["excluded_folders"]))

    # --- проверка
    box = ttk.LabelFrame(main, text="Проверка почты", padding=8)
    box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
    ttk.Label(box, text="Проверять каждые").grid(row=0, column=0, sticky="w")
    v_interval = tk.StringVar(value=str(cfg["check_interval"]))
    ttk.Spinbox(box, from_=10, to=3600, increment=10, textvariable=v_interval, width=7).grid(
        row=0, column=1, padx=8)
    ttk.Label(box, text="секунд").grid(row=0, column=2, sticky="w")

    # --- мелодии
    box = ttk.LabelFrame(main, text="Мелодии (.wav)", padding=8)
    box.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    box.columnconfigure(1, weight=1)
    v_inc = tk.StringVar(value=cfg["incident_sound"])
    v_fail = tk.StringVar(value=cfg["fail_sound"])

    def pick(var):
        path = filedialog.askopenfilename(title="Выбери .wav", filetypes=[("WAV", "*.wav")])
        if path:
            var.set(path)

    def test_play(var):
        if not var.get():
            messagebox.showinfo("MailAlarm", "Сначала выбери мелодию")
            return
        threading.Thread(target=play_melody, args=(var.get(), 6, False), daemon=True).start()

    for row, (title, var) in enumerate((("Пришёл инцидент", v_inc), ("Ошибка / что-то сломалось", v_fail))):
        ttk.Label(box, text=title).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=var, state="readonly", width=38).grid(
            row=row, column=1, sticky="ew", padx=8)
        ttk.Button(box, text="Выбрать…", command=lambda v=var: pick(v)).grid(row=row, column=2)
        ttk.Button(box, text="▶", width=3, command=lambda v=var: test_play(v)).grid(
            row=row, column=3, padx=(4, 0))

    # --- кнопки и статус
    bar = ttk.Frame(main)
    bar.grid(row=4, column=0, sticky="ew")
    saved = ttk.Label(main, text="")
    saved.grid(row=5, column=0, sticky="w", pady=(4, 0))

    def on_save():
        ok, msg = apply_form(e_email.get(), e_pass.get(),
                             t_senders.get("1.0", "end"), t_folders.get("1.0", "end"),
                             v_interval.get(), v_inc.get(), v_fail.get())
        if not ok:
            messagebox.showerror("MailAlarm", msg)
            return
        e_pass.delete(0, "end")
        refresh_pw_hint()
        v_interval.set(str(get_settings()["check_interval"]))
        saved.configure(text="✓ " + msg, foreground="green")
        root.after(4000, lambda: saved.configure(text=""))

    def on_exit():
        if messagebox.askyesno("MailAlarm", "Мониторинг почты остановится. Выйти?"):
            _stop(None, None)

    ttk.Button(bar, text="Сохранить", command=on_save).pack(side="left")
    ttk.Button(bar, text="Стоп звук", command=sound_stop.set).pack(side="left", padx=8)
    ttk.Button(bar, text="Выход", command=on_exit).pack(side="right")

    status = ttk.Label(main, text=STATUS["text"], wraplength=520, justify="left")
    status.grid(row=6, column=0, sticky="ew", pady=(10, 0))
    ttk.Label(main, foreground="gray", wraplength=520, justify="left",
              text="Крестик окна только сворачивает его: мониторинг продолжает работать. "
                   "Остановить совсем: кнопка «Выход» или Ctrl+C в чёрном окне."
              ).grid(row=7, column=0, sticky="w", pady=(6, 0))

    colors = {"ok": "#1a7f37", "warn": "#9a6700", "bad": "#cf222e"}

    def refresh():
        status.configure(text=STATUS["text"], foreground=colors.get(STATUS["level"], "black"))
        root.after(1000, refresh)

    def tick():  # будит Tk, чтобы Python успевал обрабатывать Ctrl+C и флаг выхода
        if not running:
            root.quit()
        else:
            root.after(300, tick)

    refresh()
    tick()
    if not is_configured(cfg):
        root.lift()
    return {"save": on_save, "email": e_email, "password": e_pass, "senders": t_senders,
            "folders": t_folders, "interval": v_interval, "incident": v_inc, "fail": v_fail}


def run_gui():
    import tkinter as tk
    root = tk.Tk()
    build_gui(root)
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------- main
def main():
    global running
    setup_logging()
    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        pass

    if not acquire_single_instance():
        log.error("MailAlarm уже запущен (порт %d занят). Закрой старую копию.", SINGLE_INSTANCE_PORT)
        return 2

    disable_quickedit()
    cfg = load_settings()
    log.info(f"Запуск. Ящик: {cfg['email'] or '(не задан)'}. Проверка каждые {cfg['check_interval']} с, "
             f"таймаут {TIMEOUT} с")
    log.info(f"Исключённые папки: {cfg['excluded_folders']}; исключённые отправители: {cfg['excluded_senders']}")
    log.info(f"INBOX {'проверяем' if CHECK_INBOX else 'НЕ проверяем'}; "
             f"напоминания: {'каждые %d мин' % REMIND_EVERY_MIN if REMIND_EVERY_MIN else 'выкл'}")
    for title, path in (("инцидента", cfg["incident_sound"]), ("ошибки", cfg["fail_sound"])):
        if path and not os.path.exists(path):
            log.error(f"Файл мелодии {title} не найден: {path} - будет сирена из бипов")
    try:
        if winsound:
            winsound.Beep(1000, 200)  # проверка, что звук вообще работает
    except Exception as e:
        log.error(f"Тестовый бип не прошёл (нет звукового устройства?): {e}")

    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()
    threading.Thread(target=monitor_loop, daemon=True, name="monitor").start()

    try:
        run_gui()
    except Exception as e:  # нет tkinter / нет экрана: работаем без окна, настройки берём из файла
        log.error(f"Окно настроек не открылось ({e!r}). Мониторинг работает без окна.")
        while running:
            time.sleep(0.5)

    running = False
    log.info("Остановлено пользователем")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
