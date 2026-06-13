# HWP 보고서 자동생성 — 양식 기반 동적 본문 생성 파이프라인

전북특별자치도 캡스톤 디자인 프로젝트 (2026)

본 코드는 어떤 HWPX 양식이든 입력받아 AI가 양식의 구조·서식·표기 규칙을 분석한 후, 같은 형식을 유지하면서 새 내용을 채워 새 HWPX 문서를 생성한다. 양식별 하드코딩 없이 동작하는 것이 목표다.

본 README는 코드북을 겸한다. 평가자가 코드를 읽기 전에 이 문서를 한 번 통독하면 전체 구조와 책임 분리를 파악할 수 있도록 작성했다.

---

## 1. 프로젝트 개요

### 목표
- 양식 + 소스 파일을 입력하면, 양식 구조를 정확히 파악해 소스 내용을 채워 동일 서식의 새 HWPX 문서 생성
- 특정 양식에 과적합되지 않음 — 마커·역할·계층 등을 동적으로 학습
- 양식은 동적으로 늘어나거나 줄어들 수 있어야 함

### 입력 / 출력
- **입력 1 (양식)**: `.hwpx` (한글 문서 양식). 빈 양식 또는 예시가 들어찬 양식 모두 지원.
- **입력 2 (소스)**: `.hwpx` 또는 `.pdf`. 본문 내용 원천. 문자 추출이 가능한 형식이면 확장 가능.
- **출력**: `.hwpx` (양식과 동일한 구조·서식 + 소스 내용)

### 핵심 차별점
| 항목 | 접근 |
|---|---|
| 양식 일반화 | 마커·챕터·말투·강조를 AI가 동적으로 분석 → 양식별 하드코딩 X |
| AI vs 코드 분리 | AI는 분석·내용 생성만 / XML 조립은 100% 코드 → 파일 무결성 보장 |
| 양식 캐시 | 같은 양식은 한 번만 분석 → 재사용 시 분석 단계 전부 건너뜀 |
| 단일 책임 | 한 단계 = 한 결정. 16단계로 분해하여 단계별 오류 차단 |

---

## 2. 전체 아키텍처

사용자가 채팅창에 양식+소스를 첨부하면, 채팅 LLM이 본 도구(`Tools.generate_document`)를 호출한다. 도구는 다음 16단계를 순차/병렬로 오케스트레이션한다.

### 16단계 파이프라인 (실행 순서)

```
[분석] 양식 hash 기반 캐시 — 처음 1회만 실행, 이후 재사용
  1a  AI    paragraph 구조 분석 — 모양으로 그룹 + 그룹별 역할 추측
  1b  AI    paragraph별 역할 후보
  1c  AI    깊이(level) + 부모(parent) 결정
  1d  AI    차례(TOC) 기반 챕터 단위 식별
  1e  AI    같은 구조 단락 묶음 (canonical cluster)
  1f  AI    묶음 재점검 (cluster repair)
  1g  AI    전체 트리 재구성 (tree rebuild)
  1h  AI    묶음별 표기 규칙 (①·가.·로마자) 추출
  1i  AI    챕터 안 반복 패턴 식별 (chapter pattern family)
  1j  AI    묶음별 말투·술어 패턴 분석
  1k  AI    묶음별 강조 layer + 강조 예산

[생성] 양식 + 소스마다 매번 실행
  2a  AI    챕터 제목 다시쓰기 + 표지 슬롯 확정
  2b  AI    챕터별 소스 구간 분배 (중복 허용)
  2c  AI    챕터별 본문 골격 (말투·강조·마커 X)
  2d  AI    챕터별 본문 말투·술어 정제
  2e  AI    챕터별 마커·강조 markup 부착

(조립) 코드  XML 조립 + HWPX 파일 출력 (assemble_hwpx_hybrid, 번호 없음)
```

### 16단계로 분해한 이유

한 번에 전체를 처리하면 구조를 잘못 읽거나 내용을 엉뚱한 위치에 넣는 문제가 발생한다. 이를 막기 위해 **단일 책임 원칙**을 적용해 한 단계가 두 가지 일을 동시에 하지 않도록 분해했다. 앞 단계는 양식을 처음부터 읽어야 하므로 책임 범위가 넓고, 뒤로 갈수록 앞에서 정리된 결과 위에서 결정 하나씩만 내리도록 책임을 좁혔다.

