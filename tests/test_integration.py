"""The runmanager/BLACS shot exchange, with both real implementations talking.

Each repository's own tests stand in for the other side, so neither can catch
the two halves drifting apart. Here both run in one process: a real
``RunmanagerServer`` and ``BlacsServer`` on free local ports, reached by the
real clients, over ``RunManager``'s real exchange methods, ``QueueManager`` and
``ShotExecutor``'s real shot loop. Faked is what cannot run in a test:
runmanager's startup, whose exchange methods are borrowed onto a plain object,
and the apparatus (the connection table check and the device tabs).
"""
import os
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest

import labscript_utils.h5_lock  # must precede h5py, as it does in BLACS itself
import h5py
from qtutils.qt.QtWidgets import QApplication

import fixtures
import runmanager.__main__
from runmanager.__main__ import RunManager, RunmanagerServer
from runmanager.blacs_status import blacs_state
from runmanager.client import RunmanagerClient
from runmanager.queueing import QueueController, QueueManager

# fixtures has already imported BLACS, with its splash stubbed.
from fixtures import BlacsServer, make_executor
import blacs.__main__
from blacs.client import BlacsClient

from blacs import shot_execution
from blacs.shot_execution import ShotExecutor


# BLACS's own exchange client waits this long for runmanager's reply.
REPLY_TIMEOUT = 1

_qapplication = None


def ensure_qapplication():
    global _qapplication
    if QApplication.instance() is None:
        # Held for the life of the process: a garbage-collected QApplication
        # takes the Qt machinery with it.
        _qapplication = QApplication([])


def free_port():
    # Passed to each server, which would otherwise bind the labconfig's port.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class FakeOutputBox:
    """What runmanager shows its user, which is where protocol trouble shows."""

    def __init__(self):
        self.lines = []

    def output(self, text, red=False):
        self.lines.append(text)

    def said(self, *words):
        return [line for line in self.lines if all(word in line for word in words)]


class FakeAnalysisSubmission:
    """Where a completed shot goes on to lyse."""

    def __init__(self):
        self.submitted = []

    def notify_shot_complete(self, path):
        self.submitted.append(path)


class RunmanagerApp:
    """Runmanager's exchange, over the surface of it the exchange uses.

    ``RunManager.__init__`` builds the whole application, so this holds the few
    things the exchange reaches for. The exchange methods are RunManager's own
    and the queue is a real QueueManager, so the rules applied are runmanager's;
    only producing a default shot and submitting to lyse are stood in for.

    Supply every attribute the borrowed methods reach for, not only those these
    tests do: a name missing here surfaces as an AttributeError from inside
    runmanager's code, far from the test that provoked it.
    """

    queue_exchange = RunManager.queue_exchange
    apply_shot_outcome = RunManager.apply_shot_outcome
    offer_shot = RunManager.offer_shot

    def __init__(self):
        self.output_box = FakeOutputBox()
        self.queue_controller = QueueController()
        self.queue_manager = QueueManager(
            self.queue_controller,
            lambda item, default_globals: None,
            lambda labscript_file, path: True,
            lambda path: None,
            self.output_box.output,
        )
        self.analysis_submission = FakeAnalysisSubmission()
        self.default_shot_files = []
        self.default_shots_taken = 0

    def take_default_shot(self, labscript_file):
        self.default_shots_taken += 1
        # None when no prepared default shot is left, which is runmanager's
        # answer while the compile it started has not finished.
        return self.default_shot_files.pop(0) if self.default_shot_files else None

    def discard_default_shot(self):
        self.default_shot_files = []

    def rows(self):
        return self.queue_controller.get_queue_display_items()


class FakeTab:
    """The master pseudoclock, for a shot with no devices in it."""

    def __init__(self, on_start_run):
        self.on_start_run = on_start_run

    def start_run(self, notifications):
        self.on_start_run()
        notifications.put('done')


class FakeFrontPanelSettings:
    def get_save_data(self):
        return {}, {}, {}, {}

    def store_front_panel_in_h5(self, hdf5_file, *args, **kwargs):
        pass


class FakeConfig(fixtures.FakeConfig):
    def getfloat(self, section, option, fallback=None):
        # Longer than a stalled reply, so a runmanager still busy with one is
        # not taken for absent, and short enough to give up on one that is.
        return 3 * REPLY_TIMEOUT if option == 'liveness_timeout' else fallback


