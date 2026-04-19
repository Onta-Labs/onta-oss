from pydantic import BaseModel, Field


class Triple(BaseModel):
    subject: str = Field(description="RDF subject URI or blank node")
    predicate: str = Field(description="RDF predicate URI")
    object: str = Field(description="RDF object (URI, literal, or blank node)")


class TripleCreate(BaseModel):
    triples: list[Triple] = Field(min_length=1, max_length=1000)


class TripleDelete(BaseModel):
    triples: list[Triple] = Field(min_length=1, max_length=1000)


class TripleBatch(BaseModel):
    inserted: int = 0
    deleted: int = 0
