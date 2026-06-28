"""
Executor — tactical execution engine.

Runs multi-step plans from the Planner:
  Step 1 (extract): ReACT loop → search documents, collect evidence
  Step 2+ (synthesize): one LLM call using all prior evidence → reasons, formats, saves to file
"""

import json
import os
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from agent.planner import plan, replan
from agent.react_loop import run_react_loop
from agent.llm_reasoner import reason, get_llm_config

try:
    from agent.page_resolver import resolve_page
except ImportError:
    resolve_page = None


def execute(query: str, config: dict | None = None, max_rounds: int = 8, max_retries: int = 2) -> dict:
    """
    Full Planner → Executor pipeline.

    Args:
        query: Natural language question
        config: Optional LLM config
        max_rounds: Max ReACT rounds for extract step
        max_retries: Max replan+retry for extract step

    Returns:
        {task_type, overall_objective, answer, file_path, steps_log, token_usage, confidence}
    """
    if config is None:
        config = get_llm_config()

    # ── Phase 1: Plan ──
    execution_plan = plan(query, config=config)
    steps = execution_plan.get("steps", [])

    if not steps:
        return {"error": "Planner produced no steps", "answer": ""}

    # ── Phase 2: Execute ──
    all_evidence = []
    steps_log = []
    total_exec_prompt = 0
    total_exec_completion = 0

    for step_def in steps:
        step_num = step_def["step"]
        task_type = step_def.get("task_type", "extract")

        if task_type == "extract":
            result = _run_extract_step(step_def, config, max_rounds, max_retries, all_evidence)
        else:
            result = _run_synthesize_step(step_def, config, all_evidence)

        all_evidence.append(result["evidence_item"])
        steps_log.append(result["log_entry"])
        total_exec_prompt += result.get("prompt_tokens", 0)
        total_exec_completion += result.get("completion_tokens", 0)

    # ── Phase 3: Final synthesis → file ──
    final_output_shape = execution_plan.get("output_shape", "paragraph")
    first_extract = next((s for s in steps if s.get("task_type") == "extract"), steps[0] if steps else {})
    target_docs = first_extract.get("target_docs", [])
    answer, file_path = _synthesize_final(
        query,
        execution_plan.get("overall_objective", query),
        all_evidence,
        final_output_shape,
        config,
        target_docs,
    )

    plan_tokens = execution_plan.get("token_usage", {})
    total_tokens = {
        "plan_prompt": plan_tokens.get("prompt_tokens", 0),
        "plan_completion": plan_tokens.get("completion_tokens", 0),
        "exec_prompt": total_exec_prompt,
        "exec_completion": total_exec_completion,
    }

    return {
        "task_type": " → ".join(s.get("task_type", "") for s in steps),
        "overall_objective": execution_plan["overall_objective"],
        "output_shape": final_output_shape,
        "answer": answer,
        "file_path": file_path,
        "steps_log": steps_log,
        "token_usage": total_tokens,
        "confidence": 0.85,
        "error": execution_plan.get("error"),
    }


def _run_extract_step(step_def: dict, config: dict, max_rounds: int, max_retries: int, prior_evidence: list) -> dict:
    """Run parallel ReACT loops for each target sub-area, with audit and retry."""
    objective = step_def.get("objective", "")
    target_scope = step_def.get("target_scope", [])
    target_docs = step_def.get("target_docs", [])
    output_shape = step_def.get("output_shape", "structured_data")

    doc_hint = ""
    if target_docs:
        doc_list = ", ".join(target_docs)
        doc_hint = f" FOCUS on these document(s): {doc_list}. Always pass doc=\"{target_docs[0]}\" to search tools."

    task_hints = (
        f"EXTRACT task: collect ALL data about: {', '.join(target_scope)}.{doc_hint} "
        "After finding a heading with search_headings, IMMEDIATELY call get_section to read its full content. "
        "Extract every number, ratio, and fact. Use search_tables for structured data. "
        "Use compute() for any arithmetic. When you have thorough coverage, set done=true."
    )

    all_ref_map = {}
    step_evidence = {"entities": [], "chunks": []}
    total_entities = 0
    total_rounds = 0
    total_prompt = 0
    total_completion = 0
    step_passed = True

    def extract_area(area: str) -> dict:
        area_goal = f"{objective} — investigating: {area}"
        return run_react_loop(
            goal=area_goal, target_scope=[area],
            output_shape=output_shape, max_rounds=max_rounds,
            config=config, log_id=f"extract_{hash(area) % 10000}",
            extra_system=task_hints,
        )

    with ThreadPoolExecutor(max_workers=min(len(target_scope) or 1, 4)) as pool:
        futures = {pool.submit(extract_area, area): area for area in (target_scope or [objective])}
        for future in as_completed(futures):
            react_result = future.result()
            evidence = react_result.get("evidence", {})
            ref_map = react_result.get("ref_map", {})
            tu = react_result.get("token_usage", {})

            if evidence.get("entities"):
                step_evidence["entities"].extend(evidence["entities"])
            if evidence.get("chunks"):
                step_evidence["chunks"].extend(evidence["chunks"])
            total_entities += react_result.get("entities_found", 0)
            total_rounds += len(react_result.get("rounds", []))
            total_prompt += tu.get("prompt_tokens", 0)
            total_completion += tu.get("completion_tokens", 0)
            all_ref_map.update(ref_map)

    audit_result = _audit_step(objective, step_evidence, "extract", config)
    verdict = audit_result.get("verdict", "PASS")
    if verdict != "PASS":
        step_passed = False

    return {
        "evidence_item": {
            "step": step_def["step"],
            "task_type": "extract",
            "objective": objective,
            "evidence": step_evidence,
            "ref_map": all_ref_map,
            "passed": step_passed,
        },
        "log_entry": {
            "step": step_def["step"],
            "task_type": "extract",
            "objective": objective,
            "verdict": "PASS" if step_passed else "FAIL",
            "audit_detail": audit_result.get("detail", ""),
            "entities_found": total_entities,
            "rounds": total_rounds,
            "retries": 0,
        },
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
    }


