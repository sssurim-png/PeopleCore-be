# 성과 평가 자동 산식 — 점수 집계·가중치·Z-score·강제분포

> 평가 시즌 산정은 자기평가·상위자평가 점수 집계 → 가중치 적용 → 팀장 편향 보정 → 강제분포 등급 배분의 4단 알고리즘으로 수행됩니다. 모든 계산은 BigDecimal 정밀도로 처리되어 재산정 시 동일 결과를 보장합니다.

[← README로 돌아가기](../README.md)

---

## 1. 산정 흐름 개요

```
[자기평가]                  [상위자평가]
   │                            │
   ▼                            ▼
aggregateSelfScore()    aggregateManagerScore()
   │                            │
   └────────────┬───────────────┘
                ▼
   ① 점수 집계 (KPI 달성률·등급 라벨)
                ▼
   ② 가중치 적용 점수 산출 (calculateAutoGrades)
                ▼
   ③ Z-score 팀장 편향 보정 (applyBiasAdjustment)
                ▼
   ④ 강제분포 등급 배분 (applyDistribution)
                ▼
            최종 등급
```

---

## 2. ① 점수 집계

### 2-1. 자기평가 — KPI 가중평균

[`EvalGradeService.aggregateSelfScore()`](../hr-service/src/main/java/com/peoplecore/evaluation/service/EvalGradeService.java)

```java
// 승인된 KPI 만 수집, actualValue null 시 미제출 판정
BigDecimal weightedSum = kpis.stream()
    .filter(kpi -> kpi.getActualValue() != null)
    .map(kpi -> kpi.achievement().multiply(kpi.getWeight()))
    .reduce(BigDecimal.ZERO, BigDecimal::add);

BigDecimal score = weightedSum
    .divide(totalWeight, 6, RoundingMode.HALF_UP)
    .min(BigDecimal.valueOf(100));  // 100 상한 clip
```

### 2-2. 상위자평가 — 등급 라벨 매핑

```java
// gradeLabel ("A", "B", "C"...) → snapshot.rawScoreTable 룩업
String gradeLabel = managerEvaluation.getGradeLabel();
BigDecimal score = snapshot.getRawScoreTable().get(gradeLabel);
```

상위자평가는 평가자 1명이라 평균 산출 없음. 등급 라벨이 박제 스냅샷의 `rawScoreTable` 을 통해 점수로 변환.

---

## 3. ② 가중치 적용 점수 산출

[`EvalGradeService.calculateAutoGrades()`](../hr-service/src/main/java/com/peoplecore/evaluation/service/EvalGradeService.java)

```java
// 최종 점수 = (자기 × selfWeight + 상위자 × mgrWeight) / weightSum + adjustment
BigDecimal weightedScore = selfScore.multiply(selfWeight)
    .add(managerScore.multiply(mgrWeight))
    .divide(selfWeight.add(mgrWeight), 6, RoundingMode.HALF_UP);

BigDecimal finalScore = weightedScore.add(adjustment);
```

BigDecimal + `RoundingMode.HALF_UP` 일관 사용 → double 누적 오차로 인한 결정성 손상 없음.

---

## 4. ③ Z-score 팀장 편향 보정

팀마다 팀장의 평가 성향이 다른 점을 통계적으로 정규화합니다.

### 4-1. 산출 공식

```
Z = (점수 - 팀 평균) / 팀 표준편차
보정 점수 = Z × 전사 표준편차 + 전사 평균
```

### 4-2. 코드

[`EvalGradeService.applyBiasAdjustment()`](../hr-service/src/main/java/com/peoplecore/evaluation/service/EvalGradeService.java)

```java
BigDecimal teamMean = calcMgrAvg(teamScores);
BigDecimal teamStdDev = calcMgrStdDev(teamScores, teamMean);
BigDecimal companyMean = calcMgrAvg(companyScores);
BigDecimal companyStdDev = calcMgrStdDev(companyScores, companyMean);

for (EvalGrade g : teamGrades) {
    BigDecimal z = g.getManagerScore().subtract(teamMean)
        .divide(teamStdDev, 6, RoundingMode.HALF_UP);
    BigDecimal adjusted = z.multiply(companyStdDev).add(companyMean);
    g.setBiasAdjustedScore(adjusted);
}
```

### 4-3. 보정 스킵 조건

| 조건 | 처리 |
|---|---|
| 팀 크기 < `minTeamSize` (기본 5) | 보정 스킵, 원점수 사용 |
| 팀 표준편차 = 0 | 보정 스킵 (Z-score 분모 0 회피) |

표본 크기가 작은 팀은 통계적 신뢰도 낮아 보정 대상에서 제외.

---

## 5. ④ 강제분포 등급 배분

### 5-1. 산출 절차

[`EvalGradeService.applyDistribution()`](../hr-service/src/main/java/com/peoplecore/evaluation/service/EvalGradeService.java)

```java
// biasAdjustedScore 내림차순 정렬
grades.sort(Comparator.comparing(EvalGrade::getBiasAdjustedScore).reversed());

// snapshot.gradeRules 의 비율 기준 배분 (예: S 10%, A 20%, B 40%, C 20%, D 10%)
for (GradeRule rule : snapshot.getGradeRules()) {
    int count = Math.round(total * rule.getRatio() / 100);
    // 마지막 등급은 잔여 몰기
}
```

### 5-2. 동점자 처리

경계값에 동일 점수 사원 발생 시 상위 등급으로 흡수 (예: B 경계 동점자 → A 등급으로 흡수).

---

## 6. 운영 규칙

| 규칙 | 이유 |
|---|---|
| 모든 계산 BigDecimal + `HALF_UP` | double 누적 오차 차단, 재산정 시 동일 결과 보장 |
| 가중치·비율은 시즌 박제 스냅샷 참조 | 시즌 도중 회사 규칙 변경 무영향 |
| 팀 크기 < `minTeamSize` 보정 제외 | 통계적 신뢰도 부족 |
| 마지막 등급은 잔여 몰기 | 반올림 누적 오차 흡수 |

---

## 7. 효과

| 항목 | 결과 |
|---|---|
| 동일 입력 → 동일 결과 | BigDecimal 결정성 |
| 팀장 편향 | Z-score 정규화로 사원 간 등급 공정성 확보 |
| 강제분포 비율 | 시즌 정의 비율 그대로 유지 |
| 시즌 도중 규칙 변경 | 스냅샷 참조로 무영향 |

---

## 8. 코드 위치

- [`EvalGradeService.java`](../hr-service/src/main/java/com/peoplecore/evaluation/service/EvalGradeService.java)
  - `aggregateSelfScore()`, `aggregateManagerScore()`
  - `calculateAutoGrades()`
  - `applyBiasAdjustment()`, `calcMgrAvg()`, `calcMgrStdDev()`
  - `applyDistribution()`
- [`Season.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/Season.java) — `formSnapshot`
- [`ManagerEvaluation.java`](../hr-service/src/main/java/com/peoplecore/evaluation/domain/ManagerEvaluation.java)
