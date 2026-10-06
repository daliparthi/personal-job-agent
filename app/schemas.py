"""Request bodies of the local API, validated by FastAPI (a bad body is answered with 422 and a field message).

Unknown fields are ignored, so a page that is a version ahead or behind the server still works.
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    hide_knockouts: bool | None = None
    sponsorship: Literal["any", "offered", "h4ead", "not_denied"] | None = None
    sort: Literal["match_salary", "match_date", "date_salary", "date_match", "salary_date", "salary_match",
                  "date_oldest"] | None = None


class AutofillPatch(_Body):
    experience: bool | None = None
    add_entries: bool | None = None
    answers: bool | None = None
    capture: bool | None = None


Disclosure = Literal["decline", "skip"]


class DisclosuresPatch(_Body):
    gender: Disclosure | None = None
    ethnicity: Disclosure | None = None
    veteran: Disclosure | None = None
    disability: Disclosure | None = None


class AnswerIn(_Body):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=4000)
    kind: Literal["text", "choice"] = "text"


class AnswerPatch(_Body):
    question: str | None = Field(None, min_length=1, max_length=500)
    answer: str | None = Field(None, min_length=1, max_length=4000)
    kind: Literal["text", "choice"] | None = None

    def values(self) -> dict:
        return self.model_dump(exclude_unset=True, exclude_none=True)


class SettingsPatch(_Body):
    """Only the fields you send change; filters, profile, autofill and disclosures are merged into what is stored."""
    mandatory: str | None = None
    optional: str | None = None
    current_employer: str | None = None
    disabled_companies: list[str] | None = None
    filters: FiltersPatch | None = None
    profile: dict[str, str] | None = None
    engine: Literal["auto", "onnx"] | None = None
    upload_format: Literal["docx", "pdf"] | None = None
    max_bullets: int | None = Field(None, ge=0, le=40)
    keep_new_days: int | None = Field(None, ge=1, le=365)
    ghost_after_days: int | None = Field(None, ge=3, le=365)
    autofill: AutofillPatch | None = None
    disclosures: DisclosuresPatch | None = None

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
    search_id: int | None = None  # run a saved search (else the keywords in Settings)


class StageIn(_Body):
    status: str
    note: str | None = Field(None, max_length=2000)


class NoteIn(_Body):
    note: str = Field(..., min_length=1, max_length=4000)


class FollowUpIn(_Body):
    at: date | None = None  # None clears the follow-up
    action: str = Field("", max_length=200)


class FeedbackIn(_Body):
    value: Literal[-1, 0, 1]  # thumbs down, cleared, thumbs up


class PasswordIn(_Body):
    password: str = Field("", max_length=512)
    company: str = Field("", max_length=60)  # e.g. NVIDIA for NVIDIA_WORKDAY_PASSWORD; empty: the general one


class PathIn(_Body):
    path: str


class OpenIn(_Body):
    what: str = ""


class SavedSearchIn(_Body):
    name: str = Field(..., min_length=1, max_length=80)
    mandatory: str = Field("", max_length=500)
    optional: str = Field("", max_length=500)
    every_hours: int | None = Field(None, ge=1, le=168)        # None: only when you click Run
    notify_min_score: int | None = Field(None, ge=0, le=100)   # None: no alerts
    enabled: bool = True

    @model_validator(mode="after")
    def _has_keywords(self):
        if not (self.mandatory.strip() or self.optional.strip()):
            raise ValueError("a saved search needs at least one mandatory or optional keyword")
        return self


class SavedSearchPatch(_Body):
    name: str | None = Field(None, min_length=1, max_length=80)
    mandatory: str | None = Field(None, max_length=500)
    optional: str | None = Field(None, max_length=500)
    every_hours: int | None = Field(None, ge=1, le=168)
    notify_min_score: int | None = Field(None, ge=0, le=100)
    enabled: bool | None = None

    def values(self) -> dict:
        # Only the schedule and the alert threshold can be cleared: every_hours: null turns the schedule off.
        clearable = {"every_hours", "notify_min_score"}
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if v is not None or k in clearable}


class AlertsSeenIn(_Body):
    ids: list[int] | None = None  # None: all
