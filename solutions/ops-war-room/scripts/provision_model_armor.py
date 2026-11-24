#!/usr/bin/env python3
"""Provision or verify dedicated $0/mo Google Cloud Model Armor template for ACC-04."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import google.auth
import httpx
from google.auth.transport.requests import Request as AuthRequest

SOLUTION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOLUTION_ROOT / "backend"))

from app.core.attribution import DEFAULT_MODEL_ARMOR_TEMPLATE, build_gcp_labels  # noqa: E402


def provision_template(*, project_id: str, location: str, template_id: str, env: str) -> None:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not creds.valid:
        creds.refresh(AuthRequest())
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    base = f"https://modelarmor.{location}.rep.googleapis.com/v1/projects/{project_id}/locations/{location}/templates"

    with httpx.Client(timeout=20.0) as client:
        existing = client.get(f"{base}/{template_id}", headers=headers)
        if existing.status_code == 200:
            print(f"Model Armor template already exists: {project_id}/{location}/{template_id}")
            return

        body = {
            "filterConfig": {
                "piAndJailbreakFilterSettings": {
                    "filterEnforcement": "ENABLED",
                    "confidenceLevel": "LOW_AND_ABOVE",
                },
                "maliciousUriFilterSettings": {"filterEnforcement": "ENABLED"},
                "raiSettings": {
                    "raiFilters": [
                        {"filterType": "DANGEROUS", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                        {"filterType": "HATE_SPEECH", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                        {"filterType": "HARASSMENT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                        {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                    ]
                },
            },
            "labels": build_gcp_labels(
                tier="tier1_serverless",
                managed_by="provision_script",
                env=env,
                component="model_armor_guardrail",
            ),
        }
        created = client.post(f"{base}?templateId={template_id}", headers=headers, json=body)
        if created.status_code not in (200, 201):
            raise SystemExit(f"ERROR creating Model Armor template ({created.status_code}): {created.text}")
        print(f"Created Model Armor template: {project_id}/{location}/{template_id} ($0/mo standing)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision ACC-04 Model Armor template")
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--template-id", default=DEFAULT_MODEL_ARMOR_TEMPLATE)
    parser.add_argument("--env", default="dev")
    args = parser.parse_args()
    provision_template(
        project_id=args.project,
        location=args.location,
        template_id=args.template_id,
        env=args.env,
    )


if __name__ == "__main__":
    main()
