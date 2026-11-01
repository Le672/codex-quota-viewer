import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

from codexquota.instance import SingleInstance, instance_exists
from codexquota.lifecycle import (CodexExitSignal, CodexFollower, FollowState, HostProcess,
                                  find_codex_desktops, is_codex_desktop_path, is_desktop_main,
                                  launch_viewer, viewer_command, window_is_open, ensure_registered_follower)

DESKTOP = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.930.3748.0_x64__family\app\ChatGPT.exe'
HOST = (HostProcess(101, 1.0),)


def process(pid, executable=DESKTOP, arguments=None):
    return SimpleNamespace(pid=pid, info={'name': Path(executable).name}, exe=lambda: executable,
                           cmdline=lambda: arguments if arguments is not None else [executable],
                           create_time=lambda: 1.0, is_running=lambda: True)


class DesktopDetectionTests(unittest.TestCase):
    def setUp(self):
        self.window_probe = patch('codexquota.lifecycle.open_desktop_pids', return_value=None)
        self.window_probe.start()

    def tearDown(self):
        self.window_probe.stop()

    def test_desktop_window_close_is_distinct_from_background_process(self):
        with patch('psutil.process_iter', return_value=[process(101)]), \
                patch('codexquota.lifecycle.open_desktop_pids', return_value=set()):
            self.assertEqual(find_codex_desktops(), ())

    def test_store_package_updates_and_name_case(self):
        self.assertTrue(is_codex_desktop_path(DESKTOP))
        self.assertTrue(is_codex_desktop_path(DESKTOP.replace('26.930.3748.0', '27.101.9999.0').upper()))
        self.assertTrue(is_codex_desktop_path(DESKTOP.replace('ChatGPT.exe', 'Codex.exe')))

    def test_chatgpt_cli_service_and_similarly_named_paths_are_excluded(self):
        for path in (DESKTOP.replace('OpenAI.Codex_', 'OpenAI.ChatGPT_'),
                     DESKTOP.replace('app\\ChatGPT.exe', 'app\\resources\\codex.exe'),
                     DESKTOP.replace('ChatGPT.exe', 'codex-windows-sandbox-service.exe'),
                     r'C:\Users\me\AppData\Local\OpenAI\Codex\bin\hash\codex.exe',
                     r'C:\Tools\OpenAI.Codex_fake\app\ChatGPT.exe', ''):
            with self.subTest(path=path):
                self.assertFalse(is_codex_desktop_path(path))

    def test_electron_helpers_including_orphans_are_excluded(self):
        for role in ('renderer', 'utility', 'gpu-process', 'crashpad-handler'):
            self.assertFalse(is_desktop_main([DESKTOP, f'--type={role}']))
            self.assertFalse(is_desktop_main([DESKTOP, '--type', role]))
        self.assertFalse(is_desktop_main([]))
        self.assertTrue(is_desktop_main([DESKTOP, '--some-main-flag']))

    def test_only_main_processes_are_returned(self):
        processes = [process(101), process(102, arguments=[DESKTOP, '--type=renderer']),
                     process(103, DESKTOP.replace('OpenAI.Codex_', 'OpenAI.ChatGPT_')),
                     process(104, DESKTOP.replace('app\\ChatGPT.exe', 'app\\resources\\codex.exe')),
                     process(105)]
        with patch('psutil.process_iter', return_value=processes):
            self.assertEqual(find_codex_desktops(), (HostProcess(101, 1), HostProcess(105, 1)))

    def test_empty_or_inaccessible_cmdline_does_not_mean_codex_exited(self):
        with patch('psutil.process_iter', return_value=[process(101, arguments=[])]):
            self.assertIsNone(find_codex_desktops())
        item = process(101)
        item.cmdline = Mock(side_effect=psutil.AccessDenied(101))
        with patch('psutil.process_iter', return_value=[item]):
            self.assertIsNone(find_codex_desktops())

    def test_process_exiting_during_scan_is_ignored(self):
        item = process(101)
        item.exe = Mock(side_effect=psutil.NoSuchProcess(101))
        with patch('psutil.process_iter', return_value=[item]):
            self.assertEqual(find_codex_desktops(), ())

    def test_a_confirmed_host_allows_start_despite_an_inaccessible_candidate(self):
        item = process(102)
        item.exe = Mock(side_effect=psutil.AccessDenied(102))
        with patch('psutil.process_iter', return_value=[item, process(101)]):
            self.assertEqual(find_codex_desktops(), HOST)


class FollowStateTests(unittest.TestCase):
    def test_window_reopen_with_same_process_starts_a_new_session(self):
        state = FollowState()
        state.step(HOST, 0)
        state.step((), 1)
        self.assertEqual(state.step((), 3), 'stop')
        self.assertEqual(state.step(HOST, 4), 'start')

    def test_hidden_tool_window_and_remaining_taskbar_proxy_mean_closed(self):
        rows = [dict(visible=True, tool=False), dict(visible=False, tool=True)]
        self.assertFalse(window_is_open(rows, prefer_tool=True))
        self.assertFalse(window_is_open(rows[:1], prefer_tool=True))
        self.assertTrue(window_is_open([dict(visible=True, tool=True)], prefer_tool=True))
        self.assertTrue(window_is_open(rows[:1], prefer_tool=False))

    def test_start_only_once_during_codex_session(self):
        state = FollowState()
        self.assertEqual(state.step(HOST, 0), 'start')
        self.assertIsNone(state.step(HOST, 10))
        self.assertIsNone(state.step((HostProcess(102, 10),), 20))

    def test_initial_absence_closes_existing_widget_once(self):
        state = FollowState()
        self.assertEqual(state.step((), 0), 'stop')
        self.assertIsNone(state.step((), 100))
        self.assertEqual(state.step(HOST, 101), 'start')

    def test_last_host_exit_has_grace_then_next_session_reopens(self):
        state = FollowState()
        state.step(HOST, 0)
        self.assertIsNone(state.step((), 1))
        self.assertIsNone(state.step((), 2.9))
        self.assertEqual(state.step((), 3), 'stop')
        self.assertIsNone(state.step((), 5))
        self.assertEqual(state.step(HOST, 6), 'start')

    def test_quick_codex_restart_does_not_close_or_duplicate_widget(self):
        state = FollowState()
        state.step(HOST, 0)
        state.step((), 1)
        self.assertIsNone(state.step((HostProcess(102, 2),), 2))
        self.assertTrue(state.active)

    def test_scanner_failure_resets_absence_timer(self):
        state = FollowState()
        self.assertIsNone(state.step(None, 0))
        state.step(HOST, 1)
        state.step((), 2)
        self.assertIsNone(state.step(None, 10))
        self.assertIsNone(state.step((), 20))
        self.assertEqual(state.step((), 22), 'stop')


