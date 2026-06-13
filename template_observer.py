"""
12.0 Template Unit Observation (debug-only)

template의 구조를 관측하여 어떤 target unit이 적합한지 AI가 판단.
기존 2a/2b pipeline에 영향을 주지 않는 debug observation.

핵심 원칙:
- unit_observations가 primary output
- derived_mode_label은 convenience summary (policy switch 아님)
- pipeline_fit은 code-only (2a 결과와의 비교)
- fit_assessment(observed_role)는 planning 확정값이 아닌 observation
- 12.2에서 독립적 target_unit planning을 수행
"""

import json
import logging

log = logging.getLogger(__name__)

CURRENT_OBSERVER_VERSION = "v0.2"

VALID_UNIT_TYPES = {"chapter", "shallow_block", "table", "slot", "section", "attachment"}
VALID_OBSERVED_ROLES = {"strong_candidate", "moderate_candidate", "background_signal", "not_indicated"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Facts Layer (code-only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def extract_template_unit_features(structure: dict, cache_data: dict | None = None) -> dict:
    """
    structure에서 AI 판단에 필요한 facts를 추출.
    cache_data가 있으면 top-level 필드(table_count 등)도 활용.
    """
    paragraphs = structure.get("paragraphs", [])
    grammar = structure.get("template_grammar", {}).get("global", {})
    chapter_types = structure.get("chapter_types", {})

    chapter_title_level = _get_chapter_title_level(paragraphs)
    body_info = _count_body_paragraphs(paragraphs, chapter_title_level)
    table_signal = _detect_table_signal(paragraphs, cache_data)
    structural_signals = _detect_structural_signals(paragraphs, chapter_title_level)

    # depth distribution
    depth_dist = {}
    for p in paragraphs:
        lv = p.get("level", 0)
        depth_dist[f"level_{lv}"] = depth_dist.get(f"level_{lv}", 0) + 1

    # grammar summary
    total_roles = len(grammar)
    repeatable_count = sum(1 for g in grammar.values() if g.get("repeatable"))
    leaf_count = sum(1 for g in grammar.values() if not g.get("allowed_children"))

    # deepest path roles (roles with max depth in grammar)
    max_depth_roles = []
    if paragraphs:
        max_lv = max(p.get("level", 0) for p in paragraphs)
        max_depth_roles = sorted(set(
            p.get("role", "") for p in paragraphs
            if p.get("level", 0) >= max_lv - 1 and p.get("role")
        ))[:5]

    return {
        "paragraph_count": len(paragraphs),
        "body_paragraph_observation": body_info,
        "section_count": structure.get("section_count", 1),
        "table_signal": table_signal,
        "role_cluster_count": total_roles,
        "chapter_type_count": len(chapter_types),
        "chapter_type_names": sorted(chapter_types.keys()),
        "max_observed_level": max(
            (p.get("level", 0) for p in paragraphs), default=0
        ),
        "depth_distribution": depth_dist,
        "repeatable_role_ratio": round(
            repeatable_count / max(total_roles, 1), 3
        ),
        "leaf_role_ratio": round(leaf_count / max(total_roles, 1), 3),
        "marker_family_summary": _summarize_marker_families(structure),
        "structural_signals": structural_signals,
        "grammar_summary": {
            "total_roles": total_roles,
            "max_children_per_role": max(
                (len(g.get("allowed_children", []))
                 for g in grammar.values()),
                default=0,
            ),
            "max_parents_per_role": max(
                (len(g.get("allowed_parents", []))
                 for g in grammar.values()),
                default=0,
            ),
            "deepest_path_roles": max_depth_roles,
        },
    }


def _get_chapter_title_level(paragraphs: list[dict]) -> int:
    """chapter title level 추정. _build_chapter_types와 동일 로직."""
    n = len(paragraphs)
    l0_with_children = 0
    for i, p in enumerate(paragraphs):
        if p.get("level", 0) == 0:
            if i + 1 < n and paragraphs[i + 1].get("level", 0) > 0:
                l0_with_children += 1
    return 0 if l0_with_children >= 2 else 1


def _count_body_paragraphs(paragraphs: list[dict], chapter_title_level: int) -> dict:
    """body paragraph count + debug info."""
    n = len(paragraphs)
    header_like = 0
    title_like = 0
    empty = 0
    caveats = []

    first_chapter_idx = None
    for i, p in enumerate(paragraphs):
        if p.get("level", 0) == chapter_title_level:
            has_child = any(
                paragraphs[j].get("level", 0) > chapter_title_level
                for j in range(i + 1, min(i + 6, n))
            )
            if has_child:
                first_chapter_idx = p.get("idx", i)
                break

    header_samples = []
    title_samples = []
    empty_samples = []
    included_samples = []

    for i, p in enumerate(paragraphs):
        idx = p.get("idx", i)
        level = p.get("level", 0)
        role = p.get("role", "")

        if first_chapter_idx is not None and idx < first_chapter_idx:
            header_like += 1
            if len(header_samples) < 5:
                header_samples.append(idx)
            continue

        if level == chapter_title_level:
            has_child = any(
                paragraphs[j].get("level", 0) > chapter_title_level
                for j in range(i + 1, min(i + 6, n))
            )
            if has_child:
                title_like += 1
                if len(title_samples) < 5:
                    title_samples.append(idx)
                continue

        if not role:
            empty += 1
            if len(empty_samples) < 5:
                empty_samples.append(idx)
            continue

        if len(included_samples) < 5:
            included_samples.append(idx)

    body_count = n - header_like - title_like - empty

    if first_chapter_idx is None:
        caveats.append("no_chapter_boundary_found")
    if body_count < 0:
        caveats.append("negative_body_count_clamped")
        body_count = 0

    return {
        "count": body_count,
        "method": "exclude_header_title_empty",
        "excluded": {
            "header_like": header_like,
            "title_like": title_like,
            "empty_or_placeholder": empty,
        },
        "debug": {
            "chapter_title_level": chapter_title_level,
            "first_chapter_idx": first_chapter_idx,
            "excluded_idx_samples": {
                "header_like": header_samples,
                "title_like": title_samples,
                "empty_or_placeholder": empty_samples,
            },
            "included_idx_samples": included_samples,
            "caveats": caveats,
        },
    }


def _detect_table_signal(paragraphs: list[dict], cache_data: dict | None = None) -> dict:
    """table signal 감지. structure.tables의 rows/cols 기반 분류."""
    structure_tables = None
    if cache_data and "structure" in cache_data:
        structure_tables = cache_data["structure"].get("tables", [])
    elif cache_data and cache_data.get("table_count") is not None:
        # structure.tables가 없으면 top-level table_count만 사용 (legacy)
        pass

    if structure_tables is not None:
        n = len(paragraphs) or 1
        total = len(structure_tables)
        single_cell = sum(
            1 for t in structure_tables
            if t.get("rows", 1) == 1 and t.get("cols", 1) == 1
        )
        multi_cell = total - single_cell
        return {
            "detection_method": "hwpx_dom_rows_cols_analysis",
            "detection_available": True,
            "table_detected": total > 0,
            "total_table_count": total,
            "single_cell_table_count": single_cell,
            "multi_cell_table_count": multi_cell,
            "content_table_candidate_count": multi_cell,
            "content_table_candidate_ratio": round(multi_cell / n, 3),
            "total_table_ratio": round(total / n, 3),
            "accuracy_caveat": (
                "multi_cell_table_count is a weak heuristic (rows>1 OR cols>1); "
                "may still include layout tables (e.g., 1x3 title boxes); "
                "not a definitive content table classifier"
            ),
        }

    # Fallback: top-level table_count only (no rows/cols breakdown)
    if cache_data and cache_data.get("table_count") is not None:
        actual_count = cache_data["table_count"]
        n = len(paragraphs) or 1
        return {
            "detection_method": "hwpx_dom_table_count_only",
            "detection_available": True,
            "table_detected": actual_count > 0,
            "total_table_count": actual_count,
            "single_cell_table_count": None,
            "multi_cell_table_count": None,
            "content_table_candidate_count": None,
            "content_table_candidate_ratio": None,
            "total_table_ratio": round(actual_count / n, 3),
            "accuracy_caveat": "total count only; no rows/cols breakdown available; may include decorative/layout tables",
        }

    # Fallback: description keyword
    TABLE_KEYWORDS = {"표", "테이블", "table", "현황표", "실적표", "계획표", "서식"}
    has_descriptions = any(p.get("description") for p in paragraphs)
    if not has_descriptions:
        return {
            "detection_method": None,
            "detection_available": False,
            "table_detected": False,
            "table_count": None,
            "table_ratio": None,
            "accuracy_caveat": "no descriptions available",
        }

    matched = [
        p.get("idx")
        for p in paragraphs
        if any(kw in (p.get("description") or "") for kw in TABLE_KEYWORDS)
    ]
    n = len(paragraphs) or 1
    return {
        "detection_method": "description_keyword_heuristic",
        "detection_available": True,
        "table_detected": len(matched) > 0,
        "table_count": len(matched),
        "table_ratio": round(len(matched) / n, 3),
        "accuracy_caveat": "keyword-based, may over/undercount",
    }


def _detect_structural_signals(paragraphs: list[dict], chapter_title_level: int) -> dict:
    """structural signals 감지."""
    HEADER_KW = {"제목", "일자", "날짜", "기관", "기관명", "작성자", "보안"}
    APPROVAL_KW = {"결재", "승인", "배포", "직위", "담당"}
    ATTACH_KW = {"붙임", "첨부", "별첨", "별표"}
    SLOT_KW = {"고정", "slot", "빈칸", "기입"}

    # semantic_tag observation
    per_type_sem = None
    dominant_tags = []
    # gather from paragraphs descriptions
    tag_counts = {}
    for p in paragraphs:
        desc = p.get("description") or ""
        # simple heuristic tag from description keywords
        if any(kw in desc for kw in {"제목", "표지", "단원", "장 시작"}):
            t = "title_like"
        elif any(kw in desc for kw in {"표", "테이블", "현황"}):
            t = "table_related"
        elif any(kw in desc for kw in {"본문", "설명", "서술", "내용"}):
            t = "body_paragraph"
        elif any(kw in desc for kw in {"보충", "예시", "각주", "참고"}):
            t = "supporting_note"
        else:
            t = "other"
        tag_counts[t] = tag_counts.get(t, 0) + 1
    # top 3
    dominant_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:3]
    dominant_tags = [t for t, _ in dominant_tags]

    # header region detection (early paragraphs before first chapter body)
    has_header = False
    header_descs = []
    for p in paragraphs[:10]:
        desc = p.get("description") or ""
        if any(kw in desc for kw in HEADER_KW):
            has_header = True
            if len(header_descs) < 5:
                header_descs.append(desc[:60])

    # approval line
    has_approval = any(
        any(kw in (p.get("description") or "") for kw in APPROVAL_KW)
        for p in paragraphs[:10]
    )

    # attachment
    has_attachment = any(
        any(kw in (p.get("description") or "") for kw in ATTACH_KW)
        for p in paragraphs
    )

    # slot-like
    has_slot = any(
        any(kw in (p.get("description") or "") for kw in SLOT_KW)
        for p in paragraphs
    )

    # title role count
    title_roles = set()
    for p in paragraphs:
        if p.get("level", 0) == chapter_title_level:
            r = p.get("role", "")
            if r:
                title_roles.add(r)

    return {
        "has_header_region": has_header,
        "has_approval_line": has_approval,
        "has_attachment_region": has_attachment,
        "has_slot_like_region": has_slot,
        "semantic_tag_observation": {
            "source": "description_keyword_heuristic",
            "used_for_unit_decision": False,
            "dominant_tags": dominant_tags,
        },
        "title_role_count": len(title_roles),
        "header_paragraph_descriptions": header_descs,
    }


def _summarize_marker_families(structure: dict) -> dict:
    """marker policy에서 family 분포 집계."""
    policies = structure.get("marker_policy_1f", {})
    if not policies:
        # fallback: role_text_types 등에서 추론 불가 -> empty
        return {}
    family_counts = {}
    for role, info in policies.items():
        if isinstance(info, dict):
            ptype = info.get("policy_type", "unknown")
        else:
            ptype = "unknown"
        # group into families
        if ptype in ("fixed_char",):
            fam = "fixed_char"
        elif ptype in ("arabic_sequence", "roman_sequence", "circled_sequence",
                       "circled_num_sequence", "num_paren_sequence", "korean_sequence"):
            fam = "sequence"
        elif ptype in ("star_depth",):
            fam = "star_depth"
        elif ptype in ("no_marker",):
            fam = "no_marker"
        else:
            fam = "other"
        family_counts[fam] = family_counts.get(fam, 0) + 1
    return family_counts


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AI Judgment Layer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


TEMPLATE_UNIT_OBSERVATION_PROMPT = """당신은 구조화된 template facts를 해석하는 schema-constrained observer입니다.

아래에 양식(template)의 구조 분석 결과가 주어집니다.
이 양식의 content를 채울 때 어떤 target unit이 후보로 관측되는지 평가하세요.

## target unit types

- chapter: 깊은 role hierarchy가 있어 chapter tree 단위 생성이 자연스러운 구조
- shallow_block: 얕은 구조로 bullet/block 단위 채움이 자연스러운 구조
- table: 표(table) 셀 채우기가 주요 작업인 구조
- slot: 고정 위치(제목, 날짜, 기관명 등)를 채우는 것이 의미 있는 구조
- section: HWPX 물리 section이 content boundary로도 의미 있는 구조 (단순 레이아웃 구분이면 평가 불필요)
- attachment: 붙임/첨부 영역이 별도 채움 단위인 구조

## observed_role 정의

- strong_candidate: 이 unit에 대한 structural evidence가 강함 (다음 단계에서 우선 검토 대상)
- moderate_candidate: 부분적 evidence 있음 (조건부 검토 대상)
- background_signal: 약한 signal이 있으나 dominant하지 않음 (참고용)
- not_indicated: 현재 features 기준으로 근거가 약함

이 값은 독립적 signal 강도입니다. 두 unit이 모두 strong_candidate일 수 있습니다.
이 값은 planning 확정값이 아닙니다. 후속 단계에서 독립적 planning이 별도 수행됩니다.

## 핵심 규칙

1. **제공된 features에서 직접 관찰 가능한 근거만 사용하세요.**
2. **일반 문서 지식으로 추측하지 마세요.**
   - 금지: "업무계획서는 보통 chapter 단위가 적합하다"
   - 금지: "보고서라서 shallow이다"
   - 금지: 문서명/기관명/정책명 기반 판단
3. **features의 구조 수치/분포/signal만 evidence로 사용하세요.**
4. **특정 숫자를 threshold rule처럼 사용하지 마세요.**
   - 금지: "body count가 40 이상이므로 chapter가 적합하다"
   - 허용: "body 70개로 단일 생성에 과하며, 깊은 hierarchy와 함께 분할 단위가 자연스러워 보인다"
5. **관련 있는 unit type만 평가하세요.** 6개 전부 의무 평가 아님.
   - features에서 근거를 찾을 수 있는 unit만 unit_observations에 포함
   - 모든 unit을 채우지 마세요
6. **not_assessed_units는 선택적입니다.** 아래 경우에만 기록:
   - features에 해당 unit 관련 signal이 있었지만 unit_observations에 포함하지 않은 경우
   - 누락으로 오해될 수 있어 이유를 남길 필요가 있는 경우
   - 모든 미평가 unit을 나열하지 마세요
7. **not_indicated는 의무 출력이 아닙니다.** 기존 pipeline과 충돌하거나, 누락되면 오해될 수 있는 unit에 대해서만 출력하세요.
8. **section은 content boundary로 의미 있을 때만 평가하세요.**
   - 단순 multi-section(페이지 나눔)이면 section unit으로 보지 않음
   - section 내 content가 독립적이고 section 단위 채움이 자연스러울 때만 평가
9. **evidence_fields에는 features의 필드명을 사용하세요.** (dot notation 허용)
10. **assessment_summary는 다음 단계 AI가 읽을 수 있게 작성하세요.** 2~3문장, evidence 기반, 확정 표현 금지.
11. **semantic_tag_observation은 보조 참고만 가능합니다.** (heuristic 기반이므로 단독 근거 금지)
12. **table unit 판단 시 total_table_count만으로 strong을 부여하지 마세요.**
    - total_table_count에는 1x1 텍스트 박스, 제목 박스 등 layout/decorative table이 포함됩니다.
    - content_table_candidate_count (multi-cell tables)를 우선 참고하세요.
    - content_table_candidate_count도 layout table을 포함할 수 있습니다 (accuracy_caveat 참조).
    - table unit을 strong으로 보려면 content_table_candidate_count와 ratio가 의미 있어야 합니다.
    - single_cell_table_count가 total의 대부분이면 실제 content table은 적을 가능성이 높습니다.

## 출력 형식

반드시 아래 JSON만 출력하세요.

```json
{
  "unit_observations": [
    {
      "unit_type": "unit type name",
      "observed_role": "strong_candidate | moderate_candidate | background_signal | not_indicated",
      "assessment_summary": "2~3문장 관측 요약 (다음 단계 AI가 읽을 context)",
      "evidence_fields": ["features 필드명"],
      "evidence_values": "핵심 값 요약",
      "risks": ["이 unit 사용 시 위험 요소 (있으면)"],
      "counter_signals": ["이 unit에 불리한 signal (있으면)"]
    }
  ],
  "not_assessed_units": [
    {
      "unit_type": "unit type name",
      "reason": "미평가 이유 (signal 있었지만 제외한 경우에만)"
    }
  ],
  "cross_unit_concerns": ["여러 unit에 걸친 관측/우려 (있으면)"],
  "ambiguity_flags": ["판단이 모호한 지점 (있으면)"]
}
```
"""


def build_template_unit_prompt(features: dict) -> list[dict]:
    """AI prompt 구성."""
    return [
        {"role": "system", "content": TEMPLATE_UNIT_OBSERVATION_PROMPT},
        {"role": "user", "content": json.dumps(features, ensure_ascii=False, indent=2)},
    ]


def parse_template_unit_observation_from_llm(raw_output: str) -> dict | None:
    """
    AI raw output -> structured dict.
    Returns None if parse fails or required fields missing.
    """
    text = _extract_json_text(raw_output)
    if not text:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    # Required: unit_observations as non-empty list
    units = data.get("unit_observations")
    if not isinstance(units, list) or len(units) == 0:
        return None

    # Each unit must have unit_type, observed_role, evidence_fields, assessment_summary
    for u in units:
        if not isinstance(u, dict):
            return None
        if not u.get("unit_type") or not u.get("observed_role"):
            return None
        if not u.get("evidence_fields") or not isinstance(u["evidence_fields"], list):
            return None
        if not u.get("assessment_summary"):
            return None

    # Ensure optional fields exist with defaults
    data.setdefault("not_assessed_units", [])
    data.setdefault("cross_unit_concerns", [])
    data.setdefault("ambiguity_flags", [])

    return data


def _extract_json_text(raw: str) -> str | None:
    """Extract JSON from AI output (code fence strip, brace extraction)."""
    if not raw:
        return None
    # Strip code fences
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
    # Find outermost braces
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        return raw[first:last + 1]
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Derivation & Validation Layer (code-only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def derive_mode_label(unit_observations: list[dict]) -> dict:
    """
    unit_observations에서 기계적으로 mode label을 도출.

    IMPORTANT: 이 label은 debug convenience summary이다.
    - generation/validation/assemble route를 직접 결정하지 않는다.
    - pipeline 분기에 사용하지 않는다 (if label == "..." 금지).
    - 12.2에서 독립적 target_unit planning을 수행한다.
    - 사람이 빠르게 양식 성격을 파악하기 위한 요약일 뿐이다.
    """
    strong = [u for u in unit_observations if u.get("observed_role") == "strong_candidate"]
    moderate = [u for u in unit_observations if u.get("observed_role") == "moderate_candidate"]
    strong_types = {u["unit_type"] for u in strong}
    moderate_types = {u["unit_type"] for u in moderate}

    if not strong and not moderate:
        label = "unknown"
        rule = "no_strong_or_moderate_candidates"
        derived_from = []
    elif strong_types == {"chapter"}:
        label = "chapter_generation"
        rule = "chapter_only_strong"
        derived_from = ["chapter"]
    elif "chapter" in strong_types and strong_types <= {"chapter", "slot"}:
        label = "chapter_generation"
        rule = "chapter_strong_with_slot_only"
        derived_from = sorted(strong_types)
    elif "chapter" not in strong_types and strong_types & {"shallow_block", "table", "slot", "attachment"}:
        label = "shallow_report"
        rule = "non_chapter_units_strong"
        derived_from = sorted(strong_types)
    elif "chapter" in strong_types and (strong_types - {"chapter", "slot"}):
        label = "mixed"
        rule = "chapter_and_other_content_units_both_strong"
        derived_from = sorted(strong_types)
    elif not strong and moderate:
        if "chapter" in moderate_types and not (moderate_types - {"chapter", "slot"}):
            label = "chapter_generation"
            rule = "chapter_moderate_only"
            derived_from = sorted(moderate_types)
        elif "chapter" not in moderate_types:
            label = "shallow_report"
            rule = "non_chapter_moderate"
            derived_from = sorted(moderate_types)
        else:
            label = "mixed"
            rule = "multiple_moderate_including_chapter"
            derived_from = sorted(moderate_types)
    else:
        label = "unknown"
        rule = "unclassifiable_combination"
        derived_from = sorted(strong_types | moderate_types)

    # Confidence
    if len(strong) >= 1 and len(strong) <= 2:
        confidence = "high"
    elif len(strong) >= 3:
        confidence = "medium"
    elif moderate:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "label": label,
        "is_policy_switch": False,
        "derivation_rule": rule,
        "derived_from_units": derived_from,
        "confidence_level": confidence,
        "note": (
            "debug convenience summary; does NOT determine "
            "generation/validation/assemble route; "
            "12.2 performs independent planning"
        ),
    }


def validate_unit_observation(features: dict, observation: dict) -> dict:
    """
    AI observation에 대한 code-based sanity check.
    observation을 대체하지 않음 — blocker/warning만 생성.
    """
    blockers = []
    warnings = []
    confidence_downgrade = False

    units = observation.get("unit_observations", [])
    valid_top_keys = set(features.keys())

    # ── Blockers ──

    if not units:
        blockers.append("no_unit_observations")

    for u in units:
        if u.get("observed_role") in ("strong_candidate", "moderate_candidate"):
            fields = u.get("evidence_fields", [])
            if fields:
                hallucinated = [f for f in fields if f.split(".")[0] not in valid_top_keys]
                if len(hallucinated) == len(fields):
                    blockers.append(f"fully_hallucinated_evidence: {u.get('unit_type')}")

    # ── Warnings ──

    for u in units:
        for f in u.get("evidence_fields", []):
            if f.split(".")[0] not in valid_top_keys:
                warnings.append(f"hallucinated_field: {f} in {u.get('unit_type')}")

    for u in units:
        if u.get("observed_role") == "strong_candidate" and not u.get("assessment_summary"):
            warnings.append(f"strong_without_summary: {u.get('unit_type')}")

    for u in units:
        if u.get("observed_role") == "strong_candidate":
            fields = u.get("evidence_fields", [])
            hallucinated = [f for f in fields if f.split(".")[0] not in valid_top_keys]
            if hallucinated:
                confidence_downgrade = True
                warnings.append(f"strong_has_hallucinated_evidence: {u.get('unit_type')}")

    for u in units:
        if u.get("unit_type") not in VALID_UNIT_TYPES:
            warnings.append(f"unknown_unit_type: {u.get('unit_type')}")

    for u in units:
        if u.get("observed_role") not in VALID_OBSERVED_ROLES:
            warnings.append(f"unknown_observed_role: {u.get('observed_role')}")

    for u in units:
        if u.get("observed_role") in ("strong_candidate", "moderate_candidate"):
            if not u.get("evidence_values"):
                warnings.append(f"empty_evidence_values: {u.get('unit_type')}")

    return {
        "valid": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "confidence_downgrade": confidence_downgrade,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pipeline Fit Layer (code-only, per-run)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def compute_pipeline_fit(
    derived_label: dict,
    unit_observations: list[dict],
    pipeline_context: dict | None,
) -> dict:
    """
    derived label과 unit_observations를 기반으로 기존 2a/2b pipeline fit 진단.
    code-only, 매 run 실행.
    """
    if not pipeline_context:
        return {"has_2a_data": False}

    label = derived_label.get("label", "unknown")
    conflicts = []
    ch_count = pipeline_context.get("chapter_count", 0)
    concentration = pipeline_context.get("source_concentration_ratio", 0)
    underfill = pipeline_context.get("underfill_candidates", [])

    # unit observations에서 추가 정보
    non_chapter_strong = [
        u for u in unit_observations
        if u.get("unit_type") not in ("chapter", "slot")
        and u.get("observed_role") == "strong_candidate"
    ]

    if label == "unknown":
        conflicts.append({
            "type": "label_undetermined",
            "severity": "watch",
            "detail": "derived label is unknown; legacy 2a fit cannot be confidently assessed",
        })

    elif label == "shallow_report":
        if ch_count >= 3:
            conflicts.append({
                "type": "shallow_template_multi_chapter_2a",
                "severity": "watch",
                "detail": (
                    f"template is shallow_report but 2a produced {ch_count} chapters "
                    "— legacy pipeline likely mismatched"
                ),
            })
        if concentration and concentration > 0.8:
            conflicts.append({
                "type": "shallow_template_source_imbalanced",
                "severity": "watch",
                "detail": (
                    f"source_concentration={concentration:.3f} "
                    "— source split may not be meaningful for shallow template"
                ),
            })

    elif label == "mixed":
        if non_chapter_strong:
            unit_names = [u["unit_type"] for u in non_chapter_strong]
            conflicts.append({
                "type": "mixed_template_non_chapter_units_ignored",
                "severity": "watch",
                "detail": (
                    f"non-chapter strong candidates {unit_names} exist but "
                    "legacy 2a/2b pipeline only handles chapter-based generation"
                ),
            })
        if concentration and concentration > 0.7:
            conflicts.append({
                "type": "mixed_template_source_allocation_risk",
                "severity": "watch",
                "detail": (
                    f"source_concentration={concentration:.3f} "
                    "— mixed template may need per-unit source allocation"
                ),
            })

    elif label == "chapter_generation":
        if ch_count == 1:
            conflicts.append({
                "type": "chapter_template_single_chapter_2a",
                "severity": "watch",
                "detail": "template supports chapter generation but 2a produced only 1 chapter",
            })
        if len(underfill) >= 2:
            conflicts.append({
                "type": "chapter_template_underfill",
                "severity": "watch",
                "detail": f"{len(underfill)} underfill chapters — source allocation issue",
            })

    return {
        "has_2a_data": True,
        "observed_2a_chapter_count": ch_count,
        "source_concentration_ratio": concentration,
        "underfill_candidates": underfill,
        "overfill_candidates": pipeline_context.get("overfill_candidates", []),
        "primary_conflict": conflicts[0]["type"] if conflicts else "none",
        "conflict_details": conflicts,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def assemble_observation_output(
    features: dict,
    ai_observation: dict | None,
    validation_result: dict,
    derived_label: dict,
    pipeline_fit: dict,
    cache_status: dict,
    ai_call_info: dict,
    fallback_reason: str | None = None,
) -> dict:
    """최종 debug output dict 구성."""
    result = {
        "schema_version": 1,
        "observer_version": CURRENT_OBSERVER_VERSION,
        "debug_only": True,
        "observation_scope": "template_unit_observation",
        "cache_status": cache_status,
        "features": features,
        "ai_observation": ai_observation or {
            "unit_observations": [],
            "not_assessed_units": [{"unit_type": "all", "reason": fallback_reason or "unknown_error"}],
            "cross_unit_concerns": [],
            "ambiguity_flags": [],
        },
        "ai_call_info": ai_call_info,
        "validation_result": validation_result,
        "derived_mode_label": derived_label,
        "pipeline_fit_diagnostics": pipeline_fit,
    }
    if fallback_reason:
        result["fallback_reason"] = fallback_reason
    return result


def build_cache_payload(
    ai_observation: dict,
    derived_label: dict,
    validation_result: dict,
    features: dict,
) -> dict:
    """cache에 저장할 observation payload."""
    return {
        "observer_version": CURRENT_OBSERVER_VERSION,
        "unit_observations": ai_observation.get("unit_observations", []),
        "not_assessed_units": ai_observation.get("not_assessed_units", []),
        "cross_unit_concerns": ai_observation.get("cross_unit_concerns", []),
        "ambiguity_flags": ai_observation.get("ambiguity_flags", []),
        "derived_mode_label": derived_label,
        "validation_result": validation_result,
        "features_snapshot": features,
        "features_snapshot_scope": "debug/repro only; not used by generation or assemble",
    }


def is_cache_valid(cached_observation: dict | None) -> bool:
    """cache HIT 유효성 검증."""
    if not cached_observation:
        return False
    if cached_observation.get("observer_version") != CURRENT_OBSERVER_VERSION:
        return False
    if not cached_observation.get("validation_result", {}).get("valid", False):
        return False
    return True
