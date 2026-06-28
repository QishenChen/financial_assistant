"""
Generic ReACT Loop — Think→Act→Observe engine.

Used by the Executor for each step. LLM decides tools per round.
get_section is always available. Full observations passed to next THINK.
"""

import json
import re
from agent.llm_reasoner import call_llm, parse_json_from_response, get_llm_config, estimate_tokens
from agent.tools import TOOLS, execute_tool, build_tools_prompt
from agent.ref_mapper import RefMapper


def run_react_loop(
    goal: str,
    target_scope: list[str],
    output_shape: str = "list",
    max_rounds: int = 8,
    config: dict | None = None,
    log_id: str = "",
    extra_system: str = "",
) -> dict:
    """
    Generic ReACT loop. get_section available from round 1. Full observations passed through.

    Returns:
        {
            "evidence": dict,
            "confidence": float,
            "rounds": list,
            "ref_map": dict,     # All R1,R2... refs with resolved pages
            "token_usage": {"prompt_tokens": int, "completion_tokens": int},
            "status": str,
            "entities_found": int,
        }
    """
    if config is None:
        config = get_llm_config()

    tools_desc = build_tools_prompt()
    mapper = RefMapper()

    round_log = []
    collected = []
    found_entities = set()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    saturation_count = 0
    status = "max_rounds"

    for round_num in range(1, max_rounds + 1):
        think_result = _think_step(
            goal=goal,
            target_scope=target_scope,
            output_shape=output_shape,
            tools_desc=tools_desc,
            round_log=round_log,
            round_num=round_num,
            max_rounds=max_rounds,
            config=config,
            log_id=log_id,
            extra_system=extra_system,
        )

        if think_result.get("usage"):
            total_prompt_tokens += think_result["usage"].get("prompt_tokens", 0)
            total_completion_tokens += think_result["usage"].get("completion_tokens", 0)

        actions = think_result.get("actions", [])
        if think_result.get("done") or not actions:
            status = "done"
            break

        act_results = _act_step(actions)
        new_count = _observe_step(act_results, collected, found_entities)
        
        # Assign ref IDs immediately (async page resolution fires in background)
        if mapper:
            ref_context = mapper.assign(act_results)
        
        round_log.append({
            "round": round_num,
            "reasoning": think_result.get("reasoning", ""),
            "actions": actions,
            "results": act_results,
            "ref_context": ref_context if mapper else {},
        })

        if new_count == 0:
            saturation_count += 1
            if saturation_count >= 2:
                status = "saturated"
                break
        else:
            saturation_count = 0

    confidence = _compute_confidence(collected, found_entities, status)
    ref_map = mapper.resolve_all_now()  # Wait for pending page resolutions
    mapper.shutdown()

    return {
        "collected": collected,
        "evidence": _summarize_evidence(collected, found_entities),
        "confidence": confidence,
        "rounds": round_log,
        "ref_map": ref_map,
        "token_usage": {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total": total_prompt_tokens + total_completion_tokens,
        },
        "status": status,
        "entities_found": len(found_entities),
    }


def _think_step(
    goal: str,
    target_scope: list[str],
    output_shape: str,
    tools_desc: str,
    round_log: list[dict],
    round_num: int,
    max_rounds: int,
    config: dict,
    log_id: str,
    extra_system: str,
) -> dict:
    """THINK phase: decide next tool(s). Full observation data passed through."""

    # Build full observation summary from previous rounds — pass actual results
    obs_summary = ""
    for r in round_log:
        obs_summary += f"\n--- Round {r['round']} ---\n"
        obs_summary += f"Reasoning: {r.get('reasoning', '')[:300]}\n"
        for i, act in enumerate(r.get("actions", [])):
            result = r.get("results", [{}])[i] if i < len(r.get("results", [])) else {}
            tool = act.get("tool", "unknown")
            params = json.dumps(act.get("params", {}), ensure_ascii=False)
            obs_summary += f"  Action: {tool}({params})\n"
            # Include actual result data (up to 2000 chars per result)
            result_data = result.get("result")
            if isinstance(result_data, list):
                obs_summary += f"  Result: {json.dumps(result_data[:10], ensure_ascii=False)[:2000]}\n"
            elif isinstance(result_data, dict):
                snippet = {}
                for k in ["heading", "content"]:
                    if k in result_data:
                        snippet[k] = str(result_data[k])[:1000]
                for k in ["tables"]:
                    if k in result_data:
                        snippet[k] = f"[{len(result_data[k])} tables]"
                obs_summary += f"  Result: {json.dumps(snippet, ensure_ascii=False)[:2000]}\n"
            elif result.get("error"):
                obs_summary += f"  Error: {result['error'][:200]}\n"
            else:
                obs_summary += f"  Result: {str(result_data)[:500]}\n"

    # Build cumulative ref context from all previous rounds
    ref_context_lines = []
    for r in round_log:
        rc = r.get("ref_context", {})
        if rc:
            for ref_id, info in rc.items():
                doc = info.get("doc_id", "doc")
                text = info.get("text", "")[:150]
                ref_context_lines.append(f"  {ref_id}: {text} [{doc}]")
    ref_context_str = "\n".join(ref_context_lines)
    if ref_context_str:
        ref_context_str = "Evidence references (cite these as {R1}, {R2}, etc.):\n" + ref_context_str

    # Truncate if too long
    if len(obs_summary) > 8000:
        obs_summary = obs_summary[-8000:]
        obs_summary = "...(earlier rounds truncated)\n" + obs_summary

    system = f"""You are a financial document analyst executing a retrieval plan.

Goal: {goal}
Target areas: {', '.join(target_scope)}
Expected output: {output_shape}

{tools_desc}

Rules:
- get_section is available in ALL rounds — use it whenever you find a relevant heading
- Use search_headings first to find relevant sections
- Use search_text with 2-3 pipe-separated keywords (e.g. "利润|资产|负债")
- Use search_tables for structured data  
- After finding a heading, CALL get_section to read the full content and extract ALL numbers
- Use compute for ALL numeric calculations
- Search in Chinese by default for Chinese documents
- Return ONLY JSON: {{"actions": [{{"tool": "...", "params": {{...}}}}], "reasoning": "...", "done": false}}

When you have extracted enough data to satisfy the goal, set done=true and actions=[].
{extra_system}"""

    user = f"""Round {round_num}/{max_rounds}

Previous observations:
{obs_summary if obs_summary else '(First round — no observations yet)'}

{ref_context_str}

What tool(s) should you use next? Return JSON."""

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    result = call_llm(messages, config, log_qid=log_id, round_num=round_num)

    if result.get("error"):
        terms = re.findall(r'[\u4e00-\u9fff]{2,}', goal)
        query = "|".join(list(dict.fromkeys(terms))[:6]) or goal[:60]
        return {
            "actions": [{"tool": "search_headings", "params": {"query": query, "max_results": 10}}],
            "reasoning": f"LLM error fallback: {result['error']}",
            "done": False,
            "usage": result.get("usage", {}),
        }

    parsed = parse_json_from_response(result["content"])

    if parsed.get("error"):
        terms = re.findall(r'[\u4e00-\u9fff]{2,}', goal)
        query = "|".join(list(dict.fromkeys(terms))[:6]) or goal[:60]
        return {
            "actions": [{"tool": "search_headings", "params": {"query": query, "max_results": 10}}],
            "reasoning": f"JSON parse fallback",
            "done": False,
            "usage": result.get("usage", {}),
        }

    return {
        "actions": parsed.get("actions", []),
        "reasoning": parsed.get("reasoning", ""),
        "done": parsed.get("done", False),
        "usage": result.get("usage", {}),
    }


