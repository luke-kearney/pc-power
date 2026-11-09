"""Constants for PC Power."""
DOMAIN = "pc_power"
DEFAULT_PORT = 8765

CONF_TOKEN = "token"
CONF_MACHINE_ID = "machine_id"
CONF_DISKS = "disks"

OPT_SCAN_INTERVAL = "scan_interval"
OPT_DISKS_W = "disks_w"
OPT_CONTROLLER_W = "controller_w"
OPT_FANS_W = "fans_w"
OPT_FANS_DYNAMIC = "fans_dynamic"
OPT_RGB_W = "rgb_w"
OPT_BOARD_W = "board_w"
OPT_EFFICIENCY = "efficiency"
OPT_OFFLINE_W = "offline_w"
OPT_GRACE_POLLS = "grace_polls"
OPT_MONITOR_STANDBY_W = "monitor_standby_w"
OPT_USB_OVERRIDES = "usb_overrides"
OPT_MONITOR_OVERRIDES = "monitor_overrides"
USB_FIELD_PREFIX = "usb_"
MONITOR_FIELD_PREFIX = "monitor_"

DEFAULT_SCAN_INTERVAL = 10
DEFAULTS = {
    OPT_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    OPT_CONTROLLER_W: 0.0,
    OPT_FANS_W: 4.5,
    OPT_FANS_DYNAMIC: True,
    OPT_RGB_W: 0.0,
    OPT_BOARD_W: 12.0,
    OPT_EFFICIENCY: 0.85,
    OPT_OFFLINE_W: 0.0,
    OPT_GRACE_POLLS: 3,
    OPT_MONITOR_STANDBY_W: 0.5,
}

WOL_PORT = 9
