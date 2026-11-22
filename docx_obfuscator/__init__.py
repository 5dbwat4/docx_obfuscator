# -*- coding: utf-8 -*-
"""docx_obfuscator: font-based DOCX anti-copy obfuscation toolkit."""
from __future__ import annotations

__version__ = "2.0.0"

from .config import Options
from .normalize import normalize
from .obfuscate import obfuscate_docx
from .verify import verify

__all__ = ["Options", "normalize", "obfuscate_docx", "verify", "__version__"]
