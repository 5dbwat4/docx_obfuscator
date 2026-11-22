# -*- coding: utf-8 -*-
"""Stage 2: bucket-based font obfuscation.

Algorithm
---------
1. Walk every ``<w:r>`` and resolve its effective font (paragraph style
   chain + character style chain + direct formatting).
2. A character is *scrambled* when its effective font is 宋体 / Times New
   Roman / Consolas and its Unicode category is a letter or digit.  Whitespace
   and punctuation are deliberately left untouched so Word's line breaking and
   punctuation compression behave exactly as in the normalized document.
3. Every scrambled character instance is assigned to a random *bucket*.  A
   family with N scrambled characters gets ``max(1, N // 10)`` buckets, so each
   bucket has ~10 characters and Word pages carry hundreds of tiny fonts.
4. Each bucket gets a random synthetic font family name
   ``_5dbwat4_<8 base64 chars>``, one scrambled codepoint per distinct
   (character, family) pair, and up to four faces (regular/bold/italic/bold
   italic), all embedded as ``.odttf`` parts.
5. Text runs are split into per-character runs; scrambled characters point at
   their bucket font, all other characters keep the original run properties.
   Non-text run children (``w:br``, ``w:drawing``, ``w:tab``, ...) are copied
   verbatim, preserving diagrams, line breaks and character styles.

The document therefore displays normally while a plain-text copy yields the
scrambled codepoints (which have no meaning without the embedded cmap).
"""
from __future__ import annotations

import base64
import collections
import io
import os
import random
import re
import time
import unicodedata
import uuid
from typing import Dict, List, Optional, Tuple

from .config import (
    CHOSEN_FAMILIES,
    FAMILIES,
    OBFUSCATE_CATEGORIES,
    WEIGHT_ORDER,
    WEIGHT_SUBFAMILY,
    WEIGHT_TAGS,
    WHITESPACE,
    Options,
)
from .fonts import FontLibrary, obfuscate
from .ooxml import (
    RUN_RE,
    RPR_RE,
    WT_RE,
    escape,
    parse_attrs,
    read_package,
    slot_of,
    text_parts,
    top_level_children,
    unescape,
    write_package,
)
from .styles import PartContext, StyleModel

CLOSE_FONT_TABLE = "</w:fonts>"
CLOSE_STYLES = "</w:styles>"

CJK_POOL = list(range(0x4E00, 0x9FA6))


def char_class(char: str) -> str:
    """Layout-preserving replacement class for a character."""
    cp = ord(char)
    if (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x2FA1F
    ):
        return "cjk"
    if char in "\u3001\uff0c\uff1b\uff1a":
        return "ea_comma"
    if char in "\u3002\uff01\uff1f":
        return "ea_stop"
    if char in "\uff08\u3010\u300a\u3008\u3014\uff3b\uff5b\u300c\u300e":
        return "ea_open"
    if char in "\uff09\u3011\u300b\u3009\u3015\uff3d\uff5d\u300d\u300f":
        return "ea_close"
    if char in "\u201c\u201d\u2018\u2019":
        return "quote"
    if char in "\u2010\u2011\u2012\u2013\u2014\u2212":
        return "dash"
    if char == "\u2026":
        return "ellipsis"
    if char in "\u00b7\u2022\u2027":
        return "middot"
    if 0xFF21 <= cp <= 0xFF3A:
        return "fw_upper"
    if 0xFF41 <= cp <= 0xFF5A:
        return "fw_lower"
    if 0xFF10 <= cp <= 0xFF19:
        return "fw_digit"
    if 0xFF00 <= cp <= 0xFFEF:
        return "fw_punct"
    if 0x3000 <= cp <= 0x303F or 0xFE30 <= cp <= 0xFE4F:
        return "ea_punct"
    if 0x2460 <= cp <= 0x2473:
        return "circled"
    if cp < 0x80:
        if char in ",;:":
            return "comma"
        if char in ".!?":
            return "stop"
        if char in "([{":
            return "open"
        if char in ")]}":
            return "close"
        if char == "-":
            return "dash"
        if char == "_":
            return "connector"
        if char in "/\\":
            return "slash"
        if char in "+<=>|~*":
            return "math"
        if char in '"\'"':
            return "quote"
        if "A" <= char <= "Z":
            return "upper"
        if "a" <= char <= "z":
            return "lower"
        if "0" <= char <= "9":
            return "digit"
        return "punct"
    category = unicodedata.category(char)
    if category == "Lu":
        return "upper_lat"
    if category == "Ll":
        return "lower_lat"
    if category == "Nd":
        return "digit_lat"
    if category == "Lo":
        return "cjk"
    if category == "Ps":
        return "open"
    if category == "Pe":
        return "close"
    if category == "Pd":
        return "dash"
    if category == "Pc":
        return "connector"
    if category == "Sm":
        return "math"
    return "punct"


