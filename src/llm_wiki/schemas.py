"""The plan.v1 contract between the plan call and the validator: schema and models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from llm_wiki.pages import AdrStatus, Confidence, PageType, Status

# Written by hand and kept byte-stable (the plan call's --json-schema); every
# property is required, and the nullable adr_status carries an explicit null.
PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "source_title",
        "source_summary",
        "pages",
        "contradictions",
        "open_questions",
    ],
    "properties": {
        "source_title": {"type": "string"},
        "source_summary": {
            "type": "string",
            "description": "One or two sentences.",
        },
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "action",
                    "type",
                    "status",
                    "adr_status",
                    "title",
                    "summary",
                    "claims",
                    "reason",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update"]},
                    "type": {
                        "type": "string",
                        "enum": [
                            "concept",
                            "component",
                            "system",
                            "integration",
                            "environment",
                            "process",
                            "convention",
                            "decision",
                            "requirement",
                            "howto",
                            "reference",
                            "glossary",
                            "source-summary",
                            "overview",
                            "hub",
                        ],
                    },
                    "status": {
                        "type": "string",
                        "enum": ["as-is", "to-be", "deprecated", "superseded"],
                    },
                    "adr_status": {
                        "type": ["string", "null"],
                        "enum": [
                            "proposed",
                            "accepted",
                            "rejected",
                            "superseded",
                            None,
                        ],
                    },
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "source", "locator", "confidence"],
                            "properties": {
                                "text": {"type": "string"},
                                "source": {
                                    "type": "string",
                                    "description": "A raw path, e.g. raw/docs/a.md.",
                                },
                                "locator": {"type": "string"},
                                "confidence": {
                                    "type": "string",
                                    "enum": ["stated", "high", "medium", "low"],
                                },
                            },
                        },
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["page_id", "existing", "new", "locator"],
                "properties": {
                    "page_id": {"type": "string"},
                    "existing": {"type": "string"},
                    "new": {"type": "string"},
                    "locator": {"type": "string"},
                },
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    source: str  # a raw path
    locator: str
    confidence: Confidence


class PagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action: Literal["create", "update"]
    type: PageType
    status: Status
    adr_status: AdrStatus | None
    title: str
    summary: str
    claims: list[Claim] = Field(min_length=1)
    reason: str


class Contradiction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str
    existing: str
    new: str
    locator: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_title: str
    source_summary: str  # one or two sentences
    pages: list[PagePlan]
    contradictions: list[Contradiction]
    open_questions: list[str]
