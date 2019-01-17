from oslo_config import types
from oslo_log import log
import six

from hpedockerplugin import exception

LOG = log.getLogger(__name__)


class FPG(types.String, types.IPAddress):
    """FPG type.
    Used to represent multiple pools per backend values.
    Converts configuration value to an FPGs value.
    FPGs value format::
        FPG name, IP address 1, IP address 2, ..., IP address 4
    where FPG name is a string value,
    IP address is of type types.IPAddress
    Optionally doing range checking.
    If value is whitespace or empty string will raise error
    :param min_ip: Optional check that number of min IP address of VFS.
    :param max_ip: Optional check that number of max IP address of VFS.
    :param type_name: Type name to be used in the sample config file.
    """

    MAX_SUPPORTED_IP_PER_VFS = 4

    def __init__(self, type_name='FPG'):
        types.String.__init__(self, type_name=type_name)
        types.IPAddress.__init__(self, type_name=type_name)

    def __call__(self, value):
        if value is None or value.strip(' ') is '':
            message = ("ERROR: Invalid configuration. 'hpe3par_fpg' must be "
                       "set in the format 'FPG-name:IP:address. Check help "
                       "for usage")
            LOG.error(message)
            raise exception.InvalidInput(err=message)

        values = value.split(",")

        if len(values) != 2:
            msg = "ERROR: Require 'fpg' entry in format " \
                  "'FPG-name,IP-address'. Specified value: '%s'. " \
                  "Check help for usage." \
                  % value
            raise exception.InvalidInput(msg)

        fpg_name = values[0]
        ip_addr = types.String.__call__(self, values[1].strip())
        # Validate if the IP address is good
        try:
            types.IPAddress.__call__(self, ip_addr)
        except ValueError as val_err:
            msg = "ERROR: Invalid IP address specified: %s" % ip_addr
            raise exception.InvalidInput(msg)

        fpg = {fpg_name: ip_addr}
        return fpg

    def __repr__(self):
        return 'FPG'

    def _formatter(self, value):
        return six.text_type(value)
