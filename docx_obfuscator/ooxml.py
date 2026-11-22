# -*- coding: utf-8 -*-
"""Small OOXML helpers used by the whole pipeline.

The project deliberately uses regular expressions on the WordprocessingML
parts instead of a DOM: Word documents produced by WPS/Word contain markup
that is round-tripped far more faithfully this way (and it keeps attribute
order, namespace prefixes and unknown extension elements intact).
"""
from __future__ import annotations

import os
import re
import zipfile
from typing import Dict, List, Tuple

TEXT_PART_RE = re.compile(r"<w:t\b")
RUN_RE = re.compile(r"<w:r(?:\s[^>]*)?>.*?</w:r>", re.S)
WT_RE = re.compile(r"<w:t\b[^>]*>(.*?)</w:t>", re.S)
RPR_RE = re.compile(r"<w:rPr\b[^>]*/>|<w:rPr>.*?</w:rPr>", re.S)


def unescape(text: str) -> str:
    for entity, char in (
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&apos;", "'"),
        ("&amp;", "&"),
    ):
        text = text.replace(entity, char)
    return text


def escape(text: str) -> str:
    for entity, char in (
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&apos;"),
    ):
        text = text.replace(entity, char)
    return text


def parse_attrs(tag: str) -> Dict[str, str]:
    """Parse ``w:foo="bar"`` attributes of an XML tag into a dict."""
    return {m.group(1): m.group(2) for m in re.finditer(r'w:([A-Za-z0-9]+)="([^"]*)"', tag)}


def rfonts_tag(attrs: Dict[str, str]) -> str:
    return "<w:rFonts" + "".join(f' w:{k}="{v}"' for k, v in attrs.items()) + "/>"


def is_east_asian(cp: int) -> bool:
    return (
        (0x2E80 <= cp <= 0x303F)
        or (0x3040 <= cp <= 0x33FF)
        or (0x3400 <= cp <= 0x4DBF)
        or (0x4E00 <= cp <= 0x9FFF)
        or (0xF900 <= cp <= 0xFAFF)
        or (0xFE30 <= cp <= 0xFE4F)
        or (0xFF00 <= cp <= 0xFFEF)
        or (0x20000 <= cp <= 0x2FA1F)
    )


def slot_of(char: str) -> str:
    """Return the rFonts slot Word will use for a character."""
    cp = ord(char)
    if is_east_asian(cp):
        return "eastAsia"
    return "ascii" if cp < 0x80 else "hAnsi"


def read_package(path: str) -> Tuple[List[str], Dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        order = list(archive.namelist())
        parts = {name: archive.read(name) for name in order}
    return order, parts


def write_package(path: str, order: List[str], parts: Dict[str, bytes]) -> None:
    """Write a docx, preserving the original part order where possible."""
    if os.path.exists(path):
        os.remove(path)
    written = set()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in order:
            if name in parts and name not in written:
                archive.writestr(name, parts[name])
                written.add(name)
        for name, data in parts.items():
            if name not in written:
                archive.writestr(name, data)
                written.add(name)


def text_parts(order: List[str], parts: Dict[str, bytes]) -> List[str]:
    """All word/*.xml parts that contain ``<w:t>`` text nodes."""
    result = []
    for name in order:
        if not (name.startswith("word/") and name.endswith(".xml")):
            continue
        if TEXT_PART_RE.search(parts[name].decode("utf-8", "ignore")):
            result.append(name)
    return sorted(result)


def text_nodes(xml: str) -> List[str]:
    return WT_RE.findall(xml)


def all_text(xml: str) -> str:
    return "".join(unescape(node) for node in text_nodes(xml))


def top_level_children(source: str) -> List[Tuple[str, str]]:
    """Split a run body into top-level elements and raw whitespace.

    A tiny streaming scanner is used instead of an XML parser so that run
    children such as ``<w:br/>``, ``<w:tab/>`` and ``<w:drawing>`` (which may
    contain nested elements) are copied verbatim.
    """
    result: List[Tuple[str, str]] = []
    i, n = 0, len(source)
    while i < n:
        j = source.find("<", i)
        if j < 0:
            if source[i:].strip():
                result.append(("raw", source[i:]))
            break
        if j > i and source[i:j].strip():
            result.append(("raw", source[i:j]))
        if source.startswith("<!--", j):
            k = source.find("-->", j + 4)
            k = n if k < 0 else k + 3
            result.append(("raw", source[j:k]))
            i = k
            continue
        if source.startswith("<![CDATA[", j):
            k = source.find("]]>", j + 9)
            k = n if k < 0 else k + 3
            result.append(("raw", source[j:k]))
            i = k
            continue
        k = source.find(">", j)
        if k < 0:
            result.append(("raw", source[j:]))
            break
        tag = source[j : k + 1]
        if (
            tag.startswith("</")
            or tag.endswith("/>")
            or tag.startswith("<?")
            or tag.startswith("<!")
        ):
            result.append(("raw", tag))
            i = k + 1
            continue
        match = re.match(r"<([A-Za-z_][\w.-]*:[\w.-]+)", tag)
        if not match:
            result.append(("raw", tag))
            i = k + 1
            continue
        name = match.group(1)
        pattern = re.compile(
            r"<(/?)" + re.escape(name) + r"(?:\s[^>]*?)?(/?)>", re.S
        )
        depth, end = 0, None
        for found in pattern.finditer(source, j):
            if found.group(2) == "/":
                if depth == 0:
                    end = found.end()
                    break
            elif found.group(1) == "/":
                depth -= 1
                if depth == 0:
                    end = found.end()
                    break
            else:
                depth += 1
        if end is None:
            result.append(("raw", source[j:]))
            break
        result.append(("element", source[j:end]))
        i = end
    return result
