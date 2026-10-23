#####################################################################
#                                                                   #
# /tests/test_worker.py                                             #
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
import unittest
import warnings
from unittest import mock

# Imported as BLACS imports it, before Worker.run's own import, which would
# otherwise reach the stand-in process tree.
import labscript_utils.h5_lock
from blacs.tab_base_classes import Worker


class DeviceWorker(Worker):
    timeout = 5


class WorkerargTests(unittest.TestCase):
    """The arguments a device tab gives its worker."""

    def run_worker(self, workerargs):
        # Worker.run's once-per-process logging setup, its naming of this process
        # to zlock and its job loop are stood in for.
        worker = DeviceWorker.__new__(DeviceWorker)
        worker.mainloop = lambda: None
        with mock.patch('labscript_utils.setup_logging.setup_logging'), \
             mock.patch('labscript_utils.excepthook.set_logger'), \
             mock.patch('labscript_utils.ls_zprocess.ProcessTree'):
            worker.run('main_worker', 'device', workerargs)
        return worker

    def test_a_workerarg_overrides_the_device_workers_class_default(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            self.assertEqual(self.run_worker({'timeout': 2}).timeout, 2)

    def test_a_name_the_worker_base_class_has_is_refused_with_a_warning(self):
        with self.assertWarns(RuntimeWarning):
            worker = self.run_worker({'init': None})
        self.assertTrue(callable(worker.init))


if __name__ == '__main__':
    unittest.main()
