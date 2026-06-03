package com.peoplecore.report.dto;

import com.peoplecore.report.domain.AiReport;

import java.time.LocalDateTime;

// 보고서 목록 응답 DTO. 메타데이터만.
public record AiReportListItemDto(
        Long aiReportId,
        Long subjectEmpId,
        String title,
        String originalFileName,
        Long fileSize,
        LocalDateTime createdAt
) {
    public static AiReportListItemDto from(AiReport r) {
        return new AiReportListItemDto(
                r.getAiReportId(),
                r.getSubjectEmpId(),
                r.getTitle(),
                r.getOriginalFileName(),
                r.getFileSize(),
                r.getCreatedAt()
        );
    }
}
