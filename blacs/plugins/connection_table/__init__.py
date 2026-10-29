#####################################################################
#                                                                   #
# /blacs/plugins/connection_table/__init__.py                       #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import logging
import os
import sys

from qtutils.qt.QtCore import Qt
from qtutils.qt.QtGui import QStandardItemModel, QStandardItem
import labscript_utils.qtwidgets.file_dialogs as file_dialogs

from blacs.compile_and_restart import CompileAndRestart
from runmanager.globals_file import default_toml_path
from labscript_utils.filewatcher import FileWatcher
from labscript_utils.labconfig import LabConfig
from labscript_utils.text_editor import open_in_editor
from qtutils import inmain, UiLoader
from blacs.plugins import PLUGINS_DIR, PLUGIN_CONFIG_SECTION, BasePlugin

FILEPATH_COLUMN = 0
name = "Connection Table"
module = "connection_table" # should be folder name
logger = logging.getLogger(f'BLACS.plugin.{module}')

class Plugin(BasePlugin):
    def __init__(self):
        self.notifications = {}
        self.clean_modified_info = None
        self.BLACS = None

    def get_ui_contributions(self):
        return [
            {'context': 'settings', 'page': Setting},
            {'context': 'notifications', 'notification': RecompileNotification},
            {'context': 'notifications', 'notification': BrokenDevicesNotification},
        ]

    def get_menu_contributions(self):
        return [
            {'location': 'menubar', 'path': name, 'order': order, 'name': text,
             'action': action, 'icon': icon}
            for order, (text, action, icon) in enumerate([
                ('Edit', self.on_edit_connection_table, ':/qtutils/fugue/document--pencil'),
                ('Select Globals', self.on_select_globals, ':/qtutils/fugue/table--pencil'),
                ('Recompile', self.on_recompile_connection_table, ':/qtutils/fugue/arrow-circle'),
            ])
        ]

    def get_event_handlers(self):
        return {'settings_changed':self.notifications[RecompileNotification].setup_filewatching}

    def plugin_setup_complete(self, BLACS):
        self.BLACS = BLACS
        self.notifications = {
            notification: BLACS['notifications'].get_instance(notification)
            for notification in (RecompileNotification, BrokenDevicesNotification)
        }
        # The 'clean' modified info. We don't save the 'dirty' modified info. If watched
        # files are 'dirty' then our callback will be immediately called.
        self.notifications[RecompileNotification].setup_filewatching(self.clean_modified_info)
        failed_devices = list(self.BLACS['shot_execution'].BLACS.failed_device_settings.keys())
        if failed_devices:
            self.notifications[RecompileNotification]._show()
            self.notifications[BrokenDevicesNotification].set_broken_devices(failed_devices)
            self.notifications[BrokenDevicesNotification]._show()

    def get_save_data(self):
        return self.notifications[RecompileNotification].get_save_data()

    def restore_save_data(self, data):
        self.clean_modified_info = data.get('clean_modified_info', None)

    def close(self):
        self.notifications[RecompileNotification].close()
        self.notifications[BrokenDevicesNotification].close()

    def on_select_globals(self,*args,**kwargs):
        self.BLACS['settings'].create_dialog(goto_page=Setting)
      
    def on_edit_connection_table(self,*args,**kwargs):
        open_in_editor(
            self.BLACS['exp_config'].get('paths', 'connection_table_py'),
            parent=self.BLACS['ui'],
        )

    def on_recompile_connection_table(self,*args,**kwargs):
        logger.info('recompile connection table called')
        globals_files = self.BLACS['settings'].get_value(Setting,'globals_list')
        CompileAndRestart(self.BLACS, globals_files, self.BLACS['exp_config'].get('paths','connection_table_py'), self.BLACS['exp_config'].get('paths','connection_table_h5'),close_notification_func=self.notifications[RecompileNotification].on_restart)


class BrokenDevicesNotification:
    name = 'Device initialization failed'
    def __init__(self, BLACS):
        self._ui = UiLoader().load(os.path.join(PLUGINS_DIR, module, 'broken_device_notification.ui'))

    def get_widget(self):
        return self._ui

    def set_broken_devices(self, device_names):
        self._ui.label.setText(f'''<html><head/><body><span style=" font-weight:600; color:#ff0000;">BLACS failed to initialize some of your devices.
            It is advised that you solve this problem before using BLACS.
            The devices causing problems were: {", ".join(device_names)}</span></body></html>''')

    def get_properties(self):
        return {'can_hide':False, 'can_close':False}

    def set_functions(self,show_func,hide_func,close_func,get_state):
        self._show = show_func
        self._hide = hide_func
        self._close = close_func
        self._get_state = get_state

    def get_save_data(self):
        return {}

    def close(self):
        pass

