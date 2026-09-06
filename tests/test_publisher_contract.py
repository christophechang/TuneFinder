"""Tests for the publish-pool schema gate (src/publisher/contract.py).

Validates the vendored samples against the vendored schemas, and checks the
four relational rules the README says no schema can express (mirrored here
so the publisher never posts an item the API would refuse).
"""
import json
import os

import pytest

from src.publisher.contract import (
    CONTRACT_DIR,
    RELATIONAL_INVALID_SAMPLES,
    SCHEMA_NAMES,
    SERVICE_ONLY_INVALID_SAMPLES,
    ContractError,
    check_item_relations,
    validate,
)
from src.publisher.taxonomy import load_taxonomy

SAMPLES_DIR = os.path.join(CONTRACT_DIR, "samples")
INVALID_DIR = os.path.join(SAMPLES_DIR, "invalid")
INVALID_SAMPLE_FILES = sorted(os.listdir(INVALID_DIR))


def _load(path):
    with open(path) as f:
        return json.load(f)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_samples_validate(name):
    payload = _load(os.path.join(SAMPLES_DIR, f"{name}.json"))
    validate(name, payload)  # must not raise


@pytest.mark.parametrize("filename", INVALID_SAMPLE_FILES)
def test_invalid_samples_fail_schema_except_relational_and_service_only(filename):
    path = os.path.join(INVALID_DIR, filename)
    stem = filename[: -len(".json")]

    if stem == "malformed_json":
        with open(path) as f:
            with pytest.raises(json.JSONDecodeError):
                json.load(f)
        return

    payload = _load(path)

    if stem in SERVICE_ONLY_INVALID_SAMPLES:
        # Valid against the schema and accepted by the relational check — the
        # future-date rule is the API service's own clock comparison.
        validate("batch", payload)
        assert check_item_relations(payload["items"][0]) is None
        return

    reason = stem.split(".")[0]
    if reason in RELATIONAL_INVALID_SAMPLES:
        # Valid against the schema; caught only by the relational check.
        validate("batch", payload)
        assert check_item_relations(payload["items"][0]) == reason
        return

    with pytest.raises(ContractError):
        validate("batch", payload)


def test_validate_reports_path_of_first_error():
    payload = _load(os.path.join(INVALID_DIR, "bad_bpm.json"))
    with pytest.raises(ContractError) as exc_info:
        validate("batch", payload)
    assert exc_info.value.path.endswith("items/0/bpm")


def test_schema_enums_match_taxonomy():
    schema = _load(os.path.join(CONTRACT_DIR, "batch.schema.json"))
    taxonomy = load_taxonomy()
    assert set(schema["$defs"]["family"]["enum"]) == set(taxonomy.families)
    assert set(schema["$defs"]["fine_genre"]["enum"]) == set(taxonomy.fine_genres)


def test_check_relations_accepts_sample_items():
    payload = _load(os.path.join(SAMPLES_DIR, "batch.json"))
    for item in payload["items"]:
        assert check_item_relations(item) is None
