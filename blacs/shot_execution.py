#####################################################################
#                                                                   #
# /blacs/shot_execution.py                                          #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
"""``docs/source/shot-management.rst`` is the contract of record for the
runmanager handover, so change either side against it."""
import queue
import logging
import os
import threading
import time
import datetime
import sys
import shutil
from collections import defaultdict, namedtuple
import tempfile

from qtutils.qt.QtCore import Qt

import zprocess
from labscript_utils.ls_zprocess import ProcessTree
process_tree = ProcessTree.instance()
import labscript_utils.h5_lock, h5py

from qtutils import inmain_decorator, inmain

from labscript_utils.qtwidgets.elide_label import elide_label
from labscript_utils.qtwidgets.link_indicator import LinkIndicator
import labscript_utils.qtwidgets.file_dialogs as file_dialogs
from labscript_utils.connections import ConnectionTable
from labscript_utils.file_utils import next_available_indexed_filepath
import labscript_utils.properties
from labscript_utils.shared_drive import path_to_agnostic, path_to_local

from blacs.tab_base_classes import MODE_TRANSITION_TO_BUFFERED, MODE_BUFFERED
import blacs.plugins as plugins

from runmanager.client import (
    BLACS_ABORTED,
    BLACS_COMPLETED,
    BLACS_FAILED,
    BLACS_REJECTED,
    PROVIDER_NONE,
    PROVIDER_PAUSED,
    PROVIDER_PENDING,
    RunmanagerClient,
)

# Default reply deadline for "are you there" (labconfig [timeouts]
# liveness_timeout), paid once per shot while runmanager is absent. Err long, and
# keep it apart from communication_timeout; see shot-management.rst.
LIVENESS_TIMEOUT = 5

# What the runmanager light says. Requesting with nothing offered is not
# idleness; the light is how an operator sees the Request shots button is in.
REQUESTING = 'Requesting shots'
NOT_REQUESTING = 'Not requesting shots'

# One value, so the GUI thread can write it and the server thread read it
# without a lock; see set_status().
PublishedStatus = namedtuple('PublishedStatus', ['text', 'shot_filepath', 'shot_id'])


def device_status(names, description):
    """Say which devices a failure was in, as 'Device a ...' or 'Devices a, b ...'."""
    names = sorted(set(names))
    if not names:
        return f'Device(s) {description}'
    noun = 'Device' if len(names) == 1 else 'Devices'
    return f"{noun} {', '.join(names)} {description}"


