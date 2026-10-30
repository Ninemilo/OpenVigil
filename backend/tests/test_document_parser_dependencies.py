"""The isolated parser's runtime identity must match its shipped hash lock."""

from pathlib import Path

from windops_backend.document_parse_identity import DOCUMENT_PARSER_VERSIONS
from windops_backend.structural_dependencies import locked_distributions


def test_document_parser_numerical_contract_matches_the_shipped_lock() -> None:
    lock = Path(__file__).parents[1] / "requirements.document-parser.txt"
    packages = locked_distributions(lock)
    assert {name: packages[name].version for name in DOCUMENT_PARSER_VERSIONS} == (
        DOCUMENT_PARSER_VERSIONS
    )
