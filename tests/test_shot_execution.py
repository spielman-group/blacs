"""Behavioural tests for BLACS's shot executor that need no runmanager.

ShotExecutor.__init__ starts the shot loop and wires up real Qt widgets,
neither of which belongs in a unit test, so these build an executor without it
and give it the small surface the methods under test actually use.
"""
import os
import shutil
import tempfile
import types
import unittest

import zprocess

from fixtures import make_executor

from blacs import shot_execution
from blacs.shot_execution import ShotExecutor

# After the imports above, never before them: labscript_utils.h5_lock has to
# be imported before anything imports h5py, and BLACS is what imports it.
import h5py


class RequestShotsControlTests(unittest.TestCase):
    def test_restoring_saved_state_never_starts_requesting_shots(self):
        executor = make_executor()
        executor.restore_save_data(
            {
                'manager_paused': False,
                'last_opened_shots_folder': '/tmp/shots',
                'local_override_path': '/tmp/override.h5',
            }
        )
        self.assertFalse(executor.requesting_shots)
        self.assertEqual(executor.last_opened_shots_folder, '/tmp/shots')
        self.assertEqual(
            executor._ui.local_override_lineEdit.text(), '/tmp/override.h5'
        )


class LocalFallbackTests(unittest.TestCase):
    def test_each_fallback_repetition_gets_a_file_of_its_own(self):
        # The same override file is run over and over, and each run's data is
        # written to a fresh numbered copy rather than over the last one.
        executor = make_executor()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        override = os.path.join(directory, 'override.h5')
        open(override, 'w').close()

        repetitions = []
        for _ in range(3):
            path, repeat_number = executor.new_rep_name(override)
            self.assertFalse(
                os.path.exists(path), 'a repetition never lands on existing data'
            )
            open(path, 'w').close()
            repetitions.append((path, repeat_number))

        paths = [path for path, _ in repetitions]
        self.assertEqual(len(set(paths)), 3, 'each repetition is its own file')
        self.assertNotIn(override, paths, 'and none of them is the override itself')
        self.assertEqual([number for _, number in repetitions], [1, 2, 3])


class RerunShotFileTests(unittest.TestCase):
    """What a copy of a shot file carries over, and what BLACS writes itself.

    A shot file that already holds data is run by copying it first, so the run
    writes into the copy and the data already there is left alone. Every root
    attribute crosses over untouched; ``run repeat``, which counts which
    execution of the shot a file holds, is the one field BLACS writes.
    """

    def setUp(self):
        self.executor = make_executor()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def shot_file(self, name, **attrs):
        """A shot file that has been run: root attributes, and data from a run."""
        path = os.path.join(self.directory, name)
        with h5py.File(path, 'w') as h5_file:
            for attribute, value in attrs.items():
                h5_file.attrs[attribute] = value
            h5_file.create_group('globals')
            h5_file.create_group('data')
        return path

    def test_a_rerun_copy_is_the_same_shot_without_the_previous_data(self):
        original = self.shot_file(
            'shot.h5', shot_id='shot-1', sequence_id='seq-1', **{'run number': 3}
        )
        copy = os.path.join(self.directory, 'shot_rep00001.h5')

        self.assertTrue(self.executor.clean_h5_file(original, copy, repeat_number=1))

        with h5py.File(copy, 'r') as h5_file:
            attributes = dict(h5_file.attrs)
            self.assertNotIn(
                'data', h5_file, 'the copy is ready to be run, not already run'
            )
            self.assertIn('globals', h5_file, 'and is still the same experiment')
        self.assertEqual(attributes['shot_id'], 'shot-1')
        self.assertEqual(attributes['sequence_id'], 'seq-1')
        self.assertEqual(attributes['run number'], 3)
        self.assertEqual(attributes['run repeat'], 1, 'which execution this file is')

    def test_a_failed_shot_is_reset_for_another_go_at_the_same_run(self):
        # A shot that failed is put back as it was, in place and under its own
        # name, so it can be run again. It is the same execution being attempted
        # again, so its repeat number stands.
        path = self.shot_file(
            'shot_rep00002.h5', shot_id='shot-1', **{'run repeat': 2}
        )

        self.executor.reset_failed_shot_file(path)

        with h5py.File(path, 'r') as h5_file:
            attributes = dict(h5_file.attrs)
            self.assertNotIn(
                'data', h5_file, 'the failed run leaves nothing behind to trip on'
            )
        self.assertEqual(attributes['shot_id'], 'shot-1')
        self.assertEqual(
            attributes['run repeat'], 2, 'a run that failed is not another repeat'
        )


def failing_manage():
    raise RuntimeError('the shot loop fell over')


class LocalErrorLatchTests(unittest.TestCase):
    def test_a_shot_loop_that_dies_stops_requests_and_says_why(self):
        executor = make_executor()
        executor._requesting_shots = True
        executor._manage = failing_manage
        shot_execution.zprocess = types.SimpleNamespace(
            raise_exception_in_thread=lambda info: None
        )
        try:
            ShotExecutor.manage(executor)
        finally:
            shot_execution.zprocess = zprocess

        self.assertFalse(executor.requesting_shots)
        self.assertIn('Shot execution stopped', executor.local_error)


if __name__ == '__main__':
    unittest.main()