class ShotExecutor:

    # Deadline for the last exchange, which hands over an outcome held as the
    # shot loop ends. Bounded, so a runmanager that is not answering cannot hold
    # the quit open.
    OUTCOME_FLUSH_TIMEOUT = 2

    def __init__(self, BLACS, ui):
        self._ui = ui
        self.BLACS = BLACS
        self.last_opened_shots_folder = BLACS.exp_config.get('paths', 'experiment_shot_storage')
        self._manager_running = True
        # Requesting shots always starts off: enabling hardware execution is a
        # deliberate act at this BLACS, never something a restart resumes.
        self._requesting_shots = False
        self.master_pseudoclock = self.BLACS.connection_table.master_pseudoclock
        self._runmanager_request_client = RunmanagerClient()
        self._runmanager_request_error_logged = False
        # The queue tabs report to, so abort signals can be put to it when tabs
        # never respond and are restarted by the user. Replaced for each shot.
        self.notify_queue = queue.Queue()
        # Why requests were stopped, read from outside this class so a runmanager
        # user who is not at BLACS can be shown it.
        self.local_error = None
        # The outcome of the shot just run, held until an exchange carries it to
        # runmanager, and the id of the running shot.
        self._pending_outcome = None
        self._current_shot_id = None
        # What the labels say, readable without the GUI thread. Written only by
        # set_status().
        self.published_status = PublishedStatus('', None, None)
        self._next_rep_index = {}

        self._logger = logging.getLogger('BLACS.ShotExecutor')

        self._ui.shot_request_button.toggled.connect(self._toggle_request_shots)
        self._ui.local_override_browse_button.clicked.connect(
            self.browse_local_override
        )
        self._ui.local_override_lineEdit.textChanged.connect(
            self._ui.local_override_lineEdit.setToolTip
        )

        elide_label(self._ui.shot_status, self._ui.shot_status_verticalLayout, Qt.ElideRight)
        elide_label(self._ui.running_shot_name, self._ui.shot_status_verticalLayout, Qt.ElideLeft)
        # The light polls on its own: with requests off the shot loop never
        # contacts runmanager, so it cannot keep the light current.
        self._runmanager_link = LinkIndicator(
            'runmanager',
            self._runmanager_request_client.host,
            self._runmanager_request_client.port,
        )
        self._ui.runmanager_link_layout.addWidget(self._runmanager_link)
        self._runmanager_link.start()

        self.manager = threading.Thread(target = self.manage)
        self.manager.daemon=True
        self.manager.start()

    def get_save_data(self):
        # Whether shots are requested is left out on purpose: enabling hardware
        # execution is a deliberate act at this BLACS, not a saved state.
        return {'last_opened_shots_folder': self.last_opened_shots_folder,
                'local_override_path': str(self._ui.local_override_lineEdit.text()).strip(),
               }

    def restore_save_data(self,data):
        if 'last_opened_shots_folder' in data:
            self.last_opened_shots_folder = data['last_opened_shots_folder']
        if 'local_override_path' in data and data['local_override_path']:
            self._ui.local_override_lineEdit.setText(str(data['local_override_path']))
        
    @property
    @inmain_decorator(True)
    def manager_running(self):
        return self._manager_running
        
    @manager_running.setter
    @inmain_decorator(True)
    def manager_running(self,value):
        value = bool(value)
        self._manager_running = value
        
    def _toggle_request_shots(self,checked):
        self.requesting_shots = checked

    @property
    @inmain_decorator(True)
    def requesting_shots(self):
        return self._requesting_shots

    @requesting_shots.setter
    @inmain_decorator(True)
    def requesting_shots(self,value):
        value = bool(value)
        if value:
            # Asking for shots again acknowledges whatever stopped them. Nothing is
            # re-checked first: the per-device check made when the shot is
            # programmed stays the final authority.
            self.local_error = None
        self._requesting_shots = value
        if value != self._ui.shot_request_button.isChecked():
            self._ui.shot_request_button.setChecked(value)

    @inmain_decorator(True)
    def stop_requesting_shots(self, reason):
        """Stop asking for shots because something here needs attention.

        Every shot that does not complete comes through here, so the reason is
        kept and shown here and to a runmanager user who cannot see this window.
        It is called from the shot loop but runs on the GUI thread, so recording
        the reason and stopping requests are one step: an operator asking for
        shots in between would otherwise clear the reason."""
        self.local_error = str(reason)
        self.requesting_shots = False

    def browse_local_override(self):
        shot_file = file_dialogs.open_file(
            self._ui,
            'Select shot file',
            self.last_opened_shots_folder,
            'HDF5 files (*.h5 *.hdf5)',
        )
        if not shot_file:
            return

        self.last_opened_shots_folder = os.path.dirname(shot_file)
        self._ui.local_override_lineEdit.setText(shot_file)

    def runmanager_rpc(self, method, unavailable_message, *args, client=None, **kwargs):
        try:
            if client is None:
                client = self._runmanager_request_client
            response = method(client, *args, **kwargs)
            self._runmanager_request_error_logged = False
            return True, response
        except Exception as exc:
            if not self._runmanager_request_error_logged:
                self._logger.warning(unavailable_message, exc, exc_info=exc)
                self._runmanager_request_error_logged = True
            return False, None

    def report_shot_outcome(self, path, status, message=''):
        """Hold how a shot turned out, to be reported on the next exchange.

        Runmanager applies the outcome to the row it offered before choosing
        what to offer next. Only a shot runmanager gave us has an outcome it can
        record, so for a local override shot this does nothing."""
        if self._current_shot_id is None:
            return
        outcome = {
            'shot_id': self._current_shot_id,
            'status': status,
            'message': message,
        }
        if path is not None:
            # The file run, which differs from the one offered when a fresh copy was
            # made to re-run a shot that already held data.
            outcome['path'] = path_to_agnostic(path)
        self._pending_outcome = outcome
        self._current_shot_id = None

    def stop(self):
        """Stop shot execution, as BLACS closes. A shot being programmed or run is
        aborted, as the Abort button does; one being saved, or starting just as
        this is called, completes. The loop's thread ends once it has reported
        that shot. Called from the GUI thread."""
        self._runmanager_link.shutdown()
        self.manager_running = False
        self.notify_queue.put(['Shot Executor', 'abort'])

    def final_report_pending(self):
        """Whether the loop still owes runmanager a word about a shot.

        Read from the GUI thread as BLACS quits: True means ending the process
        now would take the report with it, as the loop's thread is a daemon.
        False once the outcome has been taken or the loop has ended. The loop
        can be a whole communication timeout from noticing a stop, so the
        caller bounds its wait."""
        return self._pending_outcome is not None and self.manager.is_alive()

    def name_held_outcome(self):
        """Log the shot whose outcome runmanager was never told, if one is held."""
        outcome = self._pending_outcome
        if outcome is None:
            return
        self._logger.warning(
            'Runmanager was never told that shot %s %s%s.%s',
            outcome['shot_id'],
            outcome['status'],
            ': %s' % outcome['message'] if outcome['message'] else '',
            ' Its run is in %s.' % path_to_local(outcome['path'])
            if 'path' in outcome
            else '',
        )

    def exchange_with_runmanager(self, request_shot, timeout=None):
        """Report the finished shot's outcome, and ask for the next shot.

        One message does both, so runmanager retires the row it offered before
        choosing what to offer next. The outcome is let go only once runmanager
        has taken it; otherwise it rides on the next exchange.

        Returns ``(response, reached)``: runmanager's reply (its provider
        ``state``, and the ``shot_id`` and ``path`` of any shot offered) and
        whether it was reached. An unreachable runmanager has offered nothing
        and is not reported as paused."""
        no_shot = {'state': PROVIDER_NONE, 'shot_id': None, 'path': None}
        # A deadline other than the client's own needs a client built with it.
        client = None if timeout is None else RunmanagerClient(timeout=timeout)
        reached, response = self.runmanager_rpc(
            RunmanagerClient.queue_exchange,
            'Runmanager unavailable while exchanging shots: %s',
            self._pending_outcome,
            request_shot,
            client=client,
        )
        if not reached:
            return no_shot, False
        self._pending_outcome = None
        return response, True

    def runmanager_alive(self, timeout=None):
        """Ask runmanager whether it is there, and say whether it answered.

        Gates the exchange: a runmanager that does not answer is not asked for a
        shot, so BLACS falls through to its local override instead of waiting
        out an exchange's longer allowance. Hence its own timeout."""
        alive, _ = self.runmanager_rpc(
            RunmanagerClient.say_hello,
            'Runmanager unavailable while checking status: %s',
            timeout=self.BLACS.exp_config.getfloat(
                'timeouts', 'liveness_timeout', fallback=LIVENESS_TIMEOUT
            )
            if timeout is None
            else timeout,
        )
        return alive

    def process_request(self,h5_filepath):
        try:
            new_conn = ConnectionTable(h5_filepath, logging_prefix='BLACS')
        except Exception:
            return None, "H5 file not accessible to Control PC\n"
        result,error = inmain(self.BLACS.connection_table.compare_to,new_conn)
        if result:
            with h5py.File(h5_filepath, 'r') as h5_file:
                if 'data' in h5_file['/']:
                    rerun = True
                else:
                    rerun = False
            if rerun:
                self._logger.debug('Run file has already been run! Creating a fresh copy to rerun')
                new_h5_filepath, repeat_number = self.new_rep_name(h5_filepath)
                success = self.clean_h5_file(h5_filepath, new_h5_filepath, repeat_number=repeat_number)
                if not success:
                   return None, 'Cannot create a re run of this experiment. Is it a valid run file?'
                h5_filepath = new_h5_filepath
            return h5_filepath, ''
        else:
            message =  ("Connection table of your file is not a subset of the experimental control apparatus.\n"
                       "You may have:\n"
                       "    Submitted your file to the wrong control PC\n"
                       "    Added new channels to your h5 file, without rewiring the experiment and updating the control PC\n"
                       "    Renamed a channel at the top of your script\n"
                       "    Submitted an old file, and the experiment has since been rewired\n"
                       "\n"
                       "Please verify your experiment script matches the current experiment configuration, and try again\n"
                       f"The error was {error}\n")
            return None, message
            
    def new_rep_name(self, h5_filepath):
        start = self._next_rep_index.get(h5_filepath, 1)
        next_path, index = next_available_indexed_filepath(
            h5_filepath,
            '_rep{index:05d}',
            start=start,
        )
        self._next_rep_index[h5_filepath] = index + 1
        return next_path, index
        
    def clean_h5_file(self, h5file, new_h5_file, repeat_number=0):
        """Copy a shot file without run data or ``run time``, keeping the other root
        attributes and setting ``run repeat``."""
        try:
            with h5py.File(h5file, 'r') as old_file:
                with h5py.File(new_h5_file, 'w') as new_file:
                    groups_to_copy = [
                        'devices',
                        'calibrations',
                        'script',
                        'globals',
                        'connection table',
                        'labscriptlib',
                        'waits',
                        'time_markers',
                        'shot_properties',
                    ]
                    for group in groups_to_copy:
                        if group in old_file:
                            new_file.copy(old_file[group], group)
                    for name in old_file.attrs:
                        if name != 'run time':
                            new_file.attrs[name] = old_file.attrs[name]
                    new_file.attrs['run repeat'] = repeat_number
        except Exception:
            self._logger.exception('Clean H5 File Error.')
            return False
            
        return True

    def reset_failed_shot_file(self, path):
        """Rewrite a failed shot's file without the run's data, keeping its repeat
        number. Errors are logged, not raised, so they cannot end the shot loop."""
        try:
            with h5py.File(path, 'r') as h5_file:
                repeat_number = h5_file.attrs.get('run repeat', 0)
            handle, temp_path = tempfile.mkstemp(prefix='BLACS-temp-', suffix='.h5')
            os.close(handle)
            if not self.clean_h5_file(path, temp_path, repeat_number=repeat_number):
                # Never move a copy that was not written over the shot.
                os.remove(temp_path)
                raise RuntimeError('No clean copy of the shot file could be written.')
            try:
                shutil.move(temp_path, path)
            except Exception:
                stem, ext = os.path.splitext(path)
                retry_path = stem + '_retry' + ext
                self._logger.warning(
                    "Couldn't delete failed run file %s, another process may be "
                    "using it. Using alternate filename %s for second attempt.",
                    path, retry_path, exc_info=True,
                )
                shutil.move(temp_path, retry_path)
        except Exception:
            self._logger.exception(
                'Could not reset the failed shot file %s. It keeps the data of '
                'the run that failed and cannot be run again as it stands.',
                path,
            )

    @inmain_decorator(wait_for_return=True)
    def set_status(self, status_text, shot_filepath=None):
        # Published as one immutable value, so a status query on the server
        # thread answers lock-free while the GUI thread runs a shot. The id is
        # taken here: _current_shot_id clears at the outcome but the path does not,
        # and a path with no id is reported to runmanager as a local override shot.
        status_text = str(status_text)
        self.published_status = PublishedStatus(
            text=status_text,
            shot_filepath=shot_filepath,
            shot_id=self._current_shot_id if shot_filepath else None,
        )
        self._ui.shot_status.setText(status_text)
        if shot_filepath is not None:
            self._ui.running_shot_name.setText(f'<b>{os.path.basename(shot_filepath)}</b>')
        else:
            self._ui.running_shot_name.setText('')

    def get_status_snapshot(self):
        """Report what this BLACS is doing, for a runmanager user to read.

        Read-only and read without the GUI thread, so a busy BLACS still
        answers. It changes nothing: the gate on hardware execution, the error
        that closed it, and Abort stay with the operator at this apparatus. The
        status text and shot come from one read of the value ``set_status``
        publishes, so they cannot describe two statuses. The shot's path is
        shared-drive agnostic, as an outcome's is."""
        status = self.published_status
        shot_path = status.shot_filepath
        return {
            'requesting_shots': bool(self._requesting_shots),
            'status': status.text,
            'shot_id': status.shot_id,
            'shot_path': path_to_agnostic(shot_path) if shot_path else None,
            'error': self.local_error,
        }

    @inmain_decorator(wait_for_return=True)
    def get_status(self):
        return self._ui.shot_status.text()
    
    @inmain_decorator(wait_for_return=True)    
    def transition_device_to_buffered(self, name, transition_list, h5file, restart_receiver):
        tab = self.BLACS.tablist[name]
        if self.get_device_error_state(name,self.BLACS.tablist):
            return False
        tab.connect_restart_receiver(restart_receiver)
        tab.transition_to_buffered(h5file, self.notify_queue)
        transition_list[name] = tab
        return True
    
    @inmain_decorator(wait_for_return=True)
    def get_device_error_state(self,name,device_list):
        return device_list[name].error_message

    def _abort_buffered_devices(self, devices_in_use, restart_function):
        self.notify_queue = queue.Queue()
        for devicename, tab in devices_in_use.items():
            if tab.mode == MODE_BUFFERED or tab.mode == MODE_TRANSITION_TO_BUFFERED:
                tab.abort_buffered(self.notify_queue)
            inmain(tab.disconnect_restart_receiver, restart_function)
       
     
    def manage(self):
        """Run the shot loop. An error it does not handle is raised visibly, rather
        than ending the thread silently."""
        try:
            self._manage()
        except Exception:
            self._logger.exception('Shot execution stopped on an unhandled error.')
            # Raise in a thread for visibility, as for a failed shot.
            zprocess.raise_exception_in_thread(sys.exc_info())
            self.stop_requesting_shots(
                'Shot execution stopped on an unhandled error; restart BLACS')
            self.set_status("Shot execution stopped\nSee the log; restart BLACS")
            # No loop is left to read an abort.
            inmain(self._ui.shot_abort_button.setEnabled, False)
        finally:
            # In a finally so an unhandled error cannot strand a held outcome.
            self._deliver_or_name_held_outcome()

    def _deliver_or_name_held_outcome(self):
        """Hand over an outcome still held as the loop ends, or log the run lost."""
        if self._pending_outcome is None:
            return
        try:
            self.exchange_with_runmanager(False, timeout=self.OUTCOME_FLUSH_TIMEOUT)
        except Exception:
            self._logger.exception('Could not hand over a held shot outcome.')
        # If undelivered, the row stays marked running in runmanager's queue and
        # is offered again; only this run is lost, so name it.
        self.name_held_outcome()

    def _manage(self):
        logger = logging.getLogger('BLACS.shot_executor.thread')
        process_tree.zlock_client.set_thread_name('shot_executor')
        logger.info('starting')
        
        # HDF5 prints errors that are not errors; h5py silences them in the main
        # thread on import, so silence them in this thread too.
        h5py._errors.silence_errors()

        timeout_limit = 300 #seconds
        self.set_status("Idle")
        
        while self.manager_running:
            path = None
            # Unchecking Request shots stops the next request, not the shot in hand,
            # nor the report of an outcome still held, so runmanager always learns
            # how the shot it offered turned out.
            if not self.requesting_shots and self._pending_outcome is None:
                self._runmanager_link.show_state(NOT_REQUESTING)
                # Leave a standing local error on the status line.
                if not self.local_error:
                    self.set_status('Idle')
                time.sleep(1)
                continue

            shot_id = None
            agnostic_path = None
            runmanager_paused = False
            runmanager_pending = False
            runmanager_answered = self.runmanager_alive()
            # Read after the probe, which can take a while: unticking Request
            # shots during it stops the request that follows.
            request_shot = self.requesting_shots
            if runmanager_answered:
                # The only request for a shot, made between shots with the last
                # outcome in hand. Runmanager's reclaim relies on it: asking from
                # anywhere else, or during a shot, would hand one shot out twice.
                response, _ = self.exchange_with_runmanager(request_shot)
                shot_id = response['shot_id']
                agnostic_path = response['path']
                # A paused runmanager falls through to the local override shot, as an
                # empty queue does; it is told apart so the light can say why no
                # queued work is arriving.
                runmanager_paused = response['state'] == PROVIDER_PAUSED
                # Pending is the shot runmanager will offer next, still
                # compiling: wait for it rather than run ours in the gap.
                runmanager_pending = response['state'] == PROVIDER_PENDING
            self._current_shot_id = shot_id

            if not agnostic_path and self.requesting_shots and not runmanager_pending:
                local_override_path = str(
                    inmain(self._ui.local_override_lineEdit.text)
                ).strip()
                if local_override_path:
                    agnostic_path = path_to_agnostic(
                        os.path.abspath(local_override_path)
                    )

            if not agnostic_path:
                # Leave a standing local error on the status line: this pass,
                # which delivers that shot's outcome, would otherwise wipe it
                # before an operator could read it.
                if not self.local_error:
                    self.set_status('Idle')
                if not request_shot:
                    self._runmanager_link.show_state(NOT_REQUESTING)
                elif runmanager_paused:
                    self._runmanager_link.show_state('Runmanager queue paused')
                else:
                    self._runmanager_link.show_state(REQUESTING)
                time.sleep(1)
                continue

            path, message = self.process_request(path_to_local(str(agnostic_path)))
            if path is None:
                logger.error(message.strip())
                if shot_id is not None:
                    # Reported as rejected on the next exchange. Requests stay on:
                    # the shot is unusable, not the apparatus, and runmanager stops
                    # offering the row, so we run our own shot meanwhile.
                    self.report_shot_outcome(None, BLACS_REJECTED, message.strip())
                    self.set_status("Rejected shot from runmanager")
                else:
                    # A local override shot has no runmanager row to hold it, so stop
                    # rather than retry an unreadable file every second.
                    self.set_status("Rejected local override shot\nRequests stopped")
                    self.stop_requesting_shots(message.strip())
                time.sleep(1)
                continue

            self.set_status('Preparing shot...', path)
            logger.info('Got a file: %s'%path)
            
            devices_in_use = {}
            transition_list = {}   
            self.notify_queue = queue.Queue()

            def abort_function():
                try:
                    # "Shot Executor" is never a device name: it has a space, so is not
                    # a valid Python name.
                    self.notify_queue.put(['Shot Executor', 'abort'])
                except Exception:
                    logger.exception('Could not send abort message to the shot executor')
        
            def restart_function(device_name):
                try:
                    self.notify_queue.put([device_name, 'restart'])
                except Exception:
                    logger.exception('Could not send restart message to the shot executor for device %s'%device_name)
        
            # Transition to buffered.
            try:  
                
                timed_out = False
                error_condition = False
                abort = False
                restarted = False
                error_devices = []
                restarted_devices = []
                self.set_status("Transitioning to buffered...", path)
                
                inmain(self._ui.shot_abort_button.clicked.connect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,True)
                                
                plugins.manager.fire_event('pre_transition_to_buffered', path)

                start_time = time.time()
                
                with h5py.File(path, 'r') as hdf5_file:
                    devices_in_use = {}
                    start_order = {}
                    stop_order = {}
                    for name in  hdf5_file['devices']:
                        device_properties = labscript_utils.properties.get(
                            hdf5_file, name, 'device_properties'
                        )
                        devices_in_use[name] = self.BLACS.tablist[name]
                        start_order[name] = device_properties.get('start_order', None)
                        stop_order[name] = device_properties.get('stop_order', None)

                start_groups = defaultdict(set)
                stop_groups = defaultdict(set)
                for name in devices_in_use:
                    start_groups[start_order[name]].add(name)
                    stop_groups[stop_order[name]].add(name)

                while (transition_list or start_groups) and not error_condition:
                    if not transition_list:
                        # Ready to transition the next group:
                        for name in start_groups.pop(min(start_groups)):
                            try:
                                success = self.transition_device_to_buffered(name,transition_list,path,restart_function)
                                if not success:
                                    logger.error('%s has an error condition, aborting run' % name)
                                    error_condition = True
                                    error_devices.append(name)
                                    break
                            except Exception:
                                logger.exception('Exception while transitioning %s to buffered mode.'%(name))
                                error_condition = True
                                error_devices.append(name)
                                break
                        if error_condition:
                            break
                        
                    try:
                        logger.debug('Waiting for the following devices to finish transitioning to buffered mode: %s'%str(transition_list))
                        device_name, result = self.notify_queue.get(timeout=2)
                        
                        if device_name == 'Shot Executor' and result == 'abort':
                            logger.info('abort signal received from GUI')
                            abort = True
                            break
                            
                        if result == 'fail':
                            logger.info('abort signal received during transition to buffered of %s' % device_name)
                            error_condition = True
                            error_devices.append(device_name)
                            break
                        elif result == 'restart':
                            logger.info('Device %s was restarted, aborting shot.'%device_name)
                            restarted = True
                            restarted_devices.append(device_name)
                            break
                            
                        logger.debug('%s finished transitioning to buffered mode' % device_name)
                        
                        # The tab says it's done, but does it have an error condition?
                        if self.get_device_error_state(device_name,transition_list):
                            logger.error('%s has an error condition, aborting run' % device_name)
                            error_condition = True
                            error_devices.append(device_name)
                            break

                        del transition_list[device_name]
                    except queue.Empty:
                        # It's been 2 seconds without a device finishing
                        # transitioning to buffered. Is there an error?
                        for name in transition_list:
                            if self.get_device_error_state(name,transition_list):
                                logger.error('%s has an error condition, aborting run' % name)
                                error_condition = True
                                error_devices.append(name)
                                
                        if error_condition:
                            break
                            
                        if time.time() - start_time > timeout_limit:
                            logger.error('Transitioning to buffered mode timed out')
                            timed_out = True
                            break

                if timed_out or error_condition or abort or restarted:
                    # Any shot that did not complete stops requests, an abort included, so
                    # runmanager decides its fate and an operator sees why before
                    # another is asked for.
                    outcome_path = path
                    if timed_out:
                        outcome_status, reason = BLACS_FAILED, device_status(
                            transition_list, 'timed out programming')
                    elif abort:
                        outcome_status, reason = BLACS_ABORTED, 'Aborted'
                    elif restarted:
                        outcome_status, reason = BLACS_FAILED, device_status(
                            restarted_devices, 'restarted while transitioning to buffered')
                    else:
                        outcome_status, reason = BLACS_FAILED, device_status(
                            error_devices, 'in error state')
                    self.set_status(f"{reason}\nRequests stopped")
                    self.stop_requesting_shots(reason)
                    self.report_shot_outcome(outcome_path, outcome_status, reason)

                    # Recreate the queue: we do not want to hear from devices
                    # that are still transitioning to buffered mode.
                    self.notify_queue = queue.Queue()
                    for tab in devices_in_use.values():                        
                        # abort_buffered acts only in mode BUFFERED, so it suits both a
                        # buffered tab and one that already returned to manual mode.
                        # Its result needs no check: it succeeds or raises in the tab.
                        tab.abort_buffered(self.notify_queue)
                        inmain(tab.disconnect_restart_receiver,restart_function)
                        
                    inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                    inmain(self._ui.shot_abort_button.setEnabled,False)
                    
                    continue

                # Front panel data is read now but saved to the h5 file after the run.
                states,tab_positions,window_data,plugin_data = self.BLACS.front_panel_settings.get_save_data()
                self.set_status(f"Running (program time: {time.time() - start_time:.3f}s)...", path)
                    
                experiment_finished_notifications = queue.Queue()
                logger.debug('About to start the master pseudoclock')
                run_time = datetime.datetime.now()

                plugins.manager.fire_event('science_starting', path)

                self.BLACS.tablist[self.master_pseudoclock].start_run(
                    experiment_finished_notifications
                )
                
                                                
                abort = False
                restarted = False
                done = False
                error_devices = []
                restarted_devices = []
                while not (abort or restarted or done):
                    try:
                        done = (
                            experiment_finished_notifications.get(timeout=0.5) == 'done'
                        )
                    except queue.Empty:
                        pass
                    try:
                        # Poll notify_queue for abort signals from the button or
                        # device restarts.
                        device_name, result = self.notify_queue.get_nowait()
                        if (device_name == 'Shot Executor' and result == 'abort'):
                            abort = True
                        if result == 'restart':
                            restarted = True
                            restarted_devices.append(device_name)
                        for device_name, tab in devices_in_use.items():
                            if self.get_device_error_state(device_name,devices_in_use):
                                logger.error('%s has an error condition, aborting run' % device_name)
                                restarted = True
                                error_devices.append(device_name)
                    except queue.Empty:
                        pass
                        
                if abort or restarted:
                    for devicename, tab in devices_in_use.items():
                        if tab.mode == MODE_BUFFERED:
                            tab.abort_buffered(self.notify_queue)
                        inmain(tab.disconnect_restart_receiver,restart_function)
                                            
                inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,False)
                
                if restarted:
                    if error_devices:
                        reason = device_status(error_devices, 'in error state during the run')
                    else:
                        reason = device_status(restarted_devices, 'restarted during the run')
                    self.stop_requesting_shots(reason)
                    self.set_status(f"{reason}\nRequests stopped")
                    self.report_shot_outcome(path, BLACS_FAILED, reason)
                elif abort:
                    # Stop as for any other shot that did not complete, so
                    # runmanager decides whether it goes back in the queue:
                    self.stop_requesting_shots('Aborted')
                    self.set_status("Aborted\nRequests stopped")
                    self.report_shot_outcome(path, BLACS_ABORTED, 'Aborted')
                    
                if abort or restarted:
                    continue                
                
                logger.info('Run complete')
                self.set_status("Saving data...", path)
            except Exception:
                logger.exception("Error in shot execution. Requests stopped.")

                zprocess.raise_exception_in_thread(sys.exc_info())
                self.stop_requesting_shots('Error in shot execution')
                self.reset_failed_shot_file(path)
                
                # Need to put devices back in manual mode
                self._abort_buffered_devices(devices_in_use, restart_function)
                self.set_status("Error in shot execution\nRequests stopped")
                self.report_shot_outcome(
                    path, BLACS_FAILED, 'Error in shot execution')

                inmain(self._ui.shot_abort_button.clicked.disconnect,abort_function)
                inmain(self._ui.shot_abort_button.setEnabled,False)
                
                continue
                             
            finally:
                plugins.manager.fire_event('science_over', path)

            # Transition to manual.
            try:
                with h5py.File(path,'r+') as hdf5_file:
                    self.BLACS.front_panel_settings.store_front_panel_in_h5(
                        hdf5_file,
                        states,
                        tab_positions,
                        window_data,
                        plugin_data,
                        save_conn_table=False,
                        shot_file=True,
                    )

                    data_group = hdf5_file['/'].require_group('data')
                    hdf5_file.attrs['run time'] = run_time.strftime('%Y%m%dT%H%M%S.%f')
        
                error_condition = False
                error_devices = []
                response_list = {}
                # If one device fails we still transition the rest to manual mode.
                while stop_groups:
                    transition_list = {}
                    for name in stop_groups.pop(min(stop_groups)):
                        if name in response_list:
                            # Restarted while an earlier group was waited on.
                            continue
                        tab = devices_in_use[name]
                        try:
                            tab.transition_to_manual(self.notify_queue)
                            transition_list[name] = tab
                        except Exception:
                            logger.exception('Exception while transitioning %s to manual mode.'%(name))
                            error_condition = True
                            error_devices.append(name)
                    logged_waiting = None
                    while transition_list:
                        # Logged when the list changes, not at each 2 s check.
                        if set(transition_list) != logged_waiting:
                            logged_waiting = set(transition_list)
                            logger.info('Waiting for the following devices to finish transitioning to manual mode: %s'%str(transition_list))
                        try:
                            name, result = self.notify_queue.get(timeout=2)
                            if name == 'Shot Executor' and result == 'abort':
                                # Ignore abort signals left in the queue:
                                # it is too late to abort.
                                continue
                        except queue.Empty:
                            # 2 seconds without a device transitioning to manual mode.
                            # Is there an error:
                            for name in transition_list.copy():
                                if self.get_device_error_state(name, transition_list):
                                    error_condition = True
                                    error_devices.append(name)
                                    logger.debug('%s is in an error state' % name)
                                    response_list[name] = 'error'
                                    inmain(transition_list[name].disconnect_restart_receiver, restart_function)
                                    del transition_list[name]
                            continue
                        if name in response_list:
                            # A device given up on above may still answer, too late.
                            continue
                        response_list[name] = result
                        if result == 'fail':
                            error_condition = True
                            error_devices.append(name)
                            logger.debug('%s failed to transition to manual' % name)
                        elif result == 'restart':
                            error_condition = True
                            error_devices.append(name)
                            logger.debug('%s restarted during transition to manual' % name)
                        elif self.get_device_error_state(name, devices_in_use):
                            error_condition = True
                            error_devices.append(name)
                            logger.debug('%s is in an error state' % name)
                        else:
                            logger.debug('%s finished transitioning to manual mode' % name)
                        tab = devices_in_use[name]
                        inmain(tab.disconnect_restart_receiver, restart_function)
                        # Absent for a device of a later group that restarted.
                        transition_list.pop(name, None)
                    
                if error_condition:
                    reason = device_status(error_devices, 'reported an error after the run')
                    self.set_status(f"{reason}\nRequests stopped")

            except Exception:
                error_condition = True
                # Saving the data or putting the devices back failed, which is not a
                # device-reported error: this is what the operator is told and what
                # runmanager shows on the row.
                reason = 'Error saving data after the run'
                logger.exception("Error in shot execution. Requests stopped.")
                self.set_status("Error in shot execution\nRequests stopped")
                self._abort_buffered_devices(devices_in_use, restart_function)

                zprocess.raise_exception_in_thread(sys.exc_info())

            if error_condition:
                self.stop_requesting_shots(reason)
                self.reset_failed_shot_file(path)

                self.report_shot_outcome(path, BLACS_FAILED, reason)
                continue
            
            logger.info('All devices are back in static mode.')  

            self.report_shot_outcome(path, BLACS_COMPLETED)

            plugins.manager.fire_event('shot_complete', path)

            self.set_status("Idle")
        logger.info('Stopping')
