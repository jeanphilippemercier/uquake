# -*- coding: utf-8 -*-
# ------------------------------------------------------------------
# Filename: inventory.py
#  Purpose: Expansion of the obspy.core.inventory.inventory module
#   Author: microquake development team
#    Email: devs@microquake.org
#
# Copyright (C) 2016 microquake development team
# --------------------------------------------------------------------
"""
Expansion of the obspy.core.event module

:copyright:
    microquake development team (devs@microquake.org)
:license:
    GNU Lesser General Public License, Version 3
    (http://www.gnu.org/copyleft/lesser.html)
"""

from obspy.core import inventory, AttribDict, UTCDateTime

from obspy.core.inventory import Network

from obspy.signal.invsim import corn_freq_2_paz
from obspy.core.inventory import (Response, InstrumentSensitivity,
                                  PolesZerosResponseStage)
import numpy as np
import copy

# from obspy.core.inventory import Inventory
from obspy.core.inventory.util import (Equipment, Operator, Person,
                                       PhoneNumber, Site, _textwrap,
                                       _unified_content_strings)

from obspy.clients.nrl import NRL
from .logging import logger
from uquake import __package_name__ as ns

nrl = NRL()

import pandas as pd


def load_from_excel(file_name):
    """
    Read in a multi-sheet excel file with network metadata sheets:
        Sites, Networks, Hubs, Stations, Components, Sensors, Cables,
        Boreholes
    Organize these into a microquake Inventory object

    :param xls_file: path to excel file
    :type: xls_file: str
    :return: inventory
    :rtype: microquake.core.data.inventory.Inventory

    """

    df_dict = pd.read_excel(file_name, sheet_name=None)

    source = df_dict['Sites'].iloc[0]['code']
    # sender (str, optional) Name of the institution sending this message.
    sender = df_dict['Sites'].iloc[0]['operator']
    net_code = df_dict['Networks'].iloc[0]['code']
    net_descriptions = df_dict['Networks'].iloc[0]['name']

    contact_name = df_dict['Networks'].iloc[0]['contact_name']
    contact_email = df_dict['Networks'].iloc[0]['contact_email']
    contact_phone = df_dict['Networks'].iloc[0]['contact_phone']
    site_operator = df_dict['Sites'].iloc[0]['operator']
    site_country = df_dict['Sites'].iloc[0]['country']
    site_name = df_dict['Sites'].iloc[0]['name']
    site_code = df_dict['Sites'].iloc[0]['code']

    print("source=%s" % source)
    print("sender=%s" % sender)
    print("net_code=%s" % net_code)

    network = Network(net_code)
    inventory = Inventory([network], source)

    # obspy requirements for PhoneNumber are super specific:
    # So likely this will raise an error if/when someone changes the value in
    # Networks.contact_phone
    """
    PhoneNumber(self, area_code, phone_number, country_code=None, 
    description=None):
        :type area_code: int
        :param area_code: The area code.
        :type phone_number: str
        :param phone_number: The phone number minus the country and 
        area code. Must be in the form "[0-9]+-[0-9]+", e.g. 1234-5678.
        :type country_code: int, optional
        :param country_code: The country code.
    """

    import re
    phone = re.findall(r"[\d']+", contact_phone)
    area_code = int(phone[0])
    number = "%s-%s" % (phone[1], phone[2])
    phone_number = PhoneNumber(area_code=area_code, phone_number=number)

    person = Person(names=[contact_name], agencies=[site_operator],
                    emails=[contact_email], phones=[phone_number])
    operator = Operator(site_operator, contacts=[person])
    site = Site(name=site_name, description=site_name,
                country=site_country)

    # Merge Stations+Components+Sensors+Cables info into sorted stations +
    # channels dicts:

    df_dict['Stations']['station_code'] = df_dict['Stations']['code']
    df_dict['Sensors']['sensor_code'] = df_dict['Sensors']['code']
    df_dict['Components']['code_channel'] = df_dict['Components']['code']
    df_dict['Components']['sensor'] = df_dict['Components']['sensor__code']
    df_merge = pd.merge(df_dict['Stations'], df_dict['Sensors'],
                        left_on='code', right_on='station__code',
                        how='inner', suffixes=('', '_channel'))

    df_merge2 = pd.merge(df_merge, df_dict['Components'],
                         left_on='sensor_code', right_on='sensor__code',
                         how='inner', suffixes=('', '_sensor'))

    df_merge3 = pd.merge(df_merge2, df_dict['Cable types'],
                         left_on='cable__code', right_on='code',
                         how='inner', suffixes=('', '_cable'))

    df_merge4 = pd.merge(df_merge3, df_dict['Sensor types'],
                         left_on='sensor_type__model', right_on='model',
                         how='inner', suffixes=('', '_sensor_type'))

    df = df_merge4.sort_values(['sensor_code', 'location_code']).fillna(0)

    # Need to sort by unique station codes, then look through 1-3 channels
    # to add
    stn_codes = set(df['sensor_code'])
    stations = []

    for code in stn_codes:
        chan_rows = df.loc[df['sensor_code'] == code]
        row = chan_rows.iloc[0]
        station = {}
        # Set some keys explicitly
        #     from ipdb import set_trace; set_trace()
        station['code'] = '{}'.format(row['sensor_code'])
        station['x'] = row['location_x_channel']
        station['y'] = row['location_y_channel']
        station['z'] = row['location_z_channel']
        station['loc'] = np.array(
            [station['x'], station['y'], station['z']])
        station['long_name'] = "{}.{}.{:02d}".format(row['network__code'],
                                                     row['station_code'],
                                                     row['location_code'])

        # MTH: 2019/07 Seem to have moved from pF to F on Cables sheet:
        station['cable_capacitance_pF_per_meter'] = row['c'] * 1e12

        # Set the rest (minus empty fields) directly from spreadsheet names:
        renamed_keys = {'sensor_code', 'location_x', 'location_y',
                        'location_z', 'name'}

        # These keys are either redundant or specific to channel, not station:
        remove_keys = {'code', 'id_channel', 'orientation_x',
                       'orientation_y', 'orientation_z', 'id_sensor',
                       'enabled_channel', 'station_id', 'id_cable'}
        keys = row.keys()
        empty_keys = keys[pd.isna(row)]
        keys = set(keys) - set(empty_keys) - renamed_keys - remove_keys

        for key in keys:
            station[key] = row[key]

        # Added keys:
        station['motion'] = 'VELOCITY'

        if row['sensor_type'].upper() == 'ACCELEROMETER':
            station['motion'] = 'ACCELERATION'

        # Attach channels:
        station['channels'] = []

        for index, rr in chan_rows.iterrows():
            chan = {}
            chan['cmp'] = rr['code_channel_sensor'].upper()
            chan['orientation'] = np.array([rr['orientation_x'],
                                            rr['orientation_y'],
                                            rr['orientation_z']])
            chan['x'] = row['location_x_channel']
            chan['y'] = row['location_y_channel']
            chan['z'] = row['location_z_channel']
            chan['enabled'] = rr['enabled']
            station['channels'].append(chan)

        stations.append(station)

    # from ipdb import set_trace; set_trace()

    # Convert these station dicts to inventory.Station objects and attach to
    # inventory.network:
    station_list = []

    for station in stations:
        # This is where namespace is first employed:
        station = Station.from_station_dict(station, site_name)
        station.site = site
        station.operators = [operator]
        station_list.append(station)

    network.stations = station_list

    return inventory


