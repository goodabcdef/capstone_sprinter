"""
title: HWP 파일 생성 v3 (로컬)
author: sj
version: 4.0.0
license: MIT
description: 로컬에서 HWPX 문서를 직접 생성합니다. 분석 11단계(1a~1k, 양식 캐시) + 생성 5단계(2a~2e, 매번 실행).
"""

from pydantic import BaseModel, Field
from typing import Optional
import json
import os
import io
import uuid
import logging

log = logging.getLogger(__name__)


class Tools:
    class Valves(BaseModel):
        AI_MODEL: str = Field(
            default="",
            description="1차/2차 AI에 사용할 모델 ID (비어있으면 시스템 기본 TASK_MODEL 사용, 예: gpt-5.4, gpt-5.4-mini, ChatGPT-oss-120B)",
        )
        MAX_PDF_PAGES: int = Field(
            default=0,
            description="PDF 이미지 변환 최대 페이지 수 (0이면 전체 변환)",
        )
        DEBUG_MODE: str = Field(
            default="off",
            description="on이면 각 단계의 중간 결과물을 채팅에 출력합니다",
        )
        HYBRID_MEASUREMENT: str = Field(
            default="off",
            description="parent_hint 신뢰성 측정 모드. on이면 1c hybrid 프롬프트 + step1ab cache만 사용 (full cache 무시). 측정 종료 시 off.",
        )
        CANONICAL_FALLBACK_MODE: str = Field(
            default="report_only",
            description="_FAMILY_DEFAULT_CANONICAL fallback. on=current(override), report_only=no_apply+log, off=no_apply+no_log. parent_first 전환 후 데이터 기반 canonicalization으로 대체되었으나 비교용으로 report_only 권장.",
        )
        ANALYSIS_ONLY_MODE: str = Field(
            default="off",
            description="on이면 1차 분석(1a~1j + 1k)까지만 진행하고 본문 생성(2a/2b/assembly) 전에 종료. rules debug 검증용. cache + debug 파일은 정상 생성됨.",
        )
    def __init__(self):
        self.valves = self.Valves()

    async def generate_document(
        self,
        content: str = "",
        file_name: str = "문서",
        doc_title: str = "",
        template_file_id: str = "",
        content_file_id: str = "",
        __user__: dict = {},
        __event_emitter__=None,
        __chat_id__: str = "",
        __message_id__: str = "",
        __request__=None,
        __files__: list = [],
        **_unknown_kwargs,
    ) -> str:
        """
        HWP 문서를 로컬에서 생성합니다. 사용자가 HWP/한글 문서 생성을 요청하면 이 도구를 사용하세요.

        양식 파일과 소스 파일을 첨부하면 역할 기반으로 문서를 자동 생성합니다.
        - 양식: 파일명이 "(양식)"으로 시작하는 .hwpx (또는 .hwp)
        - 소스: 나머지 첨부파일 1개 (.hwpx 또는 .pdf 모두 가능)
        - 하위호환: "(양식)" 접두가 없으면 확장자로 추정 (.hwpx → 양식, .pdf → 소스)

        :param content: 문서에 들어갈 내용. 양식+소스파일이 있으면 비워도 됩니다.
        :param file_name: 저장할 파일 이름 (확장자 제외)
        :param doc_title: 문서 내부에 표시될 공식 제목
        :param template_file_id: 양식 파일 ID. 비어있으면 첨부파일에서 자동 감지.
        :param content_file_id: 소스 파일 ID(.hwpx 또는 .pdf). 비어있으면 첨부파일에서 자동 감지.
        :return: 생성 결과 메시지
        """

        if __event_emitter__:
            await __event_emitter__(
                {"type": "status", "data": {"description": "HWP 파일 생성 준비 중...", "done": False}}
            )

        try:
            from open_webui.models.files import Files, FileForm
            from open_webui.models.chats import Chats
            from open_webui.storage.provider import Storage

            user_id = __user__.get("id", "system")
            title = doc_title or file_name

            # ─── 첨부파일에서 양식/소스 파일 자동 감지 ───
            #   1차: "(양식)" 또는 "（양식）"(전각) 접두 → template, 나머지 → source
            #   2차 fallback: 확장자 기반 (.hwpx → template, .pdf → source) — 하위호환
            def _is_template_name(name: str) -> bool:
                n = (name or "").lstrip()
                return n.startswith("(양식)") or n.startswith("（양식）")

            if not template_file_id or not content_file_id:
                for f in __files__:
                    fid = f.get("id", "")
                    fname = f.get("name", f.get("filename", ""))
                    if _is_template_name(fname):
                        if not template_file_id:
                            template_file_id = fid
                    else:
                        if not content_file_id:
                            content_file_id = fid

            if not template_file_id or not content_file_id:
                for f in __files__:
                    fid = f.get("id", "")
                    fname_l = f.get("name", f.get("filename", "")).lower()
                    ftype = f.get("type", f.get("content_type", "")).lower()
                    if not template_file_id and (fname_l.endswith(".hwpx") or fname_l.endswith(".hwp")) and fid != content_file_id:
                        template_file_id = fid
                    elif not content_file_id and (fname_l.endswith(".pdf") or "pdf" in ftype) and fid != template_file_id:
                        content_file_id = fid

            if not template_file_id:
                return "오류: 양식 파일을 첨부해주세요. (파일명 앞에 '(양식)'을 붙이세요)"

            # ─── 하이브리드 동적 생성 ───
            hwpx_bytes, debug_log = await self._generate_hybrid(
                template_file_id=template_file_id,
                content_file_id=content_file_id,
                content_text=content,
                user_id=user_id,
                __request__=__request__,
                __user__=__user__,
                __event_emitter__=__event_emitter__,
            )

            # ─── ANALYSIS_ONLY_MODE: 본문 생성 없이 분석 결과만 반환 ───
            if hwpx_bytes is None:
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {"description": "1차 분석 완료", "done": True}})
                return debug_log

            # ─── 파일 저장 및 반환 ───
            filename = f"{file_name}.hwpx"
            file_buf = io.BytesIO(hwpx_bytes)
            file_buf.name = filename

            contents, file_path = Storage.upload_file(file_buf, filename, {})
            file_meta = {
                "name": filename,
                "content_type": "application/hwp+zip",
                "size": len(hwpx_bytes),
                "source": "local_hwp_generator_v3",
            }

            file_id = str(uuid.uuid4())
            file_record = Files.insert_new_file(
                user_id,
                FileForm(id=file_id, filename=filename, path=file_path, meta=file_meta),
            )

            file_id = file_record.id
            download_url = f"/api/v1/files/{file_id}/content"

            file_item = {
                "type": "file",
                "url": download_url,
                "name": filename,
                "content_type": "application/hwp+zip",
            }

            if __chat_id__ and __message_id__:
                Chats.add_message_files_by_id_and_message_id(__chat_id__, __message_id__, [file_item])

            if __event_emitter__:
                await __event_emitter__({"type": "chat:message:files", "data": {"files": [file_item]}})
                await __event_emitter__({"type": "status", "data": {"description": "HWP 파일 생성 완료", "done": True}})

            base_url = ""
            if __request__:
                scheme = __request__.headers.get("x-forwarded-proto", "https")
                host = __request__.headers.get("x-forwarded-host") or __request__.headers.get("host", "")
                if host:
                    base_url = f"{scheme}://{host}"
            full_url = f"{base_url}{download_url}"

            result_msg = f"HWP 문서 '{filename}'가 생성되었습니다.\n\n[{filename} 다운로드]({full_url})"
            return result_msg

        except Exception as e:
            log.exception("HWP 생성 오류")
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "HWP 파일 생성 실패", "done": True}})
            import traceback
            return f"HWP 생성 오류: {type(e).__name__}: {str(e)}\n\n```\n{traceback.format_exc()}\n```"

    async def _generate_hybrid(
        self,
        template_file_id: str,
        content_file_id: str,
        content_text: str,
        user_id: str,
        __request__=None,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> tuple:
        """
        HWPX 본문 생성 오케스트레이션 — 분석 11단계 + 생성 5단계 (실행 순서).

        [분석] 양식 hash 기반 캐시, 처음 1회만 실행
          1a  AI    paragraph 구조 분석 (모양 그룹 + 역할 추측)
          1b  AI    paragraph별 역할 후보
          1c  AI    깊이 + 부모 결정
          1d  AI    차례(TOC) 기반 챕터 단위
          1e  AI    같은 구조 단락 묶음 (canonical cluster)
          1f  AI    묶음 재점검 (cluster repair)
          1g  AI    전체 트리 재구성 (tree rebuild)
          1h  AI    묶음별 표기 규칙
          1i  AI    챕터 안 반복 패턴 (chapter pattern family)
          1j  AI    묶음별 말투 (cluster별 batch)
          1k  AI    묶음별 강조 layer + 예산 (cluster별 batch)

        [생성] 매번 실행 (캐시 X)
          2a  AI    챕터 제목 다시쓰기 + 표지 슬롯
          2b  AI    챕터별 소스 구간 분배
          2c  AI    챕터별 본문 골격
          2d  AI    챕터별 본문 말투 다듬기
          2e  AI    챕터별 마커·강조 부착
          (조립)  코드  XML 조립 + HWPX 파일 출력 (assemble_hwpx_hybrid)

        16단계 외 보조 AI 호출 + 코드 후처리는 hwpx_analyzer.py 상단 참조.
        """
        from open_webui.models.files import Files
        from open_webui.storage.provider import Storage
        from open_webui.utils.hwpx_analyzer import (
            analyze_hwpx,
            truncate_xml,
            build_structure_analysis_prompt,
            parse_structure_from_llm,
            build_level_analysis_prompt,
            parse_level_from_llm,
            merge_levels_into_structure,
            build_role_classification_prompt,
            parse_role_classification_from_llm,
            merge_roles_into_structure,
            compute_parent_instance_children,
            build_exclusivity_analysis_prompt,
            parse_exclusivity_from_llm,
            compute_format_observations,
            build_format_analysis_prompt,
            parse_format_rules_from_llm,
            build_chapter_types_from_structure,
            build_chapter_classify_prompt,
            parse_chapter_classify_from_llm,
            build_section_fill_prompt,
            parse_section_fill_from_llm,
            build_section_polish_prompt,
            parse_section_polish_from_llm,
            build_section_style_prompt,
            parse_section_style_from_llm,
            apply_section_style_to_items,
            reconstruct_tree_from_flat,
            validate_reconstruction,
            validate_text_quality,
            process_section_fill_result,
            classify_role_text_types,
            write_stage_debug_files,
            split_source_by_chapters,
            _extract_texts_by_idx,
            _collect_style_samples,
            extract_role_markers_from_1f,
            build_style_profile_prompt,
            parse_style_profile_from_llm,
            extract_paragraph_emphasis_map,
            build_emphasis_layer_prompt,
            parse_emphasis_layer_from_llm,
            extract_marker_policies,
            save_template_cache,
            load_template_cache,
            pdf_to_base64_images,
            pdf_to_text,
        )
        from open_webui.utils.marker_separator import build_marker_roundtrip_debug
        from open_webui.utils.source_block_adapter import text_blob_to_source_blocks, compute_preserve_indices
        from open_webui.utils.hwpx_analyzer import (
            build_shallow_fill_prompt,
            parse_shallow_fill_from_llm,
            validate_shallow_output,
            should_use_shallow_route,
            extract_shallow_section_plan_seed,
            observe_section_plan_compliance,
            extract_chapter_template_plan_seed,
            extract_chapter_template_tree,
            extract_paragraph_run_parts,
            build_source_range_prompt,
            parse_source_ranges_from_llm,
            apply_source_ranges_with_safety,
            compute_region_action_plan,
            diagnose_multi_section,
            pattern_to_grammar,
            measure_title_role_consistency,
            diagnose_chapter_empty_reason,
            build_chapter_object,
            # 13.7b B0a (pre-1a section census, debug-only)
            extract_section_census,
            # 13.7c (template-first)
            build_source_inventory_prompt,
            parse_source_inventory_from_llm,
            build_adaptation_plan_prompt,
            extract_toc_t_list,
            parse_adaptation_plan_from_llm,
            validate_adaptation_decision,
            compute_reference_metrics,
            normalize_adaptation_decision,
            make_unavailable_decision,
            make_validation_failed_decision,
            summarize_adaptation_plan,
            # 13.7b B2.2 (section_role_proposal AI sub-step, debug-only)
            summarize_section_for_proposal,
            build_section_role_proposal_prompt,
            parse_section_role_proposal_from_llm,
            validate_section_role_proposal,
            make_fallback_section_role_proposal,
            summarize_section_role_proposals,
            # 13.7b B0b (post-1a merge feasibility, debug-only)
            measure_merge_feasibility,
            build_b0b_observation_artifact,
            # 13.7b section-local generation-lite (deadline path)
            compute_section_offsets,
            extract_section_chapter_list,
            decide_section_processing,
            summarize_section_local_decisions,
            # 13.7b fix: 1a→xml idx mapping
            _build_1a_to_xml_p_idx_mapping,
            extract_section_xml_paragraph_texts,
            # 13.7b §4: chapter-local exemplars
            build_chapter_local_exemplars,
        )
        from open_webui.utils.target_unit_planner import (
            propose_template_regions,
            build_target_unit_planning_prompt,
            parse_target_unit_plan_from_llm,
            validate_target_unit_plan,
            compute_legacy_comparison,
            is_plan_cache_valid,
            build_plan_cache_payload,
            assemble_planning_debug,
            CURRENT_PLANNER_VERSION,
        )
        from open_webui.utils.template_observer import (
            extract_template_unit_features,
            build_template_unit_prompt,
            parse_template_unit_observation_from_llm,
            validate_unit_observation,
            derive_mode_label,
            compute_pipeline_fit,
            assemble_observation_output,
            build_cache_payload,
            is_cache_valid,
            CURRENT_OBSERVER_VERSION,
        )
        from open_webui.utils.hwp_generator import assemble_hwpx_hybrid
        from open_webui.utils.chat import generate_chat_completion
        from open_webui.utils.task import get_task_model_id
        from open_webui.models.users import Users

        debug = str(self.valves.DEBUG_MODE).lower() in ("on", "true", "1")
        debug_log = []
        _shallow_done = False

        def _debug_add(title, content):
            if debug:
                debug_log.append(f"### [DEBUG] {title}\n{content}")

        if not __request__:
            raise ValueError("request 객체가 없어 AI를 호출할 수 없습니다")

        models = __request__.app.state.MODELS
        # Valves에서 모델 지정 시 우선 사용
        if self.valves.AI_MODEL and self.valves.AI_MODEL in models:
            model_id = self.valves.AI_MODEL
            log.info(f"Valves 모델 사용: {model_id}")
        else:
            model_id = get_task_model_id(
                "", __request__.app.state.config.TASK_MODEL,
                __request__.app.state.config.TASK_MODEL_EXTERNAL, models,
            )
        if not model_id or model_id not in models:
            raise ValueError(f"사용 가능한 AI 모델이 없습니다: {model_id}")

        user_obj = Users.get_user_by_id(user_id)
        if not user_obj:
            raise ValueError("사용자 정보를 찾을 수 없습니다")

        async def _call_llm(messages, task_name):
            # 내부 vLLM (Qwen 등) reasoning 모드 끄기 + max_tokens 명시.
            # 397B 가 thinking 에 토큰 소모해 content 가 truncate 되는 현상 막음.
            payload = {
                "model": model_id,
                "messages": messages,
                "stream": False,
                "metadata": {"task": task_name},
                "max_tokens": 65536,
                "chat_template_kwargs": {"enable_thinking": True},
            }
            resp = await generate_chat_completion(__request__, form_data=payload, user=user_obj)
            if hasattr(resp, "body"):
                resp = json.loads(resp.body.decode("utf-8"))
            if "error" in resp:
                error_msg = resp["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise ValueError(f"AI 응답 오류 ({task_name}): {error_msg}")
            if "choices" not in resp:
                raise ValueError(f"AI 응답 형식 오류: {str(resp)[:500]}")
            _content = resp["choices"][0]["message"]["content"]
            # 2b-a (section_fill) + 2b-b (section_polish) 입출력 dump — 진단용 (2026-05-25)
            if isinstance(task_name, str) and (
                task_name.startswith("hwpx_section_fill") or
                task_name.startswith("hwpx_section_polish")
            ):
                try:
                    import os as _io_os, json as _io_json
                    _io_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                    _io_kind = "2b-b_polish" if task_name.startswith("hwpx_section_polish") else "2b-a_fill"
                    _io_entry = {
                        "task_name": task_name,
                        "kind": _io_kind,
                        "system_content_full": messages[0].get("content", "") if messages else "",
                        "user_content_full": messages[-1].get("content", "") if messages else "",
                        "raw_full": _content or "",
                        "user_content_len": len(messages[-1].get("content", "")) if messages else 0,
                        "raw_len": len(_content or ""),
                    }
                    with open("/tmp/hwpx_debug/2b_io_log.jsonl", "a", encoding="utf-8") as _io_f:
                        _io_f.write(_io_json.dumps(_io_entry, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            return _content

        def _flow_trace(stage, **kwargs):
            try:
                import os as _t_os, json as _t_json
                _t_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                with open("/tmp/hwpx_debug/2c_flow_trace.jsonl", "a", encoding="utf-8") as _t_f:
                    _t_f.write(_t_json.dumps({"stage": stage, **kwargs}, ensure_ascii=False) + "\n")
            except Exception:
                pass


        # ══════════════════════════════════════════════════════════════
        # 준비: 양식 분석 + XML 축소
        # ══════════════════════════════════════════════════════════════
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "양식 파일 분석 중...", "done": False}})

        template_file = Files.get_file_by_id(template_file_id)
        if not template_file:
            raise ValueError(f"양식 파일을 찾을 수 없습니다: {template_file_id}")
        template_path = Storage.get_file(template_file.path)
        if not template_path:
            raise ValueError("양식 파일 경로를 찾을 수 없습니다")

        analysis = analyze_hwpx(template_path)
        light_xml = analysis["light_xml"]
        truncate_result = truncate_xml(light_xml)
        truncated_xml = truncate_result["xml"]
        removed_indices = truncate_result["removed_indices"]
        idx_map = truncate_result.get("idx_map", {})

        log.info(
            f"양식 분석: 문단 {analysis['paragraph_count']}개, 표 {analysis['table_count']}개, "
            f"XML {len(light_xml):,} → {len(truncated_xml):,}자"
        )

        _debug_add(
            "Step 1: 양식 분석 + 축소",
            f"문단 {analysis['paragraph_count']}개, 표 {analysis['table_count']}개\n"
            f"XML: {len(light_xml):,} → {len(truncated_xml):,}자\n\n"
            f"<details><summary>축소된 XML (클릭)</summary>\n\n```xml\n{truncated_xml[:30000]}"
            f"{'... (잘림)' if len(truncated_xml) > 30000 else ''}\n```\n</details>",
        )

        # ── 내용 확보 ──
        content_images = None
        pdf_text_content = ""

        if content_file_id:
            content_file = Files.get_file_by_id(content_file_id)
            if content_file:
                content_type = content_file.meta.get("content_type", "")
                fname = content_file.meta.get("name", content_file.filename)
                fname_l = fname.lower()
                content_path = Storage.get_file(content_file.path)
                # pdf_text_content 변수명은 downstream 호환 — 실제로는 "소스 원본 텍스트"
                if content_type == "application/pdf" or fname_l.endswith(".pdf"):
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "PDF 텍스트 추출 중...", "done": False}})
                    try:
                        pdf_text_content = pdf_to_text(content_path)
                    except Exception as e:
                        log.warning(f"PDF 텍스트 추출 실패: {e}")
                elif fname_l.endswith(".hwpx") or fname_l.endswith(".hwp"):
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "HWPX 본문 텍스트 추출 중...", "done": False}})
                    try:
                        from open_webui.utils.hwpx_analyzer import hwpx_to_text
                        pdf_text_content = hwpx_to_text(content_path)
                    except Exception as e:
                        log.warning(f"HWPX 텍스트 추출 실패: {e}")
                # 이미지 추출 제거 — 텍스트만 사용 (토큰 절약). content_images는 None 유지
                if not pdf_text_content and content_images is None and not content_text:
                    content_text = content_file.data.get("content", "") if content_file.data else ""

        if not content_text and not content_images and not pdf_text_content:
            raise ValueError("작성할 내용이 없습니다.")

        # ══════════════════════════════════════════════════════════════
        # CACHE CHECK: 두 namespace
        #   - full: 1a~1e+chapter_types 통째 (정상 모드)
        #   - step1ab: 1a/1b 결과만 (hybrid 측정 모드 — 1c 격리 실험용)
        # ══════════════════════════════════════════════════════════════
        from open_webui.utils.hwpx_analyzer import (
            load_template_cache, save_template_cache, get_template_cache_path,
            compute_template_hash,
        )
        try:
            _cache_key = compute_template_hash(template_path)
        except Exception as _e:
            log.warning(f"[CACHE] hash 계산 실패, file_id로 대체: {_e}")
            _cache_key = template_file_id

        # ── 단계별 캐시 helper ──
        import os as _os
        _STEP_CACHE_DIR = "/tmp/hwpx_cache"
        _os.makedirs(_STEP_CACHE_DIR, exist_ok=True)
        def _step_cache_path(step_name):
            return f"{_STEP_CACHE_DIR}/{_cache_key}_{step_name}.json"
        def _load_step_cache(step_name):
            p = _step_cache_path(step_name)
            if _os.path.exists(p):
                with open(p, "r", encoding="utf-8") as _f:
                    return json.load(_f)
            return None
        def _save_step_cache(step_name, data):
            p = _step_cache_path(step_name)
            with open(p, "w", encoding="utf-8") as _f:
                json.dump(data, _f, ensure_ascii=False, default=str)
            log.info(f"[STEP_CACHE] saved {step_name} → {p}")

        hybrid_mode = (str(self.valves.HYBRID_MEASUREMENT).lower() == "on")
        canonical_mode = str(self.valves.CANONICAL_FALLBACK_MODE).lower()
        if canonical_mode not in ("on", "report_only", "off"):
            canonical_mode = "on"
        log.info(f"[VALVES] canonical_fallback_mode={canonical_mode}")

        # B2.1.2b: cache load 전 양식 section count 계산 (cache hit/miss 양쪽 사용).
        # actual vs cached section_count 정합성 검증으로 stale cache 방지 (schema 정합성, 의미 판단 아님).
        from open_webui.utils.hwpx_analyzer import (
            extract_all_sections_xml as _extract_all_sections_xml_pre,
        )
        try:
            _actual_section_count = len(_extract_all_sections_xml_pre(template_path))
        except Exception as _e:
            log.warning(f"[B2.1.2b] extract_all_sections_xml 실패: {_e}")
            _actual_section_count = 0

        # Hybrid mode: full cache 무시, step1ab만 사용. 1c는 매번 hybrid prompt로 새로.
        # 정상 mode: full cache 우선, step1ab 사용 안 함.
        _cached = None
        if not hybrid_mode:
            _cached = load_template_cache(_cache_key, namespace='full')

        _from_cache = _cached is not None

        # B2.1.2b: cache load 시 section_count 정합성 검증.
        # actual section count vs cached section_count 불일치 시 cache miss 처리 (stale cache 방지).
        if _from_cache:
            _cached_section_count = _cached.get("section_count", 0)
            if _actual_section_count != _cached_section_count:
                log.warning(
                    f"[CACHE] section_count mismatch: actual={_actual_section_count}, "
                    f"cached={_cached_section_count}. cache miss 처리."
                )
                _cached = None
                _from_cache = False

        # ══════════════════════════════════════════════════════════════
        # 13.7b B2.1.1: section별 1a~1f 결과 보관 (section-local 그대로).
        # document-level merge 금지 — paragraphs/parent_idx/chapter_types/
        # marker_policy_1f/role_cluster 모두 section_id 키로 분리 유지.
        # 현재는 single section (section0 기준). multi-section loop은 B2.1.2에서.
        # outer code/assemble은 section_results[0] unwrap으로 backward compat 유지.
        # ══════════════════════════════════════════════════════════════
        section_results: dict = {}

        log.info(f"[CACHE] hybrid_mode={hybrid_mode}, full_hit={_from_cache}")

        if _from_cache:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "양식 분석 캐시 사용 (1차/1.5차 AI 건너뜀)...", "done": False}})
            structure = _cached["structure"]
            chapter_types = _cached.get("chapter_types", {})
            _signals_cache = _cached.get("signals", {})
            _idx_texts_cache = _cached.get("idx_texts", {})
            _marker_policy_1f_cache = _cached.get("marker_policy_1f")
            if _marker_policy_1f_cache:
                structure["marker_policy_1f"] = _marker_policy_1f_cache
            # B2.1.1: section_results 로드 (cache v5+). v4 cache는 schema bump로 자동 invalidate됐음.
            # JSON에서 로드된 section_results는 키가 str. 기존 코드(int 키 조회) 호환 위해 int로 변환.
            _raw_sr_hit = _cached.get("section_results", {})
            section_results = {(int(k) if str(k).isdigit() else k): v for k, v in _raw_sr_hit.items()}
            # 1d (v6+): cache에 phase_e_chapter_planner + chapter_pattern_family 있으면 로드
            _cached_phase_e = _cached.get("phase_e_chapter_planner")
            _cached_track_c = _cached.get("chapter_pattern_family")
            # v7+: cache hit 시 1c 후 1d block (A2 loop 안) skip되므로
            # _phase_e_chapter_planner 변수를 cache 값으로 명시적으로 set.
            # paragraph chapter_id는 cache의 structure.paragraphs에 이미 포함됨 (v7 schema).
            _phase_e_chapter_planner = _cached_phase_e
            # placeholder for debug
            messages_1 = []
            llm_content_1 = "[FROM CACHE]"
            messages_level = []
            llm_content_level = "[FROM CACHE]"
            level_map = {}
            log.info(f"[CACHE/full] 사용: {template_file_id}, chapter_types={list(chapter_types.keys())}")

        # ══════════════════════════════════════════════════════════════
        # 분석 단계 (1a~1k): 양식 구조 분석 + role 태깅 + chapter_types
        # ══════════════════════════════════════════════════════════════
        import re as _re
        import copy as _copy
        from open_webui.utils.hwpx_analyzer import (
            _normalize_marker_type,
            _repair_json,
            _extract_texts_by_idx,
            compute_role_context_signals,
        )

        async def _do_step1a_1b(_truncated_xml_arg):
            """1a + 1b 호출. step1ab cache에 저장 가능한 dict 반환.

            B2.1.2b: section-local truncated_xml을 인자로 받음 (closure 변수 → 인자).
            """
            from open_webui.utils.hwpx_analyzer import compute_paragraph_features

            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "1a: AI가 양식 구조 분석 중...", "done": False}})

            msgs_1, _paragraph_styles = build_structure_analysis_prompt(_truncated_xml_arg, auto_truncate=False)
            content_1 = await _call_llm(msgs_1, "hwpx_structure_analysis")
            structure_l = parse_structure_from_llm(content_1)

            # 코드에서 paraPrIDRef/charPrIDRef 삽입 (1a AI 출력에서 제거됨)
            if _paragraph_styles:
                for _p in structure_l.get("paragraphs", []):
                    _pidx = _p.get("idx")
                    _ps = _paragraph_styles.get(_pidx, _paragraph_styles.get(str(_pidx)))
                    if _ps:
                        _p.setdefault("paraPrIDRef", _ps.get("paraPrIDRef", "0"))
                        _p.setdefault("charPrIDRef", _ps.get("charPrIDRef", "0"))
                        _p.setdefault("body_first_charpr", _ps.get("body_first_charpr", ""))

            _m = _re.search(r'```(?:json)?\s*([\[{][\s\S]*?[\]}])\s*```', content_1)
            if _m:
                raw_json = _m.group(1)
            else:
                _m2 = _re.search(r'\{[\s\S]*\}', content_1)
                raw_json = _m2.group(0) if _m2 else "{}"
            try:
                data_before = json.loads(raw_json, strict=False)
            except json.JSONDecodeError:
                data_before = json.loads(_repair_json(raw_json), strict=False)
            paragraphs_before_l = data_before.get("paragraphs", [])
            before_by_idx = {p.get("idx"): p.get("role", "") for p in paragraphs_before_l}

            split_log_l = []
            for _p in structure_l.get("paragraphs", []):
                _idx = _p.get("idx")
                _after_role = _p.get("role", "")
                _before_role = before_by_idx.get(_idx, "")
                if _before_role != _after_role:
                    split_log_l.append({
                        "idx": _idx,
                        "marker": _p.get("marker", ""),
                        "marker_type": _normalize_marker_type(_p.get("marker", "")),
                        "before_role": _before_role,
                        "after_role": _after_role,
                    })

            marker_norm_l = {}
            for _p in paragraphs_before_l:
                _role = _p.get("role", "")
                _marker = _p.get("marker", "")
                if not _role:
                    continue
                _mt = _normalize_marker_type(_marker)
                marker_norm_l.setdefault(_role, {}).setdefault(_mt, set()).add(_marker)
            marker_norm_l = {
                _role: {_mt: sorted(list(_markers)) for _mt, _markers in _by.items()}
                for _role, _by in marker_norm_l.items()
            }

            idx_texts_l = {}
            idx_full_texts_l = {}
            try:
                idx_texts_l = _extract_texts_by_idx(_truncated_xml_arg)
                idx_full_texts_l = _extract_texts_by_idx(_truncated_xml_arg, max_chars=None)
            except Exception:
                pass

            structure_l["paragraphs"] = compute_paragraph_features(structure_l.get("paragraphs", []))

            signals_pre_l = {"paragraph_texts": [
                {"idx": p.get("idx", -1), "text": idx_texts_l.get(p.get("idx", -1), "")}
                for p in structure_l.get("paragraphs", [])
            ]}

            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "1b: 문단별 semantic_role 후보 분석 중...", "done": False}})
            msgs_role = build_role_classification_prompt(structure_l, signals=signals_pre_l)
            content_role = await _call_llm(msgs_role, "hwpx_1b_role_candidates")
            role_candidates_l = parse_role_classification_from_llm(content_role)
            structure_l = merge_roles_into_structure(structure_l, role_candidates_l)

            return {
                "messages_1": msgs_1,
                "llm_content_1": content_1,
                "paragraphs_before": paragraphs_before_l,
                "split_log": split_log_l,
                "marker_norm": marker_norm_l,
                "idx_texts": idx_texts_l,
                "idx_full_texts": idx_full_texts_l,
                "signals_pre": signals_pre_l,
                "structure_after_1b": structure_l,
                "messages_role": msgs_role,
                "llm_content_role": content_role,
                "role_candidates": role_candidates_l,
            }

        if not _from_cache:
            # ══════════════════════════════════════════════════════════════
            # 13.7b B2.1.2a: sections_to_analyze 결정 + section별 truncate 준비.
            # 임시 [:1] 가드 — B2.1.2b에서 제거 예정.
            # cache save가 아직 loop 안이라 multi-iteration 활성화 시 cache 덮어쓰기 위험.
            # B2.1.2b에서 (a) cache save / validation / _debug_payload를 loop 밖으로 이동
            #            (b) [:1] 가드 제거 + 민원인 s0~s4 전부 분석
            #            (c) _do_step1a_1b signature + 1c~1f 변수 교체 (section-local truncate 사용)
            #            (d) cache load 시 section_count 정합성 검증 추가
            # B2.1.2a에서는 1c~1f가 outer truncated_xml/light_xml/idx_map 그대로 사용 (1 iteration이라 동일).
            # ══════════════════════════════════════════════════════════════
            from open_webui.utils.hwpx_analyzer import (
                extract_all_sections_xml as _extract_all_sections_xml,
                lighten_xml as _lighten_xml,
            )
            _all_sections = _extract_all_sections_xml(template_path)
            sections_to_analyze = [
                (idx, name, xml) for idx, (name, xml) in enumerate(_all_sections)
            ]
            log.info(
                f"13.7b B2.1.2a sections_to_analyze: {len(sections_to_analyze)} sections "
                f"(loop iter [:1] guard, B2.1.2b 제거 예정)"
            )

            # B2.1.2b: section별 _cache_validation 수집 (양식 메모리, cache 저장 X).
            # loop 밖에서 aggregate (모든 section can_cache AND).
            _section_cache_validations = {}
            _section0_can_cache = False  # B2.1.2b: section0 기준 cache gate (임시 backward compat)

            # B2.1.2b: [:1] 가드 제거 — 양식의 모든 section 처리.
            # 민원인 양식 실행 시 section_results key가 0~4 총 5개여야 함 (sanity 기준).
            for section_id, section_name, section_xml in sections_to_analyze:
                log.info(f"13.7b B2.1.2b section {section_id} ({section_name}) baseline 시작")

                # B2.1.2a: section별 light_xml/truncated_xml 변수 정의 (B2.1.2b에서 1c~1f가 사용).
                _section_light_xml = _lighten_xml(section_xml)
                _section_truncate_result = truncate_xml(_section_light_xml)
                _section_truncated_xml = _section_truncate_result["xml"]
                _section_removed_indices = _section_truncate_result["removed_indices"]
                _section_idx_map = _section_truncate_result.get("idx_map", {})

                # === 1a/1b: cache 또는 fresh ===
                _r1ab = await _do_step1a_1b(_section_truncated_xml)

                # unpack
                messages_1 = _r1ab.get("messages_1", [])
                llm_content_1 = _r1ab.get("llm_content_1", "")
                _paragraphs_before = _r1ab.get("paragraphs_before", [])
                _split_log = _r1ab.get("split_log", [])
                _marker_norm = _r1ab.get("marker_norm", {})
                _idx_texts = _r1ab.get("idx_texts", {})
                _idx_full_texts = _r1ab.get("idx_full_texts", {})
                _signals_pre = _r1ab.get("signals_pre", {})
                structure = _r1ab.get("structure_after_1b", {})
                messages_role = _r1ab.get("messages_role", [])
                llm_content_role = _r1ab.get("llm_content_role", "")
                role_candidates = _r1ab.get("role_candidates", {})

                # ══════════════════════════════════════════════════════════
                # 1c: level + selected_index 결정 (계층/소제목)
                # 코드 후처리: parent_idx + sibling_group_id + structure_role + validator
                # (merge_levels_into_structure 안에서 자동 수행)
                # ══════════════════════════════════════════════════════════

                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {"description": "1c: level + 후보 index 선택 중...", "done": False}})

                messages_level = build_level_analysis_prompt(structure, signals=_signals_pre, hybrid=hybrid_mode)
                llm_content_level = await _call_llm(messages_level, "hwpx_1c_level_hybrid" if hybrid_mode else "hwpx_1c_level")
                level_parsed = parse_level_from_llm(llm_content_level, hybrid=hybrid_mode)
                structure = merge_levels_into_structure(structure, level_parsed, canonical_mode=canonical_mode)
                level_map = level_parsed.get("level_map", {})

                # ══════════════════════════════════════════════════════════
                # Parent post-correction (1d 전):
                #   1. container_score multi-signal 계산
                #   2. 화살표 marker family를 직전 enumeration의 자식으로 reattach
                #   3. weak parent의 자식들을 strong-container grandparent로 승격
                # 보정 전/후 snapshot + 1d before/after 비교 dump.
                # ══════════════════════════════════════════════════════════
                from open_webui.utils.hwpx_analyzer import (
                    _compute_container_scores,
                    reparent_leaf_prone_children,
                    compute_exclusivity_rules_code,
                    compute_format_rules_code,
                )
                import copy as _copy_pc
                _para_before_correction = _copy_pc.deepcopy(structure.get("paragraphs", []))

                container_scores = _compute_container_scores(structure.get("paragraphs", []))

                # 1d 보정 전 (참고용)
                _pc_before = compute_parent_instance_children({"paragraphs": _para_before_correction})
                try:
                    _exclusive_before = compute_exclusivity_rules_code(_pc_before) if _pc_before else []
                except Exception:
                    _exclusive_before = []

                # 보정 적용 — 순서 중요: reparent 먼저, reattach 나중.
                # reattach가 만든 새 parent가 weak이면 reparent가 다시 grandparent로 되돌리는 상쇄 방지.
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {"description": "Parent 보정: leaf-prone reparent + arrow reattach...", "done": False}})
                _reparent_log = []  # reparent_leaf_prone_children 비활성화 — container 필터링 제거로 불필요
                _reattach_log = []  # reattach_arrow_markers 비활성화 — 실제 parent 변경 0건 (dead code)

                # ══════════════════════════════════════════════════════════
                # Hybrid 측정 (parent_hint 신뢰성)
                #   - hint validation (self_loop/forward_ref/out_of_range)
                #   - hint vs stack conflict 분류 (match/ancestor/descendant/unrelated)
                #   - hint_override_tree (단순 (a) — valid hint paragraph parent_idx만 변경)
                #   - core_cases (조달청 답지 dump only)
                # 1d/1e/2b 에는 영향 X. dump only.
                # ══════════════════════════════════════════════════════════
                _hint_validation = None
                _hint_conflicts = None
                _hint_override_paras = None
                _hint_tree_paras = None
                _tree_diff = None
                _pc_hint = None
                _excl_hint = []
                _chapter_types_hint = {}
                _stack_inconsistency = None
                _pf_inconsistency = None
                _pc_stack_by_pidx = None
                _excl_stack_by_pidx = []
                _stack_post_correction_paras = None
                _role_registry = None
                _role_registry_baseline = None
                _canonical_clustering_dump = None
                _core_cases = []
                if hybrid_mode:
                    from open_webui.utils.hwpx_analyzer import (
                        validate_parent_hints, classify_hint_conflicts, build_hint_override_tree,
                        build_hint_tree, compute_tree_diff,
                        compute_parent_instance_children_by_parent_idx,
                        measure_tree_inconsistency,
                        canonicalize_by_data,
                    )
                    _decisions_hint = level_parsed.get("decisions", {})
                    _paras_now = structure.get("paragraphs", [])
                    _hint_validation = validate_parent_hints(_decisions_hint, _paras_now)
                    _hint_conflicts = classify_hint_conflicts(_paras_now, _decisions_hint, _hint_validation)
                    _hint_override_paras = build_hint_override_tree(_paras_now, _decisions_hint, _hint_validation)
                    # 양식별 core_cases — 양식 hash로 키. 다른 양식 측정 시 비어 있어도 OK.
                    # 답지 정의된 양식만 core_cases로 측정. dump only.
                    _TEMPLATE_CORE_CASES = {
                        "34fce805c7cbccc0": {  # 조달청 2024 업무계획
                            5: 4,      # M chapter1 → H
                            20: 4,     # N chapter1 → H
                            40: 39,    # M chapter3 strat1 → J
                            131: 130,  # M chapter3 strat2 → J
                            206: 205,  # M chapter3 strat3 → J
                            41: 39,    # K chapter3 strat1 → J
                            132: 130,  # K chapter3 strat2 → J
                            207: 205,  # K chapter3 strat3 → J
                            195: 191,  # X → Q (trailing summary)
                        },
                    }
                    _CORE_CASES_ANSWERS = _TEMPLATE_CORE_CASES.get(_cache_key, {})
                    _para_by_idx = {p.get("idx"): p for p in _paras_now}
                    for cidx, ans in _CORE_CASES_ANSWERS.items():
                        para = _para_by_idx.get(cidx)
                        if not para:
                            continue
                        d = _decisions_hint.get(cidx) or _decisions_hint.get(str(cidx)) or {}
                        val_status = _hint_validation["per_idx"].get(cidx, "no_hint")
                        cf = _hint_conflicts["per_idx"].get(cidx, {})
                        _core_cases.append({
                            "idx": cidx,
                            "role": para.get("canonical_role") or para.get("role"),
                            "stack_parent": para.get("parent_idx"),
                            "hint_parent": d.get("parent_hint_idx"),
                            "hint_validation": val_status,
                            "conflict_kind": cf.get("kind"),
                            "confidence": d.get("confidence"),
                            "hint_reason_code": d.get("parent_hint_reason_code"),
                            "answer_parent": ans,
                        })

                    # === hint_tree 비교 실험 (read-only, 1d/2a/2b/조립 영향 X) ===
                    # valid hint면 hint parent, 그 외는 stack parent fallback. BFS level 재계산.
                    _hint_tree_paras = build_hint_tree(_paras_now, _decisions_hint, _hint_validation)
                    _tree_diff = compute_tree_diff(
                        _paras_now, _hint_tree_paras,
                        core_idxs=set(_CORE_CASES_ANSWERS.keys()),
                    )
                    # 1d/exclusivity on hint_tree (parent_idx 기반)
                    _pc_hint = compute_parent_instance_children_by_parent_idx(_hint_tree_paras)
                    try:
                        _excl_hint = compute_exclusivity_rules_code(_pc_hint) if _pc_hint else []
                    except Exception as _e:
                        log.warning(f"hint_tree 1d 계산 실패: {_e}")
                        _excl_hint = []
                    # chapter_types on hint_tree (deepcopy로 격리)
                    import copy as _cp_hint
                    _hint_struct = {
                        "paragraphs": _cp_hint.deepcopy(_hint_tree_paras),
                        "tables": structure.get("tables", []),
                    }
                    try:
                        _hint_struct = build_chapter_types_from_structure(_hint_struct)
                        _chapter_types_hint = _hint_struct.get("chapter_types", {})
                    except Exception as _e:
                        log.warning(f"hint_tree chapter_types 계산 실패: {_e}")
                        _chapter_types_hint = {}

                    # === 내적 일관성 측정 (Step B) ===
                    # stack tree와 parent_first tree 각각 parent_idx ↔ level 정합 체크.
                    # stack tree는 leaf-only 노드 skip 정책 때문에 level 갭 ≥ 2 발생 가능.
                    # parent_first tree는 BFS level 재계산이라 by construction 일관 (= 0).
                    _stack_inconsistency = measure_tree_inconsistency(_paras_now)
                    _pf_inconsistency = measure_tree_inconsistency(_hint_tree_paras)

                    # parent_idx 기반 1d on stack — level 기반과 비교해서 자기모순 정도 직접 노출
                    _pc_stack_by_pidx = compute_parent_instance_children_by_parent_idx(_paras_now)
                    try:
                        _excl_stack_by_pidx = compute_exclusivity_rules_code(_pc_stack_by_pidx) if _pc_stack_by_pidx else []
                    except Exception as _e:
                        log.warning(f"stack parent_idx-based 1d 실패: {_e}")
                        _excl_stack_by_pidx = []

                # ══════════════════════════════════════════════════════════
                # Parent-first 전환 — main path를 hint_tree로 교체
                #   - structure["paragraphs"]: stack post-correction → parent_first
                #   - 다운스트림(1d/chapter_types/2a/2b/assemble) 모두 parent_first 사용
                #   - stack post-correction snapshot은 dump용으로 보존
                # ══════════════════════════════════════════════════════════
                _role_registry_baseline = None  # canonicalize_by_data baseline (debug only)
                _canonical_clustering_dump = None  # 1e LLM raw + parsed
                _tree_rebuild_dump = None  # tree rebuild (1g) LLM raw + parsed
                if hybrid_mode and _hint_tree_paras is not None:
                    _stack_post_correction_paras = list(structure.get("paragraphs", []))
                    structure["paragraphs"] = _hint_tree_paras
                    log.info(
                        f"[1d parent-first] main path switched to hint_tree "
                        f"(changed {(_tree_diff or {}).get('edge_change_count', 0)} edges, "
                        f"stack inconsistencies {(_stack_inconsistency or {}).get('level_mismatch_count', 0)}, "
                        f"pf inconsistencies {(_pf_inconsistency or {}).get('level_mismatch_count', 0)})"
                    )



                # ── canonicalize_by_data baseline (debug/comparison only) ──
                # main path 영향 X. deepcopy로 격리 후 cluster 결과만 dump에 저장.
                import copy as _cp_baseline
                _baseline_paras = _cp_baseline.deepcopy(structure["paragraphs"])
                try:
                    _role_registry_baseline = canonicalize_by_data(_baseline_paras)
                    log.info(
                        f"[canonicalize_by_data baseline] {len(_role_registry_baseline)} clusters "
                        f"(debug only, main path 영향 X)"
                    )
                except Exception as _e:
                    log.warning(f"baseline canonicalize_by_data 실패: {_e}")
                    _role_registry_baseline = None

                # ══════════════════════════════════════════════════════════
                # 1d (1c 후) — TOC-based chapter unit planner.
                # 1e canonical clustering 전에 chapter regions 결정 → paragraph에
                # chapter_id 부여 → 1e가 chapter-aware cluster (같은 marker라도
                # 다른 chapter면 다른 cluster).
                # 이전엔 1f 후 위치였는데 cluster/tree가 chapter 정보 없이 결정되는
                # 비대칭 문제 해결 위해 1c 후로 이동.
                # ══════════════════════════════════════════════════════════
                _phase_e_chapter_planner = None
                try:
                    from open_webui.utils.hwpx_analyzer import (
                        has_toc_gate,
                        build_toc_based_chapter_plan_prompt,
                        parse_toc_based_chapter_plan_from_llm,
                        validate_toc_based_chapter_plan,
                        diagnose_1c_non_body_handling,
                        assign_chapter_ids_from_phase_e,
                    )
                    # 임시 section_results — 1c 시점이라 정식 section_results 아직 안 채워짐.
                    # has_toc_gate / build_prompt는 paragraphs + idx_texts + idx_full_texts만 사용.
                    _temp_sr_pe = {
                        0: {
                            "structure": {"paragraphs": structure.get("paragraphs", [])},
                            "idx_texts": _idx_texts,
                            "idx_full_texts": _idx_full_texts,
                        }
                    }
                    _pe_one_c_diag = diagnose_1c_non_body_handling(_temp_sr_pe)

                    # cache hit이면 cached phase_e 그대로 — 1e에 chapter_id 전달
                    _cached_phase_e_local = locals().get("_cached_phase_e")
                    if _from_cache and _cached_phase_e_local:
                        _phase_e_chapter_planner = {
                            **_cached_phase_e_local,
                            "one_c_diagnostic": _pe_one_c_diag,
                            "loaded_from_cache": True,
                        }
                        if __event_emitter__:
                            await __event_emitter__({"type": "status", "data": {
                                "description": "1d: cache hit, AI skip",
                                "done": False,
                            }})
                    else:
                        _pe_gate = has_toc_gate(_temp_sr_pe)
                        if _pe_gate.get("has_toc"):
                            if __event_emitter__:
                                await __event_emitter__({"type": "status", "data": {
                                    "description": "1d: TOC 기반 chapter regions 결정 중...",
                                    "done": False,
                                }})

                            _pe_toc = []
                            _pe_seen = set()
                            for _h in _pe_gate.get("toc_paragraph_hints", []):
                                _sid = _h["section_id"]
                                _lid = _h["local_idx"]
                                if (_sid, _lid) in _pe_seen:
                                    continue
                                _pe_seen.add((_sid, _lid))
                                _pe_toc.append({
                                    "section_id": _sid,
                                    "local_idx": _lid,
                                    "role": _h.get("role", ""),
                                    "text": _idx_full_texts.get(str(_lid)) or _idx_full_texts.get(_lid) or _h.get("text_preview", ""),
                                })

                            _pe_body: dict = {}
                            _pe_tree: dict = {}
                            _pe_all_idxs: dict = {}
                            _bl, _tl, _idxs = [], [], set()
                            for _p in structure.get("paragraphs", []):
                                _li = _p.get("idx")
                                if _li is None:
                                    continue
                                _idxs.add(int(_li))
                                _bl.append({
                                    "local_idx": _li,
                                    "marker": _p.get("marker", ""),
                                    "role": _p.get("canonical_role") or _p.get("role") or "",
                                    "text": _idx_texts.get(str(_li)) or _idx_texts.get(_li) or "",
                                })
                                _tl.append({
                                    "local_idx": _li,
                                    "level": _p.get("level"),
                                    "parent_idx": _p.get("parent_idx"),
                                })
                            _pe_body[0] = _bl
                            _pe_tree[0] = _tl
                            _pe_all_idxs[0] = _idxs

                            _pe_msgs = build_toc_based_chapter_plan_prompt(
                                toc_paragraphs=_pe_toc,
                                body_paragraphs_by_section=_pe_body,
                                one_c_tree_by_section=_pe_tree,
                            )
                            _pe_plan = None
                            _pe_retry = 0
                            _pe_last_err = None
                            while _pe_retry <= 1:
                                _pe_task = "hwpx_phase_e_toc_plan" if _pe_retry == 0 else "hwpx_phase_e_toc_plan_retry"
                                try:
                                    _pe_raw = await _call_llm(_pe_msgs, _pe_task)
                                    _pe_parsed = parse_toc_based_chapter_plan_from_llm(_pe_raw)
                                    if "parse_error" not in _pe_parsed:
                                        _pe_plan = _pe_parsed
                                        break
                                    _pe_last_err = _pe_parsed.get("parse_error")
                                    log.warning(f"[1d pre-1e] parse error (retry {_pe_retry}): {_pe_last_err}")
                                except Exception as _pe_ce:
                                    _pe_last_err = str(_pe_ce)
                                    log.warning(f"[1d pre-1e] AI 호출 실패 (retry {_pe_retry}): {_pe_ce}")
                                _pe_retry += 1

                            if _pe_plan is not None:
                                _pe_validated = validate_toc_based_chapter_plan(_pe_plan, _pe_all_idxs)
                                _pe_vr = _pe_validated.get("validation_result") or {}
                                _pe_status = "validation_fallback" if _pe_vr.get("fallback_required") else "ok"
                                _phase_e_chapter_planner = {
                                    "gate": _pe_gate, "status": _pe_status,
                                    "toc_plan": _pe_validated,
                                    "one_c_diagnostic": _pe_one_c_diag,
                                    "retry_count": _pe_retry,
                                }
                            else:
                                _phase_e_chapter_planner = {
                                    "gate": _pe_gate, "status": "ai_call_failed",
                                    "toc_plan": None,
                                    "one_c_diagnostic": _pe_one_c_diag,
                                    "retry_count": _pe_retry,
                                    "last_error": _pe_last_err,
                                }
                        else:
                            _phase_e_chapter_planner = {
                                "gate": _pe_gate, "status": "no_toc_deferred",
                                "toc_plan": None,
                                "one_c_diagnostic": _pe_one_c_diag,
                                "retry_count": 0,
                            }

                    # paragraph에 chapter_id 부여 (1e가 chapter-aware cluster용)
                    _ch_id_stats = assign_chapter_ids_from_phase_e(structure, _phase_e_chapter_planner)
                    log.info(
                        f"[1d pre-1e] chapter_ids: assigned={_ch_id_stats['assigned']} "
                        f"to {_ch_id_stats['chapter_count']} chapters, "
                        f"no_chapter={_ch_id_stats['no_chapter']}"
                    )
                except Exception as _pe_e:
                    log.warning(f"[1d pre-1e] 실패 (1e가 chapter-aware 안 됨, -1 fallback): {_pe_e}", exc_info=True)
                    _phase_e_chapter_planner = {"error": str(_pe_e), "debug_only": True}
                    for _p in structure.get("paragraphs", []):
                        _p["chapter_id"] = -1

    # ── 1e: AI structural canonicalization ──
                # Flow:
                #   1. 1e LLM call → parse → validate
                #   2. parse 실패 → canonicalize_by_data (최후 fallback)
                #   3. parse 성공 + issues 있음 → 1e repair LLM call → parse → validate
                #   4. repair parse 실패 또는 여전히 issues → canonicalize_by_data
                #   5. issues 없음 → 적용
                from open_webui.utils.hwpx_analyzer import (
                    build_canonical_clustering_prompt,
                    build_canonical_clustering_repair_prompt,
                    parse_canonical_clustering_from_llm,
                    apply_structural_clustering,
                    canonicalize_by_data,
                )

                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": "1e: structural cluster ID 할당 (AI)...",
                        "done": False
                    }})

                _1e_messages = build_canonical_clustering_prompt(
                    paragraphs=structure["paragraphs"],
                    role_candidates=role_candidates,
                    decisions=level_parsed.get("decisions", {}),
                )
                _1e_llm_raw = await _call_llm(_1e_messages, "hwpx_canonical_clustering")

                _expected_idxs = {p.get("idx") for p in structure["paragraphs"] if p.get("idx") is not None}

                _1e_repair_messages = None
                _1e_repair_raw = None
                _1e_repair_parsed = None
                _1e_final_source = None  # "1e_original" / "1e_repaired" / "fallback_baseline"

                try:
                    _1e_parsed = parse_canonical_clustering_from_llm(_1e_llm_raw, _expected_idxs)
                except Exception as _e_parse:
                    log.error(f"1e LLM JSON parse 실패: {_e_parse}. fallback to baseline.")
                    _role_registry = canonicalize_by_data(structure["paragraphs"])
                    _1e_final_source = "fallback_baseline"
                    _1e_parsed = None
                    _fallback_reason = f"parse_failed: {_e_parse}"
                else:
                    # parse 성공. 무조건 repair 호출 (의미 검증 책임은 repair).
                    # 코드 validation 은 idx 형식 (누락/중복/extra) 만 검사 — marker family / parent / chapter / level 위반은
                    # repair LLM 이 hard constraint 보고 직접 검증 + 정정.
                    log.info(
                        f"1e parse OK (idx issues: {_1e_parsed.get('issues', [])}). "
                        f"Always calling repair for semantic validation."
                    )
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {
                            "description": "1e repair: 의미 검증 + 정정 (AI)...",
                            "done": False
                        }})

                    _1e_repair_messages = build_canonical_clustering_repair_prompt(
                        paragraphs=structure["paragraphs"],
                        previous_clusters=_1e_parsed["clusters"],
                        issues=_1e_parsed["issues"],
                        role_candidates=role_candidates,
                        decisions=level_parsed.get("decisions", {}),
                    )
                    _1e_repair_raw = await _call_llm(_1e_repair_messages, "hwpx_canonical_clustering_repair")

                    try:
                        _1e_repair_parsed = parse_canonical_clustering_from_llm(_1e_repair_raw, _expected_idxs)
                    except Exception as _e_repair_parse:
                        log.error(f"1e repair JSON parse 실패: {_e_repair_parse}.")
                        # repair parse 실패 → 1e original 시도 (idx OK 면 의미 wrong 이어도 적용)
                        if not _1e_parsed.get("issues"):
                            log.warning("Falling back to 1e original (idx OK, semantic wrong may remain).")
                            _role_registry = apply_structural_clustering(
                                structure["paragraphs"],
                                _1e_parsed["cluster_map"],
                                _1e_parsed["clusters"],
                            )
                            _1e_final_source = "1e_original"
                            _fallback_reason = f"repair_parse_failed: {_e_repair_parse}"
                        else:
                            log.warning("1e original also has idx issues. Falling back to baseline.")
                            _role_registry = canonicalize_by_data(structure["paragraphs"])
                            _1e_final_source = "fallback_baseline"
                            _fallback_reason = f"both_failed: repair_parse={_e_repair_parse}, original_issues={_1e_parsed['issues']}"
                    else:
                        if _1e_repair_parsed.get("issues"):
                            log.error(
                                f"1e repair has idx issues: {_1e_repair_parsed['issues']}."
                            )
                            # repair 가 idx 깨먹음 → 1e original 시도 (idx OK 면)
                            if not _1e_parsed.get("issues"):
                                log.warning("Falling back to 1e original (idx OK, semantic wrong may remain).")
                                _role_registry = apply_structural_clustering(
                                    structure["paragraphs"],
                                    _1e_parsed["cluster_map"],
                                    _1e_parsed["clusters"],
                                )
                                _1e_final_source = "1e_original"
                                _fallback_reason = f"repair_idx_issues: {_1e_repair_parsed['issues']}"
                            else:
                                log.warning("Both have idx issues. Falling back to baseline.")
                                _role_registry = canonicalize_by_data(structure["paragraphs"])
                                _1e_final_source = "fallback_baseline"
                                _fallback_reason = f"both_idx_issues: repair={_1e_repair_parsed['issues']}, original={_1e_parsed['issues']}"
                        else:
                            # repair 성공 — 적용
                            _role_registry = apply_structural_clustering(
                                structure["paragraphs"],
                                _1e_repair_parsed["cluster_map"],
                                _1e_repair_parsed["clusters"],
                            )
                            _1e_final_source = "1e_repaired"
                            _fallback_reason = None
                            log.info(f"[1e repair] 성공 — {len(_role_registry)} clusters")

                _canonical_clustering_dump = {
                    "prompt_messages": _1e_messages,
                    "llm_raw_response": _1e_llm_raw,
                    "parsed": _1e_parsed,
                    "repair_attempted": _1e_repair_messages is not None,
                    "repair_prompt_messages": _1e_repair_messages,
                    "repair_llm_raw_response": _1e_repair_raw,
                    "repair_parsed": _1e_repair_parsed,
                    "final_source": _1e_final_source,
                    "fallback_reason": _fallback_reason,
                }
                log.info(
                    f"[1e canonicalization] {len(_role_registry)} clusters, "
                    f"final_source={_1e_final_source}"
                )

                # ══════════════════════════════════════════════════════════
                # Tree rebuild (1g) — cluster 확정 후 트리 재구성 (별도 LLM)
                # 1c level/parent 는 paragraph 단위 추론 한계로 wrong 가능.
                # cluster + 의미만 보고 parent_idx + level 재계산. fallback 없음 — wrong 시 RuntimeError.
                # ══════════════════════════════════════════════════════════
                from open_webui.utils.hwpx_analyzer import (
                    build_tree_rebuild_prompt,
                    parse_tree_rebuild_from_llm,
                    apply_tree_rebuild_to_paragraphs,
                )

                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": "tree rebuild: cluster + 의미로 트리 재구성 (AI)...",
                        "done": False
                    }})

                _tree_messages = build_tree_rebuild_prompt(
                    paragraphs=structure["paragraphs"],
                    decisions=level_parsed.get("decisions", {}),
                    idx_texts=_idx_texts,
                )
                _tree_raw = await _call_llm(_tree_messages, "hwpx_tree_rebuild")
                _tree_expected_idxs = {
                    p.get("idx") for p in structure["paragraphs"] if p.get("idx") is not None
                }
                _tree_parsed = parse_tree_rebuild_from_llm(_tree_raw, _tree_expected_idxs)

                if _tree_parsed["issues"]:
                    raise RuntimeError(
                        f"tree_rebuild: validation failed: {_tree_parsed['issues']}"
                    )

                apply_tree_rebuild_to_paragraphs(structure["paragraphs"], _tree_parsed["tree"])

                _tree_rebuild_dump = {
                    "prompt_messages": _tree_messages,
                    "llm_raw_response": _tree_raw,
                    "parsed": _tree_parsed,
                }
                log.info(f"[tree rebuild] 성공 — {len(_tree_parsed['tree'])} paragraphs")

                # role 확정 + 보정 이후 signals 재계산
                _signals = compute_role_context_signals(
                    structure.get("paragraphs", []), idx_texts=_idx_texts
                )

                # ══════════════════════════════════════════════════════════
                # 1c 후처리 (코드, 번호 X): 형제 배타 규칙 — 보정된 트리 기준  [코드 식별자 "1d" 잔존]
                #   parent-first 전환 후엔 parent_idx 기반 직접 계산 (level 기반 stack 재구성 우회)
                # ══════════════════════════════════════════════════════════
                from open_webui.utils.hwpx_analyzer import (
                    compute_parent_instance_children_by_parent_idx,
                    compute_exclusivity_rules_code,
                    compute_sibling_cooccurrence_rules,
                    compute_format_rules_code,
                )
                exclusive_rules = []
                sibling_cooccurrence_rules = []
                if hybrid_mode:
                    _pc_data = compute_parent_instance_children_by_parent_idx(structure.get("paragraphs", []))
                else:
                    _pc_data = compute_parent_instance_children(structure)
                if _pc_data:
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "1d: 형제 배타규칙 계산 중 (코드)...", "done": False}})
                    # 옛 blacklist (variants) — debug 보존만, 2b prompt에서는 사용 X
                    try:
                        exclusive_rules = compute_exclusivity_rules_code(_pc_data)
                    except Exception as _e:
                        log.warning(f"1d 코드 계산 실패 (variants): {_e}")
                        exclusive_rules = []
                    if exclusive_rules:
                        structure["exclusive_rules"] = exclusive_rules
                    # 새 white-list (cooccurrence + variant + sample) — 2b prompt 입력
                    # v10: paragraphs + idx_texts 받아 variant에 양식 instance sample (marker + text) 포함
                    try:
                        sibling_cooccurrence_rules = compute_sibling_cooccurrence_rules(
                            paragraphs=structure.get("paragraphs", []),
                            idx_texts=_idx_texts,
                        )
                    except Exception as _e:
                        log.warning(f"1d cooccurrence 계산 실패: {_e}")
                        sibling_cooccurrence_rules = []
                    if sibling_cooccurrence_rules:
                        structure["sibling_cooccurrence_rules"] = sibling_cooccurrence_rules

                # ══════════════════════════════════════════════════════════
                # 1h 전 (코드, 번호 X): format/blank 규칙 — 관측 카운트 기반  [코드 식별자 "1e_format" 잔존]
                # AI 호출 폐기. 결정적·고속·무토큰.
                # ══════════════════════════════════════════════════════════
                format_rules = {}
                blank_rules = []
                _format_obs = compute_format_observations(structure, _section_light_xml, idx_map=_section_idx_map)
                if _format_obs.get("role_formats") or _format_obs.get("transitions"):
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "1e: 빈 줄·들여쓰기 규칙 계산 중 (코드)...", "done": False}})
                    try:
                        _parsed_format = compute_format_rules_code(_format_obs)
                        format_rules = _parsed_format.get("format_rules", {})
                        blank_rules = _parsed_format.get("blank_rules", [])
                    except Exception as _e:
                        log.warning(f"1e 코드 계산 실패: {_e}")
                        format_rules = {}
                        blank_rules = []
                    if format_rules:
                        structure["format_rules"] = format_rules
                    if blank_rules:
                        structure["blank_rules"] = blank_rules
                # ══════════════════════════════════════════════════════════
                # 1h: Marker policy induction (role-level, post-clustering)  [코드 식별자 "1f" 잔존]
                # ══════════════════════════════════════════════════════════
                _marker_policy_1f = None
                try:
                    from open_webui.utils.hwpx_analyzer import (
                        build_marker_policy_prompt,
                        parse_marker_policy_from_llm,
                        verify_marker_policy_evidence,
                    )
                    if __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "1f: role별 마커 정책 판별 중...", "done": False}})
                    _msgs_1f = build_marker_policy_prompt(
                        structure.get("paragraphs", []), _idx_texts,
                        light_xml=_section_light_xml,
                    )
                    _llm_1f = await _call_llm(_msgs_1f, "hwpx_1f_marker_policy")
                    _marker_policy_1f = parse_marker_policy_from_llm(_llm_1f)
                    _marker_policy_1f = verify_marker_policy_evidence(_marker_policy_1f, _idx_texts)
                    structure["marker_policy_1f"] = _marker_policy_1f
                    _verified = sum(1 for r in _marker_policy_1f.get("roles", []) if r.get("verification") == "consistent")
                    _total = len(_marker_policy_1f.get("roles", []))
                    log.info(f"[1f] marker policy: {_verified}/{_total} verified")
                except Exception as _e:
                    log.warning(f"[1f] marker policy induction 실패: {_e}")
                    _marker_policy_1f = None

                structure = build_chapter_types_from_structure(structure)
                chapter_types = structure.get("chapter_types", {})

                # 캐시 저장 전 구조 validation (7단계 gate)
                from open_webui.utils.hwpx_analyzer import (
                    validate_structure_for_cache, write_cache_validation_debug,
                )
                _cache_validation = validate_structure_for_cache(structure, chapter_types)
                _debug_dir = "/tmp/hwpx_debug"
                write_cache_validation_debug(_cache_validation, _debug_dir)

                # B2.1.2b: section별 _cache_validation 수집 (05d dump + 05b debug용).
                _section_cache_validations[section_id] = _cache_validation

                # B2.1.2b: section0 기준 cache gate + should_abort (임시 backward compat).
                # section0 can_cache=True면 incremental cache save 허용. section1~4 결과는 debug only.
                # should_abort도 section0만 트리거 (보수적, false positive 방지).
                if section_id == 0:
                    _section0_can_cache = _cache_validation.get("can_cache", False)
                    if _cache_validation["should_abort"]:
                        _triggered = [c for c in _cache_validation["checks"] if c["triggered"]]
                        raise ValueError(
                            f"구조 분석 오류 — 캐시 저장 안 함: "
                            + ", ".join(f"{c['check_id']}:{c['name']}" for c in _triggered)
                        )

                # B2.1.1: section_results 채움 (section0 기준 single section).
                # paragraphs/parent_idx는 section-local 그대로 (document-global 변환은 B3).
                # document-level merge 금지 — chapter_types/marker_policy_1f도 section-local.
                section_results[section_id] = {
                    "structure": structure,
                    "chapter_types": chapter_types,
                    "marker_policy_1f": _marker_policy_1f,
                    "signals": _signals,
                    "idx_texts": _idx_texts,
                    "idx_full_texts": _idx_full_texts,
                }

                # B2.1.2b: cache save (incremental — 매 iteration 누적 저장).
                # section_results dict는 mutable이라 매 save가 현재까지의 모든 section 포함.
                # cache gate: section0 can_cache 기준 (임시 backward compat, B0b/B3에서 정책 결정).
                if _section0_can_cache and 0 in section_results:
                    sr0 = section_results[0]
                    try:
                        save_template_cache(_cache_key, {
                            "structure": sr0["structure"],
                            "chapter_types": sr0["chapter_types"],
                            "signals": sr0["signals"],
                            "idx_texts": sr0["idx_texts"],
                            "idx_full_texts": sr0["idx_full_texts"],
                            "marker_policy_1f": sr0["marker_policy_1f"],
                            "paragraph_count": analysis.get("paragraph_count", 0),
                            "table_count": analysis.get("table_count", 0),
                            "template_file_id": template_file_id,
                            "section_count": len(section_results),
                            "section_results": section_results,
                            # 1d 결과 (v7+ — 1c 후 위치에서 결정, paragraph chapter_id 포함)
                            "phase_e_chapter_planner": locals().get("_phase_e_chapter_planner"),
                        })
                    except Exception as _e:
                        log.warning(f"[CACHE] section {section_id} 저장 실패: {_e}")

                # B2.1.2b: _debug_payload는 section_id == 0 일 때만 outer set (backward compat alias).
                # dict 내부 raw vars (split_log, marker_norm 등)는 reference — 이후 section iteration에서
                # 변수 덮어쓰기되어도 dict 안 reference는 section0 시점 값 유지 (multi-iteration 안전).
                if section_id == 0:
                  _debug_payload = {
                    "model": model_id,
                    "from_cache": False,
                    "cache_path": get_template_cache_path(_cache_key),
                    "cache_key": _cache_key,
                    "cache_validation": _cache_validation,
                    "marker_policy_1f": _marker_policy_1f,
                    "llm_raw_response": llm_content_1,
                    "structure_before_split": {
                        "paragraphs": _paragraphs_before,
                        "tables": structure.get("tables", []),
                    },
                    "structure_after_split": {
                        "paragraphs": structure.get("paragraphs", []),
                        "tables": structure.get("tables", []),
                        "chapter_types": structure.get("chapter_types", {}),
                        "template_grammar": structure.get("template_grammar", {}),
                        "role_text_types": structure.get("role_text_types", {}),
                        "per_type_role_semantics": structure.get("per_type_role_semantics", {}),
                    },
                    "split_log": _split_log,
                    "marker_normalization": _marker_norm,
                    "signals": _signals,
                    "xml": {
                        "light_xml_size": len(_section_light_xml),
                        "truncated_xml_size": len(_section_truncated_xml),
                        "removed_indices_count": len(_section_removed_indices),
                    },
                    "1b_role_candidates": {  # AI 1 (먼저 실행)
                        "prompt_messages": messages_role,
                        "llm_raw_response": llm_content_role,
                        "role_candidates": role_candidates,
                    },
                    "1c_structure_global": {  # AI 2 (다음 실행)
                        "prompt_messages": messages_level,
                        "llm_raw_response": llm_content_level,
                        "level_map": level_map,
                        "decisions": level_parsed.get("decisions", {}),
                        "validator_issues": structure.get("validator_issues", {}),
                    },
                    "level_analysis": {  # 하위호환 (덤프 분석 스크립트용)
                        "structure_with_levels": structure,
                        "chapter_types": chapter_types,
                    },
                    "parent_correction": {
                        "container_scores": container_scores,
                        "reattach_log": _reattach_log,
                        "reparent_log": _reparent_log,
                        "before_paragraphs": _para_before_correction,                  # 1c 직후, 보정 전
                        "stack_post_correction_paragraphs": _stack_post_correction_paras,  # stack 보정 후 (parent-first 전환 직전)
                        "after_paragraphs": structure.get("paragraphs", []),           # 최종 (1e cluster_id 적용 후, 다운스트림 입력)
                    },
                    "1e_canonical_clustering": {
                        "role_registry": _role_registry,                               # main path 적용 결과 (1e AI or fallback)
                        "role_registry_baseline_code": _role_registry_baseline,        # canonicalize_by_data baseline (debug only)
                        "llm": _canonical_clustering_dump,                             # prompt + raw + parsed
                    },
                    "tree_rebuild": _tree_rebuild_dump,                                # 1g — tree rebuild LLM dump (prompt + raw + parsed)
                    "parent_hint_measurement": {
                        "enabled": hybrid_mode,
                        "decisions": (level_parsed.get("decisions", {}) if hybrid_mode else {}),
                        "validation": _hint_validation,
                        "conflicts": _hint_conflicts,
                        "override_tree": _hint_override_paras,
                        "core_cases": _core_cases,
                        "tree_comparison": {
                            "diff": _tree_diff,
                            "hint_tree_paragraphs_slim": ([
                                {
                                    "idx": p.get("idx"),
                                    "role": p.get("role"),
                                    "parent_idx": p.get("parent_idx"),
                                    "level": p.get("level"),
                                    "sibling_group_id": p.get("sibling_group_id"),
                                }
                                for p in (_hint_tree_paras or [])
                            ] if _hint_tree_paras else []),
                            "hint_tree_parent_instances": {
                                k: [sorted(list(v)) for v in vs]
                                for k, vs in (_pc_hint or {}).items()
                            },
                            "hint_tree_exclusive_rules": _excl_hint,
                            "hint_tree_chapter_types": _chapter_types_hint,
                        } if hybrid_mode else None,
                        "tree_self_consistency": {
                            "stack": _stack_inconsistency,
                            "parent_first": _pf_inconsistency,
                            "stack_pc_by_parent_idx": {
                                k: [sorted(list(v)) for v in vs]
                                for k, vs in (_pc_stack_by_pidx or {}).items()
                            },
                            "stack_exclusive_rules_by_parent_idx": _excl_stack_by_pidx,
                        } if hybrid_mode else None,
                    },
                    "exclusivity_analysis": {
                        "from_code": True,
                        "parent_instances": {
                            k: [sorted(list(v)) for v in vs]
                            for k, vs in (_pc_data or {}).items()
                        },
                        "exclusive_rules": exclusive_rules,
                        "before_correction": {
                            "parent_instances": {
                                k: [sorted(list(v)) for v in vs]
                                for k, vs in (_pc_before or {}).items()
                            },
                            "exclusive_rules": _exclusive_before,
                        },
                    },
                    "format_analysis": {
                        "from_code": True,
                        "observations": _format_obs,
                        "format_rules": format_rules,
                        "blank_rules": blank_rules,
                    },
                  }
            # === B2.1.2b: loop 끝 — 05d dump + outer 변수 복원 + dry-run break ===
            # cache save는 loop 안에서 매 iteration incremental 저장 (옵션 사용자, v3).
            # validation aggregation 제거 — section0 cache gate는 loop 안에서 직접 set (_section0_can_cache).
            # section1~4 validation은 debug only (05d 파일). 정책 결정은 B0b/B3 review에서.

            # B2.1.2b: section별 validation 결과 별도 파일에 dump (debug-only, hard gate 사용 X).
            _section_can_caches = {sid: v.get("can_cache") for sid, v in _section_cache_validations.items()}
            try:
                import json as _json_scv
                import os as _os_scv
                from datetime import datetime as _dt_scv
                _scv_path = "/tmp/hwpx_debug/05d_section_cache_validations.json"
                _os_scv.makedirs(_os_scv.path.dirname(_scv_path), exist_ok=True)
                with open(_scv_path, "w", encoding="utf-8") as _scv_f:
                    _json_scv.dump({
                        "generated_at": _dt_scv.now().isoformat(),
                        "section_count": len(_section_cache_validations),
                        "section0_can_cache": _section0_can_cache,
                        "cache_gate_basis": "section0_only (B2.1.2b 임시 backward compat)",
                        "section_can_caches": {str(sid): v for sid, v in _section_can_caches.items()},
                        "section_validations": {
                            str(sid): v for sid, v in _section_cache_validations.items()
                        },
                        "policy_note": "B2.1.2b: section0 기준 can_cache로 양식 단위 cache gate. section1~4 결과는 debug only. B0b/B3에서 정책 결정.",
                    }, _scv_f, ensure_ascii=False, indent=2, default=str)
            except Exception as _scv_e:
                log.warning(f"[05d dump 실패] {_scv_e}")

            # B2.1.2b: outer 변수 복원 — 캐시 미스/히트 경로 간 변수 형태 통일.
            # 미스 경로에서 방금 저장한 캐시를 다시 load해서 히트 경로와 동일한 변수 형태로 set.
            # 그러면 1j/1k/2a/2b/2c 등 후속 단계가 미스/히트 무관하게 같은 데이터 형태 (JSON 디코딩 결과,
            # section_results는 str key dict)를 받음.
            _post_save_cached = None
            if _section0_can_cache:
                try:
                    _post_save_cached = load_template_cache(_cache_key, namespace='full')
                except Exception as _pse:
                    log.warning(f"[CACHE] 미스 경로 끝 reload 실패: {_pse}")

            if _post_save_cached:
                structure = _post_save_cached["structure"]
                chapter_types = _post_save_cached.get("chapter_types", {})
                _signals = _post_save_cached.get("signals", {})
                _idx_texts = _post_save_cached.get("idx_texts", {})
                _idx_full_texts = _post_save_cached.get("idx_full_texts", {})
                _marker_policy_1f_cache = _post_save_cached.get("marker_policy_1f")
                if _marker_policy_1f_cache:
                    structure["marker_policy_1f"] = _marker_policy_1f_cache
                    _marker_policy_1f = _marker_policy_1f_cache
                # JSON에서 로드된 section_results는 키가 str. 기존 코드(int 키 조회) 호환 위해 int로 변환.
                _raw_sr = _post_save_cached.get("section_results", {})
                section_results = {(int(k) if str(k).isdigit() else k): v for k, v in _raw_sr.items()}
                _phase_e_chapter_planner = _post_save_cached.get("phase_e_chapter_planner")
                _cached_track_c = _post_save_cached.get("chapter_pattern_family")
            elif 0 in section_results:
                # 캐시 저장 못한 경우 fallback — 메모리 변수 사용
                sr0 = section_results[0]
                structure = sr0["structure"]
                chapter_types = sr0["chapter_types"]
                _signals = sr0["signals"]
                _idx_texts = sr0["idx_texts"]
                _idx_full_texts = sr0["idx_full_texts"]
                _marker_policy_1f = sr0["marker_policy_1f"]


        else:
            # 캐시 로드 완료 → structure, chapter_types, _signals_cache, _idx_texts_cache 이미 세팅됨
            _signals = _signals_cache
            _idx_texts = _idx_texts_cache
            _idx_full_texts = _cached.get("idx_full_texts", {})
            _debug_payload = {
                "model": model_id,
                "from_cache": True,
                "cache_path": get_template_cache_path(_cache_key),
                "cache_key": _cache_key,
                "signals": _signals,
                "xml": {
                    "light_xml_size": len(light_xml),
                    "truncated_xml_size": len(truncated_xml),
                    "removed_indices_count": len(removed_indices),
                },
                "level_analysis": {
                    "prompt_messages": [],
                    "llm_raw_response": "[FROM CACHE]",
                    "level_map": {},
                    "structure_with_levels": structure,
                    "chapter_types": chapter_types,
                },
            }

        # B2.1.2c: section_results_debug 분리 (cache miss/hit 공통, debug-only).
        # section_results는 cache miss (loop 안 채움) 또는 cache hit (load) 둘 다 사용 가능.
        # cache_validation은 cache miss 시 _section_cache_validations, cache hit 시 빈 (cache JSON 미저장).
        try:
            _scv_for_debug = _section_cache_validations if '_section_cache_validations' in dir() else {}
            if section_results and isinstance(_debug_payload, dict):
                _debug_payload["section_results_debug"] = {
                    str(sid): {
                        "paragraph_count": len(sv.get("structure", {}).get("paragraphs", [])),
                        "table_count": len(sv.get("structure", {}).get("tables", [])),
                        "chapter_types_keys": list(sv.get("chapter_types", {}).keys()),
                        "marker_policy_1f_present": sv.get("marker_policy_1f") is not None,
                        "role_count": len({
                            p.get("role") for p in sv.get("structure", {}).get("paragraphs", [])
                            if p.get("role")
                        }),
                        "cache_validation": _scv_for_debug.get(sid, {}),
                    }
                    for sid, sv in section_results.items()
                }
        except Exception as _srd_e:
            log.warning(f"[B2.1.2c] section_results_debug attach 실패: {_srd_e}")

        # ══════════════════════════════════════════════════════════════
        # 1d debug payload 통합 — 실제 호출은 1c 후로 이동됨.
        # 이 위치에서는 1c 후에 저장된 _phase_e_chapter_planner를 _debug_payload에
        # set + cache save (cache miss 시) 만 수행. 호환을 위한 변수 유지.
        # ══════════════════════════════════════════════════════════════
        try:
            # 1c 후 위치에서 set된 _phase_e_chapter_planner (또는 cache hit 시 cached) 사용.
            # cache hit branch에서는 1c 후 위치가 cached 데이터 사용했음.
            _section_count = len(section_results) if isinstance(section_results, dict) else 0
            _is_multi_section = _section_count > 1
            _phase_e_for_debug = locals().get("_phase_e_chapter_planner")
            if _phase_e_for_debug is None:
                _phase_e_for_debug = {"status": "not_executed", "toc_plan": None}
            _debug_payload["phase_e_chapter_planner"] = {
                **_phase_e_for_debug,
                "section_0_only_due_to_multi_section": _is_multi_section,
            }
            _phase_e_skipped_by_cache = bool(_phase_e_for_debug.get("loaded_from_cache"))
        except Exception as _pe_dbg_e:
            log.warning(f"[1d debug attach] 실패: {_pe_dbg_e}")
            _debug_payload["phase_e_chapter_planner"] = {"error": str(_pe_dbg_e), "debug_only": True}
            _phase_e_skipped_by_cache = False

        # _section_results_for_phase_e — 1i가 참조하는 변수, 통일된 view 유지
        _section_results_for_phase_e = (
            {0: section_results[0]}
            if (_is_multi_section and 0 in section_results) else section_results
        )

        # ══════════════════════════════════════════════════════════════
        # 1i — Chapter Pattern Family Analysis (cache 통합, v6+)
        # cache hit 시 _cached_track_c 사용 + AI 호출 skip
        # 1d status=ok일 때만 호출.
        # ══════════════════════════════════════════════════════════════
        try:
            _tc_pe_status = (_debug_payload.get("phase_e_chapter_planner") or {}).get("status")
            _cached_track_c_local = locals().get("_cached_track_c")
            # cache hit 시 track_c cache 사용
            if _from_cache and _cached_track_c_local and _tc_pe_status == "ok":
                _debug_payload["chapter_pattern_family"] = {
                    **_cached_track_c_local,
                    "loaded_from_cache": True,
                }
                _track_c_skipped_by_cache = True
            else:
                _track_c_skipped_by_cache = False

            if not _track_c_skipped_by_cache and _tc_pe_status == "ok":
                from open_webui.utils.hwpx_analyzer import (
                    extract_generation_unit_subtrees,
                    build_chapter_pattern_family_prompt,
                    parse_chapter_pattern_family_from_llm,
                    validate_chapter_pattern_family,
                )
                _tc_pe_plan = _debug_payload["phase_e_chapter_planner"].get("toc_plan") or {}
                # multi-section 양식이면 section 0 only로 동일하게 처리
                _tc_section_results = locals().get("_section_results_for_phase_e") or section_results
                _tc_subtrees = extract_generation_unit_subtrees(_tc_pe_plan, _tc_section_results)
                if _tc_subtrees:
                    _tc_msgs = build_chapter_pattern_family_prompt(_tc_subtrees)
                    _tc_plan = None
                    _tc_retry = 0
                    _tc_last_err = None
                    while _tc_retry <= 1:
                        _tc_task = "hwpx_track_c_pattern_family" if _tc_retry == 0 else "hwpx_track_c_pattern_family_retry"
                        try:
                            _tc_raw = await _call_llm(_tc_msgs, _tc_task)
                            _tc_parsed = parse_chapter_pattern_family_from_llm(_tc_raw)
                            if "parse_error" not in _tc_parsed:
                                _tc_plan = _tc_parsed
                                break
                            _tc_last_err = _tc_parsed.get("parse_error")
                            log.warning(f"[1i] parse error (retry {_tc_retry}): {_tc_last_err}")
                        except Exception as _tc_ce:
                            _tc_last_err = str(_tc_ce)
                            log.warning(f"[1i] AI 호출 실패 (retry {_tc_retry}): {_tc_ce}")
                        _tc_retry += 1
                    if _tc_plan is not None:
                        _tc_validated = validate_chapter_pattern_family(_tc_plan, len(_tc_subtrees))
                        _debug_payload["chapter_pattern_family"] = {
                            "status": "ok",
                            "subtree_summary": [
                                {
                                    "unit_index": s["unit_index"],
                                    "title": s["title"],
                                    "subtree_paragraph_total": s["subtree_paragraph_total"],
                                    "structural_summary": s["structural_summary"],
                                    "parent_container_index": s["parent_container_index"],
                                }
                                for s in _tc_subtrees
                            ],
                            "result": _tc_validated,
                            "retry_count": _tc_retry,
                        }
                    else:
                        _debug_payload["chapter_pattern_family"] = {
                            "status": "ai_call_failed",
                            "retry_count": _tc_retry,
                            "last_error": _tc_last_err,
                        }
                else:
                    _debug_payload["chapter_pattern_family"] = {
                        "status": "no_generation_units",
                    }
            else:
                _debug_payload["chapter_pattern_family"] = {
                    "status": "skipped",
                    "reason": f"phase_e status={_tc_pe_status!r}",
                }
        except Exception as _tc_e:
            log.warning(f"[1i] block 실패 (debug-only, 생성 무영향): {_tc_e}")
            _debug_payload["chapter_pattern_family"] = {
                "error": str(_tc_e),
                "debug_only": True,
            }

        # ══════════════════════════════════════════════════════════════
        # 1d + 1i cache 통합 + chapter_types PRODUCTION 전환
        # 1) cache miss 시: 1d + 1i 결과를 cache에 추가 저장
        # 2) chapter_types를 1d 결과로 덮어쓰기 (in-memory + section_results)
        # ══════════════════════════════════════════════════════════════
        try:
            from open_webui.utils.hwpx_analyzer import (
                load_template_cache, save_template_cache,
                _phase_e_to_chapter_types,
            )

            _pe_final = _debug_payload.get("phase_e_chapter_planner") or {}
            _tc_final = _debug_payload.get("chapter_pattern_family") or {}

            # 1) cache update — 1d AI 새 호출했으면 cache에 저장 (cache hit이어도 phase_e 누락 시 저장)
            _pe_skipped_local = locals().get("_phase_e_skipped_by_cache", False)
            if not _pe_skipped_local:
                try:
                    _cache_data = load_template_cache(_cache_key, namespace='full')
                    if _cache_data:
                        # loaded_from_cache 플래그 제외하고 저장
                        _pe_for_cache = {k: v for k, v in _pe_final.items() if k != "loaded_from_cache"}
                        _tc_for_cache = {k: v for k, v in _tc_final.items() if k != "loaded_from_cache"}
                        _cache_data["phase_e_chapter_planner"] = _pe_for_cache
                        _cache_data["chapter_pattern_family"] = _tc_for_cache
                        save_template_cache(_cache_key, _cache_data)
                        log.info(f"[1d cache update] phase_e + track_c 저장 (from_cache={_from_cache}, pe_skipped={_pe_skipped_local})")
                except Exception as _cu_e:
                    log.warning(f"[1d cache update] 실패: {_cu_e}")

            # 2) chapter_types 덮어쓰기 — 1d status=ok이면
            if _pe_final.get("status") == "ok":
                _new_chapter_types = _phase_e_to_chapter_types(
                    _pe_final, _tc_final, structure,
                )
                _legacy_chapter_types_keys = list(chapter_types.keys()) if chapter_types else []
                chapter_types = _new_chapter_types
                structure["chapter_types"] = _new_chapter_types
                if isinstance(section_results, dict) and 0 in section_results:
                    section_results[0]["chapter_types"] = _new_chapter_types
                    if isinstance(section_results[0].get("structure"), dict):
                        section_results[0]["structure"]["chapter_types"] = _new_chapter_types
                _debug_payload["chapter_types_phase_e_production"] = {
                    "overwritten": True,
                    "legacy_keys": _legacy_chapter_types_keys,
                    "new_keys": list(_new_chapter_types.keys()),
                    "new_count": len(_new_chapter_types),
                }
                log.info(
                    f"[chapter_types 1d production] "
                    f"legacy {len(_legacy_chapter_types_keys)} types → "
                    f"1d {len(_new_chapter_types)} types"
                )
            else:
                _debug_payload["chapter_types_phase_e_production"] = {
                    "overwritten": False,
                    "reason": f"phase_e status={_pe_final.get('status')!r} — legacy chapter_types 유지",
                }
        except Exception as _ctp_e:
            log.warning(f"[chapter_types 1d production] 실패: {_ctp_e}")
            _debug_payload["chapter_types_phase_e_production"] = {
                "error": str(_ctp_e),
                "overwritten": False,
            }

        # ══════════════════════════════════════════════════════════════
        # 1j + 1k — Style Profile + Inline Emphasis Layer
        # cluster 확정 직후 (1e 결과 + 1d chapter_types 반영 후) 실행.
        # 본문 생성(2b)에 cluster별 말투/강조 rule 전달용. AI input 아닌
        # downstream 보조 통계(raw_measurements)는 code 결정적 추출.
        # style namespace cache(_style.json) — main cache와 독립적으로
        # invalidate 가능 (prompt 수정 시 main cache 안 건드림).
        # ══════════════════════════════════════════════════════════════
        STYLE_CACHE_SCHEMA_VERSION = 3
        _style_profiles = {}
        _emphasis_layers_by_cluster = {}
        _paragraph_emphasis_map = {}
        _style_from_cache = False
        try:
            _section0_sr = section_results.get(0) or {}
            _section0_structure = _section0_sr.get("structure") or structure
            _section0_paragraphs = _section0_structure.get("paragraphs") or []
            _section0_idx_full_texts = _section0_sr.get("idx_full_texts") or (
                _idx_full_texts if "_idx_full_texts" in dir() else {}
            )
            _section0_light_xml = (
                _all_sections[0][1] if ("_all_sections" in dir() and _all_sections)
                else (_section_light_xml if "_section_light_xml" in dir() else "")
            )

            # cluster signature (간단 비교 — cluster set + count)
            from collections import Counter as _Counter
            _cluster_sig_cnt = _Counter(
                p.get("role", "") for p in _section0_paragraphs if p.get("role")
            )
            _cluster_signature = ",".join(f"{k}:{v}" for k, v in sorted(_cluster_sig_cnt.items()))

            # style cache load (namespace='style' — main cache와 분리)
            import os as _os
            import json as _json_sty
            _style_cache_path = f"/tmp/hwpx_cache/{_cache_key}_style.json"
            _style_cache_data = None
            if _os.path.exists(_style_cache_path):
                try:
                    with open(_style_cache_path) as _f_sc:
                        _candidate = _json_sty.load(_f_sc)
                    if (
                        _candidate.get("style_cache_schema_version") == STYLE_CACHE_SCHEMA_VERSION
                        and _candidate.get("cluster_signature") == _cluster_signature
                    ):
                        _style_cache_data = _candidate
                        log.info(f"[STYLE-CACHE] hit: {_style_cache_path}")
                except Exception as _sc_e:
                    log.warning(f"[STYLE-CACHE] load 실패: {_sc_e}")

            # cache hit 이어도 parse_failed cluster 있으면 invalidate → fresh 1k 재실행.
            # persistent_failure_after_retry 는 통과 (이미 재시도 끝난 항목, 무한 retry 방지).
            if _style_cache_data is not None:
                _cached_em = _style_cache_data.get("emphasis_layers", {}) or {}
                _invalidating_pf = [
                    _cid for _cid, _em in _cached_em.items()
                    if (_em or {}).get("_parse_status") == "parse_failed"
                ]
                if _invalidating_pf:
                    log.warning(
                        f"[STYLE-CACHE] parse_failed cluster {len(_invalidating_pf)}개 발견 "
                        f"({_invalidating_pf[:5]}...) — cache invalidate + fresh 1k 재실행"
                    )
                    _style_cache_data = None
            if _style_cache_data is not None:
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {"description": "1j/1k cache hit — AI 건너뜀", "done": False}})
                _style_profiles = _style_cache_data.get("style_profiles", {}) or {}
                _emphasis_layers_by_cluster = _style_cache_data.get("emphasis_layers", {}) or {}
                _paragraph_emphasis_map = _style_cache_data.get("paragraph_emphasis_map", {}) or {}
                _style_from_cache = True
            else:

                # 11.1 semantic_tag 사용 가능 시 전달 (debug payload 안에 들어있음)
                _semantic_tags = (_debug_payload.get("structural_intent") or {}).get("paragraphs") or []

                # ── 1j 본체 준비 (10개 cluster batch, AI 호출은 아래 gather로 동시 처리) ──
                _section0_marker_policy_1f = (
                    _section0_sr.get("marker_policy_1f")
                    or structure.get("marker_policy_1f")
                    or {}
                )
                _section0_role_markers = extract_role_markers_from_1f(_section0_marker_policy_1f)
                _style_samples = _collect_style_samples(
                    _section0_paragraphs,
                    _section0_idx_full_texts,
                    semantic_tags=_semantic_tags,
                    sample_text_char_budget=80000,
                    marker_policies=_section0_role_markers,
                )
                _STYLE_BATCH = 5
                _total_sp_clusters = len(_style_samples)
                _total_sp_batches = (_total_sp_clusters + _STYLE_BATCH - 1) // _STYLE_BATCH
                _sp_batches = [
                    _style_samples[_bi * _STYLE_BATCH : (_bi + 1) * _STYLE_BATCH]
                    for _bi in range(_total_sp_batches)
                ]

                # ── 1k — paragraph emphasis map 추출 (code only, raw 양식 zipfile) ──
                # 디버그 추적: 호출 직전 입력 + 추출 함수 내부 + 호출 직후 결과 모음
                _em_extract_dbg = {
                    "caller_before": {
                        "template_path": str(template_path),
                        "template_path_exists": _os.path.exists(template_path) if isinstance(template_path, str) else None,
                        "section0_paragraphs_count": len(_section0_paragraphs) if _section0_paragraphs else 0,
                        "section0_paragraphs_with_role": sum(1 for p in (_section0_paragraphs or []) if p.get("role")),
                        "section0_idx_full_texts_count": len(_section0_idx_full_texts) if _section0_idx_full_texts else 0,
                        "section0_paragraphs_sample": [
                            {"idx": p.get("idx"), "role": p.get("role", ""), "has_idx_text": p.get("idx") in (_section0_idx_full_texts or {})}
                            for p in (_section0_paragraphs or [])[:3]
                        ],
                    },
                }
                _paragraph_emphasis_map = extract_paragraph_emphasis_map(
                    template_path,
                    _section0_paragraphs,
                    idx_full_texts=_section0_idx_full_texts,
                    debug_trace=_em_extract_dbg,
                )
                _em_extract_dbg["caller_after"] = {
                    "paragraph_emphasis_map_cluster_count": len(_paragraph_emphasis_map),
                    "paragraph_emphasis_map_keys": list(_paragraph_emphasis_map.keys())[:10],
                }
                try:
                    _os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                    with open("/tmp/hwpx_debug/11_2b_emphasis_extract_trace.json", "w", encoding="utf-8") as _f_emd:
                        _json_sty.dump(_em_extract_dbg, _f_emd, ensure_ascii=False, indent=2, default=str)
                except Exception as _emd_e:
                    log.warning(f"[1k] extract trace 저장 실패: {_emd_e}")

                # ── 1k — emphasis layer AI batch 준비 (multi-charpr cluster만) ──
                # 글꼴 1종 cluster는 AI 호출 없이 fake entry로 채움 (base_layer_id="em1", emphasis 없음).
                # 2c가 base에도 markup 박는 기본 동작으로 변경되어 모든 cluster에 base 정보 필요.
                _em_multi_items = []
                for _cid_pre, _cem_pre in _paragraph_emphasis_map.items():
                    _stats_pre = _cem_pre.get("layer_stats", []) or []
                    if len(_stats_pre) >= 2:
                        _em_multi_items.append((_cid_pre, _cem_pre))
                    else:
                        _base_pre = _stats_pre[0] if _stats_pre else {"layer_id": "em1", "charpr_id": ""}
                        _emphasis_layers_by_cluster[_cid_pre] = {
                            "role": _cid_pre,
                            "base_layer_id": _base_pre.get("layer_id", "em1"),
                            "base_charpr_id": _base_pre.get("charpr_id", ""),
                            "base_judgement_reason": "single-charpr cluster (auto, no AI call)",
                            "emphasis_layers": [],
                            "additional_observations": "",
                            "_parse_status": "auto_single_charpr",
                            "_evidence_missing_rule_count": 0,
                            "_total_paragraphs_in_cluster": _cem_pre.get("total_paragraphs_in_cluster", 0),
                            "_multi_charpr_paragraph_count": 0,
                            "_sample_count": 0,
                        }
                _em_items = _em_multi_items
                _total_em_clusters = len(_em_items)
                _EM_BATCH = 5
                # layer 10개 이상 cluster 는 단독 batch (응답 길이 폭주 방지)
                _LARGE_LAYER_THRESHOLD = 10
                _em_large_batches = []  # 단독 batch list
                _em_small_pool = []     # 묶어서 5씩
                for _ci_pair in _em_items:
                    _layer_n = len((_ci_pair[1] or {}).get("layer_stats", []) or [])
                    if _layer_n >= _LARGE_LAYER_THRESHOLD:
                        _em_large_batches.append([_ci_pair])
                    else:
                        _em_small_pool.append(_ci_pair)
                _em_batches_to_process = list(_em_large_batches)
                for _i_sm in range(0, len(_em_small_pool), _EM_BATCH):
                    _em_batches_to_process.append(_em_small_pool[_i_sm:_i_sm + _EM_BATCH])
                _total_em_batches = len(_em_batches_to_process)

                # ── 1j batch helper (chapter loop gather와 동일 패턴) ──
                async def _run_sp_batch(_bi, _batch):
                    _msgs_sp = build_style_profile_prompt(_batch)
                    try:
                        _llm_sp = await _call_llm(
                            _msgs_sp,
                            f"hwpx_style_profile_batch_{_bi + 1}",
                        )
                        _parsed_sp_batch = parse_style_profile_from_llm(_llm_sp, _batch)
                    except Exception as _sp_e:
                        log.warning(f"[1j] batch {_bi + 1} AI 호출 실패: {_sp_e}")
                        _parsed_sp_batch = {
                            _e["role"]: {
                                "role": _e["role"],
                                "content_style_rules_for_generation": [],
                                "additional_observations": "",
                                "_parse_status": "ai_call_failed",
                                "_evidence_missing_rule_count": 0,
                            }
                            for _e in _batch
                        }
                    for _cid, _parsed in _parsed_sp_batch.items():
                        _e = next((x for x in _batch if x["role"] == _cid), None)
                        if _e:
                            _parsed["sample_count"] = len(_e.get("samples", []))
                            _parsed["sampling_method"] = _e.get("sampling_method", "all")
                            _parsed["_raw_measurements"] = _e.get("raw_measurements", {})
                    return _parsed_sp_batch

                # ── 1k batch helper (repair retry는 batch 내부 직렬 유지) ──
                async def _run_em_batch(_bi, _em_batch):
                    _msgs_em = build_emphasis_layer_prompt(_em_batch)
                    try:
                        _llm_em = await _call_llm(
                            _msgs_em,
                            f"hwpx_emphasis_layer_batch_{_bi + 1}",
                        )
                        _parsed_em_batch = parse_emphasis_layer_from_llm(_llm_em, _em_batch)
                        _all_pf = all(
                            (_parsed_em_batch.get(_c2, {}).get("_parse_status") == "parse_failed")
                            for _c2, _ in _em_batch
                        )
                        if _all_pf and _llm_em:
                            log.warning(f"[1k] batch {_bi + 1} 전체 parse_failed — repair 1회 retry")
                            _repair_msgs = [
                                {"role": "system", "content": (
                                    "JSON 형식 수정 전문가입니다. "
                                    "직전 응답의 의미·내용은 그대로 두고, JSON parse 가 깨진 형식 오류 "
                                    "(배열 객체 사이 `,` 누락, quote escape, 구조 불일치 등) 만 수정하세요. "
                                    "유효한 JSON 외 다른 출력 금지."
                                )},
                                {"role": "user", "content": (
                                    "직전 응답이 JSON parse 실패입니다. 같은 분석 내용을 유효한 JSON 으로 다시 출력하세요.\n"
                                    "분석 다시 하지 말고 형식 오류만 고치세요.\n\n"
                                    "직전 응답:\n```\n"
                                    + (_llm_em[:40000] if _llm_em else "")
                                    + "\n```"
                                )},
                            ]
                            try:
                                _llm_em_re = await _call_llm(
                                    _repair_msgs,
                                    f"hwpx_emphasis_layer_batch_{_bi + 1}_repair",
                                )
                                _parsed_retry = parse_emphasis_layer_from_llm(_llm_em_re, _em_batch)
                                _retry_all_pf = all(
                                    (_parsed_retry.get(_c2, {}).get("_parse_status") == "parse_failed")
                                    for _c2, _ in _em_batch
                                )
                                if not _retry_all_pf:
                                    _parsed_em_batch = _parsed_retry
                                    log.info(f"[1k] batch {_bi + 1} repair retry 성공")
                                else:
                                    log.warning(f"[1k] batch {_bi + 1} repair retry 도 parse_failed 유지 — persistent 마킹")
                                    for _cpf, _ in _em_batch:
                                        _ent_pf = _parsed_em_batch.get(_cpf)
                                        if _ent_pf and _ent_pf.get("_parse_status") == "parse_failed":
                                            _ent_pf["_parse_status"] = "persistent_failure_after_retry"
                            except Exception as _re_e:
                                log.warning(f"[1k] batch {_bi + 1} repair retry 호출 실패: {_re_e}")
                    except Exception as _em_e:
                        log.warning(f"[1k] batch {_bi + 1} AI 호출 실패: {_em_e}")
                        _parsed_em_batch = {}
                        for _cid, _cem in _em_batch:
                            _stats = _cem.get("layer_stats", []) or []
                            _base = _stats[0] if _stats else {"layer_id": "", "charpr_id": ""}
                            _parsed_em_batch[_cid] = {
                                "role": _cid,
                                "base_layer_id": _base.get("layer_id", ""),
                                "base_charpr_id": _base.get("charpr_id", ""),
                                "base_judgement_reason": f"AI call failed: {_em_e}",
                                "emphasis_layers": [
                                    {
                                        "layer_id": ls["layer_id"],
                                        "charpr_id": ls["charpr_id"],
                                        "segment_count": ls.get("segment_count", 0),
                                        "rules_for_generation": [],
                                    }
                                    for ls in _stats[1:]
                                ],
                                "additional_observations": "",
                                "_parse_status": "ai_call_failed",
                                "_evidence_missing_rule_count": 0,
                            }
                    for _cid, _cem in _em_batch:
                        _parsed = _parsed_em_batch.get(_cid)
                        if _parsed is None:
                            _stats = _cem.get("layer_stats", []) or []
                            _base = _stats[0] if _stats else {"layer_id": "", "charpr_id": ""}
                            _parsed = {
                                "role": _cid,
                                "base_layer_id": _base.get("layer_id", ""),
                                "base_charpr_id": _base.get("charpr_id", ""),
                                "base_judgement_reason": "missing in AI response",
                                "emphasis_layers": [
                                    {
                                        "layer_id": ls["layer_id"],
                                        "charpr_id": ls["charpr_id"],
                                        "segment_count": ls.get("segment_count", 0),
                                        "rules_for_generation": [],
                                    }
                                    for ls in _stats[1:]
                                ],
                                "additional_observations": "",
                                "_parse_status": "missing_in_ai_response",
                                "_evidence_missing_rule_count": 0,
                            }
                        _parsed["_total_paragraphs_in_cluster"] = _cem.get("total_paragraphs_in_cluster", 0)
                        _parsed["_multi_charpr_paragraph_count"] = _cem.get("multi_charpr_paragraph_count", 0)
                        _parsed["_sample_count"] = len(_cem.get("sample_paragraphs", []) or [])
                        _parsed_em_batch[_cid] = _parsed
                    return _parsed_em_batch

                # ── 1j + 1k 동시 gather (chapter loop와 동일 패턴) ──
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": (
                            f"1j/1k — {_total_sp_batches + _total_em_batches} batch 동시 시작 "
                            f"(1j={_total_sp_batches}, 1k={_total_em_batches})"
                        ),
                        "done": False,
                    }})
                import asyncio as _sty_asyncio
                _sp_tasks = [_run_sp_batch(_bi, _b) for _bi, _b in enumerate(_sp_batches)]
                _em_tasks = [_run_em_batch(_bi, _b) for _bi, _b in enumerate(_em_batches_to_process)]
                _all_results = await _sty_asyncio.gather(
                    *_sp_tasks, *_em_tasks, return_exceptions=True
                )
                _sp_results = _all_results[: len(_sp_tasks)]
                _em_results = _all_results[len(_sp_tasks):]
                for _r in _sp_results:
                    if isinstance(_r, Exception):
                        log.warning(f"[1j] gather batch Exception: {_r}")
                        continue
                    for _cid, _p in _r.items():
                        _style_profiles[_cid] = _p
                for _r in _em_results:
                    if isinstance(_r, Exception):
                        log.warning(f"[1k] gather batch Exception: {_r}")
                        continue
                    for _cid, _p in _r.items():
                        _emphasis_layers_by_cluster[_cid] = _p

                # ── style cache 저장 ──
                try:
                    _os.makedirs(_os.path.dirname(_style_cache_path), exist_ok=True)
                    with open(_style_cache_path, "w", encoding="utf-8") as _f_sc:
                        _json_sty.dump(
                            {
                                "style_cache_schema_version": STYLE_CACHE_SCHEMA_VERSION,
                                "cluster_signature": _cluster_signature,
                                "style_profiles": _style_profiles,
                                "emphasis_layers": _emphasis_layers_by_cluster,
                                "paragraph_emphasis_map": _paragraph_emphasis_map,
                            },
                            _f_sc,
                            ensure_ascii=False,
                            indent=2,
                            default=str,
                        )
                    log.info(
                        f"[STYLE-CACHE] saved: {_style_cache_path} "
                        f"(profiles={len(_style_profiles)}, "
                        f"emphasis_clusters={len(_emphasis_layers_by_cluster)})"
                    )
                except Exception as _sc_w_e:
                    log.warning(f"[STYLE-CACHE] save 실패: {_sc_w_e}")
        except Exception as _sty_outer:
            log.warning(f"[1j/1k] outer 실패 — 빈 결과로 계속: {_sty_outer}")

        _debug_payload["style_profile"] = {
            "from_cache": _style_from_cache,
            "profiles": _style_profiles,
            "cluster_count": len(_style_profiles),
        }
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {
                "description": f"1j/1k 완료 — style {len(_style_profiles)}개 + emphasis {len(_emphasis_layers_by_cluster)}개",
                "done": False,
            }})

        # paragraph_emphasis_map summary (size 큰 sample paragraph 자체는 cache에만)
        _pem_summary = {}
        for _cid, _cem in _paragraph_emphasis_map.items():
            _pem_summary[_cid] = {
                "charpr_count": len(_cem.get("charpr_to_layer") or {}),
                "total_paragraphs_in_cluster": _cem.get("total_paragraphs_in_cluster", 0),
                "multi_charpr_paragraph_count": _cem.get("multi_charpr_paragraph_count", 0),
                "sample_paragraph_count": len(_cem.get("sample_paragraphs") or []),
                "layer_stats": _cem.get("layer_stats", []),
            }
        _debug_payload["emphasis_layer"] = {
            "from_cache": _style_from_cache,
            "paragraph_emphasis_map_summary": _pem_summary,
            "emphasis_layers": _emphasis_layers_by_cluster,
            "emphasis_cluster_count": len(_emphasis_layers_by_cluster),
        }
        log.info(
            f"[1j/1k] complete: style_profiles={len(_style_profiles)}, "
            f"paragraph_emphasis_map={len(_paragraph_emphasis_map)} clusters, "
            f"emphasis_layers={len(_emphasis_layers_by_cluster)}"
            f"{' (from cache)' if _style_from_cache else ''}"
        )

        _dump_path = "/tmp/hwpx_debug_last.json"

        # role 통계
        role_counts = {}
        for p in structure.get("paragraphs", []):
            r = p.get("role", "unknown")
            role_counts[r] = role_counts.get(r, 0) + 1

        log.info(
            f"구조 분석: 문단 {len(structure.get('paragraphs', []))}개, "
            f"roles: {role_counts}, chapter_types: {list(chapter_types.keys())}"
        )

        _debug_add(
            "Step 2: 1차 AI — 구조 + role + chapter_types",
            f"문단 {len(structure.get('paragraphs', []))}개, 표 {len(structure.get('tables', []))}개\n"
            f"roles: {role_counts}\n"
            f"chapter_types: {list(chapter_types.keys())}\n\n"
            f"<details><summary>chapter_types (클릭)</summary>\n\n```json\n"
            f"{json.dumps(chapter_types, ensure_ascii=False, indent=2)}\n```\n</details>\n\n"
            f"<details><summary>전체 구조 (클릭)</summary>\n\n```json\n"
            f"{json.dumps(structure, ensure_ascii=False, indent=2)[:30000]}\n```\n</details>",
        )

        if not chapter_types:
            raise ValueError("1차 분석에서 chapter_types가 없습니다. 양식에 대제목이 없을 수 있습니다.")

        _tg = structure.get("template_grammar", {})
        if _tg:
            _global_roles = len(_tg.get("global", {}))
            _transitions = len(_tg.get("observed_transitions", []))
            _per_type = _tg.get("per_type", {})
            log.info(f"template_grammar: {_global_roles} roles, {_transitions} transitions, {len(_per_type)} types")
            for _tn, _ti in _per_type.items():
                _rr = _ti.get("root_roles", [])
                log.info(f"  {_tn}: root={_rr}, {len(_ti.get('grammar', {}))} grammar roles")

        # ── 1차 결과 dump (DEBUG raise 제거 — generation까지 진행) ──
        # tree_rebuild dump 강제 박기 (dict literal 누락 방어)
        if isinstance(_debug_payload, dict) and "_tree_rebuild_dump" in dir() and _tree_rebuild_dump is not None:
            _debug_payload["tree_rebuild"] = _tree_rebuild_dump
        try:
            with open(_dump_path, "w", encoding="utf-8") as _f:
                json.dump(_debug_payload, _f, ensure_ascii=False, indent=2, default=str)
            write_stage_debug_files(_debug_payload)
        except Exception as _e:
            log.warning(f"[DEBUG-HWPX] 1차 덤프 실패: {_e}")

        # ── ANALYSIS_ONLY_MODE: 1차까지만 진행하고 종료 ──
        if str(self.valves.ANALYSIS_ONLY_MODE).lower() in ("on", "true", "1"):
            log.info(
                f"[ANALYSIS_ONLY_MODE] 1차 분석 완료 — 본문 생성 skip. "
                f"cache_key={_cache_key}, "
                f"style_profiles={len(_style_profiles)}, "
                f"emphasis_layers={len(_emphasis_layers_by_cluster)}, "
                f"chapter_types={len(chapter_types)}"
            )
            _summary_msg = (
                f"📋 1차 분석 완료 (analysis_only_mode)\n\n"
                f"- cache_key: `{_cache_key}`\n"
                f"- cluster: {sum(1 for p in structure.get('paragraphs', []) if p.get('role'))} paragraph (role 부여)\n"
                f"- style_profiles: {len(_style_profiles)} clusters\n"
                f"- emphasis_layers: {len(_emphasis_layers_by_cluster)} clusters (글꼴 2종+ 있는 cluster)\n"
                f"- paragraph_emphasis_map: {len(_paragraph_emphasis_map)} clusters\n"
                f"- chapter_types: {list(chapter_types.keys())}\n\n"
                f"debug 파일:\n"
                f"- `/tmp/hwpx_debug_last.json` (통합)\n"
                f"- `/tmp/hwpx_debug/12b_style_profile.json` (말투 rules)\n"
                f"- `/tmp/hwpx_debug/12d_emphasis_layers.json` (강조 layer rules + base 판정)\n"
                f"- style cache: `/tmp/hwpx_cache/{_cache_key}_style.json`\n"
            )
            _debug_add("1차 분석 완료 (analysis_only_mode)", _summary_msg)
            return None, _summary_msg

        # ══════════════════════════════════════════════════════════════
        # 2a: 소스 내용을 양식 챕터에 매핑
        # ══════════════════════════════════════════════════════════════
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "AI가 소스 대제목 분류 중...", "done": False}})

        # header role 목록 추출 — 속성 기반:
        # level 0 + 첫 level 1 문단 이전에 등장 + chapter_types의 title_role 제외
        title_roles = {t.get("title_role") for t in chapter_types.values() if t.get("title_role")}
        first_ch_idx = None
        for p in structure.get("paragraphs", []):
            if p.get("level", 0) == 1:
                first_ch_idx = p.get("idx", 0)
                break
        if first_ch_idx is None:
            first_ch_idx = float("inf")

        header_roles = []
        for p in structure.get("paragraphs", []):
            role = p.get("role", "")
            if not role or role in header_roles:
                continue
            if role in title_roles:
                continue
            if p.get("level", 0) != 0:
                continue
            if p.get("idx", 0) >= first_ch_idx:
                continue
            header_roles.append(role)

        # 옛 2a 호출은 chapter route 결정 이후로 이동됨.
        # chapter route는 신 2a(=13.7c B 흡수본)가 header까지 결정. shallow route는 아래에서 옛 2a 호출.
        # 위쪽 코드의 chapters/header_data/source_sections 참조 호환을 위해 빈 값으로 초기화.
        messages_2a: list = []
        llm_content_2a: str = ""
        classify_result: dict = {"chapters": [], "header": {}}
        chapters: list = []
        header_data: dict = {}
        chapter_titles_list: list = []
        source_sections: list = []
        _source_split_log = None
        _debug_payload["chapter_classify"] = {
            "deferred_until_route_decision": True,
            "header_roles": header_roles,
        }
        _debug_payload["source_split_decision"] = None

        # 13.0 debug-only: source_blocks adapter
        _source_text_for_blocks = pdf_text_content or content_text or ""
        if _source_text_for_blocks:
            _source_blocks = text_blob_to_source_blocks(_source_text_for_blocks)
            _debug_payload["source_blocks"] = {
                "block_count": len(_source_blocks),
                "source_length": len(_source_text_for_blocks),
                "blocks": _source_blocks,
            }

        # role 카탈로그 구성
        idx_texts = {}
        if truncated_xml:
            try:
                idx_texts = _extract_texts_by_idx(truncated_xml)
            except Exception:
                pass

        from collections import Counter as _Counter_rc
        _role_counts = _Counter_rc(
            p.get("role", "") for p in structure.get("paragraphs", []) if p.get("role")
        )
        full_role_catalog = {}
        for p in structure.get("paragraphs", []):
            role = p.get("role", "")
            if role and role not in full_role_catalog:
                sample = idx_texts.get(p.get("idx", -1), "")
                full_role_catalog[role] = {
                    "description": p.get("description", ""),
                    "marker": p.get("marker", ""),
                    "level": p.get("level", 0),
                    "sample": sample,
                    "count": _role_counts.get(role, 0),  # 양식 전체 등장 횟수
                }

        def _collect_roles(pat):
            roles = set()
            for rname, info in pat.items():
                roles.add(rname)
                children = info.get("children", {})
                if children:
                    roles.update(_collect_roles(children))
            return roles

        # ══════════════════════════════════════════════════════════════
        # 13.7e: target_unit_planning을 shallow route 결정 (13.3) 전에 호출.
        # 기존엔 chapter route fallback path (line ~2275) 안에서만 호출되어
        # shallow route 결정 시점에 structure["target_unit_plan"] 빈 상태였음.
        # → shallow_route 항상 False. shallow 양식이 cache invalidate 후 재호출 시
        # chapter route로 강제 전환되는 문제 (CC7 케이스).
        # 이 위치에서 호출 → structure["target_unit_plan"] set + cache update.
        # 기존 line 2275의 호출은 is_plan_cache_valid 통과로 cache hit 처리 (호출 skip).
        # cache hit/miss 둘 다 처리 (structure에 target_unit_plan 없으면).
        # ══════════════════════════════════════════════════════════════
        _tup_existing = structure.get("target_unit_plan") or {}
        _tup_has_regions = bool(_tup_existing.get("regions"))
        if not _tup_has_regions:
            try:
                _tuo_obs_e = structure.get("template_unit_observation", {}).get("unit_observations", [])
                _proposal_e = propose_template_regions(structure, _cached if '_cached' in dir() else None, _tuo_obs_e)
                _msgs_e = build_target_unit_planning_prompt(_proposal_e, structure.get("paragraphs", []), _tuo_obs_e)
                _parsed_e = None
                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {"description": "13.7e: 양식 region planning 중 (shallow/chapter 결정용)...", "done": False}})
                try:
                    _raw_e = await _call_llm(_msgs_e, "hwpx_target_unit_planning")
                    _parsed_e = parse_target_unit_plan_from_llm(_raw_e)
                    if _parsed_e is None:
                        _raw_e = await _call_llm(_msgs_e, "hwpx_target_unit_planning_retry")
                        _parsed_e = parse_target_unit_plan_from_llm(_raw_e)
                except Exception as _ai_e:
                    log.warning(f"[13.7e] AI 호출 실패: {_ai_e}")
                if _parsed_e:
                    _val_e = validate_target_unit_plan(_parsed_e, structure.get("paragraphs", []), _tuo_obs_e)
                    _plan_e = build_plan_cache_payload(_parsed_e, _val_e)
                    structure["target_unit_plan"] = _plan_e
                    # cache update (load → update → save)
                    try:
                        _wb_e = load_template_cache(_cache_key, namespace='full')
                        if _wb_e and "structure" in _wb_e:
                            _wb_e["structure"]["target_unit_plan"] = _plan_e
                            # section_results[0].structure도 sync (cache schema v5)
                            if isinstance(_wb_e.get("section_results"), dict):
                                _sr_0 = _wb_e["section_results"].get("0") or _wb_e["section_results"].get(0)
                                if isinstance(_sr_0, dict) and "structure" in _sr_0:
                                    _sr_0["structure"]["target_unit_plan"] = _plan_e
                            save_template_cache(_cache_key, _wb_e)
                            log.info(
                                f"[13.7e] target_unit_plan early call OK: "
                                f"{len(_plan_e.get('regions', []))} regions, cache updated"
                            )
                    except Exception as _ce:
                        log.warning(f"[13.7e] cache update 실패: {_ce}")
                else:
                    log.warning("[13.7e] target_unit_planning AI fail — shallow route 결정 skip")
            except Exception as _ee:
                log.warning(f"[13.7e] target_unit_planning early call 실패: {_ee}")

        # ══════════════════════════════════════════════════════════════
        # 1d → target_unit_plan PRODUCTION 전환 (2단계)
        # 1d status=ok면 structure["target_unit_plan"]을 1d 결과로 덮어쓰기.
        # in-memory만 변경. cache 자체는 그대로 (legacy AI 결과 유지).
        # 매번 실행 시 1d 호출 + 변환 + 덮어쓰기 (cache invalidate 회피).
        # section 0 only 매핑 (multi-section은 13.7b deferred).
        # ══════════════════════════════════════════════════════════════
        try:
            from open_webui.utils.hwpx_analyzer import (
                _phase_e_to_target_unit_plan,
                build_target_unit_plan_dispatcher_decision,
            )
            _pe_for_tup = _debug_payload.get("phase_e_chapter_planner") or {}
            _tup_decision = build_target_unit_plan_dispatcher_decision(_pe_for_tup)
            _tup_legacy = structure.get("target_unit_plan") or {}
            _tup_legacy_regions_count = len(_tup_legacy.get("regions") or [])
            _tup_legacy_unit_types = sorted({
                r.get("unit_type") for r in (_tup_legacy.get("regions") or [])
                if isinstance(r, dict) and r.get("unit_type")
            })
            if _tup_decision["route"] == "phase_e":
                _tup_new = _phase_e_to_target_unit_plan(_pe_for_tup, structure)
                # PRODUCTION: structure 덮어쓰기 (in-memory만, cache 변경 X)
                structure["target_unit_plan"] = _tup_new
                # section_results[0] 동기화 (cache schema v5)
                try:
                    if isinstance(section_results, dict) and 0 in section_results:
                        if isinstance(section_results[0].get("structure"), dict):
                            section_results[0]["structure"]["target_unit_plan"] = _tup_new
                except Exception:
                    pass
                _debug_payload["target_unit_plan_phase_e_production"] = {
                    "decision": _tup_decision,
                    "overwritten": True,
                    "legacy_regions_count": _tup_legacy_regions_count,
                    "new_regions_count": len(_tup_new.get("regions") or []),
                    "legacy_unit_types": _tup_legacy_unit_types,
                    "new_unit_types": sorted({
                        r.get("unit_type") for r in (_tup_new.get("regions") or [])
                        if isinstance(r, dict) and r.get("unit_type")
                    }),
                    "multi_section_skipped_count": len(_tup_new.get("_multi_section_units_skipped") or []),
                    "legacy_target_unit_plan_for_compare": _tup_legacy,
                }
                log.info(
                    f"[1d production] target_unit_plan 덮어쓰기: "
                    f"legacy {_tup_legacy_regions_count} regions → 1d {len(_tup_new.get('regions') or [])} regions"
                )
            else:
                _debug_payload["target_unit_plan_phase_e_production"] = {
                    "decision": _tup_decision,
                    "overwritten": False,
                    "reason": "phase_e status not ok — legacy AI 결과 유지",
                    "legacy_regions_count": _tup_legacy_regions_count,
                }
        except Exception as _tup_pe_e:
            log.warning(f"[1d→target_unit_plan production] 실패 (legacy 유지): {_tup_pe_e}")
            _debug_payload["target_unit_plan_phase_e_production"] = {
                "error": str(_tup_pe_e),
                "overwritten": False,
                "reason": "exception_legacy_fallback",
            }

        # ── 13.3: Shallow route — 2b single-call shallow mode ──
        _shallow_done = False
        _tup = structure.get("target_unit_plan", {})
        _shallow_route, _route_debug = should_use_shallow_route(_tup) if _tup else (False, {"route_reason": "no target_unit_plan"})

        if _shallow_route:
            log.info(f"13.3 shallow route: {_route_debug.get('route_reason', '')}")

            # shallow route 전용 옛 2a 호출 — chapters + header_data 채움.
            # chapter route는 신 2a(=13.7c B 흡수본)가 결정하므로 여기서 호출 X.
            try:
                messages_2a = build_chapter_classify_prompt(
                    chapter_types, header_roles,
                    content_text=content_text, content_images=content_images, pdf_text=pdf_text_content,
                    template_grammar=structure.get("template_grammar"),
                    paragraphs=structure.get("paragraphs"),
                )
                llm_content_2a = await _call_llm(messages_2a, "hwpx_chapter_classify_shallow")
                classify_result = parse_chapter_classify_from_llm(llm_content_2a)
                chapters = classify_result.get("chapters", [])
                header_data = classify_result.get("header", {})
                log.info(f"shallow 옛2a 결과: {len(chapters)}개 대제목, header={list(header_data.keys())}")
                _debug_payload["chapter_classify"] = {
                    "prompt_messages": messages_2a,
                    "llm_raw_response": llm_content_2a,
                    "header_roles": header_roles,
                    "chapters": chapters,
                    "header_data": header_data,
                    "route": "shallow",
                }
                # source split (옛 흐름)
                chapter_titles_list = [ch.get("title", "") for ch in chapters]
                if pdf_text_content:
                    source_sections, _source_split_log = split_source_by_chapters(pdf_text_content, chapter_titles_list)
                else:
                    source_sections = [""] * len(chapters)
                    _source_split_log = None
                _debug_payload["source_split_decision"] = _source_split_log
            except Exception as _shallow_2a_e:
                log.warning(f"shallow route 옛 2a 호출 실패: {_shallow_2a_e}")

            _shallow_region = None
            _tup_regions = _tup.get("regions", []) or _tup.get("ai_plan", {}).get("regions", [])
            for _r in _tup_regions:
                if _r.get("unit_type") == "shallow_block":
                    _shallow_region = _r
                    break

            if _shallow_region and chapters:
                # Use first chapter_type's pattern for 2b call
                _shallow_ch_type = list(chapter_types.keys())[0] if chapter_types else ""
                _shallow_type_info = chapter_types.get(_shallow_ch_type, {})
                _shallow_pattern = _shallow_type_info.get("pattern", {})
                _shallow_title_role = _shallow_type_info.get("title_role", "")
                _shallow_pi = _shallow_region.get("paragraph_indices", [])
                _shallow_desc = _shallow_region.get("description", "shallow block")

                # Collect pattern roles for this type
                _shallow_pattern_roles = set()
                def _walk_pattern(p):
                    for rn, ri in p.items():
                        _shallow_pattern_roles.add(rn)
                        if ri.get("children"):
                            _walk_pattern(ri["children"])
                _walk_pattern(_shallow_pattern)

                # 13.3b-1: section plan seed extraction
                _section_plan_seed_result = extract_shallow_section_plan_seed(
                    _tup, structure, _idx_full_texts,
                    marker_policies=structure.get("marker_policy_1f"),
                )
                _has_seed = bool((_section_plan_seed_result or {}).get("seed"))
                if _has_seed:
                    log.info(f"13.3b-1 section plan seed: {(_section_plan_seed_result['seed'] or {}).get('heading_count', 0)} headings")
                else:
                    log.info(f"13.3b-1 section plan seed fallback: {(_section_plan_seed_result or {}).get('fallback_reason', 'unknown')}")

                # Build 2b prompt in shallow mode
                _shallow_source = pdf_text_content or content_text or ""
                _shallow_2b_msgs = build_section_fill_prompt(
                    chapter_title=_shallow_desc,
                    chapter_type_name=_shallow_ch_type,
                    pattern=_shallow_pattern,
                    role_catalog=full_role_catalog,
                    pdf_text=_shallow_source,
                    content_text=content_text if _shallow_source != content_text else "",
                    format_rules=structure.get("format_rules", {}),
                    role_text_types=structure.get("role_text_types"),
                    per_type_role_semantics=structure.get("per_type_role_semantics"),
                    cooccurrence_rules=structure.get("sibling_cooccurrence_rules", []),

                    style_profiles=_style_profiles,

                    emphasis_layers=_emphasis_layers_by_cluster,
                            paragraph_emphasis_map=_paragraph_emphasis_map,
                    content_only_mode=True,
                    shallow_mode=True,
                    section_plan_seed=_section_plan_seed_result,
                    marker_policy_1f=structure.get("marker_policy_1f"),
                )

                await __event_emitter__({"type": "status", "data": {"description": "shallow 2b 생성 중...", "done": False}})
                _shallow_2b_raw = await _call_llm(_shallow_2b_msgs, "hwpx_shallow_2b")

                # Reuse existing process_section_fill_result with shallow_mode=True
                _flow_trace("call_shallow", ch_idx=0)
                _shallow_result = await process_section_fill_result(
                    llm_response=_shallow_2b_raw,
                    ch_idx=0,
                    ch_title=_shallow_desc,
                    ch_type=_shallow_ch_type,
                    title_role=_shallow_title_role,
                    template_grammar=structure.get("template_grammar", {}),
                    role_text_types=structure.get("role_text_types"),
                    pattern_roles=list(_shallow_pattern_roles),
                    section_pdf_text_len=len(_shallow_source),
                    shallow_mode=True,
                    call_llm_fn=_call_llm,
                    role_catalog=full_role_catalog,
                    paragraphs_info=_section0_paragraphs,
                    marker_policy_1f=structure.get("marker_policy_1f"),
                    style_profiles=_style_profiles,
                    emphasis_layers=_emphasis_layers_by_cluster,
                    paragraph_emphasis_map=_paragraph_emphasis_map,
                    chapter_position=0,
                    total_chapters=1,
                )
                # 2c가 chapter title에 마커 입혔으면 그 결과 사용
                if _shallow_result.get("chapter_title") and _shallow_result["chapter_title"] != _shallow_desc:
                    _shallow_desc = _shallow_result["chapter_title"]

                body_items = _shallow_result["body_items"]
                _section_fill_debug = [_shallow_result["debug_entry"]]
                _chapter_trees = []

                # 13.3b-1: compliance observation
                _compliance = observe_section_plan_compliance(body_items, _section_plan_seed_result)

                _preserve_set, _preserve_debug = compute_preserve_indices(_tup, idx_map=idx_map)
                content_data = {"header": header_data, "body": body_items}
                result = assemble_hwpx_hybrid(
                    template_path, structure, content_data,
                    removed_indices=removed_indices, idx_map=idx_map,
                    content_only_mode=True, preserve_indices=_preserve_set,
                    emphasis_layers=_emphasis_layers_by_cluster,
                    paragraph_emphasis_map=_paragraph_emphasis_map,
                )
                log.info(f"shallow assemble: success={result.success_count}, fail={result.fail_count}")

                _debug_payload["shallow_section_plan_seed"] = _section_plan_seed_result
                _debug_payload["shallow_section_plan_compliance"] = _compliance
                _debug_payload["shallow_generation"] = {
                    "route": "shallow_2b_single_call",
                    "region_id": _shallow_region.get("region_id"),
                    "region_description": _shallow_desc,
                    "region_paragraph_count": len(_shallow_pi),
                    "shallow_ch_type": _shallow_ch_type,
                    "item_count": len(body_items),
                    "grammar_passed": _shallow_result.get("grammar_passed"),
                    "roles_used": list(set(it.get("role", "") for it in body_items)),
                    **_route_debug,
                    "2a_called_for_header_data_only": True,
                    "chapter_plan_ignored_for_shallow_route": True,
                    "title_injection_skipped_for_shallow": True,
                    "fallback_used": False,
                    "table_generation_policy": "deferred",
                    "table_cell_filling_enabled": False,
                    "table_handling_scope": "preserve_structure_only",
                    **_preserve_debug,
                }
                _debug_payload["section_fill"] = _section_fill_debug
                _debug_payload["final_content"] = {"header": header_data, "body_items_count": len(body_items), "body_items": body_items}
                _debug_payload["assembly"] = {
                    "success_count": result.success_count, "fail_count": result.fail_count,
                    "errors": result.errors if result.errors else [], "output_size": len(result.data),
                    "marker_rewrite_log": structure.get("_marker_rewrite_log", []),
                    "rewrite_alignment": structure.get("_rewrite_alignment", {}),
                    "phase2_reattach_result": structure.get("_phase2_reattach_result"),
                    "section_info": structure.get("_section_info"),
                }
                _shallow_done = True
            else:
                log.warning("shallow route but no shallow_block region or no chapter_types, falling back")

        body_items = [] if not _shallow_done else body_items
        _section_fill_debug = [] if not _shallow_done else _section_fill_debug
        _chapter_trees = [] if not _shallow_done else _chapter_trees

        # 13.7a-A1: chapter route chapter object 수집 + A0 empty_reason 누적
        _chapter_objects = [] if not _shallow_done else None
        _chapter_empty_reasons = [] if not _shallow_done else None

        # region lookup (chapter_plan_seed.region_id → _tup region dict)
        _tup_regions = (_tup or {}).get("regions") or (_tup or {}).get("ai_plan", {}).get("regions") or []
        _tup_region_by_id = {r.get("region_id"): r for r in _tup_regions}
        # 2a-driven fallback용: chapter unit_type regions를 ch_idx 순서로 매핑
        _tup_chapter_regions = [r for r in _tup_regions if r.get("unit_type") == "chapter"]

        # ── 13.4b: Chapter Template Plan Seed — template-driven loop ──
        _chapter_plan_seed = None
        _chapter_loop_driver = "2a_chapters"
        _chapter_plan_debug = {}
        if not _shallow_done and _tup:
            _chapter_plan_seed = extract_chapter_template_plan_seed(_tup, structure, _idx_full_texts)
            if _chapter_plan_seed and _chapter_plan_seed.get("confidence") != "low":
                _chapter_loop_driver = "template_plan"
                log.info(
                    f"13.4b template-driven loop: {_chapter_plan_seed['total_chapters']} chapters, "
                    f"confidence={_chapter_plan_seed['confidence']}, type={_chapter_plan_seed.get('dominant_chapter_type')}"
                )
            else:
                _chapter_plan_seed = None
                log.info("13.4b fallback: seed absent or low confidence, using 2a-driven loop")

        _broad_source = pdf_text_content or content_text or ""

        # ──────────────────────────────────────────────────────────────
        # 13.7c: Source-to-Template Adaptation Planning
        # ──────────────────────────────────────────────────────────────
        # 원칙 (docs/13_7c_plan.md):
        #   - 의미 판단 AI, code는 형식/계약만
        #   - heuristic은 정책 영향 X (debug-only)
        #   - chapter_template_plan_seed가 있을 때만 동작 (2a-driven은 skip)
        _adaptation_plan_summary = None
        _ch_decisions_by_idx: dict[int, dict] = {}
        _adaptation_ai_calls: dict = {}
        # TOC replacements — 신 2a 결과로 채워짐. 13.7c skip되면 빈 채로.
        _toc_replacements: list = []
        _tpl_toc_idx = None
        # 2b-source: chapter별 source chunk — 비어있으면 _broad_source 전체 fallback
        _chapter_source_chunks: dict = {}

        if not _shallow_done and _chapter_plan_seed:
            try:
                # 13.7c AI input 정제: chapter title에서 양식 marker 제거.
                # AI는 의미(title text)만 판단, 양식 marker는 assembly 단계 code가 자동 부착.
                from open_webui.utils.hwpx_analyzer import strip_chapter_title_marker as _strip_ct_mk
                _mp1f_for_ct = structure.get("marker_policy_1f") or {}

                _ch_inputs_for_plan = []
                for _ch_i, _tpl_ch in enumerate(_chapter_plan_seed["chapters"]):
                    _lp = _tpl_ch.get("local_pattern") or {}
                    _lc = _tpl_ch.get("local_catalog") or {}
                    # local_pattern / local_catalog summary (key 목록만 — token 절약)
                    _lc_summary = "; ".join(
                        f"{k}: {v.get('exemplar') or ''}"
                        for k, v in list(_lc.items())[:5]
                    ) if _lc else ""
                    # chapter title marker strip — AI는 의미만 보고 결정
                    _tpl_title_raw = _tpl_ch.get("template_title", "")
                    _tpl_title_role = _tpl_ch.get("local_title_role", "")
                    _orig_title_clean = _strip_ct_mk(_tpl_title_raw, _tpl_title_role, _mp1f_for_ct)
                    _ch_inputs_for_plan.append({
                        "idx": _ch_i,
                        "original_title": _orig_title_clean,
                        "description": _tpl_ch.get("description", ""),
                        "local_catalog_summary": _lc_summary,
                    })

                # A: source inventory AI 호출 (template-first: inventory는 도구이지 frame 아님)
                # 13.7c는 source 전체를 받음 (cap 제거). 13.7c가 앞부분만 보고 hint를 만들면
                # 2b가 전체를 받아도 뒷부분이 활용 안 되므로 13.7c 단계에서 전체 cover.
                # source_inventory 제거 (2026-05-25) — 신 2a 가 source 전체 직접 보고 결정
                _source_inventory = {}  # 호환성 위해 빈 dict 채움 (build_adaptation_plan_prompt 인자)
                _adaptation_ai_calls["source_inventory"] = {"removed": True}
                # B (=신 2a): chapter mapping + header batch
                # template-first: chapter need first, source inventory는 도구.
                # 옛 2a에서 header 추출을 흡수하기 위해 header_roles 전달.
                # source 전체를 preview에 넣음 (cap 제거).
                _expected_idx = [ch["idx"] for ch in _ch_inputs_for_plan]
                # 양식 TOC paragraph text — 신 2a가 chapter title 교체 + 그 외 글자 "?" 처리
                _tpl_toc_text = ""
                _tpl_toc_idx = None
                for p in structure.get("paragraphs", []):
                    if p.get("semantic_role") == "table_of_contents":
                        _tpl_toc_idx = p.get("idx")
                        _tpl_toc_text = (
                            (_section0_idx_full_texts or {}).get(_tpl_toc_idx)
                            or (_section0_idx_full_texts or {}).get(str(_tpl_toc_idx))
                            or ""
                        )
                        break
                # 옵션 A (2026-05-24): toc paragraph의 t element list 추출
                # AI가 t_idx 단위로 어디에 무엇을 박을지 결정.
                _tpl_toc_t_list: list = []
                if _tpl_toc_idx is not None:
                    try:
                        _tpl_toc_t_list = extract_toc_t_list(
                            template_path, _tpl_toc_idx, _section0_idx_full_texts or {},
                        )
                    except Exception as _toc_t_e:
                        log.warning(f"toc t list 추출 실패: {_toc_t_e}")
                # header_roles에 description + 양식 원 sample 텍스트 추가
                # (제목 성격 role은 LLM이 양식 원 형식 보고 adapted_title 방식으로 보정)
                _hdr_roles_for_ap = []
                for r in header_roles:
                    _first_p = next((p for p in structure.get("paragraphs", []) if p.get("role") == r), None)
                    _desc = _first_p.get("description", "") if _first_p else ""
                    _first_idx = _first_p.get("idx") if _first_p else None
                    _tpl_sample = ""
                    _tpl_parts: list = []
                    if _first_idx is not None:
                        _tpl_sample = (
                            (_section0_idx_full_texts or {}).get(_first_idx)
                            or (_section0_idx_full_texts or {}).get(str(_first_idx))
                            or ""
                        )
                        # 양식 raw XML에서 run/t 분포 추출 (t별 charPr+text)
                        # idx_map 통해 real_idx로 변환
                        _real_first_idx = (idx_map or {}).get(_first_idx, _first_idx) if isinstance(_first_idx, int) else None
                        if isinstance(_real_first_idx, int):
                            try:
                                _tpl_parts = extract_paragraph_run_parts(template_path, _real_first_idx)
                            except Exception as _epe:
                                log.warning(f"header parts 추출 실패 (role={r}, idx={_real_first_idx}): {_epe}")
                    _entry = {
                        "role": r,
                        "description": _desc,
                        "template_sample": _tpl_sample,
                    }
                    # 양식 paragraph에 charPr 분포가 2종+이면 parts 추가 (LLM이 폰트 보존하며 결정)
                    if _tpl_parts and len({p.get("charPrIDRef") for p in _tpl_parts}) >= 2:
                        _entry["template_parts"] = _tpl_parts
                    _hdr_roles_for_ap.append(_entry)
                _ap_msgs = build_adaptation_plan_prompt(
                    _source_inventory, _ch_inputs_for_plan,
                    broad_source_preview=_broad_source,
                    max_source_preview_chars=0,
                    header_roles=[],  # KILL_SWITCH 2026-05-28: 원본 cover/TOC 보존. 복구: [] → _hdr_roles_for_ap
                    template_toc_text=_tpl_toc_text,
                    template_toc_t_list=_tpl_toc_t_list,
                )
                # single batch — split 폐기 (2026-05-25)
                _ap_raw = await _call_llm(_ap_msgs, "hwpx_13_7c_adaptation_plan")
                _ap_parsed = parse_adaptation_plan_from_llm(_ap_raw, _expected_idx)
                _adaptation_ai_calls["chapter_mapping_batch"] = {
                    "raw_response_len": _ap_parsed.get("_validation", {}).get("raw_response_len", 0),
                    "retry_count": 0,
                    "validation_ok": _ap_parsed.get("_validation", {}).get("ok", False),
                }
                if not _ap_parsed.get("_validation", {}).get("ok"):
                    # retry 1회
                    _ap_raw = await _call_llm(_ap_msgs, "hwpx_13_7c_adaptation_plan_retry")
                    _ap_parsed = parse_adaptation_plan_from_llm(_ap_raw, _expected_idx)
                    _adaptation_ai_calls["chapter_mapping_batch"]["retry_count"] = 1
                    _adaptation_ai_calls["chapter_mapping_batch"]["validation_ok"] = (
                        _ap_parsed.get("_validation", {}).get("ok", False)
                    )

                # decision 매핑 + validation + 강등
                _normalized_decisions = []
                for _d in _ap_parsed.get("chapter_decisions") or []:
                    _idx = _d.get("chapter_idx")
                    if _idx is None or _idx >= len(_ch_inputs_for_plan):
                        continue
                    _orig_title = _ch_inputs_for_plan[_idx].get("original_title", "")
                    _norm_d = normalize_adaptation_decision(_d, _orig_title)
                    _val = validate_adaptation_decision(_norm_d)
                    if _val.get("should_demote"):
                        _norm_d = make_validation_failed_decision(
                            _idx, _orig_title, _val.get("violations", []),
                        )
                    _ch_decisions_by_idx[_idx] = _norm_d
                    _normalized_decisions.append(_norm_d)

                # missing chapters fallback (plan_unavailable)
                for _idx in _expected_idx:
                    if _idx not in _ch_decisions_by_idx:
                        _orig_title = _ch_inputs_for_plan[_idx].get("original_title", "")
                        _norm_d = make_unavailable_decision(
                            _idx, _orig_title,
                            "ai_returned_no_decision_for_chapter",
                        )
                        _ch_decisions_by_idx[_idx] = _norm_d
                        _normalized_decisions.append(_norm_d)

                # 13.7e v2: overall_source_focus를 _ap_parsed에서 추출
                _osf = _ap_parsed.get("overall_source_focus") if isinstance(_ap_parsed, dict) else None
                # 신 2a: 13.7c B가 결정한 header를 header_data로 사용 (옛 2a 흡수)
                _ap_header = _ap_parsed.get("header") if isinstance(_ap_parsed, dict) else None
                if isinstance(_ap_header, dict) and _ap_header:
                    header_data = _ap_header
                    log.info(f"신 2a header_data (13.7c B 흡수): {list(header_data.keys())}")
                    _debug_payload["chapter_classify"] = {
                        "route": "chapter",
                        "deferred_until_route_decision": False,
                        "header_roles": header_roles,
                        "header_data": header_data,
                        "source": "13_7c_b_absorbed",
                    }
                # TOC 매핑 — 신 2a 와 분리된 별도 단계 (2026-05-25)
                # 신 2a 가 chapter title + header 만 결정. TOC 매핑은 여기서 별도 AI 호출.
                _toc_replacements = []
                if False:  # KILL_SWITCH 2026-05-28: 원본 TOC 보존. 복구: False → _tpl_toc_t_list and _ch_decisions_by_idx
                    try:
                        from open_webui.utils.hwpx_analyzer import (
                            build_toc_replacement_prompt,
                            parse_toc_replacement_from_llm,
                        )
                        _toc_title_pairs = []
                        for _idx in sorted(_ch_decisions_by_idx.keys()):
                            _dec = _ch_decisions_by_idx[_idx]
                            _toc_title_pairs.append({
                                "chapter_idx": _idx,
                                "original_title": _dec.get("original_title", ""),
                                "adapted_title": _dec.get("adapted_title", ""),
                            })
                        _toc_msgs = build_toc_replacement_prompt(_toc_title_pairs, _tpl_toc_t_list)
                        _toc_raw = await _call_llm(_toc_msgs, "hwpx_toc_replacement")
                        _toc_parsed = parse_toc_replacement_from_llm(_toc_raw)
                        _toc_replacements = _toc_parsed.get("toc_replacements") or []
                        log.info(f"TOC 매핑 단계: {len(_toc_replacements)}개 entry")
                        _adaptation_ai_calls["toc_replacement"] = {
                            "raw_response_len": _toc_parsed.get("_validation", {}).get("raw_response_len", 0),
                            "validation_ok": _toc_parsed.get("_validation", {}).get("ok", False),
                        }
                    except Exception as _toc_e:
                        log.warning(f"TOC 매핑 단계 실패 — empty 로 진행: {_toc_e}")
                        _toc_replacements = []
                # debug — toc 결정 상태 + 실제 양식 toc text + replacements 저장
                _debug_payload["toc_decision"] = {
                    "toc_paragraph_idx": _tpl_toc_idx,
                    "toc_paragraph_text": _tpl_toc_text,
                    "toc_paragraph_text_len": len(_tpl_toc_text),
                    "replacements_count": len(_toc_replacements),
                    "replacements": _toc_replacements,
                }
                _adaptation_plan_summary = summarize_adaptation_plan(
                    _normalized_decisions,
                    {
                        "summary": _source_inventory.get("summary", ""),
                        "available_topics": _source_inventory.get("available_topics", []),
                        "main_headings": _source_inventory.get("main_headings", []),
                        "confidence": _source_inventory.get("confidence", "low"),
                        "evidence_samples": _source_inventory.get("evidence_samples", []),
                    },
                    overall_source_focus=_osf,
                    ai_call_info=_adaptation_ai_calls,
                )
                log.info(
                    f"신 2a adaptation_plan: chapters={_adaptation_plan_summary['chapter_count']}, "
                    f"validation_failures={_adaptation_plan_summary['validation_failure_count']}"
                )

                # ──────────────────────────────────────────────────────────
                # 2b-source: chapter별 source range 분배 (한 호출, 모든 chapter)
                # ──────────────────────────────────────────────────────────
                _chapter_source_chunks: dict = {}
                if _broad_source and _ch_decisions_by_idx:
                    try:
                        _ch_inputs_for_src = []
                        for _idx in sorted(_ch_decisions_by_idx.keys()):
                            _dec = _ch_decisions_by_idx[_idx]
                            _ch_inputs_for_src.append({
                                "idx": _idx,
                                "adapted_title": _dec.get("adapted_title", ""),
                                "original_title": _dec.get("original_title", ""),
                            })
                        _src_range_msgs = build_source_range_prompt(
                            _broad_source, _ch_inputs_for_src,
                            overall_source_focus=_osf,
                        )
                        _src_range_raw = await _call_llm(_src_range_msgs, "hwpx_2b_source_range")
                        _src_range_parsed = parse_source_ranges_from_llm(
                            _src_range_raw,
                            [c["idx"] for c in _ch_inputs_for_src],
                            len(_broad_source),
                        )
                        _chapter_source_chunks = apply_source_ranges_with_safety(
                            _broad_source,
                            _src_range_parsed.get("chapter_ranges") or {},
                            expand_chars=0,
                            expected_chapter_indices=[c["idx"] for c in _ch_inputs_for_src],
                        )
                        log.info(
                            f"2b-source range 분배: validation_ok={_src_range_parsed['_validation']['ok']}, "
                            f"chapters_assigned={len(_chapter_source_chunks)}, "
                            f"missing={_src_range_parsed['_validation']['missing_indices']}"
                        )
                        _debug_payload["source_range_decision"] = {
                            "validation": _src_range_parsed["_validation"],
                            "chunk_lengths": {str(k): len(v) for k, v in _chapter_source_chunks.items()},
                            "raw_ranges": {
                                str(k): [list(t) for t in v]
                                for k, v in (_src_range_parsed.get("chapter_ranges") or {}).items()
                            },
                            "source_total_length": len(_broad_source),
                            "expand_chars": 5000,
                        }
                    except Exception as _sre:
                        log.warning(f"2b-source 호출 실패 — _broad_source 전체로 fallback: {_sre}")
                        _chapter_source_chunks = {}
                        _debug_payload["source_range_decision"] = {"error": str(_sre)}

            except Exception as _ap_e:
                log.warning(f"13.7c adaptation_plan failed: {_ap_e}, falling back to preserve-all")
                # 전체 chapter preserve fallback
                _ch_decisions_by_idx = {}
                _normalized_decisions = []
                for _ch_i, _tpl_ch in enumerate(_chapter_plan_seed["chapters"]):
                    _orig_title = _tpl_ch.get("template_title", "")
                    _norm_d = make_unavailable_decision(
                        _ch_i, _orig_title, f"adaptation_plan_exception: {_ap_e}",
                    )
                    _ch_decisions_by_idx[_ch_i] = _norm_d
                    _normalized_decisions.append(_norm_d)
                _adaptation_plan_summary = summarize_adaptation_plan(
                    _normalized_decisions,
                    {"summary": "", "confidence": "low", "_error": str(_ap_e)},
                    ai_call_info={"error": str(_ap_e)},
                )

        if not _shallow_done and _chapter_plan_seed:
            # ── Template-driven chapter loop ──
            _seed_chapters = _chapter_plan_seed["chapters"]
            _seed_ch_type = _chapter_plan_seed.get("dominant_chapter_type", "")
            _seed_type_info = chapter_types.get(_seed_ch_type, {})
            _seed_pattern = _seed_type_info.get("pattern", {})
            _seed_title_role = _seed_type_info.get("title_role", "chapter_title")
            _seed_pattern_roles = _collect_roles(_seed_pattern)
            _seed_catalog = {r: full_role_catalog[r] for r in _seed_pattern_roles if r in full_role_catalog}
            _per_ch_status = []

            async def _process_tpl_chapter(ch_idx, tpl_ch):
                ch_title = tpl_ch.get("template_title", f"Chapter {ch_idx+1}")
                ch_desc = tpl_ch.get("description", "")
                # 13.7c-2phase: 모든 action에서 adapted_title 사용 (preserve도). action 무관.
                # preserve는 body 측면만 — chapter title은 항상 결정됨.
                _ch_dec_pre = _ch_decisions_by_idx.get(ch_idx) or {}
                _ad_t = (_ch_dec_pre.get("adapted_title") or "").strip()
                if _ad_t:
                    ch_title = _ad_t

                # 13.6-B: per-chapter pattern/catalog (fallback to dominant)
                _ch_local_pattern = tpl_ch.get("local_pattern")
                if _ch_local_pattern:
                    _ch_pattern = _ch_local_pattern
                    _ch_pattern_roles = _collect_roles(_ch_pattern)
                    _ch_catalog = {r: full_role_catalog[r] for r in _ch_pattern_roles if r in full_role_catalog}
                    _ch_pattern_source = "per_chapter_subtree"
                else:
                    _ch_pattern = _seed_pattern
                    _ch_pattern_roles = _seed_pattern_roles
                    _ch_catalog = _seed_catalog
                    _ch_pattern_source = "dominant_type_fallback"
                # title_role은 분기 무관 — tpl_ch["local_title_role"](seed가 항상 첫 paragraph role로 채움)
                # 우선, 비면 _seed_title_role fallback.
                # sub-tree 없어 dominant fallback인 chapter도 chapter title cluster ID는 명확.
                _ch_title_role = tpl_ch.get("local_title_role") or _seed_title_role

                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": f"AI가 템플릿 장 {ch_idx+1}/{len(_seed_chapters)} 생성 중: {ch_title[:30]}...",
                        "done": False,
                    }})

                try:
                    # 13.7e: title_action / content_action 분리 schema 처리
                    _decision = _ch_decisions_by_idx.get(ch_idx)
                    _title_action = (_decision.get("title_action") if _decision else None) or "adapt_topic_terms"
                    _content_action = (_decision.get("content_action") if _decision else None) or "generate_from_source"
                    # 옛 호환: _action도 유지 (debug log에서 사용)
                    _action = _title_action
                    _2b_title = ch_title  # ch_title은 이미 adapted_title로 통일됨 (line 2278 직후)

                    # 13.7e: 모든 chapter는 2b 호출 (body 무조건 생성, source 부족도 scaffold로 처리)
                    if False:  # source_gap 분기 자체 제거 — body 안 만드는 옵션 없음
                        pass
                    else:
                        # 양식 실제 instance 트리 추출 (cluster + parent, 텍스트 X)
                        _ch_id_for_tree = tpl_ch.get("chapter_id", ch_idx)
                        _tpl_tree_str = extract_chapter_template_tree(
                            structure.get("paragraphs", []),
                            _ch_id_for_tree,
                        )
                        # 2b-source가 결정한 chapter chunk 사용. 없으면 _broad_source 전체 fallback.
                        _ch_chunk = _chapter_source_chunks.get(ch_idx) or _broad_source
                        messages_2b = build_section_fill_prompt(
                            _2b_title, _seed_ch_type, _ch_pattern, _ch_catalog,
                            content_text=content_text, content_images=content_images, pdf_text=_ch_chunk,
                            exclusive_rules=[],  # opted-out blacklist
                            cooccurrence_rules=structure.get("sibling_cooccurrence_rules", []),

                            style_profiles=_style_profiles,

                            emphasis_layers=_emphasis_layers_by_cluster,
                            paragraph_emphasis_map=_paragraph_emphasis_map,
                            format_rules=structure.get("format_rules", {}),
                            role_text_types=structure.get("role_text_types"),
                            per_type_role_semantics=structure.get("per_type_role_semantics"),
                            content_only_mode=True,
                            template_chapter_context=tpl_ch,
                            marker_policy_1f=structure.get("marker_policy_1f"),
                            template_chapter_tree=_tpl_tree_str,
                        )
                        # 신 2a(=13.7c B 흡수본) hint를 2b user message에 prepend.
                        # 2026-05-23: supporting_evidence / preserved_aspects / adapted_aspects 제거.
                        # 이유: 좁은 evidence가 2b LLM을 그 부분에만 매달리게 만들어 source 뒷부분 무시.
                        # adapted_title + original_title + overall_source_focus만 hint로 박음.
                        if _decision:
                            _hint_parts = ["[신 2a adaptation hint]"]
                            _hint_parts.append(f"adapted_title: {_2b_title}")
                            _hint_parts.append(f"original_title: {(_decision.get('original_title') or '')}")
                            _osf_topic = (_osf or {}).get("topic") if isinstance(_osf, dict) else None
                            if _osf_topic:
                                _hint_parts.append(f"overall_source_focus: {_osf_topic}")
                            _hint_parts.append(
                                "adapted_title의 의미를 살리되, template chapter의 역할/흐름은 보존하세요. "
                                "source 본문 전체에서 chapter role에 맞는 내용을 자유롭게 선택하세요 "
                                "(특정 부분에만 매달리지 마세요)."
                            )
                            _hint_text = "\n".join(str(x) for x in _hint_parts)
                            for _mi, _msg in enumerate(messages_2b):
                                if _msg.get("role") == "user":
                                    messages_2b[_mi] = {
                                        "role": "user",
                                        "content": _hint_text + "\n\n" + _msg.get("content", ""),
                                    }
                                    break

                        llm_content_2b = await _call_llm(messages_2b, f"hwpx_section_fill_{ch_idx}")
                        # 2b-b polish — 1차 본문 트리 받아 양식 sample 말투/술어/분할로 정제 + 보충 + variant 추가
                        # 진입 dump (호출 자체 됐는지 확인)
                        try:
                            import os as _2bb_entry_os, json as _2bb_entry_json
                            _2bb_entry_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                            with open("/tmp/hwpx_debug/2b_polish_apply_log.jsonl", "a", encoding="utf-8") as _f_entry:
                                _f_entry.write(_2bb_entry_json.dumps({
                                    "ch_idx": ch_idx, "stage": "entry",
                                    "ch_title": (_2b_title or "")[:60],
                                    "llm_content_2b_len": len(llm_content_2b or ""),
                                }, ensure_ascii=False) + "\n")
                        except Exception as _entry_e:
                            log.warning(f"[2b-b entry dump fail] {_entry_e}")
                        try:
                            _raw_items_1st = parse_section_fill_from_llm(llm_content_2b)
                            if _raw_items_1st:
                                # 2b-b prompt 빌드: 2b 와 동일한 인자 전달 (build_section_fill_prompt 인자 + items_1st)
                                _polish_msgs = build_section_polish_prompt(
                                    _raw_items_1st,
                                    chapter_title=_2b_title,
                                    chapter_type_name=_seed_ch_type,
                                    pattern=_ch_pattern,
                                    role_catalog=_ch_catalog,
                                    content_text=content_text,
                                    content_images=content_images,
                                    pdf_text=_ch_chunk,
                                    exclusive_rules=[],
                                    cooccurrence_rules=structure.get("sibling_cooccurrence_rules", []),
                                    style_profiles=_style_profiles,
                                    emphasis_layers=_emphasis_layers_by_cluster,
                                    paragraph_emphasis_map=_paragraph_emphasis_map,
                                    format_rules=structure.get("format_rules", {}),
                                    role_text_types=structure.get("role_text_types"),
                                    per_type_role_semantics=structure.get("per_type_role_semantics"),
                                    content_only_mode=True,
                                    template_chapter_context=tpl_ch,
                                    marker_policy_1f=structure.get("marker_policy_1f"),
                                    template_chapter_tree=_tpl_tree_str,
                                    paragraphs=structure.get("paragraphs"),
                                    idx_full_texts=_idx_full_texts,
                                )
                                _polish_raw = await _call_llm(_polish_msgs, f"hwpx_section_polish_{ch_idx}")
                                _polish_items = parse_section_polish_from_llm(_polish_raw)
                                try:
                                    import os as _2bb_os, json as _2bb_json
                                    _2bb_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                                    with open("/tmp/hwpx_debug/2b_polish_apply_log.jsonl", "a", encoding="utf-8") as _f:
                                        _f.write(_2bb_json.dumps({
                                            "ch_idx": ch_idx, "ch_title": (_2b_title or "")[:60],
                                            "raw_items_count": len(_raw_items_1st),
                                            "polish_items_count": len(_polish_items),
                                            "polish_raw_len": len(_polish_raw or ""),
                                            "polish_raw_full": (_polish_raw or ""),
                                        }, ensure_ascii=False) + "\n")
                                except Exception:
                                    pass
                                if _polish_items:
                                    import json as _json_pi
                                    llm_content_2b = _json_pi.dumps({"items": _polish_items}, ensure_ascii=False)
                        except Exception as _polish_e:
                            log.warning(f"[2b-b polish ch_idx={ch_idx}] 실패: {_polish_e}")
                        # 13.6-B: build override grammar from local_pattern
                        _override_grammar = None
                        _override_root_roles = None
                        if _ch_local_pattern and _ch_pattern_source == "per_chapter_subtree":
                            _override_grammar, _override_root_roles = pattern_to_grammar(_ch_local_pattern)

                        _flow_trace("call_route1", ch_idx=ch_idx)
                        _flow_trace("call_route1", ch_idx=ch_idx)
                        _sf_result = await process_section_fill_result(
                            llm_content_2b,
                            ch_idx=ch_idx,
                            ch_title=_2b_title,
                            ch_type=_seed_ch_type,
                            title_role=_ch_title_role,
                            template_grammar=structure.get("template_grammar", {}),
                            role_text_types=structure.get("role_text_types"),
                            pattern_roles=list(_ch_pattern_roles),
                            section_pdf_text_len=len(_ch_chunk),
                            override_grammar=_override_grammar,
                            override_root_roles=_override_root_roles,
                            call_llm_fn=_call_llm,
                            role_catalog=_ch_catalog,
                            paragraphs_info=_section0_paragraphs,
                            marker_policy_1f=structure.get("marker_policy_1f"),
                            style_profiles=_style_profiles,
                            emphasis_layers=_emphasis_layers_by_cluster,
                            paragraph_emphasis_map=_paragraph_emphasis_map,
                            chapter_position=ch_idx,
                            total_chapters=len(_seed_chapters),
                        )
                        if _sf_result.get("chapter_title") and _sf_result["chapter_title"] != _2b_title:
                            _2b_title = _sf_result["chapter_title"]
                            if _decision:
                                _decision["adapted_title"] = _2b_title

                    _ch_items = _sf_result["body_items"]
                    _ch_status = "filled" if len(_ch_items) > 1 else "insufficient_source"
                    if _action == "preserve":
                        _ch_status = "preserved_by_13_7c"
                    _local_tree = _sf_result["chapter_tree_nodes"]
                    _local_debug = _sf_result["debug_entry"]
                    _pe = tpl_ch.get("_pattern_extraction", {}).get("stats", {})
                    _per_ch_status.append({"ch_idx": ch_idx, "template_title": ch_title[:50], "status": _ch_status, "item_count": len(_ch_items), "pattern_source": _ch_pattern_source, "local_role_count": len(_ch_pattern_roles) if _ch_local_pattern else None, "local_max_depth": _pe.get("max_depth") if _ch_local_pattern else None, "13_7c_action": _action})

                    # 13.7a-A1 + 13.7c: chapter object 수집
                    _ch_region = _tup_region_by_id.get(tpl_ch.get("region_id"))
                    _empty_reason = diagnose_chapter_empty_reason(_sf_result)
                    # 13.7c: reference_metrics (debug-only)
                    _ref_metrics = None
                    if _decision:
                        _gen_body_text = " ".join(
                            (it.get("text") or "") for it in _sf_result.get("body_items", [])
                        )
                        _ref_metrics = compute_reference_metrics(
                            _decision, broad_source=_broad_source,
                            generated_body_text=_gen_body_text,
                        )
                    _ch_obj = build_chapter_object(
                        source_chapter_idx=ch_idx,
                        target_region=_ch_region,
                        section_fill_result=_sf_result,
                        empty_reason=_empty_reason,
                        adaptation_decision=_decision,
                        reference_metrics=_ref_metrics,
                    )
                    # 13.7c: preserve action이면 chapter object status도 empty(region preserve)로
                    if _action == "preserve":
                        _ch_obj["status"] = "empty"
                    _local_obj = _ch_obj
                    _local_empty = _empty_reason
                    return (_local_tree, _local_debug, _local_obj, _local_empty)

                except Exception as e:
                    _flow_trace("route1_except", ch_idx=ch_idx, error_type=type(e).__name__, error_msg=str(e)[:300])
                    log.warning(f"2b[{ch_idx}] 실패 ({ch_title}): {e}")
                    _local_tree = None
                    _local_debug = {"idx": ch_idx, "chapter_title": ch_title, "chapter_type": _seed_ch_type, "error": str(e)}
                    _per_ch_status.append({"ch_idx": ch_idx, "template_title": ch_title[:50], "status": "error", "error": str(e)})
                    # 13.7a-A1: 실패한 chapter도 chapter object 자리 차지 (status="fail")
                    _ch_region = _tup_region_by_id.get(tpl_ch.get("region_id"))
                    _empty_reason = {"is_empty": True, "stage": "exception",
                                     "evidence": {"exception": str(e)}}
                    _ch_fail_obj = build_chapter_object(
                        source_chapter_idx=ch_idx,
                        target_region=_ch_region,
                        section_fill_result={"body_items": [], "chapter_tree_nodes": [],
                                             "items_count": 0, "grammar_passed": False,
                                             "debug_entry": {}},
                        empty_reason=_empty_reason,
                    )
                    _ch_fail_obj["status"] = "fail"
                    _ch_fail_obj["_debug"]["fail_reason"] = str(e)
                    _local_obj = _ch_fail_obj
                    _local_empty = _empty_reason
                    return (_local_tree, _local_debug, _local_obj, _local_empty)

            import asyncio as _tpl_asyncio
            _tpl_results = await _tpl_asyncio.gather(*[
                _process_tpl_chapter(_ci, _tc) for _ci, _tc in enumerate(_seed_chapters)
            ])
            for _t, _d, _o, _e in _tpl_results:
                _chapter_trees.append(_t)
                _section_fill_debug.append(_d)
                _chapter_objects.append(_o)
                _chapter_empty_reasons.append(_e)


            # debug: template plan vs 2a comparison
            _chapter_plan_debug = {
                "loop_driver": "template_plan",
                "seed": _chapter_plan_seed,
                "per_chapter_status": _per_ch_status,
                "source_mode": "broad_source_fallback",
                "source_length": len(_broad_source),
                "2a_chapters_ignored": [{"title": ch.get("title",""), "type": ch.get("type","")} for ch in chapters],
                "2a_vs_template_diff": {
                    "2a_count": len(chapters),
                    "template_count": len(_seed_chapters),
                    "count_match": len(chapters) == len(_seed_chapters),
                },
                "note": "broad source fallback은 최종 source allocation이 아닌 임시 안전장치. 13.6~13.7에서 evidence 기반 allocation으로 대체 예정.",
            }
            # 13.6-C: Source diagnostic
            _bs_len = len(_broad_source)
            _est_tok_per_ch = _bs_len // 4  # rough char/4 estimate
            _sd_per_ch = []
            _sd_anomalies = []
            for _pcs in _per_ch_status:
                _ic = _pcs.get("item_count", 0)
                _ratio = round(_ic / max(_bs_len / 200, 1), 2)  # items per ~200 chars
                _sd_per_ch.append({"ch": _pcs["ch_idx"], "items": _ic, "insufficient": _pcs.get("status") == "insufficient_source", "source_chars": _bs_len, "ratio": _ratio})
                if _bs_len > 10000 and _ic == 0:
                    _sd_anomalies.append({"ch": _pcs["ch_idx"], "type": "source_long_items_zero", "source_chars": _bs_len, "items": 0})
                if _bs_len < 1000 and _ic > 20:
                    _sd_anomalies.append({"ch": _pcs["ch_idx"], "type": "source_short_items_many", "source_chars": _bs_len, "items": _ic})
            _chapter_plan_debug["source_diagnostic"] = {
                "broad_source_chars": _bs_len,
                "estimated_tokens_per_chapter": _est_tok_per_ch,
                "total_estimated_tokens": _est_tok_per_ch * len(_seed_chapters),
                "per_chapter": _sd_per_ch,
                "anomalies": _sd_anomalies,
                "split_available": bool(source_sections),
                "split_section_lengths": [len(s) for s in source_sections] if source_sections else [],
            }
            _debug_payload["chapter_template_plan"] = _chapter_plan_debug

        elif not _shallow_done:
            # ── 기존 2a-driven chapter loop (fallback) ──
            _chapter_plan_debug = {"loop_driver": "2a_chapters", "seed": None, "fallback_reason": "no seed or low confidence"}
            _debug_payload["chapter_template_plan"] = _chapter_plan_debug

            import asyncio as _ch_asyncio

            async def _process_one_chapter(ch_idx, chapter):
                ch_type = chapter.get("type", "")
                ch_title = chapter.get("title", "")

                if ch_type not in chapter_types:
                    log.warning(f"2b 스킵: 알 수 없는 타입 '{ch_type}' (대제목: {ch_title})")
                    return ("skip", None, None, None, None)

                type_info = chapter_types[ch_type]
                pattern = type_info.get("pattern", {})
                title_role = type_info.get("title_role", "chapter_title")

                pattern_roles = _collect_roles(pattern)
                section_catalog = {r: full_role_catalog[r] for r in pattern_roles if r in full_role_catalog}

                section_pdf_text = source_sections[ch_idx] if ch_idx < len(source_sections) else ""

                if __event_emitter__:
                    await __event_emitter__({"type": "status", "data": {
                        "description": f"AI가 섹션 {ch_idx+1}/{len(chapters)} 콘텐츠 생성 중: {ch_title[:30]}...",
                        "done": False,
                    }})

                try:
                    messages_2b = build_section_fill_prompt(
                        ch_title, ch_type, pattern, section_catalog,
                        content_text=content_text, content_images=content_images, pdf_text=section_pdf_text,
                        exclusive_rules=[],
                        cooccurrence_rules=structure.get("sibling_cooccurrence_rules", []),
                        style_profiles=_style_profiles,
                        emphasis_layers=_emphasis_layers_by_cluster,
                        paragraph_emphasis_map=_paragraph_emphasis_map,
                        format_rules=structure.get("format_rules", {}),
                        role_text_types=structure.get("role_text_types"),
                        per_type_role_semantics=structure.get("per_type_role_semantics"),
                        content_only_mode=True,
                        marker_policy_1f=structure.get("marker_policy_1f"),
                    )
                    llm_content_2b = await _call_llm(messages_2b, f"hwpx_section_fill_{ch_idx}")
                    # 2b-b polish
                    try:
                        import os as _2bb_entry_os2, json as _2bb_entry_json2
                        _2bb_entry_os2.makedirs("/tmp/hwpx_debug", exist_ok=True)
                        with open("/tmp/hwpx_debug/2b_polish_apply_log.jsonl", "a", encoding="utf-8") as _f_entry2:
                            _f_entry2.write(_2bb_entry_json2.dumps({
                                "ch_idx": ch_idx, "stage": "entry_a",
                                "ch_title": (ch_title or "")[:60],
                                "llm_content_2b_len": len(llm_content_2b or ""),
                            }, ensure_ascii=False) + "\n")
                    except Exception as _entry_e2:
                        log.warning(f"[2b-b entry_a dump fail] {_entry_e2}")
                    try:
                        _raw_items_1st = parse_section_fill_from_llm(llm_content_2b)
                        if _raw_items_1st:
                            _polish_msgs = build_section_polish_prompt(
                                _raw_items_1st,
                                chapter_title=ch_title,
                                chapter_type_name=ch_type,
                                pattern=pattern,
                                role_catalog=section_catalog,
                                content_text=content_text,
                                content_images=content_images,
                                pdf_text=section_pdf_text,
                                exclusive_rules=[],
                                cooccurrence_rules=structure.get("sibling_cooccurrence_rules", []),
                                style_profiles=_style_profiles,
                                emphasis_layers=_emphasis_layers_by_cluster,
                                paragraph_emphasis_map=_paragraph_emphasis_map,
                                format_rules=structure.get("format_rules", {}),
                                role_text_types=structure.get("role_text_types"),
                                per_type_role_semantics=structure.get("per_type_role_semantics"),
                                content_only_mode=True,
                                marker_policy_1f=structure.get("marker_policy_1f"),
                                paragraphs=structure.get("paragraphs"),
                                idx_full_texts=_idx_full_texts,
                            )
                            _polish_raw = await _call_llm(_polish_msgs, f"hwpx_section_polish_{ch_idx}")
                            _polish_items = parse_section_polish_from_llm(_polish_raw)
                            try:
                                import os as _2bb_os, json as _2bb_json
                                _2bb_os.makedirs("/tmp/hwpx_debug", exist_ok=True)
                                with open("/tmp/hwpx_debug/2b_polish_apply_log.jsonl", "a", encoding="utf-8") as _f:
                                    _f.write(_2bb_json.dumps({
                                        "ch_idx": ch_idx, "ch_title": (ch_title or "")[:60],
                                        "raw_items_count": len(_raw_items_1st),
                                        "polish_items_count": len(_polish_items),
                                        "polish_raw_len": len(_polish_raw or ""),
                                        "polish_raw_full": (_polish_raw or ""),
                                    }, ensure_ascii=False) + "\n")
                            except Exception:
                                pass
                            if _polish_items:
                                import json as _json_pi
                                llm_content_2b = _json_pi.dumps({"items": _polish_items}, ensure_ascii=False)
                    except Exception as _polish_e:
                        log.warning(f"[2b-b polish ch_idx={ch_idx}] 실패: {_polish_e}")
                    _sf_result = await process_section_fill_result(
                        llm_content_2b,
                        ch_idx=ch_idx,
                        ch_title=ch_title,
                        ch_type=ch_type,
                        title_role=title_role,
                        template_grammar=structure.get("template_grammar", {}),
                        role_text_types=structure.get("role_text_types"),
                        pattern_roles=list(pattern_roles),
                        section_pdf_text_len=len(section_pdf_text),
                        call_llm_fn=_call_llm,
                        role_catalog=section_catalog,
                        paragraphs_info=_section0_paragraphs,
                        marker_policy_1f=structure.get("marker_policy_1f"),
                        style_profiles=_style_profiles,
                        emphasis_layers=_emphasis_layers_by_cluster,
                        paragraph_emphasis_map=_paragraph_emphasis_map,
                        chapter_position=ch_idx,
                        total_chapters=len(chapters),
                    )
                    if _sf_result.get("chapter_title") and _sf_result["chapter_title"] != ch_title:
                        ch_title = _sf_result["chapter_title"]

                    _ch_region_2a = (
                        _tup_chapter_regions[ch_idx]
                        if ch_idx < len(_tup_chapter_regions) else None
                    )
                    _empty_reason_2a = diagnose_chapter_empty_reason(_sf_result)
                    _ch_obj_2a = build_chapter_object(
                        source_chapter_idx=ch_idx,
                        target_region=_ch_region_2a,
                        section_fill_result=_sf_result,
                        empty_reason=_empty_reason_2a,
                    )
                    if _ch_region_2a is None:
                        _ch_obj_2a["_debug"]["region_match"] = "fallback_no_region"

                    return (
                        "ok",
                        _sf_result["chapter_tree_nodes"],
                        _sf_result["debug_entry"],
                        _ch_obj_2a,
                        _empty_reason_2a,
                    )

                except Exception as e:
                    log.warning(f"2b[{ch_idx}] 실패 ({ch_title}): {e}")
                    _ch_region_2a = (
                        _tup_chapter_regions[ch_idx]
                        if ch_idx < len(_tup_chapter_regions) else None
                    )
                    _empty_reason_2a = {"is_empty": True, "stage": "exception",
                                        "evidence": {"exception": str(e)}}
                    _ch_fail_obj_2a = build_chapter_object(
                        source_chapter_idx=ch_idx,
                        target_region=_ch_region_2a,
                        section_fill_result={"body_items": [], "chapter_tree_nodes": [],
                                             "items_count": 0, "grammar_passed": False,
                                             "debug_entry": {}},
                        empty_reason=_empty_reason_2a,
                    )
                    _ch_fail_obj_2a["status"] = "fail"
                    _ch_fail_obj_2a["_debug"]["fail_reason"] = str(e)
                    return (
                        "error",
                        None,
                        {
                            "idx": ch_idx,
                            "chapter_title": ch_title,
                            "chapter_type": ch_type,
                            "error": str(e),
                        },
                        _ch_fail_obj_2a,
                        _empty_reason_2a,
                    )

            _ch_results = await _ch_asyncio.gather(*[
                _process_one_chapter(_ci, _ch) for _ci, _ch in enumerate(chapters)
            ])
            for _status, _tree, _dbg, _cobj, _ereason in _ch_results:
                if _status == "skip":
                    continue
                _chapter_trees.append(_tree)
                _section_fill_debug.append(_dbg)
                _chapter_objects.append(_cobj)
                _chapter_empty_reasons.append(_ereason)

        # ══════════════════════════════════════════════════════════════
        # 13.7a-0: A0 병행 measurement (debug-only)
        # ══════════════════════════════════════════════════════════════
        try:
            # A0-1: 1d chapter_types.title_role vs chapter_template_plan local_title_role
            _ctp_wrap = {"seed": _chapter_plan_seed} if _chapter_plan_seed else None
            _trc = measure_title_role_consistency(structure, _ctp_wrap)
            _debug_payload["title_role_consistency"] = _trc
            log.info(
                f"13.7a-0 title_role_consistency: status={_trc.get('status')}, "
                f"all_local_in_1d_set={_trc.get('mismatch_summary',{}).get('all_local_in_1d_set')}, "
                f"missing_from_1d_set={_trc.get('mismatch_summary',{}).get('missing_from_1d_set')}"
            )
        except Exception as _a0_e:
            log.warning(f"13.7a-0 title_role_consistency failed: {_a0_e}")
            _debug_payload["title_role_consistency"] = {"error": str(_a0_e), "debug_only": True}

        # ══════════════════════════════════════════════════════════════
        # 13.7b-B0a: Pre-1a Section Census (debug-only, AI 호출 0)
        # shallow/chapter route 무관하게 모든 양식에 호출 (측정 단계)
        # ══════════════════════════════════════════════════════════════
        try:
            _section_census = extract_section_census(template_path)
            _debug_payload["section_census"] = _section_census
            _ref = _section_census.get("reference_metrics", {}) or {}
            log.info(
                f"13.7b B0a section_census: "
                f"sections={_section_census.get('section_count')}, "
                f"total_paragraphs={_ref.get('total_paragraphs')}"
            )
        except Exception as _sc_e:
            log.warning(f"13.7b B0a section_census failed: {_sc_e}")
            _debug_payload["section_census"] = {"error": str(_sc_e)}

        # ══════════════════════════════════════════════════════════════
        # 13.7b B2.2: Section Role Proposal AI sub-step (chapter route only)
        # 각 section의 structural_relationship/placement_recommendation을 batch로 AI 호출.
        # CC7 shallow route 미진입 (사용자 §4). debug-only — production HWP 영향 X.
        # 의미 매핑은 B0b review에서 사용자+claude 합의 (§9.5).
        # ══════════════════════════════════════════════════════════════
        if not _shallow_done and section_results:
            try:
                _census_sections = (_section_census or {}).get("sections", []) if isinstance(_section_census, dict) else []
                _census_by_sid = {
                    s.get("section_id"): s
                    for s in _census_sections
                    if isinstance(s, dict)
                }
                _template_title_hint = ""
                if _census_sections:
                    _template_title_hint = (_census_sections[0].get("first_paragraph_preview") or "")[:200]

                _sections_summary_b22 = []
                _sorted_sids = sorted(
                    (int(k) if str(k).isdigit() else 999)
                    for k in section_results.keys()
                )
                for _sid_int in _sorted_sids:
                    _sr = section_results.get(_sid_int) or section_results.get(str(_sid_int))
                    if not isinstance(_sr, dict):
                        continue
                    _ce = _census_by_sid.get(_sid_int, {})
                    _sections_summary_b22.append(
                        summarize_section_for_proposal(_sid_int, _sr, _ce)
                    )

                _doc_ctx_b22 = {
                    "template_title": _template_title_hint,
                    "section_count": len(_sections_summary_b22),
                    "route": "chapter",
                }

                _srp_messages = build_section_role_proposal_prompt(
                    _sections_summary_b22, _doc_ctx_b22
                )
                _srp_ai_info = {
                    "raw_response_len": 0,
                    "retry_count": 0,
                    "validation_ok": False,
                    "errors": [],
                }
                _srp_proposals: list = []
                _srp_validation_results: list = []
                _srp_fallback_count = 0
                _expected_sids = [s["section_id"] for s in _sections_summary_b22]

                if _expected_sids:
                    try:
                        _srp_raw = await _call_llm(
                            _srp_messages, "hwpx_13_7b_section_role_proposal"
                        )
                        _srp_ai_info["raw_response_len"] = len(_srp_raw or "")
                        _srp_parsed = parse_section_role_proposal_from_llm(
                            _srp_raw, _expected_sids
                        )
                        if not _srp_parsed["_validation"]["ok"]:
                            _srp_ai_info["retry_count"] = 1
                            log.info("13.7b B2.2 retry (1)")
                            _srp_raw = await _call_llm(
                                _srp_messages,
                                "hwpx_13_7b_section_role_proposal_retry",
                            )
                            _srp_ai_info["raw_response_len"] = len(_srp_raw or "")
                            _srp_parsed = parse_section_role_proposal_from_llm(
                                _srp_raw, _expected_sids
                            )

                        _srp_ai_info["validation_ok"] = _srp_parsed["_validation"]["ok"]
                        _srp_ai_info["errors"] = list(_srp_parsed["_validation"]["errors"])
                        _missing_sids = list(
                            _srp_parsed["_validation"]["missing_section_ids"]
                        )

                        for _p in _srp_parsed["section_role_proposals"]:
                            _v = validate_section_role_proposal(_p)
                            _srp_validation_results.append(_v)
                            if _v["ok"]:
                                _srp_proposals.append(_p)
                            else:
                                _fb = make_fallback_section_role_proposal(
                                    _p.get("section_id"),
                                    "validation_failed",
                                    ",".join(_v["errors"])[:200],
                                )
                                _srp_proposals.append(_fb)
                                _srp_fallback_count += 1

                        for _sid in _missing_sids:
                            _fb = make_fallback_section_role_proposal(
                                _sid, "missing_in_response", ""
                            )
                            _srp_proposals.append(_fb)
                            _srp_validation_results.append(
                                {"ok": False, "errors": ["missing_in_response"]}
                            )
                            _srp_fallback_count += 1

                    except Exception as _srp_call_e:
                        log.warning(
                            f"13.7b B2.2 section_role_proposal AI call failed: {_srp_call_e}"
                        )
                        _srp_ai_info["errors"].append(
                            f"call_failed: {str(_srp_call_e)[:200]}"
                        )
                        for _s in _sections_summary_b22:
                            _fb = make_fallback_section_role_proposal(
                                _s["section_id"],
                                "call_failed",
                                str(_srp_call_e)[:200],
                            )
                            _srp_proposals.append(_fb)
                            _srp_validation_results.append(
                                {"ok": False, "errors": ["call_failed"]}
                            )
                            _srp_fallback_count += 1

                _debug_payload["section_role_proposals"] = summarize_section_role_proposals(
                    _srp_proposals,
                    validation_results=_srp_validation_results,
                    ai_call_info=_srp_ai_info,
                    fallback_count=_srp_fallback_count,
                )
                log.info(
                    f"13.7b B2.2 section_role_proposal: "
                    f"{len(_srp_proposals)} proposals "
                    f"({_srp_fallback_count} fallback), "
                    f"validation_ok={_srp_ai_info['validation_ok']}, "
                    f"retry={_srp_ai_info['retry_count']}"
                )
            except Exception as _srp_e:
                log.warning(f"13.7b B2.2 section_role_proposal failed: {_srp_e}")
                _debug_payload["section_role_proposals"] = {
                    "error": str(_srp_e), "debug_only": True
                }

        # ══════════════════════════════════════════════════════════════
        # 13.7b B0b: Post-1a Merge Feasibility Measurement (chapter route only)
        # section_results + section_census 비교 metric 측정 → B3 정책 결정 evidence.
        # debug-only — production HWP 영향 X. 정책 결정은 B0b review에서 합의.
        # ══════════════════════════════════════════════════════════════
        if not _shallow_done and section_results:
            try:
                _b0b_mf = measure_merge_feasibility(section_results, _section_census)
                _debug_payload["merge_feasibility"] = _b0b_mf

                _srp_summary_for_artifact = _debug_payload.get("section_role_proposals") or {}
                if isinstance(_srp_summary_for_artifact, dict) and "error" in _srp_summary_for_artifact:
                    _srp_summary_for_artifact = {"error": _srp_summary_for_artifact.get("error")}
                _b0b_artifact = build_b0b_observation_artifact(
                    _b0b_mf, section_role_proposals_summary=_srp_summary_for_artifact
                )
                _debug_payload["b0b_observation_artifact"] = _b0b_artifact

                _csp_count = _b0b_mf.get("cross_section_parent", {}).get(
                    "cross_section_parent_violation_count", 0
                )
                _conflict_count = _b0b_mf.get(
                    "section_marker_policy_comparison", {}
                ).get("role_type_conflict_count", 0)
                log.info(
                    f"13.7b B0b merge_feasibility: "
                    f"cross_section_parent_violations={_csp_count}, "
                    f"marker_policy_conflicts={_conflict_count}"
                )
            except Exception as _b0b_e:
                log.warning(f"13.7b B0b merge_feasibility failed: {_b0b_e}")
                _debug_payload["merge_feasibility"] = {
                    "error": str(_b0b_e), "debug_only": True
                }

        # 13.7c: adaptation_plan summary
        if _adaptation_plan_summary is not None:
            _debug_payload["adaptation_plan"] = _adaptation_plan_summary

        # A0-2: chapter empty reasons (collected per chapter)
        if _chapter_empty_reasons:
            _debug_payload["chapter_empty_reasons"] = _chapter_empty_reasons

        # ══════════════════════════════════════════════════════════════
        # 13.7b section-local generation-lite (chapter route, section N != 0)
        # 각 section을 자기 section_results 구조로 독립 generation.
        # section 0은 기존 13.4b extract_chapter_template_plan_seed 경로 (위에서 처리됨).
        # section N (N != 0): extract_section_chapter_list + section-local 13.7c + 2b loop.
        # B3 document-level merge 없음. section 간 parent/role/chapter merge 없음.
        # 사용자 directive 2026-05-15.
        # ══════════════════════════════════════════════════════════════
        _section_local_decisions: dict = {}
        _section_local_chapter_lists: dict = {}
        _section_local_offsets: dict = {}
        _analyzed_section_ids: set = {0}

        if not _shallow_done and section_results and _chapter_objects is not None:
            try:
                # 13.7b fix: section_xml top-level p count 기준 census offset
                # (1a paragraph 누락 영향 보정 — assembly _section_top_level_paragraphs와 일치)
                _section_xml_paragraph_counts: dict = {}
                _section_xml_paragraph_texts: dict = {}
                try:
                    from open_webui.utils.hwpx_analyzer import extract_all_sections_xml as _eas
                    _all_sec_xml = _eas(template_path)
                    for _sid_pos, (_sname, _sxml) in enumerate(_all_sec_xml):
                        # text 추출
                        _xml_texts = extract_section_xml_paragraph_texts(template_path, _sname)
                        _section_xml_paragraph_texts[_sid_pos] = _xml_texts
                        _section_xml_paragraph_counts[_sid_pos] = len(_xml_texts)
                except Exception as _e_xml:
                    log.warning(f"13.7b section_xml text 추출 실패: {_e_xml}")

                _section_local_offsets = compute_section_offsets(
                    section_results, _section_xml_paragraph_counts or None
                )

                _b22_summary_for_decision = _debug_payload.get("section_role_proposals") or {}
                _b22_proposals_list = []
                if isinstance(_b22_summary_for_decision, dict):
                    _b22_proposals_list = _b22_summary_for_decision.get("proposals") or []
                _b22_by_sid: dict = {}
                for _p in _b22_proposals_list:
                    if isinstance(_p, dict):
                        _sid_key_p = _p.get("section_id")
                        if _sid_key_p is not None:
                            _b22_by_sid[_sid_key_p] = _p

                # 모든 section의 chapter_list + decision 계산 (section 0 포함, debug용)
                for _sid_key in sorted(
                    section_results.keys(),
                    key=lambda k: int(k) if str(k).isdigit() else 999
                ):
                    try:
                        _sid_int = int(_sid_key)
                    except (TypeError, ValueError):
                        _sid_int = _sid_key
                    _sr_sec = section_results.get(_sid_key) or section_results.get(_sid_int)
                    if not isinstance(_sr_sec, dict):
                        continue
                    _offset = _section_local_offsets.get(_sid_int, 0)
                    # 13.7b fix: 1a → xml idx mapping
                    _xml_texts_for_sec = _section_xml_paragraph_texts.get(_sid_int, [])
                    _1a_to_xml_map = _build_1a_to_xml_p_idx_mapping(
                        _sr_sec.get("idx_texts", {}) or {},
                        _xml_texts_for_sec,
                    ) if _xml_texts_for_sec else {}
                    _scl = extract_section_chapter_list(
                        _sid_int, _sr_sec, _offset,
                        ai_to_xml_idx_mapping=_1a_to_xml_map,
                    )
                    _section_local_chapter_lists[_sid_int] = _scl

                    if _sid_int == 0:
                        # section 0은 기존 13.4b path 사용 (이미 위에서 처리됨)
                        _section_local_decisions[_sid_int] = {
                            "action": "existing_chapter_route",
                            "reason": "section_0_uses_existing_13_4b_path",
                            "deadline_policy_relaxation": False,
                            "details": None,
                        }
                        continue

                    _b22_p = _b22_by_sid.get(_sid_int)
                    _dec = decide_section_processing(_sid_int, _b22_p, _scl)
                    _section_local_decisions[_sid_int] = _dec

                # source_inventory: section 0 13.7c에서 _source_inventory set됐을 가능성
                _section_n_si = None
                try:
                    _section_n_si = _source_inventory  # noqa: F821 (set in 13.7c block above)
                except NameError:
                    _section_n_si = None

                # generate section N에 대해 13.7c adaptation_plan + 2b loop
                _section_n_chapter_count_total = 0
                for _sid_int, _dec in _section_local_decisions.items():
                    if _dec.get("action") != "generate":
                        continue
                    _scl_sec = _section_local_chapter_lists.get(_sid_int) or {}
                    _sec_chapters = _scl_sec.get("chapters") or []
                    if not _sec_chapters:
                        continue

                    _sr_sec = section_results.get(_sid_int) or section_results.get(str(_sid_int))
                    if not isinstance(_sr_sec, dict):
                        continue
                    _sec_structure = _sr_sec.get("structure", {}) or {}
                    _sec_chapter_types = _sr_sec.get("chapter_types", {}) or {}

                    # source_inventory: 미정의면 호출 (1회만)
                    if _section_n_si is None:
                        try:
                            _si_msgs_n = build_source_inventory_prompt(_broad_source)
                            _si_raw_n = await _call_llm(
                                _si_msgs_n,
                                f"hwpx_13_7b_section_n_source_inventory"
                            )
                            _section_n_si = parse_source_inventory_from_llm(_si_raw_n)
                            if not _section_n_si.get("_validation", {}).get("ok"):
                                _si_raw_n = await _call_llm(
                                    _si_msgs_n,
                                    f"hwpx_13_7b_section_n_source_inventory_retry"
                                )
                                _section_n_si = parse_source_inventory_from_llm(_si_raw_n)
                        except Exception as _si_e:
                            log.warning(f"13.7b section N source_inventory failed: {_si_e}")
                            _section_n_si = None

                    # Build chapter_inputs for adaptation_plan (per section)
                    _sec_ch_inputs = []
                    for _ch_local in _sec_chapters:
                        _dom_type = _ch_local.get("dominant_chapter_type")
                        _dom_ct_val = _sec_chapter_types.get(_dom_type, {}) if _dom_type else {}
                        _dom_pattern = _dom_ct_val.get("pattern", {}) if isinstance(_dom_ct_val, dict) else {}
                        _sec_ch_inputs.append({
                            "idx": _ch_local["chapter_idx"],
                            "original_title": _ch_local["template_title"],
                            "description": _ch_local["description"],
                                "local_catalog_summary": "",
                        })

                    # adaptation_plan
                    _sec_decisions_by_idx: dict = {}
                    _sec_ch_input_by_idx = {c["idx"]: c for c in _sec_ch_inputs}
                    if _section_n_si:
                        try:
                            _ap_msgs_n = build_adaptation_plan_prompt(
                                _section_n_si, _sec_ch_inputs,
                                broad_source_preview=_broad_source,
                                max_source_preview_chars=0,
                            )
                            _ap_raw_n = await _call_llm(
                                _ap_msgs_n,
                                f"hwpx_13_7b_section_n_adaptation_plan_sec{_sid_int}"
                            )
                            _ap_parsed_n = parse_adaptation_plan_from_llm(
                                _ap_raw_n, [c["idx"] for c in _sec_ch_inputs]
                            )
                            # normalize → validate → 강등 (기존 section 0 13.7c 패턴 일관)
                            for _raw_d in (_ap_parsed_n.get("chapter_decisions") or []):
                                _raw_idx = _raw_d.get("chapter_idx")
                                if _raw_idx is None or _raw_idx not in _sec_ch_input_by_idx:
                                    continue
                                _orig_title_n = _sec_ch_input_by_idx[_raw_idx].get(
                                    "original_title", ""
                                )
                                _norm_n = normalize_adaptation_decision(_raw_d, _orig_title_n)
                                _v_n = validate_adaptation_decision(_norm_n)
                                if _v_n.get("should_demote"):
                                    _norm_n = make_validation_failed_decision(
                                        _raw_idx,
                                        _orig_title_n,
                                        _v_n.get("violations") or [],
                                    )
                                _sec_decisions_by_idx[_raw_idx] = _norm_n
                        except Exception as _ap_e:
                            log.warning(
                                f"13.7b section {_sid_int} adaptation_plan failed: {_ap_e}"
                            )

                    # missing chapter fallback (plan_unavailable) — 기존 section 0 패턴 일관
                    for _ci_e in [c["idx"] for c in _sec_ch_inputs]:
                        if _ci_e not in _sec_decisions_by_idx:
                            _orig_t_e = _sec_ch_input_by_idx.get(_ci_e, {}).get(
                                "original_title", ""
                            )
                            _sec_decisions_by_idx[_ci_e] = make_unavailable_decision(
                                _ci_e, _orig_t_e,
                                "ai_returned_no_decision_for_chapter",
                            )

                    # Per-chapter 2b loop (section-local context)
                    _sec_full_catalog = {}
                    for _p_s in _sec_structure.get("paragraphs", []) or []:
                        if not isinstance(_p_s, dict):
                            continue
                        _r_s = _p_s.get("role")
                        if _r_s and _r_s not in _sec_full_catalog:
                            _sec_full_catalog[_r_s] = {
                                "exemplar": _p_s.get("text", "") or "",
                                "marker": _p_s.get("marker", "") or "",
                            }
                    _sec_first_chapter_type = next(iter(_sec_chapter_types.keys()), None)

                    for _ch_local in _sec_chapters:
                        _ci = _ch_local["chapter_idx"]
                        _title_text = _ch_local["template_title"]
                        _ch_decision_n = _sec_decisions_by_idx.get(_ci)
                        if _ch_decision_n is None:
                            _ch_decision_n = make_unavailable_decision(
                                _ci, _title_text, "no_adaptation_decision"
                            )
                        _action_n = _ch_decision_n.get("action") or "preserve"
                        _2b_title_n = _title_text
                        if _action_n == "adapted_title_generate":
                            _adapted_n = _ch_decision_n.get("adapted_title")
                            if isinstance(_adapted_n, str) and _adapted_n.strip():
                                _2b_title_n = _adapted_n

                        _dom_type_n = _ch_local.get("dominant_chapter_type") or _sec_first_chapter_type
                        _dom_ct_val_n = _sec_chapter_types.get(_dom_type_n, {}) if _dom_type_n else {}
                        _dom_pattern_n = _dom_ct_val_n.get("pattern", {}) if isinstance(_dom_ct_val_n, dict) else {}
                        _dom_pattern_roles_n = list(_collect_roles(_dom_pattern_n)) if isinstance(_dom_pattern_n, dict) and _dom_pattern_n else []
                        _dom_catalog_n = {
                            r: _sec_full_catalog[r]
                            for r in _dom_pattern_roles_n
                            if r in _sec_full_catalog
                        }
                        _title_role_sec_n = _ch_local.get("title_role") or ""

                        # 13.7b: preserve 시 synthetic title_item을 _sf_result에 채워서
                        # build_chapter_object가 title_item.role을 추출하게 함.
                        # assembly placeholder role 결정 (title_role_fallback)에 사용됨.
                        # marker는 idx_texts에서 추출 (양식 원본 그대로).
                        _synthetic_title_item_n = {
                            "role": _title_role_sec_n,
                            "text": _title_text or "",
                            "marker": _ch_local.get("marker", "") or "",
                        }
                        if _action_n == "preserve":
                            _sf_result_n = {
                                "body_items": [_synthetic_title_item_n],
                                "chapter_tree_nodes": [{
                                    "id": 0,
                                    "parent_id": None,
                                    "role": _synthetic_title_item_n["role"],
                                    "text": _synthetic_title_item_n["text"],
                                }],
                                "items_count": 0,  # body items (title 제외) — 0
                                "grammar_passed": True,
                                "debug_entry": {
                                    "idx": _ci,
                                    "section_id": _sid_int,
                                    "chapter_title": _title_text,
                                    "preserved_by_13_7c": True,
                                    "preserve_reason": _ch_decision_n.get("preserve_reason"),
                                    "preserve_reason_detail": _ch_decision_n.get("preserve_reason_detail"),
                                },
                            }
                        else:
                            try:
                                _msgs_2b_n = build_section_fill_prompt(
                                    _2b_title_n,
                                    _dom_type_n or "type_1",
                                    _dom_pattern_n or {},
                                    _dom_catalog_n,
                                    content_text=content_text,
                                    content_images=content_images,
                                    pdf_text=_broad_source,
                                    exclusive_rules=[],  # opted-out blacklist
                                    cooccurrence_rules=_sec_structure.get("sibling_cooccurrence_rules", []),

                                    style_profiles=_style_profiles,

                                    emphasis_layers=_emphasis_layers_by_cluster,
                            paragraph_emphasis_map=_paragraph_emphasis_map,
                                    format_rules=_sec_structure.get("format_rules", {}),
                                    role_text_types=_sec_structure.get("role_text_types"),
                                    per_type_role_semantics=_sec_structure.get("per_type_role_semantics"),
                                    content_only_mode=True,
                                    template_chapter_context={
                                        "template_title": _title_text,
                                        "description": _ch_local.get("description", ""),
                                        "section_id": _sid_int,
                                        "position": _ci,
                                        "total_chapters": len(_sec_chapters),
                                    },
                                    marker_policy_1f=structure.get("marker_policy_1f"),
                                )
                                # 신 2a adapted_title_generate hint prepend (section 0과 동일 패턴)
                                # 2026-05-23: supporting_evidence / preserved_aspects / adapted_aspects 제거
                                # (좁은 evidence가 LLM을 매달리게 만드는 문제). adapted_title만 박음.
                                if _action_n == "adapted_title_generate":
                                    _hint_parts_n = ["[신 2a adaptation hint]"]
                                    _hint_parts_n.append(f"adapted_title: {_2b_title_n}")
                                    _hint_parts_n.append(f"original_title: {_title_text}")
                                    _hint_parts_n.append(
                                        "adapted_title의 의미를 살리되, template chapter의 역할/흐름은 보존하세요. "
                                        "source 본문 전체에서 chapter role에 맞는 내용을 자유롭게 선택하세요."
                                    )
                                    _hint_text_n = "\n".join(str(x) for x in _hint_parts_n)
                                    for _mi_n, _msg_n in enumerate(_msgs_2b_n):
                                        if _msg_n.get("role") == "user":
                                            _msgs_2b_n[_mi_n] = {
                                                "role": "user",
                                                "content": _hint_text_n + "\n\n" + _msg_n.get("content", ""),
                                            }
                                            break

                                _llm_2b_n = await _call_llm(
                                    _msgs_2b_n,
                                    f"hwpx_section_fill_sec{_sid_int}_ch{_ci}"
                                )
                                _flow_trace("call_route3", ch_idx=_ci)
                                _sf_result_n = await process_section_fill_result(
                                    _llm_2b_n,
                                    ch_idx=_ci,
                                    ch_title=_2b_title_n,
                                    ch_type=_dom_type_n or "type_1",
                                    title_role=_title_role_sec_n,
                                    template_grammar=_sec_structure.get("template_grammar", {}),
                                    role_text_types=_sec_structure.get("role_text_types"),
                                    pattern_roles=_dom_pattern_roles_n,
                                    section_pdf_text_len=len(_broad_source),
                                    call_llm_fn=_call_llm,
                                    role_catalog=_dom_catalog_n,
                                    paragraphs_info=_section0_paragraphs,
                                    marker_policy_1f=_sec_structure.get("marker_policy_1f"),
                                    style_profiles=_style_profiles,
                                    emphasis_layers=_emphasis_layers_by_cluster,
                                    paragraph_emphasis_map=_paragraph_emphasis_map,
                                    chapter_position=_ci,
                                    total_chapters=len(_sec_chapters),
                                )
                                if _sf_result_n.get("chapter_title") and _sf_result_n["chapter_title"] != _2b_title_n:
                                    _2b_title_n = _sf_result_n["chapter_title"]
                                    if _ch_decision_n:
                                        _ch_decision_n["adapted_title"] = _2b_title_n
                            except Exception as _2b_e_n:
                                _flow_trace("route3_except", ch_idx=_ci, error_type=type(_2b_e_n).__name__, error_msg=str(_2b_e_n)[:300])
                                log.warning(
                                    f"13.7b 2b sec{_sid_int} ch{_ci} failed: {_2b_e_n}"
                                )
                                _sf_result_n = {
                                    "body_items": [],
                                    "chapter_tree_nodes": [],
                                    "items_count": 0,
                                    "grammar_passed": False,
                                    "debug_entry": {
                                        "idx": _ci,
                                        "section_id": _sid_int,
                                        "error": str(_2b_e_n)[:200],
                                    },
                                }

                        # Build chapter_object with section_id=N + section_local idx primary
                        # + doc_global paragraph_indices (legacy compat)
                        # 13.7b: section_local_first_idx 제공 → assembly Priority 1 anchor 매칭
                        _synthetic_region_n = {
                            "region_id": None,  # synthetic — no region_id
                            "section_id": _sid_int,
                            "section_local_first_idx": _ch_local.get("title_section_local_idx"),
                            "section_local_paragraph_indices": _ch_local.get(
                                "section_local_paragraph_indices", []
                            ),
                            "title_role": _title_role_sec_n,
                            "marker": _ch_local.get("marker", "") or "",
                            "paragraph_indices": _ch_local["document_global_paragraph_indices"],
                        }
                        _empty_reason_n = diagnose_chapter_empty_reason(_sf_result_n)
                        _ch_obj_n = build_chapter_object(
                            source_chapter_idx=_ci,
                            target_region=_synthetic_region_n,
                            section_fill_result=_sf_result_n,
                            empty_reason=_empty_reason_n,
                            adaptation_decision=_ch_decision_n,
                        )
                        if _action_n == "preserve":
                            _ch_obj_n["status"] = "empty"

                        # section_id 강제 확인 (region.section_id → build_chapter_object 내부 매핑)
                        _ch_obj_n["section_id"] = _sid_int

                        _chapter_objects.append(_ch_obj_n)
                        _section_n_chapter_count_total += 1

                    _analyzed_section_ids.add(_sid_int)
                    log.info(
                        f"13.7b section {_sid_int} section-local generation-lite: "
                        f"chapters={len(_sec_chapters)}, "
                        f"decisions={[(c['chapter_idx'], _sec_decisions_by_idx.get(c['chapter_idx'], {}).get('action', 'unavail')) for c in _sec_chapters[:5]]}..."
                    )

                _debug_payload["section_local_decisions"] = summarize_section_local_decisions(
                    _section_local_decisions, _section_local_chapter_lists
                )
                _debug_payload["section_local_chapter_lists"] = {
                    str(sid): scl for sid, scl in _section_local_chapter_lists.items()
                }
                log.info(
                    f"13.7b section-local generation-lite: "
                    f"generated_sections={sorted(_analyzed_section_ids - {0})}, "
                    f"section_n_chapters_added={_section_n_chapter_count_total}"
                )
            except Exception as _sl_e:
                log.warning(
                    f"13.7b section-local generation-lite failed: {_sl_e}",
                    exc_info=True,
                )
                _debug_payload["section_local_decisions"] = {
                    "error": str(_sl_e), "debug_only": True
                }

        # ══════════════════════════════════════════════════════════════
        # 조립 (코드, 번호 X): XML 조립 + HWPX 파일 출력
        # ══════════════════════════════════════════════════════════════
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "문서 조립 중...", "done": False}})

        # 13.7a-A1: chapter route는 chapters만, shallow route는 body만
        if _chapter_objects is not None and not _shallow_done:
            # chapter route
            content_data = {"header": header_data, "chapters": _chapter_objects}
            log.info(
                f"최종 콘텐츠 (chapter route): header={list(header_data.keys())}, "
                f"chapters={len(_chapter_objects)}개 "
                f"(ok={sum(1 for c in _chapter_objects if c.get('status') == 'ok')}, "
                f"empty={sum(1 for c in _chapter_objects if c.get('status') == 'empty')}, "
                f"fail={sum(1 for c in _chapter_objects if c.get('status') == 'fail')})"
            )
        else:
            # shallow route 또는 chapter route 진입 불가
            content_data = {"header": header_data, "body": body_items}
            log.info(f"최종 콘텐츠 (flat): header={list(header_data.keys())}, body={len(body_items)}개")

        if not _shallow_done:
            # ── 13.5: Region Action Plan — attachment preserve ──
            _region_plan = None
            _chapter_preserve = None
            if _tup:
                _region_plan = compute_region_action_plan(_tup, structure, idx_map=idx_map)
                if _region_plan:
                    _pi = _region_plan.get("preserve_indices", [])
                    _chapter_preserve = set(_pi) if _pi else None
                    _debug_payload["region_action_plan"] = _region_plan
                    log.info(
                        f"13.5 region action plan: {_region_plan['summary']['total_regions']} regions, "
                        f"preserve={len(_pi)} paragraphs, warnings={len(_region_plan.get('warnings', []))}"
                    )

            # 13.6-A: Multi-section diagnostic
            try:
                _ms_diag = diagnose_multi_section(template_path)
                _debug_payload["multi_section_diagnostic"] = _ms_diag
                if _ms_diag.get("section_count", 0) > 1:
                    _gd = _ms_diag.get("gate_decision", {})
                    log.info(
                        f"13.6-A multi-section: {_ms_diag['section_count']} sections, "
                        f"priority={_gd.get('recommendation_priority')}, "
                        f"assembly_needed={_gd.get('section_aware_assembly_needed')}"
                    )
            except Exception as _ms_e:
                log.warning(f"13.6-A multi-section diagnostic failed: {_ms_e}")
                _debug_payload["multi_section_diagnostic"] = {"error": str(_ms_e)}

            # 13.7b §4 chapter-local exemplars 구성
            # 각 chapter의 자기 영역 paragraph 본보기 (role → xml idx). assembly가 body item
            # insert 시 chapter-local 우선 사용 → §4 chapter-local pattern preservation 실현.
            _chapter_local_exemplars: dict = {}
            try:
                # section N chapters (N != 0) — section_local_chapter_lists 사용
                _ai_to_xml_for_local: dict = {}
                if isinstance(_section_local_chapter_lists, dict):
                    for _sid_int in _section_local_chapter_lists.keys():
                        try:
                            _sid_int_n = int(_sid_int) if not isinstance(_sid_int, int) else _sid_int
                        except (TypeError, ValueError):
                            continue
                        _xml_texts_l = _section_xml_paragraph_texts.get(_sid_int_n, [])
                        if _xml_texts_l:
                            _sr_for_m = section_results.get(_sid_int_n) or section_results.get(str(_sid_int_n)) or {}
                            _ai_to_xml_for_local[_sid_int_n] = _build_1a_to_xml_p_idx_mapping(
                                _sr_for_m.get("idx_texts", {}) or {}, _xml_texts_l
                            )

                # section N chapter local exemplars
                _section_n_local_dict = build_chapter_local_exemplars(
                    {sid: scl for sid, scl in _section_local_chapter_lists.items() if sid != 0},
                    section_results,
                    _ai_to_xml_for_local,
                ) if isinstance(_section_local_chapter_lists, dict) else {}

                # _chapter_objects index 매핑: section 0 chapter 다음 section N chapters
                _section_0_ch_count_a = sum(
                    1 for ch in (_chapter_objects or [])
                    if isinstance(ch, dict) and ch.get("section_id", 0) == 0
                )
                for _local_ch_idx, _info in _section_n_local_dict.items():
                    _global_ch_idx = _section_0_ch_count_a + _local_ch_idx
                    _chapter_local_exemplars[_global_ch_idx] = _info

                log.info(
                    f"13.7b §4 chapter_local_exemplars: "
                    f"section_N chapters with local exemplars = {len(_chapter_local_exemplars)} "
                    f"(section_0 chapter count: {_section_0_ch_count_a})"
                )
            except Exception as _cle_e:
                log.warning(
                    f"13.7b §4 chapter_local_exemplars build 실패: {_cle_e}",
                    exc_info=True,
                )

            # 13.7a-A1: chapter_trees 파라미터 제거됨 (chapter object 안으로 흡수).
            # 13.7b section-local generation-lite + §4 chapter-local exemplars
            result = assemble_hwpx_hybrid(
                template_path, structure, content_data,
                removed_indices=removed_indices, idx_map=idx_map,
                content_only_mode=True,
                preserve_indices=_chapter_preserve,
                analyzed_sections=_analyzed_section_ids,
                chapter_local_exemplars=_chapter_local_exemplars,

                emphasis_layers=_emphasis_layers_by_cluster,
                paragraph_emphasis_map=_paragraph_emphasis_map,
                toc_replacements=_toc_replacements,
                toc_paragraph_idx=_tpl_toc_idx,
            )

            log.info(f"조립 완료: 성공 {result.success_count}, 실패 {result.fail_count}")

            # 최종 덤프 — 2b 결과 + 조립 결과 포함
            _debug_payload["section_fill"] = _section_fill_debug
            _debug_payload["final_content"] = {
                "header": header_data,
                "body_items_count": len(body_items),
                "body_items": body_items,
            }
            _debug_payload["assembly"] = {
                "success_count": result.success_count,
                "fail_count": result.fail_count,
                "errors": result.errors if result.errors else [],
                "output_size": len(result.data),
                "marker_rewrite_log": structure.get("_marker_rewrite_log", []),
                "rewrite_alignment": structure.get("_rewrite_alignment", {}),
                "phase2_reattach_result": structure.get("_phase2_reattach_result"),
                "section_info": structure.get("_section_info"),
            }

        # ── 12.2: Target Unit Planning Debug ──
        try:
            _tup_cached = structure.get("target_unit_plan")
            if is_plan_cache_valid(_tup_cached):
                _tup_plan = _tup_cached
                _tup_cache_hit = True
                _tup_ai_info = {"attempts": 0, "success": True, "error": None}
                log.info(f"[TARGET-PLAN] cache HIT: {len(_tup_cached.get('regions', []))} regions")
            else:
                _tup_cache_hit = False
                _tup_unit_obs = structure.get("template_unit_observation", {}).get("unit_observations", [])
                _tup_proposal = propose_template_regions(structure, _cached if '_cached' in dir() else None, _tup_unit_obs)
                _tup_msgs = build_target_unit_planning_prompt(_tup_proposal, structure.get("paragraphs", []), _tup_unit_obs)
                _tup_parsed = None
                _tup_error = None
                _tup_attempts = 0
                try:
                    _tup_attempts = 1
                    _tup_raw = await _call_llm(_tup_msgs, "hwpx_target_unit_planning")
                    _tup_parsed = parse_target_unit_plan_from_llm(_tup_raw)
                    if _tup_parsed is None:
                        _tup_attempts = 2
                        _tup_raw = await _call_llm(_tup_msgs, "hwpx_target_unit_planning_retry")
                        _tup_parsed = parse_target_unit_plan_from_llm(_tup_raw)
                except Exception as _tup_e:
                    _tup_error = str(_tup_e)
                    if _tup_attempts < 2:
                        try:
                            _tup_attempts = 2
                            _tup_raw = await _call_llm(_tup_msgs, "hwpx_target_unit_planning_retry")
                            _tup_parsed = parse_target_unit_plan_from_llm(_tup_raw)
                            _tup_error = None
                        except Exception as _tup_e2:
                            _tup_error = str(_tup_e2)

                _tup_ai_info = {"attempts": _tup_attempts, "success": _tup_parsed is not None, "error": _tup_error}

                if _tup_parsed:
                    _tup_val = validate_target_unit_plan(_tup_parsed, structure.get("paragraphs", []), _tup_unit_obs)
                    _tup_plan = build_plan_cache_payload(_tup_parsed, _tup_val)
                else:
                    _tup_val = {"valid": False, "blockers": ["ai_call_or_parse_failed"], "warnings": [], "all_paragraphs_covered": False, "no_overlap": True, "granularity_checks": {}}
                    _tup_plan = {"planner_version": CURRENT_PLANNER_VERSION, "regions": [], "planning_notes": [], "ambiguity_flags": [], "validation": _tup_val}
                    _tup_parsed = {"regions": [], "planning_notes": [], "ambiguity_flags": []}

                # Cache write-back
                structure["target_unit_plan"] = _tup_plan
                try:
                    _wb_cache2 = load_template_cache(_cache_key, namespace='full')
                    if _wb_cache2 and "structure" in _wb_cache2:
                        _wb_cache2["structure"]["target_unit_plan"] = _tup_plan
                        save_template_cache(_cache_key, _wb_cache2)
                        log.info(f"[TARGET-PLAN] cache write-back OK")
                except Exception as _wb2_e:
                    log.warning(f"[TARGET-PLAN] cache write-back 실패: {_wb2_e}")

                log.info(f"[TARGET-PLAN] AI: {len(_tup_parsed.get('regions', []))} regions, valid={_tup_val.get('valid')}")

            # Pipeline fit context
            _tup_pipeline_ctx = None
            if '_source_split_log' in dir() and _source_split_log and isinstance(_source_split_log, dict):
                _tup_pipeline_ctx = {
                    "chapter_count": len(chapters) if 'chapters' in dir() else 0,
                    "source_concentration_ratio": _source_split_log.get("source_concentration_ratio"),
                }
            elif 'chapters' in dir() and chapters:
                _tup_pipeline_ctx = {"chapter_count": len(chapters), "source_concentration_ratio": None}

            _tup_legacy = compute_legacy_comparison(_tup_plan, _tup_pipeline_ctx)

            # Derived mode label (debug context only)
            _tup_mode_label = ""
            _tuo_obs = structure.get("template_unit_observation", {})
            if _tuo_obs:
                _tup_mode_label = _tuo_obs.get("derived_mode_label", {}).get("label", "")

            _debug_payload["target_unit_planning"] = assemble_planning_debug(
                proposal=_tup_proposal if not _tup_cache_hit else {},
                ai_plan=_tup_parsed if not _tup_cache_hit else _tup_plan,
                validation=_tup_plan.get("validation", {}),
                legacy_comparison=_tup_legacy,
                unit_observations=_tuo_obs.get("unit_observations", []) if _tuo_obs else [],
                derived_mode_label=_tup_mode_label,
                paragraph_count=len(structure.get("paragraphs", [])),
                cache_status={"plan_cache_hit": _tup_cache_hit, "plan_cache_written": not _tup_cache_hit, "planner_version_matched": _tup_cache_hit},
                ai_call_info=_tup_ai_info,
            )
        except Exception as _tup_exc:
            log.warning(f"[TARGET-PLAN] 실패 (pipeline 영향 없음): {_tup_exc}")
            _debug_payload["target_unit_planning"] = {"error": str(_tup_exc), "debug_only": True}

        # ── 12.1 Phase 1: Marker Roundtrip Readiness ──
        try:
            _mrt_rewrite_log = structure.get("_marker_rewrite_log", [])
            from open_webui.utils.hwpx_analyzer import extract_marker_policies
            _mrt_policies = extract_marker_policies(
                structure.get("paragraphs", []),
                marker_policy_1f=structure.get("marker_policy_1f"),
            )
            _mrt_derived = ""
            _tuo_cached_obs = structure.get("template_unit_observation")
            if _tuo_cached_obs:
                _mrt_derived = _tuo_cached_obs.get("derived_mode_label", {}).get("label", "")
            # Phase 2 reattach debug output
            _p2r = structure.get("_phase2_reattach_result")
            if _p2r:
                _mr_log = structure.get("_marker_rewrite_log", [])
                _chapter_title_rewrites = sum(1 for r in _mr_log if r.get("is_chapter_title") and r.get("rewrite_applied"))
                _body_rewrites = sum(1 for r in _mr_log if not r.get("is_chapter_title") and r.get("rewrite_applied"))
                _debug_payload["marker_roundtrip_readiness"] = {
                    "schema_version": 2,
                    "phase": "content_only_reattach",
                    "content_only_mode": True,
                    "reattach_applied_count": len(body_items) - _chapter_title_rewrites,
                    "ai_marker_residual_count": _p2r.get("ai_marker_residual_count", 0),
                    "rewrite_conflict_count": _p2r.get("rewrite_conflict_count", 0),
                    "rewrite_conflicts": _p2r.get("rewrite_conflicts", []),
                    "chapter_title_rewrite_count": _chapter_title_rewrites,
                    "body_rewrite_conflict_count": _body_rewrites,
                    "normalization_applied_count": sum(1 for r in _mr_log if not r.get("is_chapter_title") and r.get("skip_reason") != "star_depth"),
                }
            else:
                _debug_payload["marker_roundtrip_readiness"] = build_marker_roundtrip_debug(
                body_items=body_items,
                marker_policies=_mrt_policies,
                marker_rewrite_log=_mrt_rewrite_log,
                derived_mode_label=_mrt_derived,
            )
            _mrt_summary = _debug_payload["marker_roundtrip_readiness"].get("summary", {})
            log.info(
                f"[MARKER-ROUNDTRIP] applicable={_mrt_summary.get('applicable_items')}, "
                f"content_preserved={_mrt_summary.get('content_preservation_rate')}, "
                f"policy_correct={_mrt_summary.get('policy_marker_correctness_rate')}"
            )
        except Exception as _mrt_e:
            log.warning(f"[MARKER-ROUNDTRIP] 실패 (pipeline 영향 없음): {_mrt_e}")

        # ── 1j Style Profile Observation (DISABLED for speed) ──
        try:
            pass  # Style profile AI calls disabled to reduce latency
        except Exception as _sp_e:
            log.warning(f"[STYLE-PROFILE] 실패 (pipeline 영향 없음): {_sp_e}")


        # ══════════════════════════════════════════════════════════════
        # 12.0: Template Unit Observation (debug-only)
        # ══════════════════════════════════════════════════════════════
        try:
            _tuo_cached = structure.get("template_unit_observation")
            if is_cache_valid(_tuo_cached):
                _tuo_cache_hit = True
                _tuo_ai_info = {"attempts": 0, "success": True, "error": None, "raw_output_length": 0}
                _tuo_features = _tuo_cached.get("features_snapshot", {})
                _tuo_label = _tuo_cached.get("derived_mode_label", {})
                _tuo_val = _tuo_cached.get("validation_result", {"valid": True, "blockers": [], "warnings": [], "confidence_downgrade": False})
                _tuo_parsed = {
                    "unit_observations": _tuo_cached.get("unit_observations", []),
                    "not_assessed_units": _tuo_cached.get("not_assessed_units", []),
                    "cross_unit_concerns": _tuo_cached.get("cross_unit_concerns", []),
                    "ambiguity_flags": _tuo_cached.get("ambiguity_flags", []),
                }
                log.info(f"[TEMPLATE-OBS] cache HIT: label={_tuo_label.get('label')}")
            else:
                _tuo_cache_hit = False
                _tuo_features = extract_template_unit_features(structure, _cached if '_cached' in dir() else None)
                _tuo_msgs = build_template_unit_prompt(_tuo_features)
                _tuo_attempts = 0
                _tuo_parsed = None
                _tuo_error = None
                _tuo_raw = ""
                try:
                    _tuo_attempts = 1
                    _tuo_raw = await _call_llm(_tuo_msgs, "hwpx_template_unit_observation")
                    _tuo_parsed = parse_template_unit_observation_from_llm(_tuo_raw)
                    if _tuo_parsed is None:
                        _tuo_attempts = 2
                        _tuo_raw = await _call_llm(_tuo_msgs, "hwpx_template_unit_observation_retry")
                        _tuo_parsed = parse_template_unit_observation_from_llm(_tuo_raw)
                except Exception as _tuo_e:
                    _tuo_error = str(_tuo_e)
                    if _tuo_attempts < 2:
                        try:
                            _tuo_attempts = 2
                            _tuo_raw = await _call_llm(_tuo_msgs, "hwpx_template_unit_observation_retry")
                            _tuo_parsed = parse_template_unit_observation_from_llm(_tuo_raw)
                            _tuo_error = None
                        except Exception as _tuo_e2:
                            _tuo_error = str(_tuo_e2)

                _tuo_ai_info = {
                    "attempts": _tuo_attempts,
                    "success": _tuo_parsed is not None,
                    "error": _tuo_error,
                    "raw_output_length": len(_tuo_raw) if _tuo_raw else 0,
                }

                if _tuo_parsed:
                    _tuo_val = validate_unit_observation(_tuo_features, _tuo_parsed)
                    if _tuo_val["blockers"]:
                        _tuo_parsed["unit_observations"] = []
                        _tuo_label = derive_mode_label([])
                        _tuo_label["confidence_level"] = "undetermined"
                    else:
                        _tuo_label = derive_mode_label(_tuo_parsed["unit_observations"])
                        if _tuo_val["confidence_downgrade"] and _tuo_label["confidence_level"] == "high":
                            _tuo_label["confidence_level"] = "medium"
                else:
                    _tuo_val = {"valid": False, "blockers": ["ai_call_or_parse_failed"], "warnings": [], "confidence_downgrade": False}
                    _tuo_label = derive_mode_label([])
                    _tuo_label["confidence_level"] = "undetermined"
                    _tuo_parsed = {"unit_observations": [], "not_assessed_units": [{"unit_type": "all", "reason": _tuo_error or "parse_failed"}], "cross_unit_concerns": [], "ambiguity_flags": []}

                structure["template_unit_observation"] = build_cache_payload(_tuo_parsed, _tuo_label, _tuo_val, _tuo_features)
                # Write-back to cache file (structure cache에 observation 추가)
                try:
                    _wb_cache = load_template_cache(_cache_key, namespace='full')
                    if _wb_cache and "structure" in _wb_cache:
                        _wb_cache["structure"]["template_unit_observation"] = structure["template_unit_observation"]
                        save_template_cache(_cache_key, _wb_cache)
                        log.info(f"[TEMPLATE-OBS] cache write-back OK → {_cache_key}")
                    else:
                        log.warning(f"[TEMPLATE-OBS] cache write-back skip: no cache for {_cache_key}")
                except Exception as _wb_e:
                    log.warning(f"[TEMPLATE-OBS] cache write-back 실패: {_wb_e}")
                log.info(f"[TEMPLATE-OBS] AI: label={_tuo_label.get('label')}, attempts={_tuo_attempts}")

            # Pipeline fit
            _tuo_pipeline_ctx = None
            if '_source_split_log' in dir() and _source_split_log and isinstance(_source_split_log, dict):
                _tuo_pipeline_ctx = {
                    "chapter_count": len(chapters) if 'chapters' in dir() else 0,
                    "source_concentration_ratio": _source_split_log.get("source_concentration_ratio"),
                    "underfill_candidates": _source_split_log.get("underfill_chapters", []),
                    "overfill_candidates": [],
                }
            elif 'chapters' in dir() and chapters:
                _tuo_pipeline_ctx = {
                    "chapter_count": len(chapters),
                    "source_concentration_ratio": None,
                    "underfill_candidates": [],
                    "overfill_candidates": [],
                }
            _tuo_pfit = compute_pipeline_fit(_tuo_label, (_tuo_parsed or {}).get("unit_observations", []), _tuo_pipeline_ctx)

            _tuo_fallback = None
            if not _tuo_ai_info["success"]:
                _tuo_fallback = "ai_call_failed" if _tuo_ai_info.get("error") else "parse_failed"
            elif _tuo_val.get("blockers"):
                _tuo_fallback = "validation_blocker"

            _debug_payload["template_unit_observation"] = assemble_observation_output(
                features=_tuo_features,
                ai_observation=_tuo_parsed,
                validation_result=_tuo_val,
                derived_label=_tuo_label,
                pipeline_fit=_tuo_pfit,
                cache_status={
                    "observation_cache_hit": _tuo_cache_hit,
                    "observation_cache_written": not _tuo_cache_hit,
                    "observer_version_matched": _tuo_cache_hit,
                    "cache_warning": None,
                },
                ai_call_info=_tuo_ai_info,
                fallback_reason=_tuo_fallback,
            )
        except Exception as _tuo_exc:
            log.warning(f"[TEMPLATE-OBS] 실패 (pipeline 영향 없음): {_tuo_exc}")
            _debug_payload["template_unit_observation"] = {"error": str(_tuo_exc), "debug_only": True}

        try:
            with open(_dump_path, "w", encoding="utf-8") as _f:
                json.dump(_debug_payload, _f, ensure_ascii=False, indent=2, default=str)
            write_stage_debug_files(_debug_payload)
            log.info(f"[DEBUG-HWPX] 최종 덤프 저장 (2b+조립 포함): {_dump_path}")
        except Exception as _e:
            log.warning(f"[DEBUG-HWPX] 최종 덤프 실패: {_e}")

        step_content = (
            f"성공 {result.success_count}개, 실패 {result.fail_count}개, "
            f"크기 {len(result.data):,} bytes"
            + (f"\n\n**오류:**\n" + "\n".join(f"- {e}" for e in result.errors) if result.errors else "")
        )

        if debug:
            try:
                from hwpx import HwpxDocument
                from lxml import etree
                debug_doc = HwpxDocument.open(io.BytesIO(result.data))
                section_elem = debug_doc.paragraphs[0].element.getparent()
                raw_xml = etree.tostring(section_elem, encoding="unicode", pretty_print=True)
                preview = raw_xml[:30000]
                if len(raw_xml) > 30000:
                    preview += "\n... (잘림)"
                step_content += (
                    f"\n\n<details><summary>결과 XML ({len(raw_xml):,}자)</summary>\n\n"
                    f"```xml\n{preview}\n```\n</details>"
                )
            except Exception as e:
                step_content += f"\n\nXML 덤프 실패: {e}"

        _debug_add("Step 5: HWPX 생성 결과", step_content)

        return result.data, debug_log
