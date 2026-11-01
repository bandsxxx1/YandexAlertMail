import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

# до импорта модуля: файлы настроек/состояния/лога уйдут во временную папку
_TMP = tempfile.mkdtemp(prefix="mailalert-test-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yandex_alert_v3 as app  # noqa: E402

app.winsound = None  # в тестах ничего не играем


def u7(s):
    return "&" + base64.b64encode(s.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"


TRK = u7("Трекер")
FROMS = {
    ("Incidents", b"5"): b"From: soc@client.ru\r\n\r\n",
    ("Other", b"5"): b"From: other@client.ru\r\n\r\n",          # тот же UID в другой папке
    ("Incidents", b"6"): b"From: tracker@tracker.yandex.com\r\n\r\n",
    (TRK, b"1"): b"From: boss@x.ru\r\n\r\n",
}


class FakeMail:
    def __init__(self):
        self.cur = None

    def list(self):
        return "OK", [
            f'(\\HasNoChildren) "|" "{TRK}"'.encode(),
            b'(\\HasNoChildren) "|" Incidents',
            b'(\\HasNoChildren) "|" Other',
            b'(\\HasNoChildren \\Sent) "|" Sent',
            b'(\\HasNoChildren) "|" INBOX',
            b'(\\Noselect) "|" Broken',
        ]

    def select(self, mb, readonly=False):
        assert readonly
        self.cur = mb.strip('"')
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"777"]

    def uid(self, cmd, *a):
        if cmd == "SEARCH":
            return "OK", [b" ".join(uid for (f, uid) in FROMS if f == self.cur)]
        return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM)] {5}", FROMS[(self.cur, a[0])]), b")"]


class ImapTests(unittest.TestCase):
    def test_utf7(self):
        self.assertEqual(app.imap_utf7_decode(TRK), "Трекер")

    def test_same_uid_in_two_folders_and_exclusions(self):
        cfg = app.normalize_settings({"excluded_folders": "Трекер",
                                      "excluded_senders": ["tracker@tracker.yandex.com"]})
        pending, complete = app.fetch_pending(FakeMail(), {}, cfg)
        self.assertEqual(pending, {"Incidents|777|5", "Other|777|5"})
        self.assertTrue(complete)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        app.wake.clear()

    def test_roundtrip_password_not_in_clear_json_key(self):
        app.save_settings({"email": " a@b.c ", "password": "секрет", "check_interval": "45",
                           "excluded_senders": "x@y\n\n z@w ", "incident_sound": "/a/b.wav"})
        loaded = app.load_settings()
        self.assertEqual((loaded["email"], loaded["password"], loaded["check_interval"]),
                         ("a@b.c", "секрет", 45))
        self.assertEqual(loaded["excluded_senders"], ["x@y", "z@w"])
        raw = open(app.SETTINGS_FILE, encoding="utf-8").read()
        self.assertNotIn('"password":', raw)
        self.assertIn("password_enc", raw)

    def test_interval_clamp_and_garbage(self):
        s = app.normalize_settings({"check_interval": 1})
        self.assertEqual(s["check_interval"], 10)
        s = app.normalize_settings({"check_interval": "abc"})
        self.assertEqual(s["check_interval"], 120)

    def test_apply_form(self):
        app.save_settings({"email": "old@x.ru", "password": "pw1"})
        ok, msg = app.apply_form("new@x.ru", "", "a@b\nc@d", "Спам2", "60", "", "")
        self.assertTrue(ok, msg)
        s = app.get_settings()
        self.assertEqual((s["email"], s["password"], s["check_interval"]), ("new@x.ru", "pw1", 60))
        self.assertEqual(s["excluded_senders"], ["a@b", "c@d"])
        self.assertTrue(app.wake.is_set())
        ok, _ = app.apply_form("new@x.ru", "pw2", "", "", "60", "", "")
        self.assertEqual(app.get_settings()["password"], "pw2")

    def test_apply_form_rejects_bad_input(self):
        self.assertFalse(app.apply_form("a@b", "x", "", "", "abc", "", "")[0])
        self.assertFalse(app.apply_form("a@b", "x", "", "", "5", "", "")[0])
        ok, msg = app.apply_form("a@b", "x", "", "", "60", "/нет/такого.wav", "")
        self.assertFalse(ok)
        self.assertIn("не найден", msg)


