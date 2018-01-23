
from fibre.discovery import find_any, find_all
from fibre.udp_transport import open_udp
try:
    from fibre.usbbulk_transport import open_usb
except ModuleNotFoundError:
    pass
try:
    from fibre.serial_transport import open_serial
except ModuleNotFoundError:
    pass