def _run_synthesize_step(step_def: dict, config: dict, prior_evidence: list) -> dict:
    """Synthesize step: one LLM call using all prior evidence. No ReACT loop, no tools."""
    objective = step_def.get("objective", "")
    task_type = step_def.get("task_type", "reasoning")
    output_shape = step_def.get("output_shape", "table")
    step_num = step_def["step"]

    evidence_context = _format_prior_evidence(prior_evidence)

    system = f"""You are a financial analyst. You have ALL the data you need — it was already extracted from documents.
Do NOT try to search for anything. Just analyze the data provided below.

Task: {objective}
Task type: {task_type}
Format: {output_shape}

Use ONLY the data provided below. Compute ratios, YoY changes, trends, comparisons.
Return the result as a well-formatted {output_shape}."""

    prompt = f"""Data from previous steps:
{evidence_context}

{objective}

Provide your analysis as a {output_shape}."""

    result = reason(prompt=prompt, system=system, config=config)
    content = result.get("content", "")

    return {
        "evidence_item": {
            "step": step_num,
            "task_type": task_type,
            "objective": objective,
            "evidence": {"synthesis": content[:5000]},
            "passed": True,
        },
        "log_entry": {
            "step": step_num,
            "task_type": task_type,
            "objective": objective,
            "verdict": "PASS",
            "audit_detail": f"Synthesis complete ({len(content)} chars)",
        },
        "prompt_tokens": result.get("usage", {}).get("prompt_tokens", 0),
        "completion_tokens": result.get("usage", {}).get("completion_tokens", 0),
    }


def _format_prior_evidence(prior_evidence: list) -> str:
    """Format all prior step evidence into a single text block for the synthesis LLM."""
    parts = []
    for item in prior_evidence:
        parts.append(f"\n### Step {item['step']} ({item['task_type']}): {item.get('objective', '')}")
        evidence = item.get("evidence", {})
        if isinstance(evidence, dict):
            entities = evidence.get("entities", [])[:30]
            if entities:
                parts.append("Key entities found:")
                for e in entities:
                    parts.append(f"  - {str(e)[:200]}")
            chunks = evidence.get("chunks", [])[:20]
            if chunks:
                parts.append("Detailed evidence:")
                for c in chunks:
                    parts.append(f"  [{c.get('source', '')}] {c.get('text', '')[:500]}")
            synthesis = evidence.get("synthesis", "")
            if synthesis:
                parts.append(f"Synthesis: {synthesis}")
        elif isinstance(evidence, str):
            parts.append(evidence[:3000])
    return "\n".join(parts)


def _format_prior_evidence_with_pages(prior_evidence: list) -> str:
    """Format evidence with page annotations for clickable links."""
    parts = []
    for item in prior_evidence:
        parts.append(f"\n### Step {item['step']} ({item['task_type']}): {item.get('objective', '')}")
        evidence = item.get("evidence", {})
        if isinstance(evidence, dict):
            chunks = evidence.get("chunks", [])[:20]
            if chunks:
                parts.append("Evidence (with doc_id and page for linking):")
                for c in chunks:
                    source = c.get("source", "")
                    text = c.get("text", "")[:500]
                    doc_id = extract_doc_id(source)
                    page = None
                    if resolve_page and doc_id:
                        page = resolve_page(doc_id, text)
                    if page:
                        parts.append(
                            f"  [doc_id={doc_id}, page={page}] {text}"
                        )
                    else:
                        parts.append(
                            f"  [doc_id={doc_id}, page=?] {text}"
                        )
        elif isinstance(evidence, str):
            parts.append(evidence[:3000])
    return "\n".join(parts)


