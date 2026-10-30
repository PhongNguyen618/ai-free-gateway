from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


class UnsupportedModalityError(ValueError):
    """Raised when an unsupported content part or binary modality is submitted under Zero-Trust DLP policy."""
    pass


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
    outbound_text: str
    audit_text: str
    findings: List[DLPFinding] = field(default_factory=list)
    reverse_map: Dict[str, str] = field(default_factory=dict)
    max_risk_level: str = "NONE"

    @property
    def text(self) -> str:
        """Backward compatibility alias for outbound_text."""
        return self.outbound_text


class MessagesSanitizationResult(tuple):
    """
    Dual-projection result object that behaves as a 5-tuple for backward compatibility
    (outbound_messages, outbound_tools, all_findings, reverse_map, overall_max_risk)
    while exposing explicit properties for full audit and outbound isolation.
    """
    outbound_messages: List[Dict[str, Any]]
    audit_messages: List[Dict[str, Any]]
    outbound_tools: Optional[List[Dict[str, Any]]]
    audit_tools: Optional[List[Dict[str, Any]]]
    all_findings: List[DLPFinding]
    reverse_map: Dict[str, str]
    overall_max_risk: str
    audit_prompt_snippet: str

    def __new__(
        cls,
        outbound_messages: List[Dict[str, Any]],
        audit_messages: List[Dict[str, Any]],
        outbound_tools: Optional[List[Dict[str, Any]]],
        audit_tools: Optional[List[Dict[str, Any]]],
        all_findings: List[DLPFinding],
        reverse_map: Dict[str, str],
        overall_max_risk: str,
        audit_prompt_snippet: str,
    ):
        instance = super().__new__(
            cls,
            (outbound_messages, outbound_tools, all_findings, reverse_map, overall_max_risk),
        )
        instance.outbound_messages = outbound_messages
        instance.audit_messages = audit_messages
        instance.outbound_tools = outbound_tools
        instance.audit_tools = audit_tools
        instance.all_findings = all_findings
        instance.findings = all_findings
        instance.reverse_map = reverse_map
        instance.overall_max_risk = overall_max_risk
        instance.max_risk = overall_max_risk
        instance.audit_prompt_snippet = audit_prompt_snippet
        return instance


