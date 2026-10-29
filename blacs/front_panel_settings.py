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

import labscript_utils.excepthook
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

                for row in dataset:
                    tab_name = ensure_str(row['tab_name'])
                    try:
                        data = eval(ensure_str(row['data']), _SAVE_DATA_NAMESPACE)
                    except Exception:
                        logger.warning(f'Could not read the saved data of {tab_name}, which starts without it')
                        data = {}
                    tab_data[tab_name] = {'notebook':row['notebook'], 'page':row['page'], 'visible':row['visible'], 'data':data}

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
            logger.info(str(e))
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
                                f"This file '{current_file}' already contains a connection table.\n\n"
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
                                logger.info("Front Panel not saved as it already existed in the h5 file '"+current_file+"'")
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
                    logger.info("Front Panel not saved as the connection table in the h5 file '"+current_file+"' didn't match the current connection table.")
                return
        else:
            with h5py.File(current_file,'w') as hdf5_file:
                self.store_front_panel_in_h5(hdf5_file,states,tab_positions,window_data,plugin_data,save_conn_table=True)

    @inmain_decorator(wait_for_return=True)
    def store_front_panel_in_h5(self, hdf5_file,tab_data,notebook_data,window_data,plugin_data,save_conn_table=False,shot_file=False):
        if save_conn_table:
            if 'connection table' in hdf5_file:
                del hdf5_file['connection table']
            hdf5_file.create_dataset('connection table', data=self.connection_table.raw_table)

        data_group = hdf5_file['/'].create_group('front_panel')

        front_panel_list = []
        other_data_list = []
        # 'S', not the 'a' alias numpy 2 removed. The bytes on disk are the same, so
        # older settings files still read back.
        front_panel_dtype = [('name','S256'),('device_name','S256'),('channel','S256'),('base_value',float),('locked',bool),('base_step_size',float),('current_units','S256')]
        max_od_length = 2 # empty dictionary

        for device_name, device_state in tab_data.items():
            logger.debug("saving front panel for device:" + device_name)
            for hardware_name, data in device_state["front_panel_settings"].items():
                if data != {}:
                    if isinstance(data['base_value'], (str, bytes)):
                        logger.warning('Could not save data for channel %s on device %s because support for output values that are strings is not yet supported.'%(hardware_name, device_name))
                    elif float(data['base_value']) == data['base_value']:
                        front_panel_list.append((data['name'],
                                                 device_name,
                                                 hardware_name,
                                                 data['base_value'],
                                                 data['locked'],
                                                 data['base_step_size'] if 'base_step_size' in data else 0,
                                                 data['current_units'] if 'current_units' in data else ''
                                                )
                                               )
                    else:
                        logger.warning('Could not save data for channel %s on device %s because the output value (in base units) was not a string or could not be coerced to a float without loss of precision'%(hardware_name, device_name))

            od = repr(device_state["saved_data"])
            other_data_list.append(od)
            max_od_length = len(od) if len(od) > max_od_length else max_od_length

        if front_panel_list:
            front_panel_array = numpy.empty(len(front_panel_list),dtype=front_panel_dtype)
            for i, row in enumerate(front_panel_list):
                front_panel_array[i] = row
            data_group.create_dataset('front_panel',data=front_panel_array)

        i = 0
        tab_data = numpy.empty(len(notebook_data),dtype=[('tab_name','S256'),('notebook','S2'),('page',int),('visible',bool),('data','S'+str(max_od_length))])
        for device_name,data in notebook_data.items():
            tab_data[i] = (device_name,data["notebook"],data["page"],data["visible"],other_data_list[i])
            i += 1

        dataset = data_group.create_dataset("_notebook_data",data=tab_data)
        dataset.attrs['plugin_data'] = repr(plugin_data)
        # A shot file records the front panel's values, not the shot-execution
        # settings, and only BLACS's own settings file records the window geometry.
        if not shot_file:
            dataset.attrs['shot_execution_data'] = repr(window_data['_shot_execution'])
        if hdf5_file.filename == self.settings_path:
            dataset.attrs['window_geometry'] = repr(window_data['geometry'])
