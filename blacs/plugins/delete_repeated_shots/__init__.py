#####################################################################
#                                                                   #
# /blacs/plugins/delete_repeated_shots/__init__.py                  #
#                                                                   #
# Copyright 2017, JQI                                               #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import logging
import os
import threading
from queue import Queue

from qtutils import UiLoader

from labscript_utils.shared_drive import path_to_agnostic
from labscript_utils.ls_zprocess import Lock
from blacs.plugins import PLUGINS_DIR, BasePlugin

name = "Delete repeated shots"
module = "delete_repeated_shots" # should be folder name
logger = logging.getLogger(f'BLACS.plugin.{module}')


KEEP_ALL_SHOTS = 0

class Plugin(BasePlugin):
    def __init__(self):
        self.BLACS = None
        self.ui = None
        self.n_shots_to_keep = KEEP_ALL_SHOTS
        self.delete_queue = []
        self.event_queue = Queue()
        self.delete_queue_lock = threading.Lock()
        self.mainloop_thread = threading.Thread(target=self.mainloop)
        self.mainloop_thread.daemon = True
        self.mainloop_thread.start()
        
    def plugin_setup_complete(self, BLACS):
        self.BLACS = BLACS

        self.ui = UiLoader().load(os.path.join(PLUGINS_DIR, module, 'controls.ui'))
        BLACS['ui'].shot_controls_frame.layout().addWidget(self.ui)

        self.ui.spinBox.setValue(self.n_shots_to_keep)

        self.ui.spinBox.valueChanged.connect(self.on_spinbox_value_changed)
        self.ui.reset_button.clicked.connect(self.on_reset_button_clicked)
        self.ui.setEnabled(True)

    def on_spinbox_value_changed(self, value):
        with self.delete_queue_lock:
            self.n_shots_to_keep = value
            # Lowering the limit drops the oldest queue entries without deleting
            # their files, so shots kept under the old limit stay on disk; only
            # the newest n_shots_to_keep will be deleted, over the next shots.
            while len(self.delete_queue) > self.n_shots_to_keep:
                self.delete_queue.pop(0)

    def on_reset_button_clicked(self):
        self.ui.spinBox.setValue(KEEP_ALL_SHOTS)

    def get_save_data(self):
        return {'n_shots_to_keep': self.n_shots_to_keep,
                'delete_queue': self.delete_queue}

    def restore_save_data(self, data):
        self.n_shots_to_keep = data.get('n_shots_to_keep', KEEP_ALL_SHOTS)
        self.delete_queue = data.get('delete_queue', [])
    
    def get_event_handlers(self):
        return {'shot_complete': self.on_shot_complete}
        
    def on_shot_complete(self, h5_filepath):

        if self.n_shots_to_keep == KEEP_ALL_SHOTS:
            return

        # A repeated shot is named <shot>_rep<number>.h5.
        basename, ext = os.path.splitext(os.path.basename(h5_filepath))
        if '_rep' in basename and ext == '.h5':
            repno = basename.split('_rep')[-1]
            try:
                int(repno)
            except ValueError:
                return
            else:
                self.delete_queue.append(h5_filepath)
                self.event_queue.put('shot complete')

    def mainloop(self):
        # Shots are deleted in a separate thread so the shot loop does not wait on
        # network communication to acquire the lock.
        while True:
            try:
                event = self.event_queue.get()
                if event == 'close':
                    break
                elif event == 'shot complete':
                    while len(self.delete_queue) > self.n_shots_to_keep:
                        with self.delete_queue_lock:
                            h5_filepath = self.delete_queue.pop(0)
                        # Acquire a lock on the file so that we don't
                        # delete it whilst someone else has it open:
                        with Lock(path_to_agnostic(h5_filepath)):
                            try:
                                os.unlink(h5_filepath)
                                logger.info("Deleted repeated shot file %s" % h5_filepath)
                            except OSError:
                                logger.exception("Couldn't delete shot file %s" % h5_filepath)
                else:
                    raise ValueError(event)
            except Exception:
                logger.exception("Exception in repeated shot deletion loop, ignoring.")
    

    def close(self):
        self.event_queue.put('close')
        self.mainloop_thread.join()

