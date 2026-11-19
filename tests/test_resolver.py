import pytest

from mod_curator.models import SourceError
from mod_curator.resolver import resolve
from conftest import FakeSource, file


async def test_shared_and_cyclic_dependencies(filters):
    source = FakeSource(files={1: [file(1, [3])], 2: [file(2, [3])], 3: [file(3, [1])]})
    result = await resolve(source, [1, 2], filters)
    assert set(result["files"]) == {"1", "2", "3"}
    assert source.file_calls.count(3) == 1


@pytest.mark.parametrize("files", [
    {1: [file(1, [2])]},
    {1: [file(1, [{"modId": 2, "relationType": 5}])], 2: [file(2)]},
    {1: [file(1, [{"modId": 3, "relationType": 3, "fileId": 301}])],
     2: [file(2, [{"modId": 3, "relationType": 3, "fileId": 302}])], 3: [file(3, fid=301), file(3, fid=302)]},
])
async def test_unresolvable(filters, files):
    with pytest.raises(SourceError, match="DEPENDENCIES_UNRESOLVED"):
        await resolve(FakeSource(files=files), [1, 2], filters)


async def test_backtracking_and_optional(filters):
    bad = file(1, [9], fid=101)
    bad["fileDate"] = "2026-10-10"
    good = file(1, [{"modId": 7, "relationType": 2}], fid=100)
    result = await resolve(FakeSource(files={1: [bad, good]}), [1], filters)
    assert result["files"]["1"]["id"] == 100
    assert result["optional_not_installed"][0]["modId"] == 7


@pytest.mark.parametrize("tags", [["1.20.1", "NeoForge"], ["1.21.1", "Forge"], ["1.21.1"]])
async def test_no_compatible_file(filters, tags):
    f = file(1)
    f["gameVersions"] = tags
    with pytest.raises(SourceError, match="No consistent"):
        await resolve(FakeSource(files={1: [f]}), [1], filters)


async def test_removal_recalculates_dependencies(filters):
    source = FakeSource(files={1: [file(1, [3])], 2: [file(2, [4])], 3: [file(3)], 4: [file(4)]})
    assert set((await resolve(source, [1, 2], filters))["files"]) == {"1", "2", "3", "4"}
    assert set((await resolve(source, [1], filters))["files"]) == {"1", "3"}