class FollowerTests(unittest.TestCase):
    def test_manual_start_restores_only_our_registered_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'APPDATA': temporary, 'CODEXQUOTA_INSTANCE_SCOPE': ''}), \
                    patch('codexquota.lifecycle.instance_exists', return_value=False), \
                    patch('subprocess.run') as run:
                run.return_value.returncode = 0
                self.assertFalse(ensure_registered_follower())
                (Path(temporary)/'CodexQuotaViewer'/'codex-follow-task.json').write_text('{}')
                self.assertTrue(ensure_registered_follower())
                self.assertEqual(run.call_args.args[0][-3:], ['/Run', '/TN', 'CodexQuotaViewer-FollowCodex'])

    def test_frozen_helper_launches_sibling_viewer_with_independent_runtime(self):
        executable = str(Path(tempfile.gettempdir())/'Codex额度联动.exe')
        expected = str(Path(executable).with_name('Codex额度查看器.exe'))
        with patch('sys.frozen', True, create=True), patch('sys.executable', executable), \
                patch('subprocess.Popen') as launch:
            self.assertEqual(viewer_command(), [expected, '--follow-codex'])
            launch_viewer()
            arguments, keywords = launch.call_args
            self.assertEqual(arguments[0], [expected, '--follow-codex'])
            self.assertEqual(keywords['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')

    def test_transition_signals_and_no_relaunch_after_manual_viewer_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = [0]
            hosts = [HOST]
            signal, launch = Mock(), Mock(return_value=Mock(poll=Mock(return_value=None)))
            status = Path(temporary) / 'status.json'
            follower = CodexFollower(lambda: hosts[0], launch, signal, status, lambda: now[0])
            follower.tick()
            signal.set_absent.assert_called_once_with(False)
            launch.return_value.poll.return_value = 0  # 用户退出 GUI
            now[0] = 1
            follower.tick()
            self.assertEqual(launch.call_count, 1)
            hosts[0] = ()
            follower.tick()
            now[0] = 3
            follower.tick()
            signal.set_absent.assert_called_with(True)
            self.assertEqual(json.loads(status.read_text())['state'], 'waiting')
            hosts[0] = HOST
            now[0] = 4
            follower.tick()
            self.assertEqual(launch.call_count, 2)

    def test_launch_errors_have_bounded_retry_and_cancel_on_codex_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            now, hosts = [0], [HOST]
            launch = Mock(side_effect=OSError('test launch error'))
            follower = CodexFollower(lambda: hosts[0], launch, Mock(), Path(temporary)/'status.json', lambda: now[0])
            follower.tick()
            now[0] = 5
            follower.tick()
            now[0] = 10
            follower.tick()
            now[0] = 100
            follower.tick()
            self.assertEqual(launch.call_count, 3)
            hosts[0] = ()
            follower.tick()
            now[0] = 102
            follower.tick()
            self.assertIsNone(follower.retry_at)


@unittest.skipUnless(os.name == 'nt', 'Windows native IPC')
class NativeSignalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='quota-follow-unit-')
        self.environment = patch.dict(os.environ, {'CODEXQUOTA_INSTANCE_SCOPE': self.temporary.name,
                                                   'APPDATA': self.temporary.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_exit_signal_is_resettable_and_late_clients_can_receive_it(self):
        client = CodexExitSignal()
        owner = CodexExitSignal(owner=True)
        try:
            self.assertFalse(client.requested())
            owner.set_absent(True)
            self.assertTrue(client.requested())
            late = CodexExitSignal()
            try:
                self.assertTrue(late.requested())
                owner.set_absent(False)
                self.assertFalse(client.requested())
                self.assertFalse(late.requested())
            finally:
                late.close()
        finally:
            owner.close()
            client.close()

    def test_duplicate_watcher_does_not_request_show_or_block_viewer(self):
        owner = SingleInstance('CodexFollower', notify_existing=False)
        duplicate = SingleInstance('CodexFollower', notify_existing=False)
        viewer = SingleInstance()
        try:
            self.assertTrue(owner.acquire())
            self.assertTrue(instance_exists('CodexFollower'))
            self.assertFalse(duplicate.acquire())
            self.assertTrue(viewer.acquire())
            self.assertFalse((Path(self.temporary.name)/'CodexQuotaViewer'/'show-window.request').exists())
        finally:
            duplicate.release()
            owner.release()
            viewer.release()
        self.assertFalse(instance_exists('CodexFollower'))


if __name__ == '__main__':
    unittest.main()
