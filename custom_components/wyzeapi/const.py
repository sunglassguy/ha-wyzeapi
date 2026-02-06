"""Constants for the Wyze Home Assistant Integration integration."""

DOMAIN = "wyzeapi"
CONF_CLIENT = "wyzeapi_client"

ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
REFRESH_TIME = "refresh_time"
KEY_ID = "key_id"
API_KEY = "api_key"

WYZE_NOTIFICATION_TOGGLE = f"{DOMAIN}.wyze.notification.toggle"

# Event dispatcher signals
CAMERA_UPDATED = f"{DOMAIN}.camera_updated"
WYZE_CAMERA_EVENT = "wyze_camera_event"

# Motion detection config (KEEP THESE)
CONF_ENABLE_CAMERA_MOTION = "enable_camera_motion"
CONF_MOTION_POLL_INTERVAL = "motion_poll_interval"
CONF_MOTION_HOLD_SECONDS = "motion_hold_seconds"
CONF_MOTION_TRACKING_DEVICES = "motion_tracking_devices"
