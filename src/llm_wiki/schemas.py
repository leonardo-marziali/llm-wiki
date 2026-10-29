"""The plan.v1, deep.v1 and maint.v1 contracts between a model call and its
validator: schemas and models.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from llm_wiki.pages import AdrStatus, Confidence, PageType, Status

# page fields shared by the plan.v1 and maint.v1 page items
_PAGE_TYPE: dict = {
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
}
_STATUS: dict = {
    "type": "string",
    "enum": ["as-is", "to-be", "deprecated", "superseded"],
}
_ADR_STATUS: dict = {
    "type": ["string", "null"],
    "enum": ["proposed", "accepted", "rejected", "superseded", None],
}

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
                    "type": _PAGE_TYPE,
                    "status": _STATUS,
                    "adr_status": _ADR_STATUS,
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


# deep.v1: the `wiki lint --deep` reply; hand-written like PLAN_SCHEMA.
DEEP_KINDS = ("contradiction", "stale-claim", "missing-page", "missing-link")
DEEP_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "pages", "message", "evidence"],
                "properties": {
                    "kind": {"type": "string", "enum": list(DEEP_KINDS)},
                    "pages": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}


class DeepFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["contradiction", "stale-claim", "missing-page", "missing-link"]
    pages: list[str]
    message: str
    evidence: str


class DeepReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[DeepFinding]


# maint.v1: the `wiki lint --fix` and `wiki consolidate` plan; hand-written like
# PLAN_SCHEMA (every property required, the nullable fields carry an explicit null).
MAINT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "pages", "resolved_questions", "open_questions"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "One line, for the log and the commit message.",
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
                    "superseded_by",
                    "sources",
                    "instructions",
                    "reason",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "update"]},
                    "type": _PAGE_TYPE,
                    "status": _STATUS,
                    "adr_status": _ADR_STATUS,
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "superseded_by": {"type": ["string", "null"]},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "description": "A raw path an input page already cites.",
                        },
                    },
                    "instructions": {
                        "type": "string",
                        "description": "The concrete change to make on this page.",
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "resolved_questions": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "An exact line of wiki/open-questions.md.",
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}


class MaintPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action: Literal["create", "update"]
    type: PageType
    status: Status
    adr_status: AdrStatus | None
    title: str
    summary: str
    superseded_by: str | None
    sources: list[str]  # the raw paths the page will cite
    instructions: str  # the concrete change
    reason: str


class MaintPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str  # one line, used in the log and the commit
    pages: list[MaintPage]
    resolved_questions: list[str]  # exact lines of open-questions.md
    open_questions: list[str]
