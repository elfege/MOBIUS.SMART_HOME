"""
Humidifier app package.

Ports the Hubitat Groovy "HUMIDIFIER" app
(SMARTHOME_MAIN/APPS/CLIMATE/HUMIDITY/HUMIDIFIER.groovy) to the MOBIUS
multi-instance framework. See apps/humidifier/app.py for the full port notes.
"""

from apps.humidifier.app import HumidifierApp

__all__ = ["HumidifierApp"]
