#####################################################################
#                                                                   #
# /blacs/__main__.py                                                #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""BLACS GUI and supporting code."""
import labscript_utils.excepthook

import ast
import os
from blacs import APPLICATION_NAME, BLACS_DIR, DESKTOP_APP_MODULE

# Associate app windows with OS menu shortcuts:
import desktop_app
desktop_app.set_process_appid(DESKTOP_APP_MODULE)


from labscript_utils.splash import Splash, get_qapplication
splash = Splash(
    os.path.join(BLACS_DIR, 'blacs.svg'), application_name=APPLICATION_NAME
)
splash.show()

splash.update_text('importing standard library modules')
import subprocess
import sys
import time
import platform
import importlib.metadata
WINDOWS = platform.system() == 'Windows'

# No splash update for Qt - the splash code has already imported it:
from qtutils import inmain_later, inthread, UiLoader
import qtutils.icons  # import has side-effects we rely on
from qtutils.qt.QtCore import QTimer, Qt, qVersion
from qtutils.qt.QtGui import QIcon, QColor, QPalette
from qtutils.qt.QtWidgets import (
    QMainWindow,
    QApplication
)
from qtutils.qt import QT_ENV
PYQT_VERSION_STR = importlib.metadata.version(QT_ENV)


splash.update_text("importing zmq and zprocess")
import zmq
import zprocess
from zprocess import raise_exception_in_thread


splash.update_text('importing h5_lock and h5py')
import labscript_utils.h5_lock, h5py


splash.update_text('importing labscript suite modules')
import labscript_utils
from labscript_utils.ls_zprocess import ProcessTree, ZMQServer
from labscript_utils.setup_logging import setup_logging
import labscript_utils.shared_drive
import blacs


process_tree = ProcessTree.instance()
process_tree.zlock_client.set_process_name(APPLICATION_NAME)


logger = setup_logging(APPLICATION_NAME)
labscript_utils.excepthook.set_logger(logger)

logger.info(f'Python version {sys.version}')
logger.info(f'Platform: {sys.platform}')
logger.info(f'windows version: {sys.getwindowsversion() if WINDOWS else None}')
logger.info(f'PyZMQ version: {zmq.__version__}')
logger.info(f'ZMQ version: {zmq.zmq_version()}')
logger.info(f'h5py version: {h5py.version.info}')
logger.info(f'Qt enviroment: {QT_ENV}')
logger.info(f'PySide/PyQt version: {PYQT_VERSION_STR}')
logger.info(f'Qt version: {qVersion()}')
logger.info(f'qtutils version: {qtutils.__version__}')
logger.info(f'zprocess version: {zprocess.__version__}')
logger.info(f'labscript_utils version: {labscript_utils.__version__}')
logger.info(f'BLACS version: {blacs.__version__}')

from labscript_utils.connections import ConnectionTable
from labscript_utils.qtwidgets.dragdroptab import DragDropTabWidget
import labscript_utils.qtwidgets.file_dialogs as file_dialogs
from labscript_utils.qtwidgets.themed_window import ThemedWindow
from labscript_utils.qtwidgets.application import LabscriptGuiApplication
from labscript_utils.labconfig import LabConfig, get_default_appconfig_file
from labscript_utils.plugins import MenuContext
from labscript_profile import hostname
from blacs.shot_execution import ShotExecutor
from labscript_utils import device_registry
from blacs.front_panel_settings import FrontPanelSettings
from blacs.notifications import Notifications
from labscript_utils.settings import Settings
import blacs.plugins as plugins


class BLACSWindow(ThemedWindow, QMainWindow):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.themeChanged.connect(self.on_theme_changed)

    def closeEvent(self, event):
        if self.blacs.exit_complete:
            event.accept()
            if self.blacs._relaunch:
                logger.info('relaunching BLACS after quit')
                subprocess.Popen([sys.executable] + sys.argv)
        else:
            event.ignore()
            logger.info('destroy called')
            if not self.blacs.exiting:
                self.blacs.exiting = True
                self.blacs.shot_executor.stop()
                self.blacs.settings.close()
                self.blacs.server.shutdown()
                plugins.manager.close_plugins()

                inmain_later(self.blacs.on_save_exit)

            QTimer.singleShot(100,self.close)

    def on_theme_changed(self):
        # Tab header text is coloured per tab with setTabTextColor, so a theme
        # change has to repaint it explicitly.
        app = QApplication.instance()
        self.blacs.update_all_tab_icon_and_text(
            app.palette().color(QPalette.ColorRole.Text)
        )


