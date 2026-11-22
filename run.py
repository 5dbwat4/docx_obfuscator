#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Command-line entry point for docx_obfuscator.

Examples
--------
    python run.py all input.docx -o output.docx --map buckets.tsv
    python run.py normalize input.docx -o normalized.docx
    python run.py obfuscate normalized.docx -o obfuscated.docx --map buckets.tsv
    python run.py verify normalized.docx obfuscated.docx --map buckets.tsv
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from docx_obfuscator import Options, normalize, obfuscate_docx, verify


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--font-dir",
        default=os.environ.get("DOCX_OBF_FONTDIR", r"C:\Windows\Fonts"),
        help="directory containing simsun.ttc / times*.ttf / consola*.ttf",
    )
    parser.add_argument("--seed", type=int, default=20261011, help="random seed")
    parser.add_argument("--prefix", default="_5dbwat4_", help="synthetic font name prefix")
    parser.add_argument(
        "--bucket-divisor",
        type=int,
        default=10,
        help="one fake font per N scrambled characters (default: 10)",
    )


def _options(args: argparse.Namespace) -> Options:
    return Options(
        font_dir=args.font_dir,
        seed=args.seed,
        prefix=args.prefix,
        bucket_divisor=args.bucket_divisor,
    )


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def cmd_normalize(args: argparse.Namespace) -> int:
    _ensure_parent(args.output)
    normalize(args.input, args.output)
    return 0


def cmd_obfuscate(args: argparse.Namespace) -> int:
    _ensure_parent(args.output)
    obfuscate_docx(args.input, args.output, args.map, _options(args))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    ok = verify(
        args.normalized,
        args.obfuscated,
        args.map,
        options=_options(args),
        log_path=args.log,
        samples=args.samples,
    )
    return 0 if ok else 1


def cmd_all(args: argparse.Namespace) -> int:
    _ensure_parent(args.output)
    normalized = args.normalized or (args.output + ".normalized.docx")
    mapping = args.map or (args.output + ".buckets.tsv")
    log_path = args.verify_log or (args.output + ".verify.txt")
    _ensure_parent(normalized)
    _ensure_parent(mapping)
    _ensure_parent(log_path)

    print("[1/3] normalize -> %s" % normalized)
    normalize(args.input, normalized)
    print("[2/3] obfuscate -> %s" % args.output)
    result = obfuscate_docx(normalized, args.output, mapping, _options(args))
    print(
        "      %d scrambled chars, %d buckets, %d embedded faces, %.1f MB"
        % (
            result["scrambled_chars"],
            result["buckets"],
            result["font_files"],
            result["size"] / (1024 * 1024),
        )
    )
    print("[3/3] verify -> %s" % log_path)
    ok = verify(
        normalized,
        args.output,
        mapping,
        options=_options(args),
        log_path=log_path,
        samples=args.samples,
    )
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docx_obfuscator",
        description="Font-based anti-copy obfuscation for DOCX files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("normalize", help="normalize fonts to 宋体 / Times New Roman / Consolas")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    _add_common(p)
    p.set_defaults(func=cmd_normalize)

    p = sub.add_parser("obfuscate", help="embed scrambled bucket fonts")
    p.add_argument("input", help="normalized .docx")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--map", help="write the font mapping TSV here")
    _add_common(p)
    p.set_defaults(func=cmd_obfuscate)

    p = sub.add_parser("verify", help="check an obfuscated file against its source")
    p.add_argument("normalized")
    p.add_argument("obfuscated")
    p.add_argument("--map", required=True)
    p.add_argument("--log", help="write the verification report here")
    p.add_argument("--samples", type=int, default=60, help="outline spot-check count")
    _add_common(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("all", help="normalize + obfuscate + verify")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--normalized", help="path for the intermediate normalized docx")
    p.add_argument("--map", help="path for the bucket mapping TSV")
    p.add_argument("--verify-log", help="path for the verification report")
    p.add_argument("--samples", type=int, default=60, help="outline spot-check count")
    _add_common(p)
    p.set_defaults(func=cmd_all)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
