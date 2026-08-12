from pydantic import BaseModel, Field, field_validator
import re

class Structure(BaseModel):
    title_zh: str = Field(description="translate the paper title into concise Chinese")
    summary_zh: str = Field(description="summarize the abstract in concise Chinese")
    tldr: str = Field(description="generate a too long; didn't read summary")
    motivation: str = Field(description="describe the motivation in this paper")
    method: str = Field(description="method of this paper")
    result: str = Field(description="result of this paper")
    conclusion: str = Field(description="conclusion of this paper")