def read_inventory(path_or_file_object, format='STATIONXML', *args, **kwargs):

    obspy_inv = inventory.read_inventory(path_or_file_object,
                                         format=format,
                                         *args, **kwargs)

    return Inventory.from_obspy_inventory_object(obspy_inv)


def geophone_response(resonance_frequency, gain, damping=0.707,
                      output_resistance=np.inf,
                      cable_length=np.inf, cable_capacitance=np.inf,
                      sensitivity=1, stage_sequence_number=1):

        paz = corn_freq_2_paz(resonance_frequency,
                              damp=damping)

        l = cable_length
        R = output_resistance
        C = cable_capacitance

        if ((R * l * C) != np.inf) and ((R * l * C) != 0):
            pole_cable = -1 / (R * l * C)
            paz['poles'].append(pole_cable)

        i_s = InstrumentSensitivity(sensitivity, resonance_frequency,
                                    input_units='M/S',
                                    output_units='M/S',
                                    input_units_description='velocity',
                                    output_units_description='velocity')

        pzr = PolesZerosResponseStage(stage_sequence_number, gain,
                                      resonance_frequency, 'M/S', 'M/S',
                                      'LAPLACE (RADIANT/SECOND)',
                                      resonance_frequency, paz['zeros'],
                                      paz['poles'])

        return Response(instrument_sensitivity=i_s,
                        response_stages=[pzr])


