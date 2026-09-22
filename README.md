# Fraud Lab

AI Hub 합성 금융거래 데이터로 카드거래 이상징후를 탐지하는 프로젝트입니다. 현재 버전은 모델 학습·검증, 코틀린 API, 결과 대시보드까지 연결한 첫 실행본입니다. 거래를 선택해 승인금액과 시간대를 바꾸면 코틀린 서버가 Python 모델 서비스에 점수를 요청합니다.

원본 데이터는 [AI Hub 「이상 판별을 위한 금융거래 정보 및 사용자 패턴 합성데이터」](https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71925)에서 받습니다. 데이터 제공처는 이를 합성 금융거래 데이터로 소개합니다. CSV 원본과 모델 파일은 Git 추적 대상에서 제외했습니다.

## 현재 결과

| 평가 대상 | PR-AUC | 경보 정밀도 | 이상거래 재현율 | 경보 비율 |
| --- | ---: | ---: | ---: | ---: |
| 2024년 카드거래 검증 52,396건 | 0.989 | 99.84% | 34.97% | 1.20% |

2023년 3분기까지의 학습 데이터 1,208,562건 중 193,528건을 고정 시드로 추출해 모델을 학습했습니다. 경보 기준은 2023년 4분기 학습 데이터 109,817건에서 정하고, 2024년 검증 데이터에 한 번 적용했습니다. 검증 결과는 정탐지 628건, 오탐지 1건, 미탐지 1,168건입니다. 지표와 파일별 결과는 `reports/`에 저장됩니다.

이 수치는 합성 데이터 한 종류에서 얻은 실험 결과입니다. 실제 금융거래에서의 성능을 뜻하지 않습니다. 일부 집계 열을 운영 중 거래 승인 시점에 확보할 수 있는지도 아직 확인하지 않았습니다.

## 구성

```text
data/                    AI Hub CSV: training·validation × 카드거래·전자금융공동망
scripts/                 데이터 점검, 모델 학습·감사, 샘플 생성, 모델 HTTP 서비스
models/                  학습된 카드거래 모델(로컬 생성)
reports/                 데이터 분포·평가 지표·대시보드 샘플
server/                  Kotlin·Spring Boot API와 대시보드
```

코틀린 서버는 `GET /api/metrics`와 `GET /api/demo`로 검증 결과를 제공하고, `POST /api/score`를 Python 모델 서비스로 전달합니다. 모델 서비스는 로컬 주소 `127.0.0.1:8001`에만 연결합니다. 카드·가맹점 식별자, 이상거래 라벨·유형·설명은 모델 입력에서 제외했습니다.

## 실행

Python 3.14와 JDK 17이 필요합니다. Windows PowerShell에서 프로젝트 루트를 기준으로 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\profile_dataset.py
.\.venv\Scripts\python.exe scripts\train_card_baseline.py
.\.venv\Scripts\python.exe scripts\audit_card_baseline.py
.\.venv\Scripts\python.exe scripts\build_demo.py
```

데이터를 준비한 다음 터미널 두 개에서 각각 실행합니다.

```powershell
# 터미널 1: 프로젝트 루트
.\.venv\Scripts\python.exe scripts\serve_model.py
```

```powershell
# 터미널 2: server 폴더
.\gradlew.bat bootRun
```

화면은 `http://127.0.0.1:8080`에서 열립니다. 모델 서비스가 꺼져 있어도 저장된 평가 지표는 볼 수 있지만, 거래 재판별에는 두 프로세스가 모두 필요합니다.

## 다음 작업

전자금융공동망 거래에 맞는 별도 모델을 학습하고, 거래 시점에 확보 가능한 입력 열을 확인한 뒤 API 입력 계약을 고정할 예정입니다. 이후 거래 수신·저장, 모델 버전 관리, 경보 이력, 테스트 데이터와 운영 데이터의 분포 변화를 다루겠습니다.