class FakeBLACS(fixtures.FakeBLACS):
    """A BLACS with no device tabs, so a shot can run without hardware."""

    exp_config = FakeConfig()

    def __init__(self, on_start_run):
        self.front_panel_settings = FakeFrontPanelSettings()
        self.tablist = {'pseudoclock': FakeTab(on_start_run)}


class IntegrationFixture:
    """One runmanager and one BLACS, talking through their real servers."""

    def setUp(self):
        ensure_qapplication()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

        self.runmanager = RunmanagerApp()
        self.addCleanup(self.runmanager.queue_manager.shutdown)
        self.real_runmanager_app = getattr(runmanager.__main__, 'app', None)
        runmanager.__main__.app = self.runmanager
        self.addCleanup(self.restore_runmanager_app)
        self.runmanager_server = RunmanagerServer(
            port=free_port(), bind_address='tcp://127.0.0.1'
        )
        self.addCleanup(self.runmanager_server.shutdown)
        port = self.runmanager_server.port

        class FixtureRunmanagerClient(RunmanagerClient):
            # A client BLACS builds for a deadline of its own reaches this
            # runmanager, not the labconfig's.
            def __init__(self, timeout=REPLY_TIMEOUT):
                super().__init__(host='127.0.0.1', port=port, timeout=timeout)

        shot_execution.RunmanagerClient = FixtureRunmanagerClient
        self.addCleanup(setattr, shot_execution, 'RunmanagerClient', RunmanagerClient)

        # What the apparatus does with each shot, in the order they arrive:
        # 'completed' runs it to the end; anything else is a shot BLACS could
        # not take up at all.
        self.outcomes = []
        # Whether the operator presses Abort while the next shot is under way,
        # which exercises the latch a shot failing on the hardware sets.
        self.abort_next_shot = False
        # Whether BLACS is closed while the next shot is under way.
        self.close_during_next_shot = False
        self.shots_run = []
        self.while_running = []
        self.passes_left = 0
        self.executor = self.make_executor()

        self.blacs_server = BlacsServer(
            types.SimpleNamespace(shot_executor=self.executor),
            port=free_port(),
            bind_address='tcp://127.0.0.1',
        )
        self.addCleanup(self.blacs_server.shutdown)
        self.status_client = BlacsClient(
            host='127.0.0.1', port=self.blacs_server.port, timeout=1
        )

        self.real_process_tree = shot_execution.process_tree
        self.real_sleep = shot_execution.time.sleep
        shot_execution.process_tree = types.SimpleNamespace(
            zlock_client=types.SimpleNamespace(set_thread_name=lambda name: None)
        )

    def tearDown(self):
        shot_execution.process_tree = self.real_process_tree
        shot_execution.time.sleep = self.real_sleep

    def restore_runmanager_app(self):
        if self.real_runmanager_app is None:
            del runmanager.__main__.app
        else:
            runmanager.__main__.app = self.real_runmanager_app

    def make_executor(self):
        executor = make_executor(
            blacs=FakeBLACS(self.on_start_run), logger_name='test.integration'
        )
        executor._manager_running = True
        executor._runmanager_request_client = RunmanagerClient(
            host='127.0.0.1', port=self.runmanager_server.port, timeout=REPLY_TIMEOUT
        )
        real_alive = executor.runmanager_alive

        def alive_and_counted(*args, **kwargs):
            # The loop checks that runmanager is there once per pass with no
            # shot in hand, so this counts a pass whether or not it answers.
            self.count_pass()
            return real_alive(*args, **kwargs)

        executor.runmanager_alive = alive_and_counted
        executor.master_pseudoclock = 'pseudoclock'
        executor.process_request = self.process_request
        return executor

    def process_request(self, h5_filepath):
        """Stand in for the connection-table check and the rerun copy.

        This is where the apparatus begins, and where a shot's scripted outcome
        is applied; everything above it is real.
        """
        self.shots_run.append(h5_filepath)
        outcome = self.outcomes.pop(0) if self.outcomes else 'completed'
        if outcome != 'completed':
            return None, outcome
        return h5_filepath, 'Experiment added successfully\n'

    def on_start_run(self):
        """Called while a shot is under way, before it finishes."""
        if self.abort_next_shot:
            self.abort_next_shot = False
            # What the Abort button queues. The loop reads it in the same turn
            # as the shot finishing, and an abort takes precedence.
            self.executor.notify_queue.put(['Shot Executor', 'abort'])
        if self.close_during_next_shot:
            self.close_during_next_shot = False
            self.executor.stop()  # what closing the window does
        self.while_running.append(
            {
                'rows': self.runmanager.rows(),
                'status': self.status_client.get_status(),
            }
        )

    def make_shot_file(self, name):
        path = os.path.join(self.directory, name)
        with h5py.File(path, 'w') as h5_file:
            # With no devices, every device loop in the shot executor is empty,
            # so the shot runs without hardware.
            h5_file.create_group('devices')
        return path

    def count_pass(self):
        self.passes_left -= 1
        if self.passes_left <= 0:
            # The loop reads this at the top of each pass, so this pass still
            # runs to the end, shot and all.
            self.executor._manager_running = False

    def run_loop(self, passes=1):
        """Run the real shot loop for a given number of passes.

        A pass is one turn with no shot in hand: an exchange, then whatever it
        produced. The loop is stopped by pass count because a BLACS falling back
        to its local override shot never runs out of work.
        """
        self.passes_left = passes
        # A pass is counted by its exchange, so a BLACS that stopped exchanging
        # would spin for ever with sleep stubbed out. Bound the turns too,
        # generously, so a hang becomes failed assertions.
        turns = [0]
        limit = 50 * max(1, passes)

        def slept(seconds):
            turns[0] += 1
            if turns[0] > limit:
                self.executor._manager_running = False

        # Rebind the name in shot_execution, which does `import time`; assigning
        # to time.sleep would replace it for every thread in the interpreter.
        self.real_time = shot_execution.time
        self.addCleanup(setattr, shot_execution, 'time', self.real_time)
        shot_execution.time = types.SimpleNamespace(
            sleep=slept, time=self.real_time.time, monotonic=self.real_time.monotonic
        )
        self.executor._manager_running = True
        ShotExecutor._manage(self.executor)

    def lose_next_request(self):
        """Let the next exchange reach runmanager without being applied, and
        answer only after BLACS has stopped waiting for it."""

        def stall_unapplied(*args, **kwargs):
            del self.runmanager.queue_exchange
            time.sleep(2 * REPLY_TIMEOUT)

        self.runmanager.queue_exchange = stall_unapplied

    def lose_next_reply(self):
        """Let the next exchange reach runmanager and change its queue, and
        answer only after BLACS has stopped waiting for it."""

        def exchange_then_stall(*args, **kwargs):
            del self.runmanager.queue_exchange
            response = self.runmanager.queue_exchange(*args, **kwargs)
            time.sleep(2 * REPLY_TIMEOUT)
            return response

        self.runmanager.queue_exchange = exchange_then_stall


