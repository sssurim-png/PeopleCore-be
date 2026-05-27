# 평가 시즌 무결성 아키텍처 — 스냅샷·낙관적 락·이벤트

> 회사 규칙·평가자 매핑·평가 결과가 시즌이라는 시간 단위 안에서 일관되게 유지되어야 합니다. 시즌 OPEN 시점의 규칙을 박제하고, 등급 보정은 낙관적 락으로 직렬화하며, 평가자 퇴직은 이벤트로 분리 보정합니다.

[← README로 돌아가기](../README.md)

---

## 1. 배경

### 1-1. 도메인 특성

- 평가 시즌은 회사 단위 주기적 절차 (분기·반기)
- 시즌 안에서 사원의 평가 기준·평가자·결과는 일관되어야 함 — 같은 시즌의 사원이 다른 기준으로 평가되면 공정성 어긋남
- 시즌은 인사·급여·근태 도메인과 다르게 시작·진행·종료가 명확한 라이프사이클 보유

### 1-2. 무결성이 어긋날 수 있는 시점

| 시점 | 원인 | 영향 |
|---|---|---|
| 시즌 진행 중 회사 규칙 변경 | 가중치·강제분포 비율 수정 | 같은 시즌 사원이 다른 기준으로 산정 |
| 다중 HR 담당자 동시 보정 | Last-Write-Wins | 강제분포 비율 어긋남, 보정 이력 유실 |
| 평가자 퇴직 | 사원 도메인 변경 | EvalGrade 매핑 평가자 ID 무효 |
| 매핑 미완료 상태 시즌 시작 | 시즌 생성 단계 검증 부재 | 평가 단계에서 누락 발견 |

---

## 2. 설계 — 세 가지 보호 장치

### 2-1. 시즌 생애주기

```
DRAFT → 시즌 생성 (매핑 100% 결정 검증)
   ↓
OPEN  → 규칙 스냅샷 박제 (formSnapshot)
   ↓ (등급 보정: @Version 낙관적 락)
   ↓ (평가자 퇴직: 이벤트 기반 자동 보정)
   ↓
CLOSED
```

### 2-2. 핵심 보호 장치 매핑

| 보호 장치 | 적용 시점 | 핵심 기술 | 효과 |
|---|---|---|---|
| 시즌 OPEN 규칙 스냅샷 박제 | OPEN 진입 | `Season.formSnapshot` JSON 컬럼 | 시즌 도중 규칙 변경 무영향 |
| 등급 보정 낙관적 락 | 보정 트랜잭션 | `EvalGrade.@Version` + HTTP 409 변환 | 다중 HR 동시 보정 충돌 차단 |
| 평가자 퇴직 자동 보정 | 사원 퇴직 후 | `EmployeeRetiredEvent` AFTER_COMMIT | 진행 중 시즌 매핑 즉시 정리 |

---

## 3. 시즌 OPEN 규칙 스냅샷

### 3-1. 데이터 구조

[`Season.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/Season.java)

```java
@Column(name = "form_snapshot", columnDefinition = "JSON")
private String formSnapshot;   // 시즌 OPEN 시 회사 규칙 박제

@Column(name = "form_version")
private Long formVersion;

public void freezeSnapshot(String formJson, Long version) {
    this.formSnapshot = formJson;
    this.formVersion = version;
}
```

### 3-2. OPEN 트랜잭션

[`SeasonService.java`](../hr-service/src/main/java/com/peoplecore/evaluation/service/SeasonService.java)

```java
public void openSeason(UUID seasonId) {
    Season season = seasonRepository.findById(seasonId).orElseThrow();
    EvaluationRules rules = rulesService.getEntityByCompanyId(companyId);
    String mergedJson = rulesService.buildMergedSnapshotJson(rules);
    season.freezeSnapshot(mergedJson, rules.getFormVersion());
    season.open();
}
```

계산 엔진은 공식 하드코딩 없이 박제된 `formSnapshot` 만 참조 → 시즌 도중 규칙이 바뀌어도 그 시즌 계산은 OPEN 시점 규칙으로 고정.

---

## 4. 등급 보정 낙관적 락

### 4-1. 데이터 구조

[`EvalGrade.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/EvalGrade.java)

```java
@Version
private Long version;
```

### 4-2. 충돌 처리

```java
@ExceptionHandler(OptimisticLockingFailureException.class)
public ResponseEntity<?> handleOptimisticLock(Exception e) {
    return ResponseEntity.status(409).body(Map.of(
        "code", "OPTIMISTIC_LOCK_CONFLICT",
        "message", "다른 사용자가 방금 수정했습니다. 새로고침 후 다시 시도해 주세요."));
}
```

후행 트랜잭션은 0 row affected 시점에 예외 발생 → 사용자에게 명확한 안내 후 화면 갱신.

---

## 5. 평가자 퇴직 이벤트 보정

### 5-1. 이벤트 흐름

```
[퇴직 트랜잭션 커밋]
   ↓
EmployeeRetiredEvent 발행
   ↓
EvaluatorRetirementListener (AFTER_COMMIT)
   ↓
EvaluatorRetirementHandler.handleEmployeeRetired()
   ├─ EmpEvaluatorGlobal 에서 해당 평가자 row 삭제
   ├─ 진행 중(OPEN) 시즌 EvalGrade 중 평가자 == 퇴직자 행 선별
   └─ EvalGrade.clearEvaluator() → 평가자 필드 null
```

### 5-2. 시즌 생성 단계 사전 검증

```java
List<Employee> unmapped = employeeRepository.findUnmappedActiveEmployees(companyId);
// excluded=false && evaluator=null 인 활성 사원
if (!unmapped.isEmpty()) {
    throw new MappingIncompleteException(unmapped);
}
```

매핑 미결정자 발견 시 시즌 생성 자체 차단.

---

## 6. 효과

| 항목 | 결과 |
|---|---|
| 시즌 도중 규칙 변경 | 그 시즌 계산은 OPEN 시점 규칙으로 고정 |
| 다중 HR 동시 보정 | 선행 보정 보존, 후행 HR 은 최신 데이터 기준 재시도 |
| 평가자 퇴직 | 진행 중 시즌 매핑 즉시 정리, HR_ADMIN 알림 |
| 매핑 미완료 시즌 | 시즌 생성 단계에서 차단 |
| 퇴직 트랜잭션과 평가 정리 | AFTER_COMMIT 분리로 독립 |

---

## 7. 코드 위치

- [`Season.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/Season.java)
- [`EvalGrade.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/EvalGrade.java)
- [`SeasonService.java`](../hr-service/src/main/java/com/peoplecore/evaluation/service/SeasonService.java)
- [`EvaluatorRetirementHandler.java`](../hr-service/src/main/java/com/peoplecore/evaluation/event/EvaluatorRetirementHandler.java)
