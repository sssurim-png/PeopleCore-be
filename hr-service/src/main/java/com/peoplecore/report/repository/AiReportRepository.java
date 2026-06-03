package com.peoplecore.report.repository;

import com.peoplecore.report.domain.AiReport;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.UUID;

public interface AiReportRepository extends JpaRepository<AiReport, Long> {

    // 본인이 작성한 보고서 목록 (최신순)
    List<AiReport> findByCompanyIdAndCreatedByEmpIdOrderByCreatedAtDesc(
            UUID companyId, Long createdByEmpId);
}
