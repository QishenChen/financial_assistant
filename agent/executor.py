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


def execute(
    query: str,
    session_context: str = "",
    config: dict | None = None,
    max_rounds: int = 8,
    max_retries: int = 2,
    max_replan: int = 1,
) -> dict:
    """
    Full Planner → Executor pipeline.

    Args:
        query: Natural language question
        config: Optional LLM config
        max_rounds: Max ReACT rounds for extract step
        max_retries: Max replan+retry for extract step
        max_replan: Max extra planning rounds triggered by a failed QA step

    Returns:
        {task_type, overall_objective, answer, file_path, steps_log, token_usage, confidence, replan_count}
    """
    if config is None:
        config = get_llm_config()

    all_evidence: list = []
    steps_log: list = []
    total_exec_prompt = 0
    total_exec_completion = 0
    all_step_types: list[str] = []
    execution_plan = None
    replan_context = ""
    replan_count = 0

    for round_num in range(max_replan + 1):
        # ── Phase 1: Plan / Replan ──
        plan_context = session_context
        if replan_context:
            plan_context = plan_context + "\n\n" + replan_context
        execution_plan = plan(query, session_context=plan_context, config=config)
        steps = execution_plan.get("steps", [])

        if not steps:
            return {"error": "Planner produced no steps", "answer": ""}

        # ── Phase 2: Execute steps ──
        first_extract = next((s for s in steps if s.get("task_type") == "extract"), steps[0] if steps else {})
        target_docs = first_extract.get("target_docs", [])

        for step_def in steps:
            task_type = step_def.get("task_type", "extract")
            all_step_types.append(task_type)

            if task_type == "extract":
                result = _run_extract_step(step_def, config, max_rounds, max_retries, all_evidence, session_context)
            elif task_type == "output":
                result = _run_synthesize_step(
                    step_def,
                    config,
                    all_evidence,
                    session_context,
                    query=query,
                    overall_objective=execution_plan.get("overall_objective", query),
                    target_docs=target_docs,
                )
            else:
                result = _run_synthesize_step(step_def, config, all_evidence, session_context)

            all_evidence.append(result["evidence_item"])
            steps_log.append(result["log_entry"])
            total_exec_prompt += result.get("prompt_tokens", 0)
            total_exec_completion += result.get("completion_tokens", 0)

        # ── Phase 3: Check QA verdict ──
        qa_item = next((item for item in reversed(all_evidence) if item.get("task_type") == "qa"), None)
        if not qa_item:
            break

        qa_result = qa_item.get("evidence", {}).get("qa_result", {})
        verdict = str(qa_result.get("verdict", "PASS")).upper()
        if verdict != "FAIL" or round_num == max_replan:
            break

        # QA failed and we have replan budget left → prepare history context and replan.
        replan_count += 1
        gaps = qa_result.get("gaps", [])
        history_summary = _format_replan_history(all_evidence, steps_log)
        replan_context = (
            f"Previous execution round #{round_num + 1} failed QA.\n"
            f"QA gaps:\n" + "\n".join(f"  - {g}" for g in gaps) + "\n\n"
            f"Execution history summary:\n{history_summary}\n\n"
            "Produce a revised plan that addresses the gaps above. Avoid repeating the same failed approach."
        )

    # ── Phase 4: Output step is the final answer ──
    final_output_shape = execution_plan.get("output_shape", "paragraph") if execution_plan else "paragraph"
    output_item = next((item for item in reversed(all_evidence) if item.get("task_type") == "output"), None)
    if output_item:
        answer = output_item.get("evidence", {}).get("synthesis", "")
        ref_map = {}
        for item in all_evidence:
            if isinstance(item, dict) and "ref_map" in item:
                ref_map.update(item["ref_map"])
        if ref_map:
            answer = _replace_ref_tokens(answer, ref_map)
        file_path = _write_result_file(query, answer)
    else:
        # Fallback if planner somehow omitted output step.
        answer, file_path = _synthesize_final(
            query,
            execution_plan.get("overall_objective", query) if execution_plan else query,
            all_evidence,
            final_output_shape,
            config,
            target_docs if "target_docs" in locals() else [],
            session_context,
        )

    plan_tokens = execution_plan.get("token_usage", {}) if execution_plan else {}
    total_tokens = {
        "plan_prompt": plan_tokens.get("prompt_tokens", 0),
        "plan_completion": plan_tokens.get("completion_tokens", 0),
        "exec_prompt": total_exec_prompt,
        "exec_completion": total_exec_completion,
    }

    return {
        "task_type": " → ".join(all_step_types),
        "overall_objective": execution_plan["overall_objective"] if execution_plan else query,
        "output_shape": final_output_shape,
        "answer": answer,
        "file_path": file_path,
        "steps_log": steps_log,
        "token_usage": total_tokens,
        "confidence": 0.85,
        "replan_count": replan_count,
        "error": execution_plan.get("error") if execution_plan else None,
    }


