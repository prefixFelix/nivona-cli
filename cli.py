#!/usr/bin/env python3
import sys
import tty
import termios
import select
import asyncio
import struct
import os
import time
import threading
import pathlib
from io import StringIO

from rich.console import Console
from rich.text import Text
from rich.spinner import Spinner

_spinner = Spinner("simpleDots")

from bleak import BleakClient, BleakError, BleakScanner


# ---------------------------------------------------------------------------
# Settings option dicts
# ---------------------------------------------------------------------------

_HARDNESS       = {0: "soft", 1: "medium", 2: "hard", 3: "very hard"}
_OFF_ON         = {0: "off", 1: "on"}
_AUTO_OFF_STD   = {0: "10 min", 1: "30 min", 2: "1 h", 3: "2 h", 4: "4 h",
                   5: "6 h", 6: "8 h", 7: "10 h", 8: "12 h", 9: "off"}
_AUTO_OFF_8000  = {0: "10 min", 1: "30 min", 2: "1 h", 3: "2 h", 4: "4 h",
                   5: "6 h", 6: "8 h", 7: "10 h", 8: "12 h", 9: "14 h", 10: "16 h"}
_TEMP           = {0: "normal", 1: "high", 2: "max", 3: "individual"}
_TEMP_ON_OFF    = {0: "off", 1: "on"}
_PROFILE_STD    = {0: "dynamic", 1: "constant", 2: "intense", 3: "individual"}
_PROFILE_1040   = {0: "dynamic", 1: "constant", 2: "intense", 3: "quick", 4: "individual"}
_MILK_1030      = {0: "high", 1: "max", 2: "individual"}
_MILK_1040      = {0: "normal", 1: "high", 2: "hot", 3: "max", 4: "individual"}
_MILK_FOAM_1040 = {0: "warm", 1: "max", 2: "individual"}
_FROTHER_TIME   = {0: "10 min", 1: "20 min", 2: "30 min", 3: "40 min"}

# Recipe-specific option dicts
# Strength: raw value = beans - 1
_STR3              = {0: "1 bean",  1: "2 beans", 2: "3 beans"}
_STR5              = {0: "1 bean",  1: "2 beans", 2: "3 beans", 3: "4 beans", 4: "5 beans"}
# Recipe profile/temperature — "individual" is only settable from the main settings menu
_RECIPE_PROFILE    = {0: "dynamic", 1: "constant", 2: "intense"}
_RECIPE_PROFILE_W  = {0: "dynamic", 1: "constant", 2: "intense", 4: "quick"}
_RECIPE_TEMP       = {0: "normal",  1: "high",     2: "max"}
# Preparation (best-effort — firmware exposes field but not labels)
_PREPARATION       = {0: "normal",  1: "ristretto", 2: "lungo"}


# ---------------------------------------------------------------------------
# Slider descriptor
# ---------------------------------------------------------------------------

class _Slider:
    """Descriptor for a numeric (ml) recipe field edited with left/right arrows."""
    __slots__ = ('lo', 'hi', 'step', 'scale')

    def __init__(self, lo: int, hi: int, step: int, scale: int = 1):
        self.lo    = lo     # raw minimum value
        self.hi    = hi     # raw maximum value
        self.step  = step   # raw step per key-press
        self.scale = scale  # display ml = raw / scale  (scale=10 for fluidWriteScale10 families)


# Slider presets, raw units; 900/900-light use scale=10 (raw = ml * 10)
_SL     = _Slider(lo=20,  hi=240,  step=5,  scale=1)   # coffee / milk / foam
_SL_W   = _Slider(lo=20,  hi=500,  step=5,  scale=1)   # water
_SL10   = _Slider(lo=200, hi=2400, step=50, scale=10)  # coffee / milk / foam  (scale-10)
_SL10_W = _Slider(lo=200, hi=5000, step=50, scale=10)  # water (scale-10)


# ---------------------------------------------------------------------------
# Settings tables  (reg_id, label, options_dict)
# ---------------------------------------------------------------------------

SETTINGS: dict[str, list[tuple[int, str, dict]]] = {
    "600": [
        (101, "Water hardness", _HARDNESS),
        (102, "Temperature",    _TEMP),
        (103, "Off-rinse",      _OFF_ON),
        (104, "Auto-off",       _AUTO_OFF_STD),
        (106, "Profile",        _PROFILE_STD),
    ],
    "8000": [
        (101, "Water hardness",      _HARDNESS),
        (103, "Off-rinse",           _OFF_ON),
        (104, "Auto-off",            _AUTO_OFF_8000),
        (105, "Coffee temperature",  _TEMP_ON_OFF),
    ],
    "900": [
        (102, "Water hardness", _HARDNESS),
        (103, "Off-rinse",      _OFF_ON),
        (109, "Auto-off",       _AUTO_OFF_STD),
    ],
    "900-light": [
        (102, "Water hardness", _HARDNESS),
        (109, "Auto-off",       _AUTO_OFF_STD),
    ],
    "1030": [
        (102, "Water hardness",     _HARDNESS),
        (103, "Off-rinse",          _OFF_ON),
        (109, "Auto-off",           _AUTO_OFF_STD),
        (113, "Profile",            _PROFILE_STD),
        (114, "Coffee temperature", _TEMP),
        (115, "Water temperature",  _TEMP),
        (116, "Milk temperature",   _MILK_1030),
    ],
    "1040": [
        (102, "Water hardness",        _HARDNESS),
        (103, "Off-rinse",             _OFF_ON),
        (109, "Auto-off",              _AUTO_OFF_STD),
        (113, "Profile",               _PROFILE_1040),
        (114, "Coffee temperature",    _TEMP),
        (115, "Water temperature",     _TEMP),
        (116, "Milk temperature",      _MILK_1040),
        (117, "Milk foam temperature", _MILK_FOAM_1040),
        (118, "Power-on rinse",        _OFF_ON),
        (119, "Power-on frother time", _FROTHER_TIME),
    ],
}
SETTINGS["700"] = SETTINGS["600"]
SETTINGS["79x"] = [r for r in SETTINGS["600"] if r[0] != 103]


# ---------------------------------------------------------------------------
# Statistics tables  (reg_id, label)
# ---------------------------------------------------------------------------

STATS: dict[str, list[tuple[int, str]]] = {
    "8000": [
        (200, "Espresso"),           (201, "Coffee"),
        (202, "Americano"),          (203, "Cappuccino"),
        (204, "Caffe latte"),        (205, "Latte macchiato"),
        (206, "Warm milk"),          (207, "Hot water"),
        (208, "My coffee"),          (209, "Steam drinks"),
        (210, "Powder coffee"),      (213, "Total beverages"),
        (214, "Clean coffee system"),(215, "Clean frother"),
        (216, "Rinse cycles"),       (219, "Filter changes"),
        (220, "Descaling"),          (221, "Beverages via app"),
        (600, "Descale %"),          (610, "Brew unit clean %"),
        (620, "Frother clean %"),    (640, "Filter %"),
    ],
    "700": [
        (200, "Espresso"),           (201, "Cream"),
        (202, "Lungo"),              (203, "Americano"),
        (204, "Cappuccino"),         (205, "Latte macchiato"),
        (206, "Milk"),               (207, "Hot water"),
        (208, "My coffee"),          (213, "Total beverages"),
        (214, "Clean brewing unit"), (215, "Clean frother"),
        (216, "Rinse cycles"),       (219, "Filter changes"),
        (220, "Descaling"),          (221, "Beverages via app"),
        (600, "Descale %"),          (610, "Brew unit clean %"),
        (620, "Frother clean %"),    (640, "Filter %"),
    ],
    "600": [
        (200, "Espresso"),           (201, "Coffee"),
        (202, "Americano"),          (203, "Cappuccino"),
        (204, "Frothy milk"),        (207, "Hot water"),
        (213, "Total beverages"),
        (214, "Clean brewing unit"), (215, "Clean frother"),
        (216, "Rinse cycles"),       (219, "Filter changes"),
        (220, "Descaling"),          (221, "Beverages via app"),
        (600, "Descale %"),          (601, "Descale warning"),
        (610, "Brew unit clean %"),  (611, "Brew unit clean warning"),
        (620, "Frother clean %"),    (621, "Frother clean warning"),
        (640, "Filter %"),           (641, "Filter warning"),
        (105, "Filter dependency"),
    ],
    "1030": [
        (200, "Espresso"),           (201, "Coffee"),
        (202, "Americano"),          (203, "Cappuccino"),
        (204, "Caffe latte"),        (205, "Latte macchiato"),
        (206, "Warm milk"),          (207, "Hot milk"),
        (208, "Milk foam"),          (209, "Hot water"),
        (105, "Filter dependency"),
    ],
    "1040": [
        (200, "Espresso"),           (201, "Coffee"),
        (202, "Americano"),          (203, "Cappuccino"),
        (204, "Caffe latte"),        (205, "Latte macchiato"),
        (206, "Warm milk"),          (208, "Milk foam"),
        (209, "Hot water"),
        (105, "Filter dependency"),
    ],
    "900": [
        (200, "Espresso"),           (201, "Coffee"),
        (202, "Americano"),          (203, "Cappuccino"),
        (204, "Caffe latte"),        (205, "Latte macchiato"),
        (206, "Milk"),               (207, "Hot water"),
        (208, "My coffee"),
        (105, "Filter dependency"),
    ],
}
STATS["79x"]       = STATS["700"]
STATS["900-light"] = STATS["900"]


