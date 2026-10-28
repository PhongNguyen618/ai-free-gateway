from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


def calculate_shannon_entropy(data: str) -> float:
    """Calculate Shannon entropy for a given string."""
    if not data:
        return 0.0
    entropy = 0.0
    length = len(data)
    counter = Counter(data)
    for count in counter.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def luhn_checksum_valid(card_number: str) -> bool:
    """Validate credit card number using Luhn algorithm."""
    digits = [int(c) for c in card_number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = (len(digits) - 2) % 2
    for i, digit in enumerate(digits[:-1]):
        if i % 2 == parity:
            doubled = digit * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += digit
    return (checksum + digits[-1]) % 10 == 0


@dataclass
class DLPFinding:
    rule_name: str
    entity_type: str
    risk_level: str  # LOW, MEDIUM, HIGH
    redacted_token: str
    masked_value: str
    matched_text: str


@dataclass
class SanitizationResult:
    text: str
    findings: List[DLPFinding] = field(default_factory=list)
    reverse_map: Dict[str, str] = field(default_factory=dict)
    max_risk_level: str = "NONE"


class DLPSanitizer:
    def __init__(
        self,
        entropy_threshold: float = 4.5,
        entropy_min_length: int = 20,
        enable_entropy: bool = True,
        custom_blocklist: Optional[List[str]] = None,
    ):
        self.entropy_threshold = entropy_threshold
        self.entropy_min_length = entropy_min_length
        self.enable_entropy = enable_entropy
        self.custom_blocklist = [w.strip() for w in (custom_blocklist or []) if w and w.strip()]

        # Precompiled regex patterns ordered by specificity and risk
        self.rules: List[Dict[str, Any]] = [
            {
                "name": "Private Key",
                "type": "PRIVATE_KEY",
                "risk": "HIGH",
                "pattern": re.compile(
                    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY[^-]*-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
                    re.MULTILINE,
                ),
            },
            {
                "name": "AWS Access Key",
                "type": "AWS_KEY",
                "risk": "HIGH",
                "pattern": re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
            },
            {
                "name": "OpenAI Secret Key",
                "type": "OPENAI_KEY",
                "risk": "HIGH",
                "pattern": re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})\b"),
            },
            {
                "name": "GitHub Token",
                "type": "GITHUB_TOKEN",
                "risk": "HIGH",
                "pattern": re.compile(
                    r"\b(gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"
                ),
            },
            {
                "name": "Google API Key",
                "type": "GOOGLE_KEY",
                "risk": "HIGH",
                "pattern": re.compile(r"\b(AIza[0-9A-Za-z-_]{35})\b"),
            },
            {
                "name": "Slack Token",
                "type": "SLACK_TOKEN",
                "risk": "HIGH",
                "pattern": re.compile(r"\b(xox[baprs]-(?:[0-9a-zA-Z]+-?){2,})\b"),
            },
            {
                "name": "JSON Web Token",
                "type": "JWT",
                "risk": "HIGH",
                "pattern": re.compile(
                    r"\b(ey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
                ),
            },
            {
                "name": "Password in URL",
                "type": "PASSWORD_URL",
                "risk": "HIGH",
                "pattern": re.compile(
                    r"(?i)(?:https?|mysql|postgresql|postgres|mongodb|redis)://[^:\s]+:([^@\s]+)@"
                ),
                "extract_group": 1,
            },
            {
                "name": "Credit Card Number",
                "type": "CREDIT_CARD",
                "risk": "HIGH",
                "pattern": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
                "validator": luhn_checksum_valid,
            },
            {
                "name": "Internal IPv4 Address",
                "type": "INTERNAL_IP",
                "risk": "MEDIUM",
                "pattern": re.compile(
                    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"
                ),
            },
            {
                "name": "Vietnam Citizen Identity (CCCD)",
                "type": "CCCD",
                "risk": "MEDIUM",
                "pattern": re.compile(r"\b(0\d{11})\b"),
            },
            {
                "name": "Email Address",
                "type": "EMAIL",
                "risk": "MEDIUM",
                "pattern": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            },
            {
                "name": "Phone Number",
                "type": "PHONE",
                "risk": "MEDIUM",
                "pattern": re.compile(
                    r"\b(?:\+84|0)(?:3[2-9]|5[689]|7[06-9]|8[1-9]|9[0-9])\d{7}\b"
                ),
            },
        ]

        # Dynamically append custom blocklist rules
        for kw in self.custom_blocklist:
            escaped_kw = re.escape(kw)
            self.rules.append({
                "name": f"Custom Blocklist ({kw})",
                "type": "CUSTOM_BLOCK",
                "risk": "MEDIUM",
                "pattern": re.compile(rf"\b{escaped_kw}\b", re.IGNORECASE),
            })

        # Regex to scan for high-entropy standalone tokens (hex/base64/keys)
        self.entropy_candidate_pattern = re.compile(
            r"\b[A-Za-z0-9_\-+/=]{" + str(self.entropy_min_length) + r",}\b"
        )

    def _mask_value(self, val: str) -> str:
        """Create a safe preview for logging without leaking the whole secret."""
        if len(val) <= 6:
            return "***"
        return f"{val[:3]}...{val[-3:]}"

    def _rank_risk(self, r1: str, r2: str) -> str:
        ranks = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        return r1 if ranks.get(r1, 0) >= ranks.get(r2, 0) else r2

    def sanitize_text(
        self,
        text: str,
        forward_map: Optional[Dict[str, str]] = None,
        reverse_map: Optional[Dict[str, str]] = None,
        entity_counter: Optional[Dict[str, int]] = None,
    ) -> SanitizationResult:
        """
        Scan and pseudonymize sensitive data in text.
        Replaces by exact match spans to avoid accidental substring collision.
        """
        if not text:
            return SanitizationResult(text=text)

        if forward_map is None:
            forward_map = {}
        if reverse_map is None:
            reverse_map = {}
        if entity_counter is None:
            entity_counter = {}

        raw_spans: List[Dict[str, Any]] = []

        # 1. Apply Rule-based Regex Matching
        for rule in self.rules:
            pattern: re.Pattern = rule["pattern"]
            for match in pattern.finditer(text):
                group_idx = rule.get("extract_group", 0)
                matched_str = match.group(group_idx)
                start, end = match.span(group_idx)

                validator = rule.get("validator")
                if validator and not validator(matched_str):
                    continue

                raw_spans.append({
                    "start": start,
                    "end": end,
                    "matched_text": matched_str,
                    "rule_name": rule["name"],
                    "entity_type": rule["type"],
                    "risk_level": rule["risk"],
                    "priority": 2 if rule["risk"] == "HIGH" else 1,
                })

        # 2. Apply Shannon Entropy Scanning if enabled
        if self.enable_entropy:
            for match in self.entropy_candidate_pattern.finditer(text):
                candidate = match.group(0)
                start, end = match.span(0)

                # Skip tokens that do NOT contain at least one digit (eliminates long code identifiers)
                if not any(c.isdigit() for c in candidate):
                    continue

                # Skip snake_case identifiers (e.g. user_authentication_service_provider)
                if candidate.count("_") >= 2:
                    continue

                # Check if this span is already covered by a regex rule
                if any(s["start"] <= start and s["end"] >= end for s in raw_spans):
                    continue

                entropy = calculate_shannon_entropy(candidate)
                if entropy >= self.entropy_threshold:
                    raw_spans.append({
                        "start": start,
                        "end": end,
                        "matched_text": candidate,
                        "rule_name": f"High Shannon Entropy ({entropy:.2f})",
                        "entity_type": "HIGH_ENTROPY",
                        "risk_level": "HIGH",
                        "priority": 3,
                    })

        # 3. Resolve overlapping spans: sort by start ASC, then span length DESC, priority DESC
        raw_spans.sort(key=lambda s: (s["start"], -(s["end"] - s["start"]), -s["priority"]))

        resolved_spans: List[Dict[str, Any]] = []
        last_end = -1
        for s in raw_spans:
            if s["start"] >= last_end:
                resolved_spans.append(s)
                last_end = s["end"]

        # 4. Generate tokens and findings
        findings: List[DLPFinding] = []
        max_risk = "NONE"

        for s in resolved_spans:
            matched_str = s["matched_text"]
            rule_type = s["entity_type"]
            risk_level = s["risk_level"]
            max_risk = self._rank_risk(max_risk, risk_level)

            if matched_str in forward_map:
                token = forward_map[matched_str]
            else:
                entity_counter[rule_type] = entity_counter.get(rule_type, 0) + 1
                token = f"<REDACTED_{rule_type}_{entity_counter[rule_type]}>"
                forward_map[matched_str] = token
                reverse_map[token] = matched_str

            s["token"] = token
            finding = DLPFinding(
                rule_name=s["rule_name"],
                entity_type=rule_type,
                risk_level=risk_level,
                redacted_token=token,
                masked_value=self._mask_value(matched_str),
                matched_text=matched_str,
            )
            findings.append(finding)

        # 5. Replace by exact spans from right-to-left (end of string to start)
        # This guarantees span indexes remain invariant during replacement!
        sanitized_chars = list(text)
        for s in sorted(resolved_spans, key=lambda x: x["start"], reverse=True):
            start = s["start"]
            end = s["end"]
            token = s["token"]
            sanitized_chars[start:end] = list(token)

        sanitized_text = "".join(sanitized_chars)

        return SanitizationResult(
            text=sanitized_text,
            findings=findings,
            reverse_map=reverse_map,
            max_risk_level=max_risk,
        )

    def sanitize_messages(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[DLPFinding], Dict[str, str], str]:
        """
        Sanitize an OpenAI-compatible messages array.
        Recursively handles string content or multi-part content.
        """
        forward_map: Dict[str, str] = {}
        reverse_map: Dict[str, str] = {}
        entity_counter: Dict[str, int] = {}
        all_findings: List[DLPFinding] = []
        overall_max_risk = "NONE"

        sanitized_messages: List[Dict[str, Any]] = []

        for msg in messages:
            msg_copy = dict(msg)
            content = msg_copy.get("content")

            if isinstance(content, str):
                res = self.sanitize_text(content, forward_map, reverse_map, entity_counter)
                msg_copy["content"] = res.text
                all_findings.extend(res.findings)
                overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)

            elif isinstance(content, list):
                sanitized_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part_copy = dict(part)
                        res = self.sanitize_text(
                            part_copy.get("text", ""), forward_map, reverse_map, entity_counter
                        )
                        part_copy["text"] = res.text
                        all_findings.extend(res.findings)
                        overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)
                        sanitized_parts.append(part_copy)
                    else:
                        sanitized_parts.append(part)
                msg_copy["content"] = sanitized_parts

            sanitized_messages.append(msg_copy)

        return sanitized_messages, all_findings, reverse_map, overall_max_risk

    @staticmethod
    def restore_text(text: str, reverse_map: Dict[str, str]) -> str:
        """
        Restore original values by replacing pseudonymized tokens back.
        """
        if not text or not reverse_map:
            return text
        restored = text
        for token, original in reverse_map.items():
            restored = restored.replace(token, original)
        return restored


