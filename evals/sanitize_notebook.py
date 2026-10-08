"""Write a shareable copy of the executed walkthrough notebook with internal details removed.

Keeps the code, markdown and run outputs, but replaces LaunchDarkly hostnames, evaluation and run IDs,
and any partial credentials with placeholders, drops SDK log noise, and unpairs the copy from jupytext.
Fails if anything that looks sensitive survives.

Usage (from evals/):
    uv run python sanitize_notebook.py
    uv run python sanitize_notebook.py offline_evals_walkthrough.ipynb offline_evals_walkthrough_public.ipynb
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

SOURCE = Path("offline_evals_walkthrough.ipynb")
TARGET = Path("offline_evals_walkthrough_public.ipynb")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env.staging"

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# Production hosts may appear in the public copy; any other launchdarkly.com host is treated as internal.
PUBLIC_HOSTS = {
    "launchdarkly.com", "app.launchdarkly.com", "docs.launchdarkly.com",
    "stream.launchdarkly.com", "events.launchdarkly.com", "sdk.launchdarkly.com",
}


def host_rules() -> list[tuple[str, str]]:
    """The instance hosts come from the env file, so no internal hostname is written in this script."""
    env = dotenv_values(ENV_FILE) if ENV_FILE.is_file() else {}
    rules = []
    for variable, placeholder in (("LAUNCHDARKLY_STREAM_URI", "<stream-host>"),
                                  ("LAUNCHDARKLY_EVENTS_URI", "<events-host>"),
                                  ("LAUNCHDARKLY_API_HOST", "<launchdarkly-host>")):
        host = re.sub(r"^https?://", "", (env.get(variable) or "").strip()).split("/")[0]
        if host and host not in PUBLIC_HOSTS:
            rules.append((rf"\b{re.escape(host)}\b", placeholder))
    return rules


# Applied in order to every markdown, code and output string.
REPLACEMENTS: list[tuple[str, str]] = [
    (rf"https://[\w.-]+/projects/[\w-]+/ai/evaluations/{UUID}/runs/{UUID}",
     "https://<launchdarkly-host>/projects/<project>/ai/evaluations/<evaluation-id>/runs/<run-id>"),
    *host_rules(),
    (UUID, "<id>"),
    (r"\b(?:sdk|api|mob)-[0-9a-f]{8}-[0-9a-f-]{27}\b", "<redacted-key>"),
    (r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}", "<redacted-key>"),
    (r"\bset \((?:sdk|api|mob|sk-p|Admi)[\w-]*…\)", "set"),  # partial credentials printed by older versions
    (r"\b(?:sdk|api|mob|sk-p|Admi)[\w-]*…", "…"),
    (r"\bAdministrator-\d+\b", "<aws-profile>"),
    (r"\b\d{12}\b", "<aws-account>"),
    (r"/Users/[\w.-]+", "~"),
    (r"[\w.+-]+@launchdarkly\.com", "<email>"),
]

# Output lines that are SDK log noise rather than notebook results.
NOISE = [
    re.compile(r"OpenTelemetry packages not installed"),
    re.compile(r"Error posting \d+ events? \(will retry\)"),
    re.compile(r"A LaunchDarkly client is already initialized"),
]

# Anything matching these after sanitizing means a rule is missing.
LEAK_CHECKS = [
    UUID, r"\b(?:sdk|api|mob)-[0-9a-f]{8}-", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    r"/Users/", r"@launchdarkly\.com", r"Administrator-\d", r"\b\d{12}\b", r"\b(?:sdk|api|mob|sk-p|Admi)[\w-]*…",
]


def clean(text: str) -> str:
    for pattern, replacement in REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    return text


def clean_lines(text: str) -> str:
    kept = [line for line in text.splitlines(keepends=True) if not any(n.search(line) for n in NOISE)]
    return clean("".join(kept))


def as_text(value: Any) -> str:
    return "".join(value) if isinstance(value, list) else str(value)


def sanitize(notebook: dict[str, Any]) -> dict[str, Any]:
    for cell in notebook["cells"]:
        cell["source"] = clean(as_text(cell["source"]))
        cell.get("metadata", {}).pop("execution", None)
        outputs = []
        for output in cell.get("outputs", []):
            kind = output.get("output_type")
            if kind == "error":
                # Tracebacks carry local paths and request details; keep just the message.
                outputs.append({"output_type": "stream", "name": "stderr",
                                "text": clean(f"{output.get('ename')}: {output.get('evalue')}\n")})
                continue
            if kind == "stream":
                text = clean_lines(as_text(output.get("text", "")))
                if text.strip():
                    outputs.append({**output, "text": text})
                continue
            if "data" in output:
                output["data"] = {mime: clean(as_text(value)) if mime.startswith("text/") else value
                                  for mime, value in output["data"].items()}
            outputs.append(output)
        if cell["cell_type"] == "code":
            cell["outputs"] = outputs
    notebook["metadata"].pop("jupytext", None)
    return notebook


def leaks(notebook: dict[str, Any]) -> list[str]:
    blob = json.dumps(notebook, ensure_ascii=False)  # keep '…' literal so partial-credential checks see it
    found = [pattern for pattern in LEAK_CHECKS if re.search(pattern, blob)]
    internal_hosts = {h.lower() for h in re.findall(r"\b[\w.-]+\.launchdarkly\.com\b", blob)} - PUBLIC_HOSTS
    return found + [f"host {host}" for host in sorted(internal_hosts)]


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else TARGET
    notebook = sanitize(json.loads(source.read_text()))
    found = leaks(notebook)
    if found:
        print(f"Not written: possible sensitive values remain for {found}", file=sys.stderr)
        return 1
    target.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n")
    with_outputs = sum(1 for cell in notebook["cells"] if cell.get("outputs"))
    print(f"Wrote {target} ({len(notebook['cells'])} cells, {with_outputs} with outputs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
