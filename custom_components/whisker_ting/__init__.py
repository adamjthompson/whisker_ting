"""The Whisker Ting integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WhiskerApiClient, WhiskerAuthError, WhiskerConnectionError
from .const import (
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import WhiskerDataUpdateCoordinator

if TYPE_CHECKING:
    from typing import TypeAlias

    WhiskerConfigEntry: TypeAlias = ConfigEntry[WhiskerDataUpdateCoordinator]
else:
    WhiskerConfigEntry = ConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Whisker Ting from a config entry."""
    username = entry.data[CONF_USERNAME]
    # Use .get() so it doesn't crash on new entries that only have a refresh token
    password = entry.data.get(CONF_PASSWORD)
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    session = async_get_clientsession(hass)
    
    # Initialize client with whatever credentials we have
    client = WhiskerApiClient(
        session, 
        username=username, 
        password=password, 
        refresh_token=refresh_token
    )

    # Test the connection (this will perform auth or token refresh)
    try:
        if not await client.test_connection():
            raise ConfigEntryNotReady("Failed to connect to Whisker Ting API")
    except WhiskerAuthError as err:
        raise ConfigEntryAuthFailed("Invalid authentication") from err
    except WhiskerConnectionError as err:
        raise ConfigEntryNotReady(f"Connection error: {err}") from err

    # MIGRATION: If a plaintext password is still in the config entry, remove it
    # and store the refresh token we just obtained/validated.
    if CONF_PASSWORD in entry.data:
        _LOGGER.info("Migrating Whisker Ting config entry to remove plaintext password")
        new_data = {**entry.data}
        new_data[CONF_REFRESH_TOKEN] = client._refresh_token
        new_data.pop(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=new_data)

    # Create the coordinator
    coordinator = WhiskerDataUpdateCoordinator(hass, client, session, scan_interval)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store the coordinator
    entry.runtime_data = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(async_options_updated))

    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: WhiskerDataUpdateCoordinator = entry.runtime_data
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator.update_interval = timedelta(seconds=scan_interval)
    _LOGGER.debug("Updated scan interval to %s seconds", scan_interval)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Shutdown the coordinator (disconnects WebSocket)
    coordinator: WhiskerDataUpdateCoordinator = entry.runtime_data
    await coordinator.async_shutdown()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)