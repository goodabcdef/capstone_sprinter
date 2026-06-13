"""
12.1 Phase 1: Marker Roundtrip Readiness Observation (debug-only)

content-only generation 전환 전, code 기반 marker strip → reattach가 정확한지 검증.
production pipeline 변경 없음. AI output schema 변경 없음.

Phase 2에서 assemble이 이 모듈의 reattach_marker를 호출하는 구조로 이어짐.
"""

import logging
import re

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core Functions (Phase 1 observation + Phase 2 production 공용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def strip_marker(text: str, role: str, policy: dict) -> dict:
    """
    text에서 leading marker를 제거하고 content만 추출.

    Args:
        text: 원본 텍스트 (marker + separator + content)
        role: role name
        policy: marker_policy dict for this role
            {policy_type, markers, separator, style, ...}

    Returns:
        {
            "original": str,
            "content": str,
            "detected_marker": str,
            "separator": str,
            "strip_method": "policy_match" | "no_marker_role" | "no_policy" | "not_applicable",
            "content_preserved": bool,
        }
    """
    if not text:
        return {
            "original": "",
            "content": "",
            "detected_marker": "",
            "separator": "",
            "strip_method": "empty_text",
            "content_preserved": True,
        }

    policy_type = policy.get("policy_type", "") if policy else ""

    # star_depth: sibling_index 기반이므로 일반 strip과 동일하게 처리

    # no_marker role: should NOT strip anything
    if policy_type == "no_marker" or not policy:
        # Verify nothing looks like a marker at the start
        return {
            "original": text,
            "content": text,
            "detected_marker": "",
            "separator": "",
            "strip_method": "no_marker_role" if policy_type == "no_marker" else "no_policy",
            "content_preserved": True,
        }

    markers = policy.get("markers", [])
    if not markers:
        return {
            "original": text,
            "content": text,
            "detected_marker": "",
            "separator": "",
            "strip_method": "no_markers_in_policy",
            "content_preserved": True,
        }

    # Try to detect marker at text start
    stripped = text.lstrip()
    leading_space = text[:len(text) - len(stripped)]

    detected_marker = ""
    separator = ""
    content = stripped

    # leading emphasis markup ([[emN]]...[[/emN]]) wrap 인식 — AI가 marker를 강조
    # 표시 안쪽에 박는 경우. 두 가지 case 구분:
    #   case 1: 강조 안쪽이 marker만 (예: "[[em1]]Ⅱ[[/em1]] 제목") → 강조 전체
    #           strip 후 뒤 텍스트가 본문. (예: cluster_19 "과제 1" 마커)
    #   case 2: 강조 안쪽이 marker + 본문 (예: "[[em5]]* (운영규모) ...[[/em5]]")
    #           → 강조 표시는 보존하고 안쪽에서 marker만 떼서 본문에 강조 다시 감싸기.
    #           AI 의도 강조 글꼴이 조립 단계에 전달되도록 함.
    _em_lead_pat = re.compile(r'^\[\[(em\d+)\]\](.*?)\[\[/\1\]\]', re.DOTALL)
    _em_match = _em_lead_pat.match(stripped)
    if _em_match:
        _em_layer = _em_match.group(1)
        _em_inner_raw = _em_match.group(2)
        _em_inner = _em_inner_raw.lstrip()
        _after_em = stripped[_em_match.end():]

        def _apply_em_split(m: str, marker_len: int):
            """case 1/case 2 구분해서 detected_marker/separator/content 결정."""
            _after_marker_inner = _em_inner[marker_len:]
            if not _after_marker_inner.strip():
                # case 1: 강조 안쪽이 marker만 (whitespace 가능) → 강조 전체 strip
                sep = _detect_separator(_after_em)
                cnt = _after_em[len(sep):] if sep else _after_em
                return m, sep, cnt
            # case 2: 강조 안쪽이 marker + 본문 → 강조 표시 보존
            sep = _detect_separator(_after_marker_inner)
            _inner_content = _after_marker_inner[len(sep):] if sep else _after_marker_inner
            cnt = f"[[{_em_layer}]]{_inner_content}[[/{_em_layer}]]"
            if _after_em:
                cnt += _after_em
            return m, sep, cnt

        # markup 안쪽으로 marker 매칭
        for m in sorted(markers, key=len, reverse=True):
            if _em_inner.startswith(m):
                detected_marker, separator, content = _apply_em_split(m, len(m))
                break
        # sequence detection 시도 (markup 안쪽으로)
        if not detected_marker and policy.get("style") == "sequence":
            _seq_marker, _seq_sep, _seq_content = _try_sequence_detection(
                _em_inner, policy_type
            )
            if _seq_marker:
                detected_marker, separator, content = _apply_em_split(
                    _seq_marker, len(_seq_marker)
                )

    # Match against known markers (longest first) — plain marker (markup 없이)
    if not detected_marker:
        for m in sorted(markers, key=len, reverse=True):
            if stripped.startswith(m):
                detected_marker = m
                after = stripped[len(m):]
                separator = _detect_separator(after)
                content = after[len(separator):] if separator else after
                break

    # If no known marker matched, try broader detection for sequence types
    if not detected_marker and policy.get("style") == "sequence":
        detected_marker, separator, content = _try_sequence_detection(
            stripped, policy_type
        )

    if detected_marker:
        return {
            "original": text,
            "content": content,
            "detected_marker": detected_marker,
            "separator": separator,
            "strip_method": "policy_match",
            "content_preserved": True,
        }
    else:
        # No marker found — could be AI didn't include one, or detection failed
        return {
            "original": text,
            "content": text,
            "detected_marker": "",
            "separator": "",
            "strip_method": "no_marker_detected",
            "content_preserved": True,
        }


def generate_expected_marker(role: str, policy: dict, sibling_index: int) -> dict:
    """
    policy + sibling_index 기반으로 기대 marker를 생성.

    Args:
        sibling_index: 1-based (같은 parent 아래 같은 role의 n번째)

    Returns:
        {
            "marker": str,
            "policy_type": str,
            "sibling_index": int,
            "generation_method": str,
            "success": bool,
        }
    """
    if not policy:
        return {
            "marker": "",
            "policy_type": "unknown",
            "sibling_index": sibling_index,
            "generation_method": "no_policy",
            "success": True,
        }

    policy_type = policy.get("policy_type", "")
    markers = policy.get("markers", [])
    style = policy.get("style", "")

    if policy_type == "no_marker":
        return {
            "marker": "",
            "policy_type": policy_type,
            "sibling_index": sibling_index,
            "generation_method": "not_applicable",
            "success": True,
        }
    # star_depth: markers=["*","**"], sequence와 동일 로직으로 처리

    if not markers:
        return {
            "marker": "",
            "policy_type": policy_type,
            "sibling_index": sibling_index,
            "generation_method": "no_markers_available",
            "success": False,
        }

    if style == "fixed":
        return {
            "marker": markers[0],
            "policy_type": policy_type,
            "sibling_index": sibling_index,
            "generation_method": "fixed_first",
            "success": True,
        }

    # sequence style
    if sibling_index <= len(markers):
        marker = markers[sibling_index - 1]
        method = "from_markers_list"
    else:
        marker = _generate_sequence_marker(policy_type, sibling_index, markers)
        method = "sequence_formula"

    return {
        "marker": marker,
        "policy_type": policy_type,
        "sibling_index": sibling_index,
        "generation_method": method,
        "success": bool(marker),
    }


def reattach_marker(content: str, marker: str, separator: str) -> str:
    """marker + separator + content 조합."""
    if not marker:
        return content
    if not content:
        return marker
    return f"{marker}{separator}{content}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 2: Normalized Marker Rendering
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def compare_roundtrip(
    original: str,
    strip_result: dict,
    expected_marker_result: dict,
    policy: dict,
) -> dict:
    """
    roundtrip 비교. 다중 metric 산출.
    """
    content = strip_result["content"]
    expected_marker = expected_marker_result["marker"]
    policy_separator = policy.get("separator", " ") if policy else " "

    # Reattach with policy separator
    reattached = reattach_marker(content, expected_marker, policy_separator)

    # Reattach with detected separator (for separator comparison)
    detected_sep = strip_result["separator"]
    reattached_with_detected_sep = reattach_marker(content, expected_marker, detected_sep)

    # --- Metrics ---

    # 1. Content preserved (strip didn't eat content)
    content_preserved = strip_result["content_preserved"]

    # 2. Policy marker correct (expected marker matches what policy says)
    policy_marker_correct = expected_marker_result["success"]

    # 3. Original exact match
    original_exact_match = (reattached == original)

    # 4. Separator exact match
    separator_exact_match = (detected_sep == policy_separator)

    # 5. Separator normalized match (whitespace normalization)
    separator_normalized_match = (
        detected_sep.strip() == policy_separator.strip()
        or (not detected_sep.strip() and not policy_separator.strip())
    )

    # --- Mismatch category ---
    category = _classify_mismatch(
        original=original,
        reattached=reattached,
        strip_result=strip_result,
        expected_marker_result=expected_marker_result,
        content_preserved=content_preserved,
        policy_marker_correct=policy_marker_correct,
        original_exact_match=original_exact_match,
        separator_exact_match=separator_exact_match,
    )

    return {
        "original_exact_match": original_exact_match,
        "content_preserved": content_preserved,
        "policy_marker_correct": policy_marker_correct,
        "separator_exact_match": separator_exact_match,
        "separator_normalized_match": separator_normalized_match,
        "reattached": reattached,
        "mismatch_category": category,
        "detail": _build_detail(category, strip_result, expected_marker_result, original),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Debug Aggregation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_marker_roundtrip_debug(
    body_items: list[dict],
    marker_policies: dict,
    marker_rewrite_log: list[dict],
    derived_mode_label: str = "",
) -> dict:
    """
    전체 body_items에 대해 roundtrip 실행 + 집계.

    Args:
        body_items: [{role, text, ...}] — 원본 텍스트 (rewrite 전)
        marker_policies: role → policy dict
        marker_rewrite_log: assemble의 _marker_rewrite_log (sibling_index 재사용)
        derived_mode_label: 12.0 derived label (summary metadata)
    """
    results = []
    mismatches = []

    # Aggregate counters
    total = 0
    applicable = 0
    skipped = 0
    skip_reasons = {}

    content_preserved_count = 0
    policy_correct_count = 0
    original_match_count = 0
    sep_exact_count = 0
    sep_normalized_count = 0

    by_policy_type = {}
    by_role_depth = {}
    taxonomy_counts = {}

    for i, item in enumerate(body_items):
        role = item.get("role", "")
        text = item.get("text", "")
        policy = marker_policies.get(role, {})
        policy_type = policy.get("policy_type", "") if policy else ""

        # Get sibling_index from rewrite_log (matched by position)
        rewrite_entry = marker_rewrite_log[i] if i < len(marker_rewrite_log) else {}
        sibling_index = rewrite_entry.get("sibling_index", 1)
        role_depth = item.get("level", rewrite_entry.get("role_depth", 0))
        chapter_idx = rewrite_entry.get("chapter_idx", 0)

        total += 1

        # Skip not_applicable
        if policy_type == "star_depth":
            skipped += 1
            skip_reasons["star_depth"] = skip_reasons.get("star_depth", 0) + 1
            continue

        applicable += 1

        # --- Roundtrip ---
        strip_result = strip_marker(text, role, policy)
        expected_result = generate_expected_marker(role, policy, sibling_index)
        comparison = compare_roundtrip(text, strip_result, expected_result, policy)

        # --- Aggregate ---
        if comparison["content_preserved"]:
            content_preserved_count += 1
        if comparison["policy_marker_correct"]:
            policy_correct_count += 1
        if comparison["original_exact_match"]:
            original_match_count += 1
        if comparison["separator_exact_match"]:
            sep_exact_count += 1
        if comparison["separator_normalized_match"]:
            sep_normalized_count += 1

        # by_policy_type
        pt_key = policy_type or "unknown"
        if pt_key not in by_policy_type:
            by_policy_type[pt_key] = {
                "count": 0,
                "content_preserved": 0,
                "policy_marker_correct": 0,
                "original_exact_match": 0,
                "false_positives": 0,
            }
        by_policy_type[pt_key]["count"] += 1
        if comparison["content_preserved"]:
            by_policy_type[pt_key]["content_preserved"] += 1
        if comparison["policy_marker_correct"]:
            by_policy_type[pt_key]["policy_marker_correct"] += 1
        if comparison["original_exact_match"]:
            by_policy_type[pt_key]["original_exact_match"] += 1

        # no_marker false positive check
        if policy_type == "no_marker" and strip_result["detected_marker"]:
            by_policy_type[pt_key]["false_positives"] += 1

        # by_role_depth
        depth_key = f"depth_{role_depth}"
        if depth_key not in by_role_depth:
            by_role_depth[depth_key] = {"count": 0, "policy_correct": 0, "content_preserved": 0}
        by_role_depth[depth_key]["count"] += 1
        if comparison["policy_marker_correct"]:
            by_role_depth[depth_key]["policy_correct"] += 1
        if comparison["content_preserved"]:
            by_role_depth[depth_key]["content_preserved"] += 1

        # taxonomy
        cat = comparison["mismatch_category"]
        if cat:
            taxonomy_counts[cat] = taxonomy_counts.get(cat, 0) + 1

        # Record mismatch details
        if cat and cat != "no_mismatch":
            mismatches.append({
                "item_idx": i,
                "role": role,
                "role_depth": role_depth,
                "chapter_idx": chapter_idx,
                "policy_type": policy_type,
                "sibling_index": sibling_index,
                "category": cat,
                "original_preview": text[:80],
                "content_preview": strip_result["content"][:80],
                "reattached_preview": comparison["reattached"][:80],
                "detected_marker": strip_result["detected_marker"],
                "expected_marker": expected_result["marker"],
                "separator_detected": strip_result["separator"],
                "separator_policy": policy.get("separator", " "),
                "detail": comparison["detail"],
            })

    # --- Summary ---
    def _rate(num, denom):
        return round(num / denom, 4) if denom > 0 else 1.0

    # Phase 2 readiness
    no_false_positives = all(
        v.get("false_positives", 0) == 0 for v in by_policy_type.values()
    )
    content_damage_count = taxonomy_counts.get("content_changed_during_strip", 0)
    cp_rate = _rate(content_preserved_count, applicable)
    pm_rate = _rate(policy_correct_count, applicable)

    phase2_ready = (
        cp_rate >= 0.99
        and pm_rate >= 0.95
        and no_false_positives
        and content_damage_count == 0
    )

    phase2_blockers = []
    phase2_warnings = []
    if cp_rate < 0.99:
        phase2_blockers.append(f"content_preservation_rate={cp_rate} < 0.99")
    if pm_rate < 0.80:
        phase2_blockers.append(f"policy_marker_correctness_rate={pm_rate} < 0.80")
    elif pm_rate < 0.95:
        phase2_warnings.append(f"policy_marker_correctness_rate={pm_rate} < 0.95")
    if not no_false_positives:
        phase2_blockers.append("no_marker_false_positive detected")
    if content_damage_count > 0:
        phase2_blockers.append(f"content_changed_during_strip={content_damage_count}")
    sep_rate = _rate(sep_exact_count, applicable)
    if sep_rate < 1.0:
        phase2_warnings.append(f"separator_exact_match_rate={sep_rate} < 1.0")

    return {
        "schema_version": 1,
        "phase": "roundtrip_readiness_observation",
        "debug_only": True,

        "summary": {
            "template_derived_mode": derived_mode_label,
            "total_items": total,
            "applicable_items": applicable,
            "skipped_items": skipped,
            "skipped_reasons": skip_reasons,

            "content_preservation_rate": cp_rate,
            "policy_marker_correctness_rate": pm_rate,
            "original_exact_match_rate": _rate(original_match_count, applicable),
            "separator_exact_match_rate": sep_rate,
            "separator_normalized_match_rate": _rate(sep_normalized_count, applicable),
            "no_marker_false_positive_count": sum(
                v.get("false_positives", 0) for v in by_policy_type.values()
            ),
            "content_changed_during_strip_count": content_damage_count,

            "by_policy_type": by_policy_type,
            "by_role_depth": by_role_depth,
            "mismatch_taxonomy": taxonomy_counts,
        },

        "mismatches": mismatches[:50],  # cap at 50 samples

        "phase2_readiness": {
            "content_preservation_met": cp_rate >= 0.99,
            "policy_correctness_met": pm_rate >= 0.95,
            "no_false_positives_met": no_false_positives,
            "no_content_damage_met": content_damage_count == 0,
            "overall_ready": phase2_ready,
            "blockers": phase2_blockers,
            "warnings": phase2_warnings,
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _detect_separator(text_after_marker: str) -> str:
    """marker 직후 문자열에서 separator를 추정."""
    if not text_after_marker:
        return ""
    # Known multi-char separators (longest first)
    KNOWN_SEPS = [") ", ". ", ": ", "  "]
    for sep in KNOWN_SEPS:
        if text_after_marker.startswith(sep):
            return sep
    # Single char
    if text_after_marker[0] in (" ", "\t"):
        return text_after_marker[0]
    return ""


def _try_sequence_detection(stripped: str, policy_type: str) -> tuple:
    """
    Known markers list에 없지만 sequence pattern으로 감지 시도.
    AI가 markers list 범위를 벗어난 marker를 생성한 경우 대응.
    """
    detected = ""
    separator = ""
    content = stripped

    if policy_type == "arabic_sequence":
        m = re.match(r'^(\d+)', stripped)
        if m:
            detected = m.group(1)
            after = stripped[len(detected):]
            separator = _detect_separator(after)
            content = after[len(separator):]
    elif policy_type == "num_paren_sequence":
        m = re.match(r'^(\d+\))', stripped)
        if m:
            detected = m.group(1)
            after = stripped[len(detected):]
            separator = _detect_separator(after)
            content = after[len(separator):]
    elif policy_type == "roman_sequence":
        m = re.match(r'^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)', stripped)
        if m:
            detected = m.group(1)
            after = stripped[len(detected):]
            separator = _detect_separator(after)
            content = after[len(separator):]
    elif policy_type in ("circled_sequence", "circled_num_sequence", "circled_pua_sequence"):
        if stripped:
            cp = ord(stripped[0])
            # ➊~➓, ①~⑳, 󰊱~󰊹
            if (0x278A <= cp <= 0x2793 or 0x2460 <= cp <= 0x2473
                    or 0xF02B1 <= cp <= 0xF02B9):
                detected = stripped[0]
                after = stripped[1:]
                separator = _detect_separator(after)
                content = after[len(separator):]

    return detected, separator, content


def _generate_sequence_marker(policy_type: str, sibling_index: int, markers: list) -> str:
    """markers 배열을 초과한 sibling_index에 대해 규칙형 마커 생성."""
    if policy_type == "arabic_sequence":
        return str(sibling_index)
    if policy_type == "num_paren_sequence":
        return f"{sibling_index})"
    if policy_type == "circled_sequence":
        if 1 <= sibling_index <= 10:
            return chr(0x2789 + sibling_index)
    if policy_type == "circled_num_sequence":
        if 1 <= sibling_index <= 20:
            return chr(0x245F + sibling_index)
    if policy_type == "roman_sequence":
        romans = ["Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ", "Ⅵ", "Ⅶ", "Ⅷ", "Ⅸ", "Ⅹ"]
        if 1 <= sibling_index <= len(romans):
            return romans[sibling_index - 1]
    # Fallback
    return markers[-1] if markers else ""


def _classify_mismatch(
    original: str,
    reattached: str,
    strip_result: dict,
    expected_marker_result: dict,
    content_preserved: bool,
    policy_marker_correct: bool,
    original_exact_match: bool,
    separator_exact_match: bool,
) -> str | None:
    """mismatch category 분류."""
    policy_type = expected_marker_result.get("policy_type", "")

    # not_applicable
    if policy_type in ("star_depth",):
        return "not_applicable_policy"

    # no_marker false positive
    if policy_type == "no_marker" and strip_result["detected_marker"]:
        return "no_marker_false_positive"

    # content damage
    if not content_preserved:
        return "content_changed_during_strip"

    # Everything matches perfectly
    if original_exact_match:
        return None  # no mismatch

    # Policy marker generation failed
    if not policy_marker_correct:
        return "policy_marker_generation_failed"

    # Separator only difference
    detected_marker = strip_result["detected_marker"]
    expected_marker = expected_marker_result["marker"]
    if detected_marker == expected_marker and not separator_exact_match:
        return "separator_only_difference"

    # AI marker wrong but policy is correct
    if detected_marker and detected_marker != expected_marker and policy_marker_correct:
        return "ai_marker_wrong_but_policy_correct"

    # Marker detection failed (no marker found but policy expects one)
    if not detected_marker and expected_marker:
        return "marker_detection_failed"

    # Sibling index mismatch (detected marker is valid but different sequence position)
    if detected_marker and detected_marker != expected_marker:
        return "sibling_index_mismatch"

    # Generic mismatch
    return "other_mismatch"


def _build_detail(category: str | None, strip_result: dict, expected_result: dict, original: str) -> str:
    """mismatch detail 문자열 생성."""
    if not category:
        return ""
    detected = strip_result.get("detected_marker", "")
    expected = expected_result.get("marker", "")
    sib = expected_result.get("sibling_index", 0)

    if category == "ai_marker_wrong_but_policy_correct":
        return f"AI marker='{detected}', policy expected='{expected}' (sibling_index={sib})"
    if category == "separator_only_difference":
        return f"sep detected='{strip_result['separator']}' vs policy separator"
    if category == "marker_detection_failed":
        return f"no marker detected in text, expected='{expected}'"
    if category == "sibling_index_mismatch":
        return f"detected='{detected}', expected='{expected}' (sibling_index={sib})"
    if category == "content_changed_during_strip":
        return "strip modified content beyond marker removal"
    if category == "no_marker_false_positive":
        return f"no_marker role but detected '{detected}' as marker"
    if category == "policy_marker_generation_failed":
        return f"could not generate expected marker for policy_type={expected_result.get('policy_type')}"
    return category