def accelerometer_response(resonance_frequency, gain, sensitivity=1,
                           stage_sequence_number=1, damping=0.707):

    i_s = InstrumentSensitivity(sensitivity, resonance_frequency,
                                input_units='M/S/S', output_units='M/S/S',
                                input_units_description='acceleration',
                                output_units_description='acceleration')

    paz = corn_freq_2_paz(resonance_frequency, damp=damping)

    paz['zeros'] = []

    pzr = PolesZerosResponseStage(1, 1, 14, 'M/S/S', 'M/S',
                                  'LAPLACE (RADIANT/SECOND)',
                                  1, [],
                                  paz['poles'])

    return Response(instrument_sensitivity=i_s,
                    response_stages=[pzr])


read_inventory.__doc__ = inventory.read_inventory.__doc__.replace(
    'obspy', ns)


def get_response_from_nrl(datalogger_keys, sensor_keys):
    pass


# class Inventory(inventory.Inventory):
#
#     __doc__ = inventory.Inventory.__doc__.replace('obspy', ns)
#
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)

class Inventory(inventory.Inventory):

    # def __init__(self, *args, **kwargs):
    #     super().__init__(self, *args, **kwargs)

    @classmethod
    def from_obspy_inventory_object(cls, obspy_inventory):

        source = ns         # Network ID of the institution sending
        # the message.

        for network in obspy_inventory.networks:
            stations = []

            for station in network.stations:
                stations.append(Station.from_obspy_station(station))
            network.stations = stations

        return Inventory([network], source)

    def write(self, path_or_file_obj, format='stationxml', *args, **kwargs):
        return super().write(path_or_file_obj, format, nsmap={ns: ns},
                             *args, **kwargs)

    def get_station(self, sta):
        return self.select(sta)

    def get_channel(self, sta=None, cha=None):
        return self.select(sta, cha_code=cha)

    def select(self, network=None, station=None, sensors=None,
               location=None, channel=None):
        """
            Select a single Station or Channel object out of the Inventory
        """
        station_found = None

        for net in self:
            for sta in net.stations:
                if sta.code in station:
                    if network:
                        if net.code == network:
                            station_found = sta
                            break
                    else:
                        station_found = sta

                        break

        if not station_found:
            return None

        channel_found = None

        if channel:
            for cha in station_found.channels:
                if cha.code in channel:
                    channel_found = cha

                    break

            return channel_found

        else:
            return station_found

    def __eq__(self, other):
        return np.all(self.sensors == other.sensors)

    # def write(self, filename):
    #     super().write(self, filename, format='stationxml', nsmap={ns: ns})

    @property
    def sensors(self):
        sensors = []
        for network in self.networks:
            for station in network.stations:
                for sensor in station.sensors:
                    sensors.append(sensor)

        return np.sort(sensors)


