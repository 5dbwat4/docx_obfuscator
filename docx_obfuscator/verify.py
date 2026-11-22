# -*- coding: utf-8 -*-
"""Stage 3: verify an obfuscated document against the normalized source.

Checks include XML validity, structural preservation (line breaks, drawings,
tables, formulas, hyperlinks, character styles, media), mapping consistency,
end-to-end display reconstruction, font-table/relationship plumbing, cmap
coverage and an outline spot-check (the scrambled codepoint must render the
same outline as the original character).
"""
from __future__ import annotations

import collections
import io
import os
import random
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

from .config import (
    OBFUSCATE_CATEGORIES,
    WHITESPACE,
    Options,
)
from .fonts import deobfuscate
from .ooxml import (
    RUN_RE,
    all_text,
    read_package,
    text_parts,
    unescape,
)

TAG_NAMES = {
    "Regular": "regular",
    "Bold": "bold",
    "Italic": "italic",
    "BoldItalic": "boldItalic",
}


class CheckLog:
    def __init__(self, path: Optional[str] = None):
        self.lines: List[str] = []
        self.ok = True
        self.path = path

    def write(self, line: str = "") -> None:
        self.lines.append(line)
        print(line)

    def check(self, condition, message: str) -> None:
        self.write(("  OK  " if condition else " FAIL") + "  " + message)
        self.ok = self.ok and bool(condition)

    def note(self, message: str) -> None:
        self.write("  NOTE  " + message)

    def finish(self) -> bool:
        self.write("")
        self.write("RESULT: " + ("ALL PASSED" if self.ok else "FAILED"))
        if self.path:
            with io.open(self.path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(self.lines) + "\n")
        return self.ok


def _runs(xml: str) -> List[str]:
    return RUN_RE.findall(xml)


def _collect_fonts(font_table: str, rels: str, prefix: str) -> Dict[str, Dict[str, Tuple[str, str]]]:
    relmap = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    fonts: Dict[str, Dict[str, Tuple[str, str]]] = {}
    for match in re.finditer(r'<w:font w:name="(%s[^"]*)">(.*?)</w:font>' % re.escape(prefix), font_table, re.S):
        name, body = match.group(1), match.group(2)
        embeds = {}
        for embed in re.finditer(r'<w:embed(\w+) r:id="([^"]+)" w:fontKey="([^"]+)"/>', body):
            weight = TAG_NAMES.get(embed.group(1))
            if weight:
                embeds[weight] = ("word/" + relmap[embed.group(2)], embed.group(3))
        fonts[name] = embeds
    return fonts


