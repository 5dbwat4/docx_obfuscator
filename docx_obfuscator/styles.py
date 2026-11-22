# -*- coding: utf-8 -*-
"""Effective font / bold / italic resolution across Word style chains.

Resolution order (lowest to highest priority):

    document defaults -> default paragraph style -> paragraph style chain
    -> character style chain -> run-level direct formatting

Theme font references (``w:asciiTheme`` etc.) are resolved through
``word/theme/theme1.xml``.  The normalizer removes theme attributes from runs
and writes explicit font names, but style definitions may still use themes.
"""
from __future__ import annotations

import bisect
import re
from typing import Dict, Optional, Tuple

from .ooxml import parse_attrs

SLOTS = ("ascii", "hAnsi", "eastAsia", "cs")
THEME_ATTR = {
    "ascii": "asciiTheme",
    "hAnsi": "hAnsiTheme",
    "eastAsia": "eastAsiaTheme",
    "cs": "cstheme",
}


def parse_theme(theme_xml: str) -> Dict[str, str]:
    """Extract major/minor Latin, East-Asian and complex-script typefaces."""
    scheme = re.search(r"<a:fontScheme\b.*?</a:fontScheme>", theme_xml, re.S)
    if not scheme:
        return {}
    xml = scheme.group(0)
    result: Dict[str, str] = {}
    for kind in ("major", "minor"):
        match = re.search(r"<a:%sFont>(.*?)</a:%sFont>" % (kind, kind), xml, re.S)
        if not match:
            continue
        body = match.group(1)

        def face(tag: str) -> str:
            found = re.search(r"<a:%s\b([^>]*)/>" % tag, body)
            if not found:
                return ""
            attrs = dict(re.findall(r'([A-Za-z0-9]+)="([^"]*)"', found.group(1)))
            return attrs.get("typeface", "")

        latin, ea, cs = face("latin"), face("ea"), face("cs")
        hans = ""
        for found in re.finditer(r"<a:font\b([^>]*)/>", body):
            attrs = dict(re.findall(r'([A-Za-z0-9]+)="([^"]*)"', found.group(1)))
            if attrs.get("script") == "Hans":
                hans = attrs.get("typeface", "")
                break
        result[kind + "HAnsi"] = latin
        result[kind + "EastAsia"] = ea or hans or latin
        result[kind + "Bidi"] = cs or latin
    return result


def tri_flag(rpr: Optional[str], name: str) -> Optional[bool]:
    if not rpr:
        return None
    match = re.search(r"<w:%s\b([^>]*?)/>" % name, rpr)
    if not match:
        return None
    value = re.search(r'w:val="([^"]*)"', match.group(1))
    if not value:
        return True
    return value.group(1) not in ("0", "false", "off")


def any_flag(rpr: Optional[str], a: str, b: str) -> Optional[bool]:
    first, second = tri_flag(rpr, a), tri_flag(rpr, b)
    if first is True or second is True:
        return True
    if first is False or second is False:
        return False
    return None


def first_rfonts_rpr(body: str) -> Tuple[str, str]:
    match = re.search(r"<w:rPr>.*?</w:rPr>|<w:rPr\s*/>", body, re.S)
    if not match:
        return "", ""
    rpr = match.group(0)
    fonts = re.search(r"<w:rFonts\b[^>]*/>", rpr)
    return rpr, (fonts.group(0) if fonts else "")