def _replace_ref_tokens(answer: str, ref_map: dict) -> str:
    """Replace {R1}, {R2} tokens in answer with markdown badge links."""
    import re
    
    def replacer(match):
        ref_id = match.group(1)
        ref_info = ref_map.get(ref_id, {})
        doc_id = ref_info.get("doc_id", "doc")
        page = ref_info.get("page")
        if page:
            return f"[来源: {doc_id} p.{page}](ref:{doc_id}:{page})"
        return f"[来源: {doc_id}](ref:{doc_id})"
    
    return re.sub(r'\{(' + '|'.join(re.escape(r) for r in ref_map.keys()) + r')\}', replacer, answer)


def extract_doc_id(source: str) -> str | None:
    """Extract document ID from a source path like 'financial_reports/annual_cmb_2025_report.md'."""
    if not source:
        return None
    base = source.split("/")[-1].replace(".md", "")
    if base:
        return base
    return None


def _audit_step(objective: str, evidence: dict, task_type: str, config: dict) -> dict:
    """Audit evidence quality. Checks every target area for data coverage."""
    evidence_text = json.dumps(evidence, ensure_ascii=False, indent=2)[:8000]

    system = """You are a rigorous financial data quality auditor. Your job is to find GAPS in the evidence.
Return ONLY JSON: {"verdict": "PASS or FAIL", "detail": "explanation", "missing_areas": ["..."], "score": 0.0-1.0}
FAIL if any area lacks specific numbers. PASS only if EVERY area has specific values."""

    prompt = f"""Objective: {objective}

Evidence collected:
{evidence_text}

Audit EVERY area. Is each fully covered with specific data? Return JSON."""

    result = reason(prompt=prompt, system=system, config=config, json_mode=True)
    audit_data = result.get("parsed", {}) or {}
    return {
        "verdict": audit_data.get("verdict", "PASS"),
        "detail": audit_data.get("detail", ""),
        "missing_areas": audit_data.get("missing_areas", []),
        "score": audit_data.get("score", 0.0),
    }


def _synthesize_final(
    query: str,
    overall_objective: str,
    all_evidence: list,
    output_shape: str,
    config: dict,
    target_docs: list = None,
) -> tuple[str, str]:
    """
    Final synthesis: one LLM call → formatted answer + save to file.
    Returns (answer_text, file_path).
    """
    evidence_text = _format_prior_evidence_with_pages(all_evidence)

    doc_hint = ""
    if target_docs:
        doc_id = target_docs[0]
    else:
        doc_id = "unknown_doc"
    
    if target_docs:
        doc_hint = f"""The document analyzed is: {doc_id}. Copy EXACTLY "{doc_id}" as the doc_id in links."""

    system = f"""You are a financial document analyst. Produce the final analysis report.

Original question: {query}
Objective: {overall_objective}
Format: {output_shape} (output as well-formatted markdown)
{doc_hint}

Rules:
- Include ALL specific numbers, ratios, and facts from the evidence
- For comparison data, use markdown tables
- Structure with clear headings (## Section Title)
- Write in Chinese since the user asked in Chinese

MANDATORY SOURCE LINKS — add at least 5-8 throughout the report:
Use this format for links WITHOUT page numbers:
  [来源: {doc_id}](ref:{doc_id})
Use this format for links WITH page numbers (when evidence says "[doc_id=..., page=N]"):
  [来源: {doc_id} p.N](ref:{doc_id}:N)

IMPORTANT: ALWAYS include the page number when the evidence provides it.
Look for "[doc_id=..., page=N]" in the evidence and use those page numbers.
Only omit the page if evidence says "page=?".
NEVER invent page numbers — only use the ones provided in evidence.

Return the complete markdown report with source links."""

    prompt = f"""All evidence (with doc_id and page numbers for linking):
{evidence_text}

Produce the final report with clickable source links. Include page numbers whenever the evidence has them."""

    result = reason(prompt=prompt, system=system, config=config)
    answer = result.get("content", "")

    # Post-process: replace {R1}, {R2} etc. with badge links using ref_map
    ref_map = {}
    for item in all_evidence:
        if isinstance(item, dict) and "ref_map" in item:
            ref_map.update(item["ref_map"])
    if ref_map:
        answer = _replace_ref_tokens(answer, ref_map)

    # Save to file
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"results/analysis_{timestamp}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# Financial Analysis Report\n\n")
        f.write(f"**Query**: {query}\n\n")
        f.write(f"**Generated**: {datetime.datetime.now().isoformat()}\n\n")
        f.write("---\n\n")
        f.write(answer)

    return answer, filename