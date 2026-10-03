"""Request bodies of the local API, validated by FastAPI (a bad body is answered with 422 and a field message).

Unknown fields are ignored, so a page that is a version ahead or behind the server still works.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FiltersPatch(_Body):
    states: list[str] | None = None
    city: str | None = None
    remote_only: bool | None = None
    types: list[str] | None = None
    min_salary: float | None = Field(None, ge=0)
    include_no_salary: bool | None = None
    require_optional: bool | None = None
    show_hidden: bool | None = None


class SettingsPatch(_Body):
    """Only the fields you send change; filters and profile are merged into what is stored."""
    mandatory: str | None = None
    optional: str | None = None
    current_employer: str | None = None
    disabled_companies: list[str] | None = None
    filters: FiltersPatch | None = None
    profile: dict[str, str] | None = None
    engine: Literal["auto", "onnx"] | None = None
    upload_format: Literal["docx", "pdf"] | None = None
    max_bullets: int | None = Field(None, ge=0, le=40)

    def values(self) -> dict:
        return self.model_dump(exclude_unset=True, exclude_none=True)


class CompanyIn(_Body):
    name: str = ""
    url: str = ""


class HideIn(_Body):
    hidden: bool = True


class ScoreIn(_Body):
    resume: dict


class TailoredIn(_Body):
    doc: dict
    score_after: int | None = None
    approved: list[str] = []
    rejected: list[str] = []

    @field_validator("doc")
    @classmethod
    def _has_resume(cls, v):
        if not isinstance(v.get("resume"), dict):
            raise ValueError("doc.resume must be the tailored resume")
        return v


class PackageIn(TailoredIn):
    launch: bool = False


class PreviewIn(_Body):
    data: dict
    filename: str | None = None
    how: str | None = None


class ResumeSaveIn(_Body):
    yaml: str
    filename: str | None = None
    new_upload: bool = False


class RunIn(_Body):
    full_refresh: bool = False


class PasswordIn(_Body):
    password: str = Field("", max_length=512)
    company: str = Field("", max_length=60)  # e.g. NVIDIA for NVIDIA_WORKDAY_PASSWORD; empty: the general one


class PathIn(_Body):
    path: str


class OpenIn(_Body):
    what: str = ""