class Station(inventory.Station):

    __doc__ = inventory.Station.__doc__.replace('obspy', ns)

    extra_keys = ['x', 'y', 'z']

    def __init__(self, *args, **kwargs):
        super(Station, self).__init__(*args, **kwargs)
        if not hasattr(self, 'extra'):
            self.extra = AttribDict()

        [self.__setattr__(key, 0) for key in self.extra_keys]

    # def __setattr__(self, name, value):
    #     _set_attr_handler(self, name, value)

    @classmethod
    def from_obspy_station(cls, obspy_station):
        #     cls(*params) is same as calling Station(*params):

        stn = cls(obspy_station.code, obspy_station.latitude,
                  obspy_station.longitude, obspy_station.elevation)
        for key in obspy_station.__dict__.keys():
            try:
                stn.__dict__[key] = obspy_station.__dict__[key]
            except Exception as e:
                logger.error(e)

        stn.channels = []

        for cha in obspy_station.channels:
            stn.channels.append(Channel.from_obspy_channel(cha))

        return stn

    @classmethod
    def from_station_dict(cls, station_dict, site):
        stn = station_dict

        equipments = []
        if 'manufacturer_sensor' in stn:
            equipments = [Equipment(type='Sensor',
                                    manufacturer=stn['manufacturer_sensor'],
                                    model=stn['model'])]

        sta = cls(stn['station_code'], latitude=0., longitude=0.,
                  elevation=0., site=Site(name=site),
                  equipments=equipments,
                  historical_code=stn['long_name'],
                  creation_date=UTCDateTime("2015-12-31T12:23:34.5"),
                  start_date=UTCDateTime("2015-12-31T12:23:34.5"),
                  end_date=UTCDateTime("2599-12-31T12:23:34.5"))

        non_extras_keys = {'code', 'long_name', 'channels', 'start_date',
                           'end_date'}

        sta.channels = []

        for cha in stn['channels']:

            response = None
            if stn['nrl_sensor_keys']:
                sensor_keys = [key.strip() for key in
                               stn['nrl_sensor_keys'].split(',')]
                response = nrl.get_sensor_response(sensor_keys)

            elif stn['motion'].upper() == 'ACCELERATION':
                response = accelerometer_response(stn['resonance_frequency'],
                                                  stn['gain'])

            elif stn['motion'].upper() == 'VELOCITY':
                response = geophone_response(stn['resonance_frequency'],
                                             stn['gain'],
                                             damping=stn['damping'],
                                             output_resistance=stn[
                                                 'coil_resistance'],
                                             cable_length=stn['cable_length'],
                                             cable_capacitance=stn['c'])
            else:
                print("Unknown motion=[%s]" % stn['motion'])
                exit()

            channel_code = f'{stn["channel_base_code"].upper()}' \
                f'{cha["cmp"].upper()}'

            channel = Channel(code=channel_code,  # required
                              location_code=f'{stn["location_code"]:02d}',
                              # required
                              latitude=0.,  # required
                              longitude=0.,  # required
                              elevation=0.,  # required
                              depth=0.,  # required
                              start_date=UTCDateTime("1999-12-31T12:23:34.5"),
                              end_date=UTCDateTime("2599-12-31T12:23:34.5"),
                              azimuth=0,
                              dip=0,
                              response=response)

            sta.channels.append(channel)

            channel.x = cha['x']
            channel.y = cha['y']
            channel.z = cha['z']
            channel.set_orientation(cha['orientation'])
            channel.alternative_code = stn['code']

        return sta

    def __setattr__(self, key, value):
        if key in self.extra_keys:
            if not hasattr(self, 'extra'):
                self.extra = {}

            self.extra[key] = {'value': value, 'namespace': ns}
        else:
            super().__setattr__(key, value)

    @property
    def x(self):
        if self.extra:
            if self.extra.get('x', None):
                return float(
                    self.extra.x.value)  # obspy inv_read converts everything
                                         # in extra to str
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def y(self):
        if self.extra:
            if self.extra.get('y', None):
                return float(self.extra.y.value)
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def z(self):
        if self.extra:
            if self.extra.get('z', None):
                return float(self.extra.z.value)
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def loc(self):
        if self.extra:
            if self.extra.get('x', None) and self.extra.get(
                    'y', None) and self.extra.get(
                    'z', None):
                return np.array([self.x, self.y, self.z])
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def sensors(self):
        location_codes = []
        channel_dict = {}
        sensors = []
        for channel in self.channels:
            location_codes.append(channel.location_code)
            channel_dict[channel.location_code] = []

        for channel in self.channels:

            channel_dict[channel.location_code].append(channel)

        for key in channel_dict.keys():
            sensors.append(Sensor(self, channel_dict[key]))

        return sensors

    def __str__(self):
        contents = self.get_contents()

        x = self.latitude
        y = self.longitude
        z = self.elevation
        sensor_count = len(self.sensors)
        channel_count = len(self.channels)
        ret = (f"\tStation {self.historical_code}\n"
               f"\tStation Code: {self.code}\n"
               f"\tSensor Count: {sensor_count}\n"
               f"\tChannel Count: {channel_count}\n"
               f"\t{self.start_date} - {self.end_date}\n"
               f"\tx: {x:.0f}, y: {y:.0f}, z: {z:.0f} m\n")

        if getattr(self, 'extra', None):
            if getattr(self.extra, 'x', None) and getattr(self.extra, 'y', None):
                x = self.x
                y = self.y
                z = self.z
                ret = ("Station {station_name}\n"
                       "\tStation Code: {station_code}\n"
                       "\tChannel Count: {selected}/{total}"
                       " (Selected/Total)\n"
                       "\t{start_date} - {end_date}\n"
                       "\tEasting [x]: {x:.0f} m, Northing [y] m: {y:.0f}, "
                       "Elevation [z]: {z:.0f} m\n")

        ret = ret.format(
            station_name=contents["stations"][0],
            station_code=self.code,
            selected=self.selected_number_of_channels,
            total=self.total_number_of_channels,
            start_date=str(self.start_date),
            end_date=str(self.end_date) if self.end_date else "",
            restricted=self.restricted_status,
            alternate_code="Alternate Code: %s " % self.alternate_code if
            self.alternate_code else "",
            historical_code="Historical Code: %s " % self.historical_code if
            self.historical_code else "",
            x=x, y=y, z=z)
        ret += "\tAvailable Channels:\n"
        ret += "\n".join(_textwrap(
            ", ".join(_unified_content_strings(contents["channels"])),
            initial_indent="\t\t", subsequent_indent="\t\t",
            expand_tabs=False))

        return ret


