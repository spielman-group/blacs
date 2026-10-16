#####################################################################
#                                                                   #
# /blacs/__init__.py                                                #
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

from labscript_utils.versions import package_version

__version__ = package_version(__package__)

DESKTOP_APP_MODULE = 'blacs'
APPLICATION_NAME = 'BLACS'
BLACS_DIR = os.path.dirname(os.path.realpath(__file__))