class RecompileNotification:
    name = name
    def __init__(self, BLACS):
        self.BLACS = BLACS
        self.filewatcher = None
        self.clean_modified_info = None
        self._ui = UiLoader().load(os.path.join(PLUGINS_DIR, module, 'notification.ui'))
        self._ui.button.clicked.connect(self.on_recompile_connection_table)
            
    def get_widget(self):
        return self._ui
        
    def get_properties(self):
        return {'can_hide':True, 'can_close':False}
        
    def set_functions(self, show_func, hide_func, close_func, get_state):
        self._show = show_func
        self._hide = hide_func
        self._close = close_func
        self._get_state = get_state
                
    def on_recompile_connection_table(self,*args,**kwargs):
        self.BLACS['plugins'][module].on_recompile_connection_table()
        
    def callback(self, name, info, event=None):
        if event  == 'deleted':
            logger.info('{} {} ({})'.format(name, event, info))
            inmain(self._show)
        if event  == 'modified':
            logger.info('{} {} ({})'.format(name, event, info))
            inmain(self._show)
        elif event  == 'original':
            logger.info('All watched files restored')
            inmain(self._close)
        elif event == 'restored':
            logger.info('{} {} ({})'.format(name, event, info))
        elif event ==  'debug':
            logger.info(info)

    def setup_filewatching(self, clean_modified_info=None):
        folder_list = []
        file_list = [self.BLACS['connection_table_labscript'], self.BLACS['connection_table_h5file']]
        labconfig = self.BLACS['exp_config']
        try:
            hashable_types = labconfig.get(PLUGIN_CONFIG_SECTION, 'connection_table.hashable_types')
        except labconfig.NoOptionError:
            hashable_types = ['.py', '.txt', '.ini', '.toml', '.json']
        try:
            polling_interval = self.BLACS['exp_config'].getfloat(
                PLUGIN_CONFIG_SECTION, 'connection_table.polling_interval'
            )
        except labconfig.NoOptionError:
            polling_interval = 1
        logger.info('Using hashable_types: {}; polling_interval: {}'.format(hashable_types, polling_interval))
        
        file_list += self.BLACS['settings'].get_value(Setting,'globals_list')
        calibration_list = self.BLACS['settings'].get_value(Setting,'calibrations_list')
        for path in calibration_list:
            if os.path.isdir(path):
                folder_list.append(path)
            else:
                file_list.append(path)
        
        if self.filewatcher is not None:
            self.filewatcher.stop()
            # Should only be calling this with modified_info not None once at startup:
            assert clean_modified_info is None
            clean_modified_info = self.filewatcher.get_clean_modified_info()
            
        self.filewatcher = FileWatcher(
            self.callback,
            file_list,
            folder_list,
            clean_modified_info=clean_modified_info,
            hashable_types=hashable_types,
            interval=polling_interval,
        )

    def get_save_data(self):
        if self.clean_modified_info is not None:
            # We are doing a restart after a recompilation - return the modified info
            # that was just saved:
            return {'clean_modified_info': self.clean_modified_info}
        else:
            return {'clean_modified_info': self.filewatcher.get_clean_modified_info()}

    def on_restart(self):
        # The connection table was recompiled and we are restarting. This and the
        # first watch are the only times the 'clean' modified info is replaced with
        # the current modified info; the current state is now the clean state.
        self.filewatcher.stop()
        self.clean_modified_info = self.filewatcher.get_modified_info()
        self.filewatcher = None
        self._close()

    def close(self):
        if self.filewatcher is not None:
            self.filewatcher.stop()
    
