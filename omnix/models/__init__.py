from omnix.models.triple import Triple, TripleCreate, TripleDelete, TripleBatch
from omnix.models.query import SPARQLQuery, SPARQLResult, NLQuery, NLResult
from omnix.models.function import FunctionRef, FunctionRegister, FunctionResult

__all__ = [
    "Triple", "TripleCreate", "TripleDelete", "TripleBatch",
    "SPARQLQuery", "SPARQLResult", "NLQuery", "NLResult",
    "FunctionRef", "FunctionRegister", "FunctionResult",
]
