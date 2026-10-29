#####################################################################
#                                                                   #
# /blacs/tab_base_classes.py                                        #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
from zprocess import Process, Interruptor, Interrupted
import time
import sys
import threading
import traceback
import logging
import warnings
import queue
import pickle
from html import escape
import os
from types import GeneratorType
from bisect import insort

from qtutils.qt.QtCore import Qt, QTimer
from qtutils.qt.QtGui import QColor, QPalette
from qtutils.icons import Icon
from qtutils.qt.QtWidgets import QLabel, QWidget, QPushButton, QApplication, QVBoxLayout

from qtutils import inmain_decorator, inmain, inthread, UiLoader
from labscript_utils.qtwidgets.outputbox import OutputBox
# Imported for its side effect: it registers the :/qtutils icon resources.
import qtutils.icons

from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.ls_zprocess import ProcessTree, RemoteProcessClient
from labscript_utils.shared_drive import path_to_local
from blacs import BLACS_DIR

process_tree = ProcessTree.instance()
from labscript_utils import dedent


class Counter:
    """Returns a different integer each time ``get`` is called."""
    def __init__(self):
        self.i = 0
    def get(self):
        self.i += 1
        return self.i
        
        
MODE_MANUAL = 1
MODE_TRANSITION_TO_BUFFERED = 2
MODE_TRANSITION_TO_MANUAL = 4
MODE_BUFFERED = 8  
            
class StateQueue:
    # Do not drop the inmain decorators: the Qt lock stops Tab.mainloop taking any
    # state until the tab is initialised, which makes the _initialise_worker state
    # that create_worker queues first the first one mainloop sees.
    
    def __init__(self,device_name):
        self.logger = logging.getLogger(f'BLACS.{device_name}.state_queue')
        self.logging_enabled = False
        if self.logging_enabled:
            self.logger.debug("started")
        
        self.list_of_states = []
        self._last_requested_state = None
        # Blocks get() until a queued state matches the requested one.
        self.get_blocking_queue = queue.Queue()

    # Always run in the main thread, so get() and put() cannot race on this.
    @property
    @inmain_decorator(True)    
    def last_requested_state(self):
        return self._last_requested_state
    
    @last_requested_state.setter
    @inmain_decorator(True)
    def last_requested_state(self, value):
        self._last_requested_state = value
     
    def log_current_states(self):
        if self.logging_enabled:
            self.logger.debug('Current items in the state queue: %s'%str(self.list_of_states))
     
    # Main thread only: the list is not thread safe.
    @inmain_decorator(True)   
    def put(self, allowed_states, queue_state_indefinitely, delete_stale_states, data, priority=0):
        """Queue a state; a lower priority number runs first."""
        # Priority, then a unique increasing id, so sorting orders by priority and
        # then by when the state was added.
        state_data = [priority, get_unique_id(), allowed_states, queue_state_indefinitely, delete_stale_states,data]
        insort(self.list_of_states, state_data)
        # Wake get() if it is waiting for this state.
        if self.last_requested_state is not None and allowed_states&self.last_requested_state:
            self.get_blocking_queue.put('new item')
        
        if self.logging_enabled:
            if not isinstance(data[0],str):
                self.logger.debug('New state queued up. Allowed modes: %d, queue state indefinitely: %s, delete stale states: %s, function: %s'%(allowed_states,str(queue_state_indefinitely),str(delete_stale_states),data[0].__name__))
        self.log_current_states()
    
    # Main thread only: the list is not thread safe.
    @inmain_decorator(True)
    def check_for_next_item(self,state):
        # Discard old wake-up messages: the traversal below sees every queued item,
        # so get() should block only until a newer one is added.
        if self.logging_enabled:
            self.logger.debug('Re-initialsing self._get_blocking_queue')
        self.get_blocking_queue = queue.Queue()

        delete_index_list = []
        success = False
        for i,item in enumerate(self.list_of_states):
            priority, unique_id, allowed_states, queue_state_indefinitely, delete_stale_states, data = item
            if self.logging_enabled:
                self.logger.debug('iterating over states in queue')
            if allowed_states&state:
                delete_index_list.append(i)
                
                if self.logging_enabled:
                    self.logger.debug('requested state found in queue')
                
                # Of consecutive entries for the same state function, use the last
                # and delete the rest.
                if delete_stale_states:
                    state_function = data[0]
                    i+=1
                    while i < len(self.list_of_states) and state_function == self.list_of_states[i][5][0]:
                        if self.logging_enabled:
                            self.logger.debug('requesting deletion of stale state')
                        priority, unique_id, allowed_states, queue_state_indefinitely, delete_stale_states, data = self.list_of_states[i]
                        delete_index_list.append(i)
                        i+=1
                
                success = True
                break
            elif not queue_state_indefinitely:
                if self.logging_enabled:
                    self.logger.debug('state should not be queued indefinitely')
                delete_index_list.append(i)
        
        # Delete from the end so earlier deletions do not shift later indices.
        for index in reversed(sorted(delete_index_list)):
            if self.logging_enabled:
                self.logger.debug('deleting state')
            del self.list_of_states[index]
            
        if not success:
            data = None
        return success,data    
        
    def get(self,state):
        """Block until an item allowed in ``state`` is queued, and return its data.

        Call from one thread only, and not the main thread."""
        if self.last_requested_state:
            raise Exception('You have multiple threads trying to get from this queue at the same time. I won\'t allow it!')
    
        self.last_requested_state = state
        while True:
            if self.logging_enabled:
                self.logger.debug('requesting next item in queue with mode %d'%state)
                inmain(self.log_current_states)
            status,data = self.check_for_next_item(state)
            if not status:
                self.get_blocking_queue.get()
            else:
                self.last_requested_state = None
                return data


