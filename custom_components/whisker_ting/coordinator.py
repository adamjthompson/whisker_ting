"""Data coordinator for Whisker Ting."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceState, VoltageReading, WhiskerApiClient, WhiskerApiError, WhiskerAuthError
from .const import CONF_STATION_IDS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .websocket import VoltageData, WhiskerWebSocketManager

_LOGGER = logging.getLogger(__name__)

# Station ID candidates to probe, in order of likelihood.
# The first one that results in voltage data being received is persisted.
_STATION_ID_CANDIDATES = [
    lambda d: d.serial_number,
    lambda d: str(d.site_id) if d.site_id else None,
    lambda d: d.soc_serial_number,
    lambda d: str(d.group_id) if d.group_id else None,
]

# How long to wait for voltage data after sending InitializeStreaming
# before giving up and trying the next candidate
_PROBE_TIMEOUT = 35.0  # seconds


class WhiskerDataUpdateCoordinator(DataUpdateCoordinator[dict[str, DeviceState]]):
    """Class to manage fetching Whisker Ting data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WhiskerApiClient,
        session: aiohttp.ClientSession,
        entry: ConfigEntry,
        update_interval_seconds: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.client = client
        self._session = session
        self._entry = entry
        self._last_update_success: bool | None = None
        self._ws_manager: WhiskerWebSocketManager | None = None
        self._ws_connected = False
        # Discovered station_ids keyed by device serial number, loaded from
        # config entry options so successful probes persist across restarts
        self._discovered_station_ids: dict[str, str] = dict(
            entry.options.get(CONF_STATION_IDS, {})
        )

    @callback
    def _handle_voltage_update(self, station_id: str, voltage_data: VoltageData) -> None:
        """Handle real-time voltage update from WebSocket.

        Only triggers a state update when at least one voltage value changes
        by more than VOLTAGE_CHANGE_THRESHOLD volts, to avoid writing a new
        HA state (and recorder entry) on every WebSocket message.
        """
        if self.data is None:
            return

        VOLTAGE_CHANGE_THRESHOLD = 0.01  # Volts

        def _changed(new: float, old: float) -> bool:
            """Return True if the value changed beyond the threshold."""
            return abs((new or 0.0) - (old or 0.0)) > VOLTAGE_CHANGE_THRESHOLD

        # Find the device with this station_id
        for device_id, device_state in self.data.items():
            if device_state.station_id == station_id:
                existing = device_state.voltage

                # Only push a state update if something meaningful changed
                if not (
                    _changed(voltage_data.voltage, existing.voltage)
                    or _changed(voltage_data.voltage_hi, existing.voltage_hi)
                    or _changed(voltage_data.voltage_lo, existing.voltage_lo)
                    or _changed(voltage_data.average_peaks_max, existing.average_peaks_max)
                ):
                    return  # Nothing changed beyond threshold — skip update

                device_state.voltage = VoltageReading(
                    voltage=voltage_data.voltage,
                    voltage_hi=voltage_data.voltage_hi,
                    voltage_lo=voltage_data.voltage_lo,
                    average_peaks_max=voltage_data.average_peaks_max,
                )
                # Trigger an update for listeners
                self.async_set_updated_data(self.data)
                break

    async def _probe_station_id(
        self,
        device_state: DeviceState,
        get_stream_token,
    ) -> str | None:
        """Try each candidate station_id until one produces voltage data.

        Returns the working station_id, or None if none of the candidates work.
        The result is persisted to config entry options so future restarts skip
        the probe and connect directly with the known-good value.
        """
        for candidate_fn in _STATION_ID_CANDIDATES:
            candidate = candidate_fn(device_state)
            if not candidate:
                continue

            _LOGGER.debug(
                "Probing station_id candidate '%s' for device %s",
                candidate,
                device_state.serial_number,
            )

            connected = await self._ws_manager.connect_device(
                get_stream_token=get_stream_token,
                user_id=self.client.user_id,
                station_id=candidate,
            )

            if not connected:
                _LOGGER.debug("Could not connect with station_id '%s', trying next", candidate)
                continue

            # Wait up to _PROBE_TIMEOUT seconds for voltage data to arrive
            received = await self._ws_manager.wait_for_data(candidate, timeout=_PROBE_TIMEOUT)

            if received:
                _LOGGER.info(
                    "Discovered working station_id '%s' for device %s",
                    candidate,
                    device_state.serial_number,
                )
                # Persist so future restarts skip probing
                self._discovered_station_ids[device_state.serial_number] = candidate
                new_options = {
                    **self._entry.options,
                    CONF_STATION_IDS: self._discovered_station_ids,
                }
                self.hass.config_entries.async_update_entry(
                    self._entry, options=new_options
                )
                return candidate

            # No data — disconnect and try next candidate
            _LOGGER.debug(
                "No data received for station_id '%s' within %.0fs, trying next",
                candidate,
                _PROBE_TIMEOUT,
            )
            await self._ws_manager.disconnect_device(candidate)

        _LOGGER.warning(
            "Could not find a working station_id for device %s — "
            "voltage sensors will be unavailable",
            device_state.serial_number,
        )
        return None

    async def _connect_websocket(self, data: dict[str, DeviceState]) -> None:
        """Connect to WebSocket for real-time updates.

        Uses a previously discovered station_id if available, otherwise probes
        each candidate in order until one produces voltage data.
        """
        if self._ws_manager is None:
            self._ws_manager = WhiskerWebSocketManager(
                session=self._session,
                on_voltage_update=self._handle_voltage_update,
            )

        if not data or self._ws_connected:
            return

        user_id = self.client.user_id

        if not user_id:
            _LOGGER.debug("No user_id available, skipping WebSocket connection")
            return

        async def get_stream_token() -> str:
            await self.client._ensure_token()
            api_key = self.client.api_key
            if not api_key:
                raise ValueError("api_key is not available for WebSocket stream auth")
            _LOGGER.debug("Using api_key for WebSocket stream auth (length=%d)", len(api_key))
            return api_key

        for device_id, device_state in data.items():
            # Use previously discovered station_id if we have one
            known_station_id = self._discovered_station_ids.get(device_state.serial_number)

            if known_station_id:
                _LOGGER.debug(
                    "Using known station_id '%s' for device %s",
                    known_station_id,
                    device_state.serial_number,
                )
                try:
                    connected = await self._ws_manager.connect_device(
                        get_stream_token=get_stream_token,
                        user_id=user_id,
                        station_id=known_station_id,
                    )
                    if connected:
                        _LOGGER.info(
                            "Connected to WebSocket for device %s (station %s)",
                            device_id,
                            known_station_id,
                        )
                        self._ws_connected = True
                        device_state.station_id = known_station_id
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to connect WebSocket for device %s: %s", device_id, err
                    )
            else:
                # No known station_id — probe candidates to find one that works
                _LOGGER.info(
                    "No known station_id for device %s, probing candidates",
                    device_state.serial_number,
                )
                station_id = await self._probe_station_id(device_state, get_stream_token)
                if station_id:
                    self._ws_connected = True
                    device_state.station_id = station_id

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        if self._ws_manager:
            await self._ws_manager.disconnect_all()
            self._ws_connected = False
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, DeviceState]:
        """Fetch data from the API."""
        try:
            data = await self.client.get_all_device_states()

            # Preserve existing voltage data from WebSocket
            if self.data:
                for device_id, device_state in data.items():
                    existing = self.data.get(device_id)
                    if existing and existing.voltage.voltage > 0:
                        device_state.voltage = existing.voltage

            if self._last_update_success is False:
                _LOGGER.info("Connection to Whisker Ting API restored")
            self._last_update_success = True

            # Connect WebSocket on first fetch
            if not self._ws_connected:
                await self._connect_websocket(data)
                # Preserve any voltage data received during probing
                if self._ws_connected and self._ws_manager:
                    for device_id, device_state in data.items():
                        if device_state.station_id:
                            voltage_data = self._ws_manager.get_voltage_data(
                                device_state.station_id
                            )
                            if voltage_data:
                                device_state.voltage = VoltageReading(
                                    voltage=voltage_data.voltage,
                                    voltage_hi=voltage_data.voltage_hi,
                                    voltage_lo=voltage_data.voltage_lo,
                                    average_peaks_max=voltage_data.average_peaks_max,
                                )

            return data
        except WhiskerAuthError as err:
            self._last_update_success = False
            raise ConfigEntryAuthFailed(
                "Authentication failed - credentials may have changed"
            ) from err
        except WhiskerApiError as err:
            if self._last_update_success is not False:
                _LOGGER.warning("Unable to connect to Whisker Ting API: %s", err)
            self._last_update_success = False
            raise UpdateFailed(
                f"Error communicating with Whisker Ting API: {err}"
            ) from err