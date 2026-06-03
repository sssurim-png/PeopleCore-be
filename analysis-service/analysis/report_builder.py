"""
보고서 빌더 — 분석 결과 dict → 마크다운 + 차트 spec.

목적:
  - HITL "보고서 형식" 응답 시 사용
  - 마크다운 본문 = 표·요약 (다운로드/저장용)
  - 차트 spec = FE 가 Chart.js / Recharts 로 렌더 (JSON 응답에 포함)
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, List, Optional


# ─── 도구별 기본 응답 형식 ───
# True = report (차트·문서) / False = short (5줄 텍스트)
DEFAULT_REPORT_TOOLS = {
    "I-01": True,   # 보상-성과 정합성 — 직급별 변별력 + 후보 명단
    "I-02": True,   # 우수인재 보상 누락 — 후보 명단
    "I-04": True,   # 부서별 등급 분포 — 부서 매트릭스
    "I-05": False,  # 평가자 분포 — 보통 1~2명 (small)
    "I-06": True,   # 사원 등급 변동 — 패턴 카운트 + 명단
    "I-07": True,   # 워라밸 — 부서별 라벨 + 지표
    "I-09": True,   # 산업안전 — 위반 부서·사원
    "I-12": True,   # 종합 보고서 — 항상 report
}


# ─── 결과 크기로 short/report 결정 ───
def decide_response_form(tool_id: str, result: Dict[str, Any]) -> str:
    """
    응답 형식 결정 (short / report).

    1단계: 도구 기본값 (DEFAULT_REPORT_TOOLS)
    2단계: 결과 작으면 (후보·항목 ≤ 5) short 로 강제 전환
    """
    base = DEFAULT_REPORT_TOOLS.get(tool_id, False)
    if not base:
        return "short"

    # report 기본인데 결과 작으면 short 로
    counts = (
        len(result.get("candidates", []))
        + len(result.get("depts", []))
        + len(result.get("employees", []))
        + len(result.get("violation_employees", []))
        + len(result.get("section_3_emp_candidates", []))
    )
    if counts <= 5 and tool_id != "I-12":
        return "short"

    return "report"


# ─── 마크다운 빌더 ───
def _table_md(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    """간단한 마크다운 표 생성."""
    if not rows:
        return "_(데이터 없음)_"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = "\n".join(
        "| " + " | ".join(str(r.get(c, "")) for c in columns) + " |"
        for r in rows
    )
    return f"{header}\n{sep}\n{body}"


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


def build_markdown(result: Dict[str, Any], narrative: Optional[str] = None) -> str:
    """분석 결과 → 마크다운 보고서."""
    indicator_id = result.get("indicator_id", "?")
    report_id = result.get("report_id", "?")
    title = _title_for(indicator_id)
    season = result.get("season", {})
    summary = result.get("summary", {})

    md = [
        f"# {report_id} {title}",
        "",
        f"- 시즌: {season.get('name', '미지정')} ({season.get('start_date', '')} ~ {season.get('end_date', '')})",
        f"- 생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]

    if summary.get("mode") == "INSUFFICIENT":
        md.append(f"\n> ⚠ {summary.get('skipped_reason', '분석 표본 부족')}")
        return "\n".join(md)

    # 도구별 본문
    if indicator_id == "I-12":
        md.append(_render_comprehensive(result))
    elif indicator_id == "I-02":
        md.append(_render_underpaid(result))
    elif indicator_id == "I-04":
        md.append(_render_dept_grade(result))
    elif indicator_id == "I-06":
        md.append(_render_volatility(result))
    elif indicator_id == "I-07":
        md.append(_render_workload(result))
    elif indicator_id == "I-09":
        md.append(_render_safety(result))
    elif indicator_id == "I-01":
        md.append(_render_grade_salary(result))
    elif indicator_id == "I-05":
        md.append(_render_evaluator(result))
    else:
        md.append(_section("결과", "```json\n" + str(result.get("summary", {})) + "\n```"))

    if narrative:
        md.append(_section("AI 참고", narrative))

    md.append("\n---\n_본 보고서는 분석 시스템 진단이며, 인사 결정의 근거가 아닙니다._")
    return "\n".join(md)


def _title_for(indicator_id: str) -> str:
    titles = {
        "I-01": "보상-성과 정합성 진단",
        "I-02": "우수인재 보상 누락 발굴",
        "I-04": "부서별 등급 분포 진단",
        "I-05": "평가자별 점수 분포 차이 진단",
        "I-06": "사원 등급 시계열 변동",
        "I-07": "부서별 워라밸 진단",
        "I-09": "산업안전 위험 부서 진단",
        "I-12": "전사 진단 종합 보고서",
    }
    return titles.get(indicator_id, indicator_id)


# ─── 도구별 마크다운 섹션 ───
def _render_underpaid(r: Dict[str, Any]) -> str:
    cands = r.get("candidates", [])
    body = _table_md(cands, ["emp_name", "dept_name", "grade_name",
                              "tenure_years", "grades_seq",
                              "season_pay", "salary_pctile"])
    return _section(f"후보 사원 ({len(cands)}명)", body)


def _render_dept_grade(r: Dict[str, Any]) -> str:
    depts = r.get("depts", [])
    body = _table_md(depts, ["dept_name", "n", "s_ratio", "a_ratio", "sa_ratio",
                              "delta_sa", "label"])
    return _section(f"부서 분포 ({len(depts)}개)", body)


def _render_volatility(r: Dict[str, Any]) -> str:
    emps = r.get("employees", [])
    pattern_counts = r.get("summary", {}).get("pattern_counts", {})
    summary_md = "\n".join(f"- **{k}**: {v}명" for k, v in pattern_counts.items())
    table = _table_md(emps[:30], ["emp_name", "dept_name", "n_seasons",
                                    "grades_seq", "pattern"])
    return _section("패턴 요약", summary_md) + _section(f"사원별 패턴 (상위 30명)", table)


def _render_workload(r: Dict[str, Any]) -> str:
    depts = r.get("depts", [])
    rows = []
    for d in depts:
        rows.append({
            "dept_name": d["dept_name"],
            "n": d["dept_n"],
            "야근 (분/월)": d["mean_overtime_per_emp"],
            "야간 (분/월)": d["mean_night_per_emp"],
            "휴일근무 비율": d["holiday_work_ratio"],
            "라벨": ", ".join(d.get("labels", [])),
        })
    body = _table_md(rows, ["dept_name", "n", "야근 (분/월)", "야간 (분/월)",
                             "휴일근무 비율", "라벨"])
    return _section(f"부서별 워라밸 ({len(depts)}개)", body)


def _render_safety(r: Dict[str, Any]) -> str:
    depts = r.get("depts", [])
    viol_emps = r.get("violation_employees", [])
    label_counts = r.get("summary", {}).get("label_counts", {})

    summary_md = "\n".join(f"- **{k}**: {v}개 부서" for k, v in label_counts.items())
    dept_body = _table_md(depts, ["dept_name", "dept_n", "violation_emp_count",
                                    "consec_violation_emp_count",
                                    "max_consecutive_days", "label"])
    emp_body = _table_md(
        [{"emp_id": e["emp_id"], "violation_types": ", ".join(e.get("violation_types", [])),
          "max_weekly_hours": e["max_weekly_hours"],
          "max_consecutive_days": e["max_consecutive_days"]}
         for e in viol_emps[:30]],
        ["emp_id", "violation_types", "max_weekly_hours", "max_consecutive_days"],
    )
    return (
        _section("부서별 라벨", summary_md) +
        _section(f"부서별 상세 ({len(depts)}개)", dept_body) +
        _section(f"위반 사원 명단 (상위 30명)", emp_body)
    )


def _render_grade_salary(r: Dict[str, Any]) -> str:
    detail1 = r.get("detail1_short_term_discrimination", [])
    cands = r.get("detail3_unfair_candidates", [])
    body1 = _table_md(detail1, ["grade_name", "n", "mode", "corr_grade_bonus",
                                  "label_short_term"])
    body3 = _table_md(cands, ["emp_name", "dept_name", "grade_name",
                                "tenure_years", "years_since_last_promotion",
                                "grades_seq", "salary_pctile", "case"])
    return (
        _section("세부 1 — 단기 변별력 (직급별)", body1) +
        _section(f"세부 3 — 부당 보상 후보 ({len(cands)}명)", body3)
    )


def _render_evaluator(r: Dict[str, Any]) -> str:
    cands = r.get("candidates", [])
    summary = r.get("summary", {})
    body = (
        f"- 모드: **{summary.get('mode', '?')}** (평가자 {summary.get('n_evaluators', 0)}명)\n"
        f"- 후보 평가자: **{summary.get('n_candidates', 0)}명**\n"
    )
    table = _table_md(cands, ["evaluator_name", "dept_name", "n_evaluatees",
                                "mean_score", "sa_ratio", "stdev_score",
                                "mean_z", "flag_reasons"])
    return _section("요약", body) + _section("후보 평가자", table)


def _classify_label_severity(text: str) -> str:
    """라벨 텍스트 → 정상 / 주의 / 위험 카테고리."""
    if not text or text == "데이터 없음":
        return "데이터 없음"
    t = str(text)
    if "위반" in t or "법규" in t or "심각" in t:
        return "위험"
    if "통상" in t or "균형" in t or "정상" in t:
        return "정상"
    return "주의"


def _render_comprehensive(r: Dict[str, Any]) -> str:
    overview = r.get("summary_overview", {})
    matrix = r.get("section_2_dept_matrix", [])
    candidates = r.get("section_3_emp_candidates", [])
    priorities = r.get("section_5_priorities", [])

    # §1 전사 요약 — 핵심 지표만 카드형으로
    season = overview.get("season", {}) or {}
    underpaid_n = overview.get("underpaid_count", 0)
    unfair_n = overview.get("unfair_count", 0)
    safety_sum = overview.get("safety", {}) or {}
    eval_sum = overview.get("evaluator_pattern", {}) or {}
    strong_n = sum(1 for c in candidates if c.get("strength") == "강함")
    medium_n = sum(1 for c in candidates if c.get("strength") == "중간")
    single_n = sum(1 for c in candidates if c.get("strength") == "단일")

    overview_md = (
        f"- **분석 시즌**: {season.get('name', '미지정')}\n"
        f"- **보상 누락 후보**: {underpaid_n}명\n"
        f"- **부당 보상 후보**: {unfair_n}명\n"
        f"- **법규 위반 사원**: {safety_sum.get('total_violation_emps', 0)}명\n"
        f"- **평가자 이상치**: {eval_sum.get('n_candidates', 0)}명 / 전체 {eval_sum.get('n_evaluators', 0)}명\n"
        f"- **결합 강도 분포**: 강함 {strong_n} · 중간 {medium_n} · 단일 {single_n}명"
    )

    # §2 부서 매트릭스 — 한국어 헤더로 표 다시 짬
    matrix_rows = [
        {
            "부서": d.get("dept_name", "?"),
            "등급 분포": d.get("label_grade_dist", "-"),
            "워라밸": d.get("label_workload", "-"),
            "산업안전": d.get("label_safety", "-"),
        }
        for d in matrix
    ]
    matrix_body = _table_md(matrix_rows, ["부서", "등급 분포", "워라밸", "산업안전"])

    # §3 사원 후보 — signals 리스트 → 보기 좋게 join
    cand_rows = [
        {
            "사원": c.get("emp_name", "?"),
            "부서": c.get("dept_name", "?"),
            "직급": c.get("grade_name", "?"),
            "동시 적발 신호": ", ".join(c.get("signals", [])),
            "결합 강도": c.get("strength", "-"),
        }
        for c in candidates
    ]
    cand_body = _table_md(
        cand_rows, ["사원", "부서", "직급", "동시 적발 신호", "결합 강도"]
    )

    # §5 우선순위 — 레벨별 박스
    if priorities:
        priority_md = "\n\n".join(
            f"**[우선순위 {p['level']}] {p['category']}**\n> {p['summary']}"
            for p in priorities
        )
    else:
        priority_md = "_(우선 안건 없음)_"

    return (
        _section("§1. 전사 요약", overview_md) +
        _section(f"§2. 부서별 진단 매트릭스 ({len(matrix)}개 부서)", matrix_body) +
        _section(f"§3. 사원 후보 통합 ({len(candidates)}명)", cand_body) +
        _section("§5. HR 검토 우선순위", priority_md)
    )


# ─── 차트 spec 빌더 ───
# Chart.js / Recharts 호환 형식
#
# mode:
#   "preview" — HITL 직전 미리보기. I-12 종합은 글 위주 (차트 X).
#   "full"    — 보고서 생성 후 전체 시각화 (차트 풀세트).
# 다른 지표(I-04, I-06 등)는 mode 무관하게 항상 동일 차트.
def build_chart_specs(
    result: Dict[str, Any],
    mode: str = "full",
) -> List[Dict[str, Any]]:
    """결과 dict → 차트 spec 리스트."""
    indicator_id = result.get("indicator_id")
    if result.get("summary", {}).get("mode") == "INSUFFICIENT":
        return []

    if indicator_id == "I-04":
        depts = result.get("depts", [])
        company_sa = result.get("company", {}).get("sa_ratio", 0)
        return [{
            "type": "bar",
            "title": "부서별 S+A 비율 vs 전사 평균",
            "subtitle": f"전사 평균 {company_sa:.1f}% — 막대가 평균선과 멀수록 분포 편중",
            "x_label": "부서",
            "y_label": "S+A 비율 (%)",
            "datasets": [
                {
                    "label": "부서 S+A 비율",
                    "data": [{"x": d["dept_name"], "y": d["sa_ratio"]} for d in depts],
                },
                {
                    "label": "전사 평균",
                    "type": "line",
                    "data": [{"x": d["dept_name"], "y": company_sa} for d in depts],
                },
            ],
        }]

    if indicator_id == "I-07":
        depts = result.get("depts", [])
        return [{
            "type": "bar",
            "title": "부서별 1인당 야근 시간",
            "subtitle": "월간 1인당 야근 분 — 막대가 길수록 야근 부담 큼",
            "x_label": "부서",
            "y_label": "분/월",
            "datasets": [
                {
                    "label": "야근 분",
                    "data": [{"x": d["dept_name"], "y": d["mean_overtime_per_emp"]}
                             for d in depts],
                }
            ],
        }, {
            "type": "bar",
            "title": "부서별 미인정 초과 비율",
            "subtitle": "초과근무 신청 없이 정시 외 근무한 비율 — 시스템 외 근무 의심",
            "x_label": "부서",
            "y_label": "%",
            "datasets": [
                {
                    "label": "미인정 초과",
                    "data": [{"x": d["dept_name"], "y": d["unrecognized_ot_ratio"]}
                             for d in depts],
                }
            ],
        }]

    if indicator_id == "I-06":
        pattern_counts = result.get("summary", {}).get("pattern_counts", {})
        total = sum(pattern_counts.values()) or 1
        return [{
            "type": "doughnut",
            "title": "사원 등급 변동 패턴 분포",
            "subtitle": f"전체 {total}명 — 패턴별 사원 수",
            "datasets": [{
                "data": [{"label": k, "value": v} for k, v in pattern_counts.items()]
            }],
        }]

    if indicator_id == "I-09":
        depts = result.get("depts", [])
        return [{
            "type": "bar",
            "title": "부서별 법규 위반 사원 수",
            "subtitle": "주 52h 또는 연속근무 위반 — 0명이면 정상",
            "x_label": "부서",
            "y_label": "명",
            "datasets": [
                {
                    "label": "52h 위반",
                    "data": [{"x": d["dept_name"], "y": d["violation_emp_count"]}
                             for d in depts],
                },
                {
                    "label": "연속근무 위반",
                    "data": [{"x": d["dept_name"], "y": d["consec_violation_emp_count"]}
                             for d in depts],
                },
            ],
        }]

    if indicator_id == "I-12":
        # preview = 글 위주 (차트 X)
        if mode == "preview":
            return []
        # full = 차트 풀세트
        return _build_comprehensive_charts(result)

    return []


# ─── I-12 종합 보고서 — 차트 풀세트 ───
_PALETTE = {
    "정상": "#10B981",   # green
    "주의": "#F59E0B",   # amber
    "위험": "#EF4444",   # red
    "데이터 없음": "#9CA3AF",  # gray
    "강함": "#DC2626",
    "중간": "#F59E0B",
    "단일": "#3B82F6",
}


def _build_comprehensive_charts(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """I-12 종합 보고서용 차트 4종."""
    charts: List[Dict[str, Any]] = []
    matrix = result.get("section_2_dept_matrix", [])
    candidates = result.get("section_3_emp_candidates", [])
    priorities = result.get("section_5_priorities", [])
    overview = result.get("summary_overview", {})

    # 1) 사원별 동시 적발 위험신호 분포 (도넛)
    if candidates:
        cand_strength: Dict[str, int] = {}
        for c in candidates:
            s = c["strength"]
            cand_strength[s] = cand_strength.get(s, 0) + 1
        label_map = {
            "강함": "위험신호 3개 (즉시 면담)",
            "중간": "위험신호 2개 (검토 안건)",
            "단일": "위험신호 1개 (관찰 대상)",
        }
        order = {"강함": 0, "중간": 1, "단일": 2}
        items = sorted(cand_strength.items(), key=lambda kv: order.get(kv[0], 99))
        charts.append({
            "type": "doughnut",
            "title": "사원별 동시 적발된 위험신호 개수 분포",
            "subtitle": f"전체 {len(candidates)}명 — 강함 우선 면담",
            "datasets": [{
                "data": [
                    {
                        "label": label_map.get(k, k),
                        "value": v,
                        "color": _PALETTE.get(k, "#6B7280"),
                    }
                    for k, v in items
                ]
            }],
        })

    # 2) 부서별 진단 매트릭스 — 카테고리별 정상/주의/위험 (스택 막대)
    if matrix:
        categories = [
            ("등급 분포", "label_grade_dist"),
            ("워라밸", "label_workload"),
            ("산업안전", "label_safety"),
        ]
        severity_levels = ["정상", "주의", "위험", "데이터 없음"]
        bucket: Dict[str, Dict[str, int]] = {
            cat: {lv: 0 for lv in severity_levels} for cat, _ in categories
        }
        for d in matrix:
            for cat, key in categories:
                lv = _classify_label_severity(d.get(key, ""))
                bucket[cat][lv] += 1

        charts.append({
            "type": "bar",
            "stacked": True,
            "title": "진단 카테고리별 부서 분포",
            "subtitle": f"전체 {len(matrix)}개 부서",
            "x_label": "진단 카테고리",
            "y_label": "부서 수",
            "datasets": [
                {
                    "label": lv,
                    "color": _PALETTE.get(lv, "#6B7280"),
                    "data": [
                        {"x": cat, "y": bucket[cat][lv]}
                        for cat, _ in categories
                    ],
                }
                for lv in severity_levels
                if any(bucket[cat][lv] > 0 for cat, _ in categories)
            ],
        })

    # 3) HR 검토 우선순위 — 레벨별 카테고리 가로 막대
    if priorities:
        # signals 기반이 아니라 priorities 자체 카운트가 1개씩이라
        # 카테고리 라벨 + 레벨 색상으로 시각화
        level_color = {1: "#EF4444", 2: "#F59E0B", 3: "#3B82F6"}
        charts.append({
            "type": "bar",
            "horizontal": True,
            "title": "HR 검토 우선순위",
            "subtitle": "레벨 1 = 즉시 시정 / 2 = 면담·분산 / 3 = 검토",
            "x_label": "건",
            "y_label": "안건",
            "datasets": [{
                "label": "안건",
                "data": [
                    {
                        "x": f"[{p['level']}] {p['category']}",
                        "y": len(p.get("details", [])) or 1,
                        "color": level_color.get(p["level"], "#6B7280"),
                        "tooltip": p.get("summary", ""),
                    }
                    for p in priorities
                ],
            }],
        })

    # 4) 분석 영역별 적발 사원 수 (가로 막대)
    signal_keys = [
        ("보상 누락 (#2)", overview.get("underpaid_count", 0)),
        ("부당 보상 (#1-3)", overview.get("unfair_count", 0)),
        ("법규 위반 (#9)", (overview.get("safety", {}) or {}).get("total_violation_emps", 0)),
        ("평가자 이상치 (#5)",
         (overview.get("evaluator_pattern", {}) or {}).get("n_candidates", 0)),
    ]
    if any(v > 0 for _, v in signal_keys):
        charts.append({
            "type": "bar",
            "horizontal": True,
            "title": "분석 영역별 적발 건수",
            "subtitle": "동일 사원이 여러 영역에서 중복 집계될 수 있음",
            "x_label": "사원/안건 수",
            "y_label": "분석 영역",
            "datasets": [{
                "label": "적발",
                "color": "#6366F1",
                "data": [{"x": label, "y": v} for label, v in signal_keys],
            }],
        })

    return charts


# ─── HTML 보고서 빌더 (자체 렌더, 외부 의존성 0) ───
import math
import html as html_lib

_DEFAULT_PALETTE = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6",
    "#EC4899", "#14B8A6", "#F97316", "#6366F1", "#84CC16",
]


def _md_to_html(md_text: str) -> str:
    """python markdown 라이브러리로 HTML 변환 (테이블·코드블록 지원)."""
    import markdown as md_lib
    return md_lib.markdown(md_text, extensions=["tables", "fenced_code"])


def _svg_donut(spec: Dict[str, Any]) -> str:
    """도넛 차트 SVG — 범례 우측, 인라인 라벨."""
    data = (spec.get("datasets", [{}])[0] or {}).get("data", [])
    if not data:
        return ""
    total = sum(d.get("value", 0) for d in data) or 1

    W, H = 720, 360
    cx, cy = 200, 180
    r_out, r_in = 130, 70

    paths = []
    legend_items = []
    angle = -math.pi / 2  # 12시 방향 시작
    for i, d in enumerate(data):
        val = d.get("value", 0)
        if val <= 0:
            continue
        frac = val / total
        end = angle + frac * 2 * math.pi
        large_arc = 1 if frac > 0.5 else 0
        x1, y1 = cx + r_out * math.cos(angle), cy + r_out * math.sin(angle)
        x2, y2 = cx + r_out * math.cos(end), cy + r_out * math.sin(end)
        x3, y3 = cx + r_in * math.cos(end), cy + r_in * math.sin(end)
        x4, y4 = cx + r_in * math.cos(angle), cy + r_in * math.sin(angle)
        color = d.get("color") or _DEFAULT_PALETTE[i % len(_DEFAULT_PALETTE)]
        path = (
            f"M {x1:.1f} {y1:.1f} "
            f"A {r_out} {r_out} 0 {large_arc} 1 {x2:.1f} {y2:.1f} "
            f"L {x3:.1f} {y3:.1f} "
            f"A {r_in} {r_in} 0 {large_arc} 0 {x4:.1f} {y4:.1f} Z"
        )
        paths.append(
            f'<path d="{path}" fill="{color}" stroke="white" stroke-width="2"/>'
        )
        # 범례
        ly = 60 + i * 30
        pct = frac * 100
        legend_items.append(
            f'<rect x="400" y="{ly}" width="16" height="16" fill="{color}" rx="3"/>'
            f'<text x="424" y="{ly + 13}" font-size="13" fill="#374151">'
            f'{html_lib.escape(str(d.get("label", "")))}'
            f' <tspan fill="#6B7280">— {val} ({pct:.1f}%)</tspan>'
            f'</text>'
        )
        angle = end

    # 가운데 총합
    center = (
        f'<text x="{cx}" y="{cy - 6}" text-anchor="middle" font-size="14" '
        f'fill="#6B7280">합계</text>'
        f'<text x="{cx}" y="{cy + 22}" text-anchor="middle" font-size="28" '
        f'font-weight="700" fill="#111827">{total}</text>'
    )

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img">'
        + "".join(paths) + center + "".join(legend_items) +
        '</svg>'
    )


def _svg_bar(spec: Dict[str, Any]) -> str:
    """막대 차트 SVG — 가로/세로/스택 지원."""
    horizontal = bool(spec.get("horizontal"))
    stacked = bool(spec.get("stacked"))
    datasets = spec.get("datasets", [])
    if not datasets:
        return ""

    # X 카테고리 추출 (순서 유지)
    cats: List[str] = []
    for ds in datasets:
        for pt in ds.get("data", []):
            x = str(pt.get("x", ""))
            if x not in cats:
                cats.append(x)
    if not cats:
        return ""

    # 카테고리별 y 값 매트릭스
    matrix: Dict[str, List[float]] = {c: [] for c in cats}
    for ds in datasets:
        ds_map = {str(p.get("x", "")): float(p.get("y", 0) or 0) for p in ds.get("data", [])}
        for c in cats:
            matrix[c].append(ds_map.get(c, 0.0))

    # 최대값 — 스택은 합, 그룹은 max
    if stacked:
        max_val = max(sum(matrix[c]) for c in cats)
    else:
        max_val = max((max(matrix[c]) if matrix[c] else 0) for c in cats)
    max_val = max(max_val, 1)

    W, H = 720, max(360, 60 + len(cats) * 40) if horizontal else 380
    pad_l, pad_r, pad_t, pad_b = (160, 30, 50, 40) if horizontal else (60, 30, 50, 70)
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    parts: List[str] = []

    # 그리드 + 축 눈금
    n_ticks = 5
    for i in range(n_ticks + 1):
        v = max_val * i / n_ticks
        if horizontal:
            x = pad_l + plot_w * i / n_ticks
            parts.append(
                f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" '
                f'stroke="#E5E7EB" stroke-dasharray="3,3"/>'
                f'<text x="{x:.1f}" y="{pad_t + plot_h + 18}" text-anchor="middle" '
                f'font-size="11" fill="#6B7280">{v:.0f}</text>'
            )
        else:
            y = pad_t + plot_h - plot_h * i / n_ticks
            parts.append(
                f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                f'stroke="#E5E7EB" stroke-dasharray="3,3"/>'
                f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
                f'font-size="11" fill="#6B7280">{v:.0f}</text>'
            )

    # 막대
    band = (plot_h if horizontal else plot_w) / len(cats)
    bar_inset = band * 0.15
    n_ds = len(datasets)
    sub_w = (band - bar_inset * 2) / (1 if stacked else n_ds)

    for ci, cat in enumerate(cats):
        cum = 0.0
        for di, ds in enumerate(datasets):
            v = matrix[cat][di]
            color = ds.get("color") or _DEFAULT_PALETTE[di % len(_DEFAULT_PALETTE)]
            # 데이터셋 단일 + 데이터 항목별 색상 (overview chart)
            if len(datasets) == 1:
                pt = ds.get("data", [])[ci] if ci < len(ds.get("data", [])) else {}
                if pt.get("color"):
                    color = pt["color"]
            if horizontal:
                bar_len = plot_w * (v / max_val)
                if stacked:
                    x = pad_l + plot_w * (cum / max_val)
                    y = pad_t + ci * band + bar_inset
                    h = band - bar_inset * 2
                else:
                    x = pad_l
                    y = pad_t + ci * band + bar_inset + di * sub_w
                    h = sub_w * 0.85
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_len:.1f}" height="{h:.1f}" '
                    f'fill="{color}" rx="3"/>'
                )
                if v > 0:
                    parts.append(
                        f'<text x="{x + bar_len + 6:.1f}" y="{y + h/2 + 4:.1f}" '
                        f'font-size="11" fill="#374151">{v:.0f}</text>'
                    )
            else:
                bar_h = plot_h * (v / max_val)
                if stacked:
                    x = pad_l + ci * band + bar_inset
                    w = band - bar_inset * 2
                    y = pad_t + plot_h - plot_h * ((cum + v) / max_val)
                else:
                    x = pad_l + ci * band + bar_inset + di * sub_w
                    w = sub_w * 0.85
                    y = pad_t + plot_h - bar_h
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" '
                    f'fill="{color}" rx="3"/>'
                )
            cum += v

        # 카테고리 라벨
        if horizontal:
            ly = pad_t + ci * band + band / 2 + 4
            label = cat if len(cat) <= 20 else cat[:18] + "…"
            parts.append(
                f'<text x="{pad_l - 10}" y="{ly:.1f}" text-anchor="end" '
                f'font-size="12" fill="#374151">{html_lib.escape(label)}</text>'
            )
        else:
            lx = pad_l + ci * band + band / 2
            label = cat if len(cat) <= 8 else cat[:7] + "…"
            parts.append(
                f'<text x="{lx:.1f}" y="{pad_t + plot_h + 18}" text-anchor="middle" '
                f'font-size="12" fill="#374151">{html_lib.escape(label)}</text>'
            )

    # 범례 (스택·그룹 둘 다 — 데이터셋 라벨 있을 때)
    if not (len(datasets) == 1 and any(p.get("color") for p in datasets[0].get("data", []))):
        lx = pad_l
        ly = H - 12
        for di, ds in enumerate(datasets):
            color = ds.get("color") or _DEFAULT_PALETTE[di % len(_DEFAULT_PALETTE)]
            label = html_lib.escape(str(ds.get("label", f"Series {di+1}")))
            parts.append(
                f'<rect x="{lx}" y="{ly - 10}" width="12" height="12" fill="{color}" rx="2"/>'
                f'<text x="{lx + 18}" y="{ly}" font-size="12" fill="#374151">{label}</text>'
            )
            lx += 18 + len(label) * 8 + 24

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img">'
        + "".join(parts) +
        '</svg>'
    )


def _render_chart_block(spec: Dict[str, Any]) -> str:
    """차트 카드 1개."""
    t = spec.get("type", "")
    if t in ("doughnut", "pie"):
        body = _svg_donut(spec)
    elif t == "bar":
        body = _svg_bar(spec)
    else:
        return ""
    title = html_lib.escape(spec.get("title", "차트"))
    subtitle = spec.get("subtitle")
    sub_html = (
        f'<div class="chart-subtitle">{html_lib.escape(subtitle)}</div>'
        if subtitle else ""
    )
    return (
        f'<section class="chart-card">'
        f'<h3 class="chart-title">{title}</h3>'
        f'{sub_html}'
        f'<div class="chart-body">{body}</div>'
        f'</section>'
    )


_HTML_STYLE = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Apple SD Gothic Neo',
               'Malgun Gothic', sans-serif;
  max-width: 960px; margin: 32px auto; padding: 0 24px;
  color: #111827; line-height: 1.65; background: #FFFFFF;
}
.report-header {
  border-bottom: 3px solid #3B82F6; padding-bottom: 16px; margin-bottom: 24px;
}
.report-header h1 { margin: 0 0 8px; font-size: 26px; color: #111827; }
.report-meta { color: #6B7280; font-size: 13px; }
.report-body h1 { font-size: 24px; margin-top: 36px; }
.report-body h2 {
  font-size: 18px; margin-top: 32px; padding-bottom: 6px;
  border-bottom: 1px solid #E5E7EB; color: #1F2937;
}
.report-body h3 { font-size: 16px; margin-top: 22px; }
.report-body p { margin: 12px 0; }
.report-body ul, .report-body ol { padding-left: 22px; }
.report-body li { margin: 4px 0; }
.report-body strong { color: #111827; }
.report-body em { color: #6B7280; font-style: normal; font-size: 13px; }
.report-body blockquote {
  margin: 12px 0; padding: 10px 14px; background: #F3F4F6;
  border-left: 4px solid #6366F1; color: #374151;
}
.report-body table {
  width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 13.5px;
  background: #FFF;
}
.report-body th, .report-body td {
  border: 1px solid #E5E7EB; padding: 8px 12px; text-align: left;
}
.report-body th { background: #F9FAFB; font-weight: 600; color: #111827; }
.report-body tr:nth-child(even) td { background: #FAFBFC; }
.report-body hr { border: none; border-top: 1px solid #E5E7EB; margin: 24px 0; }
.charts-section {
  margin-top: 40px; padding-top: 24px; border-top: 2px dashed #E5E7EB;
}
.charts-section h2 {
  font-size: 20px; margin-bottom: 20px;
}
.chart-card {
  background: #FAFBFC; border: 1px solid #E5E7EB; border-radius: 10px;
  padding: 18px 20px; margin-bottom: 22px;
}
.chart-title { margin: 0 0 4px; font-size: 15px; color: #111827; font-weight: 600; }
.chart-subtitle { color: #6B7280; font-size: 12.5px; margin-bottom: 12px; }
.chart-body svg { width: 100%; height: auto; }
.report-footer {
  margin-top: 40px; padding-top: 16px; border-top: 1px solid #E5E7EB;
  color: #9CA3AF; font-size: 12px; text-align: center;
}
"""


def build_html_report(
    markdown_text: str,
    chart_specs: Optional[List[Dict[str, Any]]] = None,
    title: str = "분석 보고서",
) -> str:
    """마크다운 + 차트 spec → 자체 렌더 HTML (외부 JS·CDN 의존성 0)."""
    body_html = _md_to_html(markdown_text)
    charts_html = ""
    if chart_specs:
        chart_blocks = "".join(_render_chart_block(c) for c in chart_specs)
        if chart_blocks:
            charts_html = (
                '<section class="charts-section">'
                '<h2>📊 시각화</h2>'
                f'{chart_blocks}'
                '</section>'
            )

    safe_title = html_lib.escape(title)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        '<!DOCTYPE html>'
        '<html lang="ko"><head>'
        '<meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        f'<title>{safe_title}</title>'
        f'<style>{_HTML_STYLE}</style>'
        '</head><body>'
        '<div class="report-header">'
        f'<h1>{safe_title}</h1>'
        f'<div class="report-meta">생성 시각 · {generated_at}</div>'
        '</div>'
        f'<div class="report-body">{body_html}</div>'
        f'{charts_html}'
        '<div class="report-footer">PeopleCore HR 분석 시스템 · 자동 생성 보고서</div>'
        '</body></html>'
    )