class Setting:
    name = name

    def __init__(self,data):
        self.data = data
        
        self.stores_list = ['globals','calibrations']
        
        for store in self.stores_list:
            if f'{store}_list' not in self.data:
                self.data[f'{store}_list'] = []
                
            if f'{store}_sort_order' not in self.data:
                self.data[f'{store}_sort_order'] = 'ASC'

    def normalize_globals_path(self, filepath):
        filepath = os.path.normpath(str(filepath))
        if os.path.splitext(filepath)[1].lower() in ('.h5', '.hdf5'):
            return default_toml_path(filepath)
        return filepath
        
    # Return the settings page and an icon for its label; the label text is the
    # class's name attribute.
    def create_dialog(self,notebook):
        ui = UiLoader().load(os.path.join(PLUGINS_DIR, module, 'connection_table.ui'))
        
        self.models = {}
        self.views = {}
        self.models['globals'] = QStandardItemModel()
        self.models['globals'].setHorizontalHeaderItem(FILEPATH_COLUMN, QStandardItem('Filepath'))
        self.views['globals'] = ui.h5_treeview
        self.views['globals'].setModel(self.models['globals'])
        
        self.models['calibrations'] = QStandardItemModel()
        self.models['calibrations'].setHorizontalHeaderItem(FILEPATH_COLUMN, QStandardItem('Filepath'))
        self.views['calibrations'] = ui.unit_conversion_treeview
        self.views['calibrations'].setModel(self.models['calibrations'])
        
        ui.add_h5_file.clicked.connect(self.add_global_file)
        ui.delete_h5_file.clicked.connect(self.delete_selected_globals_file)
        ui.add_unitconversion_file.clicked.connect(self.add_calibration_file)
        ui.add_unitconversion_folder.clicked.connect(self.add_calibration_folder)
        ui.delete_unitconversion.clicked.connect(self.delete_selected_conversion_file)
        
        self.views['globals'].header().sortIndicatorChanged.connect(self.global_sort_indicator_changed)
        self.views['calibrations'].header().sortIndicatorChanged.connect(self.calibrations_sort_indicator_changed)
        
        for store in self.stores_list:
            if f'{store}_list' in self.data:
                for path in self.data[f'{store}_list']:
                    if store == 'globals':
                        path = self.normalize_globals_path(path)
                    self.models[store].appendRow(QStandardItem(path))
            else:
                self.data[f'{store}_list'] = []
            
            self.views[store].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data[f'{store}_sort_order']))
        
        return ui,None
    
    def global_sort_indicator_changed(self):
        self.data['globals_sort_order'] = self.enum_to_order(self.views['globals'].header().sortIndicatorOrder())
        
    def calibrations_sort_indicator_changed(self):
        self.data['calibrations_sort_order'] = self.enum_to_order(self.views['calibrations'].header().sortIndicatorOrder())
    
    def order_to_enum(self, order):
        # If we are accidentally passed an enum, just return it
        if order not in ['ASC', 'DESC']:
            return order
    
        if 'PySide' in sys.modules.copy():
            if order == 'ASC':
                enum = Qt.SortOrder.AscendingOrder
            else:
                enum = Qt.SortOrder.DescendingOrder
        else:
            if order == 'ASC':
                enum = Qt.AscendingOrder
            else:
                enum = Qt.DescendingOrder
        
        return enum
        
    def enum_to_order(self, enum):
        if 'PySide' in sys.modules.copy():
            if enum == Qt.SortOrder.AscendingOrder:
                order = 'ASC'
            else:
                order = 'DESC'
        else:
            if enum == Qt.AscendingOrder:
                order = 'ASC'
            else:
                order = 'DESC'
        
        return order
    
    def get_value(self,name):
        if name in self.data:
            if name == 'globals_list':
                return [self.normalize_globals_path(path) for path in self.data[name]]
            return self.data[name]
        
        return None
    
    def save(self):
        for store in self.stores_list:
            self.data[f'{store}_list'] = []
            for row_index in range(self.models[store].rowCount()):
                path = str(self.models[store].item(row_index).text())
                if store == 'globals':
                    path = self.normalize_globals_path(path)
                self.data[f'{store}_list'].append(path)
        
        return self.data
        
    def close(self):
        pass
        
    def add_global_file(self,*args,**kwargs):
        selected_files = file_dialogs.open_files(
            None,
            "select globals files",
            # Where runmanager's globals dialogs open too.
            LabConfig().get('paths', 'experiment_shot_storage', fallback=''),
            "Globals files (*.toml *.h5 *.hdf5)",
        )
        if selected_files is not None:
            for filepath in selected_files:
                filepath = self.normalize_globals_path(filepath)
                # Qt greys out OK for a typed name that does not match the dialog's
                # extension filter, yet Enter still selects the file, so check the
                # extension of each file here.
                if filepath.endswith('.toml'):
                    if not self.is_filepath_in_store(filepath, 'globals'):
                        self.models['globals'].appendRow(QStandardItem(filepath))
         
            self.views['globals'].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data['globals_sort_order']))
            
    def is_filepath_in_store(self,filepath,store):
        for row_index in range(self.models[store].rowCount()):
            if str(filepath) == str(self.models[store].item(row_index).text()):
                return True
        return False
    
    def delete_selected_globals_file(self):
        index_list = self.views['globals'].selectedIndexes()
        while index_list:
            self.models['globals'].takeRow(index_list[0].row())
            index_list = self.views['globals'].selectedIndexes()
        
        self.views['globals'].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data['globals_sort_order']))
            
    def add_calibration_file(self):
        selected_files = file_dialogs.open_files(
            None,
            "Select unit conversion scripts",
            LabConfig().get('paths', 'userlib', fallback=''),
            "Python files (*.py *.pyw)",
        )
        if selected_files is not None:
            for filepath in selected_files:
                filepath = os.path.normpath(filepath)
                # Qt greys out OK for a typed name that does not match the dialog's
                # extension filter, yet Enter still selects the file, so check the
                # extension of each file here.
                if filepath.endswith('.py') or filepath.endswith('.pyw'):
                    if not self.is_filepath_in_store(filepath,'calibrations'):
                        self.models['calibrations'].appendRow(QStandardItem(filepath))
         
            self.views['calibrations'].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data['calibrations_sort_order']))
        
    def add_calibration_folder(self):
        filepath = file_dialogs.choose_folder(
            None, "Select unit conversion folder", LabConfig().get('paths', 'userlib', fallback='')
        )
        if filepath is not None:
            if not self.is_filepath_in_store(filepath,'calibrations'):
                self.models['calibrations'].appendRow(QStandardItem(filepath))
         
            self.views['calibrations'].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data['calibrations_sort_order']))
    
    def delete_selected_conversion_file(self):
        index_list = self.views['calibrations'].selectedIndexes()
        while index_list:
            self.models['calibrations'].takeRow(index_list[0].row())
            index_list = self.views['calibrations'].selectedIndexes()
        
        self.views['calibrations'].sortByColumn(FILEPATH_COLUMN,self.order_to_enum(self.data['calibrations_sort_order']))
