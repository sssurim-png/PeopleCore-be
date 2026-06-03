-- =====================================================================
-- 분석 서비스용 SQL VIEW 정의
-- ---------------------------------------------------------------------
-- 목적:
--   hr-service 의 도메인 룰 (소프트 삭제·테넌트·상태 필터·임원 제외 등) 을
--   DB 단에 캡슐화. 분석 서비스는 VIEW 만 SELECT → 룰 누락 위험 0.
--
-- 실행:
--   $ mysql -u root -p peoplecore < analysis_views.sql
--   ※ hr-service ddl-auto: create 환경이면 hr-service 재시작 후 재실행 필요
--   ※ 운영(ddl-auto: validate/none) 환경은 한 번만 실행
--
-- 멱등성:
--   CREATE OR REPLACE VIEW 사용 → 여러 번 실행해도 안전
--
-- 권한 (운영 시):
--   분석 read-only 계정에 VIEW SELECT 만 부여 → 원본 테이블 직접 접근 차단
--   GRANT SELECT ON peoplecore.v_* TO 'analysis_ro'@'%';
-- =====================================================================

USE peoplecore;


-- =====================================================================
-- v_active_employee — 분석 대상 사원 마스터
-- ---------------------------------------------------------------------
-- 룰:
--   - emp_status = 'ACTIVE'           (활동 사원만, 휴직·퇴사자 제외)
--   - title_code != 'T-HEAD'          (본부장·이사 같은 임원 제외)
--   - employee 의 deleted_at 컬럼 X (없음 — 시드 확인 결과 emp_resign 컬럼 사용)
--   - emp_resign IS NULL              (퇴직일 없음)
--
-- 활용: 모든 분석의 사원 베이스. JOIN 으로 사용.
-- =====================================================================
CREATE OR REPLACE VIEW v_active_employee AS
SELECT
    e.emp_id,
    e.company_id,
    e.dept_id,
    d.dept_name,
    d.dept_code,
    e.grade_id,
    g.grade_name,
    g.grade_code,
    g.grade_order,
    e.title_id,
    t.title_name,
    t.title_code,
    e.emp_name,
    e.emp_num,
    e.emp_email,
    e.emp_hire_date,
    e.emp_birth_date,
    e.emp_gender,
    e.emp_type,
    e.emp_status,
    -- 재직년수 계산 (오늘 - 입사일)
    TIMESTAMPDIFF(YEAR, e.emp_hire_date, CURRENT_DATE) AS tenure_years,
    TIMESTAMPDIFF(DAY, e.emp_hire_date, CURRENT_DATE) AS tenure_days
FROM employee e
JOIN department d ON d.dept_id = e.dept_id
JOIN grade g ON g.grade_id = e.grade_id
JOIN title t ON t.title_id = e.title_id
WHERE e.emp_status = 'ACTIVE'
  AND e.emp_resign IS NULL
  AND t.title_code != 'T-HEAD';


-- =====================================================================
-- v_finalized_eval_grade — 확정 등급 (CLOSED 시즌만)
-- ---------------------------------------------------------------------
-- 룰:
--   - season.status = 'CLOSED'        (확정 시즌만)
--   - eg.final_grade IS NOT NULL      (등급 산정 완료)
--   - is_calibrated 필드는 그대로 노출 (분석 코드에서 활용)
--
-- 활용: #1 변별력, #2 보상 누락, #4 부서 분포, #6 등급 변동
-- =====================================================================
CREATE OR REPLACE VIEW v_finalized_eval_grade AS
SELECT
    eg.grade_id AS eval_id,                  -- eval_grade 의 PK (이름 충돌 회피)
    eg.emp_id,
    eg.season_id,
    s.name AS season_name,
    s.period AS season_period,                -- FIRST_HALF / SECOND_HALF
    s.start_date AS season_start_date,
    s.end_date AS season_end_date,
    eg.self_score,
    eg.manager_score,
    eg.manager_score_adjusted,
    eg.total_score,
    eg.bias_adjusted_score,
    eg.auto_grade,
    eg.final_grade,
    eg.is_calibrated,
    eg.dept_id_snapshot,
    eg.dept_name_snapshot,
    eg.position_snapshot,
    eg.evaluator_id_snapshot,
    eg.evaluator_name_snapshot,
    -- 등급 점수 매핑 (S=5, A=4, B=3, C=2, D=1) — 분석 SQL 단순화용
    CASE eg.final_grade
        WHEN 'S' THEN 5
        WHEN 'A' THEN 4
        WHEN 'B' THEN 3
        WHEN 'C' THEN 2
        WHEN 'D' THEN 1
    END AS final_grade_score
FROM eval_grade eg
JOIN season s ON s.season_id = eg.season_id
WHERE s.status = 'CLOSED'
  AND eg.final_grade IS NOT NULL;


