import json

import httpx
import pytest

from mod_curator import credentials
from mod_curator.cli import curseforge_key_command
from mod_curator.models import SourceError
from mod_curator.source import API_BASE, MINECRAFT_CHECK_PROJECT, CurseForge

DUMMY = "fixture-secret-for-tests"


class Vault:
    def __init__(self):
        self.items = {}

    def set_password(self, service, account, value):
        self.items[service, account] = value

    def get_password(self, service, account):
        return self.items.get((service, account))

    def delete_password(self, service, account):
        del self.items[service, account]


@pytest.fixture
def vault(monkeypatch):
    backend = Vault()
    monkeypatch.delenv(credentials.ENV, raising=False)
    monkeypatch.setattr(credentials, "system_keyring", lambda: backend)
    return backend


async def test_cli_checks_key_before_saving_and_status_is_local(monkeypatch, tmp_path, vault):
    monkeypatch.setattr(credentials.getpass, "getpass", lambda _: DUMMY)
    async def denied(_):
        raise SourceError("API_DENIED")
    monkeypatch.setattr(CurseForge, "check_api_access", denied)
    with pytest.raises(SourceError, match="API_DENIED"):
        await curseforge_key_command("set", tmp_path)
    assert not vault.items
    assert (await curseforge_key_command("status", tmp_path))["configured"] is False

    async def accepted(source):
        assert source.api_key == DUMMY
    monkeypatch.setattr(CurseForge, "check_api_access", accepted)
    assert (await curseforge_key_command("set", tmp_path))["minecraft_api"] == "verified"
    assert credentials.effective_key(tmp_path) == DUMMY
    assert not (tmp_path / credentials.MARKER).read_bytes()
    assert credentials.effective_key(tmp_path / "other") is None
    assert DUMMY not in json.dumps(await curseforge_key_command("status", tmp_path))

    monkeypatch.setenv(credentials.ENV, "environment-override")
    assert credentials.effective_key(tmp_path) == "environment-override"
    result = await curseforge_key_command("unset", tmp_path)
    assert result == {"configured": True, "source": "environment", "removed_from_keyring": True}
    assert not vault.items


async def test_metadata_uses_keychain_and_catalogue_request_does_not_send_key(tmp_path, vault, filters):
    credentials.store_key(tmp_path, DUMMY)
    source = CurseForge({}, credential_root=tmp_path)
    calls = []

    def endpoint(request):
        calls.append(request)
        if request.url.host == "api.curseforge.com":
            assert request.headers["x-api-key"] == DUMMY
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"data": [{"id": 7, "modId": MINECRAFT_CHECK_PROJECT}],
                                                "pagination": {"index": 0, "resultCount": 1, "totalCount": 1}})
            return httpx.Response(200, json={"data": {"id": MINECRAFT_CHECK_PROJECT, "name": "Create",
                                                    "gameId": 432, "classId": 6, "slug": "create", "downloadCount": 1}})
        assert "x-api-key" not in request.headers
        return httpx.Response(200, text="catalogue")

    await source.client.aclose()
    source.client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    try:
        assert source.metadata_provider == "curseforge_api"
        await source.check_api_access()
        files = await source.files(MINECRAFT_CHECK_PROJECT, filters)
        assert files[0]["_metadata_source"] == "curseforge_api"
        assert files[0]["_dependencies_source"].startswith(API_BASE)
        await source.html("https://www.curseforge.com/minecraft/search")
        with pytest.raises(SourceError, match="API_ENDPOINT_REFUSED"):
            await source.request("https://example.org/v1/mods/1", api=True)
        assert len(calls) == 4
    finally:
        await source.close()


@pytest.mark.parametrize("status,code", [(301, "API_REDIRECT_REFUSED"), (401, "API_DENIED"), (403, "API_DENIED")])
async def test_api_denials_and_redirects_do_not_expose_the_key(monkeypatch, tmp_path, status, code):
    monkeypatch.setenv(credentials.ENV, DUMMY)
    source = CurseForge({}, credential_root=tmp_path)
    calls = []

    def endpoint(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.org/v1/leak"}, text=DUMMY)

    await source.client.aclose()
    source.client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint), follow_redirects=True)
    try:
        with pytest.raises(SourceError, match=code) as exc:
            await source.api("mods/328085")
        assert DUMMY not in str(exc.value) and len(calls) == 1
    finally:
        await source.close()


async def test_check_without_key_fails_before_network(tmp_path, vault):
    with pytest.raises(SourceError, match="API_KEY_REQUIRED"):
        await curseforge_key_command("check", tmp_path)


def test_broken_keyring_reports_safe_error(tmp_path, vault, monkeypatch):
    credentials.store_key(tmp_path, DUMMY)

    def failed(*_):
        raise RuntimeError(DUMMY)

    monkeypatch.setattr(vault, "get_password", failed)
    with pytest.raises(SourceError, match="API_KEYRING_UNAVAILABLE") as exc:
        credentials.effective_key(tmp_path)
    assert DUMMY not in str(exc.value)


def test_console_url_is_not_a_key():
    with pytest.raises(SourceError, match="API_KEY_INVALID_FORMAT"):
        credentials.validate_key("https://console.curseforge.com/#/api-keys")