### 호출 수 / 비용

- 분석 단계: 약 11회 LLM 호출 (cluster batch 포함 시 더 증가)
- 생성 단계: 2a 1회 + 2b 1회 + 챕터 수 × 3 (2c/2d/2e)
- 양식 캐시 적용 시 분석 단계 0회 → 생성 단계만 실행

분석 단계 중 독립적인 1j (말투) + 1k (강조)는 cluster별 배치로 병렬 처리한다. 생성 단계의 챕터별 2c/2d/2e loop도 `asyncio.gather`로 병렬 실행한다.

---

## 3. 파일 구조

```
hwp_codebook/
├── README.md              본 코드북
├── requirements.txt       의존성 목록
├── .gitignore
│
├── dbtool_source.py       오케스트레이터 — 채팅 도구 entry point
│
└── (utils 모듈)
    ├── hwpx_analyzer.py        분석 단계 (1a~1k) 본체
    ├── hwp_generator.py        조립 단계 (XML 조립)
    ├── target_unit_planner.py  1d 보조 (chapter region planning)
    ├── template_observer.py    1i 보조 (template chapter pattern)
    └── marker_separator.py     1h 보조 (marker / content 분리)
```

### 파일별 역할

| 파일 | 줄 수 | 역할 |
|---|---|---|
| `dbtool_source.py` | ~4,600 | **오케스트레이터**. 채팅 도구의 entry point (`Tools.generate_document` → `_generate_hybrid`). 1a → 1b → ... → 2e → 조립 순서 호출, 캐시 hit/miss 결정, 챕터 loop 병렬 처리, 결과 파일 저장. |
| `hwpx_analyzer.py` | ~17,600 | 분석 단계 본체. 양식 HWPX 파싱, 각 단계별 prompt builder + LLM 응답 parser, cache schema 정의, structure 검증. 생성 단계의 2c/2d/2e prompt도 여기에 정의. |
| `hwp_generator.py` | ~3,100 | 조립 단계. lxml로 양식 paragraph element를 `deepcopy` → 텍스트만 교체. 마커/강조 markup 적용. |
| `target_unit_planner.py` | ~600 | 1d 보조. 차례(TOC) → chapter region 매핑, generation unit 결정. |
| `template_observer.py` | ~860 | 1i 보조. 양식 반복 챕터 family 관측. |
| `marker_separator.py` | ~690 | 1h 보조. paragraph text에서 marker 부분과 content 부분 분리. |

---

## 4. 분석 단계 상세 (1a~1k)

분석은 **양식 한 번만 처리**한다. 결과는 `compute_template_hash` 기반 캐시에 저장되어 같은 양식 재사용 시 모든 단계 skip된다.

각 단계는 `hwpx_analyzer.py`의 `build_*_prompt` + LLM 호출 + `parse_*_from_llm` 패턴이다.

### 1a — Paragraph 구조 분석 (AI)
함수: `build_structure_analysis_prompt`. task: `hwpx_structure_analysis`.
양식 XML을 경량화해 AI에게 보낸다. paragraph마다 `paraPrIDRef`·`charPrIDRef` (= 시각적 "모양") 정보를 함께 줘서, **같은 모양 paragraph끼리는 같은 의미 역할이라는 가정**으로 AI가 1차 분류한다. 출력: `{idx, role, description, marker}` per paragraph.

### 1b — Role 후보 도출 (AI)
함수: `build_role_classification_prompt`. task: `hwpx_1b_role_candidates`.
1a에서 받은 paragraph마다 의미적 역할 후보를 multiple 추출. 한 paragraph가 여러 역할 후보를 가질 수 있다는 가정 (예: "표지·제목" vs "목차·제목"). 1e가 cluster 묶음을 결정하면서 후보 중 확정 — 이후 1f가 묶음 재점검, 1g가 트리 재구성.

### 1c — 깊이 + 부모 결정 (AI)
함수: `build_level_analysis_prompt`. task: `hwpx_1c_level`.
번호·소제목 패턴(I, 1, 가, ①, ...)으로 level + parent_idx를 결정. 양식의 트리 구조 완성.

### 1d — TOC 기반 챕터 단위 (AI)
함수: `build_toc_based_chapter_plan_prompt`. task: `hwpx_phase_e_toc_plan`.
양식이 차례(목차)를 가질 경우 그 차례를 1차 evidence로 삼아 generation_unit(챕터 단위)을 결정. 차례 없으면 deferred 처리 (이후 보조 AI 호출로 backup).

