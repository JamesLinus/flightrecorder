#   fifty20.py  Flytec 5020/5030/6020/6030 and Brauniger Galileo/Competino/Compeo/Competino+/Compeo+ functions
#   Copyright (C) 2011  Tom Payne <twpayne@gmail.com>
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.


import datetime
import logging
import re
import struct

from .common import Track, add_igc_filenames
from .errors import ProtocolError, ReadError, TimeoutError, WriteError
import nmea
from .utc import UTC
from .waypoint import Waypoint


MANUFACTURER = {}
for model in '5020 5030 6020 6030'.split():
    MANUFACTURER[model] = 0
for model in 'COMPEO COMPEO+ COMPETINO COMPETINO+ GALILEO'.split():
    MANUFACTURER[model] = 1

MEMORY_MAP = {
        'glider_id': (224, '16s', str),
        'glider_type': (192, '16s', str),
        'pilot_name': (0, '16s', str),
        'recording_interval': (97, 'B', int)}

XON = '\021'
XOFF = '\023'

PBRMEMR_RE = re.compile(r'\APBRMEMR,([0-9A-F]+),([0-9A-F]+(?:,[0-9A-F]+)*)\Z')
PBRRTS_RE1 = re.compile(r'\APBRRTS,(\d+),(\d+),0+,(.*)\Z')
PBRRTS_RE2 = re.compile(r'\APBRRTS,(\d+),(\d+),(\d+),([^,]*),(.*?)\Z')
PBRSNP_RE = re.compile(r'\APBRSNP,([^,]*),([^,]*),([^,]*),([^,]*)\Z')
PBRTL_RE = re.compile(r'\APBRTL,(\d+),(\d+),(\d+).(\d+).(\d+),(\d+):(\d+):(\d+),(\d+):(\d+):(\d+)\Z')
PBRWPS_RE = re.compile(r'\APBRWPS,(\d{2})(\d{2}\.\d{3}),([NS]),(\d{3})(\d{2}\.\d{3}),([EW]),([^,]*),([^,]*),(\d+)\Z')


class Route(object):

    def __init__(self, index, name, routepoints):
        self.index = index
        self.name = name
        self.routepoints = routepoints


class Routepoint(object):

    def __init__(self, short_name, long_name):
        self.short_name = short_name
        self.long_name = long_name


class SNP(object):

    def __init__(self, model, pilot_name, serial_number, software_version):
        self.model = model
        self.pilot_name = pilot_name.strip()
        self.serial_number = int(serial_number)
        self.software_version = software_version


