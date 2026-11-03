"""Saving the front panel to HDF5, and reading it back.

It runs as BLACS closes: a failure here leaves BLACS unable to quit, because
closeEvent keeps retrying the close and never reaches exit_complete.
"""
import os
import tempfile
import types
import unittest

import labscript_utils.h5_lock  # must precede h5py, as it does in BLACS itself
import h5py

from qtutils.qt.QtWidgets import QApplication, QFrame, QVBoxLayout

from blacs.front_panel_settings import FrontPanelSettings
from blacs.tab_base_classes import PluginTab, Tab
from blacs.plugins import connection_table, delete_repeated_shots
from labscript_utils import ensure_str
import runmanager.globals_file as globals_file

# The plugin's widgets need an application, held for the life of the process.
_qapplication = QApplication.instance() or QApplication([])


def a_front_panel():
    """The smallest front panel with one saved channel and one tab."""
    tab_data = {
        'fake_device': {
            'front_panel_settings': {
                'ao0': {
                    'name': 'a channel',
                    'base_value': 1.5,
                    'locked': False,
                    'base_step_size': 0.1,
                    'current_units': 'V',
                },
            },
            'saved_data': {'a key': 'a value'},
        },
    }
    notebook_data = {
        'fake_device': {'notebook': '1', 'page': 0, 'visible': True},
    }
    window_data = {'geometry': {}, '_shot_execution': {}}
    return tab_data, notebook_data, window_data


class SaveAndRestoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.h5')
        os.close(handle)
        os.remove(self.path)
        self.addCleanup(
            lambda: os.path.exists(self.path) and os.remove(self.path)
        )
        # __init__ only records the settings path and connection table, and the
        # table is read only when asked to save it.
        self.settings = FrontPanelSettings.__new__(FrontPanelSettings)
        self.settings.settings_path = self.path

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

    def test_a_shot_file_holds_only_front_panel_values_and_device_settings(self):
        tab_data, _, _ = a_front_panel()
        with h5py.File(self.path, 'w') as h5_file:
            self.settings.store_shot_front_panel_in_h5(h5_file, tab_data)

        with h5py.File(self.path, 'r') as h5_file:
            self.assertEqual(set(h5_file['front_panel']), {'front_panel', '_notebook_data'})
            notebook = h5_file['front_panel/_notebook_data']
            self.assertEqual(dict(notebook.attrs), {})
            self.assertEqual(notebook.dtype.names, ('tab_name', 'data'))
            front_panel = h5_file['front_panel/front_panel'][:]
            notebook = notebook[:]

        self.assertEqual(front_panel[0]['base_value'], 1.5)
        self.assertEqual(ensure_str(notebook[0]['tab_name']), 'fake_device')
        self.assertEqual(eval(ensure_str(notebook[0]['data'])), {'a key': 'a value'})

    def test_shot_data_can_exclude_preferences_without_losing_application_settings(self):
        preferences = {'autolevels': True, 'targetRange_x': [0, 100],
                       'reference_clock': 'external'}
        cases = [(base, data) for base in (Tab, PluginTab)
                 for data in ({}, {'reference_clock': 'external'}, None)]
        for base, archived in cases:
            with self.subTest(base=base.__name__, archived=archived):
                class PreferencesTab(base):
                    def get_builtin_save_data(self):
                        return {}

                    def get_save_data(self):
                        return preferences

                class ArchivalTab(PreferencesTab):
                    def get_device_save_data(self):
                        return archived

                tab_class = PreferencesTab if archived is None else ArchivalTab
                tab = tab_class.__new__(tab_class)
                tab_data, positions, window = a_front_panel()
                tab.settings = tab_data['fake_device']
                self.settings.tablist = {'fake_device': tab}
                tab_data['fake_device']['saved_data'] = tab.get_all_save_data()
                with h5py.File(self.path, 'w') as h5_file:
                    self.settings.store_front_panel_in_h5(
                        h5_file, tab_data, positions, window, {}
                    )
                with h5py.File(self.path, 'r') as h5_file:
                    saved = h5_file['front_panel/_notebook_data'][0]['data']
                    self.assertEqual(eval(ensure_str(saved)), preferences)

                with h5py.File(self.path, 'w') as h5_file:
                    self.settings.store_shot_front_panel_in_h5(
                        h5_file, self.settings.get_shot_data()
                    )
                with h5py.File(self.path, 'r') as h5_file:
                    saved = h5_file['front_panel/_notebook_data'][0]['data']
                    expected = preferences if archived is None else archived
                    self.assertEqual(eval(ensure_str(saved)), expected)
                    self.assertEqual(
                        h5_file['front_panel/front_panel'][0]['base_value'], 1.5
                    )


class LoadIntoRunningPluginTests(unittest.TestCase):
    def test_legacy_connection_table_globals_are_converted_without_overwriting_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            for extension in ('.h5', '.hdf5'):
                with self.subTest(extension=extension):
                    path = os.path.join(directory, 'globals' + extension)
                    toml_path = globals_file.default_toml_path(path)
                    with h5py.File(path, 'w') as h5_file:
                        group = h5_file.create_group('globals/hardware')
                        group.attrs['voltage'] = '2 + 3'
                        group.create_group('units').attrs['voltage'] = 'V'
                    setting = connection_table.Setting({'globals_list': [path]})

                    self.assertEqual(setting.get_value('globals_list'), [toml_path])
                    self.assertTrue(os.path.isfile(toml_path))
                    self.assertEqual(
                        globals_file.get_globals_details({'hardware': toml_path}),
                        globals_file.get_globals_details({'hardware': path}),
                    )
                    globals_file.set_field(toml_path, 'hardware', 'voltage', 'default', '9')

                    self.assertEqual(setting.get_value('globals_list'), [toml_path])
                    record = globals_file.get_global_record(toml_path, 'hardware', 'voltage')
                    self.assertEqual(record['default'], '9')
                    self.assertTrue(os.path.isfile(path))
                    os.remove(toml_path)

    def test_a_loaded_front_panel_sets_the_running_repeated_shot_limit(self):
        frame = QFrame()
        frame.setLayout(QVBoxLayout())
        plugin = delete_repeated_shots.Plugin()
        self.addCleanup(plugin.close)
        plugin.plugin_setup_complete({'ui': types.SimpleNamespace(shot_controls_frame=frame)})
        plugin.delete_queue = ['this_session_rep1.h5']

        plugin.restore_save_data({'n_shots_to_keep': 3, 'delete_queue': ['old_rep1.h5']})

        self.assertEqual(plugin.ui.spinBox.value(), 3)
        self.assertEqual(plugin.n_shots_to_keep, 3)
        self.assertEqual(plugin.delete_queue, ['this_session_rep1.h5'])


if __name__ == '__main__':
    unittest.main()
