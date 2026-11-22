"""Compressed exact site evidence, with read access to older plain HTML captures."""
import gzip
from pathlib import Path
import tempfile


def _compressed(path):
    return path.with_suffix(".html.gz")


def exists(path):
    return _compressed(path).is_file() or path.is_file()


def read(path):
    compressed = _compressed(path)
    raw = gzip.decompress(compressed.read_bytes()) if compressed.is_file() else path.read_bytes()
    return raw.decode("utf-8")


def write(path, html):
    payload = gzip.compress(html.encode("utf-8"), compresslevel=3, mtime=0)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-", suffix=".part", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.close()  # Windows cannot rename the open temporary file.
            temporary.replace(_compressed(path))
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)


def catalog_path(job_dir, page):
    index = page.get('search_scope_index')
    root = job_dir if index is None else job_dir / f'search-{index + 1}'
    return root / f"catalog-{page['number']}.html"