class SuccessfulShotTests(IntegrationFixture, unittest.TestCase):
    """The path a shot takes when everything works."""

    def test_a_queued_shot_runs_is_removed_and_the_next_one_is_offered(self):
        first = self.make_shot_file('shot_a.h5')
        second = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': first, 'compiled': True}, {'path': second, 'compiled': True}]
        )
        shot_ids = [row['shot_id'] for row in self.runmanager.queue_controller.get_save_data()['items']]

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(
            self.shots_run, [first, second], 'both queued shots ran, in order'
        )
        self.assertEqual(
            self.runmanager.rows(), [], 'a completed shot leaves the queue'
        )
        self.assertEqual(
            [os.path.basename(path) for path in self.runmanager.analysis_submission.submitted],
            ['shot_a.h5', 'shot_b.h5'],
            'and is forwarded for analysis',
        )
        # The row being executed stays in the queue, marked running, under the
        # id it was offered with.
        running = [
            [(row['path'], row['state']) for row in pass_['rows']]
            for pass_ in self.while_running
        ]
        self.assertEqual(
            running,
            [[(first, 'running'), (second, '')], [(second, 'running')]],
            'the running shot is visible in the queue and the rest waits behind it',
        )
        self.assertEqual(
            [pass_['status']['shot_id'] for pass_ in self.while_running],
            shot_ids,
            'BLACS names the id runmanager offered, unchanged, while it runs',
        )