class BLACS(LabscriptGuiApplication):

    app_name = APPLICATION_NAME
    # The shot controls sit above these splitters rather than in one, so they
    # take the height they need and the device tabs get the rest.
    splitters = (
        'tab_top_vertical_splitter',
        'tab_bottom_vertical_splitter',
        'tab_horizontal_splitter',
    )
    tab_widget_ids = 7

    @staticmethod
    def _restore_shot_execution_data(blacs_settings):
        if 'shot_execution_data' not in blacs_settings:
            return {}
        return ast.literal_eval(blacs_settings['shot_execution_data'])

    def __init__(self,application):
        splash.update_text('loading graphical interface')
        self.qt_application = application
        self._relaunch = False
        self.exiting = False
        self.exit_complete = False

        logger.info('Loading BLACS ui')
        loader = UiLoader()
        self.ui = loader.load(os.path.join(BLACS_DIR, 'main.ui'), BLACSWindow())
        logger.info('BLACS ui loaded')
        self.ui.blacs=self
        self.init_config_window_title()
        self.tab_widgets = {}
        self.exp_config = exp_config # Global variable
        self.settings_path = settings_path # Global variable
        self.set_config_window_title(self.settings_path)
        self.connection_table = connection_table # Global variable
        self.connection_table_h5file = self.exp_config.get('paths','connection_table_h5')
        self.connection_table_labscript = self.exp_config.get('paths','connection_table_py')

        self.tablist = {}
        self.settings_dict = {}

        splash.update_text('loading device front panel settings')
        # Maps each connected device's name to its labscript class name.
        logger.info('finding connected devices in connection table')
        self.attached_devices = self.connection_table.get_attached_devices()

        logger.info('Loading front panel settings')
        self.front_panel_settings = FrontPanelSettings(self.settings_path, self.connection_table)
        self.front_panel_settings.setup(self)
        settings,question,error,tab_data = self.front_panel_settings.restore()

        logger.info('restoring window data')
        self.restore_window(tab_data)

        splash.update_text('creating device tabs...')
        logger.info('Creating tab widgets')
        for i in range(4):
            self.tab_widgets[i] = DragDropTabWidget(self.tab_widget_ids)
            self.tab_widgets[i].setElideMode(Qt.ElideRight)
            getattr(self.ui, f'tab_container_{i}').addWidget(self.tab_widgets[i])

        logger.info('Instantiating devices')
        self.failed_device_settings = {}
        for device_name, labscript_device_class_name in list(self.attached_devices.items()):
            try:
                self.settings_dict.setdefault(device_name,{"device_name":device_name})
                self.settings_dict[device_name]["connection_table"] = self.connection_table
                self.settings_dict[device_name]["front_panel_settings"] = settings[device_name] if device_name in settings else {}
                self.settings_dict[device_name]["saved_data"] = tab_data[device_name]['data'] if device_name in tab_data else {}
                logger.info('instantiating %s'%device_name)
                TabClass = device_registry.get_BLACS_tab(labscript_device_class_name)
                self.tablist[device_name] = TabClass(self.tab_widgets[0],self.settings_dict[device_name])
            except Exception:
                self.failed_device_settings[device_name] = {"front_panel_settings": self.settings_dict[device_name]["front_panel_settings"], "saved_data": self.settings_dict[device_name]["saved_data"]}
                del self.settings_dict[device_name]
                del self.attached_devices[device_name]
                self.connection_table.remove_device(device_name)
                raise_exception_in_thread(sys.exc_info())

        splash.update_text('instantiating plugins')
        logger.info('Instantiating plugins')
        self.plugins = plugins.manager.instantiate_plugins()
        plugins.manager.restore_save_data(
            ast.literal_eval(tab_data['BLACS settings'].get('plugin_data', '{}'))
        )

        logger.info('creating plugin tabs')
        for module_name, plugin in self.plugins.items():
            try:
                if hasattr(plugin, 'get_tab_classes'):
                    tab_dict = {}

                    for tab_name, TabClass in plugin.get_tab_classes().items():
                        settings_key = f"{module_name}: {tab_name}"
                        self.settings_dict.setdefault(settings_key, {"tab_name": tab_name})
                        self.settings_dict[settings_key]["front_panel_settings"] = (
                            settings[settings_key] if settings_key in settings else {}
                        )
                        self.settings_dict[settings_key]["saved_data"] = (
                            tab_data[settings_key]['data'] if settings_key in tab_data else {}
                        )

                        self.tablist[settings_key] = TabClass(
                            self.tab_widgets[0],
                            self.settings_dict[settings_key],
                        )
                        tab_dict[tab_name] = self.tablist[settings_key]

                    if hasattr(plugin, 'tabs_created'):
                        plugin.tabs_created(tab_dict)

            except Exception:
                logger.exception(
                    "Could not instantiate tab for plugin '%s'. Skipping" % module_name
                )

        logger.info('reordering tabs')
        self.order_tabs(tab_data)

        splash.update_text("starting shot execution")
        logger.info('starting shot execution thread')
        self.shot_executor = ShotExecutor(self,self.ui)
        self.shot_executor.restore_save_data(
            self._restore_shot_execution_data(tab_data['BLACS settings'])
        )

        blacs_data = {'exp_config':self.exp_config,
                      'ui':self.ui,
                      'set_relaunch':self.set_relaunch,
                      'plugins':self.plugins,
                      'connection_table_h5file':self.connection_table_h5file,
                      'connection_table_labscript':self.connection_table_labscript,
                      'shot_execution':self.shot_executor
                     }

        splash.update_text('setting up notifications and preferences')
        logger.info('setting up notifications and preferences')
        self.notifications = Notifications(blacs_data)
        self.settings = Settings(file=self.settings_path, parent=self.ui)
        self.settings.register_callback(
            lambda: plugins.manager.fire_event('settings_changed')
        )
        blacs_data['notifications'] = self.notifications
        blacs_data['settings'] = self.settings

        # Plugins add their menus, preferences pages and notifications here:
        menus = MenuContext(logger=logger, icon_factory=QIcon)
        menus.register_location('menubar', self.ui.menubar)
        plugins.manager.register_context('menus', menus)
        plugins.manager.register_context('settings', self.settings)
        plugins.manager.register_context('notifications', self.notifications)
        plugins.manager.setup_contexts(blacs_data)
        menus.render()

        plugins.manager.setup_complete(blacs_data)

        self.ui.actionOpenPreferences.triggered.connect(self.on_open_preferences)
        self.ui.actionSave.triggered.connect(self.on_save_front_panel)
        self.ui.actionOpen.triggered.connect(self.on_load_front_panel)
        self.ui.actionExit.triggered.connect(self.ui.close)

        # The server answers questions about this BLACS, so it starts once there is
        # one to describe: earlier, it would tell a runmanager throughout start-up
        # that this BLACS had failed.
        splash.update_text('starting experiment server')
        self.server = BlacsServer(self, int(self.exp_config.get('ports', 'blacs')))

        splash.update_text('done')
        logger.info('showing UI')
        self.ui.show()

    def set_relaunch(self,value):
        self._relaunch = bool(value)

    def restore_window(self, tab_data):
        geometry = tab_data['BLACS settings'].get('window_geometry')
        if geometry is None:
            return
        try:
            self.restore_geometry(ast.literal_eval(geometry))
        except Exception:
            logger.exception('Unable to restore the window geometry.')

    def order_tabs(self,tab_data):
        # Move each tab to its saved notebook.
        for tab_name in self.tablist.keys():
            notebook_num = 0
            if tab_name in tab_data:
                notebook_num = int(tab_data[tab_name]["notebook"])
                if notebook_num not in self.tab_widgets:
                    notebook_num = 0

            for notebook in self.tab_widgets.values():
                tab_index = notebook.indexOf(self.tablist[tab_name]._ui)
                if tab_index != -1:
                    tab_text = notebook.tabText(tab_index)
                    notebook.removeTab(tab_index)
                    self.tab_widgets[notebook_num].addTab(self.tablist[tab_name]._ui,tab_text)
                    break

        splash.update_text('restoring tab positions...')
        # Reordering waits until every tab is in its notebook.
        for tab_name in self.tablist.keys():
            if tab_name in tab_data:
                notebook_num = int(tab_data[tab_name]["notebook"])
                if notebook_num in self.tab_widgets:
                    self.tab_widgets[notebook_num].tab_bar.moveTab(self.tab_widgets[notebook_num].indexOf(self.tablist[tab_name]._ui),int(tab_data[tab_name]["page"]))

        for tab_name, data in tab_data.items():
            if tab_name == 'BLACS settings':
                continue
            if bool(data["visible"]) and int(data["notebook"]) in self.tab_widgets:
                self.tab_widgets[int(data["notebook"])].tab_bar.setCurrentIndex(int(data["page"]))

    def update_all_tab_settings(self,settings,tab_data):
        for tab_name,tab in self.tablist.items():
            self.settings_dict[tab_name]["front_panel_settings"] = settings[tab_name] if tab_name in settings else {}
            self.settings_dict[tab_name]["saved_data"] = tab_data[tab_name]['data'] if tab_name in tab_data else {}
            tab.update_from_settings(self.settings_dict[tab_name])

    def update_all_tab_icon_and_text(self, text_colour):
        # Repaints tab header text after a theme change.
        for tab in self.tablist.values():
            if tab._tab_text_colour == QColor('red'):
                # Error tabs keep their red text.
                continue
            tab._tab_text_colour = text_colour
            tab.set_tab_icon_and_colour()

    def on_load_front_panel(self,*args,**kwargs):
        filepath = file_dialogs.open_file(
            None,
            "Select file to load",
            self.exp_config.get('paths', 'experiment_shot_storage'),
            "HDF5 files (*.h5 *.hdf5)",
        )
        if filepath is not None:
            # Qt greys out OK for a typed name that lacks the filter's extension, but
            # Enter still selects it, so the extension is checked here.
            if filepath.endswith('.h5') or filepath.endswith('.hdf5'):
                try:
                    if self.question_dialog("""Warning: This will modify front panel values and cause device output values to update.
                    \nShot execution state will be replaced.
                    \n
                    \nNote: Channels that are locked will not be updated.\n\nDo you wish to continue?"""):
                        front_panel_settings = FrontPanelSettings(filepath, self.connection_table)
                        settings,question,error,tab_data = front_panel_settings.restore()

                        self.restore_window(tab_data)
                        self.order_tabs(tab_data)
                        self.update_all_tab_settings(settings,tab_data)

                        self.shot_executor.restore_save_data(
                            self._restore_shot_execution_data(tab_data['BLACS settings'])
                        )
                        plugins.manager.restore_save_data(
                            ast.literal_eval(tab_data['BLACS settings'].get('plugin_data', '{}'))
                        )
                except Exception as e:
                    logger.exception("Unable to load the front panel in %s."%(filepath))
                    self.error_dialog(f"Unable to load the front panel. The error encountered is printed below.\n\n{e}", information=True)
            else:
                self.error_dialog("You did not select a file ending with .h5 or .hdf5. Please try again", information=True)
                QTimer.singleShot(10,self.on_load_front_panel)

    def on_save_exit(self):
        data = self.front_panel_settings.get_save_data()

        if len(self.failed_device_settings) > 0:
            message = (f'Save data from broken tabs? \n Broken tabs are: \n {list(self.failed_device_settings.keys())}')
            if self.question_dialog(message):
                data[0].update(self.failed_device_settings)

        self.front_panel_settings.save_front_panel_to_h5(self.settings_path,data[0],data[1],data[2],data[3],{"overwrite":True},force_new_conn_table=True)
        logger.info('Shutting down workers')
        for tab in self.tablist.values():
            if hasattr(tab, 'shutdown_workers'):
                tab.shutdown_workers()

        QTimer.singleShot(100, self.finalise_quit)

    def finalise_quit(self, deadline=None, pending_threads=None):
        logger.info('finalise_quit called')
        WORKER_SHUTDOWN_TIMEOUT = 2
        if deadline is None:
            deadline = time.time() + WORKER_SHUTDOWN_TIMEOUT
        if pending_threads is None:
            pending_threads = {}
        overdue = time.time() > deadline
        for name, tab in list(self.tablist.items()):
            # Immediately close tabs that don't support finalise_close_tab()
            if not hasattr(tab, 'finalise_close_tab'):
                try:
                    current_page = tab.close_tab(finalise=False)
                except Exception as e:
                    logger.error('Couldn\'t close tab:\n%s' % str(e))
                del self.tablist[name]
                continue
            fatal_error = tab.state == 'fatal error'
            if not tab.shutdown_workers_complete and overdue or fatal_error:
                # Give up on cleanly shutting down this tab's worker processes:
                tab.shutdown_workers_complete = True
            if tab.shutdown_workers_complete:
                if name not in pending_threads:
                    # Worker shutdown completed or was given up on.
                    try:
                        current_page = tab.close_tab(finalise=False)
                    except Exception as e:
                        logger.error('Couldn\'t close tab:\n%s' % str(e))
                        del self.tablist[name]
                    else:
                        # finalise_close_tab can block, so it runs in a thread; it has its
                        # own timeout, so nothing here tracks how long it takes.
                        pending_threads[name] = inthread(
                            tab.finalise_close_tab, current_page
                        )
                elif not pending_threads[name].is_alive():
                    # finalise_close_tab completed, tab is closed and worker terminated
                    pending_threads[name].join()
                    del pending_threads[name]
                    del self.tablist[name]
        if not self.tablist and (
            overdue or not self.shot_executor.final_report_pending()
        ):
            # All tabs are closed. The shot executor reports its held outcome from a
            # daemon thread on its way out, so the quit waits for it, but only until
            # the deadline: a silent runmanager must not hold the quit open, and the
            # shot keeps its place in runmanager's queue.
            if overdue and self.shot_executor.final_report_pending():
                # The loop has not reached its report and the process ends here, so
                # name the held run now.
                self.shot_executor.name_held_outcome()
            self.exit_complete = True
            logger.info('quitting')
            return
        QTimer.singleShot(100, lambda: self.finalise_quit(deadline, pending_threads))
            

    def on_save_front_panel(self,*args,**kwargs):
        data = self.front_panel_settings.get_save_data()

        current_file = file_dialogs.save_file(
            None,
            "Save BLACS state",
            self.exp_config.get('paths', 'experiment_shot_storage'),
            "HDF5 files (*.h5)",
        )
        if current_file is not None:
            if not current_file.endswith('.h5'):
                current_file += '.h5'
            self.front_panel_settings.save_front_panel_to_h5(current_file,data[0],data[1],data[2],data[3])

    def on_open_preferences(self,*args,**kwargs):
        self.settings.create_dialog()

