#####################################################################
#                                                                   #
# /blacs/device_base_class.py                                       #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program BLACS, in the labscript suite    #
# (see http://labscriptsuite.org), and is licensed under the        #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################
import logging
import sys
import os
import time
from queue import Queue

from qtutils.qt.QtCore import QTimer
from qtutils.qt.QtGui import QIcon
from qtutils.qt.QtWidgets import (
    QWidget,
    QSpacerItem,
    QSizePolicy,
    QPushButton,
    QHBoxLayout,
    QApplication,
    QVBoxLayout,
)

# Imported for its side effect: it installs the suite's exception hook.
import labscript_utils.excepthook
from qtutils import UiLoader

from blacs import BLACS_DIR
from blacs.tab_base_classes import Tab, Worker, define_state
from blacs.tab_base_classes import MODE_MANUAL, MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL, MODE_BUFFERED
from blacs.output_classes import AO, DO, DDS, Image, EO
from labscript_utils.qtwidgets.toolpalette import ToolPaletteGroup
from labscript_utils.shared_drive import path_to_agnostic


class DeviceTab(Tab):
    def __init__(self,notebook,settings,restart=False):
        super().__init__(notebook,settings,restart)
        self.connection_table = settings['connection_table']
        
        self._AO = {}
        self._DO = {}
        self._DDS = {}
        self._image = {}
        self._EO = {}
        
        self._final_values = {}
        self._last_programmed_values = {}
        self._last_remote_values = {}
        self._primary_worker = None
        self._secondary_workers = []
        self._can_check_remote_values = False
        self._changed_radio_buttons = {}
        
        self.initialise_GUI() 
        self.restore_save_data(self.settings['saved_data'] if 'saved_data' in self.settings else {})
        self.initialise_workers()
        self._last_programmed_values = self.get_front_panel_values()
        # A device whose remote values can be checked is not programmed here: the
        # check finds it correct, or asks the user to resolve the inconsistency.
        if self._can_check_remote_values:
            self.statemachine_timeout_add(30000,self.check_remote_values)     
        else:       
            self.program_device()
            
    def initialise_GUI(self):
        """Override to create the device's outputs and widgets."""
        pass
        
    def initialise_workers(self):
        """Override to create the workers, and set the primary worker."""
        pass

    @property
    def primary_worker(self):
        return self._primary_worker
        
    @primary_worker.setter
    def primary_worker(self,worker):
        self._primary_worker = worker
    
    def add_secondary_worker(self,worker):
        if worker not in self._secondary_workers:
            self._secondary_workers.append(worker)
    
    def supports_remote_value_check(self,support):
        self._can_check_remote_values = bool(support)
    
    # What the properties dictionaries need to look like:
    #
    # digital_properties = {'hardware_channel_reference':{}, 'do0':{}}
    #
    #
    # analog_properties = {'hardware_channel_reference':{'base_unit':'V',
    #                                                    'min':-10.0,
    #                                                    'max':10.0,
    #                                                    'step':0.01,
    #                                                    'decimals':3
    #                                                    },
    #                      'ao1':{'base_unit':'V',
    #                             'min':-10.0,
    #                             'max':10.0,
    #                             'step':0.01,
    #                             'decimals':3
    #                             },
    #                     }
    #
    #
    #    dds_properties = {'hardware_channel_reference':{'freq':{'base_unit':'Hz',
    #                                                            'min':-10.0,
    #                                                            'max':10.0,
    #                                                            'step':0.01,
    #                                                            'decimals':3
    #                                                            },
    #                                                     'amp':{'base_unit':'Vpp',
    #                                                            'min':0.0,
    #                                                            'max':1.0,
    #                                                            'step':0.1,
    #                                                            'decimals':3
    #                                                            },  
    #                                                   'phase':{'base_unit':'Degrees',
    #                                                            'min':0.0,
    #                                                            'max':360.0,
    #                                                            'step':1,
    #                                                            'decimals':2
    #                                                            },  
    #                                                    'gate':{},
    #                                                    },    
    #                      'dds1':{'freq':{'base_unit':'Hz',
    #                                      'min':-10.0,
    #                                      'max':10.0,
    #                                      'step':0.01,
    #                                      'decimals':3
    #                                      },
    #                               'amp':{'base_unit':'Vpp',
    #                                      'min':0.0,
    #                                      'max':1.0,
    #                                      'step':0.1,
    #                                      'decimals':3
    #                                      },  
    #                             'phase':{'base_unit':'Degrees',
    #                                      'min':0.0,
    #                                      'max':360.0,
    #                                      'step':1,
    #                                      'decimals':2
    #                                      }, 
    #                              'gate':{},
    #                             },
    #                     }
    #
    #
    # eo_properties = {'hardware_channel_reference': {'options':['option 1', 'option 2']},
    #                  'eo1': {'options': {'option 1': {'index': 0, 'tooltip': 'description 1'},
    #                                      'option 2': {'index': 3, 'tooltip': 'description 2'},
    #                                      'option 3': 1},
    #                          'return_index': True}}

    def create_digital_outputs(self,digital_properties):
        for hardware_name,properties in digital_properties.items():
            self._DO[hardware_name] = self._create_DO_object(self.device_name,hardware_name,hardware_name,properties)
    
    def _create_DO_object(self,parent_device,BLACS_hardware_name,labscript_hardware_name,properties):
        device = self.get_child_from_connection_table(parent_device,labscript_hardware_name)
        connection_name = device.name if device else '-'

        return DO(BLACS_hardware_name, connection_name, self.device_name, self.program_device, self.settings)

    def create_image_outputs(self,image_properties):
        for hardware_name,properties in image_properties.items():
            self._image[hardware_name] = self._create_image_object(self.device_name,hardware_name,hardware_name,properties)
    
    def _create_image_object(self,parent_device,BLACS_hardware_name,labscript_hardware_name,properties):
        device = self.get_child_from_connection_table(parent_device,labscript_hardware_name)
        connection_name = device.name if device else '-'
        
        # Pass on only the keyword arguments that Image accepts.
        prop = {}
        accepted_kwargs = ['width', 'height', 'x', 'y']
        for kwarg in accepted_kwargs:
            if kwarg in properties:
                prop[kwarg] = properties[kwarg]                
        
        return Image(BLACS_hardware_name, connection_name, self.device_name, self.program_device, self.settings, **prop)
    
    def create_analog_outputs(self,analog_properties):
        for hardware_name,properties in analog_properties.items():                    
            self._AO[hardware_name] = self._create_AO_object(self.device_name,hardware_name,hardware_name,properties)

    def _create_AO_object(self,parent_device,BLACS_hardware_name,labscript_hardware_name,properties):
        device = self.get_child_from_connection_table(parent_device,labscript_hardware_name)
        connection_name = device.name if device else '-'
        
        calib_class = None
        calib_params = {}
        if device:
            calib_class = device.unit_conversion_class
            calib_params = device.unit_conversion_params
        
        return AO(BLACS_hardware_name, connection_name, self.device_name, self.program_device, self.settings, calib_class, calib_params,
                properties['base_unit'], properties['min'], properties['max'], properties['step'], properties['decimals'])
            
    def create_dds_outputs(self,dds_properties):
        for hardware_name,properties in dds_properties.items():
            device = self.get_child_from_connection_table(self.device_name,hardware_name)
            connection_name = device.name if device else '-'
        
            subchnl_name_list = ['freq','amp','phase']
            sub_chnls = {}
            for subchnl in subchnl_name_list:
                if subchnl in properties:
                    sub_chnls[subchnl] = self._create_AO_object(connection_name,hardware_name+'_'+subchnl,subchnl,properties[subchnl])
            
            if 'gate' in properties:
                sub_chnls['gate'] = self._create_DO_object(connection_name,hardware_name+'_gate','gate',properties)
            
            self._DDS[hardware_name] = DDS(hardware_name,connection_name,sub_chnls)

    def create_enum_outputs(self, eo_properties):
        for output_name, properties in eo_properties.items():
            self._EO[output_name] = self._create_EO_object(self.device_name, output_name, properties)

    def _create_EO_object(self, parent_device, device_property, properties):
        properties.setdefault('return_index', False)
        return EO(device_property, parent_device, self.device_name, self.program_device, self.settings,
                    properties['options'], properties['return_index'])

    def get_child_from_connection_table(self, parent_device_name, port):
        return self.connection_table.find_child(parent_device_name, port)
    
    def create_digital_widgets(self,channel_properties):
        widgets = {}
        for hardware_name,properties in channel_properties.items():
            properties.setdefault('args',[])
            properties.setdefault('kwargs',{})

            device = self.get_child_from_connection_table(self.device_name,hardware_name)
            properties['kwargs']['inverted'] = bool(device.properties.get('inverted', False) if device else properties['kwargs'].get('inverted', False))

            if hardware_name in self._DO:
                widgets[hardware_name] = self._DO[hardware_name].create_widget(*properties['args'],**properties['kwargs'])
        
        return widgets
        
    def create_image_widgets(self,channel_properties):
        widgets = {}
        for hardware_name,properties in channel_properties.items():
            properties.setdefault('args',[])
            properties.setdefault('kwargs',{})
            if hardware_name in self._image:
                widgets[hardware_name] = self._image[hardware_name].create_widget(*properties['args'],**properties['kwargs'])
        
        return widgets
        
    def create_analog_widgets(self,channel_properties):
        widgets = {}
        for hardware_name,properties in channel_properties.items():
            properties.setdefault('display_name',None)
            properties.setdefault('horizontal_alignment',False)
            properties.setdefault('parent',None)
            if hardware_name in self._AO:
                widgets[hardware_name] = self._AO[hardware_name].create_widget(properties['display_name'],properties['horizontal_alignment'],properties['parent'])
        
        return widgets
        
    def create_dds_widgets(self,channel_properties):
        widgets = {}
        for hardware_name,properties in channel_properties.items():
            properties.setdefault('args',[])
            properties.setdefault('kwargs',{})
            if hardware_name in self._DDS:
                widgets[hardware_name] = self._DDS[hardware_name].create_widget(*properties['args'],**properties['kwargs'])
        
        return widgets
    
    def create_enum_widgets(self,device_properties):

        widgets = {}
        for output_name, properties in device_properties.items():
            properties.setdefault('display_name',output_name)
            properties.setdefault('horizontal_alignment',False)
            properties.setdefault('parent',None)
            widgets[output_name] = self._EO[output_name].create_widget(properties['display_name'], properties['horizontal_alignment'], properties['parent'])

        return widgets
    
    def auto_create_enum_widgets(self):
        eo_properties = {}
        for channel,_ in self._EO.items():
            eo_properties[channel] = {}
        return self.create_enum_widgets(eo_properties)
    
    def auto_create_widgets(self):
        dds_properties = {}
        for channel,output in self._DDS.items():
            dds_properties[channel] = {}
        dds_widgets = self.create_dds_widgets(dds_properties)
        
        ao_properties = {}
        for channel,output in self._AO.items():
            ao_properties[channel] = {}
        ao_widgets = self.create_analog_widgets(ao_properties)
        
        do_properties = {}
        for channel,output in self._DO.items():
            do_properties[channel] = {}
        do_widgets = self.create_digital_widgets(do_properties)
        
        image_properties = {}
        for channel,output in self._image.items():
            image_properties[channel] = {}
        image_widgets = self.create_image_widgets(image_properties)        
        
        # Devices written before the Image output class expect only three dicts.
        if self._image:
            return dds_widgets, ao_widgets, do_widgets, image_widgets
        else:
            return dds_widgets, ao_widgets, do_widgets
    
    def auto_place_widgets(self,*args):
        """Place widgets in the tab layout, in tool palettes.

        Each argument is a dict of widgets from a ``create_*_widgets`` method, which
        goes in a palette named for its channel type, or a tuple ``(name, widgets)``
        or ``(name, widgets, key)`` to name the palette, and to sort its channels by
        ``key(channel_name)``. Empty and unrecognised arguments are skipped."""
        widget = QWidget()
        toolpalettegroup = ToolPaletteGroup(widget)
        for arg in args:
            sort_algorithm = lambda x: x
            if isinstance(arg, tuple) and len(arg) > 1 and isinstance(arg[1], dict) and len(arg[1].keys()) > 0:
                name = arg[0]
                widget_dict = arg[1]
                if len(arg) > 2:
                    sort_algorithm = arg[2]
            else:
                # Skip anything that is not a non-empty dict.
                if not isinstance(arg, dict) or len(arg.keys()) < 1:
                    continue
                if isinstance(self.get_channel(list(arg.keys())[0]),AO):
                    name = 'Analog Outputs'
                elif isinstance(self.get_channel(list(arg.keys())[0]),DO):
                    name = 'Digital Outputs'
                elif isinstance(self.get_channel(list(arg.keys())[0]),Image):
                    name = 'Image Outputs'
                elif isinstance(self.get_channel(list(arg.keys())[0]),DDS):
                    name = 'DDS Outputs'
                else:
                    # Other channel types have no default name: pass them as (name, dict).
                    continue
                widget_dict = arg
            if toolpalettegroup.has_palette(name):
                toolpalette = toolpalettegroup.get_palette(name)
            else:
                toolpalette = toolpalettegroup.append_new_palette(name)
                
            for channel in sorted(widget_dict.keys(),key=sort_algorithm):
                toolpalette.addWidget(widget_dict[channel],True)
         
        self.get_tab_layout().addWidget(widget)
        self.get_tab_layout().addItem(QSpacerItem(0,0,QSizePolicy.Minimum,QSizePolicy.MinimumExpanding))
    
    def get_save_data(self):
        """Override to return a dict of data to save that is not stored in an AO, DO,
        Image or DDS object. It is passed to ``restore_save_data`` when the tab is
        initialised."""
        return {}
    
    def restore_save_data(self,data):
        """Override to restore the dict that ``get_save_data`` returned.

        It may be empty or lack keys, as when BLACS first starts on a PC or its
        datastore has been destroyed, so handle that. It also holds the tab's own
        ``_terminal_visible`` and ``_splitter_sizes``."""
        return
    
    def update_from_settings(self,settings):
        super().update_from_settings(settings)
        self.restore_save_data(settings['saved_data'])
    
        self.settings = settings
        for output in [self._AO, self._DO, self._image, self._EO]:
            for name,channel in output.items():
                if not channel._locked:
                    channel._update_from_settings(settings)
                    
        for name,channel in self._DDS.items():
            for subchnl_name in channel._sub_channel_list:
                if hasattr(channel,subchnl_name):
                    subchnl = getattr(channel,subchnl_name)
                    if not subchnl._locked:
                        subchnl._update_from_settings(settings)
    
    def get_front_panel_values(self):
        return {channel:item.value for output in [self._AO,self._DO,self._image,self._DDS,self._EO] for channel,item in output.items()}

    def get_channel(self,channel):
        if channel in self._AO:
            return self._AO[channel]
        elif channel in self._DO:
            return self._DO[channel]
        elif channel in self._image:
            return self._image[channel]
        elif channel in self._DDS:
            return self._DDS[channel]
        elif channel in self._EO:
            return self._EO[channel]
        else:
            return None
            
    @define_state(MODE_MANUAL,True,delete_stale_states=True)
    def program_device(self):
        """Program the device with the front panel values.

        Runs only in MODE_MANUAL, and stays queued until then. Of consecutive queued
        calls only the last runs, so a burst of changes does not queue many
        programmings."""
        self._last_programmed_values = self.get_front_panel_values()
        
        # Dismiss any "remote values changed" dialog.
        self._changed_widget.hide()
        
        results = yield(self.queue_work(self._primary_worker,'program_manual',self._last_programmed_values))
        for worker in self._secondary_workers:
            if results:
                returned_results = yield(self.queue_work(worker,'program_manual',self._last_programmed_values))
                results.update(returned_results)
        
        # A worker that returns values is coercing the front panel to them.
        if results:
            for channel,remote_value in results.items():
                if channel not in self._last_programmed_values:
                    raise RuntimeError(f'The worker function program_manual for device {self.device_name} is returning data for channel {channel} but the BLACS tab is not programmed to handle this channel')
                
                output = self.get_channel(channel)
                if output is None:
                    raise RuntimeError(f'The channel {channel} on device {self.device_name} is in the last programmed values, but is not in the AO, DO or DDS output store. Something has gone badly wrong!')
                else:                    
                    if output.value == self._last_programmed_values[channel]:
                        output.set_value(remote_value,program=False)
            
                        self._last_programmed_values[channel] = remote_value
    
    @define_state(MODE_MANUAL,True)
    def check_remote_values(self):
        self._last_remote_values = yield(self.queue_work(self._primary_worker,'check_remote_values'))
        for worker in self._secondary_workers:
            if self._last_remote_values:
                returned_results = yield(self.queue_work(worker,'check_remote_values'))
                self._last_remote_values.update(returned_results)
        
        # Raise when nothing came back, so the timeout does not call this again and
        # again, filling the output box with the same error.
        if not self._last_remote_values or not isinstance(self._last_programmed_values, dict):
            raise Exception('Failed to get remote values from device. Is it still connected?')
            
        overall_changed = False
            
        self._changed_radio_buttons = {}
            
        while not self._ui.changed_layout.isEmpty():
            item = self._ui.changed_layout.itemAt(0)
            # setParent(None) is needed: layout.removeItem or removeWidget leaves the
            # old item drawn, with the new items over the top of it.
            item.widget().setParent(None)
            
        # Compare with the last programmed values, not the front panel, so a change
        # still waiting to be programmed is not reported as a mismatch.
        for channel in sorted(self._last_remote_values):
            remote_value = self._last_remote_values[channel]
            if channel not in self._last_programmed_values:
                raise RuntimeError(f'The worker function check_remote_values for device {self.device_name} is returning data for channel {channel} but the BLACS tab is not programmed to handle this channel')
            
            changed = False
            
            if channel in self._DDS:
                front_value = self._last_programmed_values[channel]
                # Format both sides to the sub-channel's decimals, then compare.
                
                front_values_formatted = {}
                remote_values_formatted = {}
                for sub_chnl in front_value:
                    if sub_chnl not in remote_value:
                        raise RuntimeError(f'The worker function check_remote_values has not returned data for the sub-channel {sub_chnl} in channel {channel}')
                    
                    if sub_chnl == 'gate':
                        front_values_formatted[sub_chnl] = str(bool(int(front_value[sub_chnl])))
                        remote_values_formatted[sub_chnl] = str(bool(int(remote_value[sub_chnl])))
                    else:
                        decimals = self._DDS[channel].__getattribute__(sub_chnl)._decimals
                        front_values_formatted[sub_chnl] = f'{front_value[sub_chnl]:.{decimals}f}'
                        remote_values_formatted[sub_chnl] = f'{remote_value[sub_chnl]:.{decimals}f}'
                        
                    if front_values_formatted[sub_chnl] != remote_values_formatted[sub_chnl]:
                        changed = True
                        
                if changed:
                    ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_value_changed_dds.ui'))
                    ui.channel_label.setText(self._DDS[channel].name)
                    for sub_chnl in front_value:
                        ui.__getattribute__(f'front_{sub_chnl}_value').setText(front_values_formatted[sub_chnl])
                        ui.__getattribute__(f'remote_{sub_chnl}_value').setText(remote_values_formatted[sub_chnl])
                    
                    for sub_chnl in self._DDS[channel].get_unused_subchnl_list():
                        ui.__getattribute__(f'front_{sub_chnl}_value').setVisible(False)
                        ui.__getattribute__(f'front_{sub_chnl}_label').setVisible(False)
                        ui.__getattribute__(f'remote_{sub_chnl}_value').setVisible(False)
                        ui.__getattribute__(f'remote_{sub_chnl}_label').setVisible(False)
                
            elif channel in self._DO:
                front_value = str(bool(int(self._last_programmed_values[channel])))
                remote_value = str(bool(int(remote_value)))
                if front_value != remote_value:
                    changed = True
                    ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_value_changed.ui'))
                    ui.channel_label.setText(self._DO[channel].name)
                    ui.front_value.setText(front_value)
                    ui.remote_value.setText(remote_value)
            elif channel in self._AO:
                front_value = f'{self._last_programmed_values[channel]:.{self._AO[channel]._decimals}f}'
                remote_value = f'{remote_value:.{self._AO[channel]._decimals}f}'
                if front_value != remote_value:
                    changed = True
                    ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_value_changed.ui'))
                    ui.channel_label.setText(self._AO[channel].name)
                    ui.front_value.setText(front_value)
                    ui.remote_value.setText(remote_value)
            elif channel in self._EO:
                front_value = str(self._last_programmed_values[channel])
                remote_value = str(remote_value)
                if front_value != remote_value:
                    changed = True
                    ui = UiLoader().load(os.path.join(BLACS_DIR, 'tab_value_changed.ui'))
                    ui.channel_label.setText(self._EO[channel].name)
                    ui.front_value.setText(front_value)
                    ui.remote_value.setText(remote_value)
            else:
                raise RuntimeError('device_base_class.py is not programmed to handle channel types other than DDS, AO and DO in check_remote_values')
                    
            if changed:
                overall_changed = True
            
                self._ui.changed_layout.addWidget(ui)
                
                # on_resolve_value_inconsistency reads these radio buttons.
                self._changed_radio_buttons[channel] = ui.use_remote_values
                
        if overall_changed:
            self._changed_widget.show()
        
            buttonWidget = QWidget()
            buttonlayout = QHBoxLayout(buttonWidget)
            button = QPushButton(QIcon(':/qtutils/fugue/arrow-turn-000-left'), "Apply")
            button.clicked.connect(self.on_resolve_value_inconsistency)
            buttonlayout.addWidget(button)
            buttonlayout.addStretch()

            self._ui.changed_layout.addWidget(buttonWidget)

    def on_resolve_value_inconsistency(self):
        needs_programming = False
        for channel,radio in self._changed_radio_buttons.items():
            if radio.isChecked():
                output = self.get_channel(channel)
                if output is None:
                    raise RuntimeError(f'on_resolve_value_inconsistency is being asked to handle a channel that is not a DDS, AO or DO (channel: {channel}, device: {self.device_name})')
                # The device already has this value, so do not program it.
                output.set_value(self._last_remote_values[channel],program=False)
            else:
                # Programming is needed only if some channel keeps its front panel value.
                needs_programming = True
                
        if needs_programming:
            self.program_device()
        else:
            # Every channel now has the remote value, so these are the last programmed.
            self._last_programmed_values = self.get_front_panel_values()
            
        self._changed_widget.hide()
    
    @define_state(MODE_BUFFERED,True)
    def start_run(self,notify_queue):
        raise NotImplementedError(f'The device {self.device_name} has not implemented a start method and so cannot be used to trigger the experiment to begin. Please implement the start method or use a different pseudoclock as the master pseudoclock')
    
    @define_state(MODE_MANUAL,True)
    def transition_to_buffered(self,h5_file,notify_queue): 
        # Dismiss any "remote values changed" dialog.
        self._changed_widget.hide()
    
        self.mode = MODE_TRANSITION_TO_BUFFERED
        
        h5_file = path_to_agnostic(h5_file)
        # The worker returns the run's final values, to show in the GUI at the end
        # of the run.
        transitioned_called = [self._primary_worker]
        front_panel_values = self.get_front_panel_values()
        self._final_values = yield(self.queue_work(self._primary_worker,'_transition_to_buffered',self.device_name,h5_file,front_panel_values,self._force_full_buffered_reprogram))
        if self._final_values is not None:
            for worker in self._secondary_workers:
                transitioned_called.append(worker)
                extra_final_values = yield(self.queue_work(worker,'_transition_to_buffered',self.device_name,h5_file,front_panel_values,self.force_full_buffered_reprogram))
                if extra_final_values is not None:
                    self._final_values.update(extra_final_values)
                else:
                    self._final_values = None
                    break
        
        # None means a worker did not finish properly.
        if self._final_values is None:
            notify_queue.put([self.device_name,'fail'])
            self.abort_transition_to_buffered(transitioned_called)
        else:
            if self._supports_smart_programming:
                self.force_full_buffered_reprogram = False
                self._ui.button_clear_smart_programming.setEnabled(True)
            # Tell the shot executor that we are done.
            self.mode = MODE_BUFFERED
            notify_queue.put([self.device_name,'success'])
       
    @define_state(MODE_TRANSITION_TO_BUFFERED,False)
    def abort_transition_to_buffered(self,workers=None):
        if workers is None:
            workers = [self._primary_worker]
            workers.extend(self._secondary_workers)
        success = True
        for worker in workers:
            abort_success = yield(self.queue_work(worker,'abort_transition_to_buffered'))
            if not abort_success:
                success = False
                # No break: return as much of the device to normal as possible.
                
        if success:
            self.mode = MODE_MANUAL
            self.program_device()
        else:
            raise Exception('Could not abort transition_to_buffered. You must restart this device to continue')
        
    @define_state(MODE_BUFFERED,False)
    def abort_buffered(self,notify_queue):
        success = yield(self.queue_work(self._primary_worker,'abort_buffered'))
        for worker in self._secondary_workers:
            abort_success = yield(self.queue_work(worker,'abort_buffered'))
            if not abort_success:
                success = False
                # No break: return as much of the device to normal as possible.
        
        if success:
            notify_queue.put([self.device_name,'success'])
            self.mode = MODE_MANUAL
            self.program_device()
        else:
            notify_queue.put([self.device_name,'fail'])
            raise Exception('Could not abort the buffered sequence. You must restart this device to continue')
            
    @define_state(MODE_BUFFERED,False)
    def transition_to_manual(self,notify_queue,program=False):
        self.mode = MODE_TRANSITION_TO_MANUAL
        
        success = yield(self.queue_work(self._primary_worker,'transition_to_manual'))
        for worker in self._secondary_workers:
            transition_success = yield(self.queue_work(worker,'transition_to_manual'))
            if not transition_success:
                success = False
                # No break: return as much of the device to normal as possible.
        
        for channel, value in self._final_values.items():
            if channel in self._AO:
                self._AO[channel].set_value(value,program=False)
            elif channel in self._DO:
                self._DO[channel].set_value(value,program=False)
            elif channel in self._image:
                self._image[channel].set_value(value,program=False)
            elif channel in self._DDS:
                self._DDS[channel].set_value(value,program=False)
            elif channel in self._EO:
                self._EO[channel].set_value(value,program=False)
        
            
        if success:
            notify_queue.put([self.device_name,'success'])
            self.mode = MODE_MANUAL
        else:
            notify_queue.put([self.device_name,'fail'])
            raise Exception('Could not transition to manual. You must restart this device to continue')
            
        if program:
            self.program_device()
        else:
            self._last_programmed_values = self.get_front_panel_values()
            
