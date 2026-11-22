# -*- coding: utf-8 -*-
"""Stage 1: normalize every text run to a small, known set of fonts.

Rule (resolved through the paragraph/character style inheritance chain,
including theme fonts):

* if the character's effective font is one of
  {楷体, Times New Roman, Consolas, 宋体} -> keep it;
* otherwise: ASCII -> Times New Roman; non-ASCII -> 宋体;
* exception: non-ASCII text whose font is Times New Roman -> 宋体.

The result is a document whose body text only uses the three families that
stage 2 knows how to embed (宋体 / Times New Roman / Consolas).  楷体 is
preserved as-is and deliberately not obfuscated.
"""
from __future__ import annotations

import collections
import os
import re
from typing import Dict, Optional, Tuple

from .config import KEEP_ALL, KEEP_EA, SONG, TIMES
from .ooxml import (
    RUN_RE,
    escape,
    parse_attrs,
    read_package,
    rfonts_tag,
    text_parts,
    write_package,
)
from .styles import PartContext, StyleModel

SLOT_LIST = ("ascii", "hAnsi", "eastAsia")


def target_font(slot: str, font: Optional[str]) -> Optional[str]:
    if slot == "cs":
        return font
    if slot == "ascii":
        return font if font in KEEP_ALL else TIMES
    if slot == "eastAsia":
        return font if font in KEEP_EA else SONG
    return font if font in KEEP_ALL else SONG


class Normalizer:
    def __init__(self, model: StyleModel):
        self.model = model

    def process_run(self, run: str, effective: Dict[str, str]) -> Tuple[str, bool]:
        match = re.match(r"(<w:r(?:\s[^>]*)?>)(.*)(</w:r>)\s*$", run, re.S)
        if not match or "<w:t" not in run:
            return run, False
        opening, inner, closing = match.group(1), match.group(2), match.group(3)

        desired = {slot: target_font(slot, effective.get(slot)) for slot in SLOT_LIST}
        if not any(effective.get(slot) != desired[slot] for slot in SLOT_LIST):
            return run, False

        rpr_match = re.search(r"<w:rPr>.*?</w:rPr>|<w:rPr\s*/>", inner, re.S)
        rpr = rpr_match.group(0) if rpr_match else None
        if rpr:
            fonts_match = re.search(r"<w:rFonts\b[^>]*/>", rpr)
            attrs = parse_attrs(fonts_match.group(0)) if fonts_match else {}
            for key in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
                attrs.pop(key, None)
            attrs.update({k: v for k, v in desired.items() if v is not None})
            new_fonts = rfonts_tag(attrs)
            if rpr.endswith("/>"):
                new_rpr = "<w:rPr>" + new_fonts + "</w:rPr>"
            elif fonts_match:
                new_rpr = rpr[: fonts_match.start()] + new_fonts + rpr[fonts_match.end() :]
            else:
                rs = re.search(r"<w:rStyle\b[^>]*/>", rpr)
                insert_at = rs.end() if rs else len("<w:rPr>")
                new_rpr = rpr[:insert_at] + new_fonts + rpr[insert_at:]
            new_inner = inner[: rpr_match.start()] + new_rpr + inner[rpr_match.end() :]
        else:
            new_inner = "<w:rPr>" + rfonts_tag(dict(desired)) + "</w:rPr>" + inner
        return opening + new_inner + closing, True

    def process_part(self, xml: str, name: str) -> Tuple[str, dict]:
        context = PartContext(xml)
        stats = {"changed": 0, "total": 0}

        def do_run(match: re.Match) -> str:
            run = match.group(0)
            if "<w:t" not in run:
                return run
            stats["total"] += 1
            rpr_match = re.search(r"<w:rPr>.*?</w:rPr>|<w:rPr\s*/>", run, re.S)
            rpr = rpr_match.group(0) if rpr_match else None
            effective = self.model.eff_fonts(context, match.start(), rpr)
            new_run, changed = self.process_run(run, effective)
            if changed:
                stats["changed"] += 1
            return new_run

        return RUN_RE.sub(do_run, xml), stats

    def run(self, src: str, dst: str, log=print) -> dict:
        order, parts = read_package(src)
        styles_xml = parts["word/styles.xml"].decode("utf-8")
        theme_xml = parts.get("word/theme/theme1.xml", b"").decode("utf-8", "ignore")

        # Keep the model supplied by the caller if present (allows inspection).
        model = self.model if self.model else StyleModel(styles_xml, theme_xml)
        total = {"changed": 0, "total": 0}
        for name in text_parts(order, parts):
            new_xml, stats = self.process_part(parts[name].decode("utf-8"), name)
            parts[name] = new_xml.encode("utf-8")
            total["changed"] += stats["changed"]
            total["total"] += stats["total"]
            log("%-24s text runs: %d, normalised: %d" % (name, stats["total"], stats["changed"]))

        write_package(dst, order, parts)

        import xml.etree.ElementTree as ET

        for name in text_parts(order, parts):
            ET.fromstring(parts[name])

        log("wrote %s (%d bytes)" % (dst, os.path.getsize(dst)))
        log("text runs: %d, normalised: %d" % (total["total"], total["changed"]))
        self._report_distribution(order, parts, log)
        return total

    def _report_distribution(self, order, parts, log) -> None:
        counter = collections.Counter()
        for name in text_parts(order, parts):
            xml = parts[name].decode("utf-8")
            for fonts in re.findall(r"<w:rFonts\b[^>]*/>", xml):
                for key, value in parse_attrs(fonts).items():
                    if key in ("ascii", "hAnsi", "eastAsia"):
                        counter[(key, value)] += 1
        for (slot, font), count in sorted(counter.items()):
            log("   %-9s = %-16s %d" % (slot, font, count))


def normalize(src: str, dst: str, log=print) -> dict:
    order, parts = read_package(src)
    styles_xml = parts["word/styles.xml"].decode("utf-8")
    theme_xml = parts.get("word/theme/theme1.xml", b"").decode("utf-8", "ignore")
    model = StyleModel(styles_xml, theme_xml)
    return Normalizer(model).run(src, dst, log=log)
