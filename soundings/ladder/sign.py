"""Sign or check ``config/standing_orders.yaml``.

``python -m soundings.ladder.sign [--path PATH] [--check]``

The signature is ``orders_digest`` of the loaded ``orders`` mapping: a sha256 over its
canonical YAML. Tamper-evident, not authenticated (DECISIONS.md 23:05). Signing rewrites
only the ``signature:`` line (comments and key order survive) and is idempotent: signing
twice gives a byte-identical file. ``--check`` writes nothing and exits 1 unless the
signature matches.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from soundings.schemas.config import STANDING_ORDERS_PATH

from .standing_orders import SIGNATURE_LABEL, orders_digest, read_document

SIGNATURE_LINE = re.compile(r"^signature:.*$", re.MULTILINE)


def signed_text(text: str, digest: str) -> str:
    """``text`` with its top-level ``signature:`` line set to ``digest`` (appended if absent)."""
    line = f'signature: "{digest}"'
    if SIGNATURE_LINE.search(text):
        return SIGNATURE_LINE.sub(line, text, count=1)
    sep = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{sep}\n{line}\n"


def sign(path: str | Path = STANDING_ORDERS_PATH) -> str:
    """Write the signature into ``path`` and return the digest."""
    p = Path(path)
    digest = orders_digest(read_document(p)["orders"])
    text = p.read_text(encoding="utf-8")
    new = signed_text(text, digest)
    if new != text:
        p.write_text(new, encoding="utf-8")
    return digest


def check(path: str | Path = STANDING_ORDERS_PATH) -> bool:
    """True iff the file's signature equals the digest of its orders. Writes nothing."""
    doc = read_document(path)
    return doc.get("signature") == orders_digest(doc["orders"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m soundings.ladder.sign", description=__doc__)
    parser.add_argument("--path", default=str(STANDING_ORDERS_PATH))
    parser.add_argument("--check", action="store_true", help="verify only; exit 1 on mismatch")
    args = parser.parse_args(argv)
    try:
        if args.check:
            ok = check(args.path)
            print(
                f"{args.path}: {'signature ok' if ok else 'signature MISMATCH'} ({SIGNATURE_LABEL})"
            )
            return 0 if ok else 1
        digest = sign(args.path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"{args.path}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.path}: signed {digest} ({SIGNATURE_LABEL})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