### 1e — 같은 구조 단락 묶음 (AI)
함수: `build_canonical_clustering_prompt`. task: `hwpx_canonical_clustering`.
1b의 role 후보 + 1c의 tree + 1d의 chapter 정보를 바탕으로 같은 구조 paragraph를 cluster로 그룹화. cluster_id 부여. **같은 marker라도 다른 chapter면 다른 cluster** (chapter-aware).

### 1f — 묶음 재점검 (AI)
함수: `build_canonical_clustering_repair_prompt`. task: `hwpx_canonical_clustering_repair`.
1e cluster 결과를 validator로 검증. issue 발견 시 AI에게 repair 요청. cluster 조정.

### 1g — 전체 트리 재구성 (AI)
함수: `build_tree_rebuild_prompt`. task: `hwpx_tree_rebuild`.
1e+1f가 확정한 cluster_id를 입력으로 받아 parent_idx + level을 재구성. 같은 cluster여도 local context (가장 가까운 heading, enumeration block)가 다르면 parent 다르게 부여. **cluster_id는 parent 판단의 절대 기준이 아니라는 점**을 명시적으로 학습.

### 1h — 묶음별 표기 규칙 (AI)
함수: `build_marker_policy_prompt`. task: `hwpx_1f_marker_policy`.
cluster별 마커 정책(①②, 가., 로마자 I/II, "•" 등) + `table_kind` (real_table / decorative_box) 결정. 결과는 `structure["marker_policy_1f"]` (코드 식별자에 옛 라벨 잔존).

### 1i — 챕터 안 반복 패턴 (AI)
함수: `build_chapter_pattern_family_prompt`. task: `hwpx_track_c_pattern_family`.
1d의 generation_unit (챕터)을 subtree로 묶어 AI에게 보내고, **같은 골격 반복** family를 식별 (예: "9개 과제", "5개 위반 사례"). family 멤버는 같은 `chapter_type`으로 묶임 → 2c 생성 시 같은 template 적용.

### 1j — 묶음별 말투·술어 (AI, cluster별 batch)
함수: `build_style_profile_prompt`. task: `hwpx_style_profile`.
cluster별 말투·술어·종결 패턴 분석. cluster를 여러 batch로 나눠 `asyncio.gather`로 병렬 호출. 결과는 별도 style cache (`<hash>_style.json`)에 저장.

### 1k — 묶음별 강조 layer + 예산 (AI, cluster별 batch)
함수: `build_emphasis_layer_prompt`. task: `hwpx_emphasis_layer`.
cluster별 강조 layer(굵게·기울임·색) 분석 + 양식 sample에서 강조 빈도 측정 → cluster별 "강조 예산" (한 paragraph당 강조 가능 token 비율) 산출. 1j와 함께 병렬 실행.

### 16단계 외 보조 AI 호출 + 코드 후처리

| task_name | 위치 | 역할 |
|---|---|---|
| `hwpx_target_unit_planning` | 1k 후 | 양식 region planning. shallow / chapter route 결정용. |
| `hwpx_template_unit_observation` | chapter route 안 | 양식 unit observation. |
| `hwpx_chapter_classify_shallow` | shallow 경로 | 2a 대체 (chapter route 진입 불가 시). |
| `hwpx_shallow_2b` | shallow 경로 | 2b~2e 대체 단일 호출 (per-chapter loop 없음). |
| `hwpx_13_7b_section_role_proposal` | multi-section | section 역할 제안 (debug-only). |
| `hwpx_13_7b_section_n_*` | multi-section | section 0 외 처리. |

코드 후처리 (AI 아님):
- 1c 후 — 형제 배타 규칙 (같은 부모 아래 동시 등장 불가 role 검출)
- 1h 전 — format/blank 규칙 관측 (들여쓰기·줄간격 통계)

---

## 5. 생성 단계 상세 (2a~2e) + 조립

생성은 **양식 + 소스마다 매번 실행**한다. 캐시 안 됨.

