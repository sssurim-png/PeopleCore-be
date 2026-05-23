$out = "C:\Users\Playdata\Desktop\최프\be23-fin-PeopleCore\PeopleCore_소개.pptx"
$ppt = New-Object -ComObject PowerPoint.Application
$ppt.Visible = [Microsoft.Office.Core.MsoTriState]::msoTrue
$pres = $ppt.Presentations.Add([Microsoft.Office.Core.MsoTriState]::msoTrue)
$pres.PageSetup.SlideWidth = 960
$pres.PageSetup.SlideHeight = 540

function ToRGB($h){ $r=[Convert]::ToInt32($h.Substring(0,2),16);$g=[Convert]::ToInt32($h.Substring(2,2),16);$b=[Convert]::ToInt32($h.Substring(4,2),16);return $b*65536+$g*256+$r }
function AddRect($sl,$l,$t,$w,$h,$f,$bdr='',$rd=0){
    $sh=$sl.Shapes.AddShape(1,$l,$t,$w,$h)
    $sh.Fill.ForeColor.RGB=ToRGB($f)
    $sh.Fill.Solid()
    if($bdr -ne ''){$sh.Line.ForeColor.RGB=ToRGB($bdr);$sh.Line.Weight=1}else{$sh.Line.Visible=[Microsoft.Office.Core.MsoTriState]::msoFalse}
    return $sh
}
function AddText($sl,$l,$t,$w,$h,$tx,$sz,$bd,$col,$ag=1){
    $sh=$sl.Shapes.AddTextbox(1,$l,$t,$w,$h)
    $tf=$sh.TextFrame
    $tf.WordWrap=[Microsoft.Office.Core.MsoTriState]::msoTrue
    $tf.AutoSize=0
    $tf.TextRange.Text=$tx
    $tf.TextRange.Font.Size=$sz
    $tf.TextRange.Font.Bold=$bd
    $tf.TextRange.Font.Name="맑은 고딕"
    $tf.TextRange.Font.Color.RGB=ToRGB($col)
    $tf.TextRange.ParagraphFormat.Alignment=$ag
    $sh.Line.Visible=[Microsoft.Office.Core.MsoTriState]::msoFalse
    $sh.Fill.Visible=[Microsoft.Office.Core.MsoTriState]::msoFalse
    return $sh
}
function AddIcon($sl,$l,$t,$sz,$emoji,$bg){
    $sh=$sl.Shapes.AddShape(1,$l,$t,$sz,$sz)
    $sh.Fill.ForeColor.RGB=ToRGB($bg)
    $sh.Fill.Solid()
    $sh.Line.Visible=[Microsoft.Office.Core.MsoTriState]::msoFalse
    $tf=$sh.TextFrame
    $tf.TextRange.Text=$emoji
    $tf.TextRange.Font.Size=[int]($sz*0.45)
    $tf.TextRange.ParagraphFormat.Alignment=2
    $tf.VerticalAnchor=3
    return $sh
}

$G='1D9E75';$DK='0F1F18';$BL='111111';$LG='9AA49E';$WH='FFFFFF';$BD='EAEEEC';$LNG='E8F7F1';$BG='FAFAFA';$MN='4DD9A2';$GL='B3E8D0';$BU='3B82F6';$RD='E05252';$GF='F0FAF5';$GN='4A5E56';$ML='E0FFF4';$LRD='FEE2E2'

# Slide 1 — 문제 제기
while($pres.Slides.Count -lt 1){ $pres.Slides.Add(1,12) | Out-Null }
$s1=$pres.Slides(1); $s1.Layout=12
AddRect $s1 0 0 960 540 $BG | Out-Null
AddRect $s1 0 0 5 540 $G | Out-Null
AddText $s1 60 38 200 18 "PROBLEM" 9 $true $G 1 | Out-Null
AddText $s1 60 60 700 50 "왜 지금의 HR 시스템은 불편할까요?" 28 $true $BL 1 | Out-Null
AddText $s1 60 112 700 22 "기존 HR 솔루션이 가진 구조적 한계" 13 $false $LG 1 | Out-Null

