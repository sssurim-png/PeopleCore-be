# 회사별 사원 폼 — 기본 항목과 커스텀 항목의 두 층위 분리

> 사원 등록 양식·항목을 회사별로 자유롭게 설정할 수 있어야 하는 SaaS 컨셉에서, 사원 정보는 동시에 급여·근태·평가 등 다른 모듈의 표준 키로 작동합니다. 양식을 **기본 항목(on/off)** + **커스텀 항목(JSON)** 두 층위로 분리해 회사 자유도와 모듈 간 연동 키 보존을 함께 확보합니다.

[← README로 돌아가기](../README.md)

---

## 1. 배경

### 1-1. 도메인 특성

- 회사마다 사원에게 관리할 항목 상이 (자격증·외주 여부·비상연락처·사내 사번 등) → 회사별 자유 정의 필수
- 사원 정보는 급여·근태·평가 등 다른 모듈이 의존하는 표준 키(`empName`, `empNum`, `hireDate`, `deptId` 등) 보유 → 모듈 간 연동 키 보존 필요
- 회사 가입 시점에 화면을 바로 띄울 수 있어야 함 → 기본값 시드 필요

### 1-2. 검토한 대안

| 옵션 | 장점 | 단점 | 채택 |
|---|---|---|---|
| 모든 항목 자유 추가·삭제 | 회사 자유도 최대 | 모듈 간 연동 키 보존 장치 없음 | X |
| EAV 정규화 | 표준 정규화 패턴 | 사원 조회마다 조인 다수, 페이징·정렬 복잡 | X |
| 회사마다 별도 사원 테이블 | 회사 격리 강함 | 회사 추가 시 DDL/마이그레이션 부담 | X |
| 정적 컬럼 확장 | 단순 | 한 회사 요구로 다른 회사 NULL 컬럼 누적 | X |
| **기본 항목 + 커스텀 JSON 두 층위** | 표준 키 보존 + 자유도, DDL 변경 0 | 폼 정의·값 분리 구조 필요 | O |

---

## 2. 설계 — 두 층위 분리

### 2-1. 데이터 구조

[`Employee.java`](../hr-service/src/main/java/com/peoplecore/employee/domain/Employee.java)

```java
// 기본 항목 - 정적 컬럼 (모듈 간 연동 키)
@Column(nullable = false)
private String empName;

@Column(nullable = false)
private String empNum;

@Column
private LocalDate hireDate;

// 커스텀 항목 - JSON 컬럼 (회사 자유 추가·삭제)
@JdbcTypeCode(SqlTypes.JSON)
@Column(name = "custom_fields", columnDefinition = "JSON")
private Map<String, String> customFields;
```

[`FormFieldSetup.java`](../hr-service/src/main/java/com/peoplecore/formsetup/domain/FormFieldSetup.java)

```java
// 회사별 + 폼타입별 필드 정의
private UUID companyId;
private FormType formType;   // EMPLOYEE_REGISTER, SALARY_CONTRACT, HR_ORDER, RESIGN_REGISTER
private String fieldKey;
private Boolean isFixed;     // 기본 필드 보호 플래그
private Boolean isUsed;      // 회사가 on/off 결정
// UNIQUE (company_id, form_type, field_key)
```

### 2-2. 두 층위 역할

| 층위 | 컬럼 | 회사 권한 | 데이터 보존 |
|---|---|---|---|
| **기본 항목** | 정적 컬럼 (`empName`, `empNum`, `hireDate`...) | 사용 여부(`isUsed` on/off) 만 선택 | 컬럼 보존, off 후 on 복원 시 데이터 유지 |
| **커스텀 항목** | JSON 컬럼 (`customFields`) | 자유 추가·삭제·이름 변경 | JSON 키를 회사 정의 변경에 맞춰 자유 운용 |

---

## 3. 폼 정의와 사원 데이터 분리

사원 수정 화면은 두 출처에서 분리 조회합니다.

```
[사원 수정 화면 요청]
   │
   ├─ ① 폼 구조 → FormFieldSetup (회사 현재 정의)
   │             → 회사가 폼을 바꾸면 즉시 반영
   │
   └─ ② 값 → Employee (기본 컬럼 + customFields JSON)
              → 사원이 등록 시 입력한 값 그대로
```

### 3-1. 회사가 폼을 변경한 후

| 경우 | 폼 표시 | 값 표시 |
|---|---|---|
| 기존 필드 그대로 | O | 기존 값 |
| 신규 필드 추가 | O | 빈 값 (입력 대기) |
| 기존 필드 삭제 | X | JSON 키 잔존, 화면 미노출 |

폼 정의 변경이 사원 데이터 마이그레이션을 트리거하지 않음 → 폼 자유도와 사원 데이터 무결성 독립.

---

## 4. 보호 장치

### 4-1. `isFixed` / `locked` — 기본 필드 보호

`empName`·`empNum` 같은 기본 항목은 정적 컬럼이라 삭제 자체 불가. `FormFieldSetup` 에서도 `isFixed=true` 로 표시되어 폼 변경 시도 차단.

### 4-2. `autoFillFrom` — 폼 간 자동 입력

연봉계약서·인사발령 폼에서 사원 부서·직급 자동 입력. `autoFillFrom=empId` 같은 메타데이터로 화면 동작 제어.

---

## 5. 효과

| 항목 | 결과 |
|---|---|
| 표준 키 | 회사 변경이 사원 모듈 간 연동 키에 영향 없음 |
| 신규 회사 운영 시작 | 기본 항목 시드 상태로 첫 사원 등록 즉시 가능 |
| 회사·필드 추가 시 DDL | 변경 없음 |
| 폼 변경 시 사원 데이터 | 마이그레이션 불필요 |
| 폼·값 분리 | 폼 변경과 사원 데이터 무결성 독립 |

---

## 6. 코드 위치

- [`Employee.java`](../hr-service/src/main/java/com/peoplecore/employee/domain/Employee.java)
- [`FormFieldSetup.java`](../hr-service/src/main/java/com/peoplecore/formsetup/domain/FormFieldSetup.java)
- [`FormFieldSetupService.java`](../hr-service/src/main/java/com/peoplecore/formsetup/service/FormFieldSetupService.java)
- [`EmployeeService.java`](../hr-service/src/main/java/com/peoplecore/employee/service/EmployeeService.java)