class BlacsServer(ZMQServer):
    def __init__(self, blacs, *args, **kwargs):
        # Set first, as the server answers from the moment it starts:
        self.blacs = blacs
        super().__init__(*args, **kwargs)

    def handle_get_status(self):
        """Report what BLACS is doing, for a runmanager user who cannot see it.

        Read-only and the only command here: enabling requests, clearing what
        stopped them, restarting a device and aborting a shot stay with the
        operator at this apparatus. Not run on the GUI thread, so a BLACS busy
        with a shot still answers."""
        return self.blacs.shot_executor.get_status_snapshot()


if __name__ == '__main__':
    if 'tracelog' in sys.argv:
        import labscript_utils.tracelog
        labscript_utils.tracelog.log(os.path.join(BLACS_DIR, 'blacs_trace.log'),
                                     ['__main__','BLACS.tab_base_classes',
                                      'qtutils',
                                      'labscript_utils.qtwidgets.ddsoutput',
                                      'labscript_utils.qtwidgets.analogoutput',
                                      'BLACS.hardware_interfaces.ni_pcie_6363',
                                      'BLACS.hardware_interfaces.output_classes',
                                      'BLACS.device_base_class',
                                      'BLACS.tab_base_classes',
                                      'BLACS.plugins.connection_table',
                                      'BLACS.recompile_and_restart',
                                      'filewatcher',
                                      'queue',
                                      'notifications',
                                      'connections',
                                      'settings',
                                      'front_panel_settings',
                                      'labscript_utils.h5_lock',
                                      'labscript_utils.shared_drive',
                                      'labscript_utils.labconfig',
                                      'zprocess',
                                     ], sub=True)

    splash.update_text('loading labconfig')
    required_config_params = {
        "default": ["apparatus_name", "app_saved_configs"],
        "paths": ["shared_drive", "connection_table_h5", "connection_table_py",],
        "ports": ["blacs", "lyse"],
    }
    exp_config = LabConfig(required_params=required_config_params)
    settings_path = get_default_appconfig_file(
        exp_config, 'blacs', f'{hostname()}_BLACS.h5', ensure_directory=True
    )

    splash.update_text('loading connection table')
    logger.info('About to load connection table: %s'%exp_config.get('paths','connection_table_h5'))
    connection_table_h5_file = exp_config.get('paths','connection_table_h5')
    connection_table = ConnectionTable(connection_table_h5_file, logging_prefix='BLACS', exceptions_in_thread=True)

    logger.info('connection table loaded')

    splash.update_text('initialising Qt application')
    qapplication = get_qapplication()
    logger.info('QApplication instantiated')
    app = BLACS(qapplication)

    logger.info('BLACS instantiated')

    splash.hide()

    sys.exit(qapplication.exec())
