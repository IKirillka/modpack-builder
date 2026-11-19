import hashlib
import io
import zipfile

import httpx
import pytest

from mod_curator.downloads import download_one, official_url
from mod_curator.models import Cancelled, SourceError
from conftest import file


def jar():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    return stream.getvalue()


@pytest.fixture
def payload():
    data = jar()
    f = file(1)
    f.update(downloadUrl="https://edge.forgecdn.net/files/1/test.jar", fileLength=len(data),
             hashes=[{"algo": 1, "value": hashlib.sha1(data).hexdigest()}])
    return f, data


async def test_download_hash_atomic_and_no_clobber(tmp_path, payload):
    f, data = payload
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as c:
        result = await download_one(c, f, tmp_path)
        assert result["status"] == "downloaded" and result["integrity"] == "platform_hash_verified"
        assert (tmp_path / f["fileName"]).read_bytes() == data
        assert (await download_one(c, f, tmp_path))["status"] == "already_verified"
        saved_hashes = f["hashes"]
        f["hashes"] = [{"algo": 99, "value": "unknown"}]
        with pytest.raises(SourceError, match="EXISTING_FILE_NO_HASH"):
            await download_one(c, f, tmp_path)
        f["hashes"] = saved_hashes
        (tmp_path / f["fileName"]).write_bytes(b"unrelated user file")
        with pytest.raises(SourceError, match="MISMATCH"):
            await download_one(c, f, tmp_path)
        assert (tmp_path / f["fileName"]).read_bytes() == b"unrelated user file"


@pytest.mark.parametrize("failure", ["hash", "network", "length", "html"])
async def test_failure_no_ready_jar(tmp_path, payload, failure, monkeypatch):
    async def sleep(n):
        pass
    monkeypatch.setattr("mod_curator.downloads.asyncio.sleep", sleep)
    f, data = payload
    if failure == "hash":
        f["hashes"][0]["value"] = "0" * 40
    if failure == "length":
        f["fileLength"] += 1
    if failure == "html":
        data = b"<html>error</html>"
        f["fileLength"] = len(data)
        f["hashes"] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500 if failure == "network" else 200, content=data))) as c:
        with pytest.raises(SourceError, match="DOWNLOAD_FAILED"):
            await download_one(c, f, tmp_path)
    assert list(tmp_path.iterdir()) == []


async def test_filename_symlink_and_redirect_escape(tmp_path, payload):
    f, data = payload
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}))) as c:
        with pytest.raises(SourceError, match="UNAPPROVED"):
            await download_one(c, f, tmp_path)
        f["fileName"] = "../escape.jar"
        with pytest.raises(SourceError, match="UNSAFE_FILENAME"):
            await download_one(c, f, tmp_path)
        f["fileName"] = "target.jar"
        outside = tmp_path.parent / "outside.jar"
        outside.write_bytes(b"private")
        (tmp_path / "target.jar").symlink_to(outside)
        with pytest.raises(OSError):
            await download_one(c, f, tmp_path)
        assert outside.read_bytes() == b"private"


async def test_manual_and_cancel(tmp_path, payload):
    f, data = payload
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as c:
        f["downloadUrl"] = None
        assert (await download_one(c, f, tmp_path))["status"] == "manual_required"
        f["downloadUrl"] = "https://edge.forgecdn.net/test.jar"
        def cancel():
            raise Cancelled()
        with pytest.raises(Cancelled):
            await download_one(c, f, tmp_path, cancel)
    assert not list(tmp_path.iterdir())