### 2a — 챕터 제목 + 표지 슬롯 (AI)
함수: `build_adaptation_plan_prompt` + `build_toc_replacement_prompt` + `extract_header_roles`.
task: `hwpx_13_7c_adaptation_plan` + `hwpx_toc_replacement`.
- adaptation_plan: 양식 챕터마다 소스의 어떤 부분이 들어갈지 + 적용 시 챕터 제목을 어떻게 바꿀지(`adapted_title`) 결정.
- toc_replacement: adapted_title을 차례 (TOC) paragraph text에도 반영.
- extract_header_roles (코드): 표지 영역의 슬롯 (document_title, subtitle 등) 식별.

### 2b — 챕터별 소스 구간 분배 (AI)
함수: `build_source_range_prompt`. task: `hwpx_2b_source_range`.
소스 텍스트 전체를 한 번에 받고, 챕터마다 어느 character range를 사용할지 결정. 한 호출로 모든 챕터 분배. **중복 허용** (한 소스 구간이 여러 챕터에 들어갈 수 있음).

### 2c — 챕터별 본문 골격 (AI, 챕터 loop 병렬)
함수: `build_section_fill_prompt`. task: `hwpx_section_fill_{ch_idx}`.
챕터마다 본문 item 리스트 생성. item에는 `{role, text}`만 들어가고 **마커·강조는 안 박힘**. 1h/1j/1k 결과를 prompt에 참조 정보로 전달 (강제는 안 함). 챕터 loop 전체를 `asyncio.gather`로 병렬 실행.

### 2d — 챕터별 본문 말투 정제 (AI, 챕터별)
함수: `build_section_polish_prompt`. task: `hwpx_section_polish_{ch_idx}`.
2c 결과를 받아 1j (style profile) 기준으로 말투·술어·종결을 양식과 일치시킨다. **구조(role/parent_idx)는 안 건드림, 텍스트만 다듬음**.

### 2e — 챕터별 마커·강조 markup 부착 (AI, 챕터별)
함수: `build_section_style_prompt` → `apply_section_style_to_items`. task: `hwpx_section_style_{ch_idx}`.
2d 결과 text에 마커(①, 가., ...)와 강조 markup(`[[em:1]]...[[/em]]`)을 입힌다. 1h (marker_policy)와 1k (emphasis_layer + 예산)를 prompt에 강제 반영. `process_section_fill_result` 함수 내부에서 호출.

### 조립 (코드, 번호 없음)
함수: `assemble_hwpx_hybrid` (hwp_generator.py:386). AI 호출 0.
lxml로 양식 HWPX를 열고:
1. cluster별 본보기(exemplar) paragraph element를 `deepcopy`
2. `_set_element_text`로 본문 text만 교체 (paraPrIDRef·charPrIDRef·style 그대로)
3. 마커/강조 markup이 있으면 segment 단위로 분할해 charPr 적용
4. 양식과 동일한 paragraph 순서로 재조립 + bytes 출력

코드 조립이라 AI가 구조를 잘못 잡아도 XML 자체는 깨지지 않는다.

---

## 6. 핵심 데이터 구조

### `structure` (분석 결과 컨테이너)

```python
{
  "paragraphs": [
    {"idx": int, "role": str, "level": int, "parent_idx": int,
     "marker": str, "cluster_id": int, "chapter_id": int, ...},
    ...
  ],
  "tables": [...],
  "chapter_types": {"<type_name>": {...}, ...},
  "template_grammar": {
    "per_type": {"<type_name>": {"grammar": {...}, "root_roles": [...]}},
    "global": {...}
  },
  "role_text_types": {"<role>": "<type>"},
  "marker_policy_1f": {"<role>": {"policy_type": ..., "markers": [...]}},
  "format_rules": {...},
  "per_type_role_semantics": {...},
  "target_unit_plan": {"regions": [...]},
  "template_unit_observation": {"unit_observations": [...]}
}
```

### `chapter_object` (생성 챕터 단위)

```python
{
  "ci": int,                    # chapter index
  "title_item": {"role": str, "text": str},
  "body_items": [{"role": str, "text": str}, ...],
  "chapter_tree_nodes": [...],
  "paragraph_indices": [...],   # 양식 paragraph idx 매핑
  "adaptation_decision": {...}, # 2a 결과 (adapted_title 등)
  "status": "ok" | "empty" | "fail",
  "section_id": int,
  ...
}
```

### Cache

```python
{
  "cache_schema_version": int,
  "structure": <structure dict>,
  "chapter_types": {...},
  "signals": {...},
  "idx_texts": {...},
  "marker_policy_1f": {...},
  "section_results": {...},
  "phase_e_chapter_planner": {...},   # 1d 결과 (코드 식별자에 옛 라벨 잔존)
  "chapter_pattern_family": {...}     # 1i 결과
}
```

