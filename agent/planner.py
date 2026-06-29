"""
Planner — strategic LLM call. Produces multi-step execution plans.

Takes a user query + available document catalog → returns a sequence of steps.
Each step has its own task_type (extract → reasoning → output → qa).
Simple queries get 1 step; complex analytical queries get 2-3 steps chained.

No keywords, no tool params — only WHAT to investigate per step.
"""

import json
import os
from agent.llm_reasoner import reason, get_llm_config
from agent.tools._loader import get_indices


def _load_upload_metadata(doc_id: str) -> dict | None:
    """Load LLM-generated metadata for an uploaded document if it exists."""
    path = os.path.join("uploads", "metadata", f"{doc_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_document_catalog() -> str:
    """Build a summary of available documents for the Planner prompt."""
    indices = get_indices()
    doc_registry = indices.get("doc_registry", {})

    by_id = doc_registry.get("by_id", {})
    if not by_id:
        by_id = doc_registry

    domains = {}
    for doc_path, info in by_id.items():
        if not isinstance(info, dict):
            continue
        domain = info.get("domain", "unknown")
        if domain not in domains:
            domains[domain] = []

        # For uploaded docs, prefer the LLM-generated display name and summary.
        name = info.get("friendly_name", doc_path)
        summary = ""
        if domain == "uploaded":
            meta = _load_upload_metadata(info.get("doc_id", doc_path))
            if meta:
                name = meta.get("display_name") or meta.get("generated_filename") or name
                summary = (meta.get("summary", "") or "")[:160]

        domains[domain].append({
            "path": doc_path,
            "name": name,
            "doc_id": info.get("doc_id", ""),
            "pages": info.get("pages", 0),
            "headings_count": info.get("headings_count", 0),
            "summary": summary,
        })

    catalog_lines = ["Available documents by domain:"]
    for domain, docs in sorted(domains.items()):
        catalog_lines.append(f"\n[{domain}] — {len(docs)} document(s):")
        for d in sorted(docs, key=lambda x: x["name"])[:20]:
            summary_text = f" — {d['summary']}" if d.get("summary") else ""
            catalog_lines.append(
                f"  - {d['name']} (id: {d['doc_id']}, ~{d['pages']} pages){summary_text}"
            )
        if len(docs) > 20:
            catalog_lines.append(f"  ... and {len(docs) - 20} more")

    return "\n".join(catalog_lines)


def plan(query: str, session_context: str = "", config: dict | None = None) -> dict:
    """
    Generate a multi-step execution plan from a natural language query.

    Returns a SEQUENCE of steps. Each step has its own task_type.
    Simple queries = 1 step (extract). Complex queries = extract → reasoning → output.

    Returns:
        {
            "steps": [{step, task_type, objective, target_scope, output_shape}, ...],
            "overall_objective": str,
            "output_shape": str (final),
            "token_usage": dict,
        }
    """
    if config is None:
        config = get_llm_config()

    catalog = build_document_catalog()

    system = """You are a financial document intelligence planner. Produce a multi-step execution plan 
by breaking the user's query into a sequence of steps. Each step has its own task_type 
(extract → reasoning → output → qa).

Task types:
- extract: Pull specific facts, entities, or data points from documents.
- reasoning: Analyze, compare, compute ratios, draw conclusions from extracted data.
- output: Synthesize into free text, summaries, or formatted reports.
- qa: Quality assurance — assess documents for completeness, accuracy, compliance.

For a complex analytical query, use a chain like:
  Step 1: extract — collect raw data
  Step 2: reasoning — compute comparisons, ratios, trends
  Step 3: output — synthesize into a readable report

For simple queries like "list all cities", use a single extract step.

target_scope: Conceptual sub-areas to investigate. NOT document section names — 
the search tools find those dynamically. Use descriptive phrases.

target_docs: List of document IDs to focus on. If the user mentions a specific company, report,
or document, identify the matching document ID from the catalog. For extract steps,
include 1-3 most relevant document IDs. Leave empty for broad cross-document searches.

Return ONLY a JSON object:
{
  "overall_objective": "What the user ultimately wants",
  "output_shape": "list | table | paragraph | assessment_report",
  "steps": [
    {
      "step": 1,
      "task_type": "extract",
      "objective": "What this step accomplishes",
      "target_scope": ["conceptual sub-area 1", "conceptual sub-area 2"],
      "target_docs": ["annual_cmb_2025_report"],
      "output_shape": "structured_data"
    },
    {
      "step": 2,
      "task_type": "reasoning",
      "objective": "Analyze and compare extracted data",
      "target_scope": ["trend analysis", "ratio computation"],
      "target_docs": [],
      "output_shape": "table"
    }
  ]
}"""

    context_block = ""
    if session_context:
        context_block = (
            "\nConversation context and relevant long-term memories:\n" + session_context + "\n"
        )

    prompt = f"""User query: {query}
{context_block}
Available document catalog:
{catalog}

Produce the multi-step execution plan. Simple queries get 1 step; complex analytical queries get 2-3 steps (extract → reasoning → output). Return ONLY JSON."""

    result = reason(prompt=prompt, system=system, config=config, json_mode=True)
    plan_data = result.get("parsed", {}) or {}

    steps = plan_data.get("steps", [])
    valid_types = {"extract", "reasoning", "output", "qa"}

    # If LLM returned old single-step format, convert
    if not steps and plan_data.get("task_type"):
        steps = [{
            "step": 1,
            "task_type": plan_data.get("task_type", "extract"),
            "objective": plan_data.get("objective", query),
            "target_scope": plan_data.get("target_scope", []),
            "output_shape": plan_data.get("output_shape", "list"),
        }]

    # Validate each step
    for s in steps:
        if s.get("task_type") not in valid_types:
            s["task_type"] = "extract"

    return {
        "steps": steps,
        "overall_objective": plan_data.get("overall_objective", query),
        "output_shape": plan_data.get("output_shape", steps[-1].get("output_shape", "paragraph") if steps else "paragraph"),
        "token_usage": result.get("usage", {}),
        "error": result.get("error"),
    }


def replan(original_step: dict, current_findings: str, audit_verdict: str, config: dict | None = None) -> dict:
    """
    Re-plan a failed step. Suggests new target areas or different approach.

    Args:
        original_step: The failed step dict
        current_findings: What we found so far
        audit_verdict: "FAIL" reason from audit

    Returns:
        Updated step with new target_scope suggestions.
    """
    if config is None:
        config = get_llm_config()

    system = """You are a financial document intelligence planner. A step failed its audit.
Suggest new conceptual areas to investigate to fix the gaps.

Return ONLY a JSON object:
{
  "new_target_scope": ["additional conceptual areas to investigate"],
  "alternative_angles": ["different approaches or keywords"],
  "reasoning": "Why these will fix the gaps"
}"""

    prompt = f"""Failed step:
{json.dumps(original_step, ensure_ascii=False, indent=2)}

Audit verdict: {audit_verdict}

Current findings:
{current_findings}

What new areas should be searched? Return JSON."""

    result = reason(prompt=prompt, system=system, config=config, json_mode=True)
    replan_data = result.get("parsed", {}) or {}

    new_step = dict(original_step)
    new_targets = replan_data.get("new_target_scope", [])
    if new_targets:
        new_step["target_scope"] = original_step.get("target_scope", []) + new_targets
    new_step["replan_reasoning"] = replan_data.get("reasoning", "")
    new_step["alternative_angles"] = replan_data.get("alternative_angles", [])
    new_step["replan_token_usage"] = result.get("usage", {})

    return new_step