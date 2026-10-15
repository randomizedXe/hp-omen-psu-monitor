"""Native widget integration tests using synthetic inputs and fake collectors."""
from collections import deque
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import omen_psu_app as gui


class FakeCollector:
    def __init__(self, interval=2):
        self.started, self.stopped = 0, 0
        self.interval = interval

    def start(self): self.started += 1
    def stop(self): self.stopped += 1
    def set_interval(self, value): self.interval = value
    def get_events(self): return []


class FakeBridge:
    def __init__(self): self.count = 0
    def publish(self, readings):
        self.count = len(list(readings))
        return self.count
    def cleanup(self):
        self.count = 0
        return True


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent / 'fixtures' / 'snapshot.json'
        cls.fixture = json.loads(path.read_text(encoding='utf-8'))

    def setUp(self):
        qa_root = Path(__file__).resolve().parents[1] / '.release-qa'
        qa_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='omen_app_test_', dir=qa_root)
        self.patches = [patch.object(gui, 'DATA_DIR', Path(self.temp.name)),
                        patch.object(gui, 'BASE_DIR', Path(self.temp.name)),
                        patch.object(gui, 'TelemetryCollector', FakeCollector),
                        patch.object(gui, 'HWiNFOBridge', FakeBridge)]
        for item in self.patches: item.start()
        self.app = gui.OmenPsuApp()
        self.app.title('OMEN PSU Monitor [离线自测]')
        self.app.update_idletasks()
        self.app.withdraw()

    def tearDown(self):
        self.app.close()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def snapshot(self, age=0):
        return {**self.fixture, 'received_monotonic': time.monotonic() - age}

    def test_snapshot_pause_resume_and_bottom_controls_fit(self):
        self.app._snapshot(self.snapshot())
        self.assertTrue(self.app.connected)
        self.assertEqual(len(self.app.tree.get_children()), 12)
        self.assertEqual(self.app.cards['power'][0].cget('text'), '259')
        self.assertLessEqual(self.app.footer.winfo_rooty() + self.app.footer.winfo_height(),
                             self.app.winfo_rooty() + self.app.winfo_height())
        self.app.toggle_pause()
        self.assertTrue(self.app.paused)
        self.assertEqual(self.app.cards['power'][0].cget('text'), '—')
        self.assertEqual(self.app.published_count, 0)
        self.app.toggle_pause()
        self.assertFalse(self.app.paused)
        self.assertEqual(self.app.collector.started, 1)

    def test_expired_snapshot_never_published(self):
        self.app.hwinfo_enabled.set(True)
        self.app._snapshot(self.snapshot(age=60))
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.bridge.count, 0)
        self.assertEqual(self.app.sample_count, 0)

    def test_default_and_portable_data_directories(self):
        root = Path(self.temp.name)
        self.assertEqual(gui.resolve_data_dir(local_app_data=root), root / 'OmenPsuMonitor')
        self.assertEqual(gui.resolve_data_dir(portable=True, base_dir=root), root / 'data')

    def test_legacy_migration_only_accepts_known_preferences(self):
        legacy = Path(self.temp.name) / 'data'
        legacy.mkdir()
        (legacy / 'settings.json').write_text(json.dumps({'interval': 5, 'hwinfo': True,
                                                         'private': 'do not migrate'}), encoding='utf-8')
        self.assertEqual(self.app._load_settings(), {'interval': 5, 'hwinfo': True})
        (legacy / 'settings.json').write_text(json.dumps({'interval': 999, 'hwinfo': 'yes'}), encoding='utf-8')
        self.assertEqual(self.app._load_settings(), {'interval': 2, 'hwinfo': False})

    def test_csv_logging_export_and_same_file_protection(self):
        log = Path(self.temp.name) / 'log.csv'
        export = Path(self.temp.name) / 'history.csv'
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value=str(log)):
            self.app.toggle_recording()
        self.app._snapshot(self.snapshot())
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value=str(log)), \
             patch.object(gui.messagebox, 'showwarning') as warning:
            self.app.export_csv()
            warning.assert_called_once()
        self.app._stop_recording()
        with log.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]['power']), 259)
        with patch.object(gui.filedialog, 'asksaveasfilename', return_value=str(export)):
            self.app.export_csv()
        with export.open(encoding='utf-8-sig', newline='') as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_failed_csv_close_and_bridge_publish_leave_safe_state(self):
        class BadFile:
            def close(self): raise OSError('simulated disk failure')
        self.app.csv_file = BadFile()
        with self.assertLogs(level='ERROR'):
            self.app._stop_recording()
        self.assertIsNone(self.app.csv_file)
        self.app.published_count = 12
        with patch.object(self.app.bridge, 'publish', side_effect=OSError('simulated publish failure')), \
             self.assertLogs(level='ERROR'):
            self.app._publish([])
        self.assertEqual(self.app.published_count, 0)

    def test_immediate_pause_prevents_delayed_collector_start(self):
        self.app.toggle_pause()
        self.app.after(200, self.app.quit)
        self.app.mainloop()
        self.assertEqual(self.app.collector.started, 0)


if __name__ == '__main__':
    unittest.main()