class Sensor:
    """
    This class is a container for grouping the channels into coherent entity
    that are sensors. From the uquake package perspective a station is
    the physical location where data acquisition instruments are grouped.
    One or multiple sensors can be connected to a station.
    """

    def __init__(self, station, channels):

        location_codes = []
        for channel in channels:
            location_codes.append(channel.location_code)

        if len(np.unique(location_codes)) > 1:
            logger.error('the channels in the channel list should have a'
                         'unique location code')
            raise KeyError

        self.station = station
        self.channels = channels

    def __repr__(self):
        ret = f'\tSensor {self.sensor_code}\n' \
              f'\tx: {self.x:.0f} m, y: {self.y:.0f} m z: {self.z:0.0f} m\n' \
              f'\tChannel Count: {len(self.channels)}'

        return ret

    def __str__(self):
        return self.sensor_code

    def __eq__(self, other):
        return str(self) == str(other)

    def __lt__(self, other):
        return str(self) < str(other)

    def __gt__(self, other):
        return str(self) > str(other)

    @property
    def loc(self):
        return np.array([self.x, self.y, self.z])

    @property
    def alternate_code(self):
        return self.channels[0].alternative_code

    @property
    def x(self):
        return self.channels[0].x

    @property
    def y(self):
        return self.channels[0].y

    @property
    def z(self):
        return self.channels[0].z

    @property
    def station_code(self):
        return self.station.code

    @property
    def location_code(self):
        return self.channels[0].location_code

    @property
    def code(self):
        return f'{self.station_code}{self.location_code}'

    @property
    def sensor_type_code(self):
        return self.channels[0].code[0:-1]

    @property
    def sensor_code(self):
        return f'{self.station_code}.{self.location_code}.' \
               f'{self.sensor_type_code}'