# Unique ids number timeouts, and order queued states by when they were added.
get_unique_id = Counter().get

def define_state(allowed_modes,queue_state_indefinitely,delete_stale_states=False):
    """Decorate a Tab method so that calling it queues it for the tab's state machine.

    The method runs in the GUI thread when the tab's mode is in ``allowed_modes``
    (MODE_* constants or-ed together). A generator method yields ``queue_work(...)``
    to have a worker do work, and receives the result. Unless
    ``queue_state_indefinitely``, a queued call is dropped, not kept, when the state
    machine reaches it in another mode. With ``delete_stale_states``, of consecutive
    queued calls of one method only the last runs."""
    def wrap(function):
        unescaped_name = function.__name__
        escapedname = '_' + function.__name__
        if allowed_modes < 1 or allowed_modes > 15:
            raise RuntimeError(f'Function {unescaped_name} has been set to run in unknown states. Please make sure allowed states is one or more of MODE_MANUAL,'+
            'MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL and MODE_BUFFERED (or-ed together using the | symbol, eg MODE_MANUAL|MODE_BUFFERED')
        def f(self,*args,**kwargs):
            function.__name__ = escapedname
            self.event_queue.put(allowed_modes,queue_state_indefinitely,delete_stale_states,[function,[args,kwargs]])
        f.__name__ = unescaped_name
        f._allowed_modes = allowed_modes
        return f        
    return wrap
    
        
