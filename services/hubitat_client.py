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
                    f"Request timeout (attempt {attempt + 1}/{retries + 1}): {endpoint}"
                )
            except requests.exceptions.ConnectionError as e:
                self.logger.warning(
                    f"Connection error (attempt {attempt + 1}/{retries + 1}): {e}"
                )
            except requests.exceptions.HTTPError as e:
                self.logger.error(f"HTTP error: {e}")
                return None
            except requests.exceptions.JSONDecodeError:
                self.logger.error(f"Invalid JSON response from: {endpoint}")
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

        return [
            device for device in all_devices
            if capability in device.get('capabilities', [])
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

        self.logger.info(f"Sending command: device={device_id}, cmd={command}, args={args}")

        result = self._make_request(endpoint)

        if result is None:
            self.logger.error(f"Command failed: {device_id}/{command}")
            return False

        self.logger.debug(f"Command response: {result}")
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
        except Exception:
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
    - MAIN_HUB_IP or defaults to <LAN_IP>
    - MAIN_HUB_APP or defaults to 268
    - TOKEN_HUB_4 (required)

    Returns:
        Configured HubitatClient instance

    Raises:
        ValueError: If TOKEN_HUB_4 is not set
    """
    token = os.environ.get('TOKEN_HUB_4')
    if not token:
        raise ValueError("TOKEN_HUB_4 environment variable is required")

    config = HubitatConfig(
        hub_ip=os.environ.get('MAIN_HUB_IP', '<LAN_IP>'),
        app_number=os.environ.get('MAIN_HUB_APP', '268'),
        token=token,
        name='primary'
    )

    return HubitatClient(config)
