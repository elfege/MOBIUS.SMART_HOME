"""
Shared constants for Advanced Motion Lighting.

Imported by all submodules that produce log output or reference color presets.
"""

# ANSI escape codes for colored device names in log output
_C = "\033[96m"   # bright cyan — device name
_R = "\033[0m"    # reset

# Named color / color-temperature presets for light control
COLOR_PRESETS = {
    'Soft White': {'temperature': 2700},
    'Warm White': {'temperature': 3000},
    'Cool White': {'temperature': 4000},
    'Daylight':   {'temperature': 6500},
    'Red':        {'hue': 0,  'saturation': 100},
    'Green':      {'hue': 33, 'saturation': 100},
    'Blue':       {'hue': 66, 'saturation': 100},
    'Yellow':     {'hue': 16, 'saturation': 100},
    'Purple':     {'hue': 75, 'saturation': 100},
    'Pink':       {'hue': 83, 'saturation': 56},
}