class DLPSanitizer:
    def __init__(
        self,
        entropy_threshold: float = 4.5,
        entropy_min_length: int = 20,
        enable_entropy: bool = True,
        custom_blocklist: Optional[List[str]] = None,
        token_format: str = "<REDACTED_{TYPE}_{INDEX}>",
    ):
        self.entropy_threshold = entropy_threshold
        self.entropy_min_length = entropy_min_length
        self.enable_entropy = enable_entropy
        self.custom_blocklist = [w.strip() for w in (custom_blocklist or []) if w and w.strip()]
        self.token_format = token_format

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

        # Regex to scan for high-entropy standalone tokens
        self.entropy_candidate_pattern = re.compile(
            r"\b[A-Za-z0-9_\-+/=]{" + str(self.entropy_min_length) + r",}\b"
        )

    def _mask_value(self, val: str) -> str:
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
        risk_actions: Optional[Dict[str, str]] = None,
    ) -> SanitizationResult:
        """
        Scan and generate two projections:
        - outbound_text: honors policy allow/redact/approve
        - audit_text: ALWAYS redacts 100% findings regardless of allow policy
        """
        if not text:
            return SanitizationResult(outbound_text=text, audit_text=text)

        if forward_map is None:
            forward_map = {}
        if reverse_map is None:
            reverse_map = {}
        if entity_counter is None:
            entity_counter = {}

        actions = risk_actions or {"HIGH": "reject", "MEDIUM": "approve", "LOW": "redact"}

        raw_spans: List[Dict[str, Any]] = []

        # 1. Rule-based Regex Matching
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

        # 2. Shannon Entropy Matching
        if self.enable_entropy:
            for match in self.entropy_candidate_pattern.finditer(text):
                candidate = match.group(0)
                start, end = match.span(0)

                if not any(c.isdigit() for c in candidate):
                    continue
                if candidate.count("_") >= 2:
                    continue
                # Skip common file paths or URLs from false entropy triggers
                if any(candidate.lower().endswith(ext) for ext in (".py", ".json", ".yaml", ".yml", ".html", ".css", ".js", ".ts", ".md", ".txt", ".bat", ".sh", ".lock")):
                    continue
                if candidate.startswith(("/", "./", "../", "http://", "https://")):
                    continue
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

        # 3. Resolve overlapping spans
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
                try:
                    token = self.token_format.format(TYPE=rule_type, INDEX=entity_counter[rule_type])
                except Exception:
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

        # 5. Build outbound projection (respecting 'allow')
        outbound_chars = list(text)
        for s in sorted(resolved_spans, key=lambda x: x["start"], reverse=True):
            r_action = actions.get(s["risk_level"], "redact")
            if r_action == "allow":
                continue
            start = s["start"]
            end = s["end"]
            token = s["token"]
            outbound_chars[start:end] = list(token)
        outbound_text = "".join(outbound_chars)

        # 6. Build audit projection (ALWAYS redacting 100% findings!)
        audit_chars = list(text)
        for s in sorted(resolved_spans, key=lambda x: x["start"], reverse=True):
            start = s["start"]
            end = s["end"]
            token = s["token"]
            audit_chars[start:end] = list(token)
        audit_text = "".join(audit_chars)

        return SanitizationResult(
            outbound_text=outbound_text,
            audit_text=audit_text,
            findings=findings,
            reverse_map=reverse_map,
            max_risk_level=max_risk,
        )

    def _sanitize_json_arguments(
        self,
        arguments_str: str,
        forward_map: Dict[str, str],
        reverse_map: Dict[str, str],
        entity_counter: Dict[str, int],
        risk_actions: Optional[Dict[str, str]],
        findings_accum: List[DLPFinding],
    ) -> Tuple[str, str]:
        """
        Sanitize arguments string preserving JSON structure and escaping.
        Returns (outbound_json_str, audit_json_str).
        """
        if not arguments_str:
            return arguments_str, arguments_str

        try:
            parsed = json.loads(arguments_str)
        except Exception:
            res = self.sanitize_text(arguments_str, forward_map, reverse_map, entity_counter, risk_actions)
            findings_accum.extend(res.findings)
            return res.outbound_text, res.audit_text

        def sanitize_val(val: Any) -> Tuple[Any, Any]:
            if isinstance(val, str):
                res = self.sanitize_text(val, forward_map, reverse_map, entity_counter, risk_actions)
                findings_accum.extend(res.findings)
                return res.outbound_text, res.audit_text
            elif isinstance(val, dict):
                out_d, aud_d = {}, {}
                for k, v in val.items():
                    ov, av = sanitize_val(v)
                    out_d[k] = ov
                    aud_d[k] = av
                return out_d, aud_d
            elif isinstance(val, list):
                out_l, aud_l = [], []
                for item in val:
                    ov, av = sanitize_val(item)
                    out_l.append(ov)
                    aud_l.append(av)
                return out_l, aud_l
            return val, val

        out_parsed, aud_parsed = sanitize_val(parsed)
        return json.dumps(out_parsed), json.dumps(aud_parsed)

    def _sanitize_schema_dict(
        self,
        schema: Any,
        forward_map: Dict[str, str],
        reverse_map: Dict[str, str],
        entity_counter: Dict[str, int],
        risk_actions: Optional[Dict[str, str]],
        findings_accum: List[DLPFinding],
    ) -> Tuple[Any, Any]:
        """Recursively scan schema properties. Returns (outbound_schema, audit_schema)."""
        if isinstance(schema, str):
            res = self.sanitize_text(schema, forward_map, reverse_map, entity_counter, risk_actions)
            findings_accum.extend(res.findings)
            return res.outbound_text, res.audit_text
        elif isinstance(schema, dict):
            out_d, aud_d = {}, {}
            for k, v in schema.items():
                if k in ("description", "title", "default", "example", "examples") or isinstance(v, (str, dict, list)):
                    ov, av = self._sanitize_schema_dict(v, forward_map, reverse_map, entity_counter, risk_actions, findings_accum)
                    out_d[k] = ov
                    aud_d[k] = av
                else:
                    out_d[k] = v
                    aud_d[k] = v
            return out_d, aud_d
        elif isinstance(schema, list):
            out_l, aud_l = [], []
            for item in schema:
                ov, av = self._sanitize_schema_dict(item, forward_map, reverse_map, entity_counter, risk_actions, findings_accum)
                out_l.append(ov)
                aud_l.append(av)
            return out_l, aud_l
        return schema, schema

    def sanitize_messages(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        risk_actions: Optional[Dict[str, str]] = None,
    ) -> Tuple[
        List[Dict[str, Any]],  # outbound_messages
        List[Dict[str, Any]],  # audit_messages (100% redacted)
        Optional[List[Dict[str, Any]]],  # outbound_tools
        Optional[List[Dict[str, Any]]],  # audit_tools
        List[DLPFinding],
        Dict[str, str],
        str,  # overall_max_risk
        str,  # audit_prompt_snippet
    ]:
        forward_map: Dict[str, str] = {}
        reverse_map: Dict[str, str] = {}
        entity_counter: Dict[str, int] = {}
        all_findings: List[DLPFinding] = []
        overall_max_risk = "NONE"

        outbound_messages: List[Dict[str, Any]] = []
        audit_messages: List[Dict[str, Any]] = []

        audit_prompt_snippet = ""

        for msg in messages:
            msg_out = dict(msg)
            msg_aud = dict(msg)
            content = msg.get("content")

            # 1. Content scanning
            if isinstance(content, str):
                res = self.sanitize_text(content, forward_map, reverse_map, entity_counter, risk_actions)
                msg_out["content"] = res.outbound_text
                msg_aud["content"] = res.audit_text
                all_findings.extend(res.findings)
                overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)
                if not audit_prompt_snippet and res.audit_text:
                    audit_prompt_snippet = res.audit_text[:150]

            elif isinstance(content, list):
                out_parts = []
                aud_parts = []
                for part in content:
                    if isinstance(part, dict):
                        part_type = part.get("type")
                        if part_type != "text":
                            raise UnsupportedModalityError(
                                f"Unsupported modality '{part_type}' in message content. "
                                "Zero-Trust DLP only supports text modalities."
                            )
                        # Ensure no extra unexpected fields in content part
                        extra_keys = set(part.keys()) - {"type", "text"}
                        if extra_keys:
                            raise UnsupportedModalityError(
                                f"Unsupported extra fields {extra_keys} in text content part."
                            )
                        p_text = part.get("text")
                        if not isinstance(p_text, str):
                            raise TypeError("Field 'text' in content part must be a string.")

                        res = self.sanitize_text(p_text, forward_map, reverse_map, entity_counter, risk_actions)
                        all_findings.extend(res.findings)
                        overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)
                        out_parts.append({"type": "text", "text": res.outbound_text})
                        aud_parts.append({"type": "text", "text": res.audit_text})
                        if not audit_prompt_snippet and res.audit_text:
                            audit_prompt_snippet = res.audit_text[:150]
                    else:
                        raise TypeError(f"Invalid content part type: {type(part).__name__}")
                msg_out["content"] = out_parts
                msg_aud["content"] = aud_parts

            elif content is not None:
                raise TypeError(f"Invalid content type: {type(content).__name__}")

            # 1b. Reasoning content scanning (e.g. DeepSeek R1 / Cline reasoning extension)
            if "reasoning_content" in msg and isinstance(msg["reasoning_content"], str):
                res = self.sanitize_text(msg["reasoning_content"], forward_map, reverse_map, entity_counter, risk_actions)
                msg_out["reasoning_content"] = res.outbound_text
                msg_aud["reasoning_content"] = res.audit_text
                all_findings.extend(res.findings)
                overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)

            # 2. Name field: protocol identifier, do NOT pseudonymize, only scan for violation
            if "name" in msg and isinstance(msg["name"], str):
                res = self.sanitize_text(msg["name"], forward_map, reverse_map, entity_counter, risk_actions)
                if res.findings:
                    # Name containing secrets is a protocol boundary risk -> reject
                    all_findings.extend(res.findings)
                    overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)
                # Keep original protocol name intact
                msg_out["name"] = msg["name"]
                msg_aud["name"] = res.audit_text

            # 3. Tool Calls
            if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                out_tcs = []
                aud_tcs = []
                for tc in msg["tool_calls"]:
                    if not isinstance(tc, dict):
                        out_tcs.append(tc)
                        aud_tcs.append(tc)
                        continue
                    tc_out = dict(tc)
                    tc_aud = dict(tc)
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        fn_out = dict(fn)
                        fn_aud = dict(fn)
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            fn_findings: List[DLPFinding] = []
                            out_args, aud_args = self._sanitize_json_arguments(
                                args, forward_map, reverse_map, entity_counter, risk_actions, fn_findings
                            )
                            all_findings.extend(fn_findings)
                            for f in fn_findings:
                                overall_max_risk = self._rank_risk(overall_max_risk, f.risk_level)
                            fn_out["arguments"] = out_args
                            fn_aud["arguments"] = aud_args
                        tc_out["function"] = fn_out
                        tc_aud["function"] = fn_aud
                    out_tcs.append(tc_out)
                    aud_tcs.append(tc_aud)
                msg_out["tool_calls"] = out_tcs
                msg_aud["tool_calls"] = aud_tcs

            # 3b. Legacy function_call scanning
            if "function_call" in msg and isinstance(msg["function_call"], dict):
                fn_call = msg["function_call"]
                fn_call_out = dict(fn_call)
                fn_call_aud = dict(fn_call)
                args = fn_call.get("arguments")
                if isinstance(args, str):
                    fn_findings: List[DLPFinding] = []
                    out_args, aud_args = self._sanitize_json_arguments(
                        args, forward_map, reverse_map, entity_counter, risk_actions, fn_findings
                    )
                    all_findings.extend(fn_findings)
                    for f in fn_findings:
                        overall_max_risk = self._rank_risk(overall_max_risk, f.risk_level)
                    fn_call_out["arguments"] = out_args
                    fn_call_aud["arguments"] = aud_args
                msg_out["function_call"] = fn_call_out
                msg_aud["function_call"] = fn_call_aud

            # 4. Tool Call ID (for role='tool')
            if "tool_call_id" in msg:
                msg_out["tool_call_id"] = msg["tool_call_id"]
                msg_aud["tool_call_id"] = msg["tool_call_id"]

            outbound_messages.append(msg_out)
            audit_messages.append(msg_aud)

        # 5. Tools Definitions
        outbound_tools = None
        audit_tools = None
        if tools is not None:
            outbound_tools = []
            audit_tools = []
            for t in tools:
                if not isinstance(t, dict):
                    outbound_tools.append(t)
                    audit_tools.append(t)
                    continue
                t_out = dict(t)
                t_aud = dict(t)
                fn = t.get("function")
                if isinstance(fn, dict):
                    fn_out = dict(fn)
                    fn_aud = dict(fn)
                    if "description" in fn and isinstance(fn["description"], str):
                        res = self.sanitize_text(fn["description"], forward_map, reverse_map, entity_counter, risk_actions)
                        fn_out["description"] = res.outbound_text
                        fn_aud["description"] = res.audit_text
                        all_findings.extend(res.findings)
                        overall_max_risk = self._rank_risk(overall_max_risk, res.max_risk_level)
                    if "parameters" in fn and isinstance(fn["parameters"], dict):
                        schema_findings: List[DLPFinding] = []
                        out_p, aud_p = self._sanitize_schema_dict(
                            fn["parameters"], forward_map, reverse_map, entity_counter, risk_actions, schema_findings
                        )
                        all_findings.extend(schema_findings)
                        for f in schema_findings:
                            overall_max_risk = self._rank_risk(overall_max_risk, f.risk_level)
                        fn_out["parameters"] = out_p
                        fn_aud["parameters"] = aud_p
                    t_out["function"] = fn_out
                    t_aud["function"] = fn_aud
                outbound_tools.append(t_out)
                audit_tools.append(t_aud)

        return MessagesSanitizationResult(
            outbound_messages=outbound_messages,
            audit_messages=audit_messages,
            outbound_tools=outbound_tools,
            audit_tools=audit_tools,
            all_findings=all_findings,
            reverse_map=reverse_map,
            overall_max_risk=overall_max_risk,
            audit_prompt_snippet=audit_prompt_snippet,
        )

    @staticmethod
    def restore_text(text: str, reverse_map: Dict[str, str]) -> str:
        """Restore original values by replacing pseudonymized tokens back."""
        if not text or not reverse_map:
            return text
        restored = text
        for token, original in reverse_map.items():
            restored = restored.replace(token, original)
        return restored

    @classmethod
    def restore_json_structure(cls, data: Any, reverse_map: Dict[str, str]) -> Any:
        """Recursively restore tokens in structured choices/tool calls."""
        if not reverse_map or data is None:
            return data
        if isinstance(data, str):
            return cls.restore_text(data, reverse_map)
        elif isinstance(data, dict):
            return {k: cls.restore_json_structure(v, reverse_map) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls.restore_json_structure(item, reverse_map) for item in data]
        return data


