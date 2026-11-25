"""Operator-side extraction of unreviewed source candidates from one named file."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys


def extract(raw, suffix):
    if not 0 < len(raw) <= 8 * 1024 * 1024:
        raise ValueError("source size outside bounds")
    sha = hashlib.sha256(raw).hexdigest()
    rows = []

    def append(quote, **loc):
        if len(rows) >= 2048:
            raise ValueError("candidate limit exceeded")
        rows.append(dict(quote=quote, file_sha256=sha, **loc))

    if suffix.lower() in (".md", ".markdown"):
        pos, heading = 0, ""
        for n, line in enumerate(raw.decode("utf-8").splitlines(keepends=True), 1):
            text = line.rstrip("\r\n")
            if text.startswith("#"):
                heading = text[:120]
            for i in range(0, len(text), 800):
                part = text[i:i+800]
                size = len(part.encode())
                if part.strip():
                    append(part, locator=f"line {n}: {heading}", byte_start=pos, byte_end=pos+size)
                pos += size
            pos += len(line[len(text):].encode())
    elif suffix.lower() == ".pdf":
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(raw)).pages
        if len(pages) > 128:
            raise ValueError("page limit exceeded")
        for n, page in enumerate(pages, 1):
            text = (page.extract_text() or "").strip()
            if not text:
                raise ValueError(f"no extractable text on physical page {n}")
            for i in range(0, len(text), 800):
                if part := text[i:i+800].strip():
                    append(part, locator=f"physical page {n}", physical_page=n)
    else:
        raise ValueError("unsupported format")
    if not rows:
        raise ValueError("no extractable text")
    return {"state": "unreviewed", "candidates": rows}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", type=Path)
    path = p.parse_args(argv).file
    try:
        with path.open("rb") as stream:
            result = extract(stream.read(8*1024*1024+1), path.suffix)
    except Exception as exc:
        print(f"Extraction failed ({type(exc).__name__}).", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
