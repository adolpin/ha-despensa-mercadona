"""Constants for the Mercadona (Despensa) integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "mercadona"
CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"
SCAN_INTERVAL: Final = timedelta(minutes=3)
EVENT_UNRESOLVED: Final = "mercadona_unresolved"
SERVICE_ADD_PRODUCT: Final = "add_product"
SERVICE_CHOOSE: Final = "choose_candidate"
SERVICE_MORE: Final = "more_candidates"
SOURCE_TODO: Final = "ha_todo"