별도 style cache (`<hash>_style.json`)에 1j (style_profiles) + 1k (emphasis_layers) 저장.

---

## 7. 캐시 전략

### 캐시 대상

| 단계 | 캐시 | 비고 |
|---|---|---|
| 1a~1k 분석 결과 | O | 양식 hash 기반 |
| 2a~2e 생성 결과 | X | 소스가 매번 다르므로 |
| 조립 (코드) | X | 즉시 실행 |

### 캐시 키
양식 파일의 SHA-1 hash 16자 (`compute_template_hash`). 양식 내용이 바뀌면 자동으로 새 캐시.

### 캐시 위치
- 메인: `/tmp/hwpx_cache/<hash16>.json`
- 스타일: `/tmp/hwpx_cache/<hash16>_style.json` (1j + 1k는 cluster id 의존성이 강해 별도 파일로 분리)

### 무효화
`CACHE_SCHEMA_VERSION` 상수를 bump하면 모든 캐시 미스 처리. 분석 로직 변경 시에만 bump.

---

## 8. AI vs 코드 책임 분리

본 파이프라인의 핵심 설계 원칙.

| 책임 | 담당 | 이유 |
|---|---|---|
| 양식 구조 인식 (1a~1c) | AI | 의미·맥락 판단 필요 |
| 챕터 단위 / family 식별 (1d, 1i) | AI | 차례·반복 패턴 의미 판단 |
| 묶음 / 트리 (1e~1g) | AI | 유사도·문맥 판단 |
| 마커·말투·강조 (1h, 1j, 1k) | AI | 자연어/시각 패턴 인식 |
| 형제 배타 / format 규칙 | 코드 (1c 후 / 1h 전) | 트리·통계 관측만으로 충분 |
| 챕터 매핑·내용 생성 (2a~2e) | AI | 자연어 작성 |
| **XML 조립** | **코드** | **파일 포맷 무결성 보장** |

핵심 효과: **AI가 hallucinate해도 출력 파일은 깨지지 않는다**. AI 출력은 데이터(JSON)일 뿐, 실제 HWPX 파일 조립은 결정론적 코드가 담당.

---

## 9. 실행 환경 / 의존성

### 언어 / 런타임
- Python 3.11+
- Linux (HWPX 라이브러리 호환성)

### 주요 의존성
- `lxml` — XML 파싱/직렬화
- `python-hwpx` — HWPX 문서 구조 라이브러리 (오픈소스, 본 repo 미포함)
- `python-hwplib` — HWP 파일 라이브러리 (jpype1 사용, 오픈소스, 본 repo 미포함)
- `pydantic` — `Tools.Valves` 설정
- Open WebUI `generate_chat_completion` — 내부 vLLM 호출 wrapper

자세한 버전은 `requirements.txt` 참고.

### LLM 모델
- **Qwen3.5-397B-A17B-FP8** — 내부 vLLM 추론 서버 (외부 API 사용 X)
- `Tools.Valves.AI_MODEL`로 다른 OpenAI 호환 모델 교체 가능

### 실행 기반
본 코드는 [Open WebUI](https://github.com/open-webui/open-webui) fork에 통합되어 동작. 채팅 UI에서 양식+소스 첨부 후 메시지 전송 → 채팅 LLM이 `Tools.generate_document` 호출 → 본 파이프라인 실행 → 생성된 HWPX 파일이 채팅 메시지에 첨부되어 다운로드 가능.

`dbtool_source.py`는 Open WebUI의 DB tool로 등록된 코드의 스냅샷이다.

---

## 10. 한계 및 향후 과제

### 현재 한계
- 첫 양식 분석은 LLM 호출이 많아 시간이 걸린다 — 같은 양식 재사용 시에는 캐시 hit으로 분석 단계 전체 skip
- LLM 호출이 챕터 수에 비례 — 대형 양식 처리 시 비용 증가

### 향후 과제
- Knowledge Base 연동 — 소스를 단일 파일이 아니라 KB collection으로 확장
- Source coverage validation — 생성된 본문이 소스를 충분히 반영하는지 자동 검증
- 표(table) 양식 동적 처리 확장
- 입력 포맷 다양화 (HWPX·PDF 외 DOCX 등 문자 추출 가능한 모든 포맷)
