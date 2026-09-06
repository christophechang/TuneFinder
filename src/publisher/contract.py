"""The publish-pool wire contract: schema gate for the payloads publish-pool
posts to TuneFinder's multi-tenant API (see tools/publish-pool-contract/README.md).

The vendored JSON Schema files (draft 2020-12) are the description; the API's
own hand-written validator is the enforcement, and this module mirrors it in
two ways:

  * `validate(name, payload)` runs the payload through the matching schema
    (with cross-file `$ref`s resolved via a `referencing.Registry`), raising
    `ContractError` on the first failure.
  * `check_item_relations(item)` re-implements the four rules the README says
    no JSON Schema can express — `family_mismatch`, `duplicate_source`,
    `bad_primary_source` and `bad_observation` — so the publisher can refuse
    to post an item the API would refuse anyway, without a network round trip.

Validators are built with `jsonschema.FormatChecker()`. Its default checker
verifies `format: date` natively (`datetime.date.fromisoformat`), which is why
`bad_date.json` (wrong only by that format) fails as its name says. `date-time`
and `uri` are checked only when their optional validator packages
(`rfc3339-validator`, `rfc3987`) are installed; this project adds neither, so
those two formats stay annotations here exactly as they are in the API, and
the payload builder is what has to get them right.

One rule the validator does *not* cover: an observation dated later than
tomorrow, UTC. That is the API service's own clock comparison, not a relation
between fields in one payload, so nothing offline can check it —
`bad_observation.future.json` is valid against the schema *and* accepted by
`check_item_relations`, and is refused only by the live endpoint.
"""
import json
import os
from functools import lru_cache

import jsonschema
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

CONTRACT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools",
    "publish-pool-contract",
)

SCHEMA_NAMES = ("batch", "manifest", "artists", "ingest-config")

# The four rules README §"The schema is the description..." says no schema
# can express — mirrored by check_item_relations() below.
RELATIONAL_INVALID_SAMPLES = {
    "family_mismatch",
    "duplicate_source",
    "bad_primary_source",
    "bad_observation",
}

# Valid against the schema *and* accepted by check_item_relations — refused
# only by the API service's own clock comparison.
SERVICE_ONLY_INVALID_SAMPLES = {"bad_observation.future"}


class ContractError(ValueError):
    """Raised by validate() for the first schema error in a payload."""

    def __init__(self, message: str, path: str):
        super().__init__(message)
        self.message = message
        self.path = path


def _load_schema(name: str) -> dict:
    with open(os.path.join(CONTRACT_DIR, f"{name}.schema.json")) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """The four vendored schemas as a referencing.Registry, keyed by their own
    `$id` — this is what lets manifest.schema.json's
    `batch.schema.json#/$defs/run_id` resolve against
    https://tunefinder.setfolio.app/schemas/publish-pool/v1/."""
    resources = [Resource.from_contents(_load_schema(name)) for name in SCHEMA_NAMES]
    return Registry().with_resources((resource.id(), resource) for resource in resources)


@lru_cache(maxsize=None)
def validator(name: str) -> jsonschema.Draft202012Validator:
    """A Draft202012Validator for one of SCHEMA_NAMES, cached per name."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"Unknown publish-pool contract schema: {name!r}")
    schema = _load_schema(name)
    return jsonschema.Draft202012Validator(
        schema, registry=load_registry(), format_checker=jsonschema.FormatChecker()
    )


def validate(name: str, payload: dict) -> None:
    """Validate payload against SCHEMA_NAMES member `name`.

    Raises ContractError for the first error (by jsonschema's best_match), with
    `.path` set to the JSON pointer of the failing value (e.g. "/items/0/bpm")
    and `.message` to the error's own message. Returns None on success.
    """
    errors = list(validator(name).iter_errors(payload))
    if not errors:
        return
    error = best_match(errors)
    path = "/" + "/".join(str(part) for part in error.absolute_path)
    raise ContractError(error.message, path)


@lru_cache(maxsize=1)
def _fine_genre_to_family() -> dict:
    # Deferred import: taxonomy.py imports CONTRACT_DIR from this module, so
    # importing taxonomy at module load time here would be circular.
    from src.publisher.taxonomy import load_taxonomy

    return load_taxonomy().fine_genres


def check_item_relations(item: dict) -> str | None:
    """The four relations no JSON Schema can express (README, "The schema is
    the description; the API's validator is the enforcement"):

      * `family_mismatch` — `families` isn't exactly the set of families the
        item's `fine_genres` belong to.
      * `duplicate_source` — two entries of `sources` name the same source.
      * `bad_primary_source` — `primary_source` isn't one of the item's own
        `sources`.
      * `bad_observation` — `observation.source` isn't one of the item's own
        `sources`, or `observation.chart_position` is below 1.

    Returns the first violated reason, or None if the item satisfies all four.
    Assumes `item` already validated against batch.schema.json's `item` def
    (fine_genres/families/sources entries are well-formed enum members).
    """
    fine_to_family = _fine_genre_to_family()
    expected_families = {
        fine_to_family[fine_genre]
        for fine_genre in item.get("fine_genres", [])
        if fine_genre in fine_to_family
    }
    if expected_families != set(item.get("families", [])):
        return "family_mismatch"

    source_names = [source.get("source") for source in item.get("sources", [])]
    if len(set(source_names)) != len(source_names):
        return "duplicate_source"

    if item.get("primary_source") not in source_names:
        return "bad_primary_source"

    observation = item.get("observation", {})
    if observation.get("source") not in source_names:
        return "bad_observation"
    chart_position = observation.get("chart_position")
    if chart_position is not None and chart_position < 1:
        return "bad_observation"

    return None
