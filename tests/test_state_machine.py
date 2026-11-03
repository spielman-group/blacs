#####################################################################
#                                                                   #
# /tests/test_state_machine.py                                      #
#                                                                   #
# Copyright 2026, JQI                                               #
# Author: Ian Spielman                                              #
#                                                                   #
# This file is part of blacs, in the labscript suite                #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from qtutils import inthread
from qtutils.qt.QtWidgets import QApplication
from labscript_utils.qtwidgets.dragdroptab import DragDropTabWidget

from blacs.tab_base_classes import MODE_MANUAL, Tab, Worker, define_state


def setUpModule():
    # Here and not at import, which the worker processes do too. Held for the life of
    # the process: a garbage-collected QApplication takes the widgets with it.
    global _qapplication
    _qapplication = QApplication.instance() or QApplication([])


def wait_until(condition, timeout=30):
    # The state machine runs its states from Qt events.
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out')
        QApplication.processEvents()
        time.sleep(0.01)


class ConnectionTable:
    """A device with no connection, so a tab builds without a lab's table."""

    def find_by_name(self, device_name):
        class Connection:
            BLACS_connection = 'None'
            properties = {}
        return Connection()


class StaleJobTab(Tab):
    next_state_ran = False

    @define_state(MODE_MANUAL, True)
    def finish_before_yielding(self):
        # A job found stale before it starts.
        return
        yield

    @define_state(MODE_MANUAL, True)
    def next_state(self):
        self.next_state_ran = True


class MainloopTests(unittest.TestCase):
    def test_a_state_that_finishes_before_yielding_leaves_the_mainloop_running(self):
        tab = StaleJobTab(
            DragDropTabWidget(),
            {'device_name': 'device', 'connection_table': ConnectionTable()},
        )
        tab.finish_before_yielding()
        tab.next_state()
        wait_until(lambda: tab.next_state_ran, timeout=5)
        tab.close_tab()


class CleanupWorker(Worker):
    def init(self):
        self.allocate()

    def allocate(self):
        self.record('a')

    def sleep(self):
        (Path(self.directory) / 'started').touch()
        time.sleep(60)

    def shutdown(self):
        self.record('r')

    def fail_job(self):
        raise RuntimeError('A worker method failed')

    @property
    def die_before_ack(self):
        os._exit(1)

    def die_after_ack(self):
        os._exit(1)

    def record(self, event):
        # Stands in for hardware, such as NI-DAQmx tasks: a log of each allocation and
        # each release.
        with open(Path(self.directory) / 'hardware', 'a') as log:
            log.write(event)


class RaisingWorker(CleanupWorker):
    def shutdown(self):
        super().shutdown()
        raise RuntimeError('An error after the release')


class SlowWorker(CleanupWorker):
    def shutdown(self):
        # Outlasts the tab's wait for it, after which the tab ends the worker:
        time.sleep(Tab.SHUTDOWN_TIMEOUT + 1)
        super().shutdown()


class HangingWorker(CleanupWorker):
    def shutdown(self):
        self.record('h')
        threading.Event().wait()


class LateQuitWorker(CleanupWorker):
    @property
    def sleep(self):
        # Looked up once the job's request is taken, before the job starts: holds the
        # job there until the worker is told to quit.
        (Path(self.directory) / 'started').touch()
        while not self._quitting.locked():
            time.sleep(0.01)
        return super().sleep


class CleanupTab(Tab):
    def __init__(self, notebook, settings, restart=False):
        super().__init__(notebook, settings, restart)
        self.create_worker('main', settings['worker'], {'directory': settings['directory']})

    @define_state(MODE_MANUAL, True)
    def allocate(self):
        yield self.queue_work('main', 'allocate')

    @define_state(MODE_MANUAL, True)
    def sleep(self):
        yield self.queue_work('main', 'sleep')

    @define_state(MODE_MANUAL, True)
    def fail(self):
        raise RuntimeError('A fatal error')

    @define_state(MODE_MANUAL, True)
    def run_job(self, method):
        self.result = yield self.queue_work('main', method)


