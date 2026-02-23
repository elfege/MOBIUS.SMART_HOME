"""
Command Models

Data models for device command execution, results, and status tracking.

Used by the DeviceCommander service to return rich results from command
execution, including verification status, retry counts, and timing.
"""

import traceback
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class CommandStatus(str, Enum):
    """
    Status of a device during/after command execution.

    Lifecycle:
        IDLE → UPDATING → VERIFIED | FAILED | TIMEOUT

    Values:
        IDLE: No command in flight
        UPDATING: Command sent, awaiting state verification
        VERIFIED: Device state confirmed to match expected value
        FAILED: Command rejected or verification failed after all retries
        TIMEOUT: Verification did not complete within DEVICE_CMD_TIMEOUT
    """
    IDLE = "idle"
    UPDATING = "updating"
    VERIFIED = "verified"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class CommandResult:
    """
    Rich result from a device command execution.

    Replaces the old bool return from send_command(). Callers should
    check both `success` (command accepted by hub) and `verified`
    (device state confirmed) before trusting the outcome.

    Attributes:
        success: True if the hub accepted the command (HTTP 200 + valid JSON)
        verified: True if the device state was polled and matches expected
        status: Current CommandStatus after execution
        actual_state: The device attribute value observed during verification
        expected_state: What the attribute should be after the command
        device_id: Hubitat device ID targeted
        device_name: Human-readable device name (for logging)
        command: Command that was sent (on, off, setLevel, etc.)
        args: Command arguments (e.g., [75] for setLevel)
        retries_used: Count of retries at each level (outer, verify)
        elapsed_ms: Total wall-clock time for the full operation
        error: Error message if command or verification failed
        traceback_str: Full traceback string if an exception occurred
        matter_sent: Whether a Matter dual-command was also dispatched
    """
    success: bool = False
    verified: bool = False
    status: CommandStatus = CommandStatus.IDLE
    actual_state: Optional[str] = None
    expected_state: Optional[str] = None
    device_id: str = ""
    device_name: str = ""
    command: str = ""
    args: Optional[List] = None
    retries_used: Dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    traceback_str: Optional[str] = None
    matter_sent: bool = False

    def __str__(self) -> str:
        """Human-readable summary for logging."""
        parts = [
            f"CommandResult({self.device_name or self.device_id}",
            f"cmd={self.command}",
            f"success={self.success}",
            f"verified={self.verified}",
            f"status={self.status.value}",
        ]
        if self.actual_state is not None:
            parts.append(f"actual={self.actual_state}")
        if self.expected_state is not None:
            parts.append(f"expected={self.expected_state}")
        if self.error:
            parts.append(f"error={self.error}")
        parts.append(f"{self.elapsed_ms:.0f}ms")
        return ", ".join(parts) + ")"

    @property
    def is_verified_success(self) -> bool:
        """Shorthand: command succeeded AND state was verified."""
        return self.success and self.verified


# =========================================================================
# Command-to-expected-state mapping
# =========================================================================

# Maps Hubitat command names to (attribute_name, expected_value).
# For commands with dynamic expected values (e.g., setLevel 75),
# the expected value is "{arg0}" — caller must substitute.
# Commands not in this map skip verification (best-effort).
COMMAND_EXPECTED_STATE: Dict[str, tuple] = {
    "on": ("switch", "on"),
    "off": ("switch", "off"),
    "setLevel": ("switch", "on"),             # setLevel implies switch=on
    "setColorTemperature": ("colorTemperature", "{arg0}"),
}


def resolve_expected_state(
    command: str,
    args: Optional[List] = None
) -> Optional[Dict[str, str]]:
    """
    Resolve the expected device attribute and value for a given command.

    Args:
        command: Hubitat command name (on, off, setLevel, etc.)
        args: Optional command arguments

    Returns:
        Dict with 'attribute' and 'expected' keys, or None if command
        is not in the mapping (verification should be skipped).

    Examples:
        >>> resolve_expected_state("on")
        {"attribute": "switch", "expected": "on"}

        >>> resolve_expected_state("setLevel", [75])
        {"attribute": "switch", "expected": "on"}

        >>> resolve_expected_state("setColor", [{"hue": 50}])
        None  # Not in mapping, skip verification
    """
    if command not in COMMAND_EXPECTED_STATE:
        return None

    attribute, expected_template = COMMAND_EXPECTED_STATE[command]

    # Substitute {arg0} with the first argument value
    if "{arg0}" in expected_template and args and len(args) > 0:
        expected = str(args[0])
    else:
        expected = expected_template

    return {"attribute": attribute, "expected": expected}
