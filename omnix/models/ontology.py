from pydantic import BaseModel, Field


class AttributeDefinition(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    datatype: str = Field(default="string", description="string, integer, float, boolean, datetime, uri, or a type name for relationships")


class TypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parent_type: str | None = Field(default=None, description="Parent type name for subtype relationship")
    attributes: list[AttributeDefinition] = Field(default_factory=list)


class TypeResponse(BaseModel):
    name: str
    description: str = ""
    parent_type: str | None = None
    attributes: list[AttributeDefinition] = Field(default_factory=list)
    subtypes: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)


class AttributeAdd(BaseModel):
    attributes: list[AttributeDefinition] = Field(min_length=1)


class SubtypeAdd(BaseModel):
    subtype: str = Field(min_length=1, description="Name of the child type")
