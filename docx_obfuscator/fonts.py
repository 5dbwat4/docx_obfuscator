# -*- coding: utf-8 -*-
"""Font subsetting, fake face construction and OOXML odttf obfuscation."""
from __future__ import annotations

import io
import os
from typing import Dict, Iterable, List, Optional, Tuple

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.subset import Options as SubsetOptions
from fontTools.subset import Subsetter
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from .config import FAMILIES, Family, Face


def obfuscate(font_bytes: bytes, guid: str) -> bytes:
    """Apply the OOXML embedded-font XOR (first 32 bytes, reversed GUID)."""
    hex_key = guid.strip("{}").replace("-", "")
    key = bytes.fromhex(hex_key)[::-1]
    data = bytearray(font_bytes)
    for i in range(min(32, len(data))):
        data[i] ^= key[i % 16]
    return bytes(data)


def deobfuscate(odttf_bytes: bytes, guid: str) -> bytes:
    return obfuscate(odttf_bytes, guid)


class FontLibrary:
    """Caches source fonts / cmaps / blank skeletons for repeated face builds."""

    def __init__(self, font_dir: str, families: Optional[Dict[str, Family]] = None):
        self.font_dir = font_dir
        self.families = families or FAMILIES
        self._cmaps: Dict[Tuple[str, Optional[int]], dict] = {}
        self._sources: Dict[Tuple[str, Optional[int]], Tuple[TTFont, object]] = {}
        self._skeletons: Dict[str, bytes] = {}
        self._coverage: Dict[str, set] = {}

    # ------------------------------------------------------------------ source
    def _path(self, face: Face) -> str:
        return os.path.join(self.font_dir, face.file)

    def cmap(self, face: Face) -> dict:
        key = (face.file, face.index)
        if key not in self._cmaps:
            index = face.index if face.index is not None else -1
            font = TTFont(self._path(face), fontNumber=index)
            self._cmaps[key] = font.getBestCmap()
            font.close()
        return self._cmaps[key]

    def family_coverage(self, family_key: str) -> set:
        if family_key not in self._coverage:
            coverage = set()
            for face in self.families[family_key].faces.values():
                coverage.update(self.cmap(face).keys())
            self._coverage[family_key] = coverage
        return self._coverage[family_key]

    def choose_source(self, family_key: str, weight: str, char: str) -> Face:
        """Pick a face that actually has a glyph for ``char``."""
        candidates = [
            (family_key, weight),
            (family_key, "regular"),
            ("simsun", "regular"),
            ("times", "regular"),
            ("consola", "regular"),
        ]
        for fam, wt in candidates:
            family = self.families.get(fam)
            if not family or wt not in family.faces:
                continue
            face = family.faces[wt]
            if ord(char) in self.cmap(face):
                return face
        return self.families["simsun"].faces["regular"]

    def check_available(self) -> List[str]:
        missing = []
        seen = set()
        for family in self.families.values():
            for face in family.faces.values():
                key = (face.file, face.index)
                if key in seen:
                    continue
                seen.add(key)
                if not os.path.exists(self._path(face)):
                    missing.append(face.file)
        return missing

    # ----------------------------------------------------------------- skeleton
    def _source_font(self, face: Face) -> Tuple[TTFont, object]:
        key = (face.file, face.index)
        if key not in self._sources:
            index = face.index if face.index is not None else -1
            font = TTFont(self._path(face), fontNumber=index)
            self._sources[key] = (font, font.getGlyphSet())
        return self._sources[key]

    def _blank_base(self, family_key: str) -> TTFont:
        if family_key not in self._skeletons:
            face = self.families[family_key].faces["regular"]
            base = TTFont(
                self._path(face),
                fontNumber=(face.index if face.index is not None else -1),
            )
            subsetter = Subsetter(options=SubsetOptions())
            subsetter.populate(unicodes=[32])
            subsetter.subset(base)
            for table in ("hdmx", "LTSH", "VDMX", "meta", "kern", "vmtx", "vhea", "VORG"):
                if table in base:
                    del base[table]
            buffer = io.BytesIO()
            base.save(buffer)
            base.close()
            self._skeletons[family_key] = buffer.getvalue()
        return TTFont(io.BytesIO(self._skeletons[family_key]))

    # ------------------------------------------------------------------- build
    def build_face(
        self,
        entries: Iterable[Tuple[str, str, Face]],
        fake_name: str,
        subfamily: str,
        family_key: str,
    ) -> Tuple[bytes, int]:
        """Build one tiny font whose cmap maps scrambled codes to real outlines.

        ``entries`` items are ``(original_char, mapped_char, source_face)``.
        The fake family keeps the regular face's vertical metrics so line
        height and page layout stay identical to the normalized document.
        """
        base = self._blank_base(family_key)
        upem = base["head"].unitsPerEm
        bold = subfamily in ("Bold", "Bold Italic")
        italic = "Italic" in subfamily
        embolden = max(1, int(round(upem * 0.018)))
        order = list(base.getGlyphOrder())
        cmap: Dict[int, str] = {}

        for index, (char, mapped, face) in enumerate(entries):
            source, glyph_set = self._source_font(face)
            scale = upem / source["head"].unitsPerEm
            source_cmap = source.getBestCmap()
            cp = ord(char)
            if cp not in source_cmap:
                continue
            glyph_name = source_cmap[cp]
            decomposed = DecomposingRecordingPen(glyph_set)
            glyph_set[glyph_name].draw(decomposed)
            pen = TTGlyphPen(None)
            # SimSun has no real bold face: fake it by overprinting with an
            # offset (right/down), which matches the original experimental
            # pipeline and keeps the glyph width unchanged.
            faux = bold and family_key == "simsun"
            offsets = (
                [(0, 0), (embolden, 0), (0, embolden), (embolden, embolden)]
                if faux
                else [(0, 0)]
            )
            for ox, oy in offsets:
                decomposed.replay(TransformPen(pen, (scale, 0, 0, scale, ox, oy)))
            new_name = "g%X_%d" % (cp, index)
            base["glyf"].glyphs[new_name] = pen.glyph()
            advance, lsb = source["hmtx"][glyph_name]
            base["hmtx"].metrics[new_name] = (
                int(round(advance * scale)),
                int(round(lsb * scale)),
            )
            order.append(new_name)
            base.setGlyphOrder(order)
            cmap[ord(mapped)] = new_name

        glyph_order = base.getGlyphOrder()
        base["glyf"].glyphOrder = list(glyph_order)
        base.setGlyphOrder(list(glyph_order))
        base["maxp"].numGlyphs = len(glyph_order)

        cmap_table = newTable("cmap")
        cmap_table.tableVersion = 0
        windows = CmapSubtable.newSubtable(4)
        windows.platformID, windows.platEncID, windows.language = 3, 1, 0
        windows.cmap = dict(cmap)
        unicode = CmapSubtable.newSubtable(4)
        unicode.platformID, unicode.platEncID, unicode.language = 0, 3, 0
        unicode.cmap = dict(cmap)
        cmap_table.tables = [windows, unicode]
        base["cmap"] = cmap_table

        full = fake_name + " " + subfamily
        postscript = fake_name + "-" + subfamily.replace(" ", "")
        name = base["name"]
        for record in list(name.names):
            value = {
                1: fake_name,
                2: subfamily,
                4: full,
                6: postscript,
                16: fake_name,
                17: subfamily,
            }.get(record.nameID)
            if value is not None:
                name.setName(value, record.nameID, record.platformID, record.platEncID, record.langID)
        for platform, encoding, language in ((3, 1, 0x409), (1, 0, 0)):
            name.setName(fake_name, 1, platform, encoding, language)
            name.setName(subfamily, 2, platform, encoding, language)
            name.setName(full, 4, platform, encoding, language)
            name.setName(postscript, 6, platform, encoding, language)
            name.setName(fake_name, 16, platform, encoding, language)
            name.setName(subfamily, 17, platform, encoding, language)

        os2 = base["OS/2"]
        os2.usWeightClass = 700 if bold else 400
        selection = 0
        if italic:
            selection |= 0x01
        if bold:
            selection |= 0x20
        if not (bold or italic):
            selection |= 0x40
        os2.fsSelection = selection
        base["head"].macStyle = (0x01 if bold else 0) | (0x02 if italic else 0)
        # Allow embedding and claim complete coverage so Word never falls back.
        os2.fsType = 0
        os2.ulUnicodeRange1 = os2.ulUnicodeRange2 = 0xFFFFFFFF
        os2.ulUnicodeRange3 = os2.ulUnicodeRange4 = 0xFFFFFFFF
        os2.ulCodePageRange1 = os2.ulCodePageRange2 = 0xFFFFFFFF

        buffer = io.BytesIO()
        base.save(buffer)
        base.close()
        return buffer.getvalue(), len(cmap)
