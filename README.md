# Fraud Lab

카드거래 이상징후 탐지 모델과 Kotlin·Spring Boot 대시보드를 연결한 프로젝트입니다. 저장소를 복제하면 학습된 모델, 검증 보고서, 가상 거래 시나리오로 화면과 재판별 기능을 실행할 수 있습니다. 원본 데이터 다운로드는 실행에 필요하지 않습니다.

모델 학습과 검증에는 AI Hub [「이상 판별을 위한 금융거래 정보 및 사용자 패턴 합성데이터」](https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71925)의 카드거래 CSV를 사용했습니다. [AI Hub 이용정책](https://www.aihub.or.kr/intrcn/guid/usagepolicy.do?currMenu=151&topMenu=105)과 [FAQ](https://www.aihub.or.kr/aihubnews/faq/list.do)에 따라 원본 CSV와 원본 행을 가공한 샘플은 배포하지 않습니다. 공개된 거래 시나리오는 이 프로젝트에서 별도로 만들었습니다.

## 현재 결과

| 평가 대상 | PR-AUC | 경보 정밀도 | 이상거래 재현율 | 경보 비율 |
| --- | ---: | ---: | ---: | ---: |
| 2024년 카드거래 검증 52,396건 | 0.989 | 99.84% | 34.97% | 1.20% |

2023년 3분기까지의 학습 데이터 1,208,562건 중 193,528건을 고정 시드로 추출해 모델을 학습했습니다. 경보 기준은 2023년 4분기 학습 데이터 109,817건에서 정하고, 2024년 검증 데이터에 한 번 적용했습니다. 검증 결과는 정탐지 628건, 오탐지 1건, 미탐지 1,168건입니다. 지표와 파일별 결과는 `reports/`에 저장됩니다.

이 수치는 AI Hub 합성 데이터 한 종류에서 얻은 실험 결과입니다. 실제 금융거래에서의 성능을 뜻하지 않습니다. 일부 집계 열을 거래 승인 시점에 확보할 수 있는지도 아직 확인하지 않았습니다. 화면의 가상 거래에는 정답 라벨이 없으며, 검증 지표 계산에도 포함되지 않습니다.

## 구성

```text
data/                    본인이 AI Hub에서 승인받아 내려받은 CSV(배포 제외)
scripts/                 실행, 데이터 점검, 모델 학습·감사, 가상 거래 생성, 모델 HTTP 서비스
models/                  배포 가능한 카드거래 모델(.skops)
reports/                 데이터 분포·평가 지표·가상 거래 시나리오
server/                  Kotlin·Spring Boot API와 대시보드
```

코틀린 서버는 `GET /api/metrics`와 `GET /api/demo`로 보고서와 가상 거래를 제공하고, `POST /api/score`를 Python 모델 서비스로 전달합니다. 두 서버는 `127.0.0.1`에만 바인딩합니다. 카드·가맹점 식별자, 이상거래 라벨·유형·설명은 모델 입력에서 제외했습니다. 모델 파일은 `joblib` 대신 허용할 타입을 고정한 `skops` 형식으로 배포합니다.

## 실행

Python 3.14와 JDK 17이 필요합니다. 처음 실행할 때 Python 패키지와 Gradle 의존성 다운로드를 위해 인터넷 연결이 필요합니다. Windows PowerShell에서 다음 명령을 실행합니다.

```powershell
git clone https://github.com/sugowslt/transaction-anomaly-detection.git
cd transaction-anomaly-detection
python scripts\run_demo.py
```

`http://127.0.0.1:8080`에서 화면을 엽니다. 종료할 때는 실행한 터미널에서 `Ctrl+C`를 누릅니다. `run_demo.py`는 로컬 가상환경을 만들고 Python 모델 서비스와 Kotlin 서버를 실행합니다. 모델 서비스의 주소는 `http://127.0.0.1:8001`입니다.

포트가 이미 사용 중이면 `python scripts\run_demo.py --web-port 8081 --model-port 8002`처럼 바꿀 수 있습니다.

직접 프로세스를 나눠 실행하려면 가상환경 설치 후 터미널 두 개를 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\serve_model.py
```

```powershell
cd server
.\gradlew.bat bootRun
```

모델 서비스가 꺼져 있어도 저장된 평가 지표는 볼 수 있지만, 거래 재판별에는 두 프로세스가 모두 필요합니다.

## 학습과 검증 재현

AI Hub에서 본인 계정으로 데이터 이용 신청·승인을 받은 뒤 카드거래 CSV를 `data/training/카드거래/`와 `data/validation/카드거래/`에 넣습니다. 원본 CSV는 Git에서 계속 제외합니다. 다음 명령은 기존 모델과 보고서를 다시 생성하므로 별도 브랜치에서 실행하는 편이 안전합니다.

```powershell
.\.venv\Scripts\python.exe scripts\train_card_baseline.py
.\.venv\Scripts\python.exe scripts\audit_card_baseline.py
.\.venv\Scripts\python.exe scripts\build_demo.py
```

`profile_dataset.py`로 전체 데이터 분포 보고서까지 다시 만들려면 전자금융공동망 CSV도 같은 `training`·`validation` 구조에 넣어야 합니다.

## 다음 작업

전자금융공동망 거래에 맞는 별도 모델을 학습하고, 거래 시점에 확보 가능한 입력 열을 확인한 뒤 API 입력 계약을 고정할 예정입니다. 이후 거래 수신·저장, 모델 버전 관리, 경보 이력, 테스트 데이터와 운영 데이터의 분포 변화를 다루겠습니다.
