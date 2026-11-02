#####################################################################
#                                                                   #
# /tab_base_classes.py                                              #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import time
import sys
import logging
import queue
import threading
from html import escape
import os

from qtutils.qt.QtCore import Qt, QTimer
from qtutils.qt.QtGui import QColor, QPalette
from qtutils.qt.QtWidgets import QLabel, QWidget, QPushButton, QApplication, QVBoxLayout

from qtutils import inmain_decorator, inmain, inmain_later, inthread, UiLoader
from labscript_utils.qtwidgets.outputbox import OutputBox
import qtutils.icons
from qtutils.icons import Icon

from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.ls_zprocess import RemoteProcessClient
from labscript_utils.shared_drive import path_to_local
from labscript_utils.workers import (
    Counter, StateQueue, StateMachine, WorkersMixin, Worker as _Worker,
    WORKER_ABORT_TIMEOUT, WORKER_SHUTDOWN_TIMEOUT, WORKER_KILL_LOCK_TIMEOUT,
    WORKER_QUIT_TIMEOUT, get_unique_id, define_state as _define_state,
)
from blacs import BLACS_DIR


MODE_MANUAL = 1
MODE_TRANSITION_TO_BUFFERED = 2
MODE_TRANSITION_TO_MANUAL = 4
MODE_BUFFERED = 8


def define_state(allowed_modes, queue_state_indefinitely, delete_stale_states=False):
    def wrap(function):
        if allowed_modes < 1 or allowed_modes > 15:
            raise RuntimeError('Function %s has been set to run in unknown states. Please make sure allowed states is one or more of MODE_MANUAL,' % function.__name__ +
                               'MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL and MODE_BUFFERED (or-ed together using the | symbol, eg MODE_MANUAL|MODE_BUFFERED')
        state = _define_state(
            allowed_modes, queue_state_indefinitely, delete_stale_states
        )(function)
        state._allowed_modes = allowed_modes
        return state
    return wrap


