package com.peoplecore.report.controller;

import com.peoplecore.report.dto.AiReportListItemDto;
import com.peoplecore.report.service.AiReportService;
import lombok.RequiredArgsConstructor;
import org.springframework.core.io.InputStreamResource;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;
import java.util.UUID;

// AI 분석 보고서 - 업로드 / 목록 / 다운로드.
// upload 는 LangGraph 에이전트가 휴먼 승인 후 호출하는 엔드포인트.
@RestController
@RequestMapping("/ai-reports")
@RequiredArgsConstructor
public class AiReportController {

    private final AiReportService aiReportService;

    // 1. 업로드 - multipart 파일 + (선택) subjectEmpId + title
    //    subjectEmpId 는 사원 단위 분석만 채움 (부서·전사 분석은 생략)
    @PostMapping("/upload")
    public ResponseEntity<Long> upload(
            @RequestHeader("X-User-Company") UUID companyId,
            @RequestHeader("X-User-Id") Long empId,
            @RequestParam("file") MultipartFile file,
            @RequestParam(value = "subjectEmpId", required = false) Long subjectEmpId,
            @RequestParam("title") String title) throws Exception {
        Long id = aiReportService.upload(companyId, empId, subjectEmpId, title, file);
        return ResponseEntity.ok(id);
    }

    // 2. 본인이 만든 보고서 목록
    @GetMapping
    public ResponseEntity<List<AiReportListItemDto>> listMine(
            @RequestHeader("X-User-Company") UUID companyId,
            @RequestHeader("X-User-Id") Long empId) {
        return ResponseEntity.ok(aiReportService.listMine(companyId, empId));
    }

    // 3. 다운로드
    @GetMapping("/{reportId}/download")
    public ResponseEntity<InputStreamResource> download(
            @RequestHeader("X-User-Company") UUID companyId,
            @PathVariable Long reportId) throws Exception {
        return aiReportService.download(companyId, reportId);
    }
}
