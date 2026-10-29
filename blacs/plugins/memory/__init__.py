#####################################################################
#                                                                   #
# /blacs/plugins/memory/__init__.py                                 #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import os
import logging
import gc
from blacs import BLACS_DIR
from blacs.plugins import BasePlugin
from labscript_utils import memprof

FILEPATH_COLUMN = 0
name = "Memory Profile"
module = "memory" # should be folder name
logger = logging.getLogger(f'BLACS.plugin.{module}')
MEMPROF_FILEPATH = os.path.join(BLACS_DIR, 'memprof.txt')

class Plugin(BasePlugin):
    def plugin_setup_complete(self, BLACS):
        memprof.start(filepath=MEMPROF_FILEPATH)

    def get_menu_contributions(self):
        return [
            {'location': 'menubar', 'path': name, 'order': order, 'name': text,
             'action': action, 'icon': icon}
            # A menu action is passed Qt's checked flag, which gc.collect and
            # memprof.start would take as their argument.
            for order, (text, action, icon) in enumerate([
                ('Garbage collect', lambda: gc.collect(), ':/qtutils/fugue/memory'),
                ('Reset profiler', lambda: memprof.start(filepath=MEMPROF_FILEPATH), ':/qtutils/fugue/counter-reset'),
                ('Diff memory usage', memprof.check, ':/qtutils/fugue/tables'),
            ])
        ]