# ---------------------------------------------------------------------------
# Recipe tables  (selector, title)
# ---------------------------------------------------------------------------

RECIPES: dict[str, list[tuple[int, str]]] = {
    "600": [
        (0, "Espresso"), (1, "Coffee"),
        (3, "Americano"), (4, "Cappuccino"),
        (6, "Frothy milk"), (7, "Hot water"),
    ],
    "700": [
        (0, "Espresso"), (1, "Cream"),   (2, "Lungo"),    (3, "Americano"),
        (4, "Cappuccino"), (5, "Latte macchiato"), (6, "Milk"), (7, "Hot water"),
    ],
    "79x": [
        (0, "Espresso"), (1, "Coffee"), (2, "Americano"),
        (3, "Cappuccino"), (5, "Latte macchiato"), (6, "Milk"), (7, "Hot water"),
    ],
    "900": [
        (0, "Espresso"),    (1, "Coffee"),   (2, "Americano"),   (3, "Cappuccino"),
        (4, "Caffe latte"), (5, "Latte macchiato"), (6, "Hot milk"), (7, "Hot water"),
    ],
    "1030": [
        (0, "Espresso"),    (1, "Coffee"),   (2, "Americano"),   (3, "Cappuccino"),
        (4, "Caffe latte"), (5, "Latte macchiato"), (6, "Hot water"), (7, "Warm milk"),
        (8, "Hot milk"),    (9, "Frothy milk"),
    ],
    "1040": [
        (0, "Espresso"),    (1, "Coffee"),   (2, "Americano"),   (3, "Cappuccino"),
        (4, "Caffe latte"), (5, "Latte macchiato"), (6, "Hot water"),
        (7, "Warm milk"),   (8, "Frothy milk"),
    ],
    "8000": [
        (0, "Espresso"),    (1, "Coffee"),   (2, "Americano"),   (3, "Cappuccino"),
        (4, "Caffe latte"), (5, "Latte macchiato"), (6, "Milk"), (7, "Hot water"),
    ],
}
RECIPES["900-light"] = RECIPES["900"]

# Recipe enum fields (non-ml): per family, (offset, label, options_dict)
RECIPE_FIELDS: dict[str, list[tuple[int, str, dict]]] = {
    "600": [
        (1, "Strength",    _STR5),
        (2, "Profile",     _RECIPE_PROFILE),
        (3, "Temperature", _RECIPE_TEMP),
        (4, "Two cups",    _OFF_ON),
    ],
    "700": [
        (1, "Strength",    _STR3),
        (2, "Profile",     _RECIPE_PROFILE),
        (3, "Temperature", _RECIPE_TEMP),
        (4, "Two cups",    _OFF_ON),
    ],
    "79x": [
        (1, "Strength",    _STR5),
        (2, "Profile",     _RECIPE_PROFILE),
        (3, "Temperature", _RECIPE_TEMP),
        (4, "Two cups",    _OFF_ON),
    ],
    "8000": [
        (1, "Strength",    _STR5),
        (2, "Profile",     _RECIPE_PROFILE_W),
        (3, "Temperature", _RECIPE_TEMP),
        (4, "Two cups",    _OFF_ON),
    ],
    "900": [
        (1,  "Strength",            _STR5),
        (2,  "Profile",             _RECIPE_PROFILE),
        (3,  "Preparation",         _PREPARATION),
        (4,  "Two cups",            _OFF_ON),
        (5,  "Coffee temperature",  _RECIPE_TEMP),
        (6,  "Water temperature",   _RECIPE_TEMP),
        (7,  "Milk temperature",    _RECIPE_TEMP),
        (8,  "Milk foam temp",      _RECIPE_TEMP),
        (13, "Overall temperature", _RECIPE_TEMP),
    ],
    "1030": [
        (1, "Strength",           _STR5),
        (2, "Profile",            _RECIPE_PROFILE_W),
        (3, "Preparation",        _PREPARATION),
        (4, "Two cups",           _OFF_ON),
        (5, "Coffee temperature", _RECIPE_TEMP),
        (6, "Water temperature",  _RECIPE_TEMP),
        (7, "Milk temperature",   _RECIPE_TEMP),
        (8, "Milk foam temp",     _RECIPE_TEMP),
    ],
    "1040": [
        (1, "Strength",           _STR5),
        (2, "Profile",            _RECIPE_PROFILE_W),
        (3, "Preparation",        _PREPARATION),
        (4, "Two cups",           _OFF_ON),
        (5, "Coffee temperature", _RECIPE_TEMP),
        (6, "Water temperature",  _RECIPE_TEMP),
        (7, "Milk temperature",   _RECIPE_TEMP),
        (8, "Milk foam temp",     _RECIPE_TEMP),
    ],
}
RECIPE_FIELDS["900-light"] = RECIPE_FIELDS["900"]

# Per-recipe ml fields: family -> selector -> [(offset, label, slider), ...]
# 600 layout offsets: +5 coffee, +6 water, +8 milk foam  (confirmed on NICR 660)
# Other families: best-effort based on beverage type, verify per model.
_ML    = _SL      # coffee / milk / foam
_ML_W  = _SL_W    # water
_ML10  = _SL10    # coffee / milk / foam  (scale-10)
_ML10_W = _SL10_W # water (scale-10)

