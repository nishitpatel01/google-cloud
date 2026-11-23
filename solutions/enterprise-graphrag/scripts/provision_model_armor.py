#!/usr/bin/env python3
"""Idempotently provision a dedicated $0/mo Google Cloud Model Armor template for ACC-05.

Creates `<MODEL_ARMOR_TEMPLATE_ID>` (default: `acc05-enterprise-graphrag-guardrail`)
in `<MODEL_ARMOR_LOCATION>` (default: `us-central1`) with Prompt Injection/Jailbreak,
Responsible AI, and Malicious URI filters so this accelerator never shares templates
with unrelated projects.

Usage:
  PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py --dry-run
  PYTHONPATH=backend .venv/bin/python scripts/provision_model_armor.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = SOLUTION_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings  # noqa: E402
from app.core.security import _get_adc_bearer_token  # noqa: E402

DEFAULT_TEMPLATE_ID = "acc05-enterprise-graphrag-guardrail"


def template_payload() -> dict[str, Any]:
    """Balanced enterprise guardrail policy ($0/mo template; pay-per-token above free tier)."""
    return {
        "filterConfig": {
            "piAndJailbreakFilterSettings": {
                "filterEnforcement": "ENABLED",
                "confidenceLevel": "MEDIUM_AND_ABOVE",
            },
            "maliciousUriFilterSettings": {
                "filterEnforcement": "ENABLED",
            },
            "raiSettings": {
                "raiFilters": [
                    {"filterType": "HATE_SPEECH", "confidenceLevel": "HIGH"},
                    {"filterType": "HARASSMENT", "confidenceLevel": "HIGH"},
                    {"filterType": "DANGEROUS", "confidenceLevel": "HIGH"},
                    {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "HIGH"},
                ]
            },
        },
        "templateMetadata": {
            "ignorePartialInvocationFailures": False,
            "logTemplateOperations": True,
            "logSanitizeOperations": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision dedicated ACC-05 Model Armor template.")
    parser.add_argument("--apply", action="store_true", help="Create template if missing.")
    parser.add_argument("--dry-run", action="store_true", help="Preview template config without mutating GCP.")
    parser.add_argument("--template-id", default="", help=f"Override template ID (default: {DEFAULT_TEMPLATE_ID}).")
    args = parser.parse_args()

    settings = Settings()
    if not settings.gcp_project_id:
        raise SystemExit("ERROR: GCP_PROJECT_ID must be set.")

    loc = settings.model_armor_location.strip() or "us-central1"
    tpl_id = (args.template_id or settings.model_armor_template_id or DEFAULT_TEMPLATE_ID).strip()
    base = f"https://modelarmor.{loc}.rep.googleapis.com/v1/projects/{settings.gcp_project_id}/locations/{loc}/templates"
    get_url = f"{base}/{tpl_id}"

    if args.dry_run or not args.apply:
        print(f"[dry-run] Target Model Armor template: projects/${{GCP_PROJECT_ID}}/locations/{loc}/templates/{tpl_id}")
        print(json.dumps(template_payload(), indent=2))
        print("[dry-run] Re-run with --apply to provision.")
        return 0

    token = _get_adc_bearer_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    existing = httpx.get(get_url, headers=headers, timeout=10.0)
    if existing.status_code == 200:
        print(f"[model-armor] Template already exists (idempotent): {tpl_id} in {loc}")
        return 0

    resp = httpx.post(
        base,
        params={"templateId": tpl_id},
        headers=headers,
        json=template_payload(),
        timeout=15.0,
    )
    if resp.status_code in (200, 201):
        print(f"[model-armor] Created dedicated $0/mo template: {tpl_id} in {loc}")
        return 0
    if resp.status_code == 409:
        print(f"[model-armor] Template already exists: {tpl_id} in {loc}")
        return 0
    raise SystemExit(f"ERROR: Model Armor provisioning failed (HTTP {resp.status_code}): {resp.text[:300]}")


if __name__ == "__main__":
    raise SystemExit(main())