$probs=@(
    @("💰","높은 도입 비용","대형 ERP 솔루션은 도입 비용만 수억~수십억 원, 세팅에 수개월이 소요됩니다.","⚠️ 중소기업은 도입 자체가 불가능"),
    @("📋","엑셀·수기 의존","HR 시스템이 없는 기업은 여전히 엑셀과 수기로 인사 데이터를 관리합니다.","⚠️ 실수·누락·데이터 파편화 빈번"),
    @("🔀","시스템 파편화","근태·급여·성과·결재가 각각 다른 프로그램에 흩어져 연동이 되지 않습니다.","⚠️ 데이터 불일치·오류 반복 발생")
)
for($i=0;$i -lt 3;$i++){
    $cx=60+$i*302; $cy=148
    AddRect $s1 $cx $cy 282 340 $WH $BD | Out-Null
    AddIcon $s1 ($cx+20) ($cy+20) 44 $probs[$i][0] 'FEECEC' | Out-Null
    AddText $s1 ($cx+12) ($cy+76) 258 28 $probs[$i][1] 16 $true $BL 1 | Out-Null
    AddText $s1 ($cx+12) ($cy+108) 258 90 $probs[$i][2] 12 $false $LG 1 | Out-Null
    AddRect $s1 ($cx+12) ($cy+210) 258 30 'FFF5F5' | Out-Null
    AddText $s1 ($cx+12) ($cy+210) 258 30 $probs[$i][3] 11 $true $RD 2 | Out-Null
}
AddText $s1 800 510 145 18 "01" 12 $false 'C8D0CC' 3 | Out-Null

# Slide 2a — 시장 트렌드
$pres.Slides.Add(2,12) | Out-Null
$s2=$pres.Slides(2); $s2.Layout=12
AddRect $s2 0 0 960 540 $BG | Out-Null
AddRect $s2 0 0 5 540 $G | Out-Null
AddText $s2 60 38 200 18 "MARKET" 9 $true $G 1 | Out-Null
AddText $s2 60 60 840 46 "HR SaaS 시장은 빠르게 성장 중입니다" 28 $true $BL 1 | Out-Null
AddText $s2 60 110 840 22 "통합과 자동화를 강조하는 시장의 흐름" 13 $false $LG 1 | Out-Null

$trends=@(
    @("📈","67%","통합 워크플로우 수요 증가","기업의 67%가 통합 플랫폼이 업무 생산성 향상에 기여한다고 응답"),
    @("🏗️","75%","오래된 ERP 구조의 해체","2027년까지 75%의 기업이 기존 모놀리식 ERP에서 모듈형으로 전환 전망"),
    @("🤖","60%","AI 기반 업무 흐름 확대","2026년까지 60% 이상의 기업이 AI 중심 업무 워크플로우로 재설계 전망")
)
$cW=270; $cGap=22; $cY=148; $cH=330
for($i=0;$i -lt 3;$i++){
    $cx=60+$i*($cW+$cGap)
    AddRect $s2 $cx $cY $cW $cH $BG $BD | Out-Null
    AddIcon $s2 ($cx+18) ($cY+18) 44 $trends[$i][0] $LNG | Out-Null
    AddText $s2 ($cx+12) ($cY+74) ($cW-24) 58 $trends[$i][1] 42 $true $G 1 | Out-Null
    AddText $s2 ($cx+12) ($cY+136) ($cW-24) 28 $trends[$i][2] 14 $true $BL 1 | Out-Null
    AddText $s2 ($cx+12) ($cY+168) ($cW-24) 90 $trends[$i][3] 11 $false $LG 1 | Out-Null
}
AddText $s2 800 510 145 18 "02" 12 $false 'C8D0CC' 3 | Out-Null

# Slide 2b — 시장 규모
$pres.Slides.Add(3,12) | Out-Null
$s2b=$pres.Slides(3); $s2b.Layout=12
AddRect $s2b 0 0 960 540 $BG | Out-Null
AddRect $s2b 0 0 5 540 $G | Out-Null
AddText $s2b 60 38 200 18 "MARKET" 9 $true $G 1 | Out-Null
AddText $s2b 60 60 840 46 "글로벌 HR Tech 시장 규모" 28 $true $BL 1 | Out-Null
AddText $s2b 60 110 840 22 "숫자로 보는 성장" 13 $false $LG 1 | Out-Null

# 성장 바
AddRect $s2b 60 148 840 130 $WH $BD | Out-Null
AddText $s2b 90 165 120 22 "2024" 12 $false $LG 1 | Out-Null
AddText $s2b 90 188 200 50 '$38.17B' 28 $true $BL 1 | Out-Null
AddText $s2b 430 190 100 44 "→" 28 $true $G 2 | Out-Null
AddRect $s2b 240 215 480 8 'E8EEEB' | Out-Null
AddRect $s2b 240 215 312 8 $G | Out-Null
AddText $s2b 390 200 180 28 "CAGR 12%" 13 $true $G 2 | Out-Null
AddText $s2b 700 165 180 22 "2030" 12 $false $LG 1 | Out-Null
AddText $s2b 700 188 180 50 '$75.30B' 28 $true $G 1 | Out-Null