RECIPE_ML: dict[str, dict[int, list[tuple[int, str, _Slider]]]] = {
    "600": {
        0: [(5, "Coffee",    _ML)],                                # Espresso
        1: [(5, "Coffee",    _ML)],                                # Coffee
        3: [(5, "Coffee",    _ML),  (6, "Water",     _ML_W)],      # Americano
        4: [(5, "Coffee",    _ML),  (8, "Milk foam",  _ML)],       # Cappuccino
        6: [(8, "Milk foam", _ML)],                                # Frothy milk
        7: [(6, "Water",     _ML_W)],                              # Hot water
    },
    "700": {
        0: [(5, "Coffee",    _ML)],                                # Espresso
        1: [(5, "Coffee",    _ML),  (8, "Milk foam",  _ML)],       # Cream
        2: [(5, "Coffee",    _ML),  (6, "Water",     _ML_W)],      # Lungo
        3: [(5, "Coffee",    _ML),  (6, "Water",     _ML_W)],      # Americano
        4: [(5, "Coffee",    _ML),  (8, "Milk foam",  _ML)],       # Cappuccino
        5: [(5, "Coffee",    _ML),  (7, "Milk",       _ML)],       # Latte macchiato
        6: [(7, "Milk",      _ML)],                                # Milk
        7: [(6, "Water",     _ML_W)],                              # Hot water
    },
    "79x": {
        0: [(5, "Coffee",    _ML)],                                # Espresso
        1: [(5, "Coffee",    _ML)],                                # Coffee
        2: [(5, "Coffee",    _ML),  (6, "Water",     _ML_W)],      # Americano
        3: [(5, "Coffee",    _ML),  (8, "Milk foam",  _ML)],       # Cappuccino
        5: [(5, "Coffee",    _ML),  (7, "Milk",       _ML)],       # Latte macchiato
        6: [(7, "Milk",      _ML)],                                # Milk
        7: [(6, "Water",     _ML_W)],                              # Hot water
    },
    "8000": {
        0: [(5, "Coffee",    _ML)],                                # Espresso
        1: [(5, "Coffee",    _ML)],                                # Coffee
        2: [(5, "Coffee",    _ML),  (6, "Water",     _ML_W)],      # Americano
        3: [(5, "Coffee",    _ML),  (8, "Milk foam",  _ML)],       # Cappuccino
        4: [(5, "Coffee",    _ML),  (7, "Milk",       _ML)],       # Caffe latte
        5: [(5, "Coffee",    _ML),  (7, "Milk",       _ML)],       # Latte macchiato
        6: [(7, "Milk",      _ML)],                                # Milk
        7: [(6, "Water",     _ML_W)],                              # Hot water
    },
    "900": {
        0: [(9,  "Coffee",    _ML10)],                             # Espresso
        1: [(9,  "Coffee",    _ML10)],                             # Coffee
        2: [(9,  "Coffee",    _ML10), (10, "Water",    _ML10_W)],  # Americano
        3: [(9,  "Coffee",    _ML10), (12, "Milk foam", _ML10)],   # Cappuccino
        4: [(9,  "Coffee",    _ML10), (11, "Milk",      _ML10)],   # Caffe latte
        5: [(9,  "Coffee",    _ML10), (11, "Milk",      _ML10)],   # Latte macchiato
        6: [(11, "Milk",      _ML10)],                             # Hot milk
        7: [(10, "Water",     _ML10_W)],                           # Hot water
    },
    "1030": {
        0: [(9,  "Coffee",    _ML)],                               # Espresso
        1: [(9,  "Coffee",    _ML)],                               # Coffee
        2: [(9,  "Coffee",    _ML),  (10, "Water",    _ML_W)],     # Americano
        3: [(9,  "Coffee",    _ML),  (12, "Milk foam", _ML)],      # Cappuccino
        4: [(9,  "Coffee",    _ML),  (11, "Milk",      _ML)],      # Caffe latte
        5: [(9,  "Coffee",    _ML),  (11, "Milk",      _ML)],      # Latte macchiato
        6: [(10, "Water",     _ML_W)],                             # Hot water
        7: [(11, "Milk",      _ML)],                               # Warm milk
        8: [(11, "Milk",      _ML)],                               # Hot milk
        9: [(12, "Milk foam", _ML)],                               # Frothy milk
    },
    "1040": {
        0: [(9,  "Coffee",    _ML)],                               # Espresso
        1: [(9,  "Coffee",    _ML)],                               # Coffee
        2: [(9,  "Coffee",    _ML),  (10, "Water",    _ML_W)],     # Americano
        3: [(9,  "Coffee",    _ML),  (12, "Milk foam", _ML)],      # Cappuccino
        4: [(9,  "Coffee",    _ML),  (11, "Milk",      _ML)],      # Caffe latte
        5: [(9,  "Coffee",    _ML),  (11, "Milk",      _ML)],      # Latte macchiato
        6: [(10, "Water",     _ML_W)],                             # Hot water
        7: [(11, "Milk",      _ML)],                               # Warm milk
        8: [(12, "Milk foam", _ML)],                               # Frothy milk
    },
}
RECIPE_ML["900-light"] = RECIPE_ML["900"]

# Per-recipe enum field exclusions: family -> selector -> set of offsets to hide.
# Fields at these offsets are neither fetched nor shown in the edit menu.
RECIPE_ENUM_EXCLUDE: dict[str, dict[int, set]] = {
    "600": {
        6: {1, 2, 3, 4},   # Frothy milk:  no strength, profile, temperature, two cups
        7: {1, 2, 3, 4},   # Hot water:    no strength, profile, temperature, two cups
    },
}


# ---------------------------------------------------------------------------
# Model detection
# ---------------------------------------------------------------------------

_MODEL_RULES: list[tuple[str, str]] = [
    ("8101", "8000"), ("8103", "8000"), ("8107", "8000"),
    ("040",  "1040"), ("030",  "1030"),
    ("660",  "600"),  ("670",  "600"),  ("675",  "600"),  ("680",  "600"),
    ("756",  "700"),  ("758",  "700"),  ("759",  "700"),  ("768",  "700"),
    ("769",  "700"),  ("778",  "700"),  ("779",  "700"),  ("788",  "700"),
    ("789",  "700"),
    ("790",  "79x"),  ("791",  "79x"),  ("792",  "79x"),  ("793",  "79x"),
    ("794",  "79x"),  ("795",  "79x"),  ("796",  "79x"),  ("797",  "79x"),
    ("799",  "79x"),
    ("920",  "900"),  ("930",  "900"),
    ("960",  "900-light"), ("965", "900-light"), ("970", "900-light"),
]


def detect_family(name: str) -> str | None:
    for token, family in _MODEL_RULES:
        if token in name:
            return family
    return None


def detect_nicr(name: str) -> str | None:
    for token, _ in _MODEL_RULES:
        if token in name:
            return token
    return None


# BLE constants
NIVONA_SVC = '0000AD00-B35C-11E4-9813-0002A5D5C51B'
NIVONA_OUI = 'EC:7D:FF'
AD02 = '0000AD02-B35C-11E4-9813-0002A5D5C51B'   # notify (RX)
AD03 = '0000AD03-B35C-11E4-9813-0002A5D5C51B'   # write  (TX)


# Protocol crypto
_WORKING_KEY = b'NIV_060616_V10_1*9#3!4$6+4res-?3'

_HU_TABLE = bytes([
    0x62,0x06,0x55,0x96,0x24,0x17,0x70,0xA4,0x87,0xCF,0xA9,0x05,0x1A,0x40,0xA5,0xDB,
    0x3D,0x14,0x44,0x59,0x82,0x3F,0x34,0x66,0x18,0xE5,0x84,0xF5,0x50,0xD8,0xC3,0x73,
    0x5A,0xA8,0x9C,0xCB,0xB1,0x78,0x02,0xBE,0xBC,0x07,0x64,0xB9,0xAE,0xF3,0xA2,0x0A,
    0xED,0x12,0xFD,0xE1,0x08,0xD0,0xAC,0xF4,0xFF,0x7E,0x65,0x4F,0x91,0xEB,0xE4,0x79,
    0x7B,0xFB,0x43,0xFA,0xA1,0x00,0x6B,0x61,0xF1,0x6F,0xB5,0x52,0xF9,0x21,0x45,0x37,
    0x3B,0x99,0x1D,0x09,0xD5,0xA7,0x54,0x5D,0x1E,0x2E,0x5E,0x4B,0x97,0x72,0x49,0xDE,
    0xC5,0x60,0xD2,0x2D,0x10,0xE3,0xF8,0xCA,0x33,0x98,0xFC,0x7D,0x51,0xCE,0xD7,0xBA,
    0x27,0x9E,0xB2,0xBB,0x83,0x88,0x01,0x31,0x32,0x11,0x8D,0x5B,0x2F,0x81,0x3C,0x63,
    0x9A,0x23,0x56,0xAB,0x69,0x22,0x26,0xC8,0x93,0x3A,0x4D,0x76,0xAD,0xF6,0x4C,0xFE,
    0x85,0xE8,0xC4,0x90,0xC6,0x7C,0x35,0x04,0x6C,0x4A,0xDF,0xEA,0x86,0xE6,0x9D,0x8B,
    0xBD,0xCD,0xC7,0x80,0xB0,0x13,0xD3,0xEC,0x7F,0xC0,0xE7,0x46,0xE9,0x58,0x92,0x2C,
    0xB7,0xC9,0x16,0x53,0x0D,0xD6,0x74,0x6D,0x9F,0x20,0x5F,0xE2,0x8C,0xDC,0x39,0x0C,
    0xDD,0x1F,0xD1,0xB6,0x8F,0x5C,0x95,0xB8,0x94,0x3E,0x71,0x41,0x25,0x1B,0x6A,0xA6,
    0x03,0x0E,0xCC,0x48,0x15,0x29,0x38,0x42,0x1C,0xC1,0x28,0xD9,0x19,0x36,0xB3,0x75,
    0xEE,0x57,0xF0,0x9B,0xB4,0xAA,0xF2,0xD4,0xBF,0xA3,0x4E,0xDA,0x89,0xC2,0xAF,0x6E,
    0x2B,0x77,0xE0,0x47,0x7A,0x8E,0x2A,0xA0,0x68,0x30,0xF7,0x67,0x0F,0x0B,0x8A,0xEF,
])


