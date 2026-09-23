# 문제 해결 기록

프로젝트 개발과 실행 중 실제로 발생하고 원인을 확인한 문제만 기록합니다. 오류 메시지, 영향, 검토한 방법, 선택한 해결책, 검증 결과를 함께 남깁니다.

## Gradle Wrapper가 배포 파일을 받지 못함

### 증상

제한된 Windows 실행 환경에서 `gradlew.bat bootRun`을 실행했을 때 Gradle 배포 파일 다운로드가 시작된 뒤 아래 오류로 종료됐습니다.

```text
java.net.SocketException: Permission denied: getsockopt
```

### 원인

처음 실행하는 Gradle Wrapper는 지정된 Gradle 배포 파일과 의존성을 외부에서 받아야 합니다. 실행 환경의 네트워크 권한이 차단돼 연결 단계에서 실패했습니다. 프로젝트 코드나 Gradle 설정의 컴파일 오류는 아니었습니다.

배포 파일이 없는 상태에서 `--offline`도 확인했지만, 로컬에 사용할 파일이 없어 같은 문제를 해결할 수 없었습니다.

### 해결

Gradle Wrapper의 첫 다운로드에만 네트워크 접근을 허용하고 같은 명령을 다시 실행했습니다. 일반 환경에서도 첫 실행에는 인터넷 연결이 필요합니다.

### 검증

Spring Boot 4.1.1 서버가 `127.0.0.1:18080`에서 시작됐고, 대시보드와 `GET /api/bank/monitoring` 응답을 확인했습니다. 검증이 끝난 뒤 서버와 포트를 종료했습니다.

## 화면은 열리지만 거래 재판별이 되지 않음

### 증상

Kotlin 서버만 실행했을 때 저장된 지표와 가상 거래는 표시됐지만, 화면 상단에 `모델 서비스 연결 필요`가 표시됐습니다. 이 상태에서 점수를 요청하면 HTTP 503과 아래 응답을 반환합니다.

```json
{"error":"Model service unavailable"}
```

### 원인

Kotlin 서버는 판별 요청을 `127.0.0.1:8001`의 Python 모델 서비스로 전달합니다. 두 프로세스 중 Kotlin 서버만 실행하면 보고서 조회는 가능하지만 새로운 판별은 처리할 수 없습니다.

### 해결

저장소 루트에서 `python scripts\run_demo.py`를 실행해 두 서비스를 함께 시작했습니다. 프로세스를 나눠 실행할 때는 Python 모델 서비스를 먼저 시작한 뒤 Kotlin 서버를 실행합니다.

### 검증

Python 모델이 이체 점수를 반환하고, Kotlin 서버가 판별 결과와 모델 버전을 H2에 저장한 뒤 모니터링 표본 수를 1건으로 갱신하는 것을 확인했습니다.

## Jackson 3에서 JsonNode 순회 코드가 컴파일되지 않음

### 증상

모니터링 기준 분포를 읽는 Kotlin 코드에서 `JsonNode`에 `map`과 `fields()`를 사용하자 다음과 같은 컴파일 오류가 발생했습니다.

```text
Unresolved reference 'size'
Unresolved reference 'fields'
```

### 원인

배열 노드의 `map` 호출이 Jackson 3의 멤버 메서드와 충돌했고, 객체 속성 순회 API도 기존 방식과 달랐습니다. 이 프로젝트는 `tools.jackson.databind` 패키지의 Jackson 3을 사용합니다.

원시 `Map`으로 변환한 뒤 강제 형변환하는 방법도 검토했지만, 잘못된 보고서 구조를 늦게 발견할 가능성이 있어 사용하지 않았습니다.

### 해결

배열은 `values()`, 객체 속성은 `properties()`로 읽도록 변경했습니다. 두 메서드의 동작은 [Jackson 3 `JsonNode` API](https://github.com/FasterXML/jackson-databind/blob/3.x/src/main/java/tools/jackson/databind/JsonNode.java)에서 확인했습니다. 금액 구간 개수와 비율 개수도 함께 검사해 잘못된 기준 보고서가 들어오면 서버 오류로 처리합니다.

### 검증

Kotlin 테스트 6개가 모두 통과했습니다. 실제 Python 모델에 이체를 요청한 뒤 Kotlin 서버가 판별 결과를 저장하고, 모니터링 API가 입력 분포 4개와 모델 버전별 경보 비율을 반환하는 것까지 확인했습니다.

## Windows 콘솔에서 집계 결과 출력이 실패함

### 증상

학습 표본의 집계 분포를 `bank_baseline.json`에 저장한 뒤 같은 내용을 콘솔에 출력하는 단계에서 아래 오류가 발생했습니다.

```text
UnicodeEncodeError: 'cp949' codec can't encode character '\u2013'
```

### 원인

JSON 파일은 UTF-8로 저장했지만 Windows 콘솔은 CP949를 사용하고 있었습니다. 기준 기간의 대시 문자 `–`를 콘솔이 표현하지 못했습니다. 파일 저장은 출력보다 먼저 끝났으므로 보고서 자체는 생성된 상태였습니다.

### 해결

실패한 출력을 성공으로 간주하지 않고 저장된 JSON을 UTF-8로 다시 읽었습니다. 콘솔에는 영문 키와 숫자만 출력해 구조와 집계값을 별도로 검증했습니다. 같은 형식의 보조 명령에서 전체 JSON을 출력해야 한다면 Python UTF-8 모드를 사용합니다.

### 검증

기준 표본은 382,040건이었고, 금액 구간과 시간대·자금구분·매체구분의 비율 합계가 각각 `1.0`인지 확인했습니다.
