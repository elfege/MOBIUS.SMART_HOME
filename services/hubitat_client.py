"""
Hubitat Maker API Client

Handles all communication with Hubitat hub(s):
- Device listing and capability discovery
- Device state queries
- Command execution
- Mode management
- Webhook registration

The Maker API is a RESTful API that provides access to devices, commands,
and events on a Hubitat hub. Each hub has its own Maker API app with a
unique app number and access token.

Endpoints reference:
- GET /apps/api/{app}/devices/all - List all devices
- GET /apps/api/{app}/devices/{id} - Get device details with current state
- GET /apps/api/{app}/devices/{id}/{command} - Execute command on device
- GET /apps/api/{app}/devices/{id}/{command}/{value} - Execute command with parameter
- GET /apps/api/{app}/devices/{id}/events - Get recent events for device
- GET /apps/api/{app}/modes - Get available location modes
- GET /apps/api/{app}/modes/{id} - Set location mode
"""

import os
import logging
import traceback
import requests
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urljoin


@dataclass
class HubitatConfig:
    """
    Hubitat hub connection configuration.

    Attributes:
        hub_ip: IP address of the Hubitat hub (e.g., '<LAN_IP>')
        app_number: Maker API app number (e.g., '268')
        token: Access token for authentication
        name: Human-readable name for this hub (e.g., 'primary', 'hub4')
    """
    hub_ip: str
    app_number: str
    token: str
    name: str = "primary"