def _act_step(actions: list[dict]) -> list[dict]:
    results = []
    for action in actions:
        tool_name = action.get("tool", "")
        params = action.get("params", {})
        tool_result = execute_tool(tool_name, **params)
        results.append({
            "tool": tool_name,
            "params": params,
            "result": tool_result.get("result"),
            "error": tool_result.get("error"),
        })
    return results


def _observe_step(act_results: list[dict], collected: list, found_entities: set) -> int:
    new_count = 0
    for r in act_results:
        result = r.get("result")
        if result is None:
            collected.append(r)
            continue

        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    entity_key = item.get("title") or item.get("name") or item.get("text") or item.get("table_id", "")
                elif isinstance(item, str):
                    entity_key = item[:80]
                else:
                    entity_key = str(item)[:80]
                if entity_key and entity_key not in found_entities:
                    found_entities.add(entity_key)
                    new_count += 1
        elif isinstance(result, dict):
            for key in ["tables", "matches", "data"]:
                items = result.get(key, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            entity_key = item.get("title") or item.get("name") or item.get("text") or item.get("table_id", "")
                        elif isinstance(item, str):
                            entity_key = item[:80]
                        else:
                            entity_key = str(item)[:80]
                        if entity_key and entity_key not in found_entities:
                            found_entities.add(entity_key)
                            new_count += 1
            content = result.get("content", "")
            if content and isinstance(content, str) and len(content) > 10:
                entity_key = content[:120]
                if entity_key not in found_entities:
                    found_entities.add(entity_key)
                    new_count += 1
            heading = result.get("heading", "")
            if heading and heading not in found_entities:
                found_entities.add(heading)
                new_count += 1
        elif isinstance(result, str) and len(result) > 1:
            if result not in found_entities:
                found_entities.add(result[:120])
                new_count += 1

        collected.append(r)
    return new_count


def _compute_confidence(collected: list, found_entities: set, status: str) -> float:
    n = len(found_entities)
    if status == "error":
        return 0.0
    if status == "saturated":
        return 0.9 if n >= 10 else (0.7 if n >= 5 else 0.5)
    if status == "done":
        return 0.85 if n >= 10 else (0.6 if n >= 5 else 0.4)
    return 0.7 if n >= 10 else (0.5 if n >= 5 else (0.3 if n > 0 else 0.0))


def _summarize_evidence(collected: list, found_entities: set) -> dict:
    entities = list(found_entities)
    chunks = []
    for obs in collected:
        result = obs.get("result")
        tool = obs.get("tool", "")

        if isinstance(result, list):
            for item in result[:10]:
                if isinstance(item, dict):
                    chunks.append({
                        "text": item.get("title") or item.get("name") or item.get("text", ""),
                        "source": item.get("doc") or item.get("heading_title", ""),
                        "tool": tool,
                    })
                elif isinstance(item, str):
                    chunks.append({"text": item[:300], "source": "", "tool": tool})
        elif isinstance(result, dict):
            content = result.get("content", "")
            if content:
                chunks.append({"text": content[:2000], "source": result.get("heading", ""), "tool": tool})
            for t in result.get("tables", [])[:5]:
                if isinstance(t, dict):
                    headers = t.get("headers", [])
                    data_rows = t.get("data", [])[:10]
                    chunks.append({
                        "text": f"Table: {t.get('name','')} [{', '.join(str(h) for h in headers)}] — {json.dumps(data_rows, ensure_ascii=False)[:500]}",
                        "source": result.get("heading", ""),
                        "tool": tool,
                    })
        elif isinstance(result, str):
            chunks.append({"text": result[:2000], "source": "", "tool": tool})

    return {
        "entities": entities[:100],
        "chunks": chunks[:30],
        "total_items": len(collected),
    }