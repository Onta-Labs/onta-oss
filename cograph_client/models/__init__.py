from cograph_client.models.triple import Triple, TripleCreate, TripleDelete, TripleBatch
from cograph_client.models.query import SPARQLQuery, SPARQLResult, NLQuery, NLResult
from cograph_client.models.function import FunctionRef, FunctionRegister, FunctionResult

__all__ = [
    "Triple", "TripleCreate", "TripleDelete", "TripleBatch",
    "SPARQLQuery", "SPARQLResult", "NLQuery", "NLResult",
    "FunctionRef", "FunctionRegister", "FunctionResult",
]