def _format_replan_history(all_evidence: list, steps_log: list) -> str:
    """Build a concise summary of previous execution rounds for replanning."""
    parts = []
    for item in all_evidence:
        stype = item.get("task_type", "unknown")
        obj = item.get("objective", "")
        synth = str(item.get("evidence", {}).get("synthesis", ""))[:250].replace("\n", " ")
        parts.append(f"- {stype}: {obj}\n  result: {synth}")
    return "\n".join(parts)


def _run_extract_step(step_def: dict, config: dict, max_rounds: int, max_retries: int, prior_evidence: list, session_context: str = "") -> dict:
    """Run parallel ReACT loops for each target sub-area, with audit and retry."""
    objective = step_def.get("objective", "")
    target_scope = step_def.get("target_scope", [])
    target_docs = step_def.get("target_docs", [])
    output_shape = step_def.get("output_shape", "structured_data")

    doc_hint = ""
    if target_docs:
        doc_list = ", ".join(target_docs)
        doc_hint = f" FOCUS on these document(s): {doc_list}. Always pass doc=\"{target_docs[0]}\" to search tools."

    context_hint = ""
    if session_context:
        context_hint = (
            "\nUse the following conversation context and long-term memories to interpret follow-up questions "
            "and to prioritize topics the user cares about:\n" + session_context
        )

    task_hints = (
        f"EXTRACT task: collect ALL data about: {', '.join(target_scope)}.{doc_hint} "
        "After finding a heading with search_headings, IMMEDIATELY call get_section to read its full content. "
        "Extract every number, ratio, and fact. Use search_tables for structured data. "
        "Use compute() for any arithmetic. When you have thorough coverage, set done=true."
        f"{context_hint}"
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


def _run_synthesize_step(
    step_def: dict,
    config: dict,
    prior_evidence: list,
    session_context: str = "",
    query: str = "",
    overall_objective: str = "",
    target_docs: list = None,
) -> dict:
    """Run a reasoning, output, or qa synthesis step with a task-specific prompt."""
    objective = step_def.get("objective", "")
    task_type = step_def.get("task_type", "output")
    output_shape = step_def.get("output_shape", "paragraph")
    step_num = step_def["step"]

    evidence_context = _format_prior_evidence_with_pages(prior_evidence)

    context_block = ""
    if session_context:
        context_block = (
            "\nConversation context and long-term memories to keep in mind:\n" + session_context + "\n"
        )

    if task_type == "reasoning":
        system, prompt = _build_reasoning_prompt(objective, output_shape, context_block, evidence_context)
    elif task_type == "qa":
        system, prompt = _build_qa_prompt(objective, output_shape, context_block, evidence_context)
    else:  # output / fallback
        system, prompt = _build_output_prompt(
            query, overall_objective, objective, output_shape, context_block, evidence_context, target_docs
        )

    result = reason(prompt=prompt, system=system, config=config, json_mode=(task_type == "qa"))
    content = result.get("content", "")
    parsed = result.get("parsed") or {}

    evidence_payload = {"synthesis": content[:5000]}
    if task_type == "qa" and isinstance(parsed, dict):
        evidence_payload["qa_result"] = parsed
        # Store the human-readable assessment as the synthesis for downstream prompts.
        assessment = parsed.get("assessment", "")
        if assessment:
            evidence_payload["synthesis"] = str(assessment)[:5000]

    return {
        "evidence_item": {
            "step": step_num,
            "task_type": task_type,
            "objective": objective,
            "evidence": evidence_payload,
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


def _build_reasoning_prompt(objective: str, output_shape: str, context_block: str, evidence_context: str) -> tuple[str, str]:
    system = f"""You are a financial reasoning engine. Analyze, compare, compute, and draw conclusions from the evidence provided below. Do NOT search for new information.

Task: {objective}
Format: {output_shape}
{context_block}
Rules:
- Use ONLY the evidence listed under "Data from previous steps".
- Compute ratios, YoY changes, trends, and comparisons explicitly.
- Flag any missing data or contradictions.
- Return your analysis as a well-formatted {output_shape}. Do NOT write the final user-facing report here."""

    prompt = f"""Data from previous steps:
{evidence_context}

{objective}

Provide your analysis as a {output_shape}."""
    return system, prompt


def _build_qa_prompt(objective: str, output_shape: str, context_block: str, evidence_context: str) -> tuple[str, str]:
    system = f"""You are a rigorous financial data quality auditor. Review the evidence and prior analysis for completeness, accuracy, and consistency. Do NOT search for new information.

Task: {objective}
Format: {output_shape}
{context_block}
Rules:
- Identify gaps, unsupported claims, contradictions, or missing sources.
- Verify units, time periods, and definitions are consistent.
- Assess whether the evidence fully answers the user's question.
- Return ONLY JSON in this exact format:
  {{"verdict": "PASS or FAIL", "gaps": ["gap 1", "gap 2"], "assessment": "concise explanation"}}
- Use FAIL only if the answer is incomplete, inconsistent, or unsupported and requires additional investigation.
- Be specific about what additional evidence or fix is needed."""

    prompt = f"""Data from previous steps:
{evidence_context}

{objective}

Provide your quality assessment as JSON."""
    return system, prompt


def _build_output_prompt(
    query: str,
    overall_objective: str,
    objective: str,
    output_shape: str,
    context_block: str,
    evidence_context: str,
    target_docs: list,
) -> tuple[str, str]:
    doc_hint = ""
    doc_id = target_docs[0] if target_docs else "unknown_doc"
    if target_docs:
        doc_hint = f'The primary document analyzed is: {doc_id}. Use EXACTLY "{doc_id}" as the doc_id in any source links.'

    system = f"""You are a financial report writer. Synthesize the evidence and prior analysis into the final answer for the user. This is the answer that will be shown directly to the user. Do NOT search for new information.

Original question: {query}
Objective: {overall_objective}
Task: {objective}
Format: {output_shape} (output as well-formatted markdown)
{doc_hint}
{context_block}
Rules:
- Include ALL specific numbers, ratios, and facts from the evidence.
- For comparison data, use markdown tables.
- Structure with clear headings (## Section Title).
- Write in Chinese since the user asked in Chinese.
- Cite sources using the evidence annotations:
    [来源: {doc_id} p.N](ref:{doc_id}:N)  when a page number is available
    [来源: {doc_id}](ref:{doc_id})        when no page number is available
- NEVER invent page numbers — only use the ones provided in evidence.
- If no document evidence is available, answer directly from the conversation context and omit source links.

Return the complete markdown answer."""

    prompt = f"""All evidence and analysis:
{evidence_context}

Produce the final {output_shape} answer."""
    return system, prompt


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
            synthesis = evidence.get("synthesis", "")
            if synthesis:
                parts.append(f"Synthesis / intermediate result:\n{synthesis}")
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
    session_context: str = "",
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

    context_block = ""
    if session_context:
        context_block = (
            "\nConversation context and long-term memories to respect:\n" + session_context + "\n"
        )

    system = f"""You are a financial document analyst. Produce the final analysis report.

Original question: {query}
Objective: {overall_objective}
Format: {output_shape} (output as well-formatted markdown)
{doc_hint}
{context_block}

Rules:
- Include ALL specific numbers, ratios, and facts from the evidence
- For comparison data, use markdown tables
- Structure with clear headings (## Section Title)
- Write in Chinese since the user asked in Chinese

SOURCE LINKS:
- If the evidence includes document sources, add source links using the page numbers provided:
    [来源: {doc_id} p.N](ref:{doc_id}:N)
  or, if no page number is available:
    [来源: {doc_id}](ref:{doc_id})
- NEVER invent page numbers — only use the ones provided in evidence.
- If no document evidence is available, answer directly from the conversation context and omit source links.

Return the complete markdown report."""

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

    filename = _write_result_file(query, answer)
    return answer, filename


def _write_result_file(query: str, answer: str) -> str:
    """Save the final answer to a timestamped markdown file."""
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"results/analysis_{timestamp}.md"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Financial Analysis Report\n\n")
        f.write(f"**Query**: {query}\n\n")
        f.write(f"**Generated**: {datetime.datetime.now().isoformat()}\n\n")
        f.write("---\n\n")
        f.write(answer)
    return filename