class Channel(inventory.Channel):

    defaults = {}
    extra_keys = ['x', 'y', 'z', 'alternative_code']

    __doc__ = inventory.Channel.__doc__.replace('obspy', ns)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for key in self.extra_keys:
            if not hasattr(self, 'extra'):
                self.extra = AttribDict()

            self.extra[key] = {'value': 0, 'namespace': ns}

    @classmethod
    def from_obspy_channel(cls, obspy_channel):

        cha = cls(obspy_channel.code, obspy_channel.location_code,
                  obspy_channel.latitude, obspy_channel.longitude,
                  obspy_channel.elevation, obspy_channel.depth)

        if hasattr(obspy_channel, 'extra'):
            for key in cha.extra_keys:
                cha.__dict__['extra'][key] = \
                    obspy_channel.__dict__['extra'][key]

        for key in obspy_channel.__dict__.keys():
            cha.__dict__[key] = obspy_channel.__dict__[key]

        return cha

    def __setattr__(self, key, value):
        if key in self.extra_keys:
            if not hasattr(self, 'extra'):
                self.extra = {}

            self.extra[key] = {'value': value, 'namespace': ns}
        else:
            super().__setattr__(key, value)

    def __repr__(self):

        ret = f'Channel {self.code}, Location {self.location_code}\n' \
              f'Time range: {self.start_date} - {self.end_date}\n' \
              f'Easting [x]: {self.x:0.0f} m, Northing [y]: ' \
              f'{self.y:0.0f} m, Elevation [z]: {self.z:0.0f} m\n' \
              f'Dip (degrees): {self.dip:0.0f}, Azimuth (degrees): ' \
              f'{self.azimuth:0.0f}\n' \

        if self.response:
            ret += "Response information available"
        else:
            ret += "Response information not available"

        return ret

# Time range: 2015-12-31T12:23:34.500000Z - 2599-12-31T12:23:34.500000Z
# Latitude: 0.00, Longitude: 0.00, Elevation: 0.0 m, Local Depth: 0.0 m
# Azimuth: 0.00 degrees from north, clockwise
# Dip: 0.00 degrees down from horizontal
# Response information available'

    def set_orientation(self, orientation_vector):
        """
        set the Azimuth and Dip from an orientation vector assuming the
        orientation vector provided is east, north, up.
        :param self:
        :param orientation_vector:
        :return:
        """

        east = orientation_vector[0]
        north = orientation_vector[1]
        up = orientation_vector[2]

        horizontal_length = np.linalg.norm([east, north])

        azimuth = np.arctan2(east, north) * 180 / np.pi
        if azimuth < 0:
            self.azimuth = 360 + self.azimuth
        else:
            self.azimuth = azimuth

        self.dip = np.arctan2(-up, horizontal_length) * 180 / np.pi

    @property
    def orientation_vector(self):

        up = -np.sin(self.dip)
        east = np.sin(self.azimuth) * np.cos(self.dip)
        north = np.cos(self.azimuth) * np.cos(self.dip)

        return np.array([east, north, up])

    @property
    def x(self):
        if self.extra:
            if self.extra.get('x', None):
                return float(
                    self.extra.x.value)  # obspy inv_read converts everything in extra to str
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def y(self):
        if self.extra:
            if self.extra.get('y', None):
                return float(
                    self.extra.y.value)  # obspy inv_read converts everything in extra to str
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def z(self):
        if self.extra:
            if self.extra.get('z', None):
                return float(
                    self.extra.z.value)  # obspy inv_read converts everything in extra to str
            else:
                raise AttributeError
        else:
            raise AttributeError

    @property
    def loc(self):
        return np.array([self.x, self.y, self.z])

    @property
    def alternative_code(self):
        if self.extra:
            if self.extra.get('alternative_code', None):
                return self.extra.alternative_code.value  # obspy inv_read converts everything in extra to str
            else:
                raise AttributeError
        else:
            raise AttributeError