# 하단 통계 카드
AddRect $s2b 60 300 400 200 $WH $BD | Out-Null
AddText $s2b 88 320 350 22 "국내 HR SaaS 시장 규모 (2024년 기준)" 12 $false $LG 1 | Out-Null
AddText $s2b 88 348 240 60 "1조" 40 $true $BL 1 | Out-Null
AddText $s2b 200 370 120 32 "원+" 18 $true $G 1 | Out-Null
AddText $s2b 88 420 350 24 "연 15% 성장 중" 12 $true $G 1 | Out-Null

AddRect $s2b 480 300 420 200 $WH $BD | Out-Null
AddText $s2b 508 320 370 22 "국내 중소기업 HR 시스템 미도입 비율" 12 $false $LG 1 | Out-Null
AddText $s2b 508 348 200 60 "60" 40 $true $BL 1 | Out-Null
AddText $s2b 620 370 120 32 "%+" 18 $true $G 1 | Out-Null
AddText $s2b 508 420 370 24 "도입 여력 충분" 12 $true $G 1 | Out-Null
AddText $s2b 800 510 145 18 "03" 12 $false 'C8D0CC' 3 | Out-Null

# Slide 4 — 제품 소개
$pres.Slides.Add(4,12) | Out-Null
$s3=$pres.Slides(4); $s3.Layout=12
AddRect $s3 0 0 960 540 $WH | Out-Null
AddRect $s3 0 0 5 540 $G | Out-Null
AddText $s3 60 32 200 18 "PRODUCT" 9 $true $G 1 | Out-Null
AddText $s3 60 52 840 40 "PeopleCore란?" 28 $true $BL 1 | Out-Null
AddText $s3 60 94 840 22 "HR 업무 전체를 하나의 플랫폼에서 — 담당자가 직접 설정하고 즉시 사용" 13 $false $LG 1 | Out-Null

$mods=@(
    @("👥","사원 관리","등록·인사발령·인력 현황"),
    @("⏰","근태 관리","출퇴근·초과근무·휴가 집계"),
    @("💸","급여 관리","자동 산정·명세서 발송"),
    @("📊","성과 평가","KPI·다면평가·등급 자동 산정"),
    @("✍️","전자결재","결재선 설정·문서 자동 채번"),
    @("💬","협업","채팅·캘린더·파일함 통합"),
    @("🔍","통합 검색","사원·문서·일정 실시간 검색"),
    @("🤖","AI Copilot","인사 데이터 기반 AI 어시스턴트")
)
for($i=0;$i -lt 8;$i++){
    $col2=$i%4; $row2=[math]::Floor($i/4)
    $mx=60+$col2*224; $my=126+$row2*168
    AddRect $s3 $mx $my 210 155 $BG $BD | Out-Null
    AddIcon $s3 ($mx+12) ($my+12) 38 $mods[$i][0] $LNG | Out-Null
    AddText $s3 ($mx+10) ($my+58) 190 24 $mods[$i][1] 13 $true $BL 1 | Out-Null
    AddText $s3 ($mx+10) ($my+84) 190 52 $mods[$i][2] 10 $false $LG 1 | Out-Null
}
AddRect $s3 60 464 840 52 $GF | Out-Null
AddRect $s3 60 464 4 52 $G | Out-Null
AddText $s3 78 464 822 52 "⚙️ 결재선·근무 정책·급여 항목·평가 등급을 HR 담당자가 직접 설정할 수 있어, 외부 업체 의뢰 없이 우리 회사에 맞는 시스템을 즉시 운영할 수 있습니다." 11 $false $GN 1 | Out-Null
AddText $s3 800 510 145 18 "04" 12 $false 'C8D0CC' 3 | Out-Null

# Slide 5 — 서비스 목표
$pres.Slides.Add(5,12) | Out-Null
$s4=$pres.Slides(5); $s4.Layout=12
AddRect $s4 0 0 960 540 $WH | Out-Null
AddRect $s4 0 0 300 540 $G | Out-Null
AddText $s4 40 60 240 18 "GOAL" 9 $false 'FFFFFFA0' 1 | Out-Null
AddText $s4 40 82 240 100 "PeopleCore가 추구하는 목표" 22 $true $WH 1 | Out-Null
AddText $s4 40 192 240 100 "중소기업도 대기업 수준의 HR 환경을 누릴 수 있도록" 12 $false $ML 1 | Out-Null