def _rc4(data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + _WORKING_KEY[i % 32]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _checksum(cmd: bytes, body: bytes) -> int:
    return (~(cmd[0] + cmd[1] + sum(body))) & 0xFF


def _build_packet(cmd: bytes, payload: bytes, session: bytes | None = None) -> bytes:
    body = bytearray()
    if session:
        body += session
    body += payload
    crc = _checksum(cmd, body)
    pkt = bytearray([0x53, cmd[0], cmd[1]])
    pkt += _rc4(bytes(body) + bytes([crc]))
    pkt.append(0x45)
    return bytes(pkt)


def _decode_packet(raw: bytes) -> tuple[bytes, bytes] | None:
    if len(raw) < 5 or raw[0] != 0x53 or raw[-1] != 0x45:
        return None
    decrypted = _rc4(raw[3:-1])
    return raw[1:3], decrypted[:-1]


def _hu_verifier(data: bytes) -> bytes:
    s = _HU_TABLE[data[0]]
    for b in data[1:]:
        s = _HU_TABLE[s ^ b]
    v0 = (s + 0x5D) & 0xFF
    s = _HU_TABLE[(data[0] + 1) & 0xFF]
    for b in data[1:]:
        s = _HU_TABLE[s ^ b]
    v1 = (s + 0xA7) & 0xFF
    return bytes([v0, v1])


def _build_hu() -> bytes:
    seed = os.urandom(4)
    return _build_packet(b'HU', seed + _hu_verifier(seed))


def _build_hr(reg_id: int, session: bytes) -> bytes:
    return _build_packet(b'HR', struct.pack('>H', reg_id), session=session)


def _build_hw(reg_id: int, value: int, session: bytes) -> bytes:
    return _build_packet(b'HW', struct.pack('>Hi', reg_id, value), session=session)


def _build_hd(reg_id: int, session: bytes) -> bytes:
    return _build_packet(b'HD', struct.pack('>H', reg_id), session=session)


def _build_hx(session: bytes) -> bytes:
    return _build_packet(b'HX', b'', session=session)


def _build_hz(session: bytes) -> bytes:
    return _build_packet(b'HZ', bytes(4), session=session)


# Brew-mode constants
_PROCESS_LABELS: dict[int, str] = {
    3:  "ready",
    4:  "preparing drink",
    8:  "ready",
    11: "preparing drink",
}

_MESSAGE_LABELS: dict[int, str] = {
    1:  "brewing unit removed",
    2:  "trays missing",
    3:  "empty trays",
    4:  "fill up water",
    5:  "close powder shaft",
    6:  "fill coffee beans",
    11: "move cup to frother and open valve",
    20: "flush required",
}

_BREW_MODE: dict[str, int] = {"8000": 0x04}  # default 0x0B for all other families

# For families where the HE/scratch brew selector differs from the register-base selector.
# Maps reg_selector -> brew_selector (HE payload[3] and HW 9001).
_BREW_SELECTOR: dict[str, dict[int, int]] = {
    "600": {3: 2, 4: 3, 6: 4, 7: 5},
}


# Paired machine cache + BLE thread
_paired: dict | None = None   # keys: device, mac, name, nicr, family
                               #       client, session, pending, inbox

_ble_loop:   asyncio.AbstractEventLoop | None = None
_ble_thread: threading.Thread | None          = None


def _run_ble(coro):
    """Submit a coroutine to the persistent BLE event loop; block until result."""
    global _ble_loop, _ble_thread
    if _ble_loop is None or not _ble_loop.is_running():
        _ble_loop = asyncio.new_event_loop()
        _ble_thread = threading.Thread(target=_ble_loop.run_forever, daemon=True)
        _ble_thread.start()
    return asyncio.run_coroutine_threadsafe(coro, _ble_loop).result()


# BLE session management
def _ble_on_notify(_h, data: bytearray) -> None:
    if _paired is None:
        return
    inbox   = _paired.get('inbox')
    pending = _paired.get('pending')
    if inbox is None or pending is None:
        return
    inbox[0] += data
    if len(inbox[0]) >= 5 and inbox[0][0] == 0x53 and inbox[0][-1] == 0x45:
        f = pending[0]
        if not f.done():
            f.set_result(bytes(inbox[0]))
        inbox[0] = bytearray()


def _ble_on_disconnect(_client) -> None:
    """Clear live connection so the next operation reconnects + re-runs HU."""
    if _paired is not None:
        _paired['client']  = None
        _paired['session'] = None


async def _ensure_session() -> tuple[bytes, list, list]:
    """Return (session_key, pending, inbox), reconnecting + re-running HU only when needed."""
    client:  BleakClient | None = _paired.get('client')
    session: bytes | None       = _paired.get('session')

    if client is not None and client.is_connected and session is not None:
        return session, _paired['pending'], _paired['inbox']

    loop    = asyncio.get_running_loop()
    pending: list = [loop.create_future()]
    inbox:   list = [bytearray()]
    _paired['pending'] = pending
    _paired['inbox']   = inbox

    client = BleakClient(_paired['device'], disconnected_callback=_ble_on_disconnect)
    _paired['client'] = client

    await client.connect()
    await client.pair()
    await client.start_notify(AD02, _ble_on_notify)

    inbox[0]   = bytearray()
    pending[0] = loop.create_future()
    await client.write_gatt_char(AD03, _build_hu(), response=False)
    raw = await asyncio.wait_for(pending[0], timeout=5)

    parsed = _decode_packet(raw)
    if parsed is None or len(parsed[1]) < 8:
        raise RuntimeError(f"HU handshake failed: {raw.hex()}")
    payload = parsed[1]
    if _hu_verifier(payload[:6]) != payload[6:8]:
        raise RuntimeError("HU session key verification failed")

    session = payload[4:6]
    _paired['session'] = session
    return session, pending, inbox


# Low-level async helpers
async def _read_register(
    client: BleakClient, session: bytes, pending: list, inbox: list,
    reg_id: int, timeout: float = 5.0,
) -> int | None:
    """Send HR for reg_id, await response, return parsed i32 or None on timeout/failure."""
    inbox[0]   = bytearray()
    pending[0] = asyncio.get_running_loop().create_future()
    await client.write_gatt_char(AD03, _build_hr(reg_id, session), response=False)
    try:
        raw = await asyncio.wait_for(pending[0], timeout=timeout)
    except asyncio.TimeoutError:
        return None
    parsed = _decode_packet(raw)
    if parsed and len(parsed[1]) >= 6:
        return struct.unpack('>i', parsed[1][2:6])[0]
    return None


async def _reset_register(
    client: BleakClient, session: bytes, pending: list, inbox: list,
    reg_id: int, timeout: float = 2.0,
) -> None:
    """Send HD for reg_id, await response (ignoring timeout)."""
    inbox[0]   = bytearray()
    pending[0] = asyncio.get_running_loop().create_future()
    await client.write_gatt_char(AD03, _build_hd(reg_id, session), response=False)
    try:
        await asyncio.wait_for(pending[0], timeout=timeout)
    except asyncio.TimeoutError:
        pass


# Async BLE operations
async def _do_scan(timeout: float = 5.0):
    """Scan using Nivona service UUID (like app.py), confirm with OUI."""
    scanner = BleakScanner(service_uuids=[NIVONA_SVC])
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()
    for device, _ in scanner.discovered_devices_and_advertisement_data.values():
        if device.address.upper().startswith(NIVONA_OUI):
            return device
    devs = list(scanner.discovered_devices_and_advertisement_data.values())
    if devs:
        return devs[0][0]
    return None


async def _do_pair(device) -> None:
    async with BleakClient(device) as client:
        await client.pair()


async def _fetch_settings(family: str) -> dict[int, int]:
    registers = SETTINGS.get(family, [])
    session, pending, inbox = await _ensure_session()
    client = _paired['client']
    values: dict[int, int] = {}
    for reg_id, _label, _options in registers:
        v = await _read_register(client, session, pending, inbox, reg_id)
        if v is not None:
            values[reg_id] = v
    return values


async def _fetch_stats(family: str) -> list[tuple[str, int]]:
    registers = STATS.get(family, [])
    session, pending, inbox = await _ensure_session()
    client = _paired['client']
    raw_values: dict[int, int] = {}
    for reg_id, _label in registers:
        v = await _read_register(client, session, pending, inbox, reg_id)
        if v is not None:
            raw_values[reg_id] = v
    return [(label, raw_values[reg_id]) for reg_id, label in registers if reg_id in raw_values]


async def _fetch_recipe(family: str, selector: int) -> dict[int, int]:
    base      = 10000 + selector * 100
    excl      = RECIPE_ENUM_EXCLUDE.get(family, {}).get(selector, set())
    enum_flds = [(off, lbl, opts) for off, lbl, opts in RECIPE_FIELDS.get(family, [])
                 if off not in excl]
    ml_flds   = RECIPE_ML.get(family, {}).get(selector, [])
    session, pending, inbox = await _ensure_session()
    client = _paired['client']
    values: dict[int, int] = {}
    for offset, _label, _options in list(enum_flds) + list(ml_flds):
        reg_id = base + offset
        v = await _read_register(client, session, pending, inbox, reg_id)
        if v is not None:
            values[reg_id] = v
    return values


async def _reset_recipe(family: str, selector: int) -> None:
    base     = 10000 + selector * 100
    excl     = RECIPE_ENUM_EXCLUDE.get(family, {}).get(selector, set())
    all_flds = (
        [(off, lbl, opts) for off, lbl, opts in RECIPE_FIELDS.get(family, []) if off not in excl]
        + list(RECIPE_ML.get(family, {}).get(selector, []))
    )
    session, pending, inbox = await _ensure_session()
    client = _paired['client']
    for offset, _, _ in all_flds:
        await _reset_register(client, session, pending, inbox, base + offset)


async def _brew(family: str, selector: int, reg_fields: list, values: dict) -> None:
    """Write recipe fields to scratch registers (9001+offset) and fire HE."""
    SCRATCH     = 9001
    base        = 10000 + selector * 100
    mode        = _BREW_MODE.get(family, 0x0B)
    he_selector = _BREW_SELECTOR.get(family, {}).get(selector, selector)

    session, pending, inbox = await _ensure_session()
    loop   = asyncio.get_running_loop()
    client = _paired['client']

    async def _send(pkt: bytes, timeout: float) -> None:
        inbox[0]   = bytearray()
        pending[0] = loop.create_future()
        for off in range(0, len(pkt), 20):
            await client.write_gatt_char(AD03, pkt[off:off + 20], response=False)
            if off + 20 < len(pkt):
                await asyncio.sleep(0.010)
        try:
            await asyncio.wait_for(pending[0], timeout=timeout)
        except asyncio.TimeoutError:
            pass

    for reg_id, _, _ in reg_fields:
        raw = values.get(reg_id)
        if raw is None:
            continue
        scratch_reg = SCRATCH + (reg_id - base)
        await _send(_build_hw(scratch_reg, raw, session), 2)

    await _send(_build_hw(SCRATCH, he_selector, session), 2)

    payload    = bytearray(18)
    payload[1] = mode
    payload[3] = he_selector & 0xFF
    payload[5] = 0x01
    await _send(_build_packet(b'HE', bytes(payload), session), 5)


async def _write_setting(reg_id: int, value: int) -> None:
    """Write one register; treats no response as success (not all machines ACK HW writes)."""
    session, pending, inbox = await _ensure_session()
    loop   = asyncio.get_running_loop()
    client = _paired['client']
    inbox[0]   = bytearray()
    pending[0] = loop.create_future()
    await client.write_gatt_char(AD03, _build_hw(reg_id, value, session), response=True)
    try:
        raw = await asyncio.wait_for(pending[0], timeout=2)
    except asyncio.TimeoutError:
        return

    parsed = _decode_packet(raw)
    if parsed is None:
        return
    cmd, _ = parsed
    if cmd[0:1] == b'N':
        raise RuntimeError(f"Machine rejected write for register {reg_id}")
    if cmd[0:1] != b'A':
        raise RuntimeError(f"HW write got unexpected response: {cmd.hex()}")


async def _do_poll_hx() -> tuple[int, int, int, int] | None:
    """Send HX and return (process, subProcess, message, progress), or None on failure."""
    session, pending, inbox = await _ensure_session()
    loop   = asyncio.get_running_loop()
    client = _paired['client']
    inbox[0]   = bytearray()
    pending[0] = loop.create_future()
    await client.write_gatt_char(AD03, _build_hx(session), response=False)
    try:
        raw = await asyncio.wait_for(pending[0], timeout=3)
    except asyncio.TimeoutError:
        return None
    parsed = _decode_packet(raw)
    if parsed and len(parsed[1]) >= 8:
        return struct.unpack('>hhhh', parsed[1][:8])
    return None


async def _do_cancel_brew() -> bool:
    """Send HZ cancel command. Returns True if machine ACKs."""
    session, pending, inbox = await _ensure_session()
    loop   = asyncio.get_running_loop()
    client = _paired['client']
    inbox[0]   = bytearray()
    pending[0] = loop.create_future()
    await client.write_gatt_char(AD03, _build_hz(session), response=False)
    try:
        raw = await asyncio.wait_for(pending[0], timeout=3)
    except asyncio.TimeoutError:
        return False
    parsed = _decode_packet(raw)
    return parsed is not None and parsed[0][0:1] == b'A'


# Bluetooth availability check
def _check_bt() -> str | None:
    bt_path = pathlib.Path('/sys/class/bluetooth')
    if not bt_path.exists() or not any(bt_path.glob('hci*')):
        return "No Bluetooth adapter found."
    return None


# Key reader
def _parse_escape(read_char) -> str | None:
    """Decode an ANSI arrow escape sequence via two read_char() calls.
    read_char must return '' on timeout/unavailability."""
    ch2 = read_char()
    if not ch2:
        return None
    ch3 = read_char()
    if not ch3:
        return None
    if ch2 == "[":
        if ch3 == "A": return "UP"
        if ch3 == "B": return "DOWN"
        if ch3 == "C": return "RIGHT"
        if ch3 == "D": return "LEFT"
    return None


def _read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            arrow = _parse_escape(lambda: sys.stdin.read(1))
            if arrow:
                return arrow
        if ch == "\x03":        return "QUIT"
        if ch in ("\r", "\n"): return "ENTER"
        if ch in ("q", "Q"):   return "QUIT"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_nonblocking(timeout: float = 0.15) -> str | None:
    """Return the next keypress without blocking longer than timeout, or None."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            def _timed_read():
                rr, _, _ = select.select([sys.stdin], [], [], 0.05)
                return sys.stdin.read(1) if rr else ''
            arrow = _parse_escape(_timed_read)
            if arrow:
                return arrow
        if ch == "\x03":        return "QUIT"
        if ch in ("\r", "\n"): return "ENTER"
        if ch in ("q", "Q"):   return "QUIT"
        if ch in ("c", "C"):   return "CANCEL"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _wait_for_ack() -> None:
    while True:
        if _read_key() in ("ENTER", "QUIT"):
            return


# Menu items
MAIN_MENU = [
    ("Scan and pair machine.", "scan"),
    ("Query statistics.",      "stats"),
    ("Change settings.",       "settings"),
    ("Change recipes.",        "recipes"),
    ("Brew beverages.",        "brew"),
    ("Leave.",                 "quit"),
]


# Rendering helpers
def _render_to_str(content: Text) -> tuple[str, int]:
    buf = StringIO()
    tmp = Console(file=buf, highlight=False, force_terminal=True)
    tmp.print(content)
    rendered = buf.getvalue()
    return rendered, rendered.count("\n")


def _render_header(section: str, status: str = "Not connected!") -> tuple[str, int]:
    t = Text()
    t.append("\n")
    t.append("     )))\n")
    t.append("    (((\n")
    t.append("  +-----+     ")
    t.append("Nivona-CLI\n")
    t.append("  |     |]\n")
    t.append("  `-----'\n")
    t.append("\n")
    stat = f"Status: {status}"
    gap  = 52 - len(section) - len(stat)
    t.append(f" {section}" + " " * (gap - 1) + stat + "\n")
    t.append("─" * 52 + "\n")
    return _render_to_str(t)


def _render_main_body(cursor: int) -> tuple[str, int]:
    t = Text()
    for i, (label, _) in enumerate(MAIN_MENU):
        t.append(f" [*] {label}\n" if i == cursor else f" [ ] {label}\n")
    t.append("\n")
    return _render_to_str(t)



def _render_spinning_body(message: str) -> tuple[str, int]:
    spin = _spinner.render(time.time()).plain
    t = Text()
    t.append(f"\n  {message}{spin}\n\n")
    return _render_to_str(t)


def _render_error_body(msg: str) -> tuple[str, int]:
    t = Text()
    t.append(f"\n  Error: {msg}\n\n")
    t.append("  Press Enter or q to return.\n\n")
    return _render_to_str(t)


def _render_scan_body(phase: str, **kw) -> tuple[str, int]:
    t = Text()
    if phase == 'scanning':
        t.append(f"\n  Scanning for Nivona machines... ({kw.get('seconds', 0)}s)\n\n")
    elif phase == 'connecting':
        spin = _spinner.render(time.time()).plain
        t.append(f"\n  Machine: {kw['name']}\n")
        t.append(f"  MAC:     {kw['mac']}\n")
        t.append(f"  NICR:    {kw['nicr']}\n")
        t.append(f"  Family:  {kw['family'] or 'unknown'}\n\n")
        t.append(f"  Connecting and pairing{spin}\n\n")
    elif phase == 'paired':
        t.append(f"\n  Machine: {kw['name']}\n")
        t.append(f"  MAC:     {kw['mac']}\n")
        t.append(f"  NICR:    {kw['nicr']}\n")
        t.append(f"  Family:  {kw['family'] or 'unknown'}\n\n")
        t.append("  Paired successfully.\n\n")
        t.append("  Press Enter or q to return.\n\n")
    return _render_to_str(t)


def _render_stats_body(rows: list[tuple[str, int]]) -> tuple[str, int]:
    t = Text()
    label_w = max((len(label) for label, _ in rows), default=16) + 2
    for label, value in rows:
        t.append(f"  {label + ':':<{label_w}} {value}\n")
    t.append("\n")
    t.append("  Press Enter or q to return.\n\n")
    return _render_to_str(t)


def _render_settings_body(registers, values: dict[int, int], cursor: int,
                          extras: tuple[str, ...] = (),
                          prefix_extras: tuple[str, ...] = ()) -> tuple[str, int]:
    t = Text()
    label_w = max((len(lbl) for _, lbl, _ in registers), default=16) + 2
    n_prefix = len(prefix_extras)
    for i, extra_label in enumerate(prefix_extras):
        marker = "[*]" if cursor == i else "[ ]"
        t.append(f" {marker} {extra_label}\n")
    for i, (reg_id, label, options) in enumerate(registers):
        current_raw = values.get(reg_id)
        if isinstance(options, _Slider):
            current_str = f"{current_raw / options.scale:.0f} ml" if current_raw is not None else "?"
        else:
            current_str = options.get(current_raw, "?") if current_raw is not None else "?"
        marker = "[*]" if i + n_prefix == cursor else "[ ]"
        t.append(f" {marker} {label + ':':<{label_w}} {current_str}\n")
    n = len(registers)
    for i, extra_label in enumerate(extras):
        marker = "[*]" if cursor == n_prefix + n + i else "[ ]"
        t.append(f" {marker} {extra_label}\n")
    marker = "[*]" if cursor == n_prefix + n + len(extras) else "[ ]"
    t.append(f" {marker} Back\n")
    t.append("\n")
    return _render_to_str(t)


def _render_recipe_list_body(recipes: list[tuple[int, str]], cursor: int) -> tuple[str, int]:
    t = Text()
    for i, (_, title) in enumerate(recipes):
        t.append(f" [*] {title}\n" if i == cursor else f" [ ] {title}\n")
    marker = "[*]" if cursor == len(recipes) else "[ ]"
    t.append(f" {marker} Back\n")
    t.append("\n")
    return _render_to_str(t)


def _render_slider_body(raw: int, slider: _Slider) -> tuple[str, int]:
    display = raw / slider.scale
    t = Text()
    t.append(f"\n  ◄  {display:.0f} ml  ►\n\n")
    t.append("  ← → adjust  ·  Enter save  ·  q cancel\n\n")
    return _render_to_str(t)


def _render_option_body(label: str, options: dict, current_raw: int | None,
                        cursor: int) -> tuple[str, int]:
    t = Text()
    items = list(options.items())
    for i, (code, name) in enumerate(items):
        marker = "[*]" if i == cursor else "[ ]"
        cur_marker = " *" if code == current_raw else "  "
        t.append(f" {marker}{cur_marker}{name}\n")
    # Back row
    marker = "[*]" if cursor == len(items) else "[ ]"
    t.append(f" {marker}  Back\n")
    t.append("\n")
    return _render_to_str(t)


def _edit_field(last: int, section: str, status: str,
                label: str, options, current_raw: int | None) -> tuple[int, int | None]:
    """Open slider or option picker for one field.
    Returns (last, new_raw) or (last, None) if cancelled or value unchanged."""
    if isinstance(options, _Slider):
        edit_raw = max(options.lo, min(options.hi,
                       current_raw if current_raw is not None else options.lo))
        while True:
            h, _ = _render_header(section, status)
            b, _ = _render_slider_body(edit_raw, options)
            last = _draw(h + b, last)
            key = _read_key()
            if   key == "RIGHT":  edit_raw = min(options.hi, edit_raw + options.step)
            elif key == "LEFT":   edit_raw = max(options.lo, edit_raw - options.step)
            elif key == "QUIT":   return last, None
            elif key == "ENTER":  return last, (edit_raw if edit_raw != current_raw else None)
    else:
        items   = list(options.items())
        opt_cur = next((i for i, (c, _) in enumerate(items) if c == current_raw), 0)
        n_opts  = len(items)
        while True:
            h, _ = _render_header(section, status)
            b, _ = _render_option_body(label, options, current_raw, opt_cur)
            last = _draw(h + b, last)
            key = _read_key()
            if   key in ("UP",   "k"): opt_cur = (opt_cur - 1) % (n_opts + 1)
            elif key in ("DOWN", "j"): opt_cur = (opt_cur + 1) % (n_opts + 1)
            elif key == "QUIT":        return last, None
            elif key == "ENTER":
                if opt_cur == n_opts:  return last, None
                new = items[opt_cur][0]
                return last, (new if new != current_raw else None)


def _draw(rendered: str, last_lines: int) -> int:
    if last_lines:
        sys.stdout.write(f"\033[{last_lines}A")
    sys.stdout.write("\033[J")
    sys.stdout.write(rendered)
    sys.stdout.flush()
    return rendered.count("\n")


# Spinner helper
def _run_with_spinner(last: int, section: str, status: str,
                      message: str, fn) -> tuple[int, object, Exception | None]:
    """Run fn() in a thread while showing a spinner. Returns (last, result, exc)."""
    result = [None]
    exc    = [None]
    done   = [False]

    def _t():
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e
        finally:
            done[0] = True

    threading.Thread(target=_t, daemon=True).start()

    while not done[0]:
        h, _ = _render_header(section, status)
        b, _ = _render_spinning_body(message)
        last = _draw(h + b, last)
        time.sleep(0.12)

    return last, result[0], exc[0]


# UI utility helpers
def _show_error(last: int, section: str, status: str, msg: str) -> tuple[int, str]:
    """Render an error, wait for ack, and return (last, status) unchanged."""
    h, _ = _render_header(section, status)
    b, _ = _render_error_body(msg)
    last = _draw(h + b, last)
    _wait_for_ack()
    return last, status


def _require_paired_and_family(
    last: int, section: str, status: str,
    table: dict, table_name: str,
) -> tuple[int, str] | None:
    """Return None if a machine is paired and its family is in table.
    Otherwise render the appropriate error and return (last, status) for the caller to return."""
    if not _paired:
        return _show_error(last, section, status,
                           "No machine paired. Use 'Scan and pair machine' first.")
    family = _paired.get('family')
    if not family or family not in table:
        return _show_error(last, section, status,
                           f"No {table_name} table for family '{family}'.")
    return None


def _build_reg_fields(
    family: str, selector: int,
    enum_flds: list, ml_by_sel: dict, excl_by_sel: dict,
) -> list:
    """Expand per-family recipe field list into (reg_id, label, options) triples."""
    base = 10000 + selector * 100
    excl = excl_by_sel.get(selector, set())
    return (
        [(base + off, lbl, opts) for off, lbl, opts in enum_flds if off not in excl] +
        [(base + off, lbl, opts) for off, lbl, opts in ml_by_sel.get(selector, [])]
    )


# Brew active monitor
def _render_brew_active_body(bevname: str,
                             hx: tuple[int, int, int, int] | None,
                             cancel_sent: bool) -> tuple[str, int]:
    t = Text()
    if cancel_sent:
        t.append(f"\n  Cancelling {bevname}...\n\n")
        t.append("  Press Enter or q to go back.\n\n")
        return _render_to_str(t)
    if hx is None:
        t.append(f"\n  {bevname} starting...\n\n")
        t.append("  c cancel  *  q back\n\n")
        return _render_to_str(t)
    process, _sub, message, progress = hx
    proc_lbl = _PROCESS_LABELS.get(process, f"process {process}")
    msg_lbl  = _MESSAGE_LABELS.get(message, "")
    brewing  = process in (4, 11)
    t.append(f"\n  {bevname}: {proc_lbl}\n")
    if message and msg_lbl:
        t.append(f"  [!] {msg_lbl}\n")
    if progress:
        t.append(f"  Progress: {progress}%\n")
    t.append("\n")
    if brewing:
        t.append("  c cancel  *  q back\n\n")
    else:
        t.append("  Done — press Enter or q to go back.\n\n")
    return _render_to_str(t)


def _run_brew_active(last: int, status: str, bevname: str) -> int:
    """Poll machine status after a brew command; offer cancel via HZ."""
    cancel_sent  = False
    hx: tuple[int, int, int, int] | None = None
    saw_active   = False
    last_poll    = 0.0

    while True:
        now = time.time()
        if now - last_poll >= 0.8:
            try:
                hx = _run_ble(_do_poll_hx())
            except Exception:
                pass
            last_poll = now

        if hx is not None and hx[0] in (4, 11):
            saw_active = True

        finished = saw_active and hx is not None and hx[0] not in (4, 11)

        h, _ = _render_header(bevname, status)
        b, _ = _render_brew_active_body(bevname, hx, cancel_sent)
        last = _draw(h + b, last)

        key = _read_key_nonblocking(0.15)

        if key == "QUIT":
            break
        if key == "ENTER" and (finished or cancel_sent):
            break
        if key == "CANCEL" and not cancel_sent:
            cancel_sent = True
            try:
                _run_ble(_do_cancel_brew())
            except Exception:
                pass

    return last


# Scan submenu
def _run_scan(last: int, status: str) -> tuple[int, str]:
    global _paired
    section = "Scan and pair machine"

    bt_err = _check_bt()
    if bt_err:
        return _show_error(last, section, status, bt_err)

    result   = [None]
    scan_exc = [None]

    def _scan_thread():
        try:
            result[0] = _run_ble(_do_scan(5.0))
        except Exception as e:
            scan_exc[0] = e

    scan_thread = threading.Thread(target=_scan_thread, daemon=True)
    scan_thread.start()

    for remaining in range(5, 0, -1):
        h, _ = _render_header(section, status)
        b, _ = _render_scan_body('scanning', seconds=remaining)
        last = _draw(h + b, last)
        time.sleep(1)

    scan_thread.join()

    if scan_exc[0]:
        return _show_error(last, section, status, str(scan_exc[0]))

    device = result[0]
    if device is None:
        return _show_error(last, section, status, "No Nivona machine found.")

    name   = device.name or ""
    nicr   = detect_nicr(name) or name
    family = detect_family(name)

    pair_done = [False]
    pair_exc  = [None]

    def _pair_thread():
        try:
            _run_ble(_do_pair(device))
        except Exception as e:
            pair_exc[0] = e
        finally:
            pair_done[0] = True

    threading.Thread(target=_pair_thread, daemon=True).start()

    while not pair_done[0]:
        h, _ = _render_header(section, status)
        b, _ = _render_scan_body('connecting', mac=device.address, name=name,
                                 nicr=nicr, family=family)
        last = _draw(h + b, last)
        time.sleep(0.12)

    if pair_exc[0]:
        return _show_error(last, section, status, str(pair_exc[0]))

    _paired    = {'device': device, 'mac': device.address, 'name': name,
                  'nicr': nicr, 'family': family}
    new_status = "Connected!"

    h, _ = _render_header(section, new_status)
    b, _ = _render_scan_body('paired', mac=device.address, name=name,
                             nicr=nicr, family=family)
    last = _draw(h + b, last)
    _wait_for_ack()
    return last, new_status


# Statistics submenu
def _run_stats(last: int, status: str) -> tuple[int, str]:
    section = "Query statistics"

    err = _require_paired_and_family(last, section, status, STATS, "statistics")
    if err is not None:
        return err
    family = _paired['family']

    last, rows, fetch_err = _run_with_spinner(
        last, section, status, "Fetching statistics",
        lambda: _run_ble(_fetch_stats(family))
    )
    if fetch_err:
        return _show_error(last, section, status, str(fetch_err))

    h, _ = _render_header(section, status)
    b, _ = _render_stats_body(rows)
    last = _draw(h + b, last)
    _wait_for_ack()
    return last, status


# Settings submenu
def _run_settings(last: int, status: str) -> tuple[int, str]:
    section = "Change settings"

    err = _require_paired_and_family(last, section, status, SETTINGS, "settings")
    if err is not None:
        return err
    family = _paired['family']

    registers = SETTINGS[family]

    last, values, fetch_err = _run_with_spinner(
        last, section, status, "Fetching current settings",
        lambda: _run_ble(_fetch_settings(family))
    )
    if fetch_err:
        return _show_error(last, section, status, str(fetch_err))

    cursor = 0
    n      = len(registers)

    while True:
        h, _ = _render_header(section, status)
        b, _ = _render_settings_body(registers, values, cursor)
        last = _draw(h + b, last)

        key = _read_key()

        if key in ("UP", "k"):
            cursor = (cursor - 1) % (n + 1)
        elif key in ("DOWN", "j"):
            cursor = (cursor + 1) % (n + 1)
        elif key == "QUIT":
            break
        elif key == "ENTER":
            if cursor == n:   # Back
                break

            reg_id, label, options = registers[cursor]
            last, new_raw = _edit_field(last, label, status, label, options, values.get(reg_id))
            if new_raw is None:
                continue

            last, _, save_err = _run_with_spinner(
                last, label, status, f"Saving {label}",
                lambda r=reg_id, v=new_raw: _run_ble(_write_setting(r, v))
            )
            if save_err:
                last, status = _show_error(last, section, status, str(save_err))
            else:
                values[reg_id] = new_raw

    return last, status


# Recipes submenu
def _run_recipes(last: int, status: str) -> tuple[int, str]:
    section = "Change recipes"

    err = _require_paired_and_family(last, section, status, RECIPES, "recipe")
    if err is not None:
        return err
    family = _paired['family']

    recipes     = RECIPES[family]
    enum_flds   = RECIPE_FIELDS.get(family, [])
    ml_by_sel   = RECIPE_ML.get(family, {})
    excl_by_sel = RECIPE_ENUM_EXCLUDE.get(family, {})
    cursor      = 0
    n           = len(recipes)

    while True:
        h, _ = _render_header(section, status)
        b, _ = _render_recipe_list_body(recipes, cursor)
        last = _draw(h + b, last)

        key = _read_key()

        if key in ("UP", "k"):
            cursor = (cursor - 1) % (n + 1)
        elif key in ("DOWN", "j"):
            cursor = (cursor + 1) % (n + 1)
        elif key == "QUIT":
            break
        elif key == "ENTER":
            if cursor == n:   # Back
                break

            selector, bevname = recipes[cursor]
            reg_fields = _build_reg_fields(family, selector, enum_flds, ml_by_sel, excl_by_sel)

            last, values, fetch_err = _run_with_spinner(
                last, bevname, status, f"Fetching {bevname} recipe",
                lambda s=selector: _run_ble(_fetch_recipe(family, s))
            )
            if fetch_err:
                last, status = _show_error(last, section, status, str(fetch_err))
                continue

            field_cursor = 0
            n_fields     = len(reg_fields)
            n_total      = n_fields + 2   # fields + Reset + Back

            while True:
                h, _ = _render_header(bevname, status)
                b, _ = _render_settings_body(reg_fields, values, field_cursor,
                                             extras=("Reset to defaults",))
                last = _draw(h + b, last)

                key2 = _read_key()

                if key2 in ("UP", "k"):
                    field_cursor = (field_cursor - 1) % n_total
                elif key2 in ("DOWN", "j"):
                    field_cursor = (field_cursor + 1) % n_total
                elif key2 == "QUIT":
                    break
                elif key2 == "ENTER":
                    if field_cursor == n_fields + 1:   # Back
                        break

                    if field_cursor == n_fields:       # Reset to defaults
                        last, _, reset_err = _run_with_spinner(
                            last, bevname, status, "Resetting to defaults",
                            lambda s=selector: _run_ble(_reset_recipe(family, s))
                        )
                        if reset_err:
                            last, status = _show_error(last, bevname, status, str(reset_err))
                        else:
                            last, new_values, fetch_err = _run_with_spinner(
                                last, bevname, status, f"Fetching {bevname} recipe",
                                lambda s=selector: _run_ble(_fetch_recipe(family, s))
                            )
                            if not fetch_err:
                                values = new_values
                        continue

                    reg_id, label, options = reg_fields[field_cursor]
                    last, new_raw = _edit_field(
                        last, f"{bevname} / {label}", status,
                        label, options, values.get(reg_id))
                    if new_raw is None:
                        continue

                    last, _, save_err = _run_with_spinner(
                        last, f"{bevname} / {label}", status,
                        f"Saving {label}",
                        lambda r=reg_id, v=new_raw: _run_ble(_write_setting(r, v))
                    )
                    if save_err:
                        last, status = _show_error(last, bevname, status, str(save_err))
                    else:
                        values[reg_id] = new_raw

    return last, status


# Brew submenu
def _run_brew(last: int, status: str) -> tuple[int, str]:
    section = "Brew beverages"

    err = _require_paired_and_family(last, section, status, RECIPES, "recipe")
    if err is not None:
        return err
    family = _paired['family']

    recipes     = RECIPES[family]
    enum_flds   = RECIPE_FIELDS.get(family, [])
    ml_by_sel   = RECIPE_ML.get(family, {})
    excl_by_sel = RECIPE_ENUM_EXCLUDE.get(family, {})
    cursor      = 0
    n           = len(recipes)

    while True:
        h, _ = _render_header(section, status)
        b, _ = _render_recipe_list_body(recipes, cursor)
        last = _draw(h + b, last)

        key = _read_key()

        if key in ("UP", "k"):
            cursor = (cursor - 1) % (n + 1)
        elif key in ("DOWN", "j"):
            cursor = (cursor + 1) % (n + 1)
        elif key == "QUIT":
            break
        elif key == "ENTER":
            if cursor == n:   # Back
                break

            selector, bevname = recipes[cursor]
            reg_fields = _build_reg_fields(family, selector, enum_flds, ml_by_sel, excl_by_sel)

            last, values, fetch_err = _run_with_spinner(
                last, bevname, status, f"Fetching {bevname} recipe",
                lambda s=selector: _run_ble(_fetch_recipe(family, s))
            )
            if fetch_err:
                last, status = _show_error(last, section, status, str(fetch_err))
                continue

            field_cursor = 0
            n_fields     = len(reg_fields)
            n_total      = n_fields + 2   # Brew + fields + Back

            while True:
                h, _ = _render_header(bevname, status)
                b, _ = _render_settings_body(reg_fields, values, field_cursor,
                                             prefix_extras=("Brew",))
                last = _draw(h + b, last)

                key2 = _read_key()

                if key2 in ("UP", "k"):
                    field_cursor = (field_cursor - 1) % n_total
                elif key2 in ("DOWN", "j"):
                    field_cursor = (field_cursor + 1) % n_total
                elif key2 == "QUIT":
                    break
                elif key2 == "ENTER":
                    if field_cursor == n_fields + 1:   # Back
                        break

                    if field_cursor == 0:              # Brew
                        last, _, brew_err = _run_with_spinner(
                            last, bevname, status, f"Brewing {bevname}",
                            lambda s=selector, rf=reg_fields, v=values: _run_ble(
                                _brew(family, s, rf, v)
                            )
                        )
                        if brew_err:
                            last, status = _show_error(last, bevname, status, str(brew_err))
                        else:
                            last = _run_brew_active(last, status, bevname)
                        break

                    # Edit a field locally — no BLE write until Brew
                    reg_id, label, options = reg_fields[field_cursor - 1]
                    last, new_raw = _edit_field(
                        last, f"{bevname} / {label}", status,
                        label, options, values.get(reg_id))
                    if new_raw is not None:
                        values[reg_id] = new_raw

    return last, status


# Main loop
def run() -> None:
    cursor = 0
    last = 0
    status = "Not connected!"

    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            h, _ = _render_header("Main Menu", status)
            b, _ = _render_main_body(cursor)
            last = _draw(h + b, last)

            key = _read_key()

            if key in ("UP", "k"):
                cursor = (cursor - 1) % len(MAIN_MENU)
            elif key in ("DOWN", "j"):
                cursor = (cursor + 1) % len(MAIN_MENU)
            elif key == "QUIT":
                break
            elif key == "ENTER":
                _, handler = MAIN_MENU[cursor]

                if handler == "quit":
                    break
                elif handler == "scan":
                    last, status = _run_scan(last, status)
                elif handler == "stats":
                    last, status = _run_stats(last, status)
                elif handler == "settings":
                    last, status = _run_settings(last, status)
                elif handler == "recipes":
                    last, status = _run_recipes(last, status)
                elif handler == "brew":
                    last, status = _run_brew(last, status)

    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


if __name__ == "__main__":
    run()