class LostReplyTests(IntegrationFixture, unittest.TestCase):
    """A row still marked running is offered again, and that is safe.

    The inference spans both applications: BLACS is sequential, asks for a shot
    only when idle, and carries the last shot's outcome on the same exchange, so
    a request that does not retire the row runmanager marked running proves this
    BLACS is not running it.
    """

    def test_a_lost_offer_reply_costs_a_pass_and_not_the_shot(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        offered_id = self.runmanager.queue_controller.get_save_data()['items'][0]['shot_id']
        self.lose_next_reply()

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(
            self.shots_run, [shot], 'the shot ran exactly once, not never and not twice'
        )
        self.assertEqual(
            self.while_running[0]['status']['shot_id'],
            offered_id,
            'and under the id the lost offer had already given it',
        )
        self.assertTrue(
            self.runmanager.output_box.said('still marked as running', 'again'),
            'runmanager says it is handing the row out again',
        )
        self.assertEqual(self.runmanager.rows(), [])
        self.assertEqual(
            len(self.runmanager.analysis_submission.submitted),
            1,
            'and one run of it reached lyse',
        )


class FailureLatchTests(IntegrationFixture, unittest.TestCase):
    """A shot the apparatus could not finish stops it, and is retried by hand."""

    def test_an_aborted_shot_stops_requests_stays_red_and_is_retried(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        offered_id = self.runmanager.queue_controller.get_save_data()['items'][0]['shot_id']
        self.abort_next_shot = True

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        self.assertFalse(
            self.executor.requesting_shots,
            'the apparatus stopped mid-shot, so BLACS waits for an operator',
        )
        self.assertIn('Aborted', self.executor.local_error)
        rows = self.runmanager.rows()
        self.assertEqual(
            [(row['path'], row['state']) for row in rows],
            [(shot, 'failed'), (later, '')],
            'the shot stays at the head of the queue, in red',
        )
        self.assertIn('Aborted', rows[0]['tooltip'], 'with the reason BLACS gave')
        self.assertEqual(
            self.runmanager.analysis_submission.submitted,
            [],
            'a shot that did not run is not analysed',
        )
        self.assertEqual(self.shots_run, [shot], 'and no later shot was started')
        state, _ = blacs_state(self.status_client.get_status())
        self.assertIn('stopped', state)
        self.assertIn('Aborted', state, 'runmanager can read why BLACS stopped')

        # Asking for shots again acknowledges the error, and runmanager offers
        # the same row back under the same id.
        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertEqual(self.shots_run, [shot, shot, later])
        self.assertEqual(
            self.while_running[0]['status']['shot_id'],
            offered_id,
            'the retry is the same queue row, under the same stable id',
        )
        self.assertEqual(self.runmanager.rows(), [])


class RejectedShotTests(IntegrationFixture, unittest.TestCase):
    """A shot BLACS cannot read is runmanager's to fix, and stops nothing.

    A file that has gone, or a connection table that does not match. Nothing
    about the apparatus is wrong, so BLACS keeps asking; runmanager holds that
    row and stops offering it, so the two do not trade the same refusal once a
    second.
    """

    def test_closing_blacs_during_a_shot_aborts_it_and_tells_runmanager(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        self.close_during_next_shot = True

        self.executor.requesting_shots = True
        self.run_loop(passes=3)
        # What the loop's thread does on its way out:
        self.executor._deliver_or_name_held_outcome()

        self.assertEqual(self.shots_run, [shot], 'the loop ended with the shot')
        rows = self.runmanager.rows()
        self.assertEqual([(row['path'], row['state']) for row in rows], [(shot, 'failed')])
        self.assertIn('Aborted', rows[0]['tooltip'])
        self.assertEqual(self.runmanager.analysis_submission.submitted, [])

    def test_blacs_reports_it_and_carries_on(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        self.outcomes = ['H5 file not accessible to Control PC\n']

        self.executor.requesting_shots = True
        self.run_loop(passes=3)

        self.assertTrue(
            self.executor.requesting_shots, 'the apparatus was never the problem'
        )
        self.assertIsNone(self.executor.local_error)
        rows = self.runmanager.rows()
        self.assertEqual(
            [(row['path'], row['state']) for row in rows],
            [(shot, 'rejected'), (later, '')],
            'the row is held, and held apart from a shot that merely failed',
        )
        self.assertIn('H5 file not accessible', rows[0]['tooltip'])
        self.assertEqual(
            self.shots_run,
            [shot],
            'and it is not offered again, however many times BLACS asks',
        )


class LocalOverrideTests(IntegrationFixture, unittest.TestCase):
    """When runmanager offers nothing, BLACS runs its own local override shot.

    That shot is in no runmanager queue, so its completions retire no row and
    reach no lyse.
    """

    def test_a_paused_queue_leaves_blacs_running_its_own_shot(self):
        queued = self.make_shot_file('shot_a.h5')
        override = self.make_shot_file('override.h5')
        self.runmanager.queue_manager.enqueue([{'path': queued, 'compiled': True}])
        self.runmanager.queue_manager.set_paused(True)

        self.executor.requesting_shots = True
        self.run_loop(passes=1)
        self.assertEqual(
            link_state(self.executor),
            'Runmanager queue paused',
            'the light says why no queued work is arriving',
        )

        self.executor._ui.local_override_lineEdit.setText(override)
        self.run_loop(passes=2)

        self.assertEqual(
            self.shots_run,
            [override, override],
            'the apparatus keeps working on the local override shot',
        )
        self.assertTrue(
            self.executor.requesting_shots,
            'a paused runmanager is not telling this apparatus to stop',
        )
        self.assertIsNone(self.executor.local_error, 'nothing here needs attention')
        self.assertEqual(
            [(row['path'], row['state']) for row in self.runmanager.rows()],
            [(queued, '')],
            'the withheld shot is untouched, and is not marked running',
        )
        self.assertEqual(self.runmanager.analysis_submission.submitted, [])
        self.assertEqual(self.runmanager.output_box.said('completed'), [])
        self.assertIsNone(
            self.while_running[0]['status']['shot_id'],
            'nor does BLACS claim it is running a queued shot',
        )

        # A runmanager that cannot be reached has offered nothing either.
        self.runmanager_server.shutdown()
        self.run_loop(passes=1)

        self.assertEqual(self.shots_run, [override] * 3)
        self.assertTrue(self.executor.requesting_shots)
        self.assertIsNone(self.executor.local_error)

    def test_a_local_override_shot_that_cannot_run_stops_requests_silently(self):
        override = self.make_shot_file('override.h5')
        self.executor._ui.local_override_lineEdit.setText(override)
        self.outcomes = ['Not a valid run file\n']

        self.executor.requesting_shots = True
        self.run_loop(passes=1)

        self.assertFalse(self.executor.requesting_shots)
        self.assertIn('Not a valid run file', self.executor.local_error)
        self.assertIn('Rejected local override shot', self.executor.get_status())
        self.assertEqual(
            self.runmanager.output_box.said('rejected'),
            [],
            'a local override shot has no row in runmanager to report against',
        )


class RequestShotsOffTests(IntegrationFixture, unittest.TestCase):
    """With Request shots off, BLACS takes up no new shot."""

    def test_no_shot_is_taken_up_while_not_requesting_shots(self):
        queued = self.make_shot_file('shot_a.h5')
        override = self.make_shot_file('override.h5')
        self.runmanager.queue_manager.enqueue([{'path': queued, 'compiled': True}])
        self.executor._ui.local_override_lineEdit.setText(override)

        # The loop asks runmanager nothing, so no pass is counted and the
        # bound on its turns is what stops it.
        self.run_loop(passes=1)

        self.assertEqual(self.shots_run, [], 'not even the local override shot')
        self.assertEqual(self.passes_left, 1, 'and runmanager is not even asked')
        self.assertEqual(
            [(row['path'], row['state']) for row in self.runmanager.rows()],
            [(queued, '')],
        )
        self.assertEqual(link_state(self.executor), 'Not requesting shots')

    def test_the_last_outcome_still_goes_out_after_requests_are_switched_off(self):
        shot = self.make_shot_file('shot_a.h5')
        later = self.make_shot_file('shot_b.h5')
        self.runmanager.queue_manager.enqueue(
            [{'path': shot, 'compiled': True}, {'path': later, 'compiled': True}]
        )
        self.executor.requesting_shots = True
        self.run_loop(passes=1)

        self.executor.requesting_shots = False
        self.run_loop(passes=1)

        self.assertEqual(self.shots_run, [shot])
        self.assertEqual(
            [(row['path'], row['state']) for row in self.runmanager.rows()],
            [(later, '')],
            'the shot that ran is retired, and no other is taken up',
        )
        self.assertEqual(
            [os.path.basename(path) for path in self.runmanager.analysis_submission.submitted],
            ['shot_a.h5'],
        )


class OutcomeDeliveryTests(IntegrationFixture, unittest.TestCase):
    def test_an_outcome_that_did_not_get_through_rides_on_the_next_exchange(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])
        self.executor.requesting_shots = True
        # The shot runs, and its outcome waits for the next exchange:
        self.run_loop(passes=1)

        self.lose_next_request()
        self.run_loop(passes=3)

        self.assertEqual(self.shots_run, [shot], 'it is not offered again')
        self.assertEqual(self.runmanager.rows(), [], 'runmanager learned it completed')
        self.assertEqual(len(self.runmanager.analysis_submission.submitted), 1)


class RecordingTimer:
    """Stands in for QTimer so the quit poll can be driven by hand.

    finalise_quit reschedules itself on a real timer, which needs an event loop
    these tests do not run; holding the callback runs the real poll, deadline
    and all, one pass at a time.
    """

    def __init__(self):
        self.scheduled = []

    def singleShot(self, msec, callback):
        self.scheduled.append(callback)


class QuittingBLACS:
    """BLACS's own quit poll, with no tabs and no window."""

    finalise_quit = blacs.__main__.BLACS.finalise_quit

    def __init__(self, shot_executor):
        self.shot_executor = shot_executor
        self.tablist = {}
        self.exit_complete = False


class QuitWithAnOutcomeHeldTests(IntegrationFixture, unittest.TestCase):
    """Quitting BLACS while the shot loop still holds the last shot's outcome.

    The outcome goes out from the loop's own thread on its way out, and that
    thread is a daemon, so nothing stops the process ending first. These drive
    BLACS's finalise_quit poll against the loop running in a thread.
    """

    def setUp(self):
        super().setUp()
        self.timer = RecordingTimer()
        real_timer = blacs.__main__.QTimer
        blacs.__main__.QTimer = self.timer
        self.addCleanup(setattr, blacs.__main__, 'QTimer', real_timer)

        self.shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': self.shot, 'compiled': True}])
        self.executor.requesting_shots = True
        # The shot runs, and its outcome waits for the next exchange:
        self.run_loop(passes=1)

    def start_shot_loop(self):
        executor = self.executor

        def wait_to_be_stopped():
            while executor._manager_running:
                time.sleep(0.01)
            # However it was stopped, the loop takes a moment to get out:
            time.sleep(0.2)

        executor._manage = wait_to_be_stopped
        executor._manager_running = True
        executor.manager = threading.Thread(target=executor.manage, daemon=True)
        self.addCleanup(executor.manager.join, 5)
        executor.manager.start()

    def quit(self, deadline_in=2.0):
        """Drive finalise_quit the way its timer would, until BLACS is done."""
        blacs_app = QuittingBLACS(self.executor)
        blacs_app.shot_executor.stop()  # what closeEvent does
        gave_up_at = time.time() + deadline_in + 2
        blacs_app.finalise_quit(time.time() + deadline_in, {})
        while not blacs_app.exit_complete:
            self.assertTrue(self.timer.scheduled, 'the quit poll stopped polling')
            self.assertLess(time.time(), gave_up_at, 'BLACS never finished quitting')
            poll = self.timer.scheduled.pop()
            time.sleep(0.02)
            poll()

    def test_a_quit_while_an_outcome_is_held_waits_for_it_to_be_delivered(self):
        self.start_shot_loop()

        self.quit()

        self.assertEqual(
            self.runmanager.rows(),
            [],
            'BLACS finished quitting only once runmanager had the outcome',
        )
        self.assertEqual(len(self.runmanager.analysis_submission.submitted), 1)

    def test_a_runmanager_that_never_answers_cannot_hold_the_quit_open(self):
        answering = threading.Event()

        def never_answer(*args, **kwargs):
            answering.wait()

        self.runmanager.queue_exchange = never_answer
        self.start_shot_loop()
        # Registered after the loop so it is released before it is joined.
        self.addCleanup(answering.set)

        started = time.time()
        self.quit(deadline_in=0.3)

        self.assertLess(
            time.time() - started,
            1.5,
            'an apparatus whose runmanager is not answering can still close BLACS',
        )


def link_state(executor):
    """What BLACS's runmanager light says, once queued updates land."""
    QApplication.instance().processEvents()
    return executor._runmanager_link.state


class StatusPullTests(IntegrationFixture, unittest.TestCase):
    """Runmanager asks BLACS what it is doing; BLACS never pushes."""

    def test_the_status_pull_follows_the_shot_blacs_is_running(self):
        shot = self.make_shot_file('shot_a.h5')
        self.runmanager.queue_manager.enqueue([{'path': shot, 'compiled': True}])

        self.assertEqual(
            blacs_state(self.status_client.get_status())[0],
            'not requesting shots',
            'BLACS starts up not requesting shots',
        )

        self.executor.requesting_shots = True
        self.run_loop(passes=2)

        state, details = blacs_state(self.while_running[0]['status'])
        self.assertEqual(state, 'running shot_a.h5')
        self.assertIn('shot_a.h5', '\n'.join(details))
        self.assertEqual(
            blacs_state(self.status_client.get_status())[0],
            'requesting shots',
            'and once the shot is over it is asking for another',
        )


if __name__ == '__main__':
    unittest.main()
