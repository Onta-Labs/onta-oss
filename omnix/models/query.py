from pydantic import BaseModel, Field


class SPARQLQuery(BaseModel):
    query: str = Field(description="SPARQL 1.1 query string")


class SPARQLResult(BaseModel):
    bindings: list[dict[str, str]] = Field(default_factory=list)
    vars: list[str] = Field(default_factory=list)


class SPARQLUpdate(BaseModel):
    update: str = Field(description="SPARQL 1.1 Update string")


class NLQuery(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    kg_name: str | None = Field(default=None, description="Query a specific knowledge graph")
    model: str | None = Field(default=None, description="Override the query generation model (OpenRouter model ID)")


class NLResult(BaseModel):
    answer: str
    sparql: str
    explanation: str
    ontology: str = Field(default="", description="Ontology summary text passed to the LLM for SPARQL generation")
    functions_invoked: list[str] = Field(default_factory=list)
    timing: dict[str, float | str] = Field(default_factory=dict, description="Stage latencies in ms and metadata")