class HubitatClient:
    """
    Client for Hubitat Maker API.

    Provides methods for device management, command execution, and mode control.
    Supports caching to reduce API calls and includes error handling with retries.

    Example usage:
        config = HubitatConfig(
            hub_ip='<LAN_IP>',
            app_number='268',
            token='your-token-here'
        )
        client = HubitatClient(config)

        # Get all devices
        devices = client.get_all_devices()

        # Get devices with a specific capability
        motion_sensors = client.get_devices_by_capability('motionSensor')

        # Send command to device
        client.send_command('123', 'on')
        client.send_command('123', 'setLevel', [75])
    """

    # Default timeout for API requests (seconds)
    DEFAULT_TIMEOUT = 10

    # Maximum retries for failed requests
    MAX_RETRIES = 3

    def __init__(self, config: HubitatConfig, cache: Optional[Any] = None):
        """
        Initialize the Hubitat client.

        Args:
            config: HubitatConfig with connection details
            cache: Optional DeviceCache instance for caching device data
        """
        self.config = config
        self.cache = cache
        self.base_url = f"http://{config.hub_ip}/apps/api/{config.app_number}"
        self.logger = logging.getLogger(f"{__name__}.{config.name}")

        # Session for connection pooling
        self._session = requests.Session()

    def _make_request(
        self,
        endpoint: str,
        method: str = 'GET',
        timeout: int = None,
        retries: int = None
    ) -> Optional[Dict[str, Any]]:
        """
        Make an HTTP request to the Hubitat API.

        Args:
            endpoint: API endpoint path (e.g., '/devices/all')
            method: HTTP method (GET, POST)
            timeout: Request timeout in seconds
            retries: Number of retry attempts on failure

        Returns:
            Parsed JSON response or None on failure
        """
        timeout = timeout or self.DEFAULT_TIMEOUT
        retries = retries if retries is not None else self.MAX_RETRIES

        url = f"{self.base_url}{endpoint}"
        params = {"access_token": self.config.token}

        for attempt in range(retries + 1):
            try:
                response = self._session.request(
                    method=method,
                    url=url,
                    params=params,
                    timeout=timeout
                )
                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout:
                self.logger.warning(
                    f"Request timeout (attempt {attempt + 1}/{retries + 1}): {endpoint}",
                    exc_info=True
                )
            except requests.exceptions.ConnectionError as e:
                self.logger.warning(
                    f"Connection error (attempt {attempt + 1}/{retries + 1}): {e}",
                    exc_info=True
                )
            except requests.exceptions.HTTPError as e:
                self.logger.error(f"HTTP error: {e}", exc_info=True)
                return None
            except requests.exceptions.JSONDecodeError:
                self.logger.error(
                    f"Invalid JSON response from: {endpoint}", exc_info=True
                )
                return None

        self.logger.error(f"All retry attempts failed for: {endpoint}")
        return None

    # =========================================================================
    # Device Operations
    # =========================================================================

    def get_all_devices(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        """
        Get all devices from Maker API.

        Args:
            use_cache: Whether to use cached data if available

        Returns:
            List of device dictionaries with id, name, label, type, capabilities
        """
        # Check cache first
        if use_cache and self.cache:
            cached = self.cache.get_all()
            if cached:
                self.logger.debug("Returning cached devices")
                return cached

        # Fetch from API
        devices = self._make_request("/devices/all")

        if devices is None:
            self.logger.error("Failed to fetch devices from Hubitat")
            return []

        # Update cache
        if self.cache:
            self.cache.update_all(devices)

        self.logger.info(f"Fetched {len(devices)} devices from Hubitat")
        return devices

    def get_device(self, device_id: str, use_cache: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get single device details with current attribute values.

        This returns more detail than get_all_devices(), including current
        state of all attributes (e.g., switch: 'on', level: 75).

        Args:
            device_id: Hubitat device ID
            use_cache: Whether to use cached data

        Returns:
            Device dictionary with full details or None if not found
        """
        if use_cache and self.cache:
            cached = self.cache.get_device(device_id)
            if cached:
                return cached

        device = self._make_request(f"/devices/{device_id}")

        if device is None:
            self.logger.warning(f"Device not found: {device_id}")
            return None

        # Update cache with this device's data
        if self.cache:
            self.cache.update_device(device_id, device)

        return device

    def get_devices_by_capability(self, capability: str) -> List[Dict[str, Any]]:
        """
        Filter devices by capability.

        Common capabilities:
        - motionSensor: Motion sensors
        - switch: On/off switches
        - switchLevel: Dimmers (also have switch)
        - colorControl: Color bulbs
        - colorTemperature: Tunable white bulbs
        - contactSensor: Door/window sensors
        - illuminanceMeasurement: Light sensors
        - temperatureMeasurement: Temperature sensors
        - pushableButton: Buttons
        - battery: Devices with battery

        Args:
            capability: Capability name to filter by

        Returns:
            List of devices with the specified capability
        """
        all_devices = self.get_all_devices()

        # Hubitat returns PascalCase capabilities (e.g., 'MotionSensor')
        # but queries may use camelCase (e.g., 'motionSensor').
        # Compare case-insensitively.
        cap_lower = capability.lower()
        return [
            device for device in all_devices
            if any(
                isinstance(c, str) and c.lower() == cap_lower
                for c in device.get('capabilities', [])
            )
        ]

    def get_device_events(
        self,
        device_id: str,
        max_events: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get recent events for a device.

        Useful for checking motion sensor history, switch activity, etc.

        Args:
            device_id: Hubitat device ID
            max_events: Maximum number of events to return

        Returns:
            List of event dictionaries with name, value, date, etc.
        """
        events = self._make_request(f"/devices/{device_id}/events")

        if events is None:
            return []

        # Limit events if needed
        if len(events) > max_events:
            events = events[:max_events]

        return events

    # =========================================================================
    # Command Execution
    # =========================================================================

    def send_command(
        self,
        device_id: str,
        command: str,
        args: Optional[List] = None
    ) -> bool:
        """
        Send command to a device.

        Examples:
            # Turn on a switch
            send_command('123', 'on')

            # Set dimmer level
            send_command('123', 'setLevel', [75])

            # Set color temperature
            send_command('123', 'setColorTemperature', [3000])

            # Set color (HSL)
            send_command('123', 'setColor', [{'hue': 50, 'saturation': 100}])

        Args:
            device_id: Hubitat device ID
            command: Command name (on, off, setLevel, etc.)
            args: Optional list of command arguments

        Returns:
            True if command succeeded, False otherwise
        """
        # Build endpoint path
        endpoint = f"/devices/{device_id}/{command}"

        # Append arguments to path if provided
        if args:
            for arg in args:
                endpoint += f"/{arg}"

        self.logger.debug(f"API → {device_id}/{command}" + (f"/{args}" if args else ""))

        result = self._make_request(endpoint)

        if result is None:
            self.logger.error(f"API FAIL → {device_id}/{command}")
            return False
        return True

    def turn_on(self, device_id: str) -> bool:
        """Turn on a switch or dimmer."""
        return self.send_command(device_id, 'on')

    def turn_off(self, device_id: str) -> bool:
        """Turn off a switch or dimmer."""
        return self.send_command(device_id, 'off')

    def set_level(self, device_id: str, level: int, duration: int = None) -> bool:
        """
        Set dimmer level.

        Args:
            device_id: Device ID
            level: Level 0-100
            duration: Optional transition duration in seconds
        """
        args = [level]
        if duration is not None:
            args.append(duration)
        return self.send_command(device_id, 'setLevel', args)

    def set_color_temperature(self, device_id: str, kelvin: int) -> bool:
        """
        Set color temperature (for tunable white bulbs).

        Args:
            device_id: Device ID
            kelvin: Color temperature in Kelvin (typically 2000-6500)
        """
        return self.send_command(device_id, 'setColorTemperature', [kelvin])

    def set_color(self, device_id: str, hue: int, saturation: int) -> bool:
        """
        Set color (for RGB bulbs).

        Args:
            device_id: Device ID
            hue: Hue value 0-100
            saturation: Saturation value 0-100
        """
        # Hubitat expects a map with hue and saturation
        return self.send_command(device_id, 'setColor', [
            f"{{'hue':{hue},'saturation':{saturation}}}"
        ])

    # =========================================================================
    # Mode Operations
    # =========================================================================

    def get_modes(self) -> List[Dict[str, Any]]:
        """
        Get available location modes.

        Returns:
            List of mode dictionaries with id, name, active status
        """
        modes = self._make_request("/modes")
        return modes if modes else []

    def get_current_mode(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Get current location mode.

        Returns:
            Tuple of (mode_id, mode_name) or (None, None) if not found
        """
        modes = self.get_modes()

        for mode in modes:
            if mode.get('active'):
                return mode.get('id'), mode.get('name')

        return None, None

    def set_mode(self, mode_id: str) -> bool:
        """
        Set location mode.

        Args:
            mode_id: Mode ID to activate

        Returns:
            True if mode change succeeded
        """
        result = self._make_request(f"/modes/{mode_id}")
        return result is not None

    # =========================================================================
    # Hub Information
    # =========================================================================

    def get_hub_info(self) -> Optional[Dict[str, Any]]:
        """
        Get hub information (firmware version, etc.).

        Note: This endpoint may not be available on all Maker API versions.
        """
        return self._make_request("/")

    def is_connected(self) -> bool:
        """
        Check if the hub is reachable.

        Returns:
            True if hub responds to API requests
        """
        try:
            response = self._session.get(
                f"{self.base_url}/devices/all",
                params={"access_token": self.config.token},
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            self.logger.error(f"Hub connectivity check failed: {e}", exc_info=True)
            return False

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Close the HTTP session."""
        self._session.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()


def get_default_client() -> HubitatClient:
    """
    Create a HubitatClient with configuration from environment variables.

    Expected env vars:
    - HUBITAT_HUB_IP_MAIN (defaults to <LAN_IP>)
    - HUBITAT_API_NUMBER_MAIN (defaults to 268)
    - HUBITAT_API_TOKEN_MAIN (required)

    Returns:
        Configured HubitatClient instance

    Raises:
        ValueError: If HUBITAT_API_TOKEN_MAIN is not set
    """
    token = os.environ.get('HUBITAT_API_TOKEN_MAIN')
    if not token:
        raise ValueError("HUBITAT_API_TOKEN_MAIN environment variable is required")

    config = HubitatConfig(
        hub_ip=os.environ.get('HUBITAT_HUB_IP_MAIN', '<LAN_IP>'),
        app_number=os.environ.get('HUBITAT_API_NUMBER_MAIN', '268'),
        token=token,
        name='primary'
    )

    return HubitatClient(config)


# =========================================================================
# Multi-Hub Client Registry
# =========================================================================
# Maintains a pool of HubitatClient instances, one per configured hub.
# Used by DeviceCommander for native-hub command routing.

import threading

_hub_clients: Dict[str, HubitatClient] = {}
_hub_clients_lock = threading.Lock()

# Map hub names to env var suffixes
_HUB_ENV_MAP = {
    "MAIN": "MAIN",
    "Home 1": "OTHER_HUB_1",
    "Home 2": "OTHER_HUB_2",
    "Home 3": "OTHER_HUB_3",
}


def get_hub_client(hub_name: str) -> Optional[HubitatClient]:
    """
    Get or create a HubitatClient for a specific hub.

    Thread-safe: uses a lock for lazy initialization.
    Clients are cached and reused (connection pooling via requests.Session).

    Args:
        hub_name: Hub name as used in device_hub_mapping
                  (e.g., 'MAIN', 'Home 1', 'Home 2', 'Home 3')

    Returns:
        HubitatClient for the specified hub, or None if not configured
    """
    # Fast path: already initialized
    client = _hub_clients.get(hub_name)
    if client is not None:
        return client

    with _hub_clients_lock:
        # Double-check after acquiring lock
        client = _hub_clients.get(hub_name)
        if client is not None:
            return client

        suffix = _HUB_ENV_MAP.get(hub_name)
        if not suffix:
            logging.getLogger(__name__).warning(
                f"Unknown hub name: {hub_name}"
            )
            return None

        ip = os.environ.get(f"HUBITAT_HUB_IP_{suffix}")
        app_num = os.environ.get(f"HUBITAT_API_NUMBER_{suffix}")
        token = os.environ.get(f"HUBITAT_API_TOKEN_{suffix}")

        if not all([ip, app_num, token]):
            logging.getLogger(__name__).warning(
                f"Missing env vars for hub {hub_name} "
                f"(HUBITAT_*_{suffix})"
            )
            return None

        config = HubitatConfig(
            hub_ip=ip,
            app_number=app_num,
            token=token,
            name=hub_name,
        )
        new_client = HubitatClient(config)
        _hub_clients[hub_name] = new_client
        logging.getLogger(__name__).info(
            f"Created HubitatClient for hub {hub_name} ({ip})"
        )
        return new_client


def get_hub_client_by_ip(hub_ip: str) -> Optional[HubitatClient]:
    """
    Get a HubitatClient by hub IP address.

    Looks up the hub name from the env var map, then delegates to
    get_hub_client().

    Args:
        hub_ip: Hub IP address (e.g., '<LAN_IP>')

    Returns:
        HubitatClient for that hub, or None
    """
    for hub_name, suffix in _HUB_ENV_MAP.items():
        env_ip = os.environ.get(f"HUBITAT_HUB_IP_{suffix}")
        if env_ip == hub_ip:
            return get_hub_client(hub_name)
    return None


def get_all_hub_clients() -> Dict[str, HubitatClient]:
    """
    Get clients for all configured hubs.

    Returns:
        Dict mapping hub_name → HubitatClient (only hubs with valid config)
    """
    clients = {}
    for hub_name in _HUB_ENV_MAP:
        client = get_hub_client(hub_name)
        if client is not None:
            clients[hub_name] = client
    return clients