class StyleModel:
    def __init__(self, styles_xml: str, theme_xml: str = ""):
        self.theme = parse_theme(theme_xml)
        self.styles: Dict[str, dict] = {}
        for match in re.finditer(r"<w:style\b[^>]*>.*?</w:style>", styles_xml, re.S):
            style = match.group(0)
            head = style[: style.find(">") + 1]
            sid = re.search(r'w:styleId="([^"]*)"', head).group(1)
            stype = re.search(r'w:type="([^"]*)"', head).group(1)
            based = re.search(r'<w:basedOn w:val="([^"]*)"', style)
            rpr, fonts = first_rfonts_rpr(style)
            self.styles[sid] = {
                "basedOn": based.group(1) if based else None,
                "type": stype,
                "default": 'w:default="1"' in head,
                "fonts": self.parse_fonts(fonts) if fonts else {},
                "bold": any_flag(rpr, "b", "bCs"),
                "italic": any_flag(rpr, "i", "iCs"),
            }

        defaults_match = re.search(r"<w:rPrDefault>.*?</w:rPrDefault>", styles_xml, re.S)
        if defaults_match:
            fonts = re.search(r"<w:rFonts\b[^>]*/>", defaults_match.group(0))
            self.defaults = self.parse_fonts(fonts.group(0)) if fonts else {}
        else:
            self.defaults = {}

        self.default_para = next(
            (
                sid
                for sid, value in self.styles.items()
                if value["type"] == "paragraph" and value["default"]
            ),
            None,
        )
        if self.default_para is None:
            self.default_para = (
                "Normal" if "Normal" in self.styles else ("1" if "1" in self.styles else None)
            )

    def parse_fonts(self, tag: str) -> Dict[str, str]:
        attrs = parse_attrs(tag)
        result: Dict[str, str] = {}
        for slot, key in THEME_ATTR.items():
            if key in attrs:
                value = self.theme.get(attrs[key])
                if value:
                    result[slot] = value
        for slot in SLOTS:
            if slot in attrs and attrs[slot]:
                result[slot] = attrs[slot]
        return result

    def style_chain(self, sid: Optional[str]) -> list:
        chain, seen = [], set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            chain.append(sid)
            sid = self.styles[sid]["basedOn"]
        chain.reverse()
        return chain

    def eff_for_para(self, sid: Optional[str]) -> Dict[str, str]:
        effective = dict(self.defaults)
        if self.default_para and self.default_para not in self.style_chain(sid):
            for style in self.style_chain(self.default_para):
                effective.update(self.styles[style]["fonts"])
        for style in self.style_chain(sid):
            effective.update(self.styles[style]["fonts"])
        return effective

    def para_psid(self, ctx: "PartContext", pos: int) -> Optional[str]:
        index = bisect.bisect_right(ctx.starts, pos) - 1
        key = ctx.starts[index] if index >= 0 else None
        if key not in ctx.psid:
            header = ctx.doc[key:pos] if key is not None else ctx.doc[:pos]
            found = re.search(r'<w:pStyle w:val="([^"]*)"', header)
            ctx.psid[key] = found.group(1) if found else self.default_para
        return ctx.psid[key]

    def para_eff(self, ctx: "PartContext", pos: int) -> Dict[str, str]:
        index = bisect.bisect_right(ctx.starts, pos) - 1
        key = ctx.starts[index] if index >= 0 else None
        if key not in ctx.peff:
            ctx.peff[key] = self.eff_for_para(self.para_psid(ctx, pos))
        return ctx.peff[key]

    def eff_fonts(self, ctx: "PartContext", pos: int, rpr: Optional[str]) -> Dict[str, str]:
        effective = dict(self.para_eff(ctx, pos))
        rs = re.search(r'<w:rStyle\b[^>]*w:val="([^"]*)"', rpr or "")
        if rs and rs.group(1) in self.styles:
            for style in self.style_chain(rs.group(1)):
                effective.update(self.styles[style]["fonts"])
        fonts = re.search(r"<w:rFonts\b[^>]*/>", rpr or "")
        if fonts:
            effective.update(self.parse_fonts(fonts.group(0)))
        return effective

    def eff_weight(self, ctx: "PartContext", pos: int, rpr: Optional[str]) -> str:
        bold = italic = None
        for style in self.style_chain(self.para_psid(ctx, pos)):
            if self.styles[style]["bold"] is not None:
                bold = self.styles[style]["bold"]
            if self.styles[style]["italic"] is not None:
                italic = self.styles[style]["italic"]
        rs = re.search(r'<w:rStyle\b[^>]*w:val="([^"]*)"', rpr or "")
        if rs and rs.group(1) in self.styles:
            for style in self.style_chain(rs.group(1)):
                if self.styles[style]["bold"] is not None:
                    bold = self.styles[style]["bold"]
                if self.styles[style]["italic"] is not None:
                    italic = self.styles[style]["italic"]
        direct_bold = any_flag(rpr, "b", "bCs")
        direct_italic = any_flag(rpr, "i", "iCs")
        if direct_bold is not None:
            bold = direct_bold
        if direct_italic is not None:
            italic = direct_italic
        return ("bold" if bold else "") + ("Italic" if italic else "") or "regular"


class PartContext:
    """Per-part parsing context: paragraph offsets and resolved-style caches."""

    def __init__(self, xml: str):
        self.doc = xml
        self.starts = [m.start() for m in re.finditer(r"<w:p(?:\s[^>]*)?>", xml)]
        self.psid: Dict[Optional[int], Optional[str]] = {}
        self.peff: Dict[Optional[int], Dict[str, str]] = {}
