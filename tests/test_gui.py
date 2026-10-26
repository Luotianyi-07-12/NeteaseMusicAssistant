"""Integration tests against the app's own Tk widgets and local HTTP fixture."""
import contextlib
import io
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from ncm_tools.gui import Application
from test_compatibility import Fixture


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Application(self.root, Path(self.directory.name))
        self.addCleanup(self.destroy)
        self.app.api_var.set(self.fixture.client.base_url)

    def destroy(self):
        self.app.cancel.set()
        if self.app.worker:
            self.app.worker.join(timeout=3)
        self.app.closing = True
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        for child in self.root.winfo_children():
            child.destroy()
        self.app = None
        import gc
        gc.collect()

    def wait_done(self):
        deadline = time.monotonic() + 10
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def test_custom_count_250_is_allowed_but_251_is_blocked(self):
        self.app.count_var.set('250')
        self.assertEqual(self.app.configuration()['max_songs'], 250)
        self.app.count_var.set('251')
        with patch('ncm_tools.gui.messagebox.showerror') as error:
            self.assertIsNone(self.app.configuration())
            error.assert_called_once()

    def test_gui_reads_real_http_fixture_and_updates_statistics(self):
        self.app.status()
        self.wait_done()
        self.assertEqual(self.app.total_var.get(), '100')
        self.assertEqual(self.app.level_var.get(), 'Lv.6')
        self.assertIn('123', self.app.account_var.get())

    def test_gui_run_keeps_no_growth_visible(self):
        self.app.count_var.set('1')
        self.app.verify_var.set('0')
        self.app.run()
        self.wait_done()
        self.assertEqual(self.app.accepted_var.get(), '1')
        self.assertEqual(self.app.delta_var.get(), '+0')
        self.assertIn('未观测到听歌量增长', self.app.logs.get('1.0', 'end'))

    def test_gui_connection_failure_recovers_controls(self):
        self.fixture.authenticated = False
        self.app.status()
        self.wait_done()
        self.assertIn('尚未登录', self.app.logs.get('1.0', 'end'))
        self.assertTrue(all(str(button.cget('state')) == 'normal' for button in self.app.buttons
                            if button is not self.app.song_list_button))

    def test_single_mode_100_allowed_and_101_blocked(self):
        self.app.mode_var.set('单曲播放次数')
        self.app.refresh_mode()
        self.app.song_var.set('1')
        self.app.repeat_var.set('100')
        config = self.app.configuration()
        self.assertEqual(config['repetitions'], 100)
        self.assertEqual(config['song_ids'], [1])
        self.assertEqual(float(self.app.number_input.cget('to')), 100)
        self.app.repeat_var.set('101')
        with patch('ncm_tools.gui.messagebox.showerror') as error:
            self.assertIsNone(self.app.configuration())
            error.assert_called_once()

    def test_single_mode_updates_single_count_instead_of_unique_count(self):
        self.fixture.song_growth = True
        self.app.mode_var.set('单曲播放次数')
        self.app.refresh_mode()
        self.app.song_var.set('1')
        self.app.repeat_var.set('2')
        self.app.verify_var.set('0')
        self.app.interval_var.set('1')
        self.app.run()
        self.wait_done()
        self.assertEqual(self.app.accepted_var.get(), '2')
        self.assertEqual(self.app.delta_var.get(), '+2')
        self.assertEqual(self.app.total_var.get(), '100')
        self.assertEqual(self.app.delta_label_var.get(), '单曲次数增量')

    def test_connection_check_does_not_require_single_song_id(self):
        self.app.mode_var.set('单曲播放次数')
        self.app.song_var.set('')
        self.app.doctor()
        self.wait_done()
        self.assertIn('连接正常', self.app.logs.get('1.0', 'end'))

    def test_three_song_plan_and_custom_rounds_are_saved(self):
        self.app.mode_var.set('单曲/多曲次数')
        self.app.song_var.set('1, 2, 3, 1')
        self.app.repeat_var.set('10')
        self.app.rounds_var.set('2')
        self.app.parallel_var.set('3')
        self.app.refresh_mode()
        config = self.app.configuration()
        self.assertEqual(config['song_ids'], [1, 2, 3])
        self.assertEqual(config['rounds'], 2)
        self.assertEqual(config['concurrency'], 3)
        self.assertIn('计划 60 次', self.app.plan_var.get())
        import json
        stored = json.loads((self.app.state_dir / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['rounds'], 2)
        self.assertEqual(stored['song_id'], '1, 2, 3')

    def test_playlist_mode_forwards_rounds_and_disables_parallel_input(self):
        self.app.rounds_var.set('3')
        self.app.refresh_mode()
        config = self.app.configuration()
        self.assertEqual(config['rounds'], 3)
        self.assertEqual(str(self.app.parallel_input.cget('state')), 'disabled')

    def test_invalid_rounds_are_blocked_before_start(self):
        for rounds in ('0', '-1', '1001', 'abc'):
            self.app.rounds_var.set(rounds)
            with patch('ncm_tools.gui.messagebox.showerror'):
                self.assertIsNone(self.app.configuration())


if __name__ == '__main__':
    unittest.main()
