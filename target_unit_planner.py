"""
12.2 Target Unit Planning Debug (debug-only)

template의 paragraph들을 의미 있는 region으로 묶고, 각 region에 target_unit_type을 할당.
기존 chapter-only 2a/2b에서 unit-aware generation으로 전환하기 위한 planning contract.

Production pipeline 변경 없음. Debug-only.
"""

import json
import logging

log = logging.getLogger(__name__)

CURRENT_PLANNER_VERSION = "v0.1"

VALID_UNIT_TYPES = {"chapter", "shallow_block", "table", "slot", "attachment"}
VALID_STRATEGY_HINTS = {"direct_mapping", "flat_block", "tree_generation", "table_fill", "skip"}
VALID_TABLE_HINTS = {"independent_region", "embedded_in_region", "layout_only", "not_applicable"}
VALID_PROPOSAL_ACTIONS = {"accepted", "adjusted", "split", "merged", "rejected"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Code Proposal (fact-based region suggestion)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def propose_template_regions(
    structure: dict,
    cache_data: dict | None = None,
    unit_observations: list[dict] | None = None,
) -> dict:
    """
    Code 기반 1차 region proposal. AI가 확정/조정할 suggestion.

    Returns:
        {"regions": [...], "method_notes": [...]}
    """
    paragraphs = structure.get("paragraphs", [])
    n = len(paragraphs)
    if n == 0:
        return {"regions": [], "method_notes": ["empty_paragraphs"]}

    # ── Candidate 수집 (제거하지 않음, evidence만 부착) ──
    # chapter_title_level을 하나로 확정하지 않고 level 0~2까지 넓게 수집

    # level 0~1: region boundary candidate (chapter title, header, attachment)
    # level 2+: chapter 내부 sub-heading → region boundary 아님, internal_structure에서 다룸
    title_candidates = []
    from collections import Counter as _Counter
    _candidate_roles = []
    for i, p in enumerate(paragraphs):
        level = p.get("level", 0)
        if level > 1:
            continue
        has_child = any(
            paragraphs[j].get("level", 0) > level
            for j in range(i + 1, min(i + 6, n))
        )
        if has_child:
            _candidate_roles.append(p.get("role", ""))
            title_candidates.append(i)

    # Role repetition / marker sequence (evidence, not gate)
    _role_counts = _Counter(_candidate_roles)
    _repeated_roles = {r for r, c in _role_counts.items() if c >= 2}
    _candidate_markers = [paragraphs[i].get("marker", "") for i in title_candidates]
    _has_sequence_markers = len(set(_candidate_markers)) >= 2 and all(m for m in _candidate_markers)

    # 12.0 chapter observation (evidence, not gate)
    _chapter_obs_role = None
    if unit_observations:
        for u in unit_observations:
            if u.get("unit_type") == "chapter":
                _chapter_obs_role = u.get("observed_role")
                break

    # Per-candidate evidence (no exclusion — all go to AI)
    HEADER_KW = {"제목", "일자", "날짜", "기관", "결재", "배포", "목차"}
    ATTACH_TITLE_KW = {"붙임", "첨부", "별첨", "별표"}
    chapter_title_candidates = []
    for idx in title_candidates:
        p = paragraphs[idx]
        role = p.get("role", "")
        desc = (p.get("description") or "")
        marker = p.get("marker", "")
        level = p.get("level", 0)
        parent_idx = p.get("parent_idx")
        is_repeated = role in _repeated_roles
        has_header_kw = any(kw in desc for kw in HEADER_KW)
        has_attach_kw = any(kw in marker or kw in desc for kw in ATTACH_TITLE_KW)

        # child_count: 이 paragraph를 parent로 하는 직접 자식 수
        child_count = sum(1 for pp in paragraphs if pp.get("parent_idx") == idx)

        evidence = []
        counter_evidence = []

        if is_repeated:
            evidence.append("repeated_role")
        else:
            counter_evidence.append("not_repeated_role")
        if marker:
            evidence.append(f"has_marker: {marker}")
        if _has_sequence_markers and role in _repeated_roles:
            evidence.append("part_of_marker_sequence")
        if child_count >= 3:
            evidence.append(f"has_{child_count}_children")
        if _chapter_obs_role == "strong_candidate":
            evidence.append("12.0_chapter_strong")
        elif _chapter_obs_role == "moderate_candidate":
            evidence.append("12.0_chapter_moderate")
        if has_header_kw:
            counter_evidence.append("header_keyword_in_desc")
        if has_attach_kw:
            counter_evidence.append("attachment_keyword_in_marker_or_desc")
        if idx < 5:
            counter_evidence.append("early_position")
        if level > 0:
            evidence.append(f"level_{level}_not_root")

        # suggested_type: evidence 기반 suggestion (AI가 최종 결정)
        if has_attach_kw:
            suggested = "attachment_title"
            confidence = "high" if is_repeated else "medium"
        elif is_repeated and (marker or _has_sequence_markers):
            suggested = "chapter_title"
            confidence = "high"
        elif is_repeated:
            suggested = "chapter_title"
            confidence = "medium"
        elif has_header_kw and idx < 5:
            suggested = "header_slot"
            confidence = "medium"
        elif child_count >= 3 and not has_header_kw:
            suggested = "chapter_title"
            confidence = "low"
        else:
            suggested = "undetermined"
            confidence = "low"

        chapter_title_candidates.append({
            "idx": idx,
            "role": role,
            "marker": marker,
            "level": level,
            "parent_idx": parent_idx,
            "child_count": child_count,
            "description_preview": desc[:50],
            "suggested_type": suggested,
            "confidence": confidence,
            "evidence": evidence,
            "counter_evidence": counter_evidence,
        })

    # 2. Attachment candidates (keyword hit, no position filtering)
    ATTACH_KW = {"붙임", "첨부", "별첨", "별표"}
    attachment_candidates = []
    for i, p in enumerate(paragraphs):
        desc = p.get("description", "") or ""
        matched_kw = [kw for kw in ATTACH_KW if kw in desc]
        if matched_kw:
            # Check for false positive (e.g., "덧붙임")
            is_likely_false_positive = all(
                desc.find(kw) > 0 and desc[desc.find(kw) - 1] not in (" ", "\t", "(", "【", "\n")
                for kw in matched_kw
            ) if matched_kw else False

            attachment_candidates.append({
                "idx": i,
                "role": p.get("role", ""),
                "matched_keywords": matched_kw,
                "description_preview": desc[:50],
                "position_ratio": round(i / max(n, 1), 2),
                "likely_false_positive": is_likely_false_positive,
            })

    # ── Suggested regions (all body as single undetermined region, AI decides) ──
    regions = []
    method_notes = []

    # Single suggested region: entire document as body_undetermined
    # AI will split into header/chapter/shallow/attachment based on candidates + evidence
    regions.append({
        "region_id": 0,
        "unit_type": "body_undetermined",
        "paragraph_indices": list(range(n)),
        "role_ids": sorted(set(p.get("role", "") for p in paragraphs)),
        "method": "full_document_for_ai_planning",
    })
    method_notes.append(f"full document: {n} paragraphs, AI decides region split")

    return {
        "regions": regions,
        "method_notes": method_notes,
        "chapter_title_candidates": chapter_title_candidates,
        "attachment_candidates": attachment_candidates,
        "chapter_evidence_summary": {
            "total_title_candidates": len(title_candidates),
            "repeated_title_roles": sorted(_repeated_roles),
            "has_sequence_markers": _has_sequence_markers,
            "marker_samples": _candidate_markers[:5],
            "chapter_obs_role": _chapter_obs_role,
        },
    }


TARGET_UNIT_PLANNING_PROMPT = """당신은 template 구조를 분석하여 target unit region을 확정하는 planner입니다.

아래에:
1. template paragraphs (idx, role, level, description)
2. code proposal (1차 region 분할안 — suggestion, 확정 아님)
3. 12.0 unit observations (template-level signal)

이 주어집니다. 이것을 바탕으로 최종 target_unit_plan을 확정하세요.

## target unit types

- slot: 고정 위치 (제목, 날짜, 기관명 등) — direct mapping 가능
- chapter: 독립 content tree (깊은 hierarchy) — tree-based generation
- shallow_block: 얕은 block/bullet 구조 — flat list generation
- table: 독립 표 채우기 region — cell/row-based fill
- attachment: 붙임/첨부 영역 — 별도 처리 또는 skip

## 핵심 규칙

1. **code analysis는 candidate/evidence입니다. hard decision이 아닙니다.** 당신이 최종 region boundary와 unit_type을 결정하세요.
2. **code의 suggested_type과 다르게 판단하면 반드시 evidence와 reason을 남기세요.**
3. **모든 paragraph가 정확히 1개 region에 포함되어야 합니다.** 누락/중복 금지.
4. **depth가 얕고 구조가 flat하면 chapter로 강제 분할하지 마세요.** shallow_block으로 두세요.
5. **table handling을 명확히 하세요.**
   - content table이 독립 block이면: `independent_region`
   - 다른 region 안에 table이 포함되면: `embedded_in_region`
   - layout/서식용 table이면: `layout_only`
6. **internal_structure 힌트를 남기세요.** 특히 shallow_block에서:
   - heading role은 무엇인가
   - 반복 가능한 role은 무엇인가
   - sub-block으로 더 나눌 수 있는 기준 role은 무엇인가
7. **숫자 threshold로 판단하지 마세요.** paragraph facts와 12.0 evidence를 종합 판단.
8. **`generation_strategy_hint`는 hint일 뿐 확정이 아닙니다.**
9. **confidence와 ambiguity_flags를 정직하게 남기세요.**
10. **section boundary가 단순 layout이면 unit으로 보지 마세요.**
11. **`chapter_title_candidates`는 code가 수집한 후보입니다. 확정이 아닙니다.**
    - 각 candidate의 evidence, counter_evidence, suggested_type, confidence를 보고 당신이 판단하세요.
    - suggested_type=chapter_title이라도 counter_evidence가 강하면 slot/header로 바꿀 수 있습니다.
    - suggested_type=header_slot이라도 evidence가 강하면 chapter_title로 바꿀 수 있습니다.
12. **repeated chapter_title/high candidates는 독립 chapter region으로 split하는 것을 strongly prefer합니다.**
    - 같은 role이 반복되고 suggested_type=chapter_title, confidence=high인 candidate가 여러 개이면, 각각 독립 chapter region 시작점입니다.
    - 각 candidate 아래 깊은 hierarchy (child_count >= 1, 하위에 level 2+ 문단)가 있으면 독립 chapter의 강한 증거입니다.
    - 이 candidates를 단일 region으로 merge하려면 반드시 strong counter_evidence와 adjustment_reason을 남기세요.
    - "전체가 하나의 큰 chapter tree"라는 판단은 정당한 근거 없이 내리지 마세요.
    - merge가 정당한 경우: candidate들이 실제로 동일 주제의 subsection이고 generation 단위로 나눌 필요가 없는 경우.
13. **`attachment_candidates`도 code가 수집한 후보입니다.**
    - likely_false_positive, position_ratio를 참고하되 당신이 최종 판단하세요.
    - 문서 전체 구조를 보고 attachment region 경계를 결정하세요.

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "regions": [
    {
      "region_id": 0,
      "unit_type": "slot | shallow_block | chapter | table | attachment",
      "paragraph_indices": [0, 1],
      "role_ids": ["role_cluster_0", "role_cluster_1"],
      "description": "이 region의 역할 요약",
      "section_span": [0],
      "generation_strategy_hint": "direct_mapping | flat_block | tree_generation | table_fill | skip",
      "confidence": "high | medium | low",
      "evidence": ["근거"],
      "internal_structure": {
        "has_substructure": false,
        "child_roles": [],
        "subregion_candidates": [],
        "depth_range": [0, 0],
        "repeatable_roles_in_region": []
      },
      "table_handling": {
        "contains_table": false,
        "table_role_ids": [],
        "content_table_candidate_count": 0,
        "table_handling_hint": "not_applicable"
      },
      "proposal_action": "accepted | adjusted | split | merged | rejected",
      "adjustment_reason": null,
      "supporting_evidence": [],
      "counter_evidence": []
    }
  ],
  "planning_notes": [],
  "ambiguity_flags": []
}
```
"""


def build_target_unit_planning_prompt(
    proposal: dict,
    paragraphs: list[dict],
    unit_observations: list[dict] | None = None,
) -> list[dict]:
    """AI planning prompt 구성."""
    # Paragraph summary (idx, role, level, description)
    para_summary = []
    for p in paragraphs:
        para_summary.append({
            "idx": p.get("idx"),
            "role": p.get("role", ""),
            "level": p.get("level", 0),
            "description": (p.get("description") or "")[:80],
        })

    user_content = json.dumps({
        "paragraphs": para_summary,
        "code_analysis": {
            "chapter_title_candidates": proposal.get("chapter_title_candidates", []),
            "attachment_candidates": proposal.get("attachment_candidates", []),
            "chapter_evidence_summary": proposal.get("chapter_evidence_summary", {}),
            "suggested_regions": proposal.get("regions", []),
            "method_notes": proposal.get("method_notes", []),
        },
        "unit_observations": unit_observations or [],
    }, ensure_ascii=False, indent=2)

    return [
        {"role": "system", "content": TARGET_UNIT_PLANNING_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Parse
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def parse_target_unit_plan_from_llm(raw_output: str) -> dict | None:
    """AI output parse. Returns None on failure."""
    text = _extract_json_text(raw_output)
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    regions = data.get("regions")
    if not isinstance(regions, list) or len(regions) == 0:
        return None

    for r in regions:
        if not isinstance(r, dict):
            return None
        if not r.get("unit_type") or not isinstance(r.get("paragraph_indices"), list):
            return None

    data.setdefault("planning_notes", [])
    data.setdefault("ambiguity_flags", [])
    return data


def _extract_json_text(raw: str) -> str | None:
    """Extract JSON from AI output."""
    if not raw:
        return None
    if "```json" in raw:
        start = raw.index("```json") + 7
        end = raw.find("```", start)
        if end > start:
            raw = raw[start:end]
    elif "```" in raw:
        start = raw.index("```") + 3
        end = raw.find("```", start)
        if end > start:
            raw = raw[start:end]
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        return raw[first:last + 1]
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def validate_target_unit_plan(
    plan: dict,
    paragraphs: list[dict],
    unit_observations: list[dict] | None = None,
) -> dict:
    """Validate coverage, overlap, granularity."""
    blockers = []
    warnings = []

    regions = plan.get("regions", [])
    all_indices = set(p.get("idx", i) for i, p in enumerate(paragraphs))

    # Coverage & Overlap
    covered = set()
    overlaps = []
    for r in regions:
        for idx in r.get("paragraph_indices", []):
            if idx in covered:
                overlaps.append(idx)
            covered.add(idx)

    uncovered = all_indices - covered
    if uncovered:
        blockers.append(f"uncovered_paragraphs: {sorted(uncovered)[:10]}")
    if overlaps:
        blockers.append(f"overlapping_paragraphs: {sorted(set(overlaps))[:10]}")

    # Granularity checks
    granularity = {}

    # shallow_body_over_split_into_chapters
    if unit_observations:
        strong_shallow = any(
            u.get("unit_type") in ("shallow_block", "table")
            and u.get("observed_role") == "strong_candidate"
            for u in unit_observations
        )
        chapter_not_strong = not any(
            u.get("unit_type") == "chapter"
            and u.get("observed_role") == "strong_candidate"
            for u in unit_observations
        )
        has_chapter_region = any(r.get("unit_type") == "chapter" for r in regions)

        if strong_shallow and chapter_not_strong and has_chapter_region:
            blockers.append("shallow_body_over_split_into_chapters")
            granularity["shallow_body_over_split_into_chapters"] = True
        else:
            granularity["shallow_body_over_split_into_chapters"] = False

    # chapter_body_under_split
    if unit_observations:
        chapter_strong = any(
            u.get("unit_type") == "chapter"
            and u.get("observed_role") == "strong_candidate"
            for u in unit_observations
        )
        no_chapter_region = not any(r.get("unit_type") == "chapter" for r in regions)
        # Only flag if body has enough paragraphs for chapter
        body_regions = [r for r in regions if r.get("unit_type") not in ("slot", "attachment")]
        body_para_count = sum(len(r.get("paragraph_indices", [])) for r in body_regions)

        if chapter_strong and no_chapter_region and body_para_count > 20:
            blockers.append("chapter_body_under_split")
            granularity["chapter_body_under_split"] = True
        else:
            granularity["chapter_body_under_split"] = False

    # too_many_regions
    n = len(paragraphs)
    if len(regions) > max(n * 0.5, 5):
        warnings.append(f"too_many_regions: {len(regions)} regions for {n} paragraphs")
        granularity["too_many_regions"] = True
    else:
        granularity["too_many_regions"] = False

    # too_few_regions
    if len(regions) <= 1 and n > 5:
        warnings.append("too_few_regions: only 1 region")
        granularity["too_few_regions"] = True
    else:
        granularity["too_few_regions"] = False

    # unit_type validation
    for r in regions:
        if r.get("unit_type") not in VALID_UNIT_TYPES and r.get("unit_type") != "body_undetermined":
            warnings.append(f"unknown_unit_type: {r.get('unit_type')}")

    return {
        "all_paragraphs_covered": len(uncovered) == 0,
        "no_overlap": len(overlaps) == 0,
        "granularity_checks": granularity,
        "valid": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Legacy Comparison
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def compute_legacy_comparison(plan: dict, pipeline_context: dict | None) -> dict:
    """Compare plan with legacy 2a chapter result."""
    if not pipeline_context:
        return {"has_2a_data": False}

    regions = plan.get("regions", [])
    chapter_count = pipeline_context.get("chapter_count", 0)

    content_regions = [r for r in regions if r.get("unit_type") not in ("slot",)]
    plan_chapter_regions = [r for r in regions if r.get("unit_type") == "chapter"]

    unit_type_match = (
        len(plan_chapter_regions) == chapter_count
        and all(r.get("unit_type") == "chapter" for r in content_regions)
    )

    mismatch_type = "none"
    if not unit_type_match:
        if plan_chapter_regions and len(plan_chapter_regions) != chapter_count:
            mismatch_type = "chapter_count_mismatch"
        elif not plan_chapter_regions and chapter_count > 0:
            mismatch_type = "unit_type_mismatch"
        else:
            mismatch_type = "mixed_mismatch"

    # Source allocation impact
    concentration = pipeline_context.get("source_concentration_ratio")
    impact = "low"
    if mismatch_type != "none":
        if concentration and concentration > 0.8:
            impact = "high"
        elif concentration and concentration > 0.5:
            impact = "medium"
        else:
            impact = "low"

    return {
        "has_2a_data": True,
        "legacy_2a_chapter_count": chapter_count,
        "plan_region_count": len(regions),
        "plan_content_region_count": len(content_regions),
        "plan_chapter_region_count": len(plan_chapter_regions),
        "unit_type_match": unit_type_match,
        "mismatch_type": mismatch_type,
        "source_concentration_ratio": concentration,
        "source_allocation_impact": impact,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Cache Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_plan_cache_valid(cached_plan: dict | None) -> bool:
    if not cached_plan:
        return False
    if cached_plan.get("planner_version") != CURRENT_PLANNER_VERSION:
        return False
    if not cached_plan.get("validation", {}).get("valid", False):
        return False
    return True


def build_plan_cache_payload(ai_plan: dict, validation: dict) -> dict:
    return {
        "planner_version": CURRENT_PLANNER_VERSION,
        "regions": ai_plan.get("regions", []),
        "planning_notes": ai_plan.get("planning_notes", []),
        "ambiguity_flags": ai_plan.get("ambiguity_flags", []),
        "validation": validation,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Debug Output Assembly
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def assemble_planning_debug(
    proposal: dict,
    ai_plan: dict | None,
    validation: dict,
    legacy_comparison: dict,
    unit_observations: list[dict] | None,
    derived_mode_label: str,
    paragraph_count: int,
    cache_status: dict,
    ai_call_info: dict,
    fallback_reason: str | None = None,
) -> dict:
    """15_target_unit_planning.json output 구성."""
    obs_summary = []
    if unit_observations:
        for u in unit_observations:
            obs_summary.append({
                "unit_type": u.get("unit_type"),
                "observed_role": u.get("observed_role"),
            })

    return {
        "schema_version": 1,
        "planner_version": CURRENT_PLANNER_VERSION,
        "debug_only": True,

        "template_context": {
            "paragraph_count": paragraph_count,
            "derived_mode_label_context": derived_mode_label,
            "unit_observations_summary": obs_summary,
        },

        "code_proposal": proposal,
        "ai_plan": ai_plan or {"regions": [], "planning_notes": [], "ambiguity_flags": []},
        "validation": validation,
        "legacy_chapter_comparison": legacy_comparison,

        "cache_status": cache_status,
        "ai_call_info": ai_call_info,
        "fallback_reason": fallback_reason,
    }
