import re

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel

class CamelModel(BaseModel):

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )


# Everything except \t \n \r — those are legitimate in a multi-line field
# (a job description). A raw NUL byte in particular isn't just bad input:
# asyncpg/Postgres reject it outright with CharacterNotInRepertoireError,
# which (before this) surfaced to the client as a misleading 503
# "service_unavailable" instead of a 422 telling them what was actually
# wrong with their request. The rest of the C0 range plus DEL are blocked
# too as cheap insurance against control-character/log-injection tricks.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class NoControlCharsMixin:
    # Opt-in (not baked into CamelModel itself) so it only applies to
    # request schemas carrying free-text user input — response schemas
    # built from our own trusted data (DB rows, AI-generated summaries)
    # have no attacker-controlled content to police here.
    @field_validator("*", mode="before")
    @classmethod
    def _reject_control_characters(cls, value):
        if isinstance(value, str) and _CONTROL_CHAR_RE.search(value):
            raise ValueError("This field contains invalid control characters.")
        return value