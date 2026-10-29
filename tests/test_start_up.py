#####################################################################
#                                                                   #
# /tests/test_start_up.py                                           #
#                                                                   #
# Copyright 2026, JQI                                               #
# Author: Ian Spielman                                              #
#                                                                   #
# This file is part of blacs, in the labscript suite                #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labscript_utils.labconfig import LabConfig

from fixtures import blacs_main


class SecondBLACSTests(unittest.TestCase):
    def test_a_taken_port_stops_blacs_before_any_tab(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as held:
            # A listener in place of the first BLACS's server.
            held.bind(('127.0.0.1', 0))
            held.listen()
            port = held.getsockname()[1]
            config_path = Path(directory) / 'labconfig.toml'
            config_path.write_text(f'[ports]\nblacs = {port}\n')
            exp_config = LabConfig(config_path=config_path)

            # The message box is stood in for, as it would wait for a click.
            with mock.patch.object(blacs_main, 'QMessageBox') as message_box:
                with self.assertRaises(SystemExit):
                    blacs_main.exit_if_port_in_use(exp_config)
                held.close()
                # With the port free again, BLACS goes on.
                blacs_main.exit_if_port_in_use(exp_config)
            message_box.critical.assert_called_once()


if __name__ == '__main__':
    unittest.main()
