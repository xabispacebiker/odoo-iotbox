# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from collections import namedtuple
import logging
import re
import serial
from functools import reduce

from odoo import http
from odoo.addons.hw_drivers.controllers.proxy import proxy_drivers
from odoo.addons.hw_drivers.iot_handlers.drivers.SerialScaleDriver import ScaleProtocol, ScaleDriver
from odoo.addons.hw_drivers.iot_handlers.drivers.SerialBaseDriver import serial_connection

_logger = logging.getLogger(__name__)

ACTIVE_SCALE = None

TisaProtocol = ScaleProtocol(
    name='Tisa Scale',
    baudrate=9600,
    bytesize=serial.EIGHTBITS,
    stopbits=serial.STOPBITS_ONE,
    parity=serial.PARITY_NONE,
    timeout=0.1,
    writeTimeout=0.1,
    measureRegexp=b"\x39\x39([01])([0-9]{5})([01])[0-9A-Fa-f]{6}",
    # LABEL format 3 + KG in the scale settings, but Label 1/2 should work
    statusRegexp=None,
    commandTerminator=b"\r\n",
    commandDelay=0.1,
    measureDelay=0.1,
    # AZExtra beeps every time you ask for a weight that was previously returned!
    # Adding an extra delay gives the operator a chance to remove the products
    # before the scale starts beeping. Could not find a way to disable the beeps.
    newMeasureDelay=0,
    measureCommand=b'98000001\r\n',
    zeroCommand=None,
    tareCommand=None,
    clearCommand=None,  # No clear command -> Tare again
    emptyAnswerValid=True,  # AZExtra does not answer unless a new non-zero weight has been detected
    autoResetWeight=True,  # AZExtra will not return 0 after removing products
)


class ScaleReadOldRoute(http.Controller):
    @http.route('/hw_proxy/scale_read', type='json', auth='none', cors='*')
    def scale_read(self):
        if ACTIVE_SCALE:
            return {'weight': ACTIVE_SCALE._scale_read_old_route()}
        return None


class TisaDriver(ScaleDriver):
    """Driver for the Tisa scale."""

    _protocol = TisaProtocol

    def __init__(self, identifier, device):
        super(TisaDriver, self).__init__(identifier, device)
        self._is_reading = False
        self.device_manufacturer = 'Tisa'

        # Ensures compatibility with older versions of Odoo
        # Only the last scale connected is kept
        global ACTIVE_SCALE
        ACTIVE_SCALE = self
        proxy_drivers['scale'] = ACTIVE_SCALE

    # Ensures compatibility with older versions of Odoo
    def _scale_read_old_route(self):
        """Used when the iot app is not installed"""

        with self._device_lock:
            self._read_weight()
        return self.data['value']

    @classmethod
    def supported(cls, device):
        """Checks whether the device at `device` is supported by the driver.

        :param device: path to the device
        :type device: str
        :return: whether the device is supported by the driver
        :rtype: bool
        """

        protocol = cls._protocol

        try:
            with serial_connection(device['identifier'], protocol, is_probing=True) as connection:
                connection.write(protocol.measureCommand + protocol.commandTerminator)
                response = connection.read(17)
                # Checking if the response matches the expected pattern
                if (
                        response and response.startswith(b'\x39\x39')
                ):
                    return True
        except serial.serialutil.SerialTimeoutException:
            pass
        except Exception:
            _logger.exception('Error while probing %s with protocol %s' % (device, protocol.name))
        return False

    def _read_weight(self):
        """Asks for a new weight from the scale, checks if it is valid and, if it is, makes it the current value."""

        price = '00000'
        request = b'\x39\x38' + price.encode('utf-8')  # Adjust 'PPPPP' based on your actual price
        checksum = reduce(lambda x, y: x ^ y, request)  # Calculate XOR checksum
        request += bytes([checksum])
        request += b'\x0D\x0A'

        # Sending the request to the cash register
        self._connection.write(request)

        # Reading the response from the scale
        answer = self._connection.read(17)
        match = re.search(self._protocol.measureRegexp, answer)
        if match:
            # Extract captured data
            state_weight = int(match.group(1))
            if state_weight == 1:
                return False

            match_group_2 = match.group(2)
            weight = float(match.group(2))/1000  # Convert the hexadecimal value to float

            self.data = {
                'value': weight,
                'status': self._status
            }

