# =============================================================================
# schemas.py — Composable JSON Schema builders for core.llm_structured()
# =============================================================================
# Every LLM call in this codebase should describe its desired output as a
# JSON Schema and pass it to llm_structured() — these builders exist so call
# sites compose small typed pieces instead of hand-writing repetitive raw
# schema dicts (and risking inconsistencies like forgetting
# additionalProperties/required, which "strict" mode depends on).
#
# llm_generate_schema() is the deliberate exception: an opt-in escape hatch
# for the rare call site whose output shape isn't knowable ahead of time.
# Letting an LLM design the contract for another LLM call is exactly as
# failure-prone as it sounds, so the generated schema is validated against
# the real JSON Schema meta-schema before use, and any failure (unparseable
# JSON, invalid schema) falls back to a fully permissive {"type": "object"}
# rather than crashing the caller — worst case you're back to unconstrained
# output, never a crash. Prefer a hand-built schema below wherever the shape
# is knowable — this is a fallback, not a default.
# =============================================================================

import re
import json

import jsonschema


def s_string() -> dict:
    return {"type": "string"}


def s_int() -> dict:
    return {"type": "integer"}


def s_bool() -> dict:
    return {"type": "boolean"}


def s_enum(choices: list[str]) -> dict:
    """A string constrained to a fixed, known set of values."""
    return {"type": "string", "enum": list(choices)}


def s_array(item_schema: dict) -> dict:
    return {"type": "array", "items": item_schema}


def s_object(properties: dict, required: list[str] | None = None) -> dict:
    """
    Top-level (or nested) object schema. Defaults to requiring every declared
    property and forbidding extras — matches the "strict" mode every
    llm_structured() call uses, so call sites don't repeat this boilerplate
    per schema.
    """
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties.keys()),
        "additionalProperties": False,
    }


def llm_generate_schema(description: str, schema_name: str = "generated") -> dict:
    """
    Escape hatch: ask the LLM to design its own JSON Schema for a one-off
    call whose output shape genuinely isn't known ahead of time. Validated
    against the real JSON Schema meta-schema before being trusted — falls
    back to an unconstrained {"type": "object"} on any failure (bad JSON,
    invalid schema) so a broken meta-call degrades to "no structure
    enforced" rather than crashing the workflow that asked for it.
    """
    from core import llm_call, dbg  # deferred: core doesn't import schemas, but avoid any load-order assumption

    prompt = (
        f"Design a JSON Schema (draft 2020-12) describing this desired output:\n"
        f"{description}\n\n"
        f'Top-level type must be "object". Declare concrete "properties", a '
        f'"required" list covering every property, and "additionalProperties": false.\n\n'
        f"Reply with ONLY the JSON Schema, no explanation."
    )
    raw = llm_call(prompt)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        dbg("llm_generate_schema: no JSON found in response — falling back to permissive schema")
        return {"type": "object"}

    try:
        schema = json.loads(match.group())
        jsonschema.Draft202012Validator.check_schema(schema)
        return schema
    except Exception as e:
        dbg(f"llm_generate_schema: generated schema invalid ({e}) — falling back to permissive schema")
        return {"type": "object"}