class StreamingRestorer:
    """
    Stateful helper for streaming Server-Sent Events (SSE).
    Buffers potential partial placeholder tokens across chunk boundaries so
    <REDACTED_KEY_1> is properly restored even if split across chunks.
    """

    def __init__(self, reverse_map: Dict[str, str]):
        self.reverse_map = reverse_map
        self.buffer = ""
        self.token_prefix = "<REDACTED_"

    def feed(self, chunk: str) -> str:
        if not self.reverse_map or not chunk:
            return chunk

        self.buffer += chunk
        output = []

        while self.buffer:
            prefix_pos = self.buffer.find(self.token_prefix)

            # Case 1: No prefix found in buffer
            if prefix_pos == -1:
                tail_len = min(len(self.buffer), len(self.token_prefix) - 1)
                tail_match = False
                for k in range(tail_len, 0, -1):
                    if self.token_prefix.startswith(self.buffer[-k:]):
                        output.append(self.buffer[:-k])
                        self.buffer = self.buffer[-k:]
                        tail_match = True
                        break
                if not tail_match:
                    output.append(self.buffer)
                    self.buffer = ""
                break

            # Case 2: Prefix found, output everything before it
            if prefix_pos > 0:
                output.append(self.buffer[:prefix_pos])
                self.buffer = self.buffer[prefix_pos:]

            # Now self.buffer starts with <REDACTED_
            end_pos = self.buffer.find(">")
            if end_pos != -1:
                token = self.buffer[: end_pos + 1]
                self.buffer = self.buffer[end_pos + 1 :]
                # Replace with original if present
                output.append(self.reverse_map.get(token, token))
            else:
                # Incomplete token, wait for next feed
                if len(self.buffer) > 100:
                    output.append(self.buffer)
                    self.buffer = ""
                break

        return "".join(output)

    def flush(self) -> str:
        """Flush remaining buffered text."""
        remaining = self.buffer
        self.buffer = ""
        if not self.reverse_map or not remaining:
            return remaining
        return DLPSanitizer.restore_text(remaining, self.reverse_map)