class Obfuscator:
    def __init__(self, options: Optional[Options] = None, log=print):
        self.options = options or Options()
        self.log = log
        self.rng = random.Random(self.options.seed)
        self.library = FontLibrary(self.options.font_dir)
        self.model: Optional[StyleModel] = None

        self._used_names = set()
        self._new_rpr_cache: Dict[Tuple[str, str], str] = {}
        self.seq: List[Tuple[int, str, str, str]] = []
        self.seq_raw: List[Tuple[str, str, str]] = []
        self.state = {"i": 0}
        self.bucket_fams: Dict[int, str] = {}
        self.bucket_pairs: Dict[int, set] = collections.defaultdict(set)
        self.bucket_faces: Dict[int, Dict[str, set]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        self.bucket_names: Dict[int, str] = {}
        self.bucket_maps: Dict[int, Dict[Tuple[str, str], str]] = {}
        self._reset()

    def _reset(self) -> None:
        """Make the instance reusable for multiple documents."""
        self.rng = random.Random(self.options.seed)
        self._used_names = set()
        self._new_rpr_cache = {}
        self.seq = []
        self.seq_raw = []
        self.state = {"i": 0}
        self.bucket_fams = {}
        self.bucket_pairs = collections.defaultdict(set)
        self.bucket_faces = collections.defaultdict(lambda: collections.defaultdict(set))
        self.bucket_names = {}
        self.bucket_maps = {}

    # ------------------------------------------------------------------ helpers
    def _new_name(self) -> str:
        while True:
            raw = bytes(self.rng.getrandbits(8) for _ in range(self.options.name_bytes))
            name = self.options.prefix + base64.urlsafe_b64encode(raw).decode()
            if name not in self._used_names:
                self._used_names.add(name)
                return name

    def _pick_family(self, char: str, effective: Dict[str, str]) -> Optional[str]:
        if char in WHITESPACE:
            return None
        if unicodedata.category(char) not in OBFUSCATE_CATEGORIES:
            return None
        family = CHOSEN_FAMILIES.get(effective.get(slot_of(char)))
        if family is None:
            return None
        cp = ord(char)
        for candidate in (family, "simsun", "times", "consola"):
            if cp in self.library.family_coverage(candidate):
                return candidate
        return None

    def _make_new_rpr(self, rpr: Optional[str], name: str) -> str:
        key = (rpr or "", name)
        if key in self._new_rpr_cache:
            return self._new_rpr_cache[key]
        match = re.match(r"<w:rPr\b[^>]*>(.*?)</w:rPr>", rpr or "", re.S)
        inner = match.group(1) if match else ""
        fonts = re.search(r"<w:rFonts\b[^>]*/>", inner)
        hint = parse_attrs(fonts.group(0)).get("hint", "") if fonts else ""
        inner = re.sub(r"<w:rFonts\b[^>]*/>", "", inner)
        new_fonts = (
            "<w:rFonts"
            + (f' w:hint="{hint}"' if hint else "")
            + f' w:ascii="{name}" w:hAnsi="{name}" w:eastAsia="{name}" w:cs="{name}"/>'
        )
        rs = re.search(r"<w:rStyle\b[^>]*/>", inner)
        if rs:
            inner = inner[: rs.end()] + new_fonts + inner[rs.end() :]
        else:
            inner = new_fonts + inner
        result = "<w:rPr>" + inner + "</w:rPr>"
        self._new_rpr_cache[key] = result
        return result

    # --------------------------------------------------------------- run splitting
    def _split_run(self, context: PartContext, run: str, pos: int, mode: str) -> str:
        match = re.match(r"(<w:r(?:\s[^>]*)?>)(.*)(</w:r>)\s*$", run, re.S)
        if not match:
            return run
        inner = match.group(2)
        if "<w:t" not in inner:
            return run
        rpr_match = RPR_RE.search(inner)
        rpr = rpr_match.group(0) if rpr_match else ""
        body = (
            (inner[: rpr_match.start()] + inner[rpr_match.end() :])
            if rpr_match
            else inner
        )
        effective = self.model.eff_fonts(context, pos, rpr)
        weight = self.model.eff_weight(context, pos, rpr)
        children = top_level_children(body)

        if mode == "collect":
            for kind, value in children:
                if kind != "element" or not re.match(r"<w:t\b", value):
                    continue
                text_match = WT_RE.match(value)
                if not text_match:
                    continue
                for char in unescape(text_match.group(1)):
                    family = self._pick_family(char, effective)
                    if family is not None:
                        self.seq_raw.append((char, family, weight))
            return run

        output = []
        for kind, value in children:
            if kind == "element" and re.match(r"<w:t\b", value):
                text_match = WT_RE.match(value)
                if not text_match:
                    continue
                for char in unescape(text_match.group(1)):
                    family = self._pick_family(char, effective)
                    if family is None:
                        output.append(
                            "<w:r>"
                            + rpr
                            + '<w:t xml:space="preserve">'
                            + escape(char)
                            + "</w:t></w:r>"
                        )
                        continue
                    bucket, original, fam, _ = self.seq[self.state["i"]]
                    self.state["i"] += 1
                    assert (original, fam) == (char, family), (
                        self.state["i"],
                        original,
                        char,
                        fam,
                        family,
                    )
                    name = self.bucket_names[bucket]
                    mapped = self.bucket_maps[bucket][(char, family)]
                    output.append(
                        "<w:r>"
                        + self._make_new_rpr(rpr, name)
                        + '<w:t xml:space="preserve">'
                        + escape(mapped)
                        + "</w:t></w:r>"
                    )
            elif kind == "element" or (kind == "raw" and value.strip()):
                output.append("<w:r>" + rpr + value + "</w:r>")
        return "".join(output)

    # ------------------------------------------------------------------- pools
    def _build_pools(self) -> Dict[str, List[int]]:
        def make_pool(values) -> List[int]:
            if isinstance(values, str):
                values = [ord(c) for c in values]
            else:
                values = [ord(v) if isinstance(v, str) else v for v in values]
            seen, result = set(), []
            for cp in values:
                if cp not in seen:
                    seen.add(cp)
                    result.append(cp)
            self.rng.shuffle(result)
            return result

        cjk = list(CJK_POOL)
        self.rng.shuffle(cjk)
        ea_punct = (
            [ord(c) for c in "\u3001\u3002\u300a\u300b\u3010\u3011\uff08\uff09\uff0c\uff1a\uff1b"]
            + [c for c in range(0x3000, 0x3040)]
            + [c for c in range(0xFE30, 0xFE50)]
        )
        fullwidth = [c for c in range(0xFF00, 0xFFF0)]
        pools = {
            "cjk": cjk,
            "upper": make_pool("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            "lower": make_pool("abcdefghijklmnopqrstuvwxyz"),
            "upper_lat": make_pool("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            "lower_lat": make_pool("abcdefghijklmnopqrstuvwxyz"),
            "digit": make_pool("0123456789"),
            "digit_lat": make_pool("0123456789"),
            "comma": make_pool(",;:"),
            "stop": make_pool(".!?"),
            "open": make_pool("([{"),
            "close": make_pool(")]}"),
            "punct": make_pool("!\"#%&'@`$^"),
            "dash": make_pool("-\u2010\u2011\u2012\u2013\u2014\u2212"),
            "connector": make_pool("_\u203f\u2040\uff3f"),
            "slash": make_pool("/\\\u2044\u2215"),
            "math": make_pool("+<=>|~*\u00d7\u00f7\u00b1\u2264\u2265\u2260\u2248\u221e"),
            "quote": make_pool("\"'\u2018\u2019\u201c\u201d\u00ab\u00bb"),
            "ea_comma": make_pool("\u3001\uff0c\uff1b\uff1a"),
            "ea_stop": make_pool("\u3002\uff01\uff1f"),
            "ea_open": make_pool("\uff08\u3010\u300a\u3008\u3014\uff3b\uff5b\u300c\u300e"),
            "ea_close": make_pool("\uff09\u3011\u300b\u3009\u3015\uff3d\uff5d\u300d\u300f"),
            "ea_punct": make_pool(ea_punct),
            "ellipsis": make_pool("\u2026\u2025\u22ef"),
            "middot": make_pool("\u00b7\u2022\u2027"),
            "circled": make_pool([chr(c) for c in range(0x2460, 0x2474)]),
            "fw_upper": make_pool([chr(c) for c in range(0xFF21, 0xFF3B)]),
            "fw_lower": make_pool([chr(c) for c in range(0xFF41, 0xFF5B)]),
            "fw_digit": make_pool([chr(c) for c in range(0xFF10, 0xFF1A)]),
            "fw_punct": make_pool(
                [
                    chr(c)
                    for c in fullwidth
                    if not (
                        0xFF10 <= c <= 0xFF19
                        or 0xFF21 <= c <= 0xFF3A
                        or 0xFF41 <= c <= 0xFF5A
                    )
                ]
            ),
        }
        pools["__letters"] = make_pool("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
        pools["__digit_alt"] = make_pool(
            "0123456789" + "".join(chr(c) for c in range(0xFF10, 0xFF1A))
        )
        pools["__punct"] = make_pool("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
        pools["__ea_punct"] = make_pool(ea_punct)
        pools["__fullwidth"] = make_pool(fullwidth)
        pools["__ea"] = make_pool(cjk + ea_punct + fullwidth)
        pools["__latin"] = make_pool(list(range(0x21, 0x7F)))
        return pools

    @staticmethod
    def _candidate_pools(cls: str) -> List[str]:
        if cls in ("upper", "lower", "upper_lat", "lower_lat"):
            return [cls, "__letters", "__latin"]
        if cls in ("digit", "digit_lat"):
            return [cls, "__digit_alt", "__latin"]
        if cls == "cjk":
            return ["cjk"]
        if cls.startswith("ea_") or cls in ("ellipsis", "middot"):
            return [cls, "__ea_punct", "__ea"]
        if cls.startswith("fw_"):
            return [cls, "__fullwidth", "__ea"]
        return [cls, "__punct", "__latin"]

    def _take_cp(self, char: str, cursors: Dict[str, int], used: set, pools) -> int:
        original_cp = ord(char)
        for key in self._candidate_pools(char_class(char)):
            pool = pools[key]
            while cursors[key] < len(pool):
                cp = pool[cursors[key]]
                cursors[key] += 1
                if cp in used:
                    continue
                if cp == original_cp and len(pool) > 1:
                    continue
                used.add(cp)
                return cp
        for key in ("__latin", "__ea"):
            pool = pools[key]
            while cursors[key] < len(pool):
                cp = pool[cursors[key]]
                cursors[key] += 1
                if cp not in used:
                    used.add(cp)
                    return cp
        used.add(original_cp)
        return original_cp

    # -------------------------------------------------------------------- main
    def run(self, src: str, dst: str, map_path: Optional[str] = None, log=None) -> dict:
        log = log or self.log
        if self.options.bucket_divisor < 1:
            raise ValueError("bucket_divisor must be >= 1")
        if len(self.options.prefix) + 8 > 31:
            raise ValueError("font name prefix too long (Word limit is 31 chars)")
        self._reset()
        started = time.time()
        order, parts = read_package(src)
        text_names = text_parts(order, parts)
        styles_xml = parts["word/styles.xml"].decode("utf-8")
        theme_xml = parts.get("word/theme/theme1.xml", b"").decode("utf-8", "ignore")
        self.model = StyleModel(styles_xml, theme_xml)
        docs = {name: parts[name].decode("utf-8") for name in text_names}
        contexts = {name: PartContext(docs[name]) for name in text_names}

        missing = self.library.check_available()
        if missing:
            raise FileNotFoundError(
                "missing source fonts in %s: %s" % (self.options.font_dir, ", ".join(sorted(set(missing))))
            )

        body = sum(
            len(unescape(node))
            for name in text_names
            for node in re.findall(r"<w:t\b[^>]*>(.*?)</w:t>", docs[name], re.S)
        )
        log("total body chars=%d" % body)

        # ---- pass 1: collect ------------------------------------------------
        collect_time = time.time()
        for name in text_names:
            re.sub(
                RUN_RE,
                lambda m, c=contexts[name]: self._split_run(c, m.group(0), m.start(), "collect"),
                docs[name],
            )
        log(
            "collected %d scrambled character instances in %.1fs"
            % (len(self.seq_raw), time.time() - collect_time)
        )
        if not self.seq_raw:
            raise SystemExit("nothing collected: check that the document was normalized first")

        # ---- bucket assignment ---------------------------------------------
        counts = collections.Counter(family for _, family, _ in self.seq_raw)
        buckets_per_family = {
            family: max(1, count // self.options.bucket_divisor)
            for family, count in counts.items()
        }
        bases, offset = {}, 0
        for family in ("simsun", "times", "consola"):
            if family in buckets_per_family:
                bases[family] = offset
                offset += buckets_per_family[family]
        for char, family, weight in self.seq_raw:
            bucket = bases[family] + self.rng.randrange(buckets_per_family[family])
            self.bucket_fams[bucket] = family
            self.seq.append((bucket, char, family, weight))
        log(
            "buckets by family: "
            + ", ".join(
                "%s=%d" % (family, buckets_per_family[family])
                for family in ("simsun", "times", "consola")
                if family in buckets_per_family
            )
        )
        for bucket, char, family, weight in self.seq:
            self.bucket_pairs[bucket].add((char, family))
            self.bucket_faces[bucket][weight].add((char, family))
        log(
            "weights: "
            + ", ".join(
                "%s=%d" % item
                for item in collections.Counter(weight for _, _, _, weight in self.seq).items()
            )
        )

        # ---- pass 2: build fake fonts --------------------------------------
        build_time = time.time()
        pools = self._build_pools()
        bucket_names: Dict[int, str] = {}
        bucket_maps: Dict[int, Dict[Tuple[str, str], str]] = {}
        font_parts: Dict[str, bytes] = {}
        font_rels: List[Tuple[str, str]] = []
        font_entries: List[str] = []
        style_entries: List[str] = []
        face_dump: List[str] = []
        rid, font_index = 1, 1
        meta = []

        for bucket in sorted(self.bucket_pairs):
            name = self._new_name()
            bucket_names[bucket] = name
            cursors = collections.defaultdict(int)
            used_mapped = set()
            mapping = {}
            for char, family in sorted(self.bucket_pairs[bucket], key=lambda item: (item[1], ord(item[0]))):
                mapping[(char, family)] = chr(
                    self._take_cp(char, cursors, used_mapped, pools)
                )
            bucket_maps[bucket] = mapping

            all_pairs = sorted(self.bucket_pairs[bucket], key=lambda item: (item[1], ord(item[0])))
            family_key = self.bucket_fams[bucket]
            used_weights = set(self.bucket_faces[bucket].keys())
            build_weights = ["regular"] + [
                weight for weight in WEIGHT_ORDER[1:] if weight in used_weights
            ]
            embeds = []
            for weight in build_weights:
                entries = []
                for char, fam in all_pairs:
                    face = self.library.choose_source(family_key, weight, char)
                    entries.append((char, mapping[(char, fam)], face))
                if not entries:
                    continue
                data, glyph_count = self.library.build_face(
                    entries, name, WEIGHT_SUBFAMILY[weight], family_key
                )
                if glyph_count != len(entries):
                    log(
                        "WARN %s %s: %d/%d glyphs"
                        % (name, weight, glyph_count, len(entries))
                    )
                guid = "{" + str(uuid.uuid4()).upper() + "}"
                odttf = obfuscate(data, guid)
                target = "fonts/font%d.odttf" % font_index
                font_parts["word/" + target] = odttf
                rel_id = "rId%d" % rid
                rid += 1
                font_rels.append((rel_id, target))
                embeds.append((WEIGHT_TAGS[weight], rel_id, guid))
                meta.append((target, len(odttf)))
                for (char, fam), (_, mapped, face) in zip(all_pairs, entries):
                    face_dump.append(
                        "%s\t%s\t%s\tU+%04X\t%s\tU+%04X\t%s"
                        % (name, weight, target, ord(char), fam, ord(mapped), face.file)
                    )
                font_index += 1

            meta_info = FAMILIES[family_key]
            entry = (
                '<w:font w:name="%s"><w:panose1 w:val="%s"/><w:charset w:val="%s"/>'
                '<w:family w:val="%s"/><w:pitch w:val="%s"/>'
                '<w:sig w:usb0="FFFFFFFF" w:usb1="FFFFFFFF" w:usb2="FFFFFFFF" w:usb3="FFFFFFFF" '
                'w:csb0="FFFFFFFF" w:csb1="FFFFFFFF"/>'
                % (
                    name,
                    meta_info.panose,
                    meta_info.charset,
                    meta_info.family,
                    meta_info.pitch,
                )
            )
            for tag, rel_id, guid in embeds:
                entry += '<w:%s r:id="%s" w:fontKey="%s"/>' % (tag, rel_id, guid)
            entry += "</w:font>"
            font_entries.append(entry)
            style_entries.append(
                '<w:style w:type="character" w:customStyle="1" w:styleId="%s"><w:name w:val="%s"/>'
                '<w:rPr><w:rFonts w:ascii="%s" w:hAnsi="%s" w:eastAsia="%s" w:cs="%s"/></w:rPr></w:style>'
                % (name, name, name, name, name, name)
            )
        self.bucket_names = bucket_names
        self.bucket_maps = bucket_maps
        log(
            "built %d buckets, %d font files in %.1fs"
            % (len(bucket_names), len(meta), time.time() - build_time)
        )

        # ---- pass 3: emit ---------------------------------------------------
        emit_time = time.time()
        self.state = {"i": 0}
        for name in text_names:
            docs[name] = re.sub(
                RUN_RE,
                lambda m, c=contexts[name]: self._split_run(c, m.group(0), m.start(), "emit"),
                docs[name],
            )
            parts[name] = docs[name].encode("utf-8")
        log(
            "emitted document %.1fs  size=%d"
            % (time.time() - emit_time, sum(len(docs[name]) for name in text_names))
        )
        assert self.state["i"] == len(self.seq), (self.state["i"], len(self.seq))

        # ---- package plumbing ----------------------------------------------
        font_table = parts["word/fontTable.xml"].decode("utf-8")
        parts["word/fontTable.xml"] = font_table.replace(
            CLOSE_FONT_TABLE, "".join(font_entries) + CLOSE_FONT_TABLE
        ).encode("utf-8")

        parts["word/_rels/fontTable.xml.rels"] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                '<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/font" Target="%s"/>'
                % (rel_id, target)
                for rel_id, target in font_rels
            )
            + "</Relationships>"
        ).encode("utf-8")

        styles = parts["word/styles.xml"].decode("utf-8")
        parts["word/styles.xml"] = styles.replace(
            CLOSE_STYLES, "".join(style_entries) + CLOSE_STYLES
        ).encode("utf-8")

        settings = parts["word/settings.xml"].decode("utf-8")
        if "<w:embedTrueTypeFonts/>" not in settings:
            for anchor in ("<w:bordersDoNotSurroundHeader", "<w:displayBackgroundShape"):
                if anchor in settings:
                    settings = settings.replace(
                        anchor, "<w:embedTrueTypeFonts/><w:saveSubsetFonts/>" + anchor, 1
                    )
                    break
            else:
                settings = settings.replace(
                    "<w:zoom", "<w:embedTrueTypeFonts/><w:saveSubsetFonts/><w:zoom", 1
                )
        parts["word/settings.xml"] = settings.encode("utf-8")

        content_types = parts["[Content_Types].xml"].decode("utf-8")
        content_types = content_types.replace(
            "</Types>",
            "".join(
                '<Override PartName="/%s" ContentType="application/vnd.openxmlformats-officedocument.obfuscatedFont"/>'
                % path
                for path in font_parts
            )
            + "</Types>",
        )
        parts["[Content_Types].xml"] = content_types.encode("utf-8")
        parts.update(font_parts)

        write_package(dst, order, parts)

        if map_path:
            with io.open(map_path, "w", encoding="utf-8") as handle:
                handle.write("font\tweight\tfile\tchar\tfam\tmapped\tsrc\n")
                handle.write("\n".join(face_dump))

        result = {
            "body_chars": body,
            "scrambled_chars": len(self.seq),
            "buckets": len(bucket_names),
            "font_files": len(meta),
            "size": os.path.getsize(dst),
            "elapsed": time.time() - started,
        }
        log(
            "wrote %s  %d bytes (%.1fs)"
            % (dst, result["size"], result["elapsed"])
        )
        log("buckets used=%d  font files=%d" % (result["buckets"], result["font_files"]))
        return result


def obfuscate_docx(src: str, dst: str, map_path: Optional[str] = None, options: Optional[Options] = None, log=print) -> dict:
    return Obfuscator(options, log=log).run(src, dst, map_path=map_path, log=log)
