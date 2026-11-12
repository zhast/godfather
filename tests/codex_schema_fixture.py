"""Validate the JSON Schema keywords used by the generated approval responses.

Kept in stdlib so the hermetic suite needs no optional jsonschema package.
The fixture contains the unmodified Codex 0.167 generated response schemas.
"""
import json
from pathlib import Path

SCHEMAS = json.loads((Path(__file__).parent / 'fixtures/codex-approval-schemas.json').read_text())


def validate(method, value):
    root = SCHEMAS[method]

    def matches(schema, value):
        if isinstance(schema, bool):
            return schema
        if '$ref' in schema:
            target = root
            for key in schema['$ref'].split('/')[1:]:
                target = target[key]
            return matches(target, value)
        if 'oneOf' in schema and sum(matches(s, value) for s in schema['oneOf']) != 1:
            return False
        if 'anyOf' in schema and not any(matches(s, value) for s in schema['anyOf']):
            return False
        if 'allOf' in schema and not all(matches(s, value) for s in schema['allOf']):
            return False
        if 'enum' in schema and value not in schema['enum']:
            return False
        types = schema.get('type', [])
        types = [types] if isinstance(types, str) else types
        actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
                  'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else
                  'string' if isinstance(value, str) else 'integer' if isinstance(value, int) else 'number')
        if types and actual not in types and not (actual == 'integer' and 'number' in types):
            return False
        if isinstance(value, dict):
            if any(k not in value for k in schema.get('required', [])):
                return False
            properties = schema.get('properties', {})
            for key, child in value.items():
                rule = properties.get(key, schema.get('additionalProperties', True))
                if not matches(rule, child):
                    return False
        if isinstance(value, list) and not all(matches(schema.get('items', True), child) for child in value):
            return False
        if isinstance(value, (int, float)) and 'minimum' in schema and value < schema['minimum']:
            return False
        return True

    if not matches(root, value):
        raise AssertionError('Invalid ' + method + ' response: ' + json.dumps(value))
