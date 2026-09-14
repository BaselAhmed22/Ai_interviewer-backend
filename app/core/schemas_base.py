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


# Everything except \t \n \r, which are legitimate in a multi-line field.
# A raw NUL in particular makes asyncpg reject the query outright
# (CharacterNotInRepertoireError), surfacing as a misleading 503 instead
# of a 422 — the rest of the C0 range plus DEL are blocked as cheap
# insurance against control-character/log-injection tricks.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class NoControlCharsMixin:
    # Opt-in, not baked into CamelModel — only request schemas carry
    # attacker-controlled input; response schemas built from our own data don't.
    @field_validator("*", mode="before")
    @classmethod
    def _reject_control_characters(cls, value):
        if isinstance(value, str) and _CONTROL_CHAR_RE.search(value):
            raise ValueError("This field contains invalid control characters.")
        return value