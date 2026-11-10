import copy
import re
from pathlib import Path

import jsonschema
import pytest
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
    "accountno",
    "accountnumber",
    "accountid",
    "brokerorderid",
    "orderno",
    "odno",
    "rawpayload",
    "rawresponse",
    "rawerror",
    "rawexception",
    "errordetail",
    "stacktrace",
    "traceback",
    "exception",
}


def normalized(name):
    return re.sub(r"[_-]", "", name.lower())


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
    assert spec["security"] == [{"RetroReadToken": []}]
    assert spec["components"]["securitySchemes"]["RetroReadToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Retro-Read-Token",
    }
    for operation in operations:
        assert "security" not in operation or operation["security"] == spec["security"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        response = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
        assert {"$ref": "#/components/schemas/Envelope"} in response["allOf"]


def test_closed_projection_and_decimal_types():
    spec = document()
    for component, definition in spec["components"]["schemas"].items():
        for node in walk(definition):
            for name, schema in node.get("properties", {}).items():
                assert normalized(name) not in FORBIDDEN
                if schema.get("type") == "string" or (
                    isinstance(schema.get("type"), list) and "string" in schema["type"]
                ):
                    if (component, name) != ("Error", "message"):
                        assert any(
                            k in schema for k in ("enum", "pattern", "format", "const")
                        ), name
                if re.search(r"(money|amount|quantity|qty|price)", name):
                    assert schema.get("$ref") == "#/components/schemas/DecimalValue", (
                        name
                    )
    for path in spec["paths"].values():
        for operation in path.values():
            for parameter in operation["parameters"]:
                assert normalized(parameter["name"]) not in FORBIDDEN


def null_paths(value, path):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from null_paths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from null_paths(child, f"{path}.{index}")
    elif value is None:
        yield path


def test_examples_validate():
    spec = document()
    registry = Registry().with_resource(
        "urn:retro",
        Resource.from_contents(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", **spec}
        ),
    )
    operations = {
        op["operationId"]: (path_name, op)
        for path_name, path in spec["paths"].items()
        for op in path.values()
    }
    seen = set()
    for file in sorted(EXAMPLES.glob("*.yaml")):
        example = yaml.safe_load(file.read_text())
        path_name, op = operations[example["operation"]]
        assert example["request"]["path"] == path_name
        params = {p["name"]: p for p in op["parameters"]}
        query = example["request"]["query"]
        assert set(query) <= set(params)
        if example["status"] == 200:
            assert {n for n, p in params.items() if p.get("required")} <= set(query)
        for name, value in query.items():
            jsonschema.Draft202012Validator(
                params[name]["schema"], format_checker=jsonschema.FormatChecker()
            ).validate(value)
        seen.add(example["operation"])
        status = str(example["status"])
        schema = op["responses"][status]["content"]["application/json"]["schema"]
        validator = jsonschema.Draft202012Validator(
            schema,
            registry=registry,
            _resolver=registry.resolver("urn:retro"),
            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
        )
        validator.validate(example["response"])
        if example["status"] == 200:
            response = example["response"]
            unknowns = {item["field"] for item in response["unknowns"]}
            for i, row in enumerate(response["data"]):
                for path in null_paths(row, f"data.{i}"):
                    assert (
                        row.get("reason")
                        or row.get("missing_reason")
                        or path in unknowns
                    ), path
    assert seen == OPERATIONS


def test_experiment_counts_reject_negative_values():
    spec = document()
    example = yaml.safe_load((EXAMPLES / "mock_experiments.yaml").read_text())
    registry = Registry().with_resource(
        "urn:retro",
        Resource.from_contents(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", **spec}
        ),
    )
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/components/schemas/MockExperimentsResponse"},
        registry=registry,
        _resolver=registry.resolver("urn:retro"),
    )
    for field in ("planned_count", "sent_count", "filled_count", "uncertain_count"):
        response = copy.deepcopy(example["response"])
        response["data"][0][field] = -1
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(response)


def test_null_total_requires_count_unknown():
    spec = document()
    example = yaml.safe_load((EXAMPLES / "unknown_total.yaml").read_text())
    registry = Registry().with_resource(
        "urn:retro",
        Resource.from_contents(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", **spec}
        ),
    )
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/components/schemas/DataGapsResponse"},
        registry=registry,
        _resolver=registry.resolver("urn:retro"),
    )
    example["response"]["unknowns"] = []
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(example["response"])