-- =====================================================================
-- v_manager_evaluation — 1차 상위평가 (managerGrade)
-- ---------------------------------------------------------------------
-- 룰:
--   - season.status = 'CLOSED'        (확정 시즌만)
--
-- 활용: #5 평가자 점수 분포 차이 — managerGrade 만 사용
--   주의: finalGrade 와 다름. 1차 평가자 본인의 부여 점수.
-- =====================================================================
CREATE OR REPLACE VIEW v_manager_evaluation AS
SELECT
    me.mgr_eval_id,
    me.employee_id AS emp_id,
    me.evaluator_id,
    me.season_id,
    s.name AS season_name,
    s.start_date AS season_start_date,
    me.grade_label,
    -- 등급 점수 매핑 (분석 SQL 단순화용)
    CASE me.grade_label
        WHEN 'S' THEN 5
        WHEN 'A' THEN 4
        WHEN 'B' THEN 3
        WHEN 'C' THEN 2
        WHEN 'D' THEN 1
    END AS grade_score,
    me.submitted_at
FROM manager_evaluation me
JOIN season s ON s.season_id = me.season_id
WHERE s.status = 'CLOSED';


-- =====================================================================
-- v_promotion_history — 진급 발령 이력
-- ---------------------------------------------------------------------
-- 룰:
--   - order_type = 'PROMOTION'
--   - deleted_at IS NULL              (취소된 발령 제외)
--   - status = 'COMPLETED'            (확정된 발령만)
--
-- 활용: #1 진급 정체 사원 도출
-- =====================================================================
CREATE OR REPLACE VIEW v_promotion_history AS
SELECT
    h.order_id,
    h.emp_id,
    h.company_id,
    h.effective_date,
    h.order_type,
    h.status,
    h.is_notified,
    h.notified_at
FROM hr_order h
WHERE h.order_type = 'PROMOTION'
  AND h.deleted_at IS NULL
  AND h.status = 'APPLIED';   -- 발령완료 (SCHEDULED 는 미반영 상태)


-- =====================================================================
-- v_employee_payroll_by_category — 사원별·연월별·카테고리별 급여 합계
-- ---------------------------------------------------------------------
-- 룰:
--   - payItemCategory 별 합계 (SALARY / ALLOWANCE / BONUS)
--   - PAYMENT 항목만 (DEDUCTION 제외)
--   - 시즌별 합계는 분석 코드에서 GROUP BY pay_year_month
--
-- 활용: #1 등급-연봉 변별력, #2 보상 누락
-- =====================================================================
CREATE OR REPLACE VIEW v_employee_payroll_by_category AS
SELECT
    pd.emp_id,
    pd.company_id,
    pr.pay_year_month        AS pay_year_month,
    pi.pay_item_category     AS category,
    SUM(pd.amount)           AS total_amount
FROM payroll_details pd
JOIN payroll_runs pr ON pr.payroll_run_id = pd.payroll_run_id
JOIN pay_items pi    ON pi.pay_item_id    = pd.pay_item_id
WHERE pd.pay_item_type = 'PAYMENT'
  AND pi.pay_item_category IN ('SALARY', 'ALLOWANCE', 'BONUS')
GROUP BY pd.emp_id, pd.company_id, pr.pay_year_month, pi.pay_item_category;


-- =====================================================================
-- v_active_eval — v_active_employee + v_finalized_eval_grade JOIN (편의 뷰)
-- ---------------------------------------------------------------------
-- 자주 쓰는 조합: 활동 사원 × 확정 등급
--
-- 활용: #1, #2, #4, #6 모두 활용
-- =====================================================================
CREATE OR REPLACE VIEW v_active_eval AS
SELECT
    ae.emp_id,
    ae.company_id,
    ae.dept_id,
    ae.dept_name,
    ae.grade_id,
    ae.grade_order,
    ae.title_id,
    ae.emp_name,
    ae.emp_hire_date,
    ae.tenure_years,
    feg.eval_id,
    feg.season_id,
    feg.season_name,
    feg.season_period,
    feg.season_start_date,
    feg.final_grade,
    feg.final_grade_score,
    feg.auto_grade,
    feg.is_calibrated,
    feg.evaluator_id_snapshot AS evaluator_id
FROM v_active_employee ae
JOIN v_finalized_eval_grade feg ON feg.emp_id = ae.emp_id;


-- =====================================================================
-- 검증 쿼리 (실행 후 자동 카운트)
-- =====================================================================
SELECT 'v_active_employee'              AS view_name, COUNT(*) AS cnt FROM v_active_employee UNION ALL
SELECT 'v_finalized_eval_grade',                       COUNT(*)        FROM v_finalized_eval_grade UNION ALL
SELECT 'v_manager_evaluation',                         COUNT(*)        FROM v_manager_evaluation UNION ALL
SELECT 'v_promotion_history',                          COUNT(*)        FROM v_promotion_history UNION ALL
SELECT 'v_employee_payroll_by_category',               COUNT(*)        FROM v_employee_payroll_by_category UNION ALL
SELECT 'v_active_eval',                                COUNT(*)        FROM v_active_eval;
