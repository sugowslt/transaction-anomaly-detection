# 문제 해결 기록

재현과 운영 판단에 도움이 되는 문제만 남겼습니다.

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
