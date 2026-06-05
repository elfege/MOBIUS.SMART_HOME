"""
Matter Protocol Client Service

WebSocket client for the python-matter-server (port 5580).
Provides device commissioning, command execution, and state monitoring
for Matter-compatible devices.

The matter-server runs as a Docker container with host networking and
exposes a WebSocket API. This client connects to it and translates
Hubitat-style commands (on, off, setLevel, setColorTemperature) into
Matter cluster commands.

Architecture:
    FastAPI App → MatterClient (WebSocket) → matter-server → Matter Device

Usage:
    from services.matter_client import get_matter_client
    client = get_matter_client()
    await client.send_command(node_id=1, endpoint_id=1, cluster_id=6, command="On")
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import websockets
import requests

from services.supervised_tasks import supervised_spawn

logger = logging.getLogger(__name__)

# =============================================================================
# Matter Cluster Constants (from Matter spec)
# =============================================================================

# Cluster IDs for lighting devices
CLUSTER_ON_OFF = 6
CLUSTER_LEVEL_CONTROL = 8
CLUSTER_COLOR_CONTROL = 768

# Hubitat command → Matter cluster/command translation
HUBITAT_TO_MATTER_MAP = {
    "on": {
        "cluster_id": CLUSTER_ON_OFF,
        "command": "On",
        "payload": {}
    },
    "off": {
        "cluster_id": CLUSTER_ON_OFF,
        "command": "Off",
        "payload": {}
    },
    "toggle": {
        "cluster_id": CLUSTER_ON_OFF,
        "command": "Toggle",
        "payload": {}
    },
}


def translate_hubitat_to_matter(
    command: str,
    args: Optional[List] = None
) -> Optional[Dict[str, Any]]:
    """
    Translate a Hubitat command into a Matter cluster command.

    Handles on/off, setLevel (0-100% → 0-254), setColorTemperature
    (Kelvin → mireds), and setColor (Hubitat HSL → Matter HS).

    Args:
        command: Hubitat command name (e.g., 'on', 'setLevel', 'setColorTemperature')
        args: Optional Hubitat command arguments

    Returns:
        Dict with cluster_id, command, payload — or None if untranslatable
    """
    # Simple commands (no args)
    if command in HUBITAT_TO_MATTER_MAP:
        return HUBITAT_TO_MATTER_MAP[command].copy()

    # setLevel: Hubitat 0-100% → Matter 0-254
    if command == "setLevel" and args:
        level_pct = int(args[0]) if args else 0
        matter_level = min(254, max(0, int(level_pct * 2.54)))
        return {
            "cluster_id": CLUSTER_LEVEL_CONTROL,
            "command": "MoveToLevel",
            "payload": {
                "level": matter_level,
                "transitionTime": 10,  # 1 second (in 100ms units)
                "optionsMask": 0,
                "optionsOverride": 0
            }
        }

    # setColorTemperature: Kelvin → mireds (1,000,000 / K)
    if command == "setColorTemperature" and args:
        kelvin = int(args[0]) if args else 3000
        mireds = max(153, min(500, int(1_000_000 / kelvin)))  # Clamp to typical range
        return {
            "cluster_id": CLUSTER_COLOR_CONTROL,
            "command": "MoveToColorTemperature",
            "payload": {
                "colorTemperatureMireds": mireds,
                "transitionTime": 10,
                "optionsMask": 0,
                "optionsOverride": 0
            }
        }

    # setColor: Hubitat {hue: 0-100, saturation: 0-100} → Matter hue 0-254, sat 0-254
    if command == "setColor" and args:
        color_map = args[0] if isinstance(args[0], dict) else {}
        hue = int(color_map.get("hue", 0))
        sat = int(color_map.get("saturation", 100))
        matter_hue = min(254, int(hue * 2.54))
        matter_sat = min(254, int(sat * 2.54))
        return {
            "cluster_id": CLUSTER_COLOR_CONTROL,
            "command": "MoveToHueAndSaturation",
            "payload": {
                "hue": matter_hue,
                "saturation": matter_sat,
                "transitionTime": 10,
                "optionsMask": 0,
                "optionsOverride": 0
            }
        }

    logger.debug(f"No Matter translation for Hubitat command: {command}")
    return None


class MatterClient:
    """
    WebSocket client for the python-matter-server.

    Connects to the matter-server's WebSocket API to commission devices,
    send commands, and receive state change events.

    The client maintains a persistent WebSocket connection and handles
    reconnection on failure.
    """

    def __init__(self, url: str = None):
        """
        Initialize the Matter client.

        Args:
            url: WebSocket URL for the matter-server.
                 Default: ws://localhost:{MATTER_PORT}/ws
        """
        port = os.environ.get("MATTER_PORT", "5580")
        # matter-server uses host networking, so connect via host IP
        server_ip = os.environ.get("SERVER_IP", "<LAN_IP>")
        self.url = url or f"ws://{server_ip}:{port}/ws"
        self._ws = None
        self._message_id = 0
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._event_callbacks: List[Callable] = []
        self._listen_task = None
        self._connected = False
        self._nodes_cache: Dict[int, Dict] = {}

    # =========================================================================
    # Connection Management
    # =========================================================================

    async def connect(self) -> bool:
        """
        Connect to the matter-server WebSocket API.

        Returns:
            True if connected successfully
        """
        try:
            self._ws = await websockets.connect(
                self.url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5
            )
            self._connected = True
            # Supervised: matter-server WS listen loop, long-lived. A
            # crash (typical cause: matter-server SDK upstream change,
            # WS close in the middle of a frame) used to silently kill
            # the listener and matter integration would stop responding.
            # Now logs ERROR with the task name.
            self._listen_task = supervised_spawn(
                self._listen_loop(), name="matter_listen_loop"
            )
            logger.info(f"Connected to matter-server at {self.url}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to matter-server at {self.url}: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from the matter-server."""
        self._connected = False
        if self._listen_task:
            self._listen_task.cancel()
            self._listen_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected from matter-server")

    @property
    def is_connected(self) -> bool:
        """Check if connected to matter-server."""
        return self._connected and self._ws is not None

    async def _ensure_connected(self) -> bool:
        """Ensure we have an active connection, reconnecting if needed."""
        if not self.is_connected:
            return await self.connect()
        return True

    # =========================================================================
    # WebSocket Communication
    # =========================================================================

    async def _send_command(
        self,
        command: str,
        args: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Send a command to the matter-server and wait for a response.

        Args:
            command: matter-server command name
            args: Command arguments

        Returns:
            Response data from the server

        Raises:
            ConnectionError: If not connected
            TimeoutError: If response times out
        """
        if not await self._ensure_connected():
            raise ConnectionError("Cannot connect to matter-server")

        self._message_id += 1
        msg_id = str(self._message_id)

        message = {
            "message_id": msg_id,
            "command": command,
        }
        if args:
            message["args"] = args

        # Create a future for the response
        future = asyncio.get_event_loop().create_future()
        self._pending_responses[msg_id] = future

        try:
            await self._ws.send(json.dumps(message))
            # Wait for response with timeout
            # Commissioning can take 60-90s per device over network
            result = await asyncio.wait_for(future, timeout=120)
            return result
        except asyncio.TimeoutError:
            self._pending_responses.pop(msg_id, None)
            raise TimeoutError(f"Timed out waiting for response to {command}")
        except Exception as e:
            self._pending_responses.pop(msg_id, None)
            raise

    async def _listen_loop(self) -> None:
        """
        Background task that listens for WebSocket messages.

        Routes responses to pending futures and dispatches events
        to registered callbacks.
        """
        try:
            async for raw_message in self._ws:
                try:
                    message = json.loads(raw_message)
                    msg_id = message.get("message_id")

                    # If this is a response to a pending command
                    if msg_id and msg_id in self._pending_responses:
                        future = self._pending_responses.pop(msg_id)
                        if not future.done():
                            if "error_code" in message:
                                future.set_exception(
                                    RuntimeError(
                                        f"Matter error {message['error_code']}: "
                                        f"{message.get('details', 'Unknown')}"
                                    )
                                )
                            else:
                                future.set_result(message.get("result", {}))

                    # If this is an event (from start_listening subscription)
                    elif "event" in message:
                        for callback in self._event_callbacks:
                            try:
                                callback(message["event"])
                            except Exception as e:
                                logger.error(f"Event callback error: {e}")

                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON message from matter-server: {raw_message[:100]}")
                except Exception as e:
                    logger.error(f"Error processing matter-server message: {e}")

        except websockets.exceptions.ConnectionClosed:
            logger.warning("matter-server WebSocket connection closed")
            self._connected = False
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"matter-server listen loop error: {e}")
            self._connected = False

    # =========================================================================
    # Device Discovery & Commissioning
    # =========================================================================

    async def get_nodes(self) -> List[Dict[str, Any]]:
        """
        Get all commissioned Matter nodes.

        Returns:
            List of node dictionaries with id, name, endpoints, attributes
        """
        result = await self._send_command("get_nodes")
        if isinstance(result, list):
            self._nodes_cache = {n.get("node_id", 0): n for n in result}
            return result
        return []

    async def get_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific Matter node by ID.

        Args:
            node_id: Matter node ID

        Returns:
            Node dictionary or None
        """
        result = await self._send_command("get_node", {"node_id": node_id})
        return result

    async def commission_with_code(self, code: str, network_only: bool = True) -> Dict[str, Any]:
        """
        Commission a new Matter device using a pairing code.

        The code can be a QR code string (MT:...) or a manual pairing code
        (numeric).

        Args:
            code: QR code string or manual pairing code
            network_only: If True, commission over IP network only (no BLE).
                          Default True since our devices are already on WiFi via Hubitat.

        Returns:
            Node info for the newly commissioned device
        """
        logger.info(f"Commissioning device with code: {code[:10]}... (network_only={network_only})")
        result = await self._send_command(
            "commission_with_code",
            {"code": code, "network_only": network_only}
        )
        logger.info(f"Device commissioned: {result}")
        return result

    async def set_wifi_credentials(self, ssid: str, password: str) -> Dict:
        """
        Set WiFi credentials for commissioning wireless devices.

        Must be called before commission_with_code for WiFi devices.

        Args:
            ssid: WiFi network name
            password: WiFi password
        """
        return await self._send_command(
            "set_wifi_credentials",
            {"ssid": ssid, "credentials": password}
        )

    async def remove_node(self, node_id: int) -> Dict:
        """
        Remove a commissioned node from the Matter fabric.

        Args:
            node_id: Node to remove
        """
        logger.info(f"Removing Matter node {node_id}")
        return await self._send_command("remove_node", {"node_id": node_id})

    # =========================================================================
    # Device Commands
    # =========================================================================

    async def send_command(
        self,
        node_id: int,
        endpoint_id: int,
        cluster_id: int,
        command: str,
        payload: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        Send a Matter cluster command to a device.

        Args:
            node_id: Target Matter node ID
            endpoint_id: Target endpoint (usually 1 for single-endpoint devices)
            cluster_id: Matter cluster ID (e.g., 6=OnOff, 8=LevelControl)
            command: Command name (e.g., 'On', 'Off', 'MoveToLevel')
            payload: Command payload parameters

        Returns:
            Command result from the device
        """
        logger.debug(
            f"Matter command: node={node_id} ep={endpoint_id} "
            f"cluster={cluster_id} cmd={command} payload={payload}"
        )
        return await self._send_command(
            "device_command",
            {
                "node_id": node_id,
                "endpoint_id": endpoint_id,
                "cluster_id": cluster_id,
                "command_name": command,
                "payload": payload or {}
            }
        )

    async def send_hubitat_command(
        self,
        node_id: int,
        endpoint_id: int,
        hubitat_command: str,
        hubitat_args: Optional[List] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Send a Hubitat-style command translated to Matter protocol.

        Convenience method that translates Hubitat commands (on, off,
        setLevel, setColorTemperature, setColor) into the appropriate
        Matter cluster commands.

        Args:
            node_id: Target Matter node ID
            endpoint_id: Target endpoint
            hubitat_command: Hubitat command name
            hubitat_args: Hubitat command arguments

        Returns:
            Command result, or None if command can't be translated
        """
        matter_cmd = translate_hubitat_to_matter(hubitat_command, hubitat_args)
        if matter_cmd is None:
            logger.debug(
                f"Cannot translate Hubitat command '{hubitat_command}' to Matter"
            )
            return None

        return await self.send_command(
            node_id=node_id,
            endpoint_id=endpoint_id,
            cluster_id=matter_cmd["cluster_id"],
            command=matter_cmd["command"],
            payload=matter_cmd["payload"]
        )

    # =========================================================================
    # Attribute Reading
    # =========================================================================

    async def read_attribute(
        self,
        node_id: int,
        endpoint_id: int,
        cluster_id: int,
        attribute_id: int
    ) -> Any:
        """
        Read a device attribute.

        Args:
            node_id: Target node
            endpoint_id: Target endpoint
            cluster_id: Cluster containing the attribute
            attribute_id: Attribute ID to read

        Returns:
            Attribute value
        """
        result = await self._send_command(
            "read_attribute",
            {
                "node_id": node_id,
                "attribute_path": f"{endpoint_id}/{cluster_id}/{attribute_id}"
            }
        )
        return result

    # =========================================================================
    # Event Subscription
    # =========================================================================

    async def start_listening(self) -> None:
        """
        Start listening for all node events.

        After calling this, registered event callbacks will receive
        attribute change notifications for all commissioned nodes.
        The initial response includes the full state dump of all nodes.
        """
        result = await self._send_command("start_listening")
        # The result includes all current nodes
        if isinstance(result, dict) and "nodes" in result:
            for node in result["nodes"]:
                self._nodes_cache[node.get("node_id", 0)] = node
        logger.info("Started listening for Matter events")

    def on_event(self, callback: Callable) -> None:
        """
        Register a callback for Matter device events.

        The callback receives event dictionaries from the matter-server.

        Args:
            callback: Function that accepts an event dict
        """
        self._event_callbacks.append(callback)

    # =========================================================================
    # Status
    # =========================================================================

    async def get_server_info(self) -> Dict[str, Any]:
        """Get matter-server status information."""
        return await self._send_command("server_info")


# =============================================================================
# Device Matter Mapping (PostgREST queries)
# =============================================================================

def get_matter_mapping(device_id: str) -> Optional[Dict[str, Any]]:
    """
    Look up the Matter node mapping for a Hubitat device.

    Queries the device_matter_map table via PostgREST.

    Args:
        device_id: Hubitat device ID

    Returns:
        Mapping dict with matter_node_id, matter_endpoint_id, or None
    """
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        resp = requests.get(
            f"{postgrest_url}/device_matter_map",
            params={"hubitat_device_id": f"eq.{device_id}"},
            headers={"Accept": "application/json"},
            timeout=5
        )
        if resp.ok:
            rows = resp.json()
            return rows[0] if rows else None
    except Exception as e:
        logger.warning(f"Failed to query device_matter_map for device {device_id}: {e}")
    return None


def get_all_matter_mappings() -> List[Dict[str, Any]]:
    """
    Get all Hubitat-to-Matter device mappings.

    Returns:
        List of mapping dicts
    """
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        resp = requests.get(
            f"{postgrest_url}/device_matter_map",
            headers={"Accept": "application/json"},
            timeout=5
        )
        if resp.ok:
            return resp.json()
    except Exception as e:
        logger.warning(f"Failed to query device_matter_map: {e}")
    return []


# =============================================================================
# Singleton Factory
# =============================================================================

_matter_client: Optional[MatterClient] = None


def get_matter_client() -> MatterClient:
    """
    Get the singleton MatterClient instance.

    Returns:
        MatterClient instance (not yet connected — call connect() first)
    """
    global _matter_client
    if _matter_client is None:
        _matter_client = MatterClient()
    return _matter_client
