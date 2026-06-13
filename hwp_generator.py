"""
HWPX 문서 생성 모듈 — AI 명령 JSON 기반 동적 생성

python-hwpx 라이브러리를 사용하여 양식 기반으로 문서를 생성합니다.
"""

import io
import logging
from dataclasses import dataclass, field

from hwpx.document import HwpxDocument
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


def _build_parent_map(root):
    """stdlib ElementTree용 parent map 생성 (lxml getparent() 대체)."""
    return {c: p for p in root.iter() for c in p}




def _doc_to_bytes(doc: HwpxDocument) -> bytes:
    """HwpxDocument → bytes. save(BytesIO) 경유."""
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@dataclass
class HwpxResult:
    """HWPX 생성 결과"""
    data: bytes
    success_count: int = 0
    fail_count: int = 0
    errors: list[str] = field(default_factory=list)


def _process_chapter_objects(
    chapter_objects: list[dict],
    structure: dict | None = None,
    idx_map: dict | None = None,
) -> dict:
    """
    13.7a-A1: chapter_objects를 평탄화하여 assemble path가 필요로 하는
    구조를 만든다.

    13.7d (region-aware placement + title-only with placeholder):
    - chapter title item을 body_items 평탄화에서 제외 (chapter_anchor_items에 별도 저장).
      assembly가 양식 chapter title element를 anchor로 사용 + body items만 anchor 다음에 insert.
      양식 chapter title 중복 방지 + adapted_title text 교체는 별도 stage (marker/run 보존 위해 보류).
    - status="empty" (또는 생성 생략) 처리 (최종 자동작성 출력 정책):
        * empty_preserve_indices에 paragraph_indices[0]만 (chapter title preserve, body는 remove 대상)
        * placeholder body item 1개 추가 (검토 필요 문구)
          placeholder role: paragraph_indices[1]의 양식 role (structure에서 lookup, idx_map 변환)
          fallback: title_item.role + log.warning
        * 특정 양식/section 번호/제목 문자열 기준 X — action/status 기반
    - status="ok": chapter body items만 평탄화. chapter title item은 chapter_anchor_items[ci]에만.
    - status="fail": skip.

    chapter_objects가 들어오면 _chapter_title_roles는 1d structure가 아니라
    chapter object의 title_item.role union으로 구성한다 (1d 의존 제거).

    Args:
        chapter_objects: chapter object list (build_chapter_object 결과)
        structure: 양식 1a 분석 structure (placeholder role lookup용)
        idx_map: AI idx → real idx (양식 truncate된 경우 변환)

    Returns:
        {
          "body_items": list[dict],            # chapter body items만 평탄화 (title 제외)
          "node_lookup": dict[int, dict],
          "chapter_idx_lookup": dict[int, int],
          "chapter_title_roles": set[str],
          "chapter_node_maps": list[dict[int, dict]],
          "empty_preserve_indices": set[int],
          "chapter_anchor_items": dict[int, dict],   # 13.7d: ci → title_item (anchor element용)
          "adapted_title_deferred": list[dict],      # 13.7d: adapted_title 미적용 log
          "rewrite_alignment": {...},
          "tree_available": bool,
          "invariant_violations": list[dict],
        }
    """
    from open_webui.utils.hwpx_analyzer import (
        assert_chapter_object_invariants,
    )

    body_items: list[dict] = []
    node_lookup: dict[int, dict] = {}
    chapter_idx_lookup: dict[int, int] = {}
    chapter_title_roles: set[str] = set()
    chapter_node_maps: list[dict[int, dict]] = []
    empty_preserve_indices: set[int] = set()
    invariant_violations: list[dict] = []
    per_chapter: list[dict] = []
    chapter_anchor_items: dict[int, dict] = {}  # 13.7d
    adapted_title_deferred: list[dict] = []      # 13.7d

    # 13.7d: paragraph idx → role lookup (placeholder role 결정용)
    _idx_to_role: dict = {}
    if structure:
        for _p in structure.get("paragraphs", []):
            _pidx = _p.get("idx")
            if _pidx is not None:
                _idx_to_role[_pidx] = _p.get("role", "")

    fail_count = 0
    ok_count = 0
    empty_count = 0

    for ci, chapter_obj in enumerate(chapter_objects or []):
        status = chapter_obj.get("status", "ok")
        violations = assert_chapter_object_invariants(chapter_obj)
        if violations:
            invariant_violations.append({
                "chapter_idx": ci,
                "source_chapter_idx": chapter_obj.get("source_chapter_idx"),
                "violations": violations,
            })

        paragraph_indices = chapter_obj.get("paragraph_indices") or []
        target_region_id = chapter_obj.get("target_region_id")
        section_id = chapter_obj.get("section_id", 0)
        first_paragraph_idx = chapter_obj.get("first_paragraph_idx")

        if status == "empty":
            empty_count += 1
            # 13.7d: chapter title paragraph만 preserve + body 제거 + placeholder 1문단 삽입
            title_item_empty = chapter_obj.get("title_item") or {}
            if title_item_empty:
                chapter_anchor_items[ci] = title_item_empty
                if title_item_empty.get("role"):
                    chapter_title_roles.add(title_item_empty["role"])

            # body 제거 — empty_preserve_indices에 chapter title (paragraph_indices[0])만
            # 13.7d fix: paragraph_indices는 ai_idx (cache idx). assembly header_indices는
            # doc.paragraphs index 기준이라 idx_map으로 light_xml _idx 변환 필요.
            # 변환 누락 시 양식의 잘못된 paragraph (다른 cache idx의 paragraph)가 preserve됨.
            if paragraph_indices:
                _empty_ai_idx = paragraph_indices[0]
                _empty_real_idx = idx_map.get(_empty_ai_idx, _empty_ai_idx) if idx_map else _empty_ai_idx
                empty_preserve_indices.add(_empty_real_idx)
            _removed_body_count = max(0, len(paragraph_indices) - 1)

            # placeholder role: paragraph_indices[1] (chapter body 첫 paragraph)의 양식 role
            placeholder_role = None
            placeholder_role_source = "none"
            if len(paragraph_indices) > 1:
                _second_ai_idx = paragraph_indices[1]
                _second_real_idx = idx_map.get(_second_ai_idx, _second_ai_idx) if idx_map else _second_ai_idx
                placeholder_role = (
                    _idx_to_role.get(_second_real_idx)
                    or _idx_to_role.get(_second_ai_idx)
                )
                if placeholder_role:
                    placeholder_role_source = "body_role"
            if not placeholder_role:
                # fallback: title role + warning
                placeholder_role = title_item_empty.get("role", "")
                placeholder_role_source = "title_role_fallback"
                log.warning(
                    f"_process_chapter_objects: placeholder role fallback to title role "
                    f"for chapter ci={ci} (no body role available, paragraph_indices_len="
                    f"{len(paragraph_indices)})"
                )

            _placeholder_text = (
                "※ [검토 필요] 입력 자료에서 충분한 근거를 찾지 못해 "
                "본문 생성을 생략했습니다."
            )
            bi = len(body_items)
            body_items.append({
                "role": placeholder_role,
                "text": _placeholder_text,
            })
            chapter_idx_lookup[bi] = ci

            # 13.7d debug: preserve_reason, placeholder_inserted, removed_body_count
            _ch_debug = chapter_obj.get("_debug") or {}
            _ad = _ch_debug.get("adaptation_decision") or {}
            per_chapter.append({
                "chapter_idx": ci,
                "source_chapter_idx": chapter_obj.get("source_chapter_idx"),
                "target_region_id": target_region_id,
                "section_id": section_id,
                "first_paragraph_idx": first_paragraph_idx,
                "status": "empty",
                "title_aligned": None,
                "body_aligned": None,
                "body_items_count": 1,
                "body_nodes_count": 0,
                "invariant_violations": violations,
                "placeholder_inserted": True,
                "placeholder_role": placeholder_role,
                "placeholder_role_source": placeholder_role_source,
                "removed_body_count": _removed_body_count,
                "preserve_reason": _ad.get("preserve_reason") or _ad.get("action"),
                "preserve_reason_detail": _ad.get("preserve_reason_detail"),
            })
            continue

        if status == "fail":
            fail_count += 1
            per_chapter.append({
                "chapter_idx": ci,
                "source_chapter_idx": chapter_obj.get("source_chapter_idx"),
                "target_region_id": target_region_id,
                "section_id": section_id,
                "first_paragraph_idx": first_paragraph_idx,
                "status": "fail",
                "title_aligned": False,
                "body_aligned": False,
                "body_items_count": 0,
                "body_nodes_count": 0,
                "invariant_violations": violations,
            })
            continue

        # status == "ok"
        ok_count += 1
        title_item = chapter_obj.get("title_item") or {}
        title_node = chapter_obj.get("title_node") or {}
        body_items_ch = chapter_obj.get("body_items") or []
        body_nodes_ch = chapter_obj.get("body_nodes") or []

        title_aligned = (
            title_item.get("role") == title_node.get("role")
            and (title_item.get("text") or "") == (title_node.get("text") or "")
        )
        body_aligned = (
            len(body_items_ch) == len(body_nodes_ch)
            and all(
                it.get("role") == nd.get("role")
                and (it.get("text") or "") == (nd.get("text") or "")
                for it, nd in zip(body_items_ch, body_nodes_ch)
            )
        )

        # chapter title role union (1d 의존 제거)
        if title_item.get("role"):
            chapter_title_roles.add(title_item["role"])

        # 13.7d: chapter title item을 chapter_anchor_items에 별도 저장
        # body_items 평탄화에서 제외 — assembly가 양식 chapter title element를 anchor로 보존
        # adapted_title text 교체는 별도 stage (13.7d _replace_text)
        if title_item:
            chapter_anchor_items[ci] = title_item

        # node_id → node dict (chapter 단위)
        node_map: dict[int, dict] = {}
        if title_node:
            tid = title_node.get("id")
            if tid is not None:
                node_map[tid] = title_node
        for nd in body_nodes_ch:
            nid = nd.get("id")
            if nid is not None:
                node_map[nid] = nd
        chapter_node_maps.append(node_map)

        # 13.7d: chapter title item 평탄화 제외. chapter body items만 평탄화.
        # marker_rewrite는 chapter_title_roles set + chapter_idx_lookup으로 boundary 인식 (set 그대로 union).
        for k, (it, nd) in enumerate(zip(body_items_ch, body_nodes_ch)):
            bi = len(body_items)
            body_items.append({
                "role": it.get("role", ""),
                "text": it.get("text", ""),
            })
            node_lookup[bi] = nd
            chapter_idx_lookup[bi] = ci

        # 13.7d: adapted_title은 adaptation_decision에서 가져옴 (assemble path에서 _replace_text로 적용).
        # 양식 chapter title element는 anchor로 보존 + chapter_anchors loop가 text 교체.
        _adapted_text = title_item.get("text", "") if title_item else ""
        if _adapted_text:
            adapted_title_deferred.append({
                "chapter_idx": ci,
                "source_chapter_idx": chapter_obj.get("source_chapter_idx"),
                "adapted_title_in_title_item": _adapted_text,
            })

        per_chapter.append({
            "chapter_idx": ci,
            "source_chapter_idx": chapter_obj.get("source_chapter_idx"),
            "target_region_id": target_region_id,
            "section_id": section_id,
            "first_paragraph_idx": first_paragraph_idx,
            "status": "ok",
            "title_aligned": title_aligned,
            "body_aligned": body_aligned,
            "body_items_count": len(body_items_ch),
            "body_nodes_count": len(body_nodes_ch),
            "invariant_violations": violations,
        })

    chapter_count = len(chapter_objects or [])
    tree_available = (
        chapter_count > 0
        and fail_count == 0
        and all(pc["status"] != "fail" for pc in per_chapter)
        and all(
            pc["title_aligned"] in (True, None) and pc["body_aligned"] in (True, None)
            for pc in per_chapter
        )
    )

    rewrite_alignment = {
        "path": "chapter_objects",
        "tree_available": tree_available,
        "chapter_count": chapter_count,
        "ok_count": ok_count,
        "empty_count": empty_count,
        "fail_count": fail_count,
        "invariant_violations_count": sum(
            len(iv["violations"]) for iv in invariant_violations
        ),
        "per_chapter": per_chapter,
    }

    return {
        "body_items": body_items,
        "node_lookup": node_lookup,
        "chapter_idx_lookup": chapter_idx_lookup,
        "chapter_title_roles": chapter_title_roles,
        "chapter_node_maps": chapter_node_maps,
        "empty_preserve_indices": empty_preserve_indices,
        "chapter_anchor_items": chapter_anchor_items,        # 13.7d
        "adapted_title_deferred": adapted_title_deferred,    # 13.7d
        "rewrite_alignment": rewrite_alignment,
        "tree_available": tree_available,
        "invariant_violations": invariant_violations,
    }


def _reassign_unique_ids(elem, counter):
    """13.7b deepcopy 후 element 안 paragraph/tbl id를 unique sequential로 재할당.

    HWPX 양식 원본의 paragraph id가 동일값 (예: 2147483648 default)이라
    단순 deepcopy 시 같은 id가 100+회 반복 → 한컴 위조/변조 경고.

    paragraph(p), table(tbl), 기타 id attribute 가진 element 모두 처리.
    빈 문자열 id는 skip (subList 등 schema상 빈 값 의도).
    counter는 [int] mutable list로 외부에서 전달 (call 간 누적).
    """
    for e in elem.iter():
        eid = e.get("id")
        if eid is not None and eid != "":
            counter[0] += 1
            e.set("id", str(counter[0]))


def _reassign_all_section_ids(section_elements, counter_start: int = 4_000_000_000) -> dict:
    """전체 section element 안 모든 paragraph/tbl/element id를 unique sequential로 재할당.

    양식 원본 element는 default id (0, 2147483648 등)가 그대로 남아 있음.
    preserve된 양식 영역이 큰 경우 (multi-section 부록 등) → id 중복 → HWPX 위변조 경고.
    assembly 끝에 호출하여 새 element + 양식 원본 element 모두 unique 보장.

    빈 문자열 id (subList 등)는 schema 의도이므로 skip.
    """
    counter = [counter_start]
    reassigned = 0
    skipped_empty = 0
    for sec_elem in section_elements:
        if sec_elem is None:
            continue
        for e in sec_elem.iter():
            eid = e.get("id")
            if eid is None:
                continue
            if eid == "":
                skipped_empty += 1
                continue
            counter[0] += 1
            e.set("id", str(counter[0]))
            reassigned += 1
    return {
        "reassigned_count": reassigned,
        "skipped_empty_count": skipped_empty,
        "final_counter": counter[0],
    }


