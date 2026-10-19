#####################################################################
#                                                                   #
# /blacs/plugins/cycle_time/__init__.py                             #
#                                                                   #
# Copyright 2017, Monash University and contributors                #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
from queue import Queue, Empty
from time import monotonic
import logging

from qtutils import inmain

import labscript_utils.h5_lock
import h5py

from blacs.plugins import BasePlugin, callback
import labscript_utils.properties as properties

name = "cycle_time"
module = "cycle_time" # should be folder name
logger = logging.getLogger(f'BLACS.plugin.{module}')


class Plugin(BasePlugin):
    def __init__(self):
        self.BLACS = None
        self.time_of_last_shot = None
        self.abort_queue = Queue()
        self.target_cycle_time = None
        self.delay_after_programming = None
        self.next_target_cycle_time = None
        self.next_delay_after_programming = None

    def plugin_setup_complete(self, BLACS):
        self.BLACS = BLACS
        self.shot_executor = self.BLACS['shot_execution']

    def get_event_handlers(self):
        return {
            'pre_transition_to_buffered': self.pre_transition_to_buffered,
            'science_starting': self.science_starting,
        }
        
    def _abort(self):
        self.abort_queue.put('abort')

    @callback(priority=100) # this callback should run after all other callbacks.
    def pre_transition_to_buffered(self, h5_filepath):
        # Delay according to the settings of the previously run shot, and save the
        # settings for the upcoming shot:
        self.target_cycle_time = self.next_target_cycle_time
        self.delay_after_programming = self.next_delay_after_programming

        with h5py.File(h5_filepath, 'r') as f:
            try:
                group = f['shot_properties']
            except KeyError:
                return
            self.next_target_cycle_time = properties.get_attribute(group, 'target_cycle_time')
            self.next_delay_after_programming = properties.get_attribute(
                group, 'cycle_time_delay_after_programming'
            )

        if not self.delay_after_programming:
            self.do_delay(h5_filepath)

    # Runs before the progress bar plugin, but after all other callbacks.
    @callback(priority=100) 
    def science_starting(self, h5_filepath):
        if self.delay_after_programming:
            self.do_delay(h5_filepath)

    def do_delay(self, h5_filepath):
        if self.target_cycle_time is not None and self.time_of_last_shot is not None:
            # Wait until target_cycle_time has passed since the last shot started.
            deadline = self.time_of_last_shot + self.target_cycle_time
            inmain(self.BLACS['ui'].shot_abort_button.clicked.connect, self._abort)
            # Store the current shot status, to restore it after we are done:
            previous_status = self.shot_executor.get_status()
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self.shot_executor.set_status(
                    f'Waiting {remaining:.1f}s for target cycle time',
                    h5_filepath,
                )
                try:
                    self.abort_queue.get(timeout=remaining % 0.1)
                    break # Got an abort
                except Empty:
                    continue
            inmain(self.BLACS['ui'].shot_abort_button.clicked.disconnect, self._abort)
            self.shot_executor.set_status(previous_status, h5_filepath)

        self.time_of_last_shot = monotonic()