class Tab(WorkersMixin):

    ICON_OK = ':/qtutils/fugue/tick'
    ICON_BUSY = ':/qtutils/fugue/hourglass'
    ICON_ERROR = ':/qtutils/fugue/exclamation'
    ICON_FATAL_ERROR = ':/qtutils/fugue/exclamation-red'
    ICON_STOPPED = ':/qtutils/fugue/control-stop-square'
    ICON_START = ':/qtutils/fugue/control'

    def __init__(self,notebook,settings,restart=False):  
        # Store important parameters
        self.notebook = notebook
        self.settings = settings
        self._device_name = self.settings["device_name"]
        
        # Setup logging
        self.logger = logging.getLogger('BLACS.%s'%(self.device_name))   
        self.logger.debug('Started')          
        
        # Setup the timer for updating that tab text label when the tab is not 
        # actively part of a notebook
        self._tab_icon_and_colour_timer = QTimer()
        self._tab_icon_and_colour_timer.timeout.connect(self.set_tab_icon_and_colour)
        self._tab_icon = self.ICON_OK
        # The notebook, tab index, icon and text colour last set on the tab:
        self._applied_tab_icon_and_colour = None

        # Create instance variables
        self._not_responding_error_message = ''
        self._error = ''
        self._error_html = None
        self._state = ''
        self._time_of_last_state_change = time.time()
        self.not_responding_for = 0
        self.hide_not_responding_error_until = 0
        self._timeouts = set()
        self._timeout_ids = {}
        self._force_full_buffered_reprogram = True
        self._supports_smart_programming = False
        self._restart_receiver = []
        self.shutdown_workers_complete = False
        # Set by shutdown_workers(), as BLACS quits:
        self._blacs_quitting = False

        self.remote_process_client = self._get_remote_configuration()
        self.BLACS_connection = self.settings['connection_table'].find_by_name(self.device_name).BLACS_connection

        # Load the UI
        self._ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_frame.ui'))
        # set tab text color from palette to respect OS theme changes
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
        self._layout = self._ui.device_layout
        self._device_widget = self._ui.device_controls
        self._changed_widget = self._ui.changed_widget
        self._changed_layout = self._ui.changed_layout
        self._changed_widget.hide()        
        
        conn_str = self.BLACS_connection
        if self.remote_process_client is not None:
            conn_str += " via %s:%d" % (self.remote_process_client.host, self.remote_process_client.port)
        
        self._ui.device_name.setText(
            "<b>%s</b> [conn: %s]" % (str(self.device_name), conn_str)
        )
        elide_label(self._ui.device_name, self._ui.horizontalLayout, Qt.ElideRight)
        elide_label(self._ui.state_label, self._ui.state_label_layout, Qt.ElideRight)

        # Insert an OutputBox into the splitter, initially hidden:
        self._output_box = OutputBox(self._ui.splitter)
        self._ui.splitter.setCollapsible(self._ui.splitter.count() - 2, True)
        self._output_box.output_textedit.hide()

        # connect signals
        self._ui.button_clear_smart_programming.clicked.connect(self.on_force_full_buffered_reprogram)
        self._ui.button_clear_smart_programming.setEnabled(False)
        self.force_full_buffered_reprogram = True
        self._ui.button_show_terminal.toggled.connect(self.set_terminal_visible)
        self._ui.button_close.clicked.connect(self.hide_error)
        self._ui.button_restart.clicked.connect(self.restart)
        self._ui.button_stop.clicked.connect(self._on_stop_clicked)
        self._update_error_and_tab_icon()
        self.supports_smart_programming(False)
        
        # Restore settings:
        self.restore_builtin_save_data(self.settings.get('saved_data', {}))

        # This must be done after the UI is created:
        self._state_display_update_pending = False
        self.mode = MODE_MANUAL
        self.state = 'idle'
        
        # Setup the not responding timeout
        self._timeout = QTimer()
        self._timeout.timeout.connect(self.check_time)
        self._timeout.start(1000)
                
        self._init_workers(
            'BLACS', self.device_name,
            output_redirection_port=self._output_box.port,
            remote_process_client=self.remote_process_client,
            allowed_modes=15,
        )
                
        # Add the tab to the notebook
        self.notebook.addTab(self._ui,self.device_name)
    
    def _get_remote_configuration(self):
        # Create and return zprocess remote process client, if the device is configured
        # as a remote device, else None:
        PRIMARY_BLACS = '__PrimaryBLACS'
        table = self.settings['connection_table']
        properties = table.find_by_name(self.device_name).properties
        if properties.get('gui', PRIMARY_BLACS) != PRIMARY_BLACS:
            msg = "Remote BLACS GUIs not yet supported by BLACS"
            raise NotImplementedError(msg)
        remote_server_name = properties.get('worker', PRIMARY_BLACS)
        if remote_server_name != PRIMARY_BLACS:
            remote_server_device = table.find_by_name(remote_server_name)
            if remote_server_device.parent.name != PRIMARY_BLACS:
                msg = "Multi-hop remote workers not yet supported by BLACS"
                raise NotImplementedError(msg) 
            remote_host, remote_port = remote_server_device.parent_port.rsplit(':', 1)
            remote_port = int(remote_port)
            return RemoteProcessClient(remote_host, remote_port)
        return None

    def get_builtin_save_data(self):
        """Get builtin settings to be restored like whether the terminal is
        visible. Not to be overridden."""
        return {'_terminal_visible': self._ui.button_show_terminal.isChecked(),
                '_splitter_sizes': self._ui.splitter.sizes()}

    def get_device_save_data(self):
        """Return custom device data to archive in shot files.

        Defaults to get_save_data(). Override to exclude display preferences;
        application settings still save the complete get_save_data() dict."""
        if hasattr(self, 'get_save_data'):
            tab_save_data = self.get_save_data()
            if isinstance(tab_save_data, dict):
                return tab_save_data
            self.logger.warning('Incorrect format for tab save data from the get_save_data() method. Data should be a dict. Data was: %s', tab_save_data)
        return {}

    def get_all_save_data(self):
        save_data = self.get_builtin_save_data()
        if hasattr(self, 'get_save_data'):
            tab_save_data = self.get_save_data()
            if isinstance(tab_save_data, dict):
                save_data.update(tab_save_data)
            else:
                self.logger.warning('Incorrect format for tab save data from the get_save_data() method. Data should be a dict. Data was: %s', tab_save_data)
        return save_data

    def restore_builtin_save_data(self, data):
        """Restore builtin settings to be restored like whether the terminal is
        visible. Not to be overridden. Settings missing from data are not changed."""
        if '_terminal_visible' in data:
            self.set_terminal_visible(data['_terminal_visible'])
        if '_splitter_sizes' in data:
            self._ui.splitter.setSizes(data['_splitter_sizes'])

    def update_from_settings(self, settings):
        self.restore_builtin_save_data(settings['saved_data'])

    def supports_smart_programming(self,support):
        self._supports_smart_programming = bool(support)
        if self._supports_smart_programming:
            self._ui.button_clear_smart_programming.show()
        else:
            self._ui.button_clear_smart_programming.hide()
    
    def on_force_full_buffered_reprogram(self):
        self.force_full_buffered_reprogram = True

    @property
    def force_full_buffered_reprogram(self):
        return self._force_full_buffered_reprogram
        
    @force_full_buffered_reprogram.setter
    def force_full_buffered_reprogram(self,value):
        self._force_full_buffered_reprogram = bool(value)
        self._ui.button_clear_smart_programming.setEnabled(not bool(value))
    
    @property
    @inmain_decorator(True)
    def error_message(self):
        return self._error
    
    @error_message.setter
    @inmain_decorator(True)
    def error_message(self,message):
        #print message
        #print self._error
        if message != self._error:
            self._error = message
            self._update_error_and_tab_icon()
    
    @inmain_decorator(True)
    def _update_error_and_tab_icon(self):
        """Udate and show the error message for the tab, and update the icon
        and text colour on the tab"""
        prefix = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">\n<html><head><meta name="qrichtext" content="1" /><style type="text/css">\np, li { white-space: pre-wrap; }\n</style></head><body style=" font-family:"MS Shell Dlg 2"; font-size:7.8pt; font-weight:400; font-style:normal;">'
        suffix = '</body></html>'
        #print threading.current_thread().name
        error_html = prefix+self._not_responding_error_message+self._error+suffix
        if error_html != self._error_html:
            self._error_html = error_html
            self._ui.error_message.setHtml(error_html)
        if self.state in ('stopping', 'stopped'):
            self._ui.notresponding.show()
            self._tab_text_colour = QColor('grey')
            if self.state == 'stopped':
                self._tab_icon = self.ICON_STOPPED
            else:
                self._tab_icon = self.ICON_BUSY
        elif self._error or self._not_responding_error_message:
            self._ui.notresponding.show()
            self._tab_text_colour = QColor('red')
            if self.error_message:
                if self.state == 'fatal error':
                    self._tab_icon = self.ICON_FATAL_ERROR
                else: 
                    self._tab_icon = self.ICON_ERROR
        else:
            self._ui.notresponding.hide()
            # set tab text color from palette to respect OS theme changes
            self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
            if self.state == 'idle':
                self._tab_icon = self.ICON_OK
            else:
                self._tab_icon = self.ICON_BUSY
        self.set_tab_icon_and_colour()
    
    @inmain_decorator(True)
    def set_tab_icon_and_colour(self):
        """Set the tab icon and the colour of its text to the values of
        self._tab_icon and self._tab_text_colour respectively"""
        if self._ui.parentWidget() is None:
            return
        self.notebook = self._ui.parentWidget().parentWidget()
        if self.notebook is not None:
            currentpage = self.notebook.indexOf(self._ui)
            if currentpage == -1:
                # shutting down:
                return
            applied = (self.notebook, currentpage, self._tab_icon, QColor(self._tab_text_colour))
            if applied == self._applied_tab_icon_and_colour:
                return
            self._applied_tab_icon_and_colour = applied
            icon = Icon(self._tab_icon)
            self.notebook.tabBar().setTabIcon(currentpage, icon)
            self.notebook.tabBar().setTabTextColor(currentpage, self._tab_text_colour)
    
    def get_tab_layout(self):
        return self._layout
    
    @property
    def device_name(self):
        return self._device_name
    
    # sets the mode, switches between MANUAL, BUFFERED, TRANSITION_TO_BUFFERED and TRANSITION_TO_STATIC
    @property
    def mode(self):
        return self._mode
    
    @mode.setter
    def mode(self,mode):
        self._mode = mode
        self._update_state_label()
        
    @property
    def state(self):
        return self._state
        
    @state.setter
    def state(self,state):
        self._state = state        
        self._time_of_last_state_change = time.time()
        self._queue_state_display_update()

    def _queue_state_display_update(self):
        """Post an update of the state label and tab icon to the main thread without
        waiting for it to run. An update requested while one is already posted is
        merged into it."""
        if self._state_display_update_pending:
            return
        self._state_display_update_pending = True
        inmain_later(self._update_state_display)

    def _update_state_display(self):
        # Runs in the main thread. The pending flag is cleared before the state is
        # read, so that a state set after this point posts another update.
        self._state_display_update_pending = False
        self._update_state_label()
        self._update_error_and_tab_icon()
        self._update_stop_button()

    def _update_stop_button(self):
        """Show the stop button as a start button while the workers are not running"""
        if self.state in ('stopped', 'fatal error'):
            icon = self.ICON_START
            tooltip = 'Start the worker processes and initialise the device'
        else:
            icon = self.ICON_STOPPED
            tooltip = 'Stop the worker processes and release the device'
        if self._ui.button_stop.toolTip() != tooltip:
            self._ui.button_stop.setIcon(Icon(icon))
            self._ui.button_stop.setToolTip(tooltip)
    
    @inmain_decorator(True)
    def _update_state_label(self):
        if self.mode == 1:
            mode = 'Manual'
        elif self.mode == 2:
            mode = 'Transitioning to buffered'
        elif self.mode == 4:
            mode = 'Transitioning to manual'
        elif self.mode == 8:
            mode = 'Buffered'
        else:
            raise RuntimeError('self.mode for device %s is invalid. It must be one of MODE_MANUAL, MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL or MODE_BUFFERED'%(self.device_name))
    
        self._ui.state_label.setText('<b>%s mode</b> - State: %s'%(mode,self.state))
        
        # Todo: Update icon in tab
    
    def create_worker(self,name,WorkerClass,workerargs=None):
        """Set up a worker process. WorkerClass can either be a subclass of Worker, or a
        string containing a fully qualified import path to a worker. The latter is
        useful if the worker class is in a separate file with global imports or other
        import-time behaviour that is undesirable to have run in the main process, for
        example if the imports may not be available to the main process (as may be the
        case once remote worker processes are implemented and the worker may be on a
        separate computer). The worker process will not be started immediately, it will
        be started when the state machine runs its first state. This way errors in
        startup will be handled using the normal state machine machinery.

        Each workerarg, and each connection table property not among them, becomes an
        attribute of the worker before its init() runs, overriding a class attribute of
        WorkerClass. A name that Worker or zprocess's Process uses is skipped with a
        warning."""

        if workerargs is None:
            workerargs = {}
        # Add all connection table properties, if they were not already specified in
        # workerargs:
        conntable = self.settings['connection_table']
        for key, value in conntable.find_by_name(self.device_name).properties.items():
            workerargs.setdefault(key, value)
        workerargs['is_remote'] = self.remote_process_client is not None

        if name == 'GUI':
            # This is here so that we can display "(GUI)" in the status bar and have the user confident this is actually happening in the GUI,
            # not in a worker process named GUI
            raise Exception('You cannot call a worker process "GUI". Why would you want to? Your worker process cannot interact with the BLACS GUI directly, so you are just trying to confuse yourself!')
        
        super().create_worker(name, WorkerClass, workerargs)

    def _worker_run_args(self, worker_name, workerargs):
        return worker_name, self.device_name, workerargs

    def _worker_job_finished(self, success, message):
        if not success:
            now = time.strftime('%a %b %d, %H:%M:%S ', time.localtime())
            self.error_message += ('Exception in worker - %s:<br />' % now +
                                  "<FONT COLOR='#ff0000'>%s</FONT><br />" %
                                  escape(message).replace(' ', '&nbsp;').replace('\n', '<br />'))
        self.hide_not_responding_error_until = 0

    def _worker_fatal_error(self, message):
        now = time.strftime('%a %b %d, %H:%M:%S ', time.localtime())
        self.error_message += ('Fatal exception in main process - %s:<br /> ' % now +
                              "<FONT COLOR='#ff0000'>%s</FONT><br />" %
                              escape(message).replace(' ', '&nbsp;').replace('\n', '<br />'))
        self._ui.button_close.setEnabled(False)
       
    def _initialise_worker(self, worker_name, workerargs):
        yield self.queue_work(worker_name, 'init', *self._worker_run_args(worker_name, workerargs))
        if self.error_message:
            raise Exception('Device failed to initialise')
               
    @define_state(MODE_MANUAL|MODE_BUFFERED|MODE_TRANSITION_TO_BUFFERED|MODE_TRANSITION_TO_MANUAL,True)  
    def _timeout_add(self,delay,execute_timeout):
        QTimer.singleShot(delay,execute_timeout)
    
    def statemachine_timeout_add(self,delay,statefunction,*args,**kwargs):
        # Add the timeout to our set of registered timeouts. Timeouts
        # can thus be removed by the user at ay time by calling
        # self.timeouts.remove(function)
        self._timeouts.add(statefunction)
        # Here's a function which executes the timeout once, then queues
        # itself up again after a delay:
        def execute_timeout():
            # queue up the state function, but only if it hasn't been
            # removed from self.timeouts:
            if statefunction in self._timeouts and self._timeout_ids[statefunction] == unique_id:
                # Only queue up the state if we are in an allowed mode
                if statefunction._allowed_modes&self.mode:
                    statefunction(*args, **kwargs)
                # queue up another call to this function (execute_timeout)
                # after the delay time:
                self._timeout_add(delay,execute_timeout)
            
        # Store a unique ID for this timeout so that we don't confuse 
        # other timeouts for this one when checking to see that this
        # timeout hasn't been removed:
        unique_id = get_unique_id()
        self._timeout_ids[statefunction] = unique_id
        # queue the first run:
        #QTimer.singleShot(delay,execute_timeout)    
        execute_timeout()
        
    # Returns True if the timeout was removed
    def statemachine_timeout_remove(self,statefunction):
        if statefunction in self._timeouts:
            self._timeouts.remove(statefunction)
            return True
        return False
    
    # returns True if at least one timeout was removed, else returns False
    def statemachine_timeout_remove_all(self):
        # As a consistency check, we overwrite self._timeouts to an empty set always
        # This must be done after the check to see if it is empty (if self._timeouts) so do not refactor this code!
        if self._timeouts:
            self._timeouts = set()
            return True
        else:
            self._timeouts = set()
            return False        
    
    def shutdown_workers(self):
        """Run the workers' shutdown() methods as BLACS quits, and set
        shutdown_workers_complete once they have returned or SHUTDOWN_TIMEOUT seconds
        have passed. A restart under way does not then rebuild the tab."""
        self._blacs_quitting = True

        def complete():
            self.shutdown_workers_complete = True

        self._shut_down_workers_then(complete)

    def close_tab(self, finalise=True):
        """Close the tab, terminate subprocesses and join the job thread. If
        finalise=False, then do not terminate subprocesses or join the job thread. In this
        case, callers must manually call finalise_close_tab() to perform these
        potentially blocking operations"""
        self.logger.info('close_tab called')
        self._timeout.stop()
        self.close_workers()
        self.notebook = self._ui.parentWidget().parentWidget()
        currentpage = None
        if self.notebook:
            #currentpage = self.notebook.get_current_page()
            currentpage = self.notebook.indexOf(self._ui)
            self.notebook.removeTab(currentpage)
            temp_widget = QLabel("Waiting for worker(s) to exit")
            temp_widget.setAlignment(Qt.AlignCenter)
            self.notebook.insertTab(currentpage, temp_widget, '[%s]' % self.device_name)
            self.notebook.tabBar().setTabIcon(currentpage, Icon(self.ICON_BUSY))
            self.notebook.tabBar().setTabTextColor(currentpage, QColor('grey'))
            self.notebook.setCurrentWidget(temp_widget)  
        if finalise:
            self.finalise_close_tab(currentpage)
        return currentpage
    
    def finalise_close_tab(self, currentpage):
        try:
            self.finalise_workers()
        finally:
            self._output_box.shutdown()

    def connect_restart_receiver(self,function):
        if function not in self._restart_receiver:
            self._restart_receiver.append(function)
            
    def disconnect_restart_receiver(self,function):
        if function in self._restart_receiver:
            self._restart_receiver.remove(function)
    
    def _notify_restart_receivers(self):
        for f in self._restart_receiver:
            try:
                f(self.device_name)
            except Exception:
                self.logger.exception('Could not notify a connected receiver function')

    def _on_stop_clicked(self):
        if self.state in ('stopped', 'fatal error'):
            self.restart()
        else:
            self._stop_workers()

    def _stop_workers(self):
        """Run the workers' shutdown() methods and end the worker processes. The tab
        stays open with its state machine stopped until it is restarted."""
        self._notify_restart_receivers()
        self._ui.button_stop.setEnabled(False)
        self._ui.button_restart.setEnabled(False)
        self._shut_down_workers_then(self._stop_after_shutdown)

    def _stop_after_shutdown(self):
        self.logger.info('***STOP***')
        self.close_workers()
        now = time.strftime('%a %b %d, %H:%M:%S', time.localtime())
        self.error_message += (
            'Worker processes stopped - %s<br />' % now
            + 'Click the start button to start them and initialise the device.<br />'
        )
        self._ui.button_close.setEnabled(False)
        self.state = 'stopping'
        inthread(self._end_workers_for_stop)

    def _end_workers_for_stop(self):
        self._end_workers()
        inmain_later(self._stopped)

    def _stopped(self):
        self.state = 'stopped'
        self._ui.button_stop.setEnabled(True)
        self._ui.button_restart.setEnabled(True)

    def restart(self,*args):
        self._notify_restart_receivers()
        self._ui.button_stop.setEnabled(False)
        self._ui.button_restart.setEnabled(False)
        self._shut_down_workers_then(self._restart_after_shutdown)

    def _restart_after_shutdown(self):
        currentpage = self.close_tab(finalise=False)
        self.logger.info('***RESTART***')
        self.settings['saved_data'] = self.get_all_save_data()
        self._restart_thread = inthread(self.continue_restart, currentpage)
        
    def continue_restart(self, currentpage):
        """Called in a thread for the stages of restarting that may be blocking, so as to
        not block the main thread. Calls subsequent GUI operations in the main thread once
        finished blocking."""
        self.finalise_close_tab(currentpage)
        inmain(self.finalise_restart, currentpage)
        
    def clean_ui_on_restart(self):
        # Clean up UI
        ui = self._ui
        self._ui = None
        ui.setParent(None)
        ui.deleteLater()
        del ui
        
    def finalise_restart(self, currentpage):
        # BLACS, quitting, closes the tab itself. Checked in the call that removes the UI,
        # so that the quit cannot begin in between and find the UI gone:
        if self._blacs_quitting:
            return
        self.clean_ui_on_restart()
        widget = self.notebook.widget(currentpage)
        widget.setParent(None)
        widget.deleteLater()
        del widget
    
        # Note: the following function call will break if the user hasn't
        # overridden the __init__ function to take these arguments. So
        # make sure you do that!
        self.__init__(self.notebook, self.settings,restart=True)
        
        # The init method is going to place this device tab at the end of the notebook specified
        # Let's remove it from there, and place it the poition it used to be!
        self.notebook = self._ui.parentWidget().parentWidget()
        self.notebook.removeTab(self.notebook.indexOf(self._ui))
        self.notebook.insertTab(currentpage,self._ui,self.device_name)
        self.notebook.setCurrentWidget(self._ui)
        self._applied_tab_icon_and_colour = None
        self.set_tab_icon_and_colour()
            
        # If BLACS is waiting on this tab for something, tell it to abort!
        # self.BLACS.current_queue.put('abort')
    
        
    def set_terminal_visible(self, visible):
        if visible:
            self._output_box.output_textedit.show()
        else:
            self._output_box.output_textedit.hide()
        self._ui.button_show_terminal.setChecked(visible)

    def hide_error(self):
        # dont show the error again until the not responding time has doubled:
        self.hide_not_responding_error_until = 2*self.not_responding_for
        self._ui.notresponding.hide()  
        self.error_message = ''
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
        self.set_tab_icon_and_colour()
            
    def check_time(self):
        if self.state in ['idle','fatal error','stopped']:
            self.not_responding_for = 0
            if self._not_responding_error_message:
                self._not_responding_error_message = ''
                self._update_error_and_tab_icon()
        else:
            self.not_responding_for = time.time() - self._time_of_last_state_change
        if self.not_responding_for > 5 + self.hide_not_responding_error_until:
            self.hide_not_responding_error_for = 0
            self._ui.notresponding.show()
            hours, remainder = divmod(int(self.not_responding_for), 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                s = '%s hours'%hours
            elif minutes:
                s = '%s minutes'%minutes
            else:
                s = '%s seconds'%seconds
            self._not_responding_error_message = 'The hardware process has not responded for %s.<br /><br />'%s
            self._update_error_and_tab_icon()
        return True


class Worker(_Worker):
    def run(self, worker_name, device_name, extraargs):
        self.device_name = device_name
        super().run(
            'BLACS', worker_name, extraargs,
            log_name=f'BLACS.{device_name}_{worker_name}.worker',
            protected_class=Worker,
        )

    def _transition_to_buffered(self, device_name, h5_file, front_panel_values, fresh):
        # The path crosses hosts in network-agnostic form.
        h5_file = path_to_local(h5_file)
        return self.transition_to_buffered(
            device_name, h5_file, front_panel_values, fresh
        )


class PluginTab(object):
    def __init__(self, notebook, settings):
        # Store important parameters
        self.notebook = notebook
        self.settings = settings
        self._tab_name = self.settings["tab_name"]

        # Load the UI
        self._ui = UiLoader().load(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plugin_tab_frame.ui'))
        self._layout = self._ui.device_layout
        self._tab_icon = ''
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)

        self._ui.device_name.setText("<b>%s</b> [Plugin]" % (str(self.tab_name)))
        elide_label(self._ui.device_name, self._ui.horizontalLayout, Qt.ElideRight)

        # Add the tab to the notebook
        self.notebook.addTab(self._ui, self.tab_name)

        # Call the initialise GUI function
        self.initialise_GUI()
        self.restore_save_data(self.settings['saved_data'] if 'saved_data' in self.settings else {})

    @inmain_decorator(True)
    def set_tab_icon_and_colour(self):
        """Set the tab icon and the colour of its text to the values of
        self._tab_icon and self._tab_text_colour respectively"""
        if self._ui.parentWidget() is None:
            return
        self.notebook = self._ui.parentWidget().parentWidget()
        if self.notebook is not None:
            currentpage = self.notebook.indexOf(self._ui)
            if currentpage == -1:
                # shutting down:
                return
            icon = Icon(self._tab_icon)
            self.notebook.tabBar().setTabIcon(currentpage, icon)
            self.notebook.tabBar().setTabTextColor(currentpage, QColor(self._tab_text_colour))

    @property
    def tab_name(self):
        return self._tab_name

    def get_tab_layout(self):
        return self._layout

    def close_tab(self, **kwargs):
        self.notebook = self._ui.parentWidget().parentWidget()
        currentpage = None
        if self.notebook:
            #currentpage = self.notebook.get_current_page()
            currentpage = self.notebook.indexOf(self._ui)
            self.notebook.removeTab(currentpage)
            temp_widget = QWidget()
            self.notebook.insertTab(currentpage, temp_widget, self.tab_name)
            self.notebook.setCurrentWidget(temp_widget)
        return currentpage

    def initialise_GUI(self):
        return

    # This method should be overridden in your plugin class if you want to save any data
    # This method should return a dictionary, and this dictionary will be passed to the restore_save_data()
    # method when the tab is initialised
    def get_save_data(self):
        return {}
        
    def get_device_save_data(self):
        """Return shot data, defaulting to get_save_data(); exclude display preferences
        here without changing what application settings save."""
        tab_save_data = self.get_save_data()
        return tab_save_data if isinstance(tab_save_data, dict) else {}

    def get_all_save_data(self):
        save_data = self.get_builtin_save_data()
        tab_save_data = self.get_save_data()
        if isinstance(tab_save_data, dict):
            save_data.update(tab_save_data)
        return save_data


    # This method should be overridden in your plugin class if you want to restore data
    # (saved by get_save_data()) when the tab is initialised.
    # You will be passed a dictionary of the form specified by your get_save_data() method
    #
    # Note: You must handle the case where the data dictionary is empty (or one or more keys are missing)
    #       This case will occur the first time BLACS is started on a PC, or if the BLACS datastore is destroyed
    def restore_save_data(self,data):
        return

    def update_from_settings(self,settings):
        self.restore_save_data(settings['saved_data'])

    def get_builtin_save_data(self):
        return {}

# Example code! Two classes are defined below, which are subclasses
# of the ones defined above.  They show how to make a Tab class,
# and a Worker class, and get the Tab to request work to be done by
# the worker in response to GUI events.
class MyTab(Tab):
    def __init__(self,notebook,settings,restart=False): # restart will be true if __init__ was called due to a restart
        Tab.__init__(self,notebook,settings,restart) # Make sure to call this first in your __init__!
        self.create_worker('My worker',MyWorker,{'x':7})
        self.initUI()
        
    def initUI(self):
        self.layout = self.get_tab_layout()
        
        foobutton = QPushButton('foo, 10 seconds!')
        barbutton = QPushButton('bar, 10 seconds, then error!')
        bazbutton = QPushButton('baz, 0.5 seconds!')
        addbazbutton = QPushButton('add 2 second timeout to baz')
        removebazbutton = QPushButton('remove baz timeout')
        bazunpickleable= QPushButton('try to pass baz a threading.Lock()')
        fatalbutton = QPushButton('fatal error, forgot to add @define_state to callback!')
        
        self.checkbutton = QPushButton('have baz\nreturn a Queue')
        self.checkbutton.setCheckable(True)
        
        #self.device_widget.addWidget(layout)
        self.layout.addWidget(foobutton)
        self.layout.addWidget(barbutton)
        self.layout.addWidget(bazbutton)
        self.layout.addWidget(addbazbutton)
        self.layout.addWidget(removebazbutton)
        self.layout.addWidget(bazunpickleable)
        self.layout.addWidget(fatalbutton)
        self.layout.addWidget(self.checkbutton)
        
        foobutton.clicked.connect(self.foo)
        barbutton.clicked.connect(self.bar)
        bazbutton.clicked.connect(self.baz)
        fatalbutton.clicked.connect(self.fatal )
        addbazbutton.clicked.connect(self.add_baz_timeout)
        removebazbutton.clicked.connect(self.remove_baz_timeout)
        bazunpickleable.clicked.connect(self.baz_unpickleable)

    # It is critical that you decorate your callbacks with @define_state
    # as below. This makes the function get queued up and executed
    # in turn by our state machine instead of immediately by the
    # GTK mainloop. Only don't decorate if you're certain that your
    # callback can safely happen no matter what state the system is
    # in (for example, adjusting the axis range of a plot, or other
    # appearance settings). You should never be calling queue_work
    # or do_after from un undecorated callback.
    @define_state(MODE_MANUAL,True)  
    def foo(self,button=None):
        self.logger.debug('entered foo')
        #self.toplevel.set_sensitive(False)
        # Here's how you instruct the worker process to do
        # something. When this callback returns, the worker will be
        # requested to do whatever you ask in queue_work (in this
        # case, MyWorker.foo(5,6,7,x='x') ). Then, no events will
        # be processed until that work is done. Once the work is
        # done, whatever has been set with do_after will be executed
        # (in this case self.leave_foo(1,2,3,bar=baz) ).
        results = yield(self.queue_work('My worker','foo', 5,6,7,x='x'))

        #self.toplevel.set_sensitive(True)
        self.logger.debug('leaving foo')
    
    # Here's what's NOT to do: forgetting to decorate a callback with @define_state
    # when it's not something that can safely be done asynchronously
    # to the state machine:
    def fatal(self):
        # This bug could be hard to track because nothing will happen
        # when you click the button -- only once you do some other,
        # correcly decorated callback will it become apparant that
        # something is wrong. So don't make this mistake!
        self.queue_work('My worker','foo', 5,6,7,x='x')
        
    @define_state(MODE_MANUAL,True)  
    def bar(self, button=None):
        self.logger.debug('entered bar')
        results = yield(self.queue_work('My worker','bar', 5,6,7,x=5))
      
        self.logger.debug('leaving bar')
        
    @define_state(MODE_MANUAL,True)  
    def baz(self, button=None):
        print(threading.current_thread().name)
        self.logger.debug('entered baz')
        results = yield(self.queue_work('My worker','baz', 5,6,7,x='x',return_queue=self.checkbutton.isChecked()))
        print(results)
        print(threading.current_thread().name)
        results = yield(self.queue_work('My worker','baz', 4,6,7,x='x',return_queue=self.checkbutton.isChecked()))
        print(results)
        print(threading.current_thread().name)
        self.logger.debug('leaving baz')
        
    # This event shows what happens if you try to send a unpickleable
    # event through a queue to the subprocess:
    @define_state(MODE_MANUAL,True)  
    def baz_unpickleable(self, button=None):
        self.logger.debug('entered baz_unpickleable')
        results = yield(self.queue_work('My worker','baz', 5,6,7,x=threading.Lock()))
        self.logger.debug('leaving baz_unpickleable')
    
    # You don't need to decorate with @define_state if all you're
    # doing is adding a timeout -- adding a timeout can safely be done
    # asynchronously. But you can still decorate if you want, and you
    # should if you're doing other work in the same function call which
    # can't be done asynchronously.
    def add_baz_timeout(self):
        self.statemachine_timeout_add(2000,self.baz)
    
    # Similarly, no @define_state is required here -- same applies as above.    
    def remove_baz_timeout(self):
        self.statemachine_timeout_remove(self.baz)
    
        
class MyWorker(Worker):
    def init(self):
        # You read correctly, this isn't __init__, it's init. It's the
        # first thing that will be called in the new process. You should
        # do imports here, define instance variables, that sort of thing. You
        # shouldn't import the hardware modules at the top of your file,
        # because then they will be imported in both the parent and
        # the child processes and wont be cleanly restarted when the subprocess
        # is restarted. Since we're inside a method call though, you'll
        # have to use global statements for the module imports, as shown
        # below. Either that or you can make them instance variables, ie:
        # import module; self.module = module. Up to you, I prefer
        # the former.
        global serial; import serial
        self.logger.info('got x! %d', self.x)
        # randomly fail this on init, but only occasionally
        import random
        if random.random() < 0.15:
            raise Exception('bad import!')
        
    # Here's a function that will be called when requested by the parent
    # process. There's nothing special about it really. Its return
    # value will be passed as a keyword argument _results to the
    # function which was queued with do_after, if there was one.
    def foo(self,*args,**kwargs):
        self.logger.debug('working on foo!')
        time.sleep(10)
        return 'results!!!'
        
    def bar(self,*args,**kwargs):
        self.logger.debug('working on bar!')
        time.sleep(10)
        raise Exception('error!')
        return 'results!!!'
        
    def baz(self,zzz,*args,**kwargs):
        self.logger.debug('working on baz: time is %r', time.time())
        time.sleep(0.5)
        if kwargs['return_queue']:
            return queue.Queue()
        return 'results%d!!!'%zzz

if __name__ == '__main__':
    import sys
    import logging.handlers
    # Setup logging:
    from labscript_utils.setup_logging import setup_logging
    setup_logging('BLACS')
    logger = logging.getLogger('BLACS')
    handler = logging.handlers.RotatingFileHandler(os.path.join(BLACS_DIR, 'BLACS.log'), maxBytes=1024**2, backupCount=0)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    if sys.stdout is not None and sys.stdout.isatty():
        terminalhandler = logging.StreamHandler(sys.stdout)
        terminalhandler.setFormatter(formatter)
        terminalhandler.setLevel(logging.INFO)
        logger.addHandler(terminalhandler)
    else:
        sys.stdout = sys.stderr = open(os.devnull)
    logger.setLevel(logging.DEBUG)
    logger.info('\n\n===============starting===============\n')

if __name__ == '__main__':
    from labscript_utils.qtwidgets.dragdroptab import DragDropTabWidget
    app = QApplication(sys.argv)
    window = QWidget()
    layout = QVBoxLayout(window)
    notebook = DragDropTabWidget()
    layout.addWidget(notebook)
    
    class FakeConnection(object):
        def __init__(self):
            self.BLACS_connection = 'None'
            self.properties = {}
    class FakeConnectionTable(object):
        def __init__(self):
            pass
        
        def find_by_name(self, device_name):
            return FakeConnection()
    
    connection_table = FakeConnectionTable()
    
    tab1 = MyTab(notebook,settings = {'device_name': 'Example', 'connection_table':connection_table})
    tab2 = MyTab(notebook,settings = {'device_name': 'Example2', 'connection_table':connection_table})
    
    window.show()
    def run():
        app.exec()
        tab1.close_tab()
        tab2.close_tab()
    sys.exit(run())