class Tab:

    ICON_OK = ':/qtutils/fugue/tick'
    ICON_BUSY = ':/qtutils/fugue/hourglass'
    ICON_ERROR = ':/qtutils/fugue/exclamation'
    ICON_FATAL_ERROR = ':/qtutils/fugue/exclamation-red'

    def __init__(self,notebook,settings,restart=False):  
        self.notebook = notebook
        self.settings = settings
        self._device_name = self.settings["device_name"]
        
        self.logger = logging.getLogger(f'BLACS.{self.device_name}')
        self.logger.debug('Started')          
        
        # Updates the tab label while the tab is not part of a notebook.
        self._tab_icon_and_colour_timer = QTimer()
        self._tab_icon_and_colour_timer.timeout.connect(self.set_tab_icon_and_colour)
        self._tab_icon = self.ICON_OK

        self._not_responding_error_message = ''
        self._error = ''
        self._state = ''
        self._time_of_last_state_change = time.time()
        self.not_responding_for = 0
        self.hide_not_responding_error_until = 0
        self._timeouts = set()
        self._timeout_ids = {}
        self._force_full_buffered_reprogram = True
        self.event_queue = StateQueue(self.device_name)
        self.workers = {}
        self._supports_smart_programming = False
        self._restart_receiver = []
        self.shutdown_workers_complete = False

        self.remote_process_client = self._get_remote_configuration()
        self.BLACS_connection = self.settings['connection_table'].find_by_name(self.device_name).BLACS_connection

        self._ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_frame.ui'))
        # Take the text colour from the palette, so OS theme changes are respected.
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
        self._layout = self._ui.device_layout
        self._device_widget = self._ui.device_controls
        self._changed_widget = self._ui.changed_widget
        self._changed_layout = self._ui.changed_layout
        self._changed_widget.hide()        
        
        conn_str = self.BLACS_connection
        if self.remote_process_client is not None:
            conn_str += f" via {self.remote_process_client.host}:{self.remote_process_client.port}"
        
        self._ui.device_name.setText(
            f"<b>{self.device_name}</b> [conn: {conn_str}]"
        )
        elide_label(self._ui.device_name, self._ui.horizontalLayout, Qt.ElideRight)
        elide_label(self._ui.state_label, self._ui.state_label_layout, Qt.ElideRight)

        self._output_box = OutputBox(self._ui.splitter)
        self._ui.splitter.setCollapsible(self._ui.splitter.count() - 2, True)
        self._output_box.output_textedit.hide()

        self._ui.button_clear_smart_programming.clicked.connect(self.on_force_full_buffered_reprogram)
        self._ui.button_clear_smart_programming.setEnabled(False)
        self.force_full_buffered_reprogram = True
        self._ui.button_show_terminal.toggled.connect(self.set_terminal_visible)
        self._ui.button_close.clicked.connect(self.hide_error)
        self._ui.button_restart.clicked.connect(self.restart)        
        self._update_error_and_tab_icon()
        self.supports_smart_programming(False)
        
        self.restore_builtin_save_data(self.settings.get('saved_data', {}))

        # Before the mainloop starts, or self._mode may not exist yet, and after the
        # UI is created, which the setters update.
        self.mode = MODE_MANUAL
        self.state = 'idle'
        
        self._timeout = QTimer()
        self._timeout.timeout.connect(self.check_time)
        self._timeout.start(1000)
                
        self._mainloop_thread = threading.Thread(target = self.mainloop)
        self._mainloop_thread.daemon = True
        self._mainloop_thread.start()
                
        self.notebook.addTab(self._ui,self.device_name)
    
    def _get_remote_configuration(self):
        """Return the zprocess RemoteProcessClient for a remote device, else None."""
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
        """Settings BLACS saves for every tab, such as whether the terminal is
        visible. Not to be overridden."""
        return {'_terminal_visible': self._ui.button_show_terminal.isChecked(),
                '_splitter_sizes': self._ui.splitter.sizes()}

    def get_device_save_data(self):
        """Get the settings returned by the get_save_data() method of the subclass,
        such as camera acquisition settings."""
        if hasattr(self, 'get_save_data'):
            tab_save_data = self.get_save_data()
            if isinstance(tab_save_data, dict):
                return tab_save_data
            self.logger.warning('Incorrect format for tab save data from the get_save_data() method. Data should be a dict. Data was: %s', tab_save_data)
        return {}

    def get_all_save_data(self):
        save_data = self.get_builtin_save_data()
        save_data.update(self.get_device_save_data())
        return save_data

    def restore_builtin_save_data(self, data):
        """Restore the settings from ``get_builtin_save_data``. Settings missing from
        data are not changed. Not to be overridden."""
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
        if message != self._error:
            self._error = message
            self._update_error_and_tab_icon()
    
    @inmain_decorator(True)
    def _update_error_and_tab_icon(self):
        """Update the tab's error message, icon and text colour."""
        prefix = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.0//EN" "http://www.w3.org/TR/REC-html40/strict.dtd">\n<html><head><meta name="qrichtext" content="1" /><style type="text/css">\np, li { white-space: pre-wrap; }\n</style></head><body style=" font-family:"MS Shell Dlg 2"; font-size:7.8pt; font-weight:400; font-style:normal;">'
        suffix = '</body></html>'
        self._ui.error_message.setHtml(prefix+self._not_responding_error_message+self._error+suffix)
        if self._error or self._not_responding_error_message:
            self._ui.notresponding.show()
            self._tab_text_colour = QColor('red')
            if self.error_message:
                if self.state == 'fatal error':
                    self._tab_icon = self.ICON_FATAL_ERROR
                else: 
                    self._tab_icon = self.ICON_ERROR
        else:
            self._ui.notresponding.hide()
            # Take the text colour from the palette, so OS theme changes are respected.
            self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
            if self.state == 'idle':
                self._tab_icon = self.ICON_OK
            else:
                self._tab_icon = self.ICON_BUSY
        self.set_tab_icon_and_colour()
    
    @inmain_decorator(True)
    def set_tab_icon_and_colour(self):
        """Apply self._tab_icon and self._tab_text_colour to the tab."""
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
            self.notebook.tabBar().setTabTextColor(currentpage, self._tab_text_colour)
    
    def get_tab_layout(self):
        return self._layout
    
    @property
    def device_name(self):
        return self._device_name
    
    @property
    def mode(self):
        """The tab's mode: MODE_MANUAL, MODE_TRANSITION_TO_BUFFERED,
        MODE_BUFFERED or MODE_TRANSITION_TO_MANUAL."""
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
        self._update_state_label()
        self._update_error_and_tab_icon()
    
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
            raise RuntimeError(f'self.mode for device {self.device_name} is invalid. It must be one of MODE_MANUAL, MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL or MODE_BUFFERED')
    
        self._ui.state_label.setText(f'<b>{mode} mode</b> - State: {self.state}')
    
    def create_worker(self,name,WorkerClass,workerargs=None):
        """Set up a worker process called ``name``.

        ``WorkerClass`` is a Worker subclass, or the fully qualified import path of
        one. Use the path when the class's module has imports that the main process
        must not run or cannot satisfy, as with a remote worker. ``workerargs``
        become attributes of the worker, along with the device's connection table
        properties, which they override; a name the Worker class already has is
        skipped with a warning. The process starts once the state machine
        mainloop runs, so startup errors are handled like any other."""

        if workerargs is None:
            workerargs = {}
        # Connection table properties, unless workerargs already set them.
        conntable = self.settings['connection_table']
        for key, value in conntable.find_by_name(self.device_name).properties.items():
            workerargs.setdefault(key, value)
        workerargs['is_remote'] = self.remote_process_client is not None

        if name in self.workers:
            raise Exception(f'There is already a worker process with name: {name}')
        if name == 'GUI':
            # The status bar shows "(GUI)" for work done in the GUI, so no worker may
            # have that name.
            raise Exception('You cannot call a worker process "GUI". Why would you want to? Your worker process cannot interact with the BLACS GUI directly, so you are just trying to confuse yourself!')
        
        if isinstance(WorkerClass, type):
            worker = WorkerClass(
                process_tree,
                output_redirection_port=self._output_box.port,
                remote_process_client=self.remote_process_client,
                startup_timeout=30
                )
        elif isinstance(WorkerClass, str):
            # An import path: zprocess.Process imports the class in the subprocess only.
            worker = Process(
                process_tree,
                output_redirection_port=self._output_box.port,
                remote_process_client=self.remote_process_client,
                startup_timeout=30,
                subclass_fullname=WorkerClass
            )
        else:
            raise TypeError(WorkerClass)
        self.workers[name] = (worker,None,None)
        self.event_queue.put(MODE_MANUAL|MODE_BUFFERED|MODE_TRANSITION_TO_BUFFERED|MODE_TRANSITION_TO_MANUAL,True,False,[Tab._initialise_worker,[(name, workerargs),{}]], priority=-1)
       
    def _initialise_worker(self, worker_name, workerargs):
        yield (self.queue_work(worker_name, 'init', worker_name, self.device_name, workerargs))
        if self.error_message:
            raise Exception('Device failed to initialise')
               
    @define_state(MODE_MANUAL|MODE_BUFFERED|MODE_TRANSITION_TO_BUFFERED|MODE_TRANSITION_TO_MANUAL,True)  
    def _timeout_add(self,delay,execute_timeout):
        QTimer.singleShot(delay,execute_timeout)
    
    def statemachine_timeout_add(self,delay,statefunction,*args,**kwargs):
        """Queue ``statefunction(*args, **kwargs)`` now, and again ``delay`` ms after each run.

        ``statefunction`` is a ``define_state`` method of this tab, queued only
        while the tab is in one of its allowed modes. Adding the same function again
        replaces its earlier timeout. Stop one with ``statemachine_timeout_remove``."""
        self._timeouts.add(statefunction)
        # Runs the timeout once, then queues itself again after the delay.
        def execute_timeout():
            # Only while still registered, and not replaced by a newer timeout for
            # the same function.
            if statefunction in self._timeouts and self._timeout_ids[statefunction] == unique_id:
                if statefunction._allowed_modes&self.mode:
                    statefunction(*args, **kwargs)
                self._timeout_add(delay,execute_timeout)
            
        # A unique id tells this timeout from a later one added for the same function.
        unique_id = get_unique_id()
        self._timeout_ids[statefunction] = unique_id
        execute_timeout()
        
    def statemachine_timeout_remove(self,statefunction):
        """Stop the timeout for ``statefunction``, and return True if there was one."""
        if statefunction in self._timeouts:
            self._timeouts.remove(statefunction)
            return True
        return False
    
    def statemachine_timeout_remove_all(self):
        """Stop every timeout, and return True if there was at least one."""
        # Do not simplify: the set must be tested before it is replaced.
        if self._timeouts:
            self._timeouts = set()
            return True
        else:
            self._timeouts = set()
            return False        
    
    @define_state(MODE_MANUAL|MODE_BUFFERED|MODE_TRANSITION_TO_BUFFERED|MODE_TRANSITION_TO_MANUAL,True)
    def shutdown_workers(self):
        """Ask all workers to shutdown"""
        for worker_name in self.workers:
            yield(self.queue_work(worker_name, 'shutdown'))
        self.shutdown_workers_complete = True

    def close_tab(self, finalise=True):
        """Close the tab, terminate subprocesses and join the mainloop thread.

        With ``finalise=False`` do neither, and the caller must call
        ``finalise_close_tab()`` to do these potentially blocking operations."""
        self.logger.info('close_tab called')
        self._timeout.stop()
        for worker, to_worker, from_worker in self.workers.values():
            # Free the mainloop if it is blocked starting a worker or talking to one.
            worker.interrupt_startup()
            if to_worker is not None:
                to_worker.interrupt()
                from_worker.interrupt()
        # Wake the mainloop if it is blocked on the event queue, and tell it to quit.
        if self._mainloop_thread.is_alive():
            self.event_queue.put(MODE_MANUAL|MODE_BUFFERED|MODE_TRANSITION_TO_BUFFERED|MODE_TRANSITION_TO_MANUAL,True,False,['_quit',None],priority=-1)
        self.notebook = self._ui.parentWidget().parentWidget()
        currentpage = None
        if self.notebook:
            currentpage = self.notebook.indexOf(self._ui)
            self.notebook.removeTab(currentpage)
            temp_widget = QLabel("Waiting for tab mainloop and worker(s) to exit")
            temp_widget.setAlignment(Qt.AlignCenter)
            self.notebook.insertTab(currentpage, temp_widget, f'[{self.device_name}]')
            self.notebook.tabBar().setTabIcon(currentpage, Icon(self.ICON_BUSY))
            self.notebook.tabBar().setTabTextColor(currentpage, QColor('grey'))
            self.notebook.setCurrentWidget(temp_widget)  
        if finalise:
            self.finalise_close_tab(currentpage)
        return currentpage
    
    def finalise_close_tab(self, currentpage):
        TERMINATE_TIMEOUT = 2
        self._mainloop_thread.join(TERMINATE_TIMEOUT)
        if self._mainloop_thread.is_alive():
            self.logger.warning("mainloop thread of %s did not stop", self.device_name)
        kwargs = {'wait_timeout': TERMINATE_TIMEOUT}  # timeout passed to .wait()
        if self.remote_process_client is not None:
            # An Interruptor cuts off talking to the remote process server at the
            # timeout.
            interruptor = Interruptor()
            kwargs['get_interruptor'] = interruptor
            timer = inmain(QTimer)
            inmain(timer.singleShot, int(TERMINATE_TIMEOUT * 1000), interruptor.set)
        try:
            # Pop each worker so its __del__ runs, which tells a remote process
            # server that we are done with it.
            for name in self.workers.copy():
                worker, _, _ = self.workers.pop(name)
                worker.terminate(**kwargs)
        except Interrupted:
            self.logger.warning(
                "Terminating workers of %s timed out", self.device_name
            )
            return
        finally:
            self._output_box.shutdown()
            if self.remote_process_client is not None:
                inmain(timer.stop)
        

    def connect_restart_receiver(self,function):
        if function not in self._restart_receiver:
            self._restart_receiver.append(function)
            
    def disconnect_restart_receiver(self,function):
        if function in self._restart_receiver:
            self._restart_receiver.remove(function)
    
    def restart(self,*args):
        for f in self._restart_receiver:
            try:
                f(self.device_name)
            except Exception:
                self.logger.exception('Could not notify a connected receiver function')
                
        currentpage = self.close_tab(finalise=False)
        self.logger.info('***RESTART***')
        self.settings['saved_data'] = self.get_all_save_data()
        self._restart_thread = inthread(self.continue_restart, currentpage)
        
    def continue_restart(self, currentpage):
        """Run the blocking stages of a restart in a thread, and the GUI stages in
        the main thread."""
        self.finalise_close_tab(currentpage)
        inmain(self.clean_ui_on_restart)
        inmain(self.finalise_restart, currentpage)
        
    def clean_ui_on_restart(self):
        ui = self._ui
        self._ui = None
        ui.setParent(None)
        ui.deleteLater()
        del ui
        
    def finalise_restart(self, currentpage):
        widget = self.notebook.widget(currentpage)
        widget.setParent(None)
        widget.deleteLater()
        del widget
    
        # Breaks if a subclass's __init__ does not take these arguments.
        self.__init__(self.notebook, self.settings,restart=True)
        
        # __init__ adds the tab at the end of the notebook; move it back to where it was.
        self.notebook = self._ui.parentWidget().parentWidget()
        self.notebook.removeTab(self.notebook.indexOf(self._ui))
        self.notebook.insertTab(currentpage,self._ui,self.device_name)
        self.notebook.setCurrentWidget(self._ui)
            
    def queue_work(self,worker_process,worker_function,*args,**kwargs):
        """Describe a call of ``worker_function(*args, **kwargs)`` in a worker.
    
        Yield this from a ``define_state`` method: the named worker runs the call
        and its return value is sent back to the generator. If the call raises,
        ``None`` is sent instead and the error is shown on the tab. The arguments
        and the result must be picklable."""
        return worker_process,worker_function,args,kwargs
        
    def set_terminal_visible(self, visible):
        if visible:
            self._output_box.output_textedit.show()
        else:
            self._output_box.output_textedit.hide()
        self._ui.button_show_terminal.setChecked(visible)

    def hide_error(self):
        # Hide the error until the not-responding time has doubled.
        self.hide_not_responding_error_until = 2*self.not_responding_for
        self._ui.notresponding.hide()  
        self.error_message = ''
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)
        self.set_tab_icon_and_colour()
            
    def check_time(self):
        if self.state in ['idle','fatal error']:
            self.not_responding_for = 0
            if self._not_responding_error_message:
                self._not_responding_error_message = ''
                self._update_error_and_tab_icon()
        else:
            self.not_responding_for = time.time() - self._time_of_last_state_change
        if self.not_responding_for > 5 + self.hide_not_responding_error_until:
            self._ui.notresponding.show()
            hours, remainder = divmod(int(self.not_responding_for), 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                s = f'{hours} hours'
            elif minutes:
                s = f'{minutes} minutes'
            else:
                s = f'{seconds} seconds'
            self._not_responding_error_message = f'The hardware process has not responded for {s}.<br /><br />'
            self._update_error_and_tab_icon()
        return True
        
    def mainloop(self):
        logger = logging.getLogger(f"BLACS.{self.settings['device_name']}.mainloop")
        logger.debug('Starting')
        
        # Keep these references, so a restarted tab's new queue and workers are never
        # reached from this old loop.
        event_queue = self.event_queue
        workers = self.workers
        
        try:
            while True:
                logger.debug('Waiting for next event')
                func, data = event_queue.get(self.mode)
                if func == '_quit':
                    # Posted by close_tab.
                    logger.debug('Received quit signal')
                    break
                args,kwargs = data
                logger.debug('Processing event %s' % func.__name__)
                self.state = f'{func.__name__} (GUI)'
                # Run the state function in the Qt main thread.
                generator = inmain(func,self,*args,**kwargs)
                # Only a generator, which yields queue_work, talks to a worker.
                if isinstance(generator, GeneratorType):
                    # Send each yielded job to its worker, and the result back to the
                    # generator, until it raises StopIteration.
                    generator_running = True
                    try:
                        worker_process,worker_function,worker_args,worker_kwargs = inmain(generator.__next__)
                    except StopIteration:
                        # It finished without yielding a job.
                        generator_running = False
                    while generator_running:
                        try:
                            logger.debug('Instructing worker %s to do job %s'%(worker_process,worker_function) )
                            if worker_function == 'init':
                                # Start the process before its init() runs.
                                self.state = f'Starting worker process ({worker_process})'
                                worker, _, _ = self.workers[worker_process]
                                to_worker, from_worker = worker.start(*worker_args)
                                self.workers[worker_process] = (worker, to_worker, from_worker)
                                worker_args = ()
                                # Hold no reference to the worker indefinitely.
                                del worker
                            worker_arg_list = (worker_function,worker_args,worker_kwargs)
                            # Catches unpicklable arguments before they are sent.
                            try:
                                pickle.dumps(worker_arg_list)
                            except Exception:
                                self.error_message += 'Attempt to pass unserialisable object to child process:'
                                raise
                            to_worker = workers[worker_process][1]
                            from_worker = workers[worker_process][2]
                            to_worker.put(worker_arg_list)
                            self.state = f'{worker_function} ({worker_process})'
                            logger.debug('Waiting for worker to acknowledge job request')
                            success, message, results = from_worker.get()
                            if not success:
                                logger.info('Worker reported failure to start job')
                                raise Exception(message)
                            logger.debug('Worker reported job started, waiting for completion')
                            success,message,results = from_worker.get()
                            if not success:
                                logger.info('Worker reported exception during job')
                                now = time.strftime('%a %b %d, %H:%M:%S ',time.localtime())
                                self.error_message += (f'Exception in worker - {now}:<br />'
                                    f"<FONT COLOR='#ff0000'>{escape(message).replace(' ', '&nbsp;').replace('\n', '<br />')}</FONT><br />")
                            else:
                                logger.debug('Job completed')
                            
                            # We have heard from the worker, so stop hiding the
                            # not-responding error.
                            self.hide_not_responding_error_until = 0
                                
                            logger.debug('returning worker results to function %s' % func.__name__)
                            self.state = f'{func.__name__} (GUI)'
                            next_yield = inmain(generator.send,results) 
                            if next_yield:
                                worker_process,worker_function,worker_args,worker_kwargs = next_yield
                        except StopIteration:
                            logger.debug('Finalising function')
                            generator_running = False
                self.state = 'idle'
        except Interrupted:
            # A restart interrupted the queues.
            logger.debug('Interrupted by tab restart, quitting mainloop')
            return
        except Exception:
            # Report the error, and leave the tab to be restarted.
            message = traceback.format_exc()
            logger.critical('A fatal exception happened:\n %s'%message)
            now = time.strftime('%a %b %d, %H:%M:%S ',time.localtime())
            self.error_message += (f'Fatal exception in main process - {now}:<br /> '
                f"<FONT COLOR='#ff0000'>{escape(message).replace(' ', '&nbsp;').replace('\n', '<br />')}</FONT><br />")
                            
            self.state = 'fatal error'
            inmain(self._ui.button_close.setEnabled,False)
        logger.info('Exiting')
        
        
class Worker(Process):
    def init(self):
        """Override to set the worker up, in the worker process.

        It is the first job the worker runs, after the connection table properties
        and ``workerargs`` have become attributes, overriding a subclass's class
        attributes; a name the Worker base class already has is skipped with a
        warning. Import hardware modules here, not at the top of your file, so they
        load only in the worker process, not in BLACS."""
        pass
    
    def run(self, worker_name, device_name, extraargs):
        self.worker_name = worker_name
        self.device_name = device_name
        from labscript_utils.setup_logging import setup_logging
        setup_logging('BLACS')
        log_name = f'BLACS.{self.device_name}_{self.worker_name}.worker'
        self.logger = logging.getLogger(log_name)
        self.logger.debug('Starting')
        import labscript_utils.excepthook
        labscript_utils.excepthook.set_logger(self.logger)
        from labscript_utils.ls_zprocess import ProcessTree
        process_tree = ProcessTree.instance()
        import labscript_utils.h5_lock
        process_tree.zlock_client.set_process_name(log_name)
        for name, value in extraargs.items():
            # Only the base class's own names are protected: a subclass's class
            # attribute is a default the workerarg overrides.
            if hasattr(Worker, name) or name in vars(self):
                msg = f"""attribute `{name}` overwrites an attribute of the Worker base class
                    with the same name. This may cause unexpected behaviour. Consider
                    renaming it."""
                warnings.warn(dedent(msg), RuntimeWarning)
            else:
                setattr(self, name, value)
        self.mainloop()

    def _transition_to_buffered(self, device_name, h5_file, front_panel_values, fresh):
        # h5_file arrives network-agnostic; subclasses' transition_to_buffered() are
        # given a local path.
        h5_file = path_to_local(h5_file)
        return self.transition_to_buffered(
            device_name, h5_file, front_panel_values, fresh
        )

    def mainloop(self):
        while True:
            self.logger.debug('Waiting for next job request')
            funcname, args, kwargs = self.from_parent.get()
            self.logger.debug('Got job request %s' % funcname)
            try:
                func = getattr(self,funcname)
                success = True
                message = ''
            except AttributeError:
                success = False
                message = traceback.format_exc()
                self.logger.error('Couldn\'t start job:\n %s'%message)
            # Acknowledge the job, telling the parent whether the method exists.
            self.to_parent.put((success,message,None))
            if success:
                self.logger.debug('Starting job %s'%funcname)
                try:
                    results = func(*args,**kwargs)
                    success = True
                    message = ''
                    self.logger.debug('Job complete')
                except Exception:
                    results = None
                    success = False
                    traceback_lines = traceback.format_exception(*sys.exc_info())
                    del traceback_lines[1]
                    message = ''.join(traceback_lines)
                    self.logger.error('Exception in job:\n%s'%message)
                # The results must be picklable to go back to the parent.
                try:
                    pickle.dumps(results)
                except Exception:
                    message = traceback.format_exc()
                    self.logger.error('Job returned unserialisable datatypes, cannot pass them back to parent.\n' + message)
                    message = f'Attempt to pass unserialisable object {results!s} to parent process:\n' + message
                    success = False
                    results = None
                # Final reply: whether the work succeeded, and its results.
                self.to_parent.put((success,message,results))


class PluginTab:
    def __init__(self, notebook, settings):
        self.notebook = notebook
        self.settings = settings
        self._tab_name = self.settings["tab_name"]

        self._ui = UiLoader().load(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plugin_tab_frame.ui'))
        self._layout = self._ui.device_layout
        # Read by set_tab_icon_and_colour and on a theme change, as a device tab's are.
        self._tab_icon = ''
        self._tab_text_colour = self._ui.palette().color(QPalette.ColorRole.Text)

        self._ui.device_name.setText(f"<b>{self.tab_name}</b> [Plugin]")
        elide_label(self._ui.device_name, self._ui.horizontalLayout, Qt.ElideRight)

        self.notebook.addTab(self._ui, self.tab_name)

        self.initialise_GUI()
        self.restore_save_data(self.settings['saved_data'] if 'saved_data' in self.settings else {})

    @inmain_decorator(True)
    def set_tab_icon_and_colour(self):
        """Apply self._tab_icon and self._tab_text_colour to the tab."""
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
            currentpage = self.notebook.indexOf(self._ui)
            self.notebook.removeTab(currentpage)
            temp_widget = QWidget()
            self.notebook.insertTab(currentpage, temp_widget, self.tab_name)
            self.notebook.setCurrentWidget(temp_widget)
        return currentpage

    def initialise_GUI(self):
        return

    def get_save_data(self):
        """Override to return a dict of data to save, which is passed to
        ``restore_save_data`` when the tab is initialised."""
        return {}
        
    def get_device_save_data(self):
        tab_save_data = self.get_save_data()
        return tab_save_data if isinstance(tab_save_data, dict) else {}

    def get_all_save_data(self):
        save_data = self.get_builtin_save_data()
        save_data.update(self.get_device_save_data())
        return save_data


    def restore_save_data(self,data):
        """Override to restore the dict that ``get_save_data`` returned.

        It may be empty or lack keys, as when BLACS first starts on a PC or its
        datastore has been destroyed, so handle that."""
        return

    def update_from_settings(self,settings):
        self.restore_save_data(settings['saved_data'])

    def get_builtin_save_data(self):
        return {}

# Example subclasses of Tab and Worker: the tab has its worker do work in response
# to GUI events.
class MyTab(Tab):
    # restart is true when __init__ is called by a restart.
    def __init__(self,notebook,settings,restart=False):
        # Call the base __init__ first.
        super().__init__(notebook,settings,restart)
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

    # Decorate callbacks with @define_state, so the state machine runs them in turn,
    # not at once. Skip it only for callbacks safe in any state, such as plot axes or
    # appearance, and never call queue_work from an undecorated callback.
    @define_state(MODE_MANUAL,True)  
    def foo(self,button=None):
        self.logger.debug('entered foo')
        # Yielding queue_work has the worker run MyWorker.foo(5,6,7,x='x'). No other
        # event is processed until it finishes, and its return value comes back here.
        results = yield(self.queue_work('My worker','foo', 5,6,7,x='x'))

        self.logger.debug('leaving foo')
    
    # What not to do: this queues work without @define_state. Nothing happens at the
    # click, and the error appears only when some later decorated callback runs.
    def fatal(self):
        self.queue_work('My worker','foo', 5,6,7,x='x')
        
    @define_state(MODE_MANUAL,True)  
    def bar(self,button):
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
        
    # Shows what happens when an unpicklable object is sent to the worker.
    @define_state(MODE_MANUAL,True)  
    def baz_unpickleable(self, button):
        self.logger.debug('entered baz_unpickleable')
        results = yield(self.queue_work('My worker','baz', 5,6,7,x=threading.Lock()))
        self.logger.debug('leaving baz_unpickleable')
    
    # Adding and removing a timeout is safe in any state, so neither needs
    # @define_state.
    def add_baz_timeout(self):
        self.statemachine_timeout_add(2000,self.baz)
    
    def remove_baz_timeout(self):
        self.statemachine_timeout_remove(self.baz)
    
        
class MyWorker(Worker):
    def init(self):
        # init, not __init__, is the first call in the new process. Import hardware
        # modules here, not at the top of the file, so they load only in the worker
        # process, not in BLACS; `global` makes them visible to the other methods.
        global serial; import serial
        self.logger.info('got x! %d' % self.x)
        # Fail now and then, to exercise error handling on init.
        import random
        if random.random() < 0.15:
            raise Exception('bad import!')
        
    # Called when the tab asks for it with queue_work; its return value goes back to
    # the tab's generator.
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
        self.logger.debug('working on baz: time is %s'%repr(time.time()))
        time.sleep(0.5)
        if kwargs['return_queue']:
            return queue.Queue()
        return f'results{zzz}!!!'

if __name__ == '__main__':
    import logging.handlers
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
    
    class FakeConnection:
        def __init__(self):
            self.BLACS_connection = 'None'
            self.properties = {}
    class FakeConnectionTable:
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
