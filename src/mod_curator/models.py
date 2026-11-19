from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

LOADERS = {"Forge": (1, 1), "Fabric": (4, 4), "NeoForge": (6, 6), "Quilt": (5, 5)}


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minecraft: str = Field(pattern=r"^[A-Za-z0-9._ -]{1,40}$")
    loader: Literal["Forge", "Fabric", "NeoForge", "Quilt"]
    categories: list[str] = Field(default_factory=list)
    excluded_categories: list[str] = Field(default_factory=list)
    search: str = Field(default="", max_length=200)
    minimum_downloads: int = Field(ge=0)
    page_size: Literal[10, 20, 50] = 20
    wishes: str = Field(min_length=1, max_length=30000)
    destination: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    allow_prerelease: bool = False
    release_types: list[Literal[1, 2, 3]] | None = Field(default=None, min_length=1)

    @field_validator("categories", "excluded_categories")
    @classmethod
    def category_slugs(cls, values):
        import re
        if any(not re.fullmatch(r"[a-z0-9-]+", v) for v in values):
            raise ValueError("Category must be a CurseForge slug")
        return list(dict.fromkeys(values))


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    functions: str = Field(min_length=1)
    fit: str = Field(min_length=1)
    include: bool
    explanation: str = Field(min_length=1)
    uncertainties: list[str]


class SourceError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass
