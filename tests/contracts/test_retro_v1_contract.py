import re
from pathlib import Path

import jsonschema
import yaml
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "contracts/retro/v1/openapi.yaml"
EXAMPLES = ROOT / "contracts/retro/v1/examples"
OPERATIONS = {
    "retro_day",
    "retro_symbol",
    "retro_funnel",
    "retro_scoreboard",
    "retro_positions",
    "mock_experiments",
    "get_bars",
    "data_gaps",
}
FORBIDDEN = {
    "account_no",
    "account_number",
    "broker_order_id",
    "order_no",
    "odno",
    "raw_payload",
    "raw_response",
    "raw_exception",
    "exception",
}


def document():
    return yaml.safe_load(SPEC.read_text())


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def test_operations_and_envelopes():
    spec = document()
    operations = [v for p in spec["paths"].values() for v in p.values()]
    assert len(operations) == 8
    assert {o["operationId"] for o in operations} == OPERATIONS
    for operation in operations:
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        response = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
        assert {"$ref": "#/components/schemas/Envelope"} in response["allOf"]


def test_closed_projection_and_decimal_types():
    for node in walk(document()["components"]["schemas"]):
        for name, schema in node.get("properties", {}).items():
            assert name.lower() not in FORBIDDEN
            if re.search(r"(money|amount|quantity|qty|price)", name):
                assert schema.get("$ref") == "#/components/schemas/DecimalValue", name


def test_examples_validate():
    spec = document()
    registry = Registry().with_resource(
        "urn:retro",
        Resource.from_contents(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", **spec}
        ),
    )
    operations = {
        op["operationId"]: op for path in spec["paths"].values() for op in path.values()
    }
    seen = set()
    for file in sorted(EXAMPLES.glob("*.yaml")):
        example = yaml.safe_load(file.read_text())
        op = operations[example["operation"]]
        seen.add(example["operation"])
        status = str(example["status"])
        schema = op["responses"][status]["content"]["application/json"]["schema"]
        validator = jsonschema.Draft202012Validator(
            schema, registry=registry, _resolver=registry.resolver("urn:retro")
        )
        validator.validate(example["response"])
    assert seen == OPERATIONS