class StreamingRestorer:
    """
    Per-choice / per-channel stateful restorer for streaming Server-Sent Events (SSE).
    Buffers partial placeholder tokens across TCP chunk boundaries.
    """

    def __init__(self, reverse_map: Dict[str, str], token_format: str = "<REDACTED_{TYPE}_{INDEX}>"):
        self.reverse_map = reverse_map
        self.buffer = ""
        # Determine token prefix and suffix from token_format
        if "{" in token_format:
            self.token_prefix = token_format.split("{", 1)[0]
            self.token_suffix = token_format.rsplit("}", 1)[1] if "}" in token_format else ">"
        else:
            self.token_prefix = "<REDACTED_"
            self.token_suffix = ">"
        if not self.token_suffix:
            self.token_suffix = ">"

        # If reverse_map has keys, adapt to actual token boundaries
        if reverse_map:
            sample_key = next(iter(reverse_map.keys()))
            if not sample_key.startswith(self.token_prefix):
                m_start = re.match(r"^[^a-zA-Z0-9]+[a-zA-Z0-9_]*?_", sample_key)
                if m_start:
                    self.token_prefix = m_start.group(0)
            if sample_key[-1] in (">", "]", "}", ")"):
                self.token_suffix = sample_key[-1]

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

            # Now self.buffer starts with token_prefix
            end_pos = self.buffer.find(self.token_suffix)
            if end_pos != -1:
                token = self.buffer[: end_pos + len(self.token_suffix)]
                self.buffer = self.buffer[end_pos + len(self.token_suffix) :]
                output.append(self.reverse_map.get(token, token))
            else:
                if len(self.buffer) > 120:
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