class DeviceWorker(Worker):
    """Dummy worker for the demo tabs below."""
    def init(self):
        # init, not __init__, is the first call in the new process. Import hardware
        # modules here, not at the top of the file, so they load only in the worker
        # process, not in BLACS; `global` makes them visible to the other methods.
        global serial; import serial
        global time; import time
        
        self.fpv = {}
    
    def initialise(self):
        pass
        
    def shutdown(self):
        pass
        
    def program_manual(self,front_panel_values):
        for channel,value in front_panel_values.items():
            if isinstance(value, float):
                front_panel_values[channel] += 0.001
        self.fpv = front_panel_values
        return front_panel_values
        
    def check_remote_values(self):
        front_panel_values = {}
        for channel,value in self.fpv.items():
            if isinstance(value, float):
                front_panel_values[channel] = value + 1.1
            elif isinstance(value, bool):
                front_panel_values[channel] = not value
            else:
                front_panel_values[channel] = value
        
        if not front_panel_values:
            front_panel_values['ao0'] = 0
        
        return front_panel_values
        
    def transition_to_buffered(self,device_name,h5file,front_panel_values,refresh):
        time.sleep(3)
        for channel,value in front_panel_values.items():
            if isinstance(value, float):
                front_panel_values[channel] += 0.003
        return front_panel_values
        
    def abort_transition_to_buffered(self):
        pass
        
    def abort_buffered(self):
        pass
        
    def transition_to_manual(self):
        return True
        
            
