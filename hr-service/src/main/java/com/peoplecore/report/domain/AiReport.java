package com.peoplecore.report.domain;

import com.peoplecore.entity.BaseTimeEntity;
import jakarta.persistence.*;
import lombok.*;

import java.util.UUID;

// AI 에이전트가 생성한 분석 보고서. MinIO 저장 + 메타데이터 DB.
// LangGraph 에이전트의 휴먼 승인 게이트(upload_to_filevault) 통과 후 저장된다.
@Entity
@Table(name = "ai_report")
@Getter
@NoArgsConstructor
@AllArgsConstructor
@Builder
public class AiReport extends BaseTimeEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "ai_report_id")
    private Long aiReportId;

    @Column(name = "company_id", nullable = false)
    private UUID companyId;

    @Column(name = "subject_emp_id")
    private Long subjectEmpId; // 분석 대상 사원 — 사원 단위 분석만 채움 (부서·전사 분석은 null)

    @Column(name = "created_by_emp_id", nullable = false)
    private Long createdByEmpId; // 보고서 작성자 (HR 사용자)

    @Column(name = "title", nullable = false, length = 255)
    private String title;

    @Column(name = "stored_file_path", nullable = false, length = 500)
    private String storedFilePath; // MinIO 경로

    @Column(name = "original_file_name", length = 255)
    private String originalFileName;

    @Column(name = "content_type", length = 100)
    private String contentType;

    @Column(name = "file_size")
    private Long fileSize;
}