def quit_blacs(tab):
    # What BLACS's quit does, closing the tab at the next of its 100 ms polls.
    tab.shutdown_workers()
    wait_until(lambda: tab.shutdown_workers_complete)
    poll = time.monotonic() + 0.1
    wait_until(lambda: time.monotonic() > poll)
    inthread(tab.finalise_close_tab, tab.close_tab(finalise=False))


def quit_as_a_job_starts(tab):
    tab.sleep()
    wait_until(lambda: (tab.settings['directory'] / 'started').exists())
    quit_blacs(tab)


def quit_during_restart(tab):
    tab.restart()
    wait_until(lambda: hasattr(tab, '_restart_thread') and tab._restart_thread.is_alive())
    quit_blacs(tab)
    wait_until(lambda: not tab._restart_thread.is_alive())
    assert not tab.workers, 'F15: a quitting restart must not create replacement workers'


ENDS = {
    'restart': lambda tab: tab.restart(),
    'stop': lambda tab: tab._ui.button_stop.click(),
    'quit': quit_blacs,
    'quit with a state queued': lambda tab: (tab.allocate(), quit_blacs(tab)),
    'quit during a restart': quit_during_restart,
    'quit as a job starts': quit_as_a_job_starts,
    'fatal error': lambda tab: tab.fail(),
    'raising shutdown': quit_blacs,
    'slow shutdown': quit_blacs,
    'hanging shutdown': quit_blacs,
}
WORKERS = {
    'quit during a restart': SlowWorker,
    'quit as a job starts': LateQuitWorker,
    'raising shutdown': RaisingWorker,
    'slow shutdown': SlowWorker,
    'hanging shutdown': HangingWorker,
}

# Starts a worker, idle or in a sleeping job, so that the test can kill its parent:
PARENT = '''
import sys, time
from labscript_utils.ls_zprocess import ProcessTree
from test_state_machine import CleanupWorker
to_worker, from_worker = CleanupWorker(ProcessTree.instance()).start(
    'main', 'device', {'directory': sys.argv[1]}
)
to_worker.put(('init', (), {}))
# The job's start, then its end, once the hardware is allocated:
from_worker.get()
from_worker.get()
for job in sys.argv[2:]:
    to_worker.put((job, (), {}))
print('ready', flush=True)
time.sleep(60)
'''