if __name__ == '__main__':
    import logging.handlers
    logger = logging.getLogger('BLACS')
    handler = logging.handlers.RotatingFileHandler(os.path.join(BLACS_DIR, 'BLACS.log'), maxBytes=1024**2, backupCount=0)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    if sys.stdout is not None and sys.stdout.isatty():
        terminalhandler = logging.StreamHandler(sys.stdout)
        terminalhandler.setFormatter(formatter)
        terminalhandler.setLevel(logging.DEBUG)
        logger.addHandler(terminalhandler)
    else:
        sys.stdout = sys.stderr = open(os.devnull)
    logger.setLevel(logging.DEBUG)
    logger.info('\n\n===============starting===============\n')
            
if __name__ == '__main__':
    from labscript_utils.connections import ConnectionTable
    from labscript_utils.qtwidgets.dragdroptab import DragDropTabWidget
    
    class MyDAQTab(DeviceTab):
        
        def initialise_GUI(self):
            do_prop = {}
            for i in range(32):
                do_prop[f'port0/line{i}'] = {}
            self.create_digital_outputs(do_prop)
                
            ao_prop = {}
            for i in range(4):
                ao_prop[f'ao{i}'] = {'base_unit':'V',
                                     'min':-10.0,
                                     'max':10.0,
                                     'step':0.01,
                                     'decimals':3
                                    }            
            self.create_analog_outputs(ao_prop)
            
            dds_widgets,ao_widgets,do_widgets = self.auto_create_widgets()
            
            # A sort key for the channel names, as the third item of an
            # auto_place_widgets tuple.
            def sort(channel):
                port,line = channel.replace('port','').replace('line','').split('/')
                port,line = int(port),int(line)
                return f'{port:02d}/{line:02d}'
            
            self.auto_place_widgets(("DDS Outputs",dds_widgets),("Analog Outputs",ao_widgets),("Digital Outputs - Port 0",do_widgets,sort))
            
            self.create_worker("my_worker_name",DeviceWorker,{})
            self.primary_worker = "my_worker_name"    
            self.create_worker("my_secondary_worker_name",DeviceWorker,{})
            self.add_secondary_worker("my_secondary_worker_name")
    
            self.supports_remote_value_check(True)
    
            button1 = QPushButton("Transition to Buffered")
            button1.clicked.connect(lambda: self.transition_to_buffered('',Queue()))
            self.get_tab_layout().addWidget(button1)
            button2 = QPushButton("Transition to Manual")
            button2.clicked.connect(lambda: self.transition_to_manual(Queue()))
            self.get_tab_layout().addWidget(button2)

    class MyDummyTab(DeviceTab):
        
        def initialise_GUI(self):
            do_prop = {}
            for i in range(1):
                do_prop[f'port0/line{i}'] = {}
            self.create_digital_outputs(do_prop)
                
            ao_prop = {}
            for i in range(1):
                ao_prop[f'ao{i}'] = {'base_unit':'V',
                                     'min':-10.0,
                                     'max':10.0,
                                     'step':0.01,
                                     'decimals':3
                                    }            
            self.create_analog_outputs(ao_prop)

            eo_prop = {
                'Enum1':{
                    'options':['option 1', 'option 2'],
                    'return_index':True,
                },
                'Enum2':{
                    'options':{
                        'option 1':{'index':2, 'tooltip':'description 1'},
                        'option 2':4,
                    }
                }
            }
            self.create_enum_outputs(eo_prop)

            dds_widgets,ao_widgets,do_widgets = self.auto_create_widgets()
            eo_widgets = self.auto_create_enum_widgets()
            
            # A sort key for the channel names, as the third item of an
            # auto_place_widgets tuple.
            def sort(channel):
                port,line = channel.replace('port','').replace('line','').split('/')
                port,line = int(port),int(line)
                return f'{port:02d}/{line:02d}'
            
            self.auto_place_widgets(("DDS Outputs",dds_widgets),
                                    ("Analog Outputs",ao_widgets),
                                    ("Digital Outputs - Port 0",do_widgets),
                                    ('Enums', eo_widgets))
            
            self.create_worker("my_worker_name",DeviceWorker,{})
            self.primary_worker = "my_worker_name"    
            self.create_worker("my_secondary_worker_name",DeviceWorker,{})
            self.add_secondary_worker("my_secondary_worker_name")
    
            self.supports_remote_value_check(True)
    
            button1 = QPushButton("Transition to Buffered")
            button1.clicked.connect(lambda: self.transition_to_buffered('',Queue()))
            self.get_tab_layout().addWidget(button1)
            button2 = QPushButton("Transition to Manual")
            button2.clicked.connect(lambda: self.transition_to_manual(Queue()))
            self.get_tab_layout().addWidget(button2)
    
    connection_table = ConnectionTable('../tests/device_base_classes_connection_table.h5')
    
    class MyWindow(QWidget):
        
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.are_we_closed = False
            self.my_tabs = []
        
        def closeEvent(self,event):
            if not self.are_we_closed:        
                event.ignore()
                for tab in self.my_tabs:
                    tab.close_tab()
                self.are_we_closed = True
                QTimer.singleShot(1000,self.close)
            else:
                event.accept()
    
        def add_my_tab(self,tab):
            self.my_tabs.append(tab)
    
    app = QApplication(sys.argv)
    window = MyWindow()
    layout = QVBoxLayout(window)
    notebook = DragDropTabWidget()
    layout.addWidget(notebook)
    
    tab1 = MyDAQTab(notebook,settings = {'device_name': 'ni_pcie_6363_0', 'connection_table':connection_table})
    tab2 = MyDummyTab(notebook,settings = {'device_name': 'intermediate_device', 'connection_table':connection_table})
    window.add_my_tab(tab1)
    window.add_my_tab(tab2)
    window.show()
    def run():
        app.exec()
        
    sys.exit(run())