def verify(
    normalized: str,
    obfuscated: str,
    map_path: str,
    options: Optional[Options] = None,
    log_path: Optional[str] = None,
    samples: Optional[int] = None,
) -> bool:
    options = options or Options()
    log = CheckLog(log_path)
    log.write("== XML ==")
    src_order, source = read_package(normalized)
    out_order, output = read_package(obfuscated)

    for name in (
        "word/document.xml",
        "word/styles.xml",
        "word/fontTable.xml",
        "word/settings.xml",
        "word/_rels/fontTable.xml.rels",
        "[Content_Types].xml",
    ):
        try:
            import xml.etree.ElementTree as ET

            ET.fromstring(output[name])
            log.check(True, name)
        except Exception as exc:  # noqa: BLE001 - report parse error verbatim
            log.check(False, "%s -> %s" % (name, exc))

    src_names = text_parts(src_order, source)
    out_names = text_parts(out_order, output)
    log.check(out_names == src_names, "text parts unchanged: %s" % out_names)

    src_doc = source["word/document.xml"].decode("utf-8")
    out_doc = output["word/document.xml"].decode("utf-8")

    # ---- structural preservation -----------------------------------------
    log.write("")
    log.write("== structure ==")
    for tag in (
        "w:br",
        "w:drawing",
        "wp:inline",
        "w:bookmarkStart",
        "w:tbl",
        "w:sectPr",
        "m:oMath",
        "m:oMathPara",
        "mc:AlternateContent",
        "w:instrText",
        "w:fldChar",
    ):
        before = len(re.findall("<" + tag + r"\b", src_doc))
        after = len(re.findall("<" + tag + r"\b", out_doc))
        log.check(before == after, "%-20s %d -> %d" % (tag, before, after))

    src_math = re.findall(r"<m:t\b[^>]*>(.*?)</m:t>", src_doc, re.S)
    out_math = re.findall(r"<m:t\b[^>]*>(.*?)</m:t>", out_doc, re.S)
    log.check(src_math == out_math, "formula text unchanged (%d nodes)" % len(src_math))

    src_styles = collections.Counter(re.findall(r'<w:rStyle w:val="([^"]*)"/>', src_doc))
    out_styles = collections.Counter(re.findall(r'<w:rStyle w:val="([^"]*)"/>', out_doc))
    log.check(set(src_styles) == set(out_styles), "character style ids preserved")
    log.check(
        all(out_styles[key] >= src_styles[key] for key in src_styles),
        "every source character style retained",
    )

    source_media = {name: source[name] for name in source if name.startswith("word/media/")}
    output_media = {name: output[name] for name in output if name.startswith("word/media/")}
    log.check(source_media == output_media, "media parts byte-identical (%d)" % len(source_media))

    # ---- mapping table ----------------------------------------------------
    log.write("")
    log.write("== mapping ==")
    inverse: Dict[str, Dict[str, str]] = collections.defaultdict(dict)
    seen_codes: Dict[Tuple[str, str, str], str] = {}
    rows = 0
    with io.open(map_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != 7:
            log.check(False, "malformed mapping row: %r" % line[:80])
            continue
        font, weight, _, char_hex, family, mapped_hex, _ = fields
        char = chr(int(char_hex[2:], 16))
        mapped = chr(int(mapped_hex[2:], 16))
        if mapped in inverse[font] and inverse[font][mapped] != char:
            log.check(False, "inconsistent %s %r" % (font, mapped))
        inverse[font][mapped] = char
        key = (font, char_hex, family)
        if key in seen_codes and seen_codes[key] != mapped_hex:
            log.check(False, "inconsistent across weights %s %s" % (font, char_hex))
        seen_codes[key] = mapped_hex
        rows += 1
    log.check(rows == len(lines) - 1, "mapping rows %d" % rows)

    # ---- display reconstruction ------------------------------------------
    log.write("")
    log.write("== display ==")
    src_text = "".join(all_text(source[name].decode("utf-8")) for name in src_names)
    displayed: List[str] = []
    scrambled = 0
    left = 0
    for name in out_names:
        xml = output[name].decode("utf-8")
        for run in _runs(xml):
            fonts = re.search(r"<w:rFonts\b[^>]*/>", run)
            attrs = (
                dict(re.findall(r'w:(ascii|hAnsi|eastAsia|cs)="([^"]*)"', fonts.group(0)))
                if fonts
                else {}
            )
            font_name = next((value for value in attrs.values() if value in inverse), None)
            for node in re.findall(r"<w:t\b[^>]*>(.*?)</w:t>", run, re.S):
                for char in unescape(node):
                    if font_name and char in inverse[font_name]:
                        displayed.append(inverse[font_name][char])
                        scrambled += 1
                    else:
                        displayed.append(char)
                        if (
                            char not in WHITESPACE
                            and unicodedata.category(char) in OBFUSCATE_CATEGORIES
                        ):
                            left += 1
    text = "".join(displayed)
    log.check(
        text == src_text,
        "reconstructed display == normalized source (%d chars, %d scrambled)"
        % (len(text), scrambled),
    )
    if left:
        log.note(
            "%d letters/digits/CJK kept in a non-embedded font (e.g. 楷体); "
            "this is expected if that font was intentionally preserved" % left
        )

    # ---- fonts and faces --------------------------------------------------
    log.write("")
    log.write("== fonts ==")
    font_table = output["word/fontTable.xml"].decode("utf-8")
    rels = output["word/_rels/fontTable.xml.rels"].decode("utf-8")
    content_types = output["[Content_Types].xml"].decode("utf-8")
    fonts = _collect_fonts(font_table, rels, options.prefix)
    log.check(set(fonts) == set(inverse), "fontTable fake fonts == mapping fonts (%d)" % len(fonts))
    log.check(all(len(name) <= 31 for name in fonts), "font names <= 31 chars")
    log.check(
        all(
            re.fullmatch(re.escape(options.prefix) + r"[A-Za-z0-9_-]{8}", name)
            for name in fonts
        ),
        "font naming scheme",
    )
    log.check(all("regular" in embeds for embeds in fonts.values()), "every font has a regular face")
    face_count = sum(len(embeds) for embeds in fonts.values())
    log.check(
        all(
            target in output and ("/" + target) in content_types
            for embeds in fonts.values()
            for target, _ in embeds.values()
        ),
        "every face part exists + content type (%d faces)" % face_count,
    )
    settings = output["word/settings.xml"].decode("utf-8")
    log.check("<w:embedTrueTypeFonts/>" in settings, "settings embeds TrueType fonts")

    # ---- cmap coverage ----------------------------------------------------
    log.write("")
    log.write("== cmap coverage ==")
    bad = 0
    checked = 0
    for name, embeds in fonts.items():
        for weight, (target, guid) in embeds.items():
            font = TTFont(io.BytesIO(deobfuscate(output[target], guid)))
            cmap = font.getBestCmap()
            for mapped in inverse[name]:
                if ord(mapped) not in cmap:
                    bad += 1
            checked += 1
            font.close()
    log.check(bad == 0, "all mapped codepoints present in all faces (%d faces)" % checked)

    # ---- outline spot check ----------------------------------------------
    log.write("")
    log.write("== outline ==")
    rows_list = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) == 7:
            rows_list.append(fields)
    rng = random.Random(7)
    rng.shuffle(rows_list)
    limit = samples if samples is not None else options.verify_samples
    mismatches = 0
    spot = 0
    for font_name, weight, _, char_hex, family, mapped_hex, src_file in rows_list[:limit]:
        if font_name not in fonts or weight not in fonts[font_name]:
            continue
        target, guid = fonts[font_name][weight]
        fake = TTFont(io.BytesIO(deobfuscate(output[target], guid)))
        fake_cmap, fake_glyphs = fake.getBestCmap(), fake.getGlyphSet()
        char = chr(int(char_hex[2:], 16))
        mapped = chr(int(mapped_hex[2:], 16))
        source_path = os.path.join(options.font_dir, src_file)
        if not os.path.exists(source_path):
            fake.close()
            continue
        source = TTFont(
            source_path,
            fontNumber=(0 if src_file.lower().endswith(".ttc") else -1),
        )
        source_cmap = source.getBestCmap()
        if ord(char) in source_cmap:
            upem = fake["head"].unitsPerEm
            scale = upem / source["head"].unitsPerEm
            embolden = max(1, int(round(upem * 0.018)))
            faux = weight in ("bold", "boldItalic") and src_file.lower().startswith("simsun")
            offsets = (
                [(0, 0), (embolden, 0), (0, embolden), (embolden, embolden)]
                if faux
                else [(0, 0)]
            )
            fake_pen = DecomposingRecordingPen(fake_glyphs)
            fake_glyphs[fake_cmap[ord(mapped)]].draw(fake_pen)
            source_pen = DecomposingRecordingPen(source.getGlyphSet())
            for ox, oy in offsets:
                source.getGlyphSet()[source_cmap[ord(char)]].draw(
                    TransformPen(source_pen, (scale, 0, 0, scale, ox, oy))
                )
            spot += 1
            if fake_pen.value != source_pen.value:
                mismatches += 1
                log.write(
                    "   mismatch %s %s %s U+%04X" % (font_name, weight, src_file, ord(char))
                )
        source.close()
        fake.close()
    log.check(mismatches == 0, "outline spot-check: %d glyphs, mismatches=%d" % (spot, mismatches))
    return log.finish()