$goals=@(
    @("누구나 쉽게 도입","구독형 서비스로 초기 투자 비용 없이 월정액으로 즉시 사용. 중소기업도 대기업 수준의 HR 시스템을 경험할 수 있습니다."),
    @("우리 회사에 맞게","결재선·근무 정책·평가 방식을 담당자가 직접 설정. 회사마다 다른 업무 흐름을 그대로 반영합니다."),
    @("데이터로 더 나은 인사 결정","흩어진 HR 데이터를 하나로 모아 AI가 분석·제안. 직관이 아닌 데이터 기반으로 인사 판단을 내릴 수 있습니다.")
)
for($i=0;$i -lt 3;$i++){
    $gy=68+$i*152
    AddRect $s4 330 $gy 600 135 $BG $BD | Out-Null
    $num=AddRect $s4 352 ($gy+18) 40 40 $WH $G
    AddText $s4 352 ($gy+18) 40 40 "$($i+1)" 16 $true $G 2 | Out-Null
    AddText $s4 406 ($gy+16) 500 28 $goals[$i][0] 16 $true $BL 1 | Out-Null
    AddText $s4 406 ($gy+48) 500 70 $goals[$i][1] 11 $false $LG 1 | Out-Null
}
AddText $s4 800 510 145 18 "05" 12 $false 'C8D0CC' 3 | Out-Null

# Slide 6 — 기대 효과
$pres.Slides.Add(6,12) | Out-Null
$s5=$pres.Slides(6); $s5.Layout=12
AddRect $s5 0 0 960 540 $WH | Out-Null
AddRect $s5 0 0 5 540 $G | Out-Null
AddText $s5 60 32 200 18 "IMPACT" 9 $true $G 1 | Out-Null
AddText $s5 60 52 840 40 "도입 시 기대 효과" 28 $true $BL 1 | Out-Null
AddText $s5 60 94 840 22 "기업과 사회 모두에 긍정적인 변화를 만듭니다" 13 $false $LG 1 | Out-Null

# 기업 입장 컬럼
AddRect $s5 60 126 428 390 $WH | Out-Null
AddRect $s5 60 380 428 2 $G | Out-Null
AddIcon $s5 60 126 34 "🏢" $LNG | Out-Null
AddText $s5 100 126 200 34 "기업 입장" 13 $true $G 1 | Out-Null
$corps=@(
    @("업무 효율 향상","결재·근태·급여 자동화로 HR 담당자의 반복 업무 감소"),
    @("데이터 정확성 확보","단일 플랫폼 통합으로 부서 간 데이터 불일치 제거"),
    @("비용 절감","구독형 월정액으로 고비용 ERP 대비 도입·운영 비용 대폭 절감")
)
for($i=0;$i -lt 3;$i++){
    $iy=170+$i*108
    AddRect $s5 60 $iy 428 98 $BG $BD | Out-Null
    AddRect $s5 68 ($iy+18) 7 7 $G | Out-Null
    AddText $s5 84 ($iy+12) 388 24 $corps[$i][0] 13 $true $BL 1 | Out-Null
    AddText $s5 84 ($iy+38) 388 44 $corps[$i][1] 11 $false $LG 1 | Out-Null
}

# 사회적 입장 컬럼
AddRect $s5 508 126 432 390 $WH | Out-Null
AddRect $s5 508 380 432 2 $BU | Out-Null
AddIcon $s5 508 126 34 "🌏" 'EFF6FF' | Out-Null
AddText $s5 548 126 200 34 "사회적 입장" 13 $true $BU 1 | Out-Null
$socs=@(
    @("노동 환경 투명성 향상","중소기업 HR 디지털 전환 가속으로 근로 데이터 투명하게 관리"),
    @("근로기준법 준수율 향상","연차·초과근무 자동 관리로 법적 기준 준수를 시스템이 보장"),
    @("공정한 평가 문화 정착","데이터 기반 성과 평가로 주관적 판단 최소화, 조직 신뢰 향상")
)
for($i=0;$i -lt 3;$i++){
    $iy=170+$i*108
    AddRect $s5 508 $iy 432 98 $BG $BD | Out-Null
    AddRect $s5 516 ($iy+18) 7 7 $BU | Out-Null
    AddText $s5 532 ($iy+12) 388 24 $socs[$i][0] 13 $true $BL 1 | Out-Null
    AddText $s5 532 ($iy+38) 388 44 $socs[$i][1] 11 $false $LG 1 | Out-Null
}
AddText $s5 800 510 145 18 "06" 12 $false 'C8D0CC' 3 | Out-Null

$pres.SaveAs($out, 24)
$pres.Close()
$ppt.Quit()
Write-Host "DONE"