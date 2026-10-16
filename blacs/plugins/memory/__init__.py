#####################################################################
#                                                                   #
# /plugins/memory/__init__.py                                       #
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
logger = logging.getLogger('BLACS.plugin.%s'%module)

class Plugin(BasePlugin):
    def plugin_setup_complete(self, BLACS):
        memprof.start(filepath=os.path.join(BLACS_DIR, 'memprof.txt'))

    def get_menu_contributions(self):
        return [
            {'location': 'menubar', 'path': name, 'order': order, 'name': text,
             'action': action, 'icon': icon}
            for order, (text, action, icon) in enumerate([
                ('Garbage collect', gc.collect, ':/qtutils/fugue/memory'),
                ('Reset profiler', memprof.start, ':/qtutils/fugue/counter-reset'),
                ('Diff memory usage', memprof.check, ':/qtutils/fugue/tables'),
            ])
        ]