class WorkerCleanupTests(unittest.TestCase):
    def make_directory(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_unconfirmed_exit_keeps_the_controls_and_blocks_replacement_until_retry(self):
        directory = self.make_directory()
        tab = CleanupTab(DragDropTabWidget(), {
            'device_name': 'uncertain', 'connection_table': ConnectionTable(),
            'worker': CleanupWorker, 'directory': directory,
        })
        self.addCleanup(tab.close_tab)
        wait_until(lambda: tab.workers['main'][1] and tab.state == 'idle')
        child = tab.workers['main'][0].child
        # The process-server observation is unavailable; the worker transport is real.
        with mock.patch.object(child, 'poll', side_effect=TimeoutError('offline')), \
             mock.patch.object(child, 'kill', side_effect=TimeoutError('offline')):
            tab.remote_process_client = types.SimpleNamespace(host='control-host')
            try:
                tab._ui.button_stop.click()
                wait_until(lambda: tab._ui.button_restart.isEnabled())
                self.assertEqual(tab.state, 'fatal error')
                self.assertNotIn('Worker processes stopped', tab._ui.error_message.toPlainText())
                tab._ui.button_restart.click()
                wait_until(lambda: not tab._restart_thread.is_alive())
                self.assertGreaterEqual(tab.notebook.indexOf(tab._ui), 0)
                self.assertTrue(tab._ui.button_restart.isEnabled())
                text = tab._ui.error_message.toPlainText()
                for word in ('main', 'control-host', 'No replacement', 'Restore', 'Restart'):
                    self.assertIn(word, text)
                self.assertEqual((directory / 'hardware').read_text(), 'ar')
            finally:
                tab.remote_process_client = None
        tab._ui.button_restart.click()
        wait_until(lambda: not tab._restart_thread.is_alive()
                   and tab.workers['main'][1] and tab.state == 'idle')
        self.assertEqual((directory / 'hardware').read_text(), 'ara')

    def test_a_method_error_returns_none_but_a_missing_method_is_fatal(self):
        tab = CleanupTab(DragDropTabWidget(), {
            'device_name': 'errors', 'connection_table': ConnectionTable(),
            'worker': CleanupWorker, 'directory': self.make_directory(),
        })
        self.addCleanup(tab.close_tab)
        wait_until(lambda: tab.workers['main'][1] and tab.state == 'idle')
        tab.result = 'not returned'
        tab.run_job('fail_job')
        wait_until(lambda: tab.result is None)
        self.assertIn('RuntimeError', tab._ui.error_message.toPlainText())
        self.assertEqual(tab.state, 'idle')
        tab.run_job('missing_method')
        wait_until(lambda: tab.state == 'fatal error')
        self.assertIn('missing_method', tab.error_message)

    def test_a_worker_dying_before_either_reply_reports_an_error(self):
        for method in ('die_before_ack', 'die_after_ack'):
            with self.subTest(method=method):
                tab = CleanupTab(DragDropTabWidget(), {
                    'device_name': method, 'connection_table': ConnectionTable(),
                    'worker': CleanupWorker, 'directory': self.make_directory(),
                })
                self.addCleanup(tab.close_tab)
                wait_until(lambda: tab.workers['main'][1] and tab.state == 'idle')
                tab.run_job(method)
                wait_until(lambda: tab.state == 'fatal error', timeout=5)
                self.assertIn('exit', tab.error_message.lower())

    def test_however_a_worker_is_ended_its_shutdown_releases_the_hardware_once(self):
        notebook = DragDropTabWidget()
        # A fatal error cannot happen while a job runs, nor a hanging
        # shutdown() in a job be cut off.
        cases = [(end, busy) for end in ENDS for busy in (False, True)
                 if not busy or end in ('restart', 'stop', 'quit')]
        tabs = {}
        for end, busy in cases:
            settings = {'device_name': f'{end} {busy}', 'connection_table': ConnectionTable(),
                        'worker': WORKERS.get(end, CleanupWorker),
                        'directory': self.make_directory()}
            tabs[end, busy] = CleanupTab(notebook, settings)
        def ready(tab):
            return tab.workers['main'][1] and tab.state == 'idle'
        wait_until(lambda: all(ready(tab) for tab in tabs.values()))
        for (end, busy), tab in tabs.items():
            if busy:
                tab.sleep()
                wait_until(lambda: (tab.settings['directory'] / 'started').exists())
        children = [tab.workers['main'][0].child for tab in tabs.values()]
        for (end, busy), tab in tabs.items():
            ENDS[end](tab)
        wait_until(lambda: all(child.poll() is not None for child in children))
        for (end, busy), tab in tabs.items():
            if 'restart' in end:
                wait_until(lambda: not tab._restart_thread.is_alive())
            if tab.workers:
                # Rebuilt by its restart: ended in turn once it is up.
                wait_until(lambda: ready(tab))
                children.append(tab.workers['main'][0].child)
            if end in ('restart', 'stop', 'fatal error') or tab.workers:
                tab.close_tab()
        wait_until(lambda: all(child.poll() is not None for child in children))
        # Each exited by itself, before its tab would have killed it:
        self.assertEqual([child.returncode for child in children], [0] * len(children))
        for (end, busy), tab in tabs.items():
            with self.subTest(end=end, busy=busy):
                expected = {'restart': 'arar', 'hanging shutdown': 'ah'}.get(end, 'ar')
                hardware = (tab.settings['directory'] / 'hardware').read_text()
                self.assertEqual(hardware, expected)

    def test_a_worker_whose_parent_is_lost_releases_the_hardware(self):
        directories = []
        for jobs in ([], ['sleep']):
            directory = self.make_directory()
            parent = subprocess.Popen(
                [sys.executable, '-c', PARENT, str(directory), *jobs],
                cwd=Path(__file__).parent, stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(parent.stdout.close)
            self.assertEqual(parent.stdout.readline(), 'ready\n')
            if jobs:
                wait_until(lambda: (directory / 'started').exists())
            parent.kill()
            parent.wait()
            directories.append(directory)
        # Unanswered heartbeats take about 10 s to count as a lost parent.
        wait_until(lambda: all((d / 'hardware').read_text() == 'ar' for d in directories), timeout=60)


if __name__ == '__main__':
    unittest.main()