class Fifty20(object):

    SUPPORTED_INSTRUMENTS = '5020 5030 6020 6030 COMPEO COMPEO+ COMPETINO COMPETINO+ GALILEO'.split()

    def __init__(self, io):
        self.io = io
        self.buffer = ''
        self._snp = None
        self._tracks = None
        self._waypoints = None

    def readline(self, timeout=1):
        if self.buffer == '':
            self.buffer = self.io.read(timeout)
        if self.buffer[0] == XON:
            self.buffer = self.buffer[1:]
            logging.debug('read XON')
            return XON
        elif self.buffer[0] == XOFF:
            self.buffer = self.buffer[1:]
            logging.debug('read XOFF')
            return XOFF
        else:
            line = ''
            while True:
		index = self.buffer.find('\n')
		if index == -1:
                    line += self.buffer
                    self.buffer = self.io.read(timeout)
		else:
                    line += self.buffer[:index + 1]
                    self.buffer = self.buffer[index + 1:]
                    logging.info('readline %r' % line)
                    return line

    def write(self, line):
        logging.info('write %r' % line)
        self.io.write(line)

    def ieach(self, command, re=None, timeout=1):
        try:
            self.write(command.encode('nmea_sentence'))
            if self.readline(timeout) != XOFF:
                raise ProtocolError
            while True:
		line = self.readline(timeout)
		if line == XON:
                    break
		elif re is None:
                    yield line
		else:
                    m = re.match(line.decode('nmea_sentence'))
                    if m is None:
                        raise ProtocolError(line)
                    yield m
        except:
            self.io.flush()
            raise

    def none(self, command, timeout=1):
        for m in self.ieach(command, None, timeout):
            raise ProtocolError(m)

    def one(self, command, re=None, timeout=1):
        result = None
        for m in self.ieach(command, re, timeout):
            if not result is None:
                raise ProtocolError(m)
            result = m
        return result

    def pbrconf(self):
        self.none('PBRCONF,', None)
        self._snp = None

    def ipbrigc(self):
        return self.ieach('PBRIGC,')

    def pbrmemr(self, address, length):
        result = []
        first, last = address, address + length
        while first < last:
            m = self.one('PBRMEMR,%04X' % first, PBRMEMR_RE)
            if int(m.group(1), 16) != first:
                raise ProtocolError('address mismatch')
            data = list(int(i, 16) for i in m.group(2).split(','))
            result.extend(data)
            first += len(data)
        return result[:length]

    def pbrmemw(self, address, value):
        while value:
            chunk = value[:8]
            m = self.one('PBRMEMW,%04X,%d%s%s' % (address, len(chunk), ''.join(',%02X' % ord(c) for c in chunk), ',' * (8 - len(chunk))), PBRMEMR_RE)
            if int(m.group(1), 16) != address:
                raise ProtocolError('address mismatch')
            if not ''.join(chr(int(i, 16)) for i in m.group(2).split(',')).startswith(chunk):
                raise ProtocolError('readback mismatch')
            address += len(chunk)
            value = value[len(chunk):]

    def ipbrrts(self):
        for l in self.ieach('PBRRTS,'):
            l = l.decode('nmea_sentence')
            m = PBRRTS_RE1.match(l)
            if m:
		index, count, name = int(m.group(1)), int(m.group(2)), m.group(3)
		if count == 1:
                    yield Route(index, name, [])
		else:
                    routepoints = []
            else:
		m = PBRRTS_RE2.match(l)
		if m:
                    index, count, routepoint_index = (int(i) for i in m.groups()[0:3])
                    routepoint_short_name = m.group(4)
                    routepoint_long_name = m.group(5)
                    routepoints.append(Routepoint(routepoint_short_name, routepoint_long_name))
                    if routepoint_index == count - 1:
                        yield Route(index, name, routepoints)
		else:
                    raise ProtocolError(m)

    def pbrrts(self):
        return list(self.ipbrrts())

    def pbrsnp(self):
        return SNP(*self.one('PBRSNP,', PBRSNP_RE, 0.2).groups())

    def pbrtl(self):
        tracks = []
        def igc_lambda(self, index):
            return lambda: self.ipbrtr(index)
        for m in self.ieach('PBRTL,', PBRTL_RE, 0.5):
            index = int(m.group(2))
            day, month, year, hour, minute, second = (int(i) for i in m.groups()[2:8])
            hours, minutes, seconds = (int(i) for i in m.groups()[8:11])
            tracks.append(Track(
                count=int(m.group(1)),
                index=index,
                datetime=datetime.datetime(year + 2000, month, day, hour, minute, second, tzinfo=UTC()),
                duration=datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds),
                _igc_lambda=igc_lambda(self, index)))
        return add_igc_filenames(tracks, self.manufacturer, self.serial_number)

    def ipbrtr(self, index):
        return self.ieach('PBRTR,%02d' % index)

    def pbrtr(self, index):
        return list(self.ipbrtr(index))

    def pbrwpr(self, waypoint):
        self.none('PBRWPR,%02d%06.3f,%s,%03d%06.3f,%s,,%-17s,%04d' % (
            abs(60 * waypoint.lat) / 60,
            abs(60 * waypoint.lat) % 60,
            'S' if waypoint.lat < 0 else 'N',
            abs(60 * waypoint.lon) / 60,
            abs(60 * waypoint.lon) % 60,
            'W' if waypoint.lon < 0 else 'E',
            waypoint.name.encode('nmea_characters')[:17],
            waypoint.alt))

    def ipbrwps(self):
        for m in self.ieach('PBRWPS,', PBRWPS_RE):
            lat = int(m.group(1)) + float(m.group(2)) / 60
            if m.group(3) == 'S':
                lat *= -1
            lon = int(m.group(4)) + float(m.group(5)) / 60
            if m.group(6) == 'W':
                lon *= -1
            yield Waypoint(lat=lat, lon=lon, id=m.group(7).rstrip(), name=m.group(8).rstrip(), alt=int(m.group(9)))

    def pbrwps(self):
        return list(self.ipbrwps())

    def pbrwpx(self, name=None):
        if name:
            self.none('PBRWPX,%-17s' % name)
        else:
            self.none('PBRWPX,,', None)

    def to_json(self):
        return {
            'manufacturer': self.manufacturer_name,
            'model': self.model,
            'pilot_name': self.pilot_name,
            'serial_number': self.serial_number,
            'software_version': self.software_version}

    @property
    def manufacturer_name(self):
        return ['Flytec', 'Brauniger'][self.manufacturer]

    @property
    def manufacturer(self):
        return MANUFACTURER[self.snp.model]

    @property
    def model(self):
        return self.snp.model

    @property
    def pilot_name(self):
        return self.snp.pilot_name

    @property
    def serial_number(self):
        return self.snp.serial_number

    @property
    def snp(self):
        if self._snp is None:
            self._snp = self.pbrsnp()
        return self._snp

    @property
    def software_version(self):
        return self.snp.software_version

    def get(self, key):
        if key not in MEMORY_MAP:
            raise NotImplementedError
        address, format, type = MEMORY_MAP[key]
        value = ''.join(chr(byte) for byte in self.pbrmemr(address, struct.calcsize(format)))
        return struct.unpack(format, value)[0]

    def set(self, key, value, first=True, last=True):
        if key not in MEMORY_MAP:
            raise NotImplementedError
        address, format, type = MEMORY_MAP[key]
        m = re.match(r'(\d+)s\Z', format)
        if m:
            width = int(m.group(1))
            value = value[:width].ljust(width)
        self.pbrmemw(address, struct.pack(format, type(value)))
        if last:
            self.pbrconf()

    def tracks(self):
        if self._tracks is None:
            self._tracks = self.pbrtl()
        return self._tracks

    def waypoints(self):
        return self.ipbrwps()

    def waypoints_delete(self, waypoint):
        self.pbrwpx(waypoint.name)

    def waypoints_delete_all(self):
        self.pbrwpx()

    def waypoints_upload(self, waypoints):
        for waypoint in waypoints:
            self.pbrwpr(waypoint)

    def dump(self):
        memory = self.pbrmemr(0, 256)
        tracks = list(track.to_json(True) for track in self.tracks)
        waypoints = list(waypoint.to_json() for waypoint in self.waypoints)
        return dict(memory=memory, tracks=tracks, waypoints=waypoints)