# -*- coding: utf-8 -*-
"""Configuration: font families, weights and algorithm defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

SONG = "\u5b8b\u4f53"            # 宋体
KAI = "\u6977\u4f53"             # 楷体
TIMES = "Times New Roman"
CONSOLAS = "Consolas"

DEFAULT_FONT_DIR = os.environ.get("DOCX_OBF_FONTDIR", r"C:\Windows\Fonts")

SLOTS = ("ascii", "hAnsi", "eastAsia", "cs")
THEME_SLOTS = {
    "ascii": "asciiTheme",
    "hAnsi": "hAnsiTheme",
    "eastAsia": "eastAsiaTheme",
    "cs": "cstheme",
}
WEIGHT_TAGS = {
    "regular": "embedRegular",
    "bold": "embedBold",
    "italic": "embedItalic",
    "boldItalic": "embedBoldItalic",
}
WEIGHT_SUBFAMILY = {
    "regular": "Regular",
    "bold": "Bold",
    "italic": "Italic",
    "boldItalic": "Bold Italic",
}
WEIGHT_ORDER = ("regular", "bold", "italic", "boldItalic")

# Never scramble whitespace: it keeps line breaking and justification intact.
WHITESPACE = set(" \t\n\r\x0b\x0c\u00a0\u3000")

# Scramble letters, CJK ideographs and digits only.  Punctuation keeps its
# codepoint so Word's punctuation compression / line breaking is unchanged.
OBFUSCATE_CATEGORIES = {"Lu", "Ll", "Lt", "Lo", "Nd"}

# Fonts kept by the normalizer.  Text in these fonts is eligible for
# obfuscation unless it is listed only in KEEP_EA (e.g. 楷体 is preserved but
# deliberately *not* embedded/scrambled).
KEEP_ALL = {KAI, TIMES, CONSOLAS, SONG}
KEEP_EA = {KAI, SONG}


@dataclass(frozen=True)
class Face:
    """One physical source font file (TTC index when needed)."""

    file: str
    index: Optional[int] = None


@dataclass(frozen=True)
class Family:
    """A source family and the faces used for the four font weights."""

    key: str
    display_name: str
    faces: Dict[str, Face]
    panose: str
    charset: str
    family: str
    pitch: str

    def face(self, weight: str) -> Optional[Face]:
        return self.faces.get(weight)


FAMILIES: Dict[str, Family] = {
    "simsun": Family(
        key="simsun",
        display_name=SONG,
        faces={
            "regular": Face("simsun.ttc", 0),
            "bold": Face("simsun.ttc", 0),
            "italic": Face("simsun.ttc", 0),
            "boldItalic": Face("simsun.ttc", 0),
        },
        panose="02010600030101010101",
        charset="86",
        family="auto",
        pitch="default",
    ),
    "times": Family(
        key="times",
        display_name=TIMES,
        faces={
            "regular": Face("times.ttf"),
            "bold": Face("timesbd.ttf"),
            "italic": Face("timesi.ttf"),
            "boldItalic": Face("timesbi.ttf"),
        },
        panose="02020603050405020304",
        charset="00",
        family="roman",
        pitch="variable",
    ),
    "consola": Family(
        key="consola",
        display_name=CONSOLAS,
        faces={
            "regular": Face("consola.ttf"),
            "bold": Face("consolab.ttf"),
            "italic": Face("consolai.ttf"),
            "boldItalic": Face("consolaz.ttf"),
        },
        panose="020B0609020204030204",
        charset="00",
        family="modern",
        pitch="fixed",
    ),
}

# Text whose effective font is the key gets scrambled with that family.
CHOSEN_FAMILIES = {
    SONG: "simsun",
    TIMES: "times",
    CONSOLAS: "consola",
}


@dataclass
class Options:
    """Algorithm knobs shared by the normalizer / obfuscator / verifier."""

    font_dir: str = DEFAULT_FONT_DIR
    seed: int = 20261011
    prefix: str = "_5dbwat4_"
    # one fake font (bucket) per N scrambled characters of a family
    bucket_divisor: int = 10
    # 6 random bytes -> 8 urlsafe-base64 chars; total name length 17 (< 31)
    name_bytes: int = 6
    verify_samples: int = 60
