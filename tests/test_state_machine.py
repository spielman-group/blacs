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
import time
import unittest

from qtutils.qt.QtWidgets import QApplication
from labscript_utils.qtwidgets.dragdroptab import DragDropTabWidget

from blacs.tab_base_classes import MODE_MANUAL, Tab, define_state


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
        app = QApplication.instance() or QApplication([])
        tab = StaleJobTab(
            DragDropTabWidget(),
            {'device_name': 'device', 'connection_table': ConnectionTable()},
        )
        tab.finish_before_yielding()
        tab.next_state()
        # The mainloop's thread reaches the GUI thread through Qt events.
        deadline = time.monotonic() + 5
        while not tab.next_state_ran and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertTrue(tab.next_state_ran)

        tab.close_tab()


if __name__ == '__main__':
    unittest.main()
