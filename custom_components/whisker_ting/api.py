"""API client for Whisker Ting."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from .auth import AuthenticationError, WhiskerAuth
from .const import API_BASE_URL, API_USERS_ENDPOINT

_LOGGER = logging.getLogger(__name__)


@dataclass
class HazardStatus:
    """Represents a hazard status (EFH or UFH)."""

    status: str | None = None
    timestamp_utc: str | None = None
    level: int | None = None
    message: str = "No Hazards Detected"
    hex_color: str = "#00FF00"


@dataclass
class FireHazardStatus:
    """Represents the fire hazard status of a device."""

    learning_mode: bool = False
    message: str = "No Hazards Detected"
    efh_status: HazardStatus = field(default_factory=HazardStatus)
    ufh_status: HazardStatus = field(default_factory=HazardStatus)
    hex_color_light: str = "#00FF00"
    hex_color_medium: str = "#358C15"
    hex_color_dark: str = "#233016"


@dataclass
class VoltageReading:
    """Real-time voltage reading."""

    voltage: float = 0.0
    voltage_hi: float = 0.0
    voltage_lo: float = 0.0
    average_peaks_max: float = 0.0


@dataclass
class DeviceState:
    """Represents the state of a Whisker Ting device."""

    serial_number: str
    name: str
    device_type: str
    site_id: int

    # Device info
    version: str | None = None
    wifi_mac_address: str | None = None
    bluetooth_mac_address: str | None = None
    soc_serial_number: str | None = None
    station_id: str | None = None  # For WebSocket connection

    # Status flags
    is_fire: bool = False
    is_hvac_verified: bool = False
    has_frozen_pipe: bool = False
    is_owner: bool = False

    # Hazard status
    fire_hazard_status: FireHazardStatus = field(default_factory=FireHazardStatus)

    # Real-time voltage (from WebSocket)
    voltage: VoltageReading = field(default_factory=VoltageReading)

    # Group info
    group_name: str | None = None
    group_id: int | None = None


@dataclass
class Site:
    """Represents a site/location."""

    id: int
    user_id: int
    display_name: str
    address_line1: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class UserData:
    """Represents user data from the API."""

    user_id: int
    email: str
    first_name: str
    last_name: str
    phone_number: str | None = None
    devices: list[DeviceState] = field(default_factory=list)
    sites: list[Site] = field(default_factory=list)


class WhiskerApiError(Exception):
    """Base exception for Whisker API errors."""


class WhiskerAuthError(WhiskerApiError):
    """Authentication error."""


class WhiskerConnectionError(WhiskerApiError):
    """Connection error."""


class WhiskerApiClient:
    """Client for the Whisker Ting API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._username = username
        self._password = password
        self._auth = WhiskerAuth(session)

        # Token storage
        self._access_token: str | None = None
        self._refresh_token: str | None = refresh_token
        self._id_token: str | None = None
        self._api_key: str | None = None
        self._user_id: int | None = None
        self._token_expiry: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def user_id(self) -> int | None:
        """Return the user ID."""
        return self._user_id

    @property
    def api_key(self) -> str | None:
        """Return the API key."""
        return self._api_key

    async def _ensure_token(self) -> str:
        """Ensure we have a valid access token."""
        async with self._lock:
            if self._access_token and self._token_expiry:
                # Refresh if token expires in less than 5 minutes
                if datetime.now(UTC) < self._token_expiry - timedelta(minutes=5):
                    return self._access_token

            # Need to authenticate or refresh
            if self._refresh_token:
                try:
                    await self._refresh_access_token()
                    # If user_id wasn't populated from a prior full auth,
                    # fetch user attributes now so the API URL and api_key are correct
                    if not self._user_id:
                        await self._fetch_user_attributes()
                    return self._access_token
                except AuthenticationError:
                    # Refresh failed, try full auth if we have a password
                    if not self._password:
                         raise WhiskerAuthError("Refresh token expired and no password available")
                    pass

            # Full authentication
            if not self._password:
                raise WhiskerAuthError("No password available for authentication")

            await self._authenticate()
            return self._access_token

    async def _authenticate(self) -> None:
        """Perform full authentication."""
        _LOGGER.debug("Performing full authentication")
        try:
            result = await self._auth.authenticate(self._username, self._password)

            self._access_token = result["access_token"]
            self._refresh_token = result["refresh_token"]
            self._id_token = result["id_token"]

            # Use Cognito provided ExpiresIn or default to 1 hour
            expires_in = result.get("ExpiresIn", 3600)
            self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

            # Extract user info from attributes
            user_attrs = {
                attr["Name"]: attr["Value"]
                for attr in result.get("user_attributes", [])
            }
            self._user_id = int(user_attrs.get("custom:user_id", 0))
            self._api_key = user_attrs.get("custom:api_key")

            _LOGGER.debug("Authentication successful, user_id=%s", self._user_id)

        except AuthenticationError as err:
            raise WhiskerAuthError(str(err)) from err

    async def _refresh_access_token(self) -> None:
        """Refresh the access token."""
        _LOGGER.debug("Refreshing access token")
        try:
            result = await self._auth.refresh_tokens(self._refresh_token)

            self._access_token = result["AccessToken"]
            self._id_token = result.get("IdToken", self._id_token)
            
            # Use Cognito provided ExpiresIn or default to 1 hour
            expires_in = result.get("ExpiresIn", 3600)
            self._token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

            _LOGGER.debug("Access token refreshed")

        except AuthenticationError as err:
            raise WhiskerAuthError(str(err)) from err

    async def _fetch_user_attributes(self) -> None:
        """Fetch user attributes from Cognito to populate user_id and api_key.

        Called after a token refresh when user_id and api_key were not populated
        by a prior full authentication (e.g. on a fresh install with only a
        refresh token stored).
        """
        _LOGGER.debug("Fetching user attributes from Cognito")
        try:
            user_info = await self._auth._get_user(self._access_token)
            user_attrs = {
                attr["Name"]: attr["Value"]
                for attr in user_info.get("UserAttributes", [])
            }
            self._user_id = int(user_attrs.get("custom:user_id", 0))
            self._api_key = user_attrs.get("custom:api_key")
            _LOGGER.debug(
                "User attributes fetched, user_id=%s, api_key present: %s",
                self._user_id,
                bool(self._api_key),
            )
        except Exception as err:
            raise WhiskerAuthError(f"Failed to fetch user attributes: {err}") from err

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an authenticated request to the API."""
        token = await self._ensure_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "x-wl-api-key": self._api_key or "",
        }

        url = f"{API_BASE_URL}{endpoint}"

        _LOGGER.debug("Making API request: %s %s (api_key present: %s)", method, url, bool(self._api_key))

        try:
            async with self._session.request(
                method, url, headers=headers, **kwargs
            ) as response:
                if response.status == 401:
                    # Token might have expired, try refreshing once
                    async with self._lock:
                        if self._password:
                            await self._authenticate()
                        elif self._refresh_token:
                            await self._refresh_access_token()
                        else:
                            raise WhiskerAuthError("Authentication failed: No credentials to retry")
                    
                    token = self._access_token
                    headers["Authorization"] = f"Bearer {token}"
                    async with self._session.request(
                        method, url, headers=headers, **kwargs
                    ) as retry_response:
                        if retry_response.status == 401:
                            raise WhiskerAuthError("Authentication failed")
                        retry_response.raise_for_status()
                        return await retry_response.json()

                if response.status != 200:
                    text = await response.text()
                    _LOGGER.debug(
                        "API request failed: method=%s url=%s status=%s body=%s",
                        method, url, response.status, text
                    )
                    raise WhiskerApiError(
                        f"API request failed with status {response.status}: {text}"
                    )

                return await response.json()

        except aiohttp.ClientError as err:
            raise WhiskerConnectionError(f"Connection error: {err}") from err

    async def get_user_data(self) -> UserData:
        """Get user data including devices."""
        if not self._user_id:
            await self._ensure_token()

        endpoint = API_USERS_ENDPOINT.format(user_id=self._user_id)
        data = await self._request("GET", endpoint)

        return self._parse_user_data(data)

    def _parse_user_data(self, data: dict[str, Any]) -> UserData:
        """Parse user data from API response."""
        devices = []
        for device_data in data.get("devices", []):
            device = self._parse_device(device_data)
            devices.append(device)

        sites = []
        for site_data in data.get("sites", []):
            site = Site(
                id=site_data.get("id", 0),
                user_id=site_data.get("userId", 0),
                display_name=site_data.get("displayName", ""),
                address_line1=site_data.get("addressLine1"),
                city=site_data.get("city"),
                state_province=site_data.get("stateProvince"),
                postal_code=site_data.get("postalCode"),
                country=site_data.get("country"),
                latitude=site_data.get("latitude"),
                longitude=site_data.get("longitude"),
            )
            sites.append(site)

        return UserData(
            user_id=data.get("id", 0),
            email=data.get("email", ""),
            first_name=data.get("firstName", ""),
            last_name=data.get("lastName", ""),
            phone_number=data.get("phoneNumber"),
            devices=devices,
            sites=sites,
        )

    def _parse_device(self, data: dict[str, Any]) -> DeviceState:
        """Parse device state from API response."""
        _LOGGER.debug("Raw device data from API: %s", data)
        # Parse fire hazard status
        fhs_data = data.get("fireHazardStatus", {})
        efh_data = fhs_data.get("efhStatus", {})
        ufh_data = fhs_data.get("ufhStatus", {})
        hex_colors = fhs_data.get("hexColor", {})

        efh_status = HazardStatus(
            status=efh_data.get("status"),
            timestamp_utc=efh_data.get("timestampUtc"),
            level=efh_data.get("level"),
            message=efh_data.get("message", "No Hazards Detected"),
            hex_color=efh_data.get("hexColor", "#00FF00"),
        )

        ufh_status = HazardStatus(
            status=ufh_data.get("status"),
            timestamp_utc=ufh_data.get("timestampUtc"),
            level=ufh_data.get("level"),
            message=ufh_data.get("message", "No Hazards Detected"),
            hex_color=ufh_data.get("hexColor", "#00FF00"),
        )

        fire_hazard_status = FireHazardStatus(
            learning_mode=fhs_data.get("learningMode", False),
            message=fhs_data.get("message", "No Hazards Detected"),
            efh_status=efh_status,
            ufh_status=ufh_status,
            hex_color_light=hex_colors.get("light", "#00FF00"),
            hex_color_medium=hex_colors.get("medium", "#358C15"),
            hex_color_dark=hex_colors.get("dark", "#233016"),
        )

        # Parse group info
        group_data = data.get("group", {})

        # Get station_id for WebSocket - the serial number is the correct stream identifier
        station_id = data.get("serialNumber", "")

        return DeviceState(
            serial_number=data.get("serialNumber", ""),
            name=data.get("name", data.get("serialNumber", "")),
            device_type=data.get("type", "Unknown"),
            site_id=data.get("siteId", 0),
            version=data.get("version"),
            wifi_mac_address=data.get("wifiMacAddress"),
            bluetooth_mac_address=data.get("bluetoothMacAddress"),
            soc_serial_number=data.get("socSerialNumber"),
            station_id=station_id,
            is_fire=data.get("isFire", False),
            is_hvac_verified=data.get("isHvacVerified", False),
            has_frozen_pipe=data.get("hasFrozenPipe", False),
            is_owner=data.get("isOwner", False),
            fire_hazard_status=fire_hazard_status,
            group_name=group_data.get("name"),
            group_id=group_data.get("id"),
        )

    async def get_all_device_states(self) -> dict[str, DeviceState]:
        """Get the state of all devices."""
        user_data = await self.get_user_data()
        return {device.serial_number: device for device in user_data.devices}

    async def test_connection(self) -> bool:
        """Test the connection to the API."""
        try:
            await self.get_user_data()
            return True
        except WhiskerApiError:
            return False