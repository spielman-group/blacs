"""Saving the front panel to HDF5, and reading it back.

This runs when BLACS closes, and nothing else does: a failure here leaves BLACS
unable to quit at all, because closeEvent keeps retrying the close every 100 ms
and never reaches exit_complete. That is worth one round trip.
"""
import os
import tempfile
import unittest

import labscript_utils.h5_lock  # must precede h5py, as it does in BLACS itself
import h5py

from blacs.front_panel_settings import FrontPanelSettings
from labscript_utils import ensure_str


def a_front_panel():
    """The smallest front panel with one saved channel and one tab."""
    tab_data = {
        'fake_device': {
            'front_panel': {
                'ao0': {
                    'name': 'a channel',
                    'base_value': 1.5,
                    'locked': False,
                    'base_step_size': 0.1,
                    'current_units': 'V',
                },
            },
            'save_data': {'a key': 'a value'},
        },
    }
    notebook_data = {
        'fake_device': {'notebook': '1', 'page': 0, 'visible': True},
    }
    window_data = {
        '_main_window': {
            'width': 800,
            'height': 600,
            'xpos': 0,
            'ypos': 0,
            'maximized': False,
            'frame_height': 20,
            'frame_width': 2,
            '_shot_execution': {},
        },
    }
    return tab_data, notebook_data, window_data


class SaveAndRestoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.h5')
        os.close(handle)
        os.remove(self.path)
        self.addCleanup(
            lambda: os.path.exists(self.path) and os.remove(self.path)
        )
        # __init__ only records the settings path and connection table, neither
        # of which store_front_panel_in_h5 touches unless asked to save the
        # connection table:
        self.settings = FrontPanelSettings.__new__(FrontPanelSettings)

    def store(self):
        tab_data, notebook_data, window_data = a_front_panel()
        with h5py.File(self.path, 'w') as h5_file:
            self.settings.store_front_panel_in_h5(
                h5_file, tab_data, notebook_data, window_data, {}
            )

    def test_the_front_panel_can_be_written_and_read_back(self):
        self.store()

        with h5py.File(self.path, 'r') as h5_file:
            front_panel = h5_file['front_panel/front_panel'][:]
            notebook = h5_file['front_panel/_notebook_data'][:]

        self.assertEqual(ensure_str(front_panel[0]['name']), 'a channel')
        self.assertEqual(ensure_str(front_panel[0]['device_name']), 'fake_device')
        self.assertEqual(ensure_str(front_panel[0]['channel']), 'ao0')
        self.assertEqual(front_panel[0]['base_value'], 1.5)
        self.assertEqual(ensure_str(front_panel[0]['current_units']), 'V')

        self.assertEqual(ensure_str(notebook[0]['tab_name']), 'fake_device')
        self.assertEqual(ensure_str(notebook[0]['notebook']), '1')
        self.assertEqual(eval(ensure_str(notebook[0]['data'])), {'a key': 'a value'})


if __name__ == '__main__':
    unittest.main()