class SoundTests(unittest.TestCase):
    def test_failure_uses_fail_sound_then_falls_back_to_incident_sound(self):
        with mock.patch.object(app, "play_melody") as pm:
            app.ring_failure({"fail_sound": "F.wav", "incident_sound": "I.wav"})
            self.assertEqual(pm.call_args[0][0], "F.wav")
            app.ring_failure({"fail_sound": "", "incident_sound": "I.wav"})
            self.assertEqual(pm.call_args[0][0], "I.wav")
            app.ring_incident({"fail_sound": "F.wav", "incident_sound": "I.wav"})
            self.assertEqual(pm.call_args[0][0], "I.wav")

    def test_play_melody_without_winsound_does_not_crash_and_stops_on_button(self):
        app.sound_stop.clear()
        import threading
        threading.Timer(0.3, app.sound_stop.set).start()
        t0 = __import__("time").time()
        app.play_melody("", 30)  # мелодия не выбрана -> запасной путь, прерывается кнопкой
        self.assertLess(__import__("time").time() - t0, 5)


class MonitorTests(unittest.TestCase):
    """Гоняем настоящий monitor_loop, подменив сеть/звук/ожидание."""

    def run_loop(self, check_results, cycles):
        app.save_settings({"email": "a@b.c", "password": "x", "incident_sound": "I.wav",
                           "fail_sound": "F.wav", "check_interval": 30})
        calls = {"inc": [], "fail": [], "n": 0}
        it = iter(check_results)

        def fake_check(cache, cfg):
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r

        def fake_wait(seconds):
            calls["n"] += 1
            if calls["n"] >= cycles:
                app.running = False

        app.running = True
        with mock.patch.object(app, "check_once", fake_check), \
                mock.patch.object(app, "ring_incident", lambda cfg, alive=True: calls["inc"].append(cfg["incident_sound"])), \
                mock.patch.object(app, "ring_failure", lambda cfg, alive=True: calls["fail"].append(cfg["fail_sound"])), \
                mock.patch.object(app, "wait_next_check", fake_wait), \
                mock.patch.object(app, "save_state", lambda s: None), \
                mock.patch.object(app, "load_state", lambda: {}):
            app.monitor_loop()
        app.running = True
        return calls

    def test_new_mail_rings_incident_once(self):
        calls = self.run_loop([({"a|1|1"}, True), ({"a|1|1"}, True)], 2)
        self.assertEqual(calls["inc"], ["I.wav"])
        self.assertEqual(app.STATUS["level"], "ok")

    def test_two_errors_play_failure_melody_not_beeps(self):
        calls = self.run_loop([OSError("timed out"), OSError("timed out"), OSError("timed out")], 3)
        self.assertEqual(calls["fail"], ["F.wav"])   # после 2 подряд, повтор не чаще FAIL_ALARM_EVERY_MIN
        self.assertEqual(calls["inc"], [])
        self.assertEqual(app.STATUS["level"], "bad")

    def test_not_configured_waits_and_does_not_ring(self):
        app.save_settings({"email": "", "password": ""})
        n = {"c": 0}

        def fake_wait(s):
            n["c"] += 1
            if n["c"] >= 2:
                app.running = False

        app.running = True
        with mock.patch.object(app, "wait_next_check", fake_wait), \
                mock.patch.object(app, "load_state", lambda: {}), \
                mock.patch.object(app, "check_once", side_effect=AssertionError("не должен ходить в почту")):
            app.monitor_loop()
        app.running = True
        self.assertEqual(app.STATUS["level"], "warn")


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_refused(self):
        app.SINGLE_INSTANCE_PORT = 47999
        self.assertTrue(app.acquire_single_instance())
        first = app._lock_sock
        self.assertFalse(app.acquire_single_instance())
        first.close()


class GuiSmokeTests(unittest.TestCase):
    """tkinter недоступен на CI-Linux: проверяем, что код окна выполняется и кнопка зовёт apply_form."""

    def test_build_gui_with_mocked_tk(self):
        tk = mock.MagicMock()
        mods = {"tkinter": tk, "tkinter.ttk": tk.ttk, "tkinter.filedialog": tk.filedialog,
                "tkinter.messagebox": tk.messagebox}
        with mock.patch.dict(sys.modules, mods):
            root = mock.MagicMock()
            ui = app.build_gui(root)
            with mock.patch.object(app, "apply_form", return_value=(True, "ok")) as af:
                ui["save"]()
                self.assertEqual(af.call_count, 1)
                self.assertEqual(len(af.call_args[0]), 7)
            with mock.patch.object(app, "apply_form", return_value=(False, "плохо")):
                ui["save"]()
                tk.messagebox.showerror.assert_called_once()
            root.protocol.assert_called_once()


if __name__ == "__main__":
    unittest.main()