def assemble_hwpx_hybrid(
    template_source,
    structure: dict,
    content: dict,
    removed_indices: list[int] = None,
    idx_map: dict = None,
    enable_marker_rewrite: bool = True,
    content_only_mode: bool = False,
    preserve_indices: set[int] | None = None,
    analyzed_sections: set[int] | None = None,
    chapter_local_exemplars: dict | None = None,
    emphasis_layers: dict | None = None,
    paragraph_emphasis_map: dict | None = None,
    toc_replacements: list | None = None,
    toc_paragraph_idx: int | None = None,
) -> HwpxResult:
    """
    하이브리드 방식으로 HWPX 문서를 조립합니다.

    v1 구조 분석(idx + role) + v2 조립(exemplar 복제).

    1. structure에서 role → exemplar idx 매핑 생성
    2. 양식을 열고 exemplar 문단 요소를 deepcopy로 저장
    3. header 문단 텍스트 교체
    4. 본문 영역 비우기
    5. body 항목마다 role의 exemplar를 복제 + 텍스트 교체
    6. 완성된 문서를 bytes로 반환

    13.7a-A1: chapter route는 content["chapters"]에 chapter object list를
    전달한다. assemble은 이를 평탄화하여 body_items / node_lookup /
    chapter_title_roles 를 구성하고, empty chapter는 region 전체 preserve.
    shallow route / files.py legacy는 content["body"] flat path.
    chapter_trees 파라미터는 제거됨 (chapter object 안으로 흡수).

    Args:
        template_source: 양식 HWPX 파일 경로(str), bytes, 또는 file-like
        structure: parse_structure_from_llm() 반환값 (role 포함)
                   {"paragraphs": [{"idx": N, "role": "...", ...}], "tables": [...]}
        content: 다음 형태 중 하나
                 - chapter route: {"header": {...}, "chapters": [chapter_obj, ...]}
                 - shallow/legacy: {"header": {...}, "body": [{"role": ..., "text": ...}]}
                 둘 다 들어오면 chapters 우선 + 경고.
        removed_indices: truncate_xml()에서 제거된 인덱스 목록

    Returns:
        HwpxResult(data=bytes, success_count, fail_count, errors)
    """
    from copy import deepcopy
    from lxml import etree

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    # 13.7d-DIAG: assemble_hwpx_hybrid 진입 mark
    try:
        import os as _osd0, json as _jsond0
        from datetime import datetime as _dtd0
        _osd0.makedirs("/tmp/hwpx_debug", exist_ok=True)
        with open("/tmp/hwpx_debug/_d00_assemble_entry.json", "w", encoding="utf-8") as _fd0:
            _jsond0.dump({
                "timestamp": _dtd0.now().isoformat(),
                "content_keys": list(content.keys()),
                "has_chapters": "chapters" in content,
                "has_body": "body" in content,
                "chapters_count": len(content.get("chapters") or []),
                "body_count": len(content.get("body") or []),
                "preserve_indices_count": len(preserve_indices) if preserve_indices else 0,
                "analyzed_sections": list(analyzed_sections) if analyzed_sections else None,
                "content_only_mode": content_only_mode,
            }, _fd0, ensure_ascii=False, indent=2, default=str)
    except Exception as _ed0:
        pass  # diagnostic only

    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    # ── 13.7a-A1: chapter_objects 사전 처리 ──
    # content["chapters"]가 있으면 chapter route, 평탄화한 body_items와
    # node_lookup을 구성. 없으면 legacy/shallow path.
    # content["body"]와 content["chapters"]가 둘 다 있으면 chapters 우선 + 경고.
    _chapter_objects = content.get("chapters")
    _chapter_proc: dict | None = None
    if _chapter_objects is not None:
        # 13.7d: structure + idx_map 전달 (placeholder role lookup용)
        _chapter_proc = _process_chapter_objects(_chapter_objects, structure, idx_map)
        log.info(
            f"assemble: chapter-grouped path. chapters={len(_chapter_objects)}, "
            f"body_items_flat={len(_chapter_proc['body_items'])}, "
            f"empty_preserve={len(_chapter_proc['empty_preserve_indices'])}, "
            f"anchors={len(_chapter_proc.get('chapter_anchor_items', {}))}, "
            f"adapted_title_deferred={len(_chapter_proc.get('adapted_title_deferred', []))}, "
            f"tree_available={_chapter_proc['tree_available']}, "
            f"invariant_violations={_chapter_proc['rewrite_alignment']['invariant_violations_count']}"
        )

    paragraphs_info = structure.get("paragraphs", [])
    errors = []
    success_count = 0

    # idx_map: AI의 idx(축소본) → 원본 template의 실제 idx
    # 없으면 identity (축소 안 된 경우)
    def _to_real_idx(ai_idx: int) -> int:
        if idx_map:
            return idx_map.get(ai_idx, ai_idx)
        return ai_idx

    # ── 1단계: role → exemplar idx 매핑 (각 role의 첫 번째 idx를 exemplar로) ──
    role_exemplar_idx = {}  # role → 원본 template idx
    role_is_table_box = {}  # role → bool
    # skip: level 0 중 cover/toc 고정 슬롯만.
    # chapter_types의 title_role은 level 0이어도 skip하지 않음 (본문 구조의 일부).
    _title_roles = set()
    for _ct in structure.get("chapter_types", {}).values():
        tr = _ct.get("title_role", "")
        if tr:
            _title_roles.add(tr)
    # children을 가지는 level 0 문단도 skip하지 않음 (장 루트)
    _l0_with_children = set()
    for i, p in enumerate(paragraphs_info):
        if p.get("level", 0) == 0:
            if i + 1 < len(paragraphs_info) and paragraphs_info[i + 1].get("level", 0) > 0:
                _l0_with_children.add(p.get("idx"))

    def _is_skip(para: dict) -> bool:
        if para.get("level", 0) != 0:
            return False
        role = para.get("role", "")
        if role in _title_roles:
            return False
        if para.get("idx") in _l0_with_children:
            return False
        return True

    for p in paragraphs_info:
        role = p.get("role", "")
        ai_idx = p.get("idx", -1)
        real_idx = _to_real_idx(ai_idx)
        if role and role not in role_exemplar_idx and not _is_skip(p):
            role_exemplar_idx[role] = real_idx

    # 표 포함 여부 판별 (표가 있는 문단 = table box로 처리)
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            para = doc.paragraphs[idx]
            role_is_table_box[role] = bool(para.tables)

    log.info(
        f"role→exemplar 매핑: {len(role_exemplar_idx)}개 role, "
        f"table_box: {sum(role_is_table_box.values())}개"
    )

    # ── 2단계: exemplar 요소 저장 (deepcopy + ctrl/linesegarray 제거) ──
    exemplars = {}  # role → deepcopy된 XML element
    for role, idx in role_exemplar_idx.items():
        if 0 <= idx < len(doc.paragraphs):
            elem = deepcopy(doc.paragraphs[idx].element)
            _strip_document_ctrls(elem, NS)
            _strip_linesegarray(elem, NS)
            exemplars[role] = elem

    # blank exemplars by paraPrIDRef — 1.5c의 blank_rules에서 paraPrIDRef로 선택
    blank_exemplars = {}  # paraPrIDRef → element
    for i, para in enumerate(doc.paragraphs):
        if (para.text or "").strip():
            continue
        pp = para.element.get("paraPrIDRef", "0")
        if pp not in blank_exemplars:
            blank_el = deepcopy(para.element)
            _strip_linesegarray(blank_el, NS)
            _strip_secpr(blank_el, NS)
            blank_exemplars[pp] = blank_el

    # fallback: 어떤 blank도 못 찾으면 첫 문단을 비워 사용
    if not blank_exemplars and len(doc.paragraphs) > 0:
        fb = deepcopy(doc.paragraphs[0].element)
        _strip_linesegarray(fb, NS)
        _strip_document_ctrls(fb, NS)
        _strip_secpr(fb, NS)
        for run in fb.findall(f"{NS}run"):
            t = run.find(f"{NS}t")
            if t is not None:
                t.text = ""
                for child in list(t):
                    t.remove(child)
            for tbl in run.findall(f"{NS}tbl"):
                run.remove(tbl)
            for cont in run.findall(f"{NS}container"):
                run.remove(cont)
        blank_exemplars["0"] = fb

    # role → level 매핑 (전환 관계 판단용)
    role_level = {}
    for p in paragraphs_info:
        role = p.get("role", "")
        if role and role not in role_level:
            role_level[role] = p.get("level", 0)

    # 1.5c 규칙 로드
    format_rules = structure.get("format_rules", {})
    blank_rules = structure.get("blank_rules", [])
    # 같은 (from, to, relation) transition이 양식에 여러 번 나오면 다수결로 has_blank 결정.
    # 옛 코드는 dict 덮어쓰기로 마지막만 살아남아 양식 일관성 잃었음.
    _blank_votes: dict = {}
    for r in blank_rules:
        key = (r.get("from", ""), r.get("to", ""), r.get("relation", ""))
        _blank_votes.setdefault(key, []).append(r)
    blank_lookup = {}
    for _key, _rs in _blank_votes.items():
        _blank_n = sum(1 for r in _rs if r.get("has_blank"))
        _no_blank_n = len(_rs) - _blank_n
        # 동률 또는 다수 → has_blank 적용 (양식 빈 줄 보존 우선; 사용자 의도 "내려쓰기 모두 인식")
        _final_has_blank = _blank_n >= _no_blank_n and _blank_n > 0
        _final_paraPr = None
        if _final_has_blank:
            for r in _rs:
                if r.get("has_blank") and r.get("paraPrIDRef"):
                    _final_paraPr = r["paraPrIDRef"]
                    break
        blank_lookup[_key] = {
            "from": _key[0], "to": _key[1], "relation": _key[2],
            "has_blank": _final_has_blank,
            "paraPrIDRef": _final_paraPr,
        }

    # ── 3단계: header 영역 처리 ──
    # header는 {role_name: text} 형태 — role 이름을 AI가 자유롭게 지정
    header_data = content.get("header", {})

    # structure에서 role → real_idx 매핑 (첫 번째 출현만)
    role_to_first_idx = {}
    for p in paragraphs_info:
        role = p.get("role", "")
        if role and role not in role_to_first_idx:
            role_to_first_idx[role] = _to_real_idx(p.get("idx", -1))

    header_indices = set()
    for role_name, val in header_data.items():
        if not val:
            continue
        real_idx = role_to_first_idx.get(role_name, -1)
        if real_idx < 0 or real_idx >= len(doc.paragraphs):
            errors.append(f"header role '{role_name}' not found in structure")
            continue
        header_indices.add(real_idx)
        try:
            if isinstance(val, list):
                # parts list — 양식 t별 charPr 보존하면서 텍스트만 교체
                _p_elem = doc.paragraphs[real_idx].element
                _runs = _p_elem.findall(f"{NS}run")
                # 각 part의 charPrIDRef와 일치하는 run의 t.text 교체
                # 매칭 순서: parts 순서대로 + run 순서대로. charPr 매칭 시도 후 fallback 순서.
                _used_runs = set()
                for _pi, _part in enumerate(val):
                    if not isinstance(_part, dict):
                        continue
                    _cp = str(_part.get("charPrIDRef") or "")
                    _text = str(_part.get("text") or "")
                    # 1) charPr 매칭되는 run (아직 안 쓴 거)
                    _target_run = None
                    for _ri, _run in enumerate(_runs):
                        if _ri in _used_runs:
                            continue
                        if _run.get("charPrIDRef", "") == _cp:
                            _target_run = _run
                            _used_runs.add(_ri)
                            break
                    # 2) fallback: 순서대로 안 쓴 run
                    if _target_run is None and _pi < len(_runs) and _pi not in _used_runs:
                        _target_run = _runs[_pi]
                        _used_runs.add(_pi)
                    if _target_run is None:
                        continue
                    # run 안 t들 — 첫 t에 text 박고 나머지 t 비움
                    _ts = _target_run.findall(f"{NS}t")
                    if _ts:
                        _ts[0].text = _text
                        for _t in _ts[1:]:
                            _t.text = ""
                # 사용 안 된 run들의 t 비우기 (양식 잔여 텍스트 제거)
                for _ri, _run in enumerate(_runs):
                    if _ri in _used_runs:
                        continue
                    for _t in _run.findall(f"{NS}t"):
                        _t.text = ""
                success_count += 1
            else:
                # 단일 문자열 — 기존 동작
                _set_element_text(doc.paragraphs[real_idx], str(val), NS)
                success_count += 1
        except Exception as e:
            errors.append(f"header({role_name}, idx={real_idx}): {e}")

    # level 0 문단(cover/toc/spacer 등) → 보존 (header_indices에 추가)
    for p in paragraphs_info:
        real_idx = _to_real_idx(p.get("idx", -1))
        if _is_skip(p) and 0 <= real_idx < len(doc.paragraphs):
            header_indices.add(real_idx)

    # 첫 번째 문단(secPr 포함) 반드시 보존
    if len(doc.paragraphs) > 0:
        header_indices.add(0)

    # ── 4단계: 본문 영역 비우기 (header/toc/fixed/secPr 제외) ──
    # multi-section 대응: 각 paragraph가 속한 section에서 remove
    _all_sections = doc.oxml._sections
    _section_count = len(_all_sections)

    # paragraph element → owning section element 매핑
    _elem_to_section: dict = {}
    for sec in _all_sections:
        sec_el = sec.element
        for child in list(sec_el):
            _elem_to_section[child] = sec_el

    # paragraph index → section index 매핑 (preserved/residual 분류용)
    _para_to_sec_idx: dict[int, int] = {}
    for i, p in enumerate(doc.paragraphs):
        owning = _elem_to_section.get(p.element)
        if owning is not None:
            for si, sec in enumerate(_all_sections):
                if sec.element is owning:
                    _para_to_sec_idx[i] = si
                    break

    # secPr carrier 감지 (section layout 경계)
    _secpr_carriers: set[int] = set()
    for i, p in enumerate(doc.paragraphs):
        if p.element.find(f".//{NS}secPr") is not None:
            _secpr_carriers.add(i)

    # real_idx → para_info 역매핑 (preserved 분류 시 O(1) 조회)
    _real_idx_to_info: dict[int, dict] = {}
    for pi in paragraphs_info:
        ridx = _to_real_idx(pi.get("idx", -1))
        if ridx >= 0:
            _real_idx_to_info[ridx] = pi

    # header role indices (header_data에서 text 배정된 role의 real idx)
    _header_role_indices: set[int] = set()
    for rn, val in header_data.items():
        # val은 str 또는 list (parts) — 둘 다 truthy면 포함
        if val:
            ridx = role_to_first_idx.get(rn, -1)
            if 0 <= ridx < len(doc.paragraphs):
                _header_role_indices.add(ridx)

    # 9.1b: secPr carrier 보존 — section layout 경계 유지
    _secpr_preserved_count = 0
    _secpr_conflict_warnings = []
    for pidx in sorted(_secpr_carriers):
        if pidx in header_indices:
            continue  # already preserved by other rules
        p_info = _real_idx_to_info.get(pidx, {})
        role = p_info.get("role", "")
        text = (p_info.get("text", "") or "").strip()

        header_indices.add(pidx)
        _secpr_preserved_count += 1

        if text or role:
            _secpr_conflict_warnings.append({
                "para_idx": pidx,
                "section_idx": _para_to_sec_idx.get(pidx, -1),
                "role": role,
                "text_preview": text[:60],
                "conflict": "secPr_body_conflict_candidate",
            })

    if _secpr_preserved_count:
        log.info(
            f"assemble: {_secpr_preserved_count} secPr carrier(s) preserved "
            f"({len(_secpr_conflict_warnings)} with body conflict)"
        )

    # 13.3 preserve_indices: shallow route에서 slot/attachment paragraphs 보존
    _preserve_applied = False
    _preserve_debug = {}
    if preserve_indices:
        _before = len(header_indices)
        header_indices |= preserve_indices
        _added = len(header_indices) - _before
        _preserve_applied = True
        _preserve_debug = {
            "preserve_indices": sorted(preserve_indices),
            "preserve_indices_applied": True,
            "new_preservations": _added,
        }
        if _added:
            log.info(f"assemble: preserve_indices added {_added} paragraphs to header set")

    # 13.7a-A1 / 13.7d: empty chapter title preserve (chapter title paragraph만, body는 remove 대상)
    _empty_chapter_preserve_debug = {}
    if _chapter_proc and _chapter_proc["empty_preserve_indices"]:
        _eci = _chapter_proc["empty_preserve_indices"]
        _before_e = len(header_indices)
        header_indices |= _eci
        _added_e = len(header_indices) - _before_e
        _empty_chapter_preserve_debug = {
            "empty_chapter_preserve_indices": sorted(_eci),
            "empty_chapter_preserve_count": _added_e,
        }
        if _added_e:
            log.info(
                f"assemble: empty chapter preserve added {_added_e} paragraphs "
                f"({len(_eci)} indices from empty chapters)"
            )

    # 13.7d: chapter_anchors 추적 (chapter route region-aware placement)
    # chapter title element를 anchor로 보존 (status="ok"+"empty" 둘 다).
    # body items insert 시 chapter_anchors[ci] 다음 위치에 (section-aware).
    #
    # 13.7d fix: light_xml _idx → top-level paragraph element 매핑 (table cell paragraph 제외).
    # doc.paragraphs는 lib이 어떤 paragraph iter하는지 보장 X (table cell 등 포함 가능).
    # 양식 section의 direct children paragraph만 모아서 light_xml _idx와 정확 매핑.
    # header_indices는 doc.paragraphs index 기준이므로 anchor element의 doc.paragraphs idx도 찾기.
    _top_level_paragraphs: list = []
    for _sec_obj in _all_sections:
        _sec_el = _sec_obj.element
        for _child in list(_sec_el):
            if _child.tag.endswith("}p"):
                _top_level_paragraphs.append(_child)

    # doc.paragraphs element id → doc.paragraphs index 매핑 (header_indices 추가용)
    _doc_para_to_idx: dict = {}
    for _di, _dp in enumerate(doc.paragraphs):
        _doc_para_to_idx[id(_dp.element)] = _di

    log.info(
        f"[13.7d] top_level_paragraphs count: {len(_top_level_paragraphs)}, "
        f"doc.paragraphs count: {len(doc.paragraphs)}, "
        f"light_xml structure paragraphs count: {len(paragraphs_info)}"
    )

    # 13.7b section-local anchor matching:
    # section별 top-level paragraph list (per-section ordered).
    # chapter.section_id + chapter.section_local_first_idx로 직접 lookup.
    _section_top_level_paragraphs: dict[int, list] = {}
    for _sec_pos, _sec_obj in enumerate(_all_sections):
        _section_top_level_paragraphs[_sec_pos] = []
        for _child in list(_sec_obj.element):
            if _child.tag.endswith("}p"):
                _section_top_level_paragraphs[_sec_pos].append(_child)

    def _get_para_text(p_elem) -> str:
        parts = []
        for t in p_elem.iter():
            if t.tag.endswith("}t") and t.text:
                parts.append(t.text)
        return "".join(parts).strip()

    def _resolve_section_pos_of_elem(p_elem) -> int:
        owning_sec = _elem_to_section.get(p_elem)
        if owning_sec is None:
            return -1
        for si, s in enumerate(_all_sections):
            if s.element is owning_sec:
                return si
        return -1

    def _validate_anchor_signature(
        anchor_el, expected_role: str, expected_marker: str
    ) -> tuple[bool, str]:
        """anchor element의 role/marker가 chapter title과 매칭되는지 검증.
        light_xml paragraphs_info에서 anchor element의 (doc_idx → light_xml _idx)
        매핑을 통해 role 추출. signature mismatch는 fallback 진입 신호 (hard fail X).
        Returns (match, reason).
        """
        # doc_idx → role (header_indices와 동일 idx 체계, paragraphs_info는 light_xml idx)
        doc_idx_of_anchor = _doc_para_to_idx.get(id(anchor_el), -1)
        if doc_idx_of_anchor < 0:
            return False, "anchor_not_in_doc"
        # _real_idx_to_info: light_xml _idx → para_info (role/text 등). 단 doc_idx와 light_xml _idx가 다를 수 있음.
        # 일단 anchor text와 비교 (signature 검증 최소 — text matching은 일관성 검증)
        anchor_text = _get_para_text(anchor_el).strip()
        if expected_role:
            # role 검증: _real_idx_to_info에서 anchor doc_idx와 비슷한 idx의 role 찾기
            # 단 mapping이 정확하지 않을 수 있으니 보수적
            pass  # role 검증은 light_xml mapping 정확화 후 (Phase B에서 강화)
        return True, "anchor_text_present" if anchor_text else "anchor_text_empty"

    def _find_anchor_in_section_by_text(
        target_text: str, target_section_id: int
    ) -> tuple:
        """same-section fallback: target_section_id 안에서만 title_text 매칭.
        cross-section bleed 절대 X.
        """
        if not target_text or not target_text.strip():
            return None, "no_target_text"
        if target_section_id not in _section_top_level_paragraphs:
            return None, "section_id_out_of_range"
        target_norm = " ".join(target_text.split())
        sec_paras = _section_top_level_paragraphs[target_section_id]
        # exact
        for p_elem in sec_paras:
            p_text = " ".join(_get_para_text(p_elem).split())
            if p_text == target_norm:
                return p_elem, "text_exact_same_section"
        # prefix
        for p_elem in sec_paras:
            p_text = " ".join(_get_para_text(p_elem).split())
            if p_text and (
                p_text.startswith(target_norm[:30])
                or target_norm.startswith(p_text[:30])
            ):
                return p_elem, "text_prefix_same_section"
        return None, "no_text_match_in_section"

    # marker_policies 빌드 (chapter_anchors loop의 marker auto-prepend에서 사용).
    # body items 처리에서도 사용 (line ~2056) — 같은 dict 재사용.
    from open_webui.utils.hwpx_analyzer import extract_marker_policies
    _marker_policy_1f = structure.get("marker_policy_1f")
    _marker_policies = extract_marker_policies(paragraphs_info, marker_policy_1f=_marker_policy_1f)

    chapter_anchors: dict = {}  # ci → anchor element
    chapter_anchor_failures: list[dict] = []  # placement_failure list
    _chapter_anchor_debug = []

    # 13.7d-DIAG: chapter_anchors loop 진입 직전 mark
    try:
        import os as _osd1, json as _jsond1
        from datetime import datetime as _dtd1
        with open("/tmp/hwpx_debug/_d01_anchor_loop_pre.json", "w", encoding="utf-8") as _fd1:
            _jsond1.dump({
                "timestamp": _dtd1.now().isoformat(),
                "_chapter_proc_truthy": bool(_chapter_proc),
                "_chapter_proc_keys": list(_chapter_proc.keys()) if isinstance(_chapter_proc, dict) else None,
                "_chapter_objects_truthy": bool(_chapter_objects),
                "_chapter_objects_count": len(_chapter_objects) if _chapter_objects else 0,
                "will_enter_loop": bool(_chapter_proc and _chapter_objects),
                "_section_top_level_paragraphs_keys": list(_section_top_level_paragraphs.keys()) if isinstance(_section_top_level_paragraphs, dict) else None,
                "_top_level_paragraphs_count": len(_top_level_paragraphs) if _top_level_paragraphs is not None else None,
            }, _fd1, ensure_ascii=False, indent=2, default=str)
    except Exception as _ed1:
        try:
            with open("/tmp/hwpx_debug/_d01_anchor_loop_pre_ERROR.txt", "w") as _ef1:
                import traceback as _tbd1
                _tbd1.print_exc(file=_ef1)
        except Exception:
            pass

    if _chapter_proc and _chapter_objects:
        for ci, ch_obj in enumerate(_chapter_objects):
            _pi = ch_obj.get("paragraph_indices") or []
            _sec_id_of_ch = ch_obj.get("section_id", 0)
            _section_local_first_idx = ch_obj.get("section_local_first_idx")
            _title_item_for_anchor = ch_obj.get("title_item") or {}
            _title_text_for_anchor = (_title_item_for_anchor.get("text") or "").strip()
            _title_role_for_anchor = _title_item_for_anchor.get("role", "")
            _title_marker_for_anchor = (_title_item_for_anchor.get("marker") or "").strip()

            _anchor_el = None
            _anchor_match_method = None
            _anchor_owning_sec = -1

            # Priority 1: section_id + section_local_first_idx primary
            #   (Phase B 통일 chapter_object schema에서 제공)
            if (
                _section_local_first_idx is not None
                and _sec_id_of_ch in _section_top_level_paragraphs
            ):
                _sec_paras = _section_top_level_paragraphs[_sec_id_of_ch]
                if 0 <= _section_local_first_idx < len(_sec_paras):
                    _cand = _sec_paras[_section_local_first_idx]
                    _sig_ok, _sig_reason = _validate_anchor_signature(
                        _cand, _title_role_for_anchor, _title_marker_for_anchor
                    )
                    if _sig_ok:
                        _anchor_el = _cand
                        _anchor_match_method = f"section_local_idx_primary({_sig_reason})"

            # Priority 2 (legacy fallback): paragraph_indices[0] + idx_map
            #   section_local_first_idx 없는 chapter_object (section 0 기존 13.4b path)
            if _anchor_el is None and _pi:
                _ai_idx = _pi[0]
                if _sec_id_of_ch != 0:
                    _real_idx = _ai_idx
                else:
                    _real_idx = idx_map.get(_ai_idx, _ai_idx) if idx_map else _ai_idx
                if 0 <= _real_idx < len(_top_level_paragraphs):
                    _cand = _top_level_paragraphs[_real_idx]
                    _cand_sec_pos = _resolve_section_pos_of_elem(_cand)
                    if _cand_sec_pos == _sec_id_of_ch:
                        _anchor_el = _cand
                        _anchor_match_method = "legacy_idx_map_paragraph_indices"
                    else:
                        # cross-section: legacy idx map이 다른 section paragraph 가리킴
                        # → priority 3 fallback으로 진입
                        log.warning(
                            f"[13.7b anchor ci={ci}] legacy idx anchor cross-section "
                            f"(chapter.section_id={_sec_id_of_ch}, anchor.section={_cand_sec_pos}, "
                            f"real_idx={_real_idx}). text fallback 시도."
                        )

            # Priority 3 (text fallback, same section only)
            if _anchor_el is None and _title_text_for_anchor:
                _anchor_el_text, _text_reason = _find_anchor_in_section_by_text(
                    _title_text_for_anchor, _sec_id_of_ch
                )
                if _anchor_el_text is not None:
                    _anchor_el = _anchor_el_text
                    _anchor_match_method = _text_reason

            # Priority 4: placement_failure (hard fail — cross-section bleed 차단)
            if _anchor_el is None:
                _fail_info = {
                    "chapter_idx": ci,
                    "section_id": _sec_id_of_ch,
                    "title_text_preview": _title_text_for_anchor[:60],
                    "section_local_first_idx": _section_local_first_idx,
                    "paragraph_indices_first": _pi[0] if _pi else None,
                    "status": ch_obj.get("status"),
                    "failure_reason": "no_anchor_in_target_section",
                }
                chapter_anchor_failures.append(_fail_info)
                _chapter_anchor_debug.append({**_fail_info, "match_method": "placement_failure"})
                log.warning(
                    f"[13.7b anchor PLACEMENT_FAIL ci={ci}] section_id={_sec_id_of_ch} "
                    f"title='{_title_text_for_anchor[:40]}' — no anchor found, cross-section bleed 차단"
                )
                continue

            # Invariant: anchor owning section == chapter.section_id (cross-section bleed hard fail)
            _anchor_owning_sec = _resolve_section_pos_of_elem(_anchor_el)
            if _anchor_owning_sec != _sec_id_of_ch:
                _fail_info = {
                    "chapter_idx": ci,
                    "section_id": _sec_id_of_ch,
                    "anchor_owning_section": _anchor_owning_sec,
                    "match_method": _anchor_match_method,
                    "title_text_preview": _title_text_for_anchor[:60],
                    "failure_reason": "cross_section_bleed_detected",
                }
                chapter_anchor_failures.append(_fail_info)
                _chapter_anchor_debug.append({**_fail_info, "match_method": "cross_section_bleed"})
                log.error(
                    f"[13.7b anchor CROSS_SECTION_BLEED ci={ci}] "
                    f"chapter.section_id={_sec_id_of_ch}, anchor.section={_anchor_owning_sec} — HARD FAIL"
                )
                continue

            # Success: anchor 저장 + header_indices에 추가
            chapter_anchors[ci] = _anchor_el
            _anchor_doc_idx = _doc_para_to_idx.get(id(_anchor_el), -1)
            if _anchor_doc_idx >= 0 and _anchor_doc_idx not in header_indices:
                header_indices.add(_anchor_doc_idx)
            _anchor_text_preview = _get_para_text(_anchor_el)[:60]

            # 13.7d-2phase: adapted_title을 chapter title element에 반영 (action 무관).
            # 사용자 정책: preserve도 chapter title은 결정됨. action_action 검사 제거.
            # adapted_title이 양식 원본과 다르면 무조건 교체.
            _adapted_title_applied = False
            _adapted_title_skip_reason = None
            _ad_dec = (ch_obj.get("_debug") or {}).get("adaptation_decision") or {}
            _ad_action = _ad_dec.get("action", "")
            _ad_text = (_ad_dec.get("adapted_title") or "").strip()

            # 2c 분리 (2026-05-22): chapter title 마커는 2c가 _ad_text 안에 이미 입혀서 보냄.
            # 조립은 _ad_text 그대로 박기만 함. marker auto-prepend / strip / reattach 제거.
            _ad_text_with_marker = _ad_text

            _anchor_norm = " ".join(_anchor_text_preview.split())
            _title_norm = " ".join(_ad_text_with_marker.split())
            if not _ad_text:
                _adapted_title_skip_reason = "empty_adapted_title"
            else:
                # 양식 본래와 같든 다르든 항상 박음 (양식 잔재 비우고 본문 자리에 정확히 박기).
                # 이전엔 _title_norm == _anchor_norm이면 skip하여 양식 그대로 두었는데,
                # 새 제목과 그대로 제목 사이 조립 동작 불일치 발생.
                try:
                    # 본문 path 통일 (2026-05-21):
                    # 본문은 본보기(exemplars[role]) deepcopy해서 글자 박는 방식.
                    # chapter title도 동일 매커니즘 적용 — anchor element의 ctrl 없는 run을
                    # 본보기 run으로 교체. anchor가 wrong 자리(단일 t 구조 header 등)를 잡아도
                    # 본보기의 마커 t + 본문 t 분리 구조가 들어가서 split 정상 동작 → 마커 글꼴 보존.
                    # element 자체는 parent에서 안 빠지므로 doc_para_to_idx, header_indices 영향 없음.
                    _ct_exemplar = exemplars.get(_title_role_for_anchor)
                    _exemplar_run_swap_applied = False
                    if _ct_exemplar is not None:
                        _exemplar_runs = _ct_exemplar.findall(f"{NS}run")
                        # 본보기에 t 있는 run이 있을 때만 교체 (없으면 anchor 그대로)
                        _ex_has_t = any(
                            any(el.tag == f"{NS}t" for el in _r.iter())
                            for _r in _exemplar_runs
                        )
                        if _ex_has_t:
                            # anchor element의 ctrl 없는 run 모두 제거 (ctrl run은 보존)
                            for _run in list(_anchor_el.findall(f"{NS}run")):
                                if _run.find(f"{NS}ctrl") is None:
                                    _anchor_el.remove(_run)
                            # 본보기 run을 deepcopy해서 anchor element에 추가
                            for _run in _exemplar_runs:
                                _new_run = deepcopy(_run)
                                _anchor_el.append(_new_run)
                            _exemplar_run_swap_applied = True

                    # 2c output 박기 — 강조 markup 있으면 본문 path와 동일 매커니즘
                    # (markup 파싱 + 양식 글꼴 적용). 없으면 단일 텍스트.
                    _ct_em_data = (emphasis_layers or {}).get(_title_role_for_anchor) or {}
                    _ct_charpr_map: dict = {}
                    _ct_valid_layers: set = set()
                    if _ct_em_data:
                        _base_cp_ct = _ct_em_data.get("base_charpr_id", "") or ""
                        _base_lid_ct = _ct_em_data.get("base_layer_id", "") or ""
                        _ct_charpr_map["base"] = _base_cp_ct
                        # 2c가 base에도 markup 박는 기본 동작 — base_layer_id도 valid_layers에 추가
                        if _base_lid_ct and _base_cp_ct:
                            _ct_charpr_map[_base_lid_ct] = _base_cp_ct
                            _ct_valid_layers.add(_base_lid_ct)
                        for _el in (_ct_em_data.get("emphasis_layers") or []):
                            _lid = _el.get("layer_id", "") or ""
                            _cp = _el.get("charpr_id", "") or ""
                            if _lid and _cp:
                                _ct_charpr_map[_lid] = _cp
                                _ct_valid_layers.add(_lid)
                    # 들여쓰기 책임 분리 (2026-05-27): AI text leading whitespace strip
                    # + cluster 표준 indent 자동 prepend (body item path 와 동일).
                    _ad_text_stripped = _ad_text_with_marker.lstrip(" \t")
                    _ct_segments = (
                        _parse_emphasis_markup(_ad_text_stripped, _ct_valid_layers)
                        if _ct_valid_layers else []
                    )
                    # indent 정보는 paragraph_emphasis_map 의 cluster entry 에 있음
                    _ct_pem_for_indent = (paragraph_emphasis_map or {}).get(_title_role_for_anchor) or {}
                    _ct_indent_mode = _ct_pem_for_indent.get("indent_length_mode", 0) or 0
                    _ct_indent_lid = _ct_pem_for_indent.get("indent_layer_majority_id")
                    _ct_indent_cp_maj = _ct_pem_for_indent.get("indent_layer_majority_charpr")
                    if _ct_indent_mode and _ct_indent_lid and _ct_indent_cp_maj:
                        _ct_charpr_map.setdefault(_ct_indent_lid, _ct_indent_cp_maj)
                        _ct_valid_layers.add(_ct_indent_lid)
                        _ct_indent_seg = (_ct_indent_lid, " " * int(_ct_indent_mode))
                        if _ct_segments == [(None, "")]:
                            _ct_segments = [_ct_indent_seg]
                        else:
                            _ct_segments = [_ct_indent_seg] + _ct_segments
                    _ct_has_em = any(s[0] is not None for s in _ct_segments)
                    if _ct_valid_layers and _ct_has_em:
                        _replace_text_with_emphasis_segments(
                            _anchor_el, "", _ct_segments, _ct_charpr_map, NS,
                        )
                    else:
                        _replace_text_in_paragraph_elem(_anchor_el, _ad_text_stripped, NS)
                    _adapted_title_applied = True
                    log.info(
                        f"[chapter title body-path-unified ci={ci}] applied "
                        f"(action={_ad_action}, exemplar_swap={_exemplar_run_swap_applied}, "
                        f"em_segments={len(_ct_segments)}, has_em={_ct_has_em}): "
                        f"'{_anchor_text_preview[:50]}' → '{_ad_text_with_marker[:50]}'"
                    )
                except Exception as _adapt_e:
                    _adapted_title_skip_reason = f"replace_failed:{_adapt_e}"
                    log.warning(
                        f"[chapter title body-path-unified ci={ci}] 교체 실패 — 양식 원본 유지: {_adapt_e}"
                    )

            _chapter_anchor_debug.append({
                "chapter_idx": ci,
                "section_id": _sec_id_of_ch,
                "anchor_owning_section": _anchor_owning_sec,
                "match_method": _anchor_match_method,
                "section_local_first_idx": _section_local_first_idx,
                "doc_idx": _anchor_doc_idx,
                "anchor_text_preview": _anchor_text_preview,        # 교체 전 (양식 원본)
                "title_text_preview": _title_text_for_anchor[:60],
                "adapted_title_applied": _adapted_title_applied,    # 13.7d
                "adapted_title_skip_reason": _adapted_title_skip_reason,
                "status": ch_obj.get("status"),
            })
            log.info(
                f"[13.7b anchor OK ci={ci}] section={_sec_id_of_ch} method={_anchor_match_method} "
                f"anchor_text='{_anchor_text_preview}' adapted_title_applied={_adapted_title_applied}"
            )

            # 13.7d-DIAG: 각 ci 처리 결과를 즉시 별도 file에 append (loop 끝까지 안 가도 추적 가능)
            try:
                import os as _osd2, json as _jsond2
                _per_ci_log_path = "/tmp/hwpx_debug/_d02_anchor_per_ci.jsonl"
                with open(_per_ci_log_path, "a", encoding="utf-8") as _fd2:
                    _ci_diag = {
                        "ci": ci,
                        "section_id": _sec_id_of_ch,
                        "ch_obj_status": ch_obj.get("status"),
                        "title_role": _title_role_for_anchor,
                        "title_marker": _title_marker_for_anchor,
                        "title_item_text": _title_text_for_anchor[:120],
                        "anchor_match_method": _anchor_match_method,
                        "anchor_doc_idx": _anchor_doc_idx,
                        "anchor_owning_section": _anchor_owning_sec,
                        "anchor_text_preview": _anchor_text_preview[:120],
                        "ad_action": _ad_action,
                        "ad_text": _ad_text[:120],
                        "ad_decision_present": bool(_ad_dec),
                        "ad_decision_keys": list(_ad_dec.keys()) if _ad_dec else [],
                        "adapted_title_applied": _adapted_title_applied,
                        "adapted_title_skip_reason": _adapted_title_skip_reason,
                        "anchor_norm": _anchor_norm[:120],
                        "title_norm": _title_norm[:120],
                    }
                    _fd2.write(_jsond2.dumps(_ci_diag, ensure_ascii=False) + "\n")
            except Exception as _ed2:
                try:
                    with open("/tmp/hwpx_debug/_d02_anchor_per_ci_ERROR.txt", "a") as _ef2:
                        import traceback as _tbd2
                        _ef2.write(f"\nci={ci}: ")
                        _tbd2.print_exc(file=_ef2)
                except Exception:
                    pass
    if chapter_anchors:
        log.info(
            f"assemble: chapter_anchors set for {len(chapter_anchors)} chapters, "
            f"placement_failures={len(chapter_anchor_failures)}"
        )

    # 표지(cover) preserve: chapter title 첫 등장 전의 모든 doc.paragraphs를 통째 preserve.
    # truncate_xml이 token budget 위해 빈 paragraph(paraPr 296/28 등) 제거 →
    # 1a paragraphs(224)와 doc.paragraphs(418) 매핑 깨짐 → _is_skip이 1a 기반이라
    # 양식 doc.paragraphs의 빈 paragraph 못 잡음 → body remove에서 사라지는 버그.
    # chapter title 첫 anchor 등장 전까지 doc.paragraphs 통째 preserve로 우회.
    if chapter_anchors:
        _first_chapter_doc_idx = None
        for _ci_cover, _anchor_cover in chapter_anchors.items():
            if _anchor_cover is None:
                continue
            _aidx_cover = _doc_para_to_idx.get(id(_anchor_cover), -1)
            if _aidx_cover < 0:
                continue
            if _first_chapter_doc_idx is None or _aidx_cover < _first_chapter_doc_idx:
                _first_chapter_doc_idx = _aidx_cover
        if _first_chapter_doc_idx is not None and _first_chapter_doc_idx > 0:
            _cover_added = 0
            for _di_cover in range(_first_chapter_doc_idx):
                if _di_cover not in header_indices:
                    header_indices.add(_di_cover)
                    _cover_added += 1
            log.info(
                f"[cover preserve] chapter 전 doc paragraph {_cover_added}개 추가 preserve "
                f"(first_chapter_doc_idx={_first_chapter_doc_idx})"
            )

    # 13.7b: placement_failure ci set — body items 처리 시 skip 위해
    _placement_failed_chapter_indices: set = {
        f["chapter_idx"] for f in chapter_anchor_failures
        if isinstance(f, dict) and f.get("chapter_idx") is not None
    }
    # placement_failure를 errors에 추가 (chapter 단위 hard fail)
    for _f in chapter_anchor_failures:
        errors.append(
            f"chapter_placement_failure ci={_f.get('chapter_idx')} "
            f"section_id={_f.get('section_id')} reason={_f.get('failure_reason')}: "
            f"title='{_f.get('title_text_preview', '')[:50]}'"
        )

    # 13.7b: empty_preserve_indices 재계산 (chapter_anchors element 기반)
    # _process_chapter_objects가 paragraph_indices[0]을 doc.paragraphs idx로 잘못
    # 매핑한 경우 (section_offset 계산 부정확) 잘못된 paragraph가 preserve됨.
    # chapter_anchors는 Priority 1 (section_local_first_idx) 기반으로 정확하므로
    # 그 element의 doc.paragraphs idx를 직접 사용.
    if _chapter_proc and _chapter_objects:
        _chapter_proc_empty_preserve = _chapter_proc.get("empty_preserve_indices")
        if _chapter_proc_empty_preserve is not None and isinstance(_chapter_proc_empty_preserve, set):
            _chapter_proc_empty_preserve.clear()
            for ci, ch_obj in enumerate(_chapter_objects):
                if ch_obj.get("status") != "empty":
                    continue
                if ci not in chapter_anchors:
                    continue
                _anchor_el_ep = chapter_anchors[ci]
                _anchor_doc_idx_ep = _doc_para_to_idx.get(id(_anchor_el_ep), -1)
                if _anchor_doc_idx_ep >= 0:
                    _chapter_proc_empty_preserve.add(_anchor_doc_idx_ep)
                    if _anchor_doc_idx_ep not in header_indices:
                        header_indices.add(_anchor_doc_idx_ep)
            log.info(
                f"[13.7b] empty_preserve_indices recomputed from chapter_anchors: "
                f"{sorted(_chapter_proc_empty_preserve)}"
            )

    # 13.7d debug: assembly anchor 매핑 정확성 진단을 위해 file dump
    # 사용자 환경에서 worker log 직접 확인 불가 → file로 진단 정보 저장
    try:
        import os as _os_dbg
        import json as _json_dbg
        _dbg_dir = "/tmp/hwpx_debug"
        _os_dbg.makedirs(_dbg_dir, exist_ok=True)
        # doc.paragraphs와 _top_level_paragraphs 매칭 (id 기반)
        _top_set = {id(p) for p in _top_level_paragraphs}
        _doc_in_top = sum(1 for dp in doc.paragraphs if id(dp.element) in _top_set)
        # idx_map 첫 25 entries (mapping 정확성)
        _idx_map_sample = {}
        if idx_map:
            for _k in sorted(idx_map.keys())[:25] if isinstance(idx_map, dict) else []:
                _idx_map_sample[_k] = idx_map[_k]
        with open(_os_dbg.path.join(_dbg_dir, "17_assembly_anchor_debug.json"), "w", encoding="utf-8") as _f:
            _json_dbg.dump({
                "top_level_paragraphs_count": len(_top_level_paragraphs),
                "doc_paragraphs_count": len(doc.paragraphs),
                "doc_in_top_match_count": _doc_in_top,
                "doc_not_in_top_count": len(doc.paragraphs) - _doc_in_top,
                "light_xml_paragraphs_count": len(paragraphs_info),
                "header_indices_count": len(header_indices),
                "header_indices_sorted": sorted(header_indices)[:50],
                "idx_map_first_25": _idx_map_sample,
                "chapter_anchor_debug": _chapter_anchor_debug,
            }, _f, ensure_ascii=False, indent=2, default=str)
    except Exception as _dbg_e:
        log.warning(f"[13.7d] anchor debug dump 실패: {_dbg_e}")
        # 13.7d-DIAG: exception 자체를 별도 file에 traceback 기록
        try:
            with open("/tmp/hwpx_debug/_d03_anchor_dump_ERROR.txt", "w") as _efd:
                import traceback as _tb_dbg
                _efd.write(f"exception: {_dbg_e}\n\n")
                _tb_dbg.print_exc(file=_efd)
        except Exception:
            pass

    # 13.7d-DIAG: chapter_anchors loop 완전 종료 후 mark
    try:
        import os as _osd3, json as _jsond3
        from datetime import datetime as _dtd3
        with open("/tmp/hwpx_debug/_d04_anchor_loop_done.json", "w", encoding="utf-8") as _fd3:
            _jsond3.dump({
                "timestamp": _dtd3.now().isoformat(),
                "chapter_anchors_count": len(chapter_anchors),
                "chapter_anchors_keys": sorted(chapter_anchors.keys()) if chapter_anchors else [],
                "chapter_anchor_failures_count": len(chapter_anchor_failures),
                "chapter_anchor_failures": chapter_anchor_failures,
                "chapter_anchor_debug_count": len(_chapter_anchor_debug),
            }, _fd3, ensure_ascii=False, indent=2, default=str)
    except Exception as _ed3:
        try:
            with open("/tmp/hwpx_debug/_d04_anchor_loop_done_ERROR.txt", "w") as _ef3:
                import traceback as _tbd3
                _ef3.write(f"exception: {_ed3}\n\n")
                _tbd3.print_exc(file=_ef3)
        except Exception:
            pass

    # 13.5 unanalyzed section preserve safety
    _unanalyzed_section_debug = {}
    if analyzed_sections is not None:
        _all_sec_indices = set(_para_to_sec_idx.values())
        _unanalyzed = _all_sec_indices - analyzed_sections
        if _unanalyzed:
            _section_preserved = 0
            for i in range(_orig_para_count_pre := len(doc.paragraphs)):
                sec_idx = _para_to_sec_idx.get(i)
                if sec_idx is not None and sec_idx in _unanalyzed and i not in header_indices:
                    header_indices.add(i)
                    _section_preserved += 1
            _unanalyzed_section_debug = {
                "analyzed_sections": sorted(analyzed_sections),
                "unanalyzed_sections": sorted(_unanalyzed),
                "paragraphs_preserved": _section_preserved,
            }
            if _section_preserved:
                log.info(
                    f"assemble: unanalyzed section preserve — "
                    f"sections {sorted(_unanalyzed)}, {_section_preserved} paragraphs preserved"
                )

    _orig_para_count = len(doc.paragraphs)  # remove 전 총 수 (분류용)
    _table_text_skipped = 0  # 13.3 table policy: text replacement skip 횟수
    body_elements = []
    _remove_per_section: dict[int, int] = {}  # section_idx → remove count
    _body_para_indices: set[int] = set()
    for i, p in enumerate(doc.paragraphs):
        if i not in header_indices:
            body_elements.append(p.element)
            _body_para_indices.add(i)

    # secPr carrier warning: body로 분류되어 삭제될 secPr carrier
    _secpr_carrier_warnings = []
    for pidx in sorted(_secpr_carriers & _body_para_indices):
        _w_info = _real_idx_to_info.get(pidx, {})
        _secpr_carrier_warnings.append({
            "para_idx": pidx,
            "section_idx": _para_to_sec_idx.get(pidx, -1),
            "role": _w_info.get("role", ""),
            "text_preview": (_w_info.get("text", "") or "")[:60],
            "status": "will_be_removed",
        })
    if _secpr_carrier_warnings:
        log.warning(
            f"assemble: {len(_secpr_carrier_warnings)} secPr carrier(s) "
            f"in body_elements — section layout may be lost"
        )

    for elem in body_elements:
        owning_section = _elem_to_section.get(elem)
        if owning_section is not None:
            owning_section.remove(elem)
            # debug: section별 remove count
            for si, sec in enumerate(_all_sections):
                if sec.element is owning_section:
                    _remove_per_section[si] = _remove_per_section.get(si, 0) + 1
                    break
        else:
            # fallback: section[0]에서 시도 (단일 section 호환)
            try:
                _all_sections[0].element.remove(elem)
                _remove_per_section[0] = _remove_per_section.get(0, 0) + 1
            except ValueError:
                log.warning(f"assemble: paragraph element not found in any section")

    log.info(
        f"본문 {len(body_elements)}개 문단 제거, "
        f"header {len(header_indices)}개 보존, "
        f"sections={_section_count}, remove_per_section={dict(_remove_per_section)}"
    )

    # preserved/residual candidate 분류 (section별, remove 후 남은 문단)
    _preserved_per_section: dict[str, list] = {}
    _residual_candidates: list[dict] = []
    for i in sorted(header_indices):
        if i >= _orig_para_count:
            continue
        sec_idx = _para_to_sec_idx.get(i, 0)
        p_info = _real_idx_to_info.get(i, {})
        role = p_info.get("role", "")
        text_preview = (p_info.get("text", "") or "")[:60]
        has_secpr = i in _secpr_carriers

        if i in _header_role_indices:
            reason = "preserved_header"
        elif has_secpr:
            reason = "preserved_secPr_carrier"
        elif not text_preview.strip():
            reason = "spacer_candidate"
        else:
            reason = "body_residual_candidate"

        entry = {
            "para_idx": i,
            "section_idx": sec_idx,
            "role": role,
            "text_preview": text_preview,
            "has_secPr": has_secpr,
            "is_level0_skip": _is_skip(p_info) if p_info else False,
            "is_first_para": i == 0,
            "reason": reason,
        }
        sec_key = str(sec_idx)
        if sec_key not in _preserved_per_section:
            _preserved_per_section[sec_key] = []
        _preserved_per_section[sec_key].append(entry)
        if reason in ("spacer_candidate", "body_residual_candidate"):
            _residual_candidates.append(entry)

    # ── 5단계: body 항목으로 문서 재조립 (format_rules 기반 indent + blank_rules 기반 blank) ──
    # append target: remove가 가장 많이 발생한 section (=원래 body가 있던 section)
    if _remove_per_section:
        _target_sec_idx = max(_remove_per_section, key=_remove_per_section.get)
    else:
        _target_sec_idx = 0
    section_elem = _all_sections[_target_sec_idx].element

    # 9.2a: append target candidate 관측
    _body_sections = sorted(si for si, cnt in _remove_per_section.items() if cnt > 0)
    _multi_body_section = len(_body_sections) > 1
    _append_target_candidates = []
    for si in _body_sections:
        _append_target_candidates.append({
            "section_idx": si,
            "removed_count": _remove_per_section[si],
            "is_current_target": si == _target_sec_idx,
        })
    if _multi_body_section:
        log.info(
            f"assemble: multi-body-section detected — "
            f"body_sections={_body_sections}, append_target={_target_sec_idx}"
        )

    structure["_section_info"] = {
        "section_count": _section_count,
        "remove_per_section": _remove_per_section,
        "append_target_section": _target_sec_idx,
        "current_append_policy": "max_remove_section",
        "body_sections": _body_sections,
        "append_target_candidates": _append_target_candidates,
        "multi_body_section_warning": _multi_body_section,
        "preserved_per_section": _preserved_per_section,
        "residual_candidates": _residual_candidates,
        "secpr_carrier_warnings": _secpr_carrier_warnings,
        "secpr_conflict_warnings": _secpr_conflict_warnings,
        **(_preserve_debug if _preserve_debug else {}),
        **(_unanalyzed_section_debug if _unanalyzed_section_debug else {}),
        **(_empty_chapter_preserve_debug if _empty_chapter_preserve_debug else {}),
        "removed_indices": sorted(_body_para_indices),
        "table_text_replacement_skipped_count": _table_text_skipped,
    }
    prev_role = None
    prev_level = None

    # marker rewrite: marker_policy 기반으로 AI text의 marker를 교체
    # (_marker_policies는 chapter_anchors loop 직전에서 이미 빌드됨 — 같은 dict 재사용)
    _marker_rewrite_log = []
    REWRITE_ALLOWED_POLICIES = {"arabic_sequence", "circled_sequence", "fixed_char"}

    # chapter route: _chapter_proc 결과 사용 (평탄화된 body_items, node_lookup,
    # chapter_title_roles는 chapter object의 title_item.role union)
    body_items = _chapter_proc["body_items"]
    _node_lookup = _chapter_proc["node_lookup"]
    _chapter_idx_lookup = _chapter_proc["chapter_idx_lookup"]
    _chapter_title_roles = _chapter_proc["chapter_title_roles"]
    _chapter_node_maps = _chapter_proc["chapter_node_maps"]
    _tree_available = _chapter_proc["tree_available"]
    _rewrite_alignment = _chapter_proc["rewrite_alignment"]

    # sibling counter: key = (chapter_idx, parent_id, role) → count
    _sibling_counter: dict[tuple, int] = {}
    # fallback counter (no tree): key = role → count
    _fallback_counter: dict[str, int] = {}

    # total chapter title count (arabic fallback strip cap)
    _total_chapter_titles = sum(
        1 for item in body_items if item.get("role", "") in _chapter_title_roles
    )

    # title_role이 grammar에도 등장하는지 (안전장치)
    _title_role_in_grammar = False
    _per_type_grammar = structure.get("template_grammar", {}).get("per_type", {})
    for _tg in _per_type_grammar.values():
        for _tr in _chapter_title_roles:
            if _tr in _tg.get("grammar", {}):
                _title_role_in_grammar = True
                break

    _chapter_title_counter = 0

    def _generate_chapter_title_marker(policy_type: str, idx: int, markers: list) -> str:
        """chapter title 전용 marker 생성. roman_sequence 포함."""
        if policy_type == "roman_sequence":
            # Ⅰ=0x2160 ... Ⅻ=0x216B (1~12)
            if 1 <= idx <= 12:
                return chr(0x215F + idx)
        if policy_type == "arabic_sequence":
            return str(idx)
        # 기타 sequence: markers 배열 범위 내면 사용
        if idx <= len(markers):
            return markers[idx - 1]
        log.warning(
            f"chapter_title_marker: {policy_type} index={idx} "
            f"exceeds generatable range, falling back to last observed"
        )
        return markers[-1] if markers else str(idx)

    def _normalize_chapter_title(text: str, role: str, chapter_idx: int,
                                  policy: dict | None) -> tuple[str, dict]:
        """chapter title leading marker를 strip하고 template marker로 교체."""
        import re as _re

        base_log = {
            "is_chapter_title": True,
            "chapter_title_index": chapter_idx,
            "original_text": text[:80],
            "role": role,
        }

        # 안전장치: title_role이 grammar에도 등장
        if _title_role_in_grammar:
            return text, {**base_log,
                "strip_strategy": "skipped_title_role_in_grammar",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": "", "separator_used": "",
                "possible_marker_duplication": False,
            }

        # policy 없거나 non-sequence
        if not policy:
            return text, {**base_log,
                "strip_strategy": "skipped_no_policy",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": "", "separator_used": "",
                "possible_marker_duplication": False,
            }

        policy_type = policy.get("policy_type", "")
        if policy.get("style") != "sequence":
            return text, {**base_log,
                "strip_strategy": "skipped_not_sequence",
                "rewrite_applied": False,
                "detected_leading_marker": "", "expected_marker": "",
                "stripped_content": text[:80], "rewritten_text": text[:80],
                "marker_policy_type": policy_type, "separator_used": "",
                "possible_marker_duplication": False,
            }

        markers = policy.get("markers", [])
        sep = policy.get("separator", " ")

        # expected marker
        if chapter_idx <= len(markers):
            expected = markers[chapter_idx - 1]
        else:
            expected = _generate_chapter_title_marker(policy_type, chapter_idx, markers)

        # strip
        stripped = text.lstrip()
        content = stripped
        detected = ""
        strip_strategy = "none"

        # 1순위: template known marker
        for m in sorted(markers, key=len, reverse=True):
            if stripped.startswith(m):
                detected = m
                content = stripped[len(m):].lstrip(". \t")
                strip_strategy = "template_marker"
                break

        # 2순위: arabic fallback (number <= total_chapter_titles)
        if not detected:
            m_dot = _re.match(r'^(\d+)\s*[.)]\s*', stripped)
            m_space = _re.match(r'^(\d+)\s+', stripped)
            match = m_dot or m_space
            if match:
                num = int(match.group(1))
                if 1 <= num <= _total_chapter_titles:
                    detected = match.group(1)
                    content = stripped[match.end():]
                    strip_strategy = "arabic_cap_limited"

        # possible_marker_duplication: strip 실패 + 앞이 숫자
        possible_dup = False
        if not detected and stripped and stripped[0].isdigit():
            possible_dup = True

        rewritten = f"{expected}{sep}{content}" if content else f"{expected}{sep}{stripped}"
        applied = rewritten != text

        return rewritten, {**base_log,
            "strip_strategy": strip_strategy,
            "detected_leading_marker": detected,
            "expected_marker": expected,
            "stripped_content": content[:80],
            "rewritten_text": rewritten[:80],
            "rewrite_applied": applied,
            "marker_policy_type": policy_type,
            "separator_used": sep,
            "possible_marker_duplication": possible_dup,
        }

    def _generate_sequence_marker(policy_type: str, sib_idx: int, markers: list) -> str:
        """markers 배열을 초과한 sibling_index에 대해 규칙형 마커를 직접 생성."""
        if policy_type == "arabic_sequence":
            return str(sib_idx)
        if policy_type == "num_paren_sequence":
            return f"{sib_idx})"
        if policy_type == "circled_sequence":
            # ➊=0x278A ... ➓=0x2793 (1~10)
            if 1 <= sib_idx <= 10:
                return chr(0x2789 + sib_idx)
        if policy_type == "circled_num_sequence":
            # ①=0x2460 ... ⑳=0x2473 (1~20)
            if 1 <= sib_idx <= 20:
                return chr(0x245F + sib_idx)
        # 생성 불가 — fallback to last observed
        log.warning(
            f"marker_rewrite: {policy_type} sibling_index={sib_idx} "
            f"exceeds generatable range, falling back to last observed"
        )
        return markers[-1] if markers else ""

    def _next_sibling_index(body_item_idx: int, role: str) -> tuple:
        """
        sibling_index를 계산하고 counter를 1회 증가. Single source of truth.
        Returns: (sib_idx, parent_id, parent_role, sibling_group_key, ch_idx, node)
        """
        node = _node_lookup.get(body_item_idx)
        ch_idx = _chapter_idx_lookup.get(body_item_idx)
        parent_id = None
        parent_role = None
        sibling_group_key = None

        if _tree_available and node is not None and ch_idx is not None:
            parent_id = node.get("parent_id")
            if parent_id is not None and ch_idx < len(_chapter_node_maps):
                parent_node = _chapter_node_maps[ch_idx].get(parent_id)
                if parent_node:
                    parent_role = parent_node.get("role")
            sibling_group_key = f"{ch_idx}_{parent_id}_{role}"
            counter_key = (ch_idx, parent_id, role)
            _sibling_counter[counter_key] = _sibling_counter.get(counter_key, 0) + 1
            sib_idx = _sibling_counter[counter_key]
        else:
            _fallback_counter[role] = _fallback_counter.get(role, 0) + 1
            sib_idx = _fallback_counter[role]
            sibling_group_key = f"fallback_{role}"

        return sib_idx, parent_id, parent_role, sibling_group_key, ch_idx, node

    def _rewrite_marker(body_item_idx: int, role: str, text: str,
                        sibling_index_override: int | None = None) -> str:
        """marker_policy에 따라 text의 leading marker를 교체.
        sibling_index_override가 주어지면 내부 counter를 사용하지 않음."""
        node = _node_lookup.get(body_item_idx)
        ch_idx = _chapter_idx_lookup.get(body_item_idx)

        # chapter title → 전용 normalization + fallback counter 리셋
        if role in _chapter_title_roles:
            _fallback_counter.clear()
            nonlocal _chapter_title_counter
            _chapter_title_counter += 1
            rewritten, log_entry = _normalize_chapter_title(
                text, role, _chapter_title_counter,
                _marker_policies.get(role),
            )
            _marker_rewrite_log.append(log_entry)
            return rewritten

        policy = _marker_policies.get(role)
        if not policy:
            return text

        markers = policy.get("markers", [])
        policy_type = policy.get("policy_type", "")
        marker_family = policy.get("family", "")
        sep = policy.get("separator", " ")

        if not markers:
            return text

        # star_depth: preview skip
        if policy_type == "star_depth":
            _marker_rewrite_log.append({
                "chapter_idx": ch_idx,
                "node_id": node["id"] if node else None,
                "role": role,
                "parent_id": node["parent_id"] if node else None,
                "parent_role": None,
                "sibling_group_key": None,
                "marker_policy_type": policy_type,
                "marker_family": marker_family,
                "sibling_index": None,
                "detected_marker": None,
                "expected_marker": None,
                "stripped_content": text[:80],
                "rewritten_text": text[:80],
                "marker_match": None,
                "rewrite_applied": False,
                "apply_reason": None,
                "skip_reason": "star_depth",
            })
            return text

        # sibling index: override가 있으면 사용, 없으면 직접 계산
        if sibling_index_override is not None:
            sib_idx = sibling_index_override
            # metadata는 node/ch_idx에서 직접 가져옴
            parent_id = None
            parent_role = None
            sibling_group_key = None
            if _tree_available and node is not None and ch_idx is not None:
                parent_id = node.get("parent_id")
                if parent_id is not None and ch_idx < len(_chapter_node_maps):
                    parent_node = _chapter_node_maps[ch_idx].get(parent_id)
                    if parent_node:
                        parent_role = parent_node.get("role")
                sibling_group_key = f"{ch_idx}_{parent_id}_{role}"
            else:
                sibling_group_key = f"fallback_{role}"
        else:
            # 기존 동작: 내부에서 counter 계산
            sib_idx, parent_id, parent_role, sibling_group_key, ch_idx, node = \
                _next_sibling_index(body_item_idx, role)

        # expected marker 결정
        if policy.get("style") == "sequence":
            if sib_idx <= len(markers):
                expected = markers[sib_idx - 1]
            else:
                # markers 배열 초과 — 규칙형 시퀀스는 직접 생성
                expected = _generate_sequence_marker(policy_type, sib_idx, markers)
        else:
            expected = markers[0]

        # text에서 기존 marker strip
        stripped = text.lstrip()
        stripped_content = stripped
        detected = ""
        # markers list + expected (overflow 대응)
        _detect_candidates = sorted(set(markers + [expected]), key=len, reverse=True)
        for m in _detect_candidates:
            if m and stripped.startswith(m):
                detected = m
                after = stripped[len(m):]
                if after and after[0] in (" ", "\t"):
                    stripped_content = after[1:]
                elif after:
                    stripped_content = after
                else:
                    stripped_content = ""
                break

        # rewritten text 계산
        if not detected and policy_type in ("roman_sequence", "arabic_sequence",
                                             "circled_sequence", "circled_pua_sequence"):
            rewritten = f"{expected}{sep}{stripped_content}" if stripped_content else f"{expected}"
        elif detected == expected:
            rewritten = text
        else:
            rewritten = f"{expected}{sep}{stripped_content}" if stripped_content else f"{expected}"

        marker_match = (detected == expected) if detected else None
        would_change = rewritten != text

        # apply/skip 판정
        if not enable_marker_rewrite:
            applied = False
            apply_reason = None
            skip_reason = "feature_flag_disabled"
        elif policy_type not in REWRITE_ALLOWED_POLICIES:
            applied = False
            apply_reason = None
            skip_reason = "policy_not_in_allowlist"
        elif not would_change:
            applied = False
            apply_reason = None
            skip_reason = "no_change_needed"
        else:
            applied = True
            apply_reason = "enabled_and_allowed"
            skip_reason = None

        _marker_rewrite_log.append({
            "chapter_idx": ch_idx,
            "node_id": node["id"] if node else None,
            "role": role,
            "parent_id": parent_id,
            "parent_role": parent_role,
            "sibling_group_key": sibling_group_key,
            "marker_policy_type": policy_type,
            "marker_family": marker_family,
            "sibling_index": sib_idx,
            "detected_marker": detected,
            "expected_marker": expected,
            "stripped_content": stripped_content[:80],
            "rewritten_text": rewritten[:80],
            "marker_match": marker_match,
            "rewrite_applied": applied,
            "apply_reason": apply_reason,
            "skip_reason": skip_reason,
        })

        return rewritten if applied else text

    # Phase 2: content_only_mode reattach + rewrite safety net
    _phase2_rewrite_conflicts = []
    _phase2_ai_marker_residuals = 0

    # 13.7b: deepcopy element의 unique id 재할당 counter
    # 양식 원본 paragraph id가 동일값 default이면 deepcopy 시 중복 → HWPX 위조/변조 경고.
    # 큰 값에서 시작 (양식 원본 id와 충돌 회피).
    _assembly_id_counter = [3_000_000_000]

    for bi_idx, item in enumerate(body_items):
        role = item.get("role", "")
        text = item.get("text", "")

        # 13.7b §4: chapter-local exemplar 우선 (chapter 자기 영역 paragraph 본보기)
        # outer exemplars (양식 전체 일반화)는 fallback. §4 chapter-local pattern preservation
        # 진짜 실현 — Ⅰ장 body는 Ⅰ장 트리 안 paragraph 본보기로, section 4 chapter는
        # section 4 paragraph 본보기로.
        _ci_for_role = _chapter_idx_lookup.get(bi_idx, -1) if _chapter_idx_lookup else -1
        _ch_obj_for_role = (
            _chapter_objects[_ci_for_role]
            if (_chapter_objects and _ci_for_role is not None and 0 <= _ci_for_role < len(_chapter_objects))
            else None
        )

        # chapter-local exemplar 검색 (chapter_local_exemplars param)
        _local_exemplar_el = None
        if (
            chapter_local_exemplars
            and _ci_for_role is not None
            and _ci_for_role in chapter_local_exemplars
        ):
            _local_info = chapter_local_exemplars[_ci_for_role]
            _local_sec_id = _local_info.get("section_id", 0)
            _role_to_xml = _local_info.get("role_to_xml_idx", {}) or {}
            if role in _role_to_xml:
                _xml_idx = _role_to_xml[role]
                if (
                    _local_sec_id in _section_top_level_paragraphs
                    and 0 <= _xml_idx < len(_section_top_level_paragraphs[_local_sec_id])
                ):
                    _local_exemplar_el = _section_top_level_paragraphs[_local_sec_id][_xml_idx]

        if _local_exemplar_el is not None:
            # chapter-local exemplar 사용 → chapter별 unique key로 등록
            _per_chapter_role_key = f"{role}__ci{_ci_for_role}__local"
            if _per_chapter_role_key not in exemplars:
                exemplars[_per_chapter_role_key] = _local_exemplar_el
                role_is_table_box[_per_chapter_role_key] = False  # local exemplar는 일반 paragraph 가정
            role = _per_chapter_role_key
            log.info(
                f"[13.7b §4 chapter-local exemplar] ci={_ci_for_role} role={item.get('role')} "
                f"sec={_local_sec_id} xml_idx={_xml_idx}"
            )

        # section N placeholder fallback (chapter-local exemplar 없을 때 chapter title anchor 사용)
        _is_section_n_placeholder = (
            _local_exemplar_el is None  # local exemplar 못 찾았을 때만
            and _ch_obj_for_role is not None
            and isinstance(_ch_obj_for_role, dict)
            and _ch_obj_for_role.get("section_id", 0) != 0
            and _ch_obj_for_role.get("status") == "empty"
            and _ci_for_role in chapter_anchors
        )
        if _is_section_n_placeholder:
            _anchor_for_role = chapter_anchors[_ci_for_role]
            _per_chapter_role_key = f"{role}__ci{_ci_for_role}"
            if _per_chapter_role_key not in exemplars:
                exemplars[_per_chapter_role_key] = _anchor_for_role
                role_is_table_box[_per_chapter_role_key] = False
            role = _per_chapter_role_key  # chapter-specific anchor 사용

        if role not in exemplars:
            # legacy fallback: section 0 chapter의 placeholder가 outer exemplars에 role 없을 때
            _ci_for_role_legacy = _ci_for_role
            _section_n_fallback = False
            if (
                _ci_for_role_legacy is not None
                and _ci_for_role_legacy >= 0
                and _ci_for_role_legacy in chapter_anchors
            ):
                _anchor_for_role_legacy = chapter_anchors[_ci_for_role_legacy]
                if _anchor_for_role_legacy is not None:
                    _per_chapter_role_key_legacy = f"{role}__ci{_ci_for_role_legacy}"
                    if _per_chapter_role_key_legacy not in exemplars:
                        exemplars[_per_chapter_role_key_legacy] = _anchor_for_role_legacy
                        role_is_table_box[_per_chapter_role_key_legacy] = False
                    role = _per_chapter_role_key_legacy
                    _section_n_fallback = True
            if not _section_n_fallback:
                errors.append(f"unknown role '{role}', skipping: {text[:50]}")
                continue

        # 13.7b §4 outer fallback safety: 만약 사용할 exemplar가 표(tbl) 포함 element이고
        # chapter_local이 아닌 outer exemplar라면 → table_kind 조회 후 real_table만 skip.
        # 양식 chapter title의 진짜 데이터 표가 body로 떨어지는 wrong 방지가 본래 의도.
        # 1f AI가 판단한 table_kind를 사용 (decorative_box vs real_table).
        # cluster_5/13/14/15처럼 "박스 형 paragraph"(자기 안에 강조용 tbl 배너 포함)는 통과
        # 시키고, _set_cloned_element_text의 tbl 안 text 교체 분기로 정상 처리.
        _final_exemplar = exemplars.get(role)
        if _final_exemplar is not None and "__ci" not in role:
            # outer exemplar (chapter-local 아님). 표 포함 여부 확인
            _has_tbl_in_exemplar = False
            for _c in _final_exemplar.iter():
                _ctag = _c.tag.split("}")[-1] if "}" in _c.tag else _c.tag
                if _ctag == "tbl":
                    _has_tbl_in_exemplar = True
                    break
            if _has_tbl_in_exemplar and _ci_for_role is not None and _ci_for_role >= 0:
                # table_kind 조회 (1f AI 판단 결과). missing/decorative_box/not_applicable → 통과
                _role_orig = item.get("role", "")
                _policy_for_tk = _marker_policies.get(_role_orig) or {}
                _table_kind = _policy_for_tk.get("table_kind", "not_applicable")
                if _table_kind == "real_table":
                    log.warning(
                        f"[13.7b §4] outer fallback exemplar는 real_table — skip. "
                        f"role={_role_orig!r} ci={_ci_for_role}. wrong text 방지."
                    )
                    continue
                # decorative_box / not_applicable / missing → 통과 (1f 판단 신뢰)
                log.info(
                    f"[13.7b §4] outer exemplar has tbl but table_kind={_table_kind} — 통과. "
                    f"role={_role_orig!r} ci={_ci_for_role}."
                )

        # 2c 분리 (2026-05-22): 마커/강조 markup은 2c가 text 안에 이미 입혀서 보냄.
        # 조립은 text 그대로 본보기에 박기만 함. strip/reattach/sibling counter/marker rewrite
        # 모두 제거. _body_marker_text/_body_content_text는 호환성 변수로만 남김.
        _body_marker_text = ""
        _body_content_text = text

        cur_level = role_level.get(role, 0)

        # ── blank_rules 적용: 전환 관계에 따라 빈 줄 삽입 ──
        if prev_role is not None and prev_level is not None:
            if cur_level == prev_level:
                relation = "sibling"
            elif cur_level > prev_level:
                relation = "descent"
            else:
                relation = "ascent"
            rule = blank_lookup.get((prev_role, role, relation))
            if rule and rule.get("has_blank"):
                paraPr = rule.get("paraPrIDRef") or "0"
                blank_el = (
                    blank_exemplars.get(paraPr)
                    or blank_exemplars.get("0")
                    or (next(iter(blank_exemplars.values())) if blank_exemplars else None)
                )
                if blank_el is not None:
                    # region-aware placement: chapter route는 chapter_anchors[ci] 뒤에 insert.
                    # body item과 같은 방식. 이전엔 section_elem.append로 chapter 영역 밖
                    # section 끝에 몰리던 버그 (13.7d region-aware placement 도입 후 비대칭).
                    _ci_for_blank = (
                        _chapter_idx_lookup.get(bi_idx, -1)
                        if _chapter_idx_lookup else -1
                    )
                    _blank_copy = deepcopy(blank_el)
                    # unique id 재할당 (양식 원본 id 중복 방지)
                    _reassign_unique_ids(_blank_copy, _assembly_id_counter)
                    _blank_placed = False
                    if (
                        _ci_for_blank is not None
                        and _ci_for_blank >= 0
                        and _ci_for_blank in chapter_anchors
                    ):
                        _anchor_for_blank = chapter_anchors[_ci_for_blank]
                        _owning_sec_for_blank = _elem_to_section.get(_anchor_for_blank)
                        if _owning_sec_for_blank is not None:
                            try:
                                _children_b = list(_owning_sec_for_blank)
                                _idx_in_parent_b = _children_b.index(_anchor_for_blank)
                                _owning_sec_for_blank.insert(_idx_in_parent_b + 1, _blank_copy)
                                # cursor를 blank로 update → 다음 body item이 blank 뒤에 들어감
                                chapter_anchors[_ci_for_blank] = _blank_copy
                                _elem_to_section[_blank_copy] = _owning_sec_for_blank
                                _blank_placed = True
                            except (ValueError, AttributeError) as _be_e:
                                log.warning(
                                    f"blank line region-aware insert fail "
                                    f"(ci={_ci_for_blank}, bi_idx={bi_idx}): {_be_e}"
                                )
                    if not _blank_placed:
                        # fallback: chapter context 없음 (shallow route 등) → section_elem 끝
                        section_elem.append(_blank_copy)
                        _elem_to_section[_blank_copy] = section_elem

        # 2c가 들여쓰기까지 책임 (2026-05-22): 자식 도구가 양식 sample 들여쓰기
        # 그대로 따라 출력. 조립은 그 들여쓰기를 보존만 함. format_rules.indent_parts
        # 강제 무시 — cluster 단위 평균/첫값이 paragraph 다양성과 안 맞는 문제 해결.
        clean_text = text  # lstrip 안 함 — 자식 도구 들여쓰기 보존
        space_prefix = ""  # 코드 들여쓰기 추가 안 함
        num_tabs = 0       # tab 추가 안 함

        # exemplar 복제
        new_elem = deepcopy(exemplars[role])
        # 13.7b: deepcopy element의 paragraph/tbl 등 id 재할당 (unique 보장)
        # 양식 원본 id 그대로 두면 중복 → HWPX 위조/변조 경고
        _reassign_unique_ids(new_elem, _assembly_id_counter)

        # 텍스트 교체 (공백 prefix 포함)
        try:
            is_tbl_box = role_is_table_box.get(role, False)
            # 13.3 table policy: shallow route(preserve_indices 있음)에서
            # table-like role은 structural placeholder — content generation 대상 아님.
            # exemplar clone으로 표 구조만 보존, cell text replacement skip.
            # table cell filling은 별도 table stage(14-table)에서 처리.
            # chapter route(preserve_indices=None)에서는 기존 동작 유지.
            if is_tbl_box and preserve_indices:
                _table_text_skipped += 1
            else:
                # Sprint 3D: emphasis-aware body text 박기
                # 매핑 정확성: emphasis_layers[role] lookup → cluster의 charpr_map 빌드.
                # valid_layer_ids는 그 cluster에 정의된 layer만 허용 (AI 환각 markup 무시).
                # 13.7b §4 fix (2026-05-24): chapter-local / section_n_placeholder 사용 시
                # role 이 "{원래role}__ci{N}__local" 또는 "{원래role}__ci{N}" 으로 rename 됨.
                # emphasis_layers lookup 은 원래 role 사용 — 안 그러면 cluster_20 같이 emphasis
                # 정의된 cluster 의 markup 이 split path 로 빠져 [[emN]] 글자 그대로 박힘.
                _role_for_em_lookup = role.split("__ci")[0] if "__ci" in role else role
                _body_cluster_em = (emphasis_layers or {}).get(_role_for_em_lookup) or {}
                # 진단: emphasis lookup fail 시 stderr log + debug 파일 (2026-05-25)
                # 텍스트에 [[em..]] markup 있는데 cluster lookup fail 하면 본문에
                # markup 그대로 박힘. 어떤 role/lookup key 가 fail 하는지 추적.
                if not _body_cluster_em and "[[em" in (text or ""):
                    try:
                        import os as _em_diag_os, json as _em_diag_json
                        _em_diag_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                        with open("/tmp/hwpx_debug/em_lookup_fail.jsonl", "a", encoding="utf-8") as _em_f:
                            _em_f.write(_em_diag_json.dumps({
                                "role_raw": role,
                                "role_lookup": _role_for_em_lookup,
                                "available_keys": sorted((emphasis_layers or {}).keys())[:10],
                                "available_count": len(emphasis_layers or {}),
                                "text_preview": (text or "")[:200],
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                _body_charpr_map: dict = {}
                _body_valid_layers: set = set()
                if _body_cluster_em:
                    _base_cp_body = _body_cluster_em.get("base_charpr_id", "") or ""
                    _base_lid_body = _body_cluster_em.get("base_layer_id", "") or ""
                    _body_charpr_map["base"] = _base_cp_body
                    # 2c가 base에도 markup 박는 기본 동작 — base_layer_id도 valid_layers에 추가
                    if _base_lid_body and _base_cp_body:
                        _body_charpr_map[_base_lid_body] = _base_cp_body
                        _body_valid_layers.add(_base_lid_body)
                    for _el in (_body_cluster_em.get("emphasis_layers") or []):
                        _lid = _el.get("layer_id", "") or ""
                        _cp = _el.get("charpr_id", "") or ""
                        if _lid and _cp:
                            _body_charpr_map[_lid] = _cp
                            _body_valid_layers.add(_lid)

                # tbl_box인 경우 cell paragraph 찾기 (_set_cloned_element_text tbl 분기 로직)
                _body_target_p = new_elem
                if is_tbl_box:
                    _trs = new_elem.findall(f".//{NS}tr")
                    if _trs:
                        _tcs = _trs[0].findall(f"{NS}tc")
                        if _tcs:
                            _target_tc = _tcs[-1] if len(_tcs) > 1 else _tcs[0]
                            _sublist = _target_tc.find(f"{NS}subList")
                            if _sublist is not None:
                                _cell_paras = _sublist.findall(f"{NS}p")
                                if _cell_paras:
                                    _body_target_p = _cell_paras[0]
                                    for _ep in _cell_paras[1:]:
                                        _sublist.remove(_ep)

                _body_marker_with_indent = (space_prefix + _body_marker_text) if _body_marker_text else ""

                # AI text의 valid emphasis markup 미리 parse — 0개면 emphasis path 우회.
                # markup 0개에 emphasis path를 돌면 segments=[(None, 전체)]가 되어
                # cluster base로 박혀 양식 본래 글꼴 잃음. Sprint 2B path가 양식 글꼴 보존.
                # 들여쓰기 책임 분리 (2026-05-27): AI text 의 leading whitespace 는 strip.
                # cluster 표준 indent (extract_paragraph_emphasis_map 의 indent_length_mode +
                # indent_layer_majority_charpr) 가 있으면 segments 맨 앞에 자동 prepend.
                _content_for_path = _body_content_text.lstrip(" \t") if _body_marker_text else clean_text
                _content_for_path = _content_for_path.lstrip(" \t")
                _pre_segments = _parse_emphasis_markup(_content_for_path, _body_valid_layers) if _body_valid_layers else []

                # indent 정보는 paragraph_emphasis_map 의 cluster entry 에 있음
                # (emphasis_layers 는 AI 파싱 결과로 indent 정보 없음)
                _body_pem_for_indent = (paragraph_emphasis_map or {}).get(_role_for_em_lookup) or {}
                _indent_mode = _body_pem_for_indent.get("indent_length_mode", 0) or 0
                _indent_lid = _body_pem_for_indent.get("indent_layer_majority_id")
                _indent_cp_majority = _body_pem_for_indent.get("indent_layer_majority_charpr")
                if _indent_mode and _indent_lid and _indent_cp_majority:
                    _body_charpr_map.setdefault(_indent_lid, _indent_cp_majority)
                    _body_valid_layers.add(_indent_lid)
                    _indent_seg = (_indent_lid, " " * int(_indent_mode))
                    if _pre_segments == [(None, "")]:
                        _pre_segments = [_indent_seg]
                    else:
                        _pre_segments = [_indent_seg] + _pre_segments

                _has_valid_em = any(s[0] is not None for s in _pre_segments)

                if _body_valid_layers and _has_valid_em:
                    # emphasis-aware: markup parse + run split with charpr 매핑
                    _replace_text_with_emphasis_segments(
                        _body_target_p,
                        _body_marker_with_indent,
                        _pre_segments,
                        _body_charpr_map,
                        NS,
                    )
                elif _body_marker_text:
                    # Sprint 2B split (emphasis 없는 cluster 또는 markup 0개)
                    _replace_text_in_paragraph_elem_split(
                        _body_target_p,
                        _body_marker_with_indent,
                        _content_for_path,
                        NS,
                    )
                else:
                    # no marker no emphasis — 기존 path
                    if is_tbl_box:
                        _set_cloned_element_text(new_elem, space_prefix + clean_text, NS, is_tbl_box)
                    else:
                        _replace_text_in_paragraph_elem(_body_target_p, space_prefix + clean_text, NS)

            # 탭 삽입 (table_box가 아닐 때만)
            if num_tabs > 0 and not is_tbl_box:
                runs = new_elem.findall(f"{NS}run")
                if runs:
                    first_run = runs[0]
                    t_elem = first_run.find(f"{NS}t")
                    if t_elem is not None:
                        t_index = list(first_run).index(t_elem)
                        for _ in range(num_tabs):
                            tab_elem = etree.Element(f"{NS}tab")
                            first_run.insert(t_index, tab_elem)
                            t_index += 1

            # 13.7d: region-aware placement (chapter_anchors 기반)
            # 13.7b: cross-section bleed hard fail — fallback도 chapter.section_id에만 허용
            _ci = _chapter_idx_lookup.get(bi_idx, -1) if _chapter_idx_lookup else -1
            _ch_obj_for_bi = (
                _chapter_objects[_ci]
                if (_chapter_objects and _ci is not None and 0 <= _ci < len(_chapter_objects))
                else None
            )
            _target_section_id_for_bi = (
                _ch_obj_for_bi.get("section_id", 0)
                if isinstance(_ch_obj_for_bi, dict) else None
            )
            # 13.7b: placement_failure인 chapter의 body items skip (hard fail)
            if _ci is not None and _ci in _placement_failed_chapter_indices:
                log.warning(
                    f"[13.7b] body item skip — chapter {_ci} placement_failed "
                    f"(bi_idx={bi_idx}, role={role!r})"
                )
                continue
            _placed_region_aware = False
            if _ci is not None and _ci >= 0 and _ci in chapter_anchors:
                _anchor = chapter_anchors[_ci]
                _owning_sec = _elem_to_section.get(_anchor)
                if _owning_sec is not None:
                    # 13.7b invariant: owning section == chapter.section_id 강제
                    _owning_sec_pos = -1
                    for _si, _s in enumerate(_all_sections):
                        if _s.element is _owning_sec:
                            _owning_sec_pos = _si
                            break
                    if (
                        _target_section_id_for_bi is not None
                        and _owning_sec_pos != _target_section_id_for_bi
                    ):
                        errors.append(
                            f"cross_section_bleed_blocked: chapter {_ci} section_id="
                            f"{_target_section_id_for_bi}, but anchor owning section="
                            f"{_owning_sec_pos}. body item bi_idx={bi_idx} not inserted."
                        )
                        log.error(
                            f"[13.7b CROSS_SECTION_BLEED ci={_ci}] target_sec="
                            f"{_target_section_id_for_bi}, anchor_sec={_owning_sec_pos} — body item skip"
                        )
                    else:
                        try:
                            _children = list(_owning_sec)
                            _idx_in_parent = _children.index(_anchor)
                            _owning_sec.insert(_idx_in_parent + 1, new_elem)
                            chapter_anchors[_ci] = new_elem  # cursor update
                            _elem_to_section[new_elem] = _owning_sec
                            success_count += 1
                            _placed_region_aware = True
                        except (ValueError, AttributeError) as _ria_e:
                            log.warning(
                                f"region-aware insert fail (chapter {_ci}, bi_idx {bi_idx}): {_ria_e}"
                            )
                else:
                    log.warning(
                        f"chapter {_ci} anchor not in any section (table cell 등). "
                        f"target_section_id={_target_section_id_for_bi} fallback append 시도"
                    )

            if not _placed_region_aware:
                # 13.7b fallback append: chapter.section_id 의 section element에 허용
                # shallow route는 chapter_objects 없음 → section_elem (max_remove) fallback (기존 동작 유지)
                if (
                    _target_section_id_for_bi is not None
                    and 0 <= _target_section_id_for_bi < len(_all_sections)
                ):
                    _fb_section_elem = _all_sections[_target_section_id_for_bi].element
                    _fb_section_elem.append(new_elem)
                    _elem_to_section[new_elem] = _fb_section_elem
                    success_count += 1
                    log.info(
                        f"[13.7b fallback append] ci={_ci} bi_idx={bi_idx} → "
                        f"section_id={_target_section_id_for_bi} end"
                    )
                else:
                    # chapter route인데 chapter context 없음 → hard fail (cross-section bleed 위험)
                    errors.append(
                        f"orphan body item (no chapter context) bi_idx={bi_idx}, "
                        f"role={role!r}, text={text[:50]!r}. cross-section bleed 차단 — body item skip."
                    )
                    log.error(
                        f"[13.7b ORPHAN_BODY] bi_idx={bi_idx} role={role!r} — body item skip"
                    )
        except Exception as e:
            errors.append(f"assemble({role}): {e}")

        prev_role = role
        prev_level = cur_level

    # marker rewrite log + alignment를 structure에 저장 (debug용)
    changed = sum(1 for r in _marker_rewrite_log if r.get("changed"))

    # chapter_split: chapter object 단위 직접 처리 (split 없음).
    _rewrite_alignment["chapter_split"] = {
        "split_method": "chapter_objects_direct",
        "tree_split_available": True,
        "tree_scan_agreement": None,
    }
    _mr_total = len(_marker_rewrite_log)
    _mr_title = sum(1 for r in _marker_rewrite_log if r.get("is_chapter_title"))
    _mr_applied = sum(1 for r in _marker_rewrite_log if r.get("rewrite_applied"))
    _mr_skip = {}
    for r in _marker_rewrite_log:
        sr = r.get("skip_reason")
        if sr:
            _mr_skip[sr] = _mr_skip.get(sr, 0) + 1
    _rewrite_alignment["marker_rewrite"] = {
        "total_entries": _mr_total,
        "chapter_title_entries": _mr_title,
        "body_entries": _mr_total - _mr_title,
        "applied_count": _mr_applied,
        "not_applied_count": _mr_total - _mr_applied,
        "skip_reason_counts": _mr_skip,
    }

    structure["_marker_rewrite_log"] = _marker_rewrite_log
    structure["_rewrite_alignment"] = _rewrite_alignment
    if content_only_mode:
        structure["_phase2_reattach_result"] = {
            "content_only_mode": True,
            "rewrite_conflict_count": len(_phase2_rewrite_conflicts),
            "ai_marker_residual_count": _phase2_ai_marker_residuals,
            "rewrite_conflicts": _phase2_rewrite_conflicts[:20],
        }
    # ── section dirty marking: save 시 serialize 대상에 포함 ──
    # removal 또는 append가 발생한 section만 dirty 처리
    _dirty_section_indices: set[int] = set()
    # removal이 발생한 section
    for si, cnt in _remove_per_section.items():
        if cnt > 0:
            _dirty_section_indices.add(si)
    # append가 발생한 section (target section)
    if success_count > 0:
        _dirty_section_indices.add(_target_sec_idx)

    for si in _dirty_section_indices:
        if si < len(_all_sections):
            _all_sections[si].mark_dirty()

    structure["_dirty_marking"] = {
        "dirty_section_indices": sorted(_dirty_section_indices),
        "dirty_section_count": len(_dirty_section_indices),
        "mark_dirty_applied": len(_dirty_section_indices) > 0,
    }
    log.info(
        f"section dirty marking: indices={sorted(_dirty_section_indices)}, "
        f"count={len(_dirty_section_indices)}"
    )

    log.info(
        f"하이브리드 조립 완료: 성공 {success_count}, 실패 {len(errors)}, "
        f"body 항목 {len(body_items)}개, marker rewrite {changed}/{len(_marker_rewrite_log)}"
        + (f", phase2: conflicts={len(_phase2_rewrite_conflicts)}, residuals={_phase2_ai_marker_residuals}" if content_only_mode else "")
    )

    # ── final unique id reassignment ──
    # 양식 원본 paragraph id가 default (0, 2147483648 등)이라 preserve된 영역에
    # 중복 id 다수 → HWPX viewer 위변조 경고. 모든 section element의 id를 unique
    # sequential로 재할당. 빈 문자열 id (subList 등 schema 의도)는 skip.
    try:
        _all_section_elements = []
        for _s in _all_sections:
            try:
                _all_section_elements.append(_s.element if hasattr(_s, 'element') else _s)
            except Exception:
                pass
        _final_reassign = _reassign_all_section_ids(_all_section_elements, counter_start=4_000_000_000)
        structure["_final_id_reassignment"] = _final_reassign
        log.info(
            f"final id reassignment: reassigned={_final_reassign['reassigned_count']} "
            f"skipped_empty={_final_reassign['skipped_empty_count']}"
        )
    except Exception as _fri_e:
        log.warning(f"final id reassignment 실패: {_fri_e}")
        structure["_final_id_reassignment"] = {"error": str(_fri_e)}

    # ── TOC 텍스트 교체 (신 2a 가 결정한 toc_replacements 적용) ──
    # multi-paragraph schema (2026-05-25): 각 entry = (p_idx, t_idx, new_text).
    # p_idx 는 양식 xml top-level paragraph index — doc.paragraphs idx 와 동일.
    # 코드는 그 (p_idx, t_idx) 의 .text 만 set — substring 매칭/공백 처리 없음.
    if toc_replacements:
        try:
            # p_idx 별로 group — paragraph 한 번만 access 하도록 cache
            _toc_applied = 0
            _toc_skipped = 0
            _p_elem_cache: dict = {}
            for _repl in toc_replacements:
                if not isinstance(_repl, dict):
                    _toc_skipped += 1
                    continue
                _p_idx_repl = _repl.get("p_idx")
                _t_idx = _repl.get("t_idx")
                _new_text = _repl.get("new_text", "")
                if not isinstance(_t_idx, int) or _t_idx < 0:
                    _toc_skipped += 1
                    continue
                # p_idx 없으면 옛 schema — toc_paragraph_idx (1a idx) 로 fallback
                if _p_idx_repl is None:
                    if toc_paragraph_idx is None:
                        _toc_skipped += 1
                        continue
                    _p_real_idx = _to_real_idx(toc_paragraph_idx)
                else:
                    if not isinstance(_p_idx_repl, int):
                        _toc_skipped += 1
                        continue
                    # 양식 xml top-level p idx == doc.paragraphs idx 가정
                    _p_real_idx = _p_idx_repl
                if not (0 <= _p_real_idx < len(doc.paragraphs)):
                    _toc_skipped += 1
                    continue
                # paragraph element 의 t_elems cache
                if _p_real_idx not in _p_elem_cache:
                    _p_elem = doc.paragraphs[_p_real_idx].element
                    _p_elem_cache[_p_real_idx] = list(_p_elem.iter(f"{NS}t"))
                _t_elems = _p_elem_cache[_p_real_idx]
                if _t_idx >= len(_t_elems):
                    _toc_skipped += 1
                    continue
                _t_elems[_t_idx].text = str(_new_text)
                _toc_applied += 1
            log.info(
                f"TOC text 교체: applied={_toc_applied}, skipped={_toc_skipped}, "
                f"touched_paragraphs={len(_p_elem_cache)}"
            )
        except Exception as _toc_e:
            log.warning(f"TOC text 교체 실패: {_toc_e}")

    return HwpxResult(
        data=_doc_to_bytes(doc),
        success_count=success_count,
        fail_count=len(errors),
        errors=errors,
    )


def _strip_secpr(elem, NS: str):
    """
    복제된 요소에서 secPr(섹션 속성) 요소를 제거합니다.
    secPr이 있는 문단을 clone하면 매번 새 섹션이 시작되어
    불필요한 페이지 나누기가 발생합니다.
    """
    for parent in elem.iter():
        for secpr in list(parent.findall(f"{NS}secPr")):
            parent.remove(secpr)


def _strip_linesegarray(elem, NS: str):
    """
    복제된 exemplar에서 linesegarray 요소를 모두 제거합니다.
    linesegarray는 줄 위치 좌표(고정값)로, 다른 길이의 텍스트가 들어가면
    글자가 겹치는 원인이 됩니다. 제거하면 한글 뷰어가 자동 재계산합니다.
    """
    for parent in elem.iter():
        for lsa in list(parent.findall(f"{NS}linesegarray")):
            parent.remove(lsa)


def _strip_document_ctrls(elem, NS: str):
    """
    복제된 exemplar에서 document-level ctrl 요소를 제거합니다.
    footer, header, newNum, pageHiding 등은 문서에 한 번만 존재해야 하므로
    exemplar를 clone할 때 중복 생성되지 않도록 제거합니다.
    """
    remove_types = {"footer", "header", "newNum", "pageHiding"}
    parent_map = _build_parent_map(elem)
    for ctrl in elem.findall(f".//{NS}ctrl"):
        for child in list(ctrl):
            local_name = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local_name in remove_types:
                run = parent_map.get(ctrl)
                if run is not None:
                    run_parent = parent_map.get(run)
                    if run_parent is not None:
                        run_parent.remove(run)
                break


def _set_element_text(para, text: str, NS: str):
    """기존 문단(HwpxOxmlParagraph)의 텍스트를 교체합니다."""
    from lxml import etree

    # 표가 있는 문단이면 텍스트가 있는 셀을 찾아 교체
    if para.tables:
        try:
            tbl = para.tables[0]
            target_cell = None
            for r in range(tbl.row_count):
                for c in range(tbl.col_count):
                    cell = tbl.cell(r, c)
                    cell_text = "".join(
                        p.text for p in cell.paragraphs if p.text
                    ).strip()
                    if cell_text:
                        target_cell = cell
                        break
                if target_cell:
                    break
            if target_cell is None:
                target_cell = tbl.cell(0, 0)
            cell_paras = target_cell.paragraphs
            if cell_paras:
                cell_paras[0].text = text
                for cp in cell_paras[1:]:
                    cp.text = ""
            return
        except Exception as e:
            log.warning(f"_set_element_text 라이브러리 API 실패, XML 직접 접근: {e}")

    # XML 직접 접근 fallback — 표/container 내부에서 텍스트가 있는 셀을 찾아 교체
    elem = para.element
    # 표 내부 셀에서 텍스트 찾기
    for tc in elem.findall(f".//{NS}tc"):
        sublist = tc.find(f"{NS}subList")
        if sublist is None:
            continue
        for p in sublist.findall(f"{NS}p"):
            for t in p.findall(f".//{NS}t"):
                if t.text and t.text.strip():
                    # 텍스트가 있는 셀 발견 — 첫 문단 교체
                    _replace_text_in_paragraph_elem(sublist.findall(f"{NS}p")[0], text, NS)
                    # 나머지 문단 텍스트 비우기
                    for extra_p in sublist.findall(f"{NS}p")[1:]:
                        for et in extra_p.findall(f".//{NS}t"):
                            et.text = ""
                            for child in list(et):
                                et.remove(child)
                    return

    # 일반 문단
    para.text = text


def _set_cloned_element_text(elem, text: str, NS: str, is_table_box: bool):
    """deepcopy된 XML 요소의 텍스트를 교체합니다."""

    # 1) 표(tbl) 내부 텍스트 교체
    if is_table_box:
        trs = elem.findall(f".//{NS}tr")
        if trs:
            tcs = trs[0].findall(f"{NS}tc")
            if tcs:
                target_tc = tcs[-1] if len(tcs) > 1 else tcs[0]
                sublist = target_tc.find(f"{NS}subList")
                if sublist is not None:
                    paras = sublist.findall(f"{NS}p")
                    if paras:
                        _replace_text_in_paragraph_elem(paras[0], text, NS)
                        for p in paras[1:]:
                            sublist.remove(p)
                    return

    # 2) container(그리기 객체) 내부 텍스트 교체
    #    container > rect/... > drawText > subList > p 구조
    draw_texts = elem.findall(f".//{NS}drawText")
    if draw_texts:
        # 가장 텍스트가 많은 drawText를 선택 (제목 박스가 아닌 본문 영역)
        target_dt = None
        max_text_len = 0
        for dt in draw_texts:
            sub = dt.find(f"{NS}subList")
            if sub is not None:
                dt_text = ""
                for p in sub.findall(f"{NS}p"):
                    for t in p.findall(f".//{NS}t"):
                        if t.text:
                            dt_text += t.text
                if len(dt_text.strip()) > max_text_len:
                    max_text_len = len(dt_text.strip())
                    target_dt = dt
        # 텍스트 있는 drawText가 없으면 마지막 drawText 사용
        if target_dt is None and draw_texts:
            target_dt = draw_texts[-1]
        if target_dt is not None:
            sub = target_dt.find(f"{NS}subList")
            if sub is not None:
                paras = sub.findall(f"{NS}p")
                if paras:
                    _replace_text_in_paragraph_elem(paras[0], text, NS)
                    for p in paras[1:]:
                        sub.remove(p)
                return

    # 3) 일반 문단의 텍스트 교체
    _replace_text_in_paragraph_elem(elem, text, NS)


def _replace_text_in_paragraph_elem(p_elem, text: str, NS: str):
    """XML paragraph 요소 내부의 텍스트를 교체합니다.

    13.7d-fix: paragraph 안 descendant t element 전부 처리 (table cell 안 t 포함).
    양식이 chapter title을 table box로 표현하는 경우, direct run.t만 보면 table 안 text가
    안 바뀌고 first_run에 새 t가 추가되어 양식 원본 + adapted 둘 다 출력되는 문제 fix.

    동작 (2026-05-21 chapter title 조립 통일):
    - paragraph 안 모든 descendant t element 수집 (table cell 안 t 포함)
    - **본문 자리 찾기**: 비공백 t 중 가장 긴 t를 target으로 (= 본문 글꼴 자리).
      첫 t에 통째 박지 않음 — 양식 paragraph가 마커/본문 글꼴 분리된 구조일 때
      마커 자리에 본문이 박혀 마커 서식으로 보이는 wrong 방지.
    - target에 text 박고 다른 비공백 t는 비움. **공백/탭만 있는 t는 들여쓰기 자리로 보존**.
    - t element 하나도 없으면 첫 run에 새 t SubElement 추가
    - ctrl 요소가 있는 run은 보존 (header, footer, pageNum 등)
    """
    import xml.etree.ElementTree as _stdlib_ET

    # descendant t element 전부 수집 (table cell 안 포함)
    t_elems = [el for el in p_elem.iter() if el.tag == f"{NS}t"]

    if t_elems:
        # 본문 자리 = 가장 긴 비공백 t (마커 자리/공백 자리 회피)
        bearing = [t for t in t_elems if (t.text or "").strip()]
        if bearing:
            target_t = max(bearing, key=lambda t: (len(t.text or ""), t_elems.index(t)))
        else:
            target_t = t_elems[0]  # 모두 빈 t — 첫 t fallback
        target_t.text = text
        for child in list(target_t):
            target_t.remove(child)
        # 나머지 비공백 t 비움. 공백/탭 only t는 양식 본래 들여쓰기 자리로 보존.
        for t in t_elems:
            if t is target_t:
                continue
            if not (t.text or "").strip():
                continue
            t.text = ""
            for child in list(t):
                t.remove(child)
    else:
        # t element 하나도 없으면 첫 run에 새 t SubElement 추가
        runs = p_elem.findall(f"{NS}run")
        if not runs:
            return
        new_t = _stdlib_ET.SubElement(runs[0], f"{NS}t")
        new_t.text = text

    # 13.7d: ctrl 없는 redundant run 제거는 보존 logic. 단 table 포함 run은 보존 (table 자체 keep).
    # cover table cell처럼 첫 run이 ctrl이고 second run에 first_t가 들어있는 경우,
    # has_t 체크 없이는 first_t를 set한 run이 통째로 제거되어 결과 텍스트가 사라진다.
    # 따라서 t element를 가진 run도 보존 (text는 위에서 적절히 set/clear됨).
    runs = p_elem.findall(f"{NS}run")
    for run in runs[1:]:
        has_ctrl = run.find(f"{NS}ctrl") is not None
        has_tbl = any(el.tag == f"{NS}tbl" for el in run.iter())
        has_t = any(el.tag == f"{NS}t" for el in run.iter())
        if not has_ctrl and not has_tbl and not has_t:
            p_elem.remove(run)


def _replace_text_in_paragraph_elem_split(p_elem, marker_text: str, content_text: str, NS: str):
    """Sprint 2B: marker text와 content text를 paragraph 안 다른 t element에 박음.

    양식 paragraph의 t element들은 글꼴(charPrIDRef)이 t마다 다를 수 있음.
    - 첫 t는 보통 마커 글꼴
    - 본문 글꼴 t는 보통 가장 긴 t
    - 중간 t는 구두점·공백 등 짧은 segment (글꼴 다양)

    동작:
    - text-bearing t (원본에 text 있던 것) 2개 이상:
        marker → 첫 bearing t (마커 글꼴 보존)
        content → 가장 긴 bearing t (본문 글꼴 보존)
        그 외 t는 비움
    - text-bearing t 1개: split 불가 → 합쳐서 단일 t에 박음
    - t element 없음: 첫 run에 새 t 추가
    - 중간 구두점/공백 글꼴은 손실 가능 (시각상 큰 영향 X)
    """
    import xml.etree.ElementTree as _stdlib_ET

    t_elems = [el for el in p_elem.iter() if el.tag == f"{NS}t"]

    if not t_elems:
        runs = p_elem.findall(f"{NS}run")
        if not runs:
            return
        new_t = _stdlib_ET.SubElement(runs[0], f"{NS}t")
        new_t.text = marker_text + content_text
    else:
        # text-bearing t만 (원본에 의미 있는 text 있던 것)
        bearing = [t for t in t_elems if (t.text or "").strip()]

        if len(bearing) >= 2:
            # marker_text 첫 글자와 양식 t.text 첫 글자가 일치하는 t를 marker 위치로.
            # 양식 텍스트박스(tbl cell) 안에 정교한 run 구조 (예: " "/"과제 1"/" "/"민생경제..."/...)
            # 인 경우, first bearing이 공백 t일 수 있어 marker가 잘못된 글꼴에 박힘.
            # 매칭되는 t 없으면 fallback to first bearing (이전 동작).
            marker_first = marker_text.strip()[:1] if marker_text.strip() else ""
            marker_t = None
            if marker_first:
                for t in bearing:
                    t_first = (t.text or "").strip()[:1]
                    if t_first and t_first == marker_first:
                        marker_t = t
                        break
            if marker_t is None:
                marker_t = bearing[0]

            # content → 가장 긴 bearing (marker_t 제외)
            # 공백+탭만인 t는 양식 들여쓰기 자리 — content_t 후보에서 제외.
            # 그렇지 않으면 본문이 들여쓰기 글꼴(작은 폰트) 자리로 박힘.
            non_marker = [t for t in bearing if t is not marker_t]
            content_candidates = [t for t in non_marker if (t.text or "").strip()]
            if content_candidates:
                content_t = max(
                    content_candidates,
                    key=lambda t: (len(t.text or ""), bearing.index(t)),
                )
            else:
                # 모든 non_marker가 공백 only — marker_t에 통째 박기 (들여쓰기는 보존)
                content_t = marker_t

            if content_t is marker_t:
                # 같은 t에 둘 다 박음 (분리 불가 fallback)
                marker_t.text = marker_text + content_text
                for child in list(marker_t):
                    marker_t.remove(child)
                # 나머지 t 비움 — 단 양식 본래 공백/탭 only t는 들여쓰기 자리이므로 보존
                for t in t_elems:
                    if t is marker_t:
                        continue
                    if not (t.text or "").strip():
                        continue
                    t.text = ""
                    for child in list(t):
                        t.remove(child)
            else:
                marker_t.text = marker_text
                for child in list(marker_t):
                    marker_t.remove(child)
                content_t.text = content_text
                for child in list(content_t):
                    content_t.remove(child)
                # 나머지 t 비움 정책:
                # - 마커 자리와 본문 자리 사이 공백/탭 only t는 양식 구분자 자리 — 비움
                #   (marker_text가 이미 구분자 포함, 보존하면 띄어쓰기 중복)
                # - 그 외 위치(맨 앞·끝) 공백/탭 only t는 들여쓰기/trailing 자리 — 보존
                # - 글자 있는 중간 t는 기존대로 비움
                _m_idx = t_elems.index(marker_t)
                _c_idx = t_elems.index(content_t)
                _between_lo = min(_m_idx, _c_idx)
                _between_hi = max(_m_idx, _c_idx)
                for _i, t in enumerate(t_elems):
                    if t is marker_t or t is content_t:
                        continue
                    _is_ws_only = not (t.text or "").strip()
                    _is_between = _between_lo < _i < _between_hi
                    if _is_ws_only and not _is_between:
                        continue
                    t.text = ""
                    for child in list(t):
                        t.remove(child)
        else:
            # bearing 1개 이하 — split 불가, 합쳐서 박음.
            # t_elems[0]에 박으면 양식 paragraph 첫 단위(보통 들여쓰기 공백)에 박혀
            # 작은 글꼴 자리에 들어감. bearing[0](실제 글자 있던 첫 단위)에 박아야
            # 양식 본래 본문 글꼴 자리 보존. bearing 0개면 t_elems[0] fallback.
            target_t = bearing[0] if bearing else t_elems[0]
            target_t.text = marker_text + content_text
            for child in list(target_t):
                target_t.remove(child)
            # 나머지 비움 — 단 양식 본래 공백/탭 only t는 들여쓰기 자리이므로 보존
            for t in t_elems:
                if t is target_t:
                    continue
                if not (t.text or "").strip():
                    continue
                t.text = ""
                for child in list(t):
                    t.remove(child)

    # ctrl 없는 빈 run 제거 (기존 _replace_text_in_paragraph_elem과 같은 로직)
    runs = p_elem.findall(f"{NS}run")
    for run in runs[1:]:
        has_ctrl = run.find(f"{NS}ctrl") is not None
        has_tbl = any(el.tag == f"{NS}tbl" for el in run.iter())
        has_t = any(el.tag == f"{NS}t" for el in run.iter())
        if not has_ctrl and not has_tbl and not has_t:
            p_elem.remove(run)


def _parse_emphasis_markup(text: str, valid_layer_ids: set | None = None) -> list:
    """AI 출력 text 에서 [[emN]]...[[/emN]] markup 추출 (stack 기반 normalizer).

    LLM 이 open/close 짝을 잘못 출력해도 흡수:
    - 룰 A: 다른 layer 가 열려 있는데 새 open → 이전 layer auto-close 후 새 open.
    - 룰 B: stack top 과 mismatch 인 close (orphan close) → 그 자리에서 open 으로 처리.
    - 같은 layer 또 open → noop (이미 열려 있음).
    - EOF 시 stack 에 남은 layer 자동 close.
    nested 자체가 발생하지 않으므로 stack 깊이는 최대 1.

    Args:
        text: AI 본문 텍스트 (markup 포함 가능)
        valid_layer_ids: 허용된 layer_id set (예: {"em1", "em2"}).
                         None 이면 모든 emN 허용. 정의 안 된 layer_id 의 token 은 무시되어
                         그 자리의 텍스트는 현재 stack top layer (또는 base) 로 들어간다.

    Returns:
        [(layer_id|None, segment_text), ...]
        layer_id is None → base (강조 안 함, default 글꼴)
    """
    import re as _re

    # Pre-pass: LLM 오작성 — 단일 bracket variant `[em\d+]` / `[/em\d+]` strip.
    # 정식 토큰은 더블 `[[em1]]` / `[[/em1]]` 뿐. 괄호 근처에서 LLM 이 [ 하나 빠뜨려
    # `([/em1]` 형태로 출력하는 케이스 잡아냄.
    text = _re.sub(r'(?<!\[)\[/?em\d+\](?!\])', '', text)

    # 1) tokenize
    token_pattern = _re.compile(r'\[\[(/?)(em\d+)\]\]')
    tokens: list = []  # ('text', str) | ('open', layer_id) | ('close', layer_id)
    cursor = 0
    for m in token_pattern.finditer(text):
        if m.start() > cursor:
            tokens.append(('text', text[cursor:m.start()]))
        is_close = bool(m.group(1))
        layer_id = m.group(2)
        cursor = m.end()
        if valid_layer_ids is not None and layer_id not in valid_layer_ids:
            # 정의 안 된 layer 의 markup → 토큰 무시 (glyph 도 안 박힘).
            # 그 자리의 다음 text 토큰은 현재 stack top layer 로 들어감.
            continue
        tokens.append(('close' if is_close else 'open', layer_id))
    if cursor < len(text):
        tokens.append(('text', text[cursor:]))

    # 2) stack 운용 (flat tagging — nested 없음, stack 깊이 ≤ 1)
    segments: list = []
    stack: list = []
    for kind, value in tokens:
        if kind == 'text':
            current = stack[-1] if stack else None
            segments.append((current, value))
        elif kind == 'open':
            layer_id = value
            if stack and stack[-1] != layer_id:
                stack.pop()  # 룰 A — 다른 layer auto-close
            if not stack or stack[-1] != layer_id:
                stack.append(layer_id)
        else:  # 'close'
            layer_id = value
            if stack and stack[-1] == layer_id:
                stack.pop()  # 정상 close
            else:
                # 룰 B — orphan close → 그 자리에서 open 으로 처리
                if stack and stack[-1] != layer_id:
                    stack.pop()
                stack.append(layer_id)

    # 3) 안전망: 혹시 모를 남은 markup glyph strip (모든 segment) — 단일/더블 bracket 모두.
    _orphan_marker = _re.compile(r'\[{1,2}/?em\d+\]{1,2}')
    # 3b) em\d+ 본체 없이 [[ 또는 ]] 만 떠도는 잔존 — LLM 이 토큰 앞·뒷부분만 빠뜨린 경우.
    _stray_brackets = _re.compile(r'\]{2,}|\[{2,}')
    segments = [
        (layer, (_stray_brackets.sub('', _orphan_marker.sub('', t)) if t else t))
        for layer, t in segments
    ]

    # 4) 빈 segment 제거 (단 segments 비면 None entry 1개 유지)
    segments = [s for s in segments if s[1]]
    if not segments:
        segments = [(None, "")]
    return segments


def _replace_text_with_emphasis_segments(
    p_elem,
    marker_text: str,
    content_segments: list,
    charpr_map: dict,
    NS: str,
):
    """Sprint 3C: marker + emphasis segments를 paragraph에 박음.

    매핑 정확성:
    - marker_text → 첫 bearing t (첫 글자 매칭)
    - content_segments 단일 (base 1개) → 가장 긴 bearing t에 박음 (Sprint 2B와 동일)
    - content_segments 다중 → content_t의 run 위치에 새 runs 분할 삽입
      각 segment의 charPrIDRef = charpr_map[layer_id] (layer is None → charpr_map["base"])

    Args:
        p_elem: paragraph element (outer or with tbl)
        marker_text: 마커 부분 (코드가 부착)
        content_segments: [(layer_id|None, text), ...] (markup parser 결과)
        charpr_map: {"base": base_cp, "em1": cp, "em2": cp, ...}
            None layer는 "base" 키로 lookup. 누락된 layer는 base fallback.
        NS: namespace 문자열
    """
    import xml.etree.ElementTree as _stdlib_ET

    t_elems = [el for el in p_elem.iter() if el.tag == f"{NS}t"]
    base_cp = charpr_map.get("base", "")

    def _lookup_cp(layer):
        if layer is None:
            return base_cp
        return charpr_map.get(layer, base_cp)

    flat_content = "".join(s[1] for s in content_segments)

    if not t_elems:
        # 새 run 추가 (fallback)
        runs = p_elem.findall(f"{NS}run")
        if not runs:
            return
        new_t = _stdlib_ET.SubElement(runs[0], f"{NS}t")
        new_t.text = marker_text + flat_content
        return

    bearing = [t for t in t_elems if (t.text or "").strip()]

    # 2c 분리 (2026-05-22): marker_text 빈 경우 — 2c output은 마커가 첫 segment 안에 들어 있음.
    # marker_t 매칭 skip, content_t = bearing[0]로 잡고 segments 전체 분할 박기.
    if not marker_text and bearing:
        content_t = bearing[0]
        content_t_parent_run = None
        for run in p_elem.iter(f"{NS}run"):
            if content_t in list(run):
                content_t_parent_run = run
                break
        # 첫 segment → content_t
        first_layer, first_text = (
            content_segments[0] if content_segments else (None, flat_content)
        )
        first_cp = _lookup_cp(first_layer)
        if first_cp and content_t_parent_run is not None:
            content_t_parent_run.set("charPrIDRef", first_cp)
        content_t.text = first_text
        for child in list(content_t):
            content_t.remove(child)
        # 나머지 segments → 새 run 분할 (charpr_map 따라 글꼴 적용)
        if len(content_segments) > 1 and content_t_parent_run is not None:
            run_parent = None
            for el in p_elem.iter():
                if content_t_parent_run in list(el):
                    run_parent = el
                    break
            if run_parent is not None:
                run_idx = list(run_parent).index(content_t_parent_run)
                insert_idx = run_idx + 1
                for layer, seg_text in content_segments[1:]:
                    if not seg_text:
                        continue
                    cp = _lookup_cp(layer)
                    new_run = _stdlib_ET.Element(f"{NS}run")
                    if cp:
                        new_run.set("charPrIDRef", cp)
                    new_t = _stdlib_ET.SubElement(new_run, f"{NS}t")
                    new_t.text = seg_text
                    run_parent.insert(insert_idx, new_run)
                    insert_idx += 1
        # 나머지 t 모두 비움 — 자식 도구가 들여쓰기까지 출력하므로 본보기 들여쓰기 자리
        # 보존하면 중복 발생. 들여쓰기 책임이 자식 도구한테 일임된 상태.
        for t in t_elems:
            if t is content_t:
                continue
            t.text = ""
            for child in list(t):
                t.remove(child)
        return

    if len(bearing) < 2:
        # split 불가 — 합쳐서 박음.
        # bearing[0](실제 글자 있던 첫 단위)에 박아야 양식 본래 본문 글꼴 자리 보존.
        # t_elems[0]는 보통 paragraph 첫 단위(들여쓰기 공백)라 작은 글꼴 자리.
        target_t = bearing[0] if bearing else t_elems[0]
        target_t.text = marker_text + flat_content
        for child in list(target_t):
            target_t.remove(child)
        # 나머지 비움 — 양식 본래 공백/탭 only t는 들여쓰기 자리로 보존
        for t in t_elems:
            if t is target_t:
                continue
            if not (t.text or "").strip():
                continue
            t.text = ""
            for child in list(t):
                t.remove(child)
        return

    # marker t 매칭 (첫 글자)
    marker_first = marker_text.strip()[:1] if marker_text.strip() else ""
    marker_t = None
    if marker_first:
        for t in bearing:
            t_first = (t.text or "").strip()[:1]
            if t_first and t_first == marker_first:
                marker_t = t
                break
    if marker_t is None:
        marker_t = bearing[0]

    # content_t (가장 긴 bearing, marker 제외)
    # 공백+탭만인 t는 양식 들여쓰기 자리 — content_t 후보에서 제외.
    # 그렇지 않으면 본문 segments가 들여쓰기 글꼴(작은 폰트) 자리로 박힘.
    non_marker = [t for t in bearing if t is not marker_t]
    content_candidates = [t for t in non_marker if (t.text or "").strip()]
    if content_candidates:
        content_t = max(content_candidates, key=lambda t: (len(t.text or ""), bearing.index(t)))
    else:
        content_t = marker_t

    # content_t == marker_t fallback: 들여쓰기만 있는 양식(예: 공백 t + 마커+본문 합쳐진 t)
    # 분리 불가 — 양식 본래 글꼴 보존하면서 marker + content 통째 박음.
    # 들여쓰기 공백 t는 그대로 둠.
    if content_t is marker_t:
        marker_t.text = marker_text + flat_content
        for child in list(marker_t):
            marker_t.remove(child)
        for t in t_elems:
            if t is marker_t:
                continue
            if not (t.text or "").strip():
                continue
            t.text = ""
            for child in list(t):
                t.remove(child)
        return

    # marker 박기
    marker_t.text = marker_text
    for child in list(marker_t):
        marker_t.remove(child)

    # content 박기
    if len(content_segments) <= 1:
        # single segment — base(None) 또는 layer 명시 둘 다 포함.
        # layer 명시된 경우에는 본문 자리(content_t)가 속한 run의 글꼴 번호를
        # 그 layer 번호로 바꿔주어야 양식 본래 본보기 글꼴이 아닌
        # AI 의도 강조 글꼴이 적용됨. base(None)는 양식 본래 글꼴 유지.
        if content_segments:
            seg_layer, seg_text = content_segments[0]
        else:
            seg_layer, seg_text = None, flat_content
        if seg_layer is not None:
            seg_cp = _lookup_cp(seg_layer)
            if seg_cp:
                for run in p_elem.iter(f"{NS}run"):
                    if content_t in list(run):
                        run.set("charPrIDRef", seg_cp)
                        break
        content_t.text = seg_text
        for child in list(content_t):
            content_t.remove(child)
    else:
        # 다중 segments → content_t의 run 위치에 새 runs 분할
        # content_t의 부모 run + 그 run의 부모 (cell paragraph 또는 outer paragraph) 찾기
        content_t_parent_run = None
        for run in p_elem.iter(f"{NS}run"):
            if content_t in list(run):
                content_t_parent_run = run
                break

        if content_t_parent_run is None:
            # fallback: 합쳐서 박음
            content_t.text = flat_content
            for child in list(content_t):
                content_t.remove(child)
        else:
            # run의 부모 (cell paragraph 또는 outer paragraph)
            run_parent = None
            for el in p_elem.iter():
                if content_t_parent_run in list(el):
                    run_parent = el
                    break

            if run_parent is None:
                content_t.text = flat_content
                for child in list(content_t):
                    content_t.remove(child)
            else:
                # content_t_parent_run의 charPr를 첫 segment에 맞춤
                first_layer, first_text = content_segments[0]
                first_cp = _lookup_cp(first_layer)
                if first_cp:
                    content_t_parent_run.set("charPrIDRef", first_cp)
                content_t.text = first_text
                for child in list(content_t):
                    content_t.remove(child)

                # 나머지 segments → 새 run 만들어 순서대로 insert
                run_idx = list(run_parent).index(content_t_parent_run)
                insert_idx = run_idx + 1
                for layer, seg_text in content_segments[1:]:
                    if not seg_text:
                        continue
                    cp = _lookup_cp(layer)
                    new_run = _stdlib_ET.Element(f"{NS}run")
                    if cp:
                        new_run.set("charPrIDRef", cp)
                    new_t = _stdlib_ET.SubElement(new_run, f"{NS}t")
                    new_t.text = seg_text
                    run_parent.insert(insert_idx, new_run)
                    insert_idx += 1

    # 나머지 t 비움 정책 (split 함수와 통일):
    # - 마커 자리와 본문 자리 사이 공백/탭 only t는 양식 구분자 자리 — 비움
    # - 그 외 위치(맨 앞·끝) 공백/탭 only t는 들여쓰기/trailing 자리 — 보존
    # - 글자 있는 중간 t는 비움
    _em_m_idx = t_elems.index(marker_t)
    _em_c_idx = t_elems.index(content_t)
    _em_between_lo = min(_em_m_idx, _em_c_idx)
    _em_between_hi = max(_em_m_idx, _em_c_idx)
    for _ei, t in enumerate(t_elems):
        if t is marker_t or t is content_t:
            continue
        _em_ws_only = not (t.text or "").strip()
        _em_between = _em_between_lo < _ei < _em_between_hi
        if _em_ws_only and not _em_between:
            continue
        t.text = ""
        for child in list(t):
            t.remove(child)

    # ctrl/tbl/t 없는 빈 run 제거 (cleanup)
    for run in list(p_elem.iter(f"{NS}run")):
        has_ctrl = run.find(f"{NS}ctrl") is not None
        has_tbl = any(el.tag == f"{NS}tbl" for el in run.iter())
        has_t = any(el.tag == f"{NS}t" for el in run.iter())
        if not has_ctrl and not has_tbl and not has_t:
            # parent에서 제거
            for parent in p_elem.iter():
                if run in list(parent):
                    parent.remove(run)
                    break
