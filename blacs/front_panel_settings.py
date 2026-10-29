#####################################################################
#                                                                   #
# /blacs/front_panel_settings.py                                    #
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

import numpy
import labscript_utils.h5_lock, h5py
from qtutils import inmain_decorator

from labscript_utils.connections import ConnectionTable
from labscript_utils import ensure_str
import blacs.plugins as plugins

logger = logging.getLogger('BLACS.FrontPanelSettings')

# A tab's saved data is the repr of a dict, whose numpy values name only what
# numpy's namespace holds.
_SAVE_DATA_NAMESPACE = dict(vars(numpy), np=numpy, numpy=numpy)

def _bytes_dtype(values):
    """Return a bytes dtype as wide as the longest of the bytestrings in values."""
    return f'S{max([1] + [len(v) for v in values])}'

class FrontPanelSettings:
    def __init__(self,settings_path,connection_table):
        self.settings_path = settings_path
        self.connection_table = connection_table
        with h5py.File(settings_path,'a') as h5file:
            pass

    def setup(self,blacs):
        self.tablist = blacs.tablist
        self.attached_devices = blacs.attached_devices
        self.notebook = blacs.tab_widgets
        self.blacs = blacs

    def restore(self):
        # Sorts each saved value into settings (restored), question (an unnamed
        # channel a device now occupies) or error (device missing or changed); see
        # check_row.
        settings = {}
        question = {}
        error = {}
        tab_data = {'BLACS settings':{}}

        with h5py.File(self.settings_path, 'r') as h5file:
            if 'connection table' not in h5file:
                logger.info('Front panel settings connection table not found!')

                return settings, question, error, tab_data
                
        try:
            saved_ct = ConnectionTable(self.settings_path, logging_prefix='BLACS', exceptions_in_thread=True)
            ct_match, _ = self.connection_table.compare_to(saved_ct)

            with h5py.File(self.settings_path,'r') as hdf5_file:
                dataset = hdf5_file['/front_panel'].get('_notebook_data',[])

                # Shot files do not contain tab positions.
                has_positions = 'notebook' in dataset.dtype.names
                for row in dataset:
                    tab_name = ensure_str(row['tab_name'])
                    try:
                        data = eval(ensure_str(row['data']), _SAVE_DATA_NAMESPACE)
                    except Exception:
                        logger.warning('Could not read the saved data of %s, which starts without it', tab_name)
                        data = {}
                    tab_data[tab_name] = {'data':data}
                    if has_positions:
                        tab_data[tab_name].update(notebook=row['notebook'], page=row['page'], visible=row['visible'])

                tab_data['BLACS settings'] = dict(dataset.attrs)

                if 'front_panel' in hdf5_file["/front_panel"]:
                    dataset = hdf5_file["/front_panel"].get('front_panel', [])
                    for row in dataset:
                        result = self.check_row(row,ct_match,self.connection_table,saved_ct)
                        columns = ['name', 'device_name', 'channel', 'base_value', 'locked', 'base_step_size', 'current_units']
                        data_dict = {}
                        for i in range(len(row)):
                            if isinstance(row[i], bytes) or isinstance(row[i], str):
                                data_dict[columns[i]] = ensure_str(row[i])
                            else:
                                data_dict[columns[i]] = row[i]
                        settings,question,error = self.handle_return_code(data_dict,result,settings,question,error)
        except Exception as e:
            logger.info("Could not load saved settings")
            logger.info('%s', e)
        not_restored = [
            f'{name} ({reason})' for name, (row, reason) in error.items()
        ] + [
            f'{device} {port} (now connected)'
            for device, ports in question.items()
            for port in ports
        ]
        if not_restored:
            logger.warning(
                'Front panel values in %s not restored: %s',
                self.settings_path,
                ', '.join(not_restored),
            )
        return settings,question,error,tab_data

    def handle_return_code(self,row,result,settings,question,error):
        # The codes check_row returns: 1 restore to the device, 2 restore to the
        # channel the named device has moved to, 3 ask whether to restore an unnamed
        # device's values (also follows 2), -1 device missing, -2 device changed.
        if isinstance(result, tuple):
            connection = result[1]
            result = result[0]

        if result == 1:
            settings.setdefault(row['device_name'],{})
            settings[row['device_name']][row['channel']] = row
        elif result == 2:
            settings.setdefault(connection.parent.name,{})
            settings[connection.parent.name][connection.parent_port] = row
        elif result == 3:
            question.setdefault(connection.parent.name,{})
            question[connection.parent.name][connection.parent_port] = row
        elif result == -1:
            error[row['device_name']+'_'+row['channel']] = row,"missing"
        elif result == -2:
            error[row['device_name']+'_'+row['channel']] = row,"changed"

        return settings,question,error

    def check_row(self,row,ct_match,blacs_ct,saved_ct):
        if row[0] != "-":
            if ct_match:
                return 1
            else:
                connection = blacs_ct.find_by_name(row[0])
                connection2 = saved_ct.find_by_name(row[0])

                if connection:
                    # A difference in only the parent and port sends the value to the new
                    # parent; any other difference means the saved data does not fit.
                    result,error = connection.compare_to(connection2)

                    allowed_length = 0
                    if "parent_port" in error:
                        allowed_length += 1

                    if len(error) > allowed_length:
                        return -2
                    elif error == {} and connection.parent.name == connection2.parent.name:
                        return 1
                    else:
                        return 2,connection
                else:
                    return -1
        else:
            # An unnamed channel is only asked about if a device now occupies it.
            connection = blacs_ct.find_child(row[1],row[2])
            if connection:
                return 3,connection
            else:
                return 1

    @inmain_decorator(wait_for_return=True)
    def get_save_data(self):
        tab_data = {}
        notebook_data = {}

        for device_name,tab in self.tablist.items():
            tab_data[device_name] = {'front_panel_settings':tab.settings['front_panel_settings'], 'saved_data': tab.get_all_save_data()}

            # A tab found in no known notebook is saved at notebook 0, page 0, so it
            # reappears there on restart.
            current_notebook_name = 0
            page = 0
            visible = False

            for notebook_name,notebook in self.notebook.items():
                if notebook.indexOf(tab._ui) != -1:
                    current_notebook_name = notebook_name
                    page = notebook.indexOf(tab._ui)
                    visible = True if notebook.currentIndex() == page else False
                    break

            notebook_data[device_name] = {"notebook":current_notebook_name,"page":page, "visible":visible}

        plugin_data = plugins.manager.get_save_data()

        window_data = {
            'geometry': self.blacs.geometry_save_data(),
            '_shot_execution': self.blacs.shot_executor.get_save_data(),
        }

        return tab_data,notebook_data,window_data,plugin_data

    @inmain_decorator(wait_for_return=True)
    def get_shot_data(self):
        """Return the front panel values and the device settings of each tab, for
        storing in a shot file with store_shot_front_panel_in_h5()."""
        return {
            device_name: {
                'front_panel_settings': tab.settings['front_panel_settings'],
                'saved_data': tab.get_device_save_data(),
            }
            for device_name, tab in self.tablist.items()
        }

    @inmain_decorator(wait_for_return=True)
    def save_front_panel_to_h5(self,current_file,states,tab_positions,window_data,plugin_data,silent = {}, force_new_conn_table = False):
        # An existing file is written only if it has no connection table, a matching
        # one, or force_new_conn_table is set; an existing front panel is replaced
        # only after the overwrite question (or silent['overwrite']).
        if os.path.isfile(current_file):
            save_conn_table = True if force_new_conn_table else False
            result = False
            if not save_conn_table:
                try:
                    new_conn = ConnectionTable(current_file)
                    result,error = self.connection_table.compare_to(new_conn)
                except Exception:
                    # The file has no connection table, so save ours too.
                    save_conn_table = True

            # A connection table that is being saved is not compared first.
            if save_conn_table or result:
                with h5py.File(current_file,'r+') as hdf5_file:
                    if hdf5_file['/'].get('front_panel') is not None:
                        if not silent:
                            overwrite = self.blacs.question_dialog(
                                f"This file '{current_file}' already contains a front panel configuration.\n\n"
                                "Do you wish to replace the existing front panel configuration "
                                "in this file?",
                                default_no=True,
                            )
                        else:
                            overwrite = silent["overwrite"]

                        if overwrite:
                            del hdf5_file['/front_panel']
                            self.store_front_panel_in_h5(hdf5_file,states,tab_positions,window_data,plugin_data,save_conn_table)
                        else:
                            if not silent:
                                self.blacs.error_dialog("Front Panel not saved.", information=True)
                            else:
                                logger.info("Front Panel not saved as it already existed in the h5 file '%s'", current_file)
                            return
                    else:
                        self.store_front_panel_in_h5(hdf5_file,states,tab_positions,window_data,plugin_data,save_conn_table)
            else:
                if not silent:
                    self.blacs.error_dialog(
                        "The Front Panel was not saved as the file selected contains a connection table which is not a subset of the BLACS connection table.",
                        information=True,
                    )
                else:
                    logger.info("Front Panel not saved as the connection table in the h5 file '%s' didn't match the current connection table.", current_file)
                return
        else:
            with h5py.File(current_file,'w') as hdf5_file:
                self.store_front_panel_in_h5(hdf5_file,states,tab_positions,window_data,plugin_data,save_conn_table=True)

    @inmain_decorator(wait_for_return=True)
    def store_front_panel_in_h5(self, hdf5_file,tab_data,notebook_data,window_data,plugin_data,save_conn_table=False):
        if save_conn_table:
            if 'connection table' in hdf5_file:
                del hdf5_file['connection table']
            hdf5_file.create_dataset('connection table', data=self.connection_table.raw_table)

        data_group = hdf5_file['/'].create_group('front_panel')
        self._store_front_panel_values(data_group, tab_data)

        # A device whose tab failed to load has no place, and is saved at notebook 0,
        # page 0.
        tab_names = [device_name.encode() for device_name in tab_data]
        other_data = [repr(device_state["saved_data"]).encode() for device_state in tab_data.values()]
        places = [notebook_data.get(device_name, {"notebook": 0, "page": 0, "visible": False}) for device_name in tab_data]
        notebook_array = numpy.array(
            [
                (name, place["notebook"], place["page"], place["visible"], od)
                for name, place, od in zip(tab_names, places, other_data)
            ],
            dtype=[('tab_name',_bytes_dtype(tab_names)),('notebook','S2'),('page',int),('visible',bool),('data',_bytes_dtype(other_data))],
        )

        dataset = data_group.create_dataset("_notebook_data",data=notebook_array)
        dataset.attrs['plugin_data'] = repr(plugin_data)
        dataset.attrs['shot_execution_data'] = repr(window_data['_shot_execution'])
        # Only BLACS's own settings file records the window geometry.
        if hdf5_file.filename == self.settings_path:
            dataset.attrs['window_geometry'] = repr(window_data['geometry'])

    # In the main thread, which changes the front panel values that get_shot_data()
    # returns.
    @inmain_decorator(wait_for_return=True)
    def store_shot_front_panel_in_h5(self, hdf5_file, shot_data):
        """Store the front panel values and device settings returned by
        get_shot_data() in a shot file."""
        data_group = hdf5_file['/'].create_group('front_panel')
        self._store_front_panel_values(data_group, shot_data)
        tab_names = [device_name.encode() for device_name in shot_data]
        save_data = [repr(device_state["saved_data"]).encode() for device_state in shot_data.values()]
        data_group.create_dataset(
            "_notebook_data",
            data=numpy.array(
                list(zip(tab_names, save_data)),
                dtype=[('tab_name', _bytes_dtype(tab_names)), ('data', _bytes_dtype(save_data))],
            ),
        )

    def _store_front_panel_values(self, data_group, tab_data):
        front_panel_list = []
        for device_name, device_state in tab_data.items():
            logger.debug('saving front panel for device: %s', device_name)
            for hardware_name, data in device_state["front_panel_settings"].items():
                if data != {}:
                    if isinstance(data['base_value'], (str, bytes)):
                        logger.debug('Could not save data for channel %s on device %s because support for output values that are strings is not yet supported.', hardware_name, device_name)
                    elif float(data['base_value']) == data['base_value']:
                        # Encoded here, as numpy would encode a str in an 'S' field as
                        # ASCII; restore() decodes UTF-8.
                        front_panel_list.append((data['name'].encode(),
                                                 device_name.encode(),
                                                 hardware_name.encode(),
                                                 data['base_value'],
                                                 data['locked'],
                                                 data['base_step_size'] if 'base_step_size' in data else 0,
                                                 data.get('current_units', '').encode()
                                                )
                                               )
                    else:
                        logger.warning('Could not save data for channel %s on device %s because the output value (in base units) was not a string or could not be coerced to a float without loss of precision', hardware_name, device_name)

        if front_panel_list:
            names, device_names, channels, _, _, _, units = zip(*front_panel_list)
            front_panel_dtype = [('name',_bytes_dtype(names)),('device_name',_bytes_dtype(device_names)),('channel',_bytes_dtype(channels)),('base_value',float),('locked',bool),('base_step_size',float),('current_units',_bytes_dtype(units))]
            data_group.create_dataset('front_panel',data=numpy.array(front_panel_list,dtype=front_panel_dtype))
