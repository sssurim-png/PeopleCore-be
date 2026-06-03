package com.peoplecore.report.service;

import com.peoplecore.report.domain.AiReport;
import com.peoplecore.report.dto.AiReportListItemDto;
import com.peoplecore.report.repository.AiReportRepository;
import com.peoplecore.minio.service.MinioService;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.InputStreamResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;

import java.io.InputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

// AI 분석 보고서 - LangGraph 에이전트의 휴먼 승인 게이트 통과 후 호출.
// MinIO 업로드 + DB 메타데이터 저장을 한 트랜잭션으로 묶기.
@Service
@RequiredArgsConstructor
public class AiReportService {

    private final AiReportRepository aiReportRepository;
    private final MinioService minioService;

    // 1. 업로드 - multipart 파일 + 분석 대상 사원 ID + 제목
    @Transactional
    public Long upload(UUID companyId,
                       Long createdByEmpId,
                       Long subjectEmpId,
                       String title,
                       MultipartFile file) throws Exception {
        String storedPath = minioService.uploadFile(file, "ai-report");

        AiReport report = AiReport.builder()
                .companyId(companyId)
                .createdByEmpId(createdByEmpId)
                .subjectEmpId(subjectEmpId)
                .title(title)
                .storedFilePath(storedPath)
                .originalFileName(file.getOriginalFilename())
                .contentType(file.getContentType())
                .fileSize(file.getSize())
                .build();

        return aiReportRepository.save(report).getAiReportId();
    }

    // 2. 본인 보고서 목록
    public List<AiReportListItemDto> listMine(UUID companyId, Long createdByEmpId) {
        return aiReportRepository
                .findByCompanyIdAndCreatedByEmpIdOrderByCreatedAtDesc(companyId, createdByEmpId)
                .stream()
                .map(AiReportListItemDto::from)
                .toList();
    }

    // 3. 다운로드 - MinIO 스트리밍
    public ResponseEntity<InputStreamResource> download(UUID companyId, Long reportId) throws Exception {
        AiReport report = aiReportRepository.findById(reportId)
                .filter(r -> r.getCompanyId().equals(companyId))
                .orElseThrow(() -> new IllegalArgumentException("보고서를 찾을 수 없습니다: " + reportId));

        InputStream stream = minioService.downloadFile(report.getStoredFilePath());

        String fileName = report.getOriginalFileName() == null ? "report.md" : report.getOriginalFileName();
        String encodedName = URLEncoder.encode(fileName, StandardCharsets.UTF_8).replace("+", "%20");

        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.parseMediaType(
                report.getContentType() == null ? "application/octet-stream" : report.getContentType()
        ));
        if (report.getFileSize() != null) {
            headers.setContentLength(report.getFileSize());
        }
        headers.set(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename*=UTF-8''" + encodedName);

        return ResponseEntity.ok().headers(headers).body(new InputStreamResource(stream));
    }
}
