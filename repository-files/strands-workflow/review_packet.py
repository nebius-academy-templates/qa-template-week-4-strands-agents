"""Bounded source and execution evidence passed to the read-only reviewer."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

SCHEMA = "ai-for-qa/exact-api-review-packet"
VERSION = 2
MAX_PACKET_BYTES = 128 * 1024
MAX_CASE_BYTES = 32 * 1024
MAX_SOURCE_BYTES = 64 * 1024
MAX_HTTP_ATTACHMENT_BYTES = 16 * 1024
MAX_HTTP_BYTES = 64 * 1024
TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/x-www-form-urlencoded",
}


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self.suppressed += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        elif tag.lower() in {"p", "div", "li", "tr", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line.strip()).strip()


def _line_numbered(text: str) -> str:
    return "\n".join(f"{number}: {line}" for number, line in enumerate(text.splitlines(), 1))


def _redact(text: str) -> str:
    inline_header = re.compile(
        r"(?i)((?:-H|--header)\s+)(?P<quote>[\"'])(?P<name>authorization|cookie|"
        r"set-cookie|x-api-key|api[ _-]?key|x-sandbox-session)(?P<separator>\s*:\s*)"
        r"[^\"']*(?P=quote)"
    )
    text = inline_header.sub(
        lambda match: (
            f"{match[1]}{match['quote']}{match['name']}"
            f"{match['separator']}[REDACTED]{match['quote']}"
        ),
        text,
    )
    header = re.compile(
        r"(?im)^(\s*(?:authorization|cookie|set-cookie|x-api-key|api[ _-]?key|"
        r"x-sandbox-session)\s*:\s*).*$"
    )
    text = header.sub(r"\1[REDACTED]", text)
    json_secret = re.compile(
        r"(?i)([\"'](?:authorization|cookie|set-cookie|x-api-key|api[ _-]?key|"
        r"x-sandbox-session|[\w-]*token[\w-]*)[\"']\s*:\s*)"
        r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^,}\]\s]+)"
    )
    text = json_secret.sub(r'\1"[REDACTED]"', text)
    form_secret = re.compile(
        r"(?i)(?<![\w-])((?:access|refresh|id)[_-]?token|token|api[_-]?key|apikey)"
        r"(\s*=\s*)[^&\s\"'<>]+"
    )
    return form_secret.sub(r"\1\2[REDACTED]", text)


def _mime_type(metadata: dict, path: Path) -> str:
    supplied = metadata.get("type", "")
    if supplied and not isinstance(supplied, str):
        raise ValueError("HTTP attachment MIME type must be text")
    mime = supplied.split(";", 1)[0].strip().lower() if supplied else ""
    if not mime:
        mime = {
            ".html": "text/html",
            ".htm": "text/html",
            ".txt": "text/plain",
            ".log": "text/plain",
            ".json": "application/json",
            ".xml": "application/xml",
        }.get(path.suffix.lower(), "")
    if not (mime.startswith("text/") or mime in TEXT_MIME_TYPES):
        raise ValueError("Required HTTP request/response attachment is not text")
    return mime


def _artifact(root: Path, path: Path, mime_type: str, raw: bytes, content: str, **extra) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": content,
        **extra,
    }


def _source(repository, path: Path, mime_type: str) -> dict:
    path = repository.ensure_safe(path)
    if not path.is_file() or not repository.is_readable(path):
        raise ValueError("Required review source is unavailable")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Review packet source exceeds its size limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Required review source is not UTF-8 text") from error
    return _artifact(repository.root, path, mime_type, raw, _line_numbered(text))


def _normalized_http_name(kind: str, metadata: dict) -> str:
    if kind == "request":
        return "HTTP request"
    raw_name = metadata.get("name", "")
    match = re.match(r"(?i)^http/\d+(?:\.\d+)?\s+([1-5]\d{2})(?:\s|$)", str(raw_name))
    return f"HTTP response {match[1]}" if match else "HTTP response"


def _safe_allure_summary(report: dict, attachment_indices: dict[int, tuple[int, str]]) -> dict:
    """Keep review-relevant Allure structure without arbitrary result fields."""

    def status(value) -> str:
        return value if value in {"passed", "failed", "broken", "skipped", "unknown"} else "unknown"

    def attachments(node: dict) -> list[dict]:
        result = []
        for metadata in node.get("attachments", []):
            reference = attachment_indices.get(id(metadata))
            if reference is None:
                continue
            index, kind = reference
            result.append(
                {"http_index": index, "kind": kind, "name": _normalized_http_name(kind, metadata)}
            )
        return result

    def steps(node: dict) -> list[dict]:
        result = []
        for ordinal, step in enumerate(node.get("steps", []), 1):
            item = {
                "ordinal": ordinal,
                "status": status(step.get("status")),
                "http_attachments": attachments(step),
                "steps": steps(step),
            }
            result.append(item)
        return result

    allure_ids = []
    for label in report.get("labels", []):
        if not isinstance(label, dict) or label.get("name") != "AS_ID":
            continue
        value = label.get("value")
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            allure_ids.append(value)
    return {
        "full_name": str(report.get("fullName", "")),
        "status": status(report.get("status")),
        "allure_ids": allure_ids,
        "http_attachments": attachments(report),
        "steps": steps(report),
    }


def encode_review_packet(packet: dict) -> bytes:
    encoded = json.dumps(packet, indent=2, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError("Complete review packet exceeds its total size limit")
    return encoded


def evidence_manifest_from_packet(packet: dict) -> dict:
    """Return only raw-artifact identities represented by a review packet."""

    def identity(artifact: dict, scope: str) -> dict:
        return {
            "path": artifact["path"],
            "scope": scope,
            "size_bytes": artifact["size_bytes"],
            "sha256": artifact["sha256"],
        }

    evidence = packet["evidence"]
    return {
        "version": 1,
        "target": packet["target"]["name"],
        "junit": identity(evidence["junit"], "matching-testcase"),
        "allure": identity(evidence["allure"], "full-result"),
        "http": [
            {
                **identity(attachment, "full-attachment"),
                "index": attachment["index"],
                "kind": attachment["kind"],
            }
            for attachment in evidence["http"]
        ],
    }


def build_review_packet(
    repository,
    case: str,
    target: str,
    evidence: dict,
    target_record: dict,
    artifacts: dict,
    junit_status: str,
) -> dict:
    """Serialize the already-selected exact test and current run artifacts."""
    if len(case.encode("utf-8")) > MAX_CASE_BYTES:
        raise ValueError("Selected case exceeds the review packet limit")

    http = []
    http_bytes = 0
    attachment_indices: dict[int, tuple[int, str]] = {}
    for attachment in artifacts["attachments"]:
        if not attachment["kind"]:
            continue
        if attachment["error"] or attachment["path"] is None:
            raise ValueError("Required HTTP attachment is missing or unsafe")
        path = attachment["path"]
        mime_type = _mime_type(attachment["metadata"], path)
        raw = path.read_bytes()
        if len(raw) > MAX_HTTP_ATTACHMENT_BYTES:
            raise ValueError("HTTP attachment exceeds the review packet limit")
        http_bytes += len(raw)
        if http_bytes > MAX_HTTP_BYTES:
            raise ValueError("HTTP evidence exceeds the review packet limit")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Required HTTP attachment is not UTF-8 text") from error
        if mime_type in {"text/html", "application/xhtml+xml"}:
            parser = _HTMLText()
            parser.feed(text)
            parser.close()
            text = parser.text()
        index = len(http)
        attachment_indices[id(attachment["metadata"])] = (index, attachment["kind"])
        http.append(
            _artifact(
                repository.root,
                path,
                mime_type,
                raw,
                _redact(text),
                index=index,
                name=_normalized_http_name(attachment["kind"], attachment["metadata"]),
                kind=attachment["kind"],
                scope="full-attachment",
            )
        )
    if {item["kind"] for item in http} != {"request", "response"}:
        raise ValueError("Review requires ordered HTTP request and response attachment text")

    test = _source(repository, repository.path_for(target_record["path"]), "text/x-kotlin")
    plan_path = repository.root / f"agent_docs/automation-plans/{repository.case_id}.md"
    plan = _source(repository, plan_path, "text/markdown") if plan_path.is_file() else None

    junit = artifacts["selected_junit"][0]
    junit_raw = ET.tostring(junit["case"], encoding="utf-8")
    junit_summary = {"target": target, "status": junit_status}
    allure = artifacts["selected_allure"][0]
    allure_raw = allure["raw"]
    allure_summary = _safe_allure_summary(allure["report"], attachment_indices)
    evidence_summary = {
        key: deepcopy(evidence[key])
        for key in (
            "status",
            "target_status",
            "target",
            "case_id",
            "command",
            "exit_code",
            "counts",
            "reasons",
            "source_digest",
            "report",
        )
        if key in evidence
    }
    packet = {
        "schema": SCHEMA,
        "version": VERSION,
        "case": {"case_id": repository.case_id, "text": case},
        "target": {"name": target, "source_digest": evidence["source_digest"]},
        "sources": {"test": test, "plan": plan},
        "evidence": {
            "status": "VERIFIED",
            "summary": evidence_summary,
            "junit": _artifact(
                repository.root,
                junit["path"],
                "application/xml",
                junit_raw,
                json.dumps(junit_summary, indent=2, ensure_ascii=False, sort_keys=True),
                scope="matching-testcase",
                content_scope="whitelisted-summary",
                content_mime_type="application/json",
            ),
            "allure": _artifact(
                repository.root,
                allure["path"],
                "application/json",
                allure_raw,
                json.dumps(allure_summary, indent=2, ensure_ascii=False, sort_keys=True),
                scope="full-result",
                content_scope="whitelisted-summary",
            ),
            "http": http,
        },
    }
    return packet
