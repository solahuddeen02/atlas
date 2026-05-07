from pydantic import BaseModel, Field, field_validator


class ShareRequest(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name cannot be blank")
        return v
