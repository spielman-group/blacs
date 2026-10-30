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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
    def sleep(self):
        (Path(self.directory) / 'started').touch()
        time.sleep(60)

    def shutdown(self):
        # Stands in for releasing hardware, such as clearing NI-DAQmx tasks.
        (Path(self.directory) / 'released').touch()


class HangingWorker(CleanupWorker):
    def shutdown(self):
        (Path(self.directory) / 'started').touch()
        threading.Event().wait()


class CleanupTab(Tab):
    def __init__(self, notebook, settings, restart=False):
        super().__init__(notebook, settings, restart)
        self.create_worker('main', settings['worker'], {'directory': settings['directory']})

    @define_state(MODE_MANUAL, True)
    def sleep(self):
        yield self.queue_work('main', 'sleep')

    @define_state(MODE_MANUAL, True)
    def fail(self):
        raise RuntimeError('A fatal error')


def quit_blacs(tab):
    # What BLACS's quit does once the workers' shutdown state has run or timed out.
    inthread(tab.finalise_close_tab, tab.close_tab(finalise=False))


ENDS = {
    'restart': lambda tab: tab.restart(),
    'stop': lambda tab: tab._ui.button_stop.click(),
    'quit': quit_blacs,
    'fatal error': lambda tab: tab.fail(),
    'hanging shutdown': quit_blacs,
}

# Starts a worker, idle or in a sleeping job, so that the test can kill its parent:
PARENT = '''
import sys, time
from labscript_utils.ls_zprocess import ProcessTree
from test_state_machine import CleanupWorker
to_worker, from_worker = CleanupWorker(ProcessTree.instance()).start(
    'main', 'device', {'directory': sys.argv[1]}
)
for job in ['init'] + sys.argv[2:]:
    to_worker.put((job, (), {}))
    from_worker.get()
print('ready', flush=True)
time.sleep(60)
'''


class WorkerCleanupTests(unittest.TestCase):
    def make_directory(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_however_a_worker_is_ended_its_shutdown_releases_the_hardware(self):
        notebook = DragDropTabWidget()
        # A fatal error cannot happen while a job runs, nor a hanging
        # shutdown() in a job be cut off.
        cases = [(end, busy) for end in ENDS for busy in (False, True)
                 if not busy or end in ('restart', 'stop', 'quit')]
        tabs = {}
        for end, busy in cases:
            worker = HangingWorker if end == 'hanging shutdown' else CleanupWorker
            settings = {'device_name': f'{end} {busy}', 'connection_table': ConnectionTable(),
                        'worker': worker, 'directory': self.make_directory()}
            tabs[end, busy] = CleanupTab(notebook, settings)
        wait_until(lambda: all(tab.workers['main'][1] and tab.state == 'idle' for tab in tabs.values()))
        for (end, busy), tab in tabs.items():
            if busy:
                tab.sleep()
                wait_until(lambda: (tab.settings['directory'] / 'started').exists())
        children = {}
        for (end, busy), tab in tabs.items():
            children[end, busy] = tab.workers['main'][0].child
            ENDS[end](tab)
        wait_until(lambda: all(child.poll() is not None for child in children.values()))
        for (end, busy), child in children.items():
            directory = tabs[end, busy].settings['directory']
            with self.subTest(end=end, busy=busy):
                # Exited by itself, before the tab would have killed it:
                self.assertEqual(child.returncode, 0)
                if end == 'hanging shutdown':
                    self.assertTrue((directory / 'started').exists())
                    self.assertFalse((directory / 'released').exists())
                else:
                    self.assertTrue((directory / 'released').exists())
        for (end, busy), tab in tabs.items():
            if end in ('restart', 'stop', 'fatal error'):
                tab.close_tab()

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
        wait_until(lambda: all((d / 'released').exists() for d in directories), timeout=60)


if __name__ == '__main__':
    unittest.main()
