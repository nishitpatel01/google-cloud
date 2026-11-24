# Google Cloud Enterprise AI Accelerators (`solutions/`)

![Google Cloud](https://img.shields.io/badge/Google%20Cloud-Vertex%20AI%20%C2%B7%20ADK%20%C2%B7%20BigQuery%20%C2%B7%20Cloud%20Run-4285F4.svg) ![Deploy Standard](https://img.shields.io/badge/deploy-Terraform%20%2B%20Cloud%20Run%20(%3C15m)-FBBC04.svg) ![Tier-1 Idle](https://img.shields.io/badge/tier--1%20idle-%240--%243%2Fmo-34A853.svg)

Production-oriented, turnkey Google Cloud **Enterprise AI Accelerators** and **Vertical Starters** built on Google Cloud's official [Agentic AI](https://docs.cloud.google.com/architecture/agentic-ai-overview), [Generative AI](https://docs.cloud.google.com/architecture/genai-overview), and [ML Applications & Operations](https://docs.cloud.google.com/architecture/ai-ml/ml-application-operations-architecture-guides) reference architectures.

---

## Published Solutions

Solutions are added here incrementally as each standalone app is implemented, tested, and verified on Cloud Run:

| Solution | Problem Solved & Business Impact | Industries | Core GCP AI Stack | Status |
| :--- | :--- | :--- | :--- | :---: |
| [**Risk & Contagion Intelligence (`enterprise-graphrag`)**](enterprise-graphrag/README.md) | Traces 2–4 hop supply chain, sanctions, and contract contagion missed by flat vector RAG (`0.00` → `0.88–1.00` multi-tier recall; `6–16h` manual audits reduced to seconds at `$0.003–$0.006`/query) | Supply Chain, Banking AML, Legal M&A | BigQuery Property Graph (ISO GQL), Spanner Graph (Tier-2), Vertex AI `gemini-embedding-2` & GenAI Eval, Google ADK, Model Armor, React 19, Cloud Run | Production Ready (`v0.6.0`) |
| [**Operations & Incident War Room (`ops-war-room`)**](ops-war-room/README.md) | Replaces 45–75 min swivel-chair triage with parallel multi-agent investigation, adversarial log-injection defense, two-click lead approval, and atomic Cloud Workflows sagas | Cybersecurity SOC, Supply Chain O2C, Cloud SRE | Google ADK `2.15` (`ParallelAgent`), BigQuery, Cloud Workflows, Firestore, Model Armor, Vertex AI GenAI Eval, Cloud Run | In Progress (`v0.3.0` · API, Model Armor & Eval Verified) |

---

## Standard Customer Deployment Contract

Every solution under `solutions/<slug>/` is designed to be **100% standalone-deployable** in any customer's Google Cloud project:

1. **Zero-Empty-State Dual Mode**: Ships with real-public-first industry packs (`scripts/seed_data.py`), deterministic offline `DEMO_MODE=replay` fixtures, and 1-click `DEMO_MODE=live` execution on Vertex AI.
2. **Two-Tier Terraform Footprint**:
   - **Tier 1 (Default — `< 15 min` deploy, `≈ $0–$3/mo` idle)**: Cloud Run (`min-instances=0`) + BigQuery / Firestore + Vertex AI Serverless APIs + dedicated least-privilege runtime SA (zero JSON keys).
   - **Tier 2 (`enable_enterprise_scale=true`)**: Optional production upgrade path for AlloyDB ScaNN, Spanner Graph, Vertex Vector Search, Cloud Healthcare FHIR, Dataflow, Cloud IAP, and Shared VPC / Private Service Connect.
3. **Built-In Trust, Eval & ROI**: Ships with Model Armor + Cloud DLP guardrails, self-service BYO-data `/ingest`, independent Vertex AI GenAI Evaluation, and Monthly GCP TCO estimation.
4. **Clean Lifecycle & Inventory**: Every solution applies consistent GCP resource labels and ships with `./scripts/deploy.sh`, `./scripts/inventory_resources.sh`, and `./scripts/teardown.sh` for transparent discovery and safe cleanup.

```bash
# Quickstart inside any published solution directory:
cd solutions/<slug>
cp .env.example .env           # Set GCP_PROJECT_ID and GCP_REGION only
./scripts/deploy.sh --dry-run  # Preview APIs, IAM, and Cloud Run/Terraform changes
./scripts/deploy.sh --yes      # Provision, seed sample data, and launch
```

---

## Maintainer Workflow & 3-Layer README Sync

Every push adding or updating code under `solutions/` keeps all three documentation layers synchronized and passes the automated hygiene gate:

1. **Solution-local README**: `solutions/<slug>/README.md` (problem, architecture, deploy commands, tests).
2. **Solutions hub README**: [`solutions/README.md`](README.md) (add/update row and link).
3. **Parent root README**: [`../README.md`](../README.md) (mirror updated solution link & status).
4. **Pre-push gate**: Run `./scripts/check_repo_hygiene.sh` from repo root.
