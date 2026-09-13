# SAi AI Benchmark Package

## 비교 기준

자동 승격 기능의 병합 전 기준 커밋은 `76b6cd2`, 병합된 후보 커밋은 `180acac`이다.
Git 이력에 두 커밋이 모두 남아 있으므로 develop 병합 이후에도 전후 비교가 가능하다.

전체 파이프라인 비교에서는 커밋 해시를 보고서와 함께 기록하고, 두 버전에 같은
데이터 스냅샷, 모델명, 프롬프트 입력, 위험 키워드 및 Scope 설정을 사용한다.

## 요구 환경

- 평가 전용 외부 라이브러리를 추가하지 않으며 기존 프로젝트 의존성만 사용한다.
- `run-promotion --mode current`와 `run-current`는 Django/평가 DB를 사용하고,
  `model-pilot`은 Django 설정과 OpenAI API를 사용한다.
- 일반 `score`, `compare`, `validate`는 Django나 외부 API가 필요 없다.

## 실측 비용 요약

이 문서의 결과 표에는 로컬에서 실제 서비스 코드와 OpenAI API를 실행해 저장한 값만
기록한다. 과거 시뮬레이션 수치는 포함하지 않는다. 최종 보고에 사용한 두 benchmark의
실제 API 응답 token usage와 Standard 단가로 계산한 비용은 다음과 같다.

| 실행 | 실제 API 호출 | 계산 비용 |
| --- | ---: | ---: |
| 모델 선정 파일럿 | 32회 | $0.22265920 |
| 합성 RAG ablation | 4,368회 | $5.13466448 |
| 합계 | 4,400회 | $5.35732368 |

Prompt caching, 세금과 계정별 청구 조건은 반영하지 않았으므로 실제 청구서와 소수점
단위 차이가 날 수 있다. 비용과 호출 수는 raw response usage 및 실행 중 수집한 API
호출 로그를 기준으로 집계했다.

- OpenAI embedding 단가: https://developers.openai.com/api/docs/models/text-embedding-3-small

## 실측 모델 선정 파일럿

### 목적과 구성

운영 프롬프트와 구조화 출력 스키마를 그대로 사용해 기존 시연 구성과 저비용 구성을
동일한 고정 데이터셋으로 비교했다. 최종 보고 대상은
`datasets/model_selection_pilot.json`의 `SAi model-selection pilot v2`이며, SHA-256은
`747d64aca8d391d21c73acd89280073f6ccb1b3d48d64c6537f4829b227ea66a`이다.

평가 단위는 총 51개다.

- 원문 분류 18건
- 핸드북 초안 정답 규칙 2건(합성 원문 8개로부터 추출)
- 카드 지시 판정 12건
- 카드 생성 5건
- 검색된 규칙이 주어진 Q&A 답변 8건
- 영문 번역 6건

비교한 모델 라우팅은 다음과 같다.

| 역할 | 기존 시연 구성 | 저비용 구성 |
| --- | --- | --- |
| 분류 | Luna / medium | Luna / medium |
| 핸드북 초안 | Sol / high | Terra / medium |
| Q&A 답변 | Sol / high | Terra / medium |
| 번역 | Terra / low | Luna / low |

분류와 카드 지시 판정은 두 구성에서 동일한 Luna 설정이므로 결과를 한 번만 호출해
양쪽에 재사용했다. 보고서상 구성별 논리 호출은 17회이며 실제 결제 대상 호출은 총
32회다. 공통 호출 2회의 중복을 제외한 이번 비교 실행의 계산 비용 합계는
$0.22265920이다. 가격 계산에는 실행 당시 Standard API 단가를 사용했다. Sol은 입력
$4/백만 token·출력 $20/백만 token, Terra는 $2·$12, Luna는 $0.20·$1.20이다.
Prompt caching, 세금 및 계정별 조건은 반영하지 않았으므로 OpenAI 청구서 금액과는
차이가 날 수 있다.

### 실측 결과

| 지표 | 기존 시연 구성 | 저비용 구성 | 변화 |
| --- | ---: | ---: | ---: |
| 분류 정확도 | 18/18 (100.000000%) | 18/18 (100.000000%) | 0.000000%p |
| 카드 지시 판정 정확도 | 12/12 (100.000000%) | 12/12 (100.000000%) | 0.000000%p |
| 초안 rule precision / recall / F1 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 0.000000 |
| Q&A case pass | 8/8 (100.000000%) | 8/8 (100.000000%) | 0.000000%p |
| 카드 생성 case pass | 5/5 (100.000000%) | 5/5 (100.000000%) | 0.000000%p |
| 번역 pass | 6/6 (100.000000%) | 6/6 (100.000000%) | 0.000000%p |
| Prompt tokens | 20,675 | 20,675 | 0 (0.000000%) |
| Completion tokens | 5,329 | 3,010 | -2,319 (-43.516607%) |
| 전체 tokens | 26,004 | 23,685 | -2,319 (-8.917859%) |
| 계산 API 비용 | $0.162590 | $0.061493 | -$0.101097 (-62.179101%) |
| 구성별 합산 지연시간 | 101,174.841 ms | 49,217.246 ms | -51,957.595 ms (-51.354264%) |

고정 파일럿에서는 저비용 구성의 품질 회귀가 관찰되지 않았고 비용과 응답시간이 모두
감소했다. 따라서 현재 운영 후보는 분류 Luna 유지, 초안·Q&A Terra, 번역 Luna다.
Terra는 품질과 비용의 균형이 필요한 작업, Luna는 비용 민감한 고빈도 작업에 둔다는
역할 분리다.

전체 원문 출력, task별 판정, token, latency 및 계산 비용은 실행할 때 지정한 로컬
`results/` 경로에 저장된다. 이 디렉터리는 모델 출력·내부 식별자·telemetry가 포함될
수 있고 실행마다 달라지므로 Git에 올리지 않는다. 공유가 필요하면 접근이 제한된 CI
artifact 또는 object storage에 보관한다. 아래 명령은 같은 데이터셋을 다시 실측하며
실행할 때마다 실제 API 비용이 발생한다.

```powershell
poetry run python -m benchmarks model-pilot `
  --dataset benchmarks/datasets/model_selection_pilot.json `
  --output benchmarks/results/model-selection-pilot.json
```

### 해석 범위

이 결과는 모델 라우팅을 빠르게 결정하기 위한 소규모 합성 파일럿이며 서비스 전체의
일반 정확도가 100%라는 뜻은 아니다. Q&A는 검색된 규칙을 입력으로 제공해 답변의
근거 준수만 평가했으므로 embedding 검색 품질은 포함하지 않는다. 자동 승격 정책,
실제 Slack 분포, 장문·모호·적대적 입력도 이번 유료 파일럿의 범위 밖이다. 사업성
검증 단계에서는 개인정보를 제거한 실제 사례를 사람이 라벨링하고, 케이스 수와 반복
실행을 늘려 신뢰구간·P95 latency·건당 비용을 별도로 보고해야 한다.

가격과 모델 포지셔닝의 근거는 OpenAI 공식 문서다.

- https://developers.openai.com/api/docs/models
- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/models/text-embedding-3-small

## 데이터 계약

데이터셋과 모델 출력은 UTF-8 JSONL이다. 지원 task는 다음 여섯 가지다.

- `classification`: `gold.label` / `output.label`
- `drafting`: 정답 규칙의 필수 사실, Scope, evidence / 생성된 규칙 목록
- `promotion`: `gold.promotionType`, `gold.critical` / `output.promotionType`
- `retrieval`: `gold.relevantIds` / `output.rankedIds`
- `qna`: 정답 사실, 금지 사실, citation, escalation 여부
- `cards`: 지시 판정, 목적, 결과물, 기한, 단계, Blank, 긴급도

공통 모델 출력 telemetry 필드:

```json
{
  "latencyMs": 123.4,
  "promptTokens": 100,
  "completionTokens": 20,
  "costUsd": 0.0012
}
```

필드의 전체 예시는 `datasets/example.jsonl`과
`datasets/example_predictions.jsonl`에 있다. 실데이터에는 Slack 원문이나 개인정보를
그대로 커밋하지 않는다. 식별자를 가명화하고 민감한 데이터셋은 Git 외부에 둔다.

## 기본 명령

아래 명령은 `backend/sai`에서 실행한다.

```powershell
poetry run python -m benchmarks validate `
  --dataset benchmarks/datasets/example.jsonl `
  --predictions benchmarks/datasets/example_predictions.jsonl

poetry run python -m benchmarks score `
  --dataset benchmarks/datasets/example.jsonl `
  --predictions benchmarks/datasets/example_predictions.jsonl `
  --output benchmark-report.json
```

## 자동 승격 전후 비교

실제 데이터셋의 promotion case에는 동일한 DB 스냅샷에서 조회 가능한 `entryId`와
사람이 확정한 gold label을 넣는다.

```json
{"caseId":"promotion-001","task":"promotion","input":{"entryId":123,"method":"REPEATED_EVIDENCE"},"gold":{"promotionType":"AUTO_PROMOTED","critical":false}}
```

변경 전 정책은 모든 후보를 검토 대상으로 간주하므로 DB나 외부 호출이 필요 없다.

```powershell
poetry run python -m benchmarks run-promotion `
  --mode legacy `
  --dataset C:/secure/sai-bench.jsonl `
  --output C:/secure/legacy-predictions.jsonl
```

현재 정책 평가는 `evaluate_entry()`만 호출한다. 이 함수는 판정 결과를 DB에 저장하거나
규칙을 확정하지 않는다. 기존 확정 규칙이 있으면 유사도 확인을 위해 OpenAI 임베딩
호출이 발생할 수 있다.

```powershell
poetry run python -m benchmarks run-promotion `
  --mode current `
  --dataset C:/secure/sai-bench.jsonl `
  --output C:/secure/current-predictions.jsonl
```

```powershell
poetry run python -m benchmarks compare `
  --dataset C:/secure/sai-bench.jsonl `
  --baseline C:/secure/legacy-predictions.jsonl `
  --candidate C:/secure/current-predictions.jsonl `
  --output C:/secure/comparison.json
```

`deltaCandidateMinusBaseline`은 항상 현재 값에서 기준값을 뺀 값이다. 따라서 정확도와
Recall의 양수는 개선이지만 latency, failure rate, critical escape의 음수가 개선이다.

## 현재 전체 파이프라인 실행

`run-current`는 데이터셋의 DB 식별자를 이용해 현재 체크아웃의 실제 AI 경로를
실행한다. `--task`를 생략하면 여섯 작업을 모두 실행하고, 반복해서 지정하면 선택한
작업만 실행한다.

```powershell
poetry run python -m benchmarks run-current `
  --dataset C:/secure/sai-bench.jsonl `
  --output C:/secure/current-predictions.jsonl `
  --manifest C:/secure/current-manifest.json
```

```powershell
poetry run python -m benchmarks run-current `
  --task classification `
  --task retrieval `
  --task qna `
  --dataset C:/secure/sai-bench.jsonl `
  --output C:/secure/current-qna-predictions.jsonl `
  --manifest C:/secure/current-qna-manifest.json
```

manifest에는 Git commit, 데이터셋 SHA-256, Python 버전, prompt 버전,
classifier/drafter/translator/answer/embedding 모델명과 자동 승격 정책 버전이 기록된다.

### 실제 서비스 호출과 데이터 안전성

| Task | 호출하는 실제 경로 | DB 변경 |
| --- | --- | --- |
| classification | `sources.classifier._classify_batch` | 없음 |
| drafting | `handbook.drafting._draft_batch` | 전체 case transaction rollback |
| promotion | `handbook.promotion.evaluate_entry` | 없음 |
| retrieval | 질문 embedding + `qna.answering.retrieve` | 없음 |
| qna | `qna.answering.answer_question` | 없음 |
| cards | 지시 판정 + `cards.generation._build_card` | 전체 case transaction rollback |

drafting과 cards는 기존 생성·검증·저장 코드를 실제로 실행한 후 결과를 직렬화하고
트랜잭션을 강제 rollback한다. OpenAI 호출은 외부 부수효과이므로 rollback되지 않으며
실제 API 비용이 발생한다. 운영 DB 대신 복제한 평가 DB 사용을 권장한다.

각 case 실패는 전체 실행을 중단하지 않고 해당 prediction의 `error`에 기록된다.

현재 Q&A 서비스는 token usage를 반환하므로 runner가 Q&A token을 자동 기록한다.
분류·초안·카드 서비스는 반환 계약을 바꾸지 않기 위해 runner JSON에는 latency만
자동 기록하지만, 실제 OpenAI 호출은 공통 로그에 `operation`, `model`, `inputs`,
prompt/completion/total/cached token을 남긴다. 이 작업들의 정확한 token/cost 비교는
같은 benchmark run의 공통 usage 로그를 prediction 결과와 결합해 계산한다.

## 현재 파이프라인 최적화 스위치

다음 설정은 API 계약을 바꾸지 않고 검색 품질·비용·복구력을 조정한다. 값은 환경변수
또는 `secrets.json`에서 덮어쓸 수 있다.

| 설정 | 기본값 | 역할 |
| --- | ---: | --- |
| `SOURCE_LONG_DOCUMENT_CHUNK_CHARS` | 2400 | GitHub/업로드 장문 청크 최대 문자 수 |
| `SOURCE_LONG_DOCUMENT_CHUNK_OVERLAP_CHARS` | 240 | 장문 청크 문맥 중첩 문자 수 |
| `HANDBOOK_HYBRID_RETRIEVAL_ENABLED` | true | 벡터+키워드 혼합 검색 활성화 |
| `HANDBOOK_RETRIEVAL_CANDIDATE_MULTIPLIER` | 3 | 최종 개수 대비 재정렬 후보 배수 |
| `HANDBOOK_RETRIEVAL_VECTOR_WEIGHT` | 0.72 | 혼합 점수에서 벡터 유사도 가중치 |
| `OPENAI_CLASSIFIER_FALLBACK_MODEL` | drafter 모델 | 누락 index만 재판정하는 fallback 모델 |
| `OPENAI_CLASSIFIER_FALLBACK_REASONING_EFFORT` | low | fallback 추론 강도 |

분류와 카드 지시 판정의 fallback은 1차 응답에서 누락된 항목에만 한 번 실행된다.
동일 원문 번역·임베딩은 한 실행 안에서 묶고, 같은 회사의 기존 동일 텍스트 결과도
재사용한다. 긴 Slack 메시지는 작성자·스레드 단위를 보존하기 위해 분할하지 않는다.
PostgreSQL의 하이브리드 키워드 후보 조회는 `pg_trgm` GIN 인덱스를 사용한다.

## 일반 AI 대비 실제 RAG ablation 결과

### 실행 조건

동일한 모델과 Q&A prompt를 고정하고 검색 context만 바꾸어 실제 OpenAI API와 현재
Django/PostgreSQL 검색 코드를 실행했다. 합성 회사에 9개 업무 범주, 5개 프로젝트로
구성한 영문 핸드북 규칙 45개를 만들었다. 각 규칙에는 서로 다른 질문 표현 3개를
사용해 근거 기반 질문 135개를 구성했고, 근거 없음 10개와 Scope 불일치 5개를 더해
총 150개 고유 질문을 평가했다. 150개 질문을 독립 API 호출로 3회 반복했으며,
profile당 retrieval 405건과 Q&A 450건, 총 855건이다.
fixture 생성부터 전체 실행까지 하나의 transaction에서 수행한 뒤 rollback했으므로
운영 DB에는 합성 회사와 규칙이 남지 않는다. 실제 회사 원문은 외부 API에 보내지 않았다.

- Answer: `gpt-5.6-terra`, reasoning effort `medium`
- Embedding: `text-embedding-3-small`
- 검색 k: 5
- 데이터셋 SHA-256:
  `563f3697fd5f7819c1ad5105807a4f3c02a4af70b9e0743d4e4b3bee06557f5a`
- 전체 실행시간: 3,787,735.484 ms(약 63분 8초)
- 실행 실패: 0건

실행 profile의 의미는 다음과 같다.

| Profile | 검색 방식 |
| --- | --- |
| `llm-only` | 검색 context 없이 같은 답변 모델 호출 |
| `structure-dense` | 현재 DB의 확정 핸드북 규칙 대상 dense 검색 |
| `hybrid` | 같은 규칙 대상 vector+lexical 혼합 검색 강제 활성화 |
| `current` | 현재 배포 설정에 따른 실제 검색 경로 |

### 품질 결과

| 지표 | LLM 단독 | Dense | Hybrid | 현재 SAi |
| --- | ---: | ---: | ---: | ---: |
| Retrieval Hit Rate@5 | 0.000000 | 1.000000 | 1.000000 | 1.000000 |
| Retrieval Recall@5 | 0.000000 | 1.000000 | 1.000000 | 1.000000 |
| Retrieval MRR@5 | 0.000000 | 0.996296 | 1.000000 | 1.000000 |
| Retrieval nDCG@5 | 0.000000 | 0.997266 | 1.000000 | 1.000000 |
| Q&A verdict 정확도 | 0.100000 | 1.000000 | 1.000000 | 1.000000 |
| Q&A escalation 정확도 | 0.100000 | 1.000000 | 1.000000 | 1.000000 |
| Q&A required-fact recall | 0.290617 | 0.969877 | 0.972181 | 0.972346 |
| Q&A citation precision | N/A | 1.000000 | 1.000000 | 1.000000 |
| Q&A citation recall | 0.000000 | 1.000000 | 1.000000 | 1.000000 |
| Q&A citation F1 | N/A | 1.000000 | 1.000000 | 1.000000 |

현재 SAi는 LLM 단독 대비 required-fact recall이 `+0.681729`(`+68.1729%p`),
citation recall과 retrieval Hit Rate@5가 각각 `+1.000000`(`+100%p`), verdict와
escalation 정확도가 각각 `+0.900000`(`+90%p`) 개선됐다. LLM 단독의 verdict와
escalation 정확도 0.100000은 근거가 없어야 정답인 15개 질문에서 나온 값이다. 일부
fact 일치는 질문과 응답의 어휘 중복에서 나온 값이며 근거 기반 정답률로 해석하지 않는다.

이 합성 평가셋에서는 Dense 단계에서 retrieval 지표가 이미 포화되어 Hybrid의 추가
검색 품질 상승은 관찰되지 않았다. Dense의 MRR@5는 0.996296이고 Hybrid와 현재
경로는 1.000000이지만, 이번 합성 데이터만으로 Hybrid의 일반적 우위를 주장하지 않는다.
`hybrid`와 `current`는 이번 실행에서 실질적으로 같은 검색 설정이다. 정답 규칙과
citation gold는 모델 출력을 만들기 전에 고정했으며 실행 후 정답을 변경하지 않았다.

### 3회 반복 안정성

| Profile | 1회차 fact recall | 2회차 fact recall | 3회차 fact recall | 최소~최대 |
| --- | ---: | ---: | ---: | ---: |
| `llm-only` | 0.289630 | 0.289630 | 0.292593 | 0.289630~0.292593 |
| `structure-dense` | 0.969877 | 0.969877 | 0.969877 | 0.969877~0.969877 |
| `hybrid` | 0.973827 | 0.975309 | 0.967407 | 0.967407~0.975309 |
| `current` | 0.972840 | 0.971358 | 0.972840 | 0.971358~0.972840 |

Retrieval Hit Rate@5, verdict 정확도, escalation 정확도, citation F1과 실패 건수는
각 profile의 세 반복에서 동일했다. 보고서의 `repeatReports`에는 1회차부터 3회차까지
각각 독립 채점한 전체 지표가 포함된다.

### 지연시간과 비용

| Profile | 실제 API 호출 | 전체 token | 계산 비용 | Retrieval P50 / P95 | Q&A P50 / P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `llm-only` | 450 | 428,257 | $1.25051400 | 0.005 / 0.007 ms | 1,847.5 / 2,294.1 ms |
| `structure-dense` | 1,305 | 524,538 | $1.29480126 | 366.466 / 410.015 ms | 1,770.5 / 2,242.6 ms |
| `hybrid` | 1,305 | 524,350 | $1.29455526 | 373.997 / 421.478 ms | 1,774.5 / 2,216.3 ms |
| `current` | 1,305 | 524,368 | $1.29477126 | 372.102 / 440.654 ms | 1,810.5 / 2,285.2 ms |

합성 규칙 45개의 fixture embedding 3회, 1,135 token, `$0.00002270`을 포함한
ablation 전체 실제 API 호출은 4,368회이고 계산 비용은 `$5.13466448`다. 현재 SAi는
LLM 단독보다 profile당 `$0.04425726`(`3.539126%`) 더 사용했지만, 검색·citation
검증으로 위 근거 기반 품질을 확보했다. 현재 profile의 Q&A P50은 LLM 단독보다
37.0 ms 낮았으므로 이 실행에서는 답변 생성 지연 회귀가 관찰되지 않았고, 대신 질문
embedding을 포함한 검색 단계 P50 372.102 ms가 추가된다. profile별 응답 길이가 달라
동일한 검색 profile 사이에도 token이 소폭 다를 수 있다.

### 재현 명령과 결과 보관

```powershell
poetry run python -m benchmarks synthetic-rag `
  --output-dir benchmarks/results `
  --repeats 3
```

실행 결과에는 profile별 JSONL, manifest, 합산 보고서가 생성된다. `results/`는 실제
모델 출력과 telemetry를 포함하므로 `.gitignore`에 등록했으며 저장소에는 올리지 않는다.
요약 결과와 재현 조건은 이 README에 유지한다.

이번 실측은 45개 규칙과 150개 고유 질문을 세 번 실행해 소규모 파일럿보다 반복성과
범주 다양성을 높였지만, 여전히 영문 합성 데이터다. 확정 핸드북 규칙 검색과 Q&A
품질은 검증하지만 RawDocumentChunk fixture를
사용하지 않았으므로 semantic chunking의 순수 개선량, 개선 전 커밋의 naive RAG,
분류 fallback, drafting evidence 병합, 번역·임베딩 중복 제거 효과는 포함하지 않는다.
그 항목의 전후 수치를 주장하려면 익명화한 동일 원문 fixture를 개선 전·후 worktree에
복제하고 같은 manifest 조건으로 별도 실행해야 한다.

## 평가 설계의 신뢰성 근거

이 패키지의 **평가 방법**은 정보 검색과 RAG 분야에서 반복 사용되는 지표와 분리 평가
원칙을 따른다. 위 결과는 고정 합성 데이터셋에서 실제 서비스 코드와 API를 실행해 얻은
재현 가능한 측정값이다. 다만 합성 데이터의 내부 타당성과 실제 고객 분포에 대한 외적
타당성은 구분해야 하며, 서비스 전체의 일반 정확도로 확대 해석하지 않는다.

- [BEIR](https://arxiv.org/abs/2104.08663)는 서로 다른 검색 방식과 도메인을 비교하는
  정보 검색 benchmark다. 이 패키지는 같은 계열의 Recall@k, MRR@k, nDCG@k와
  lexical/dense/hybrid baseline 비교를 사용한다.
- [RAGBench](https://arxiv.org/abs/2407.11005)는 RAG를 retrieval과 generation 품질로
  나눠 평가하고 산업 도메인의 근거 기반 응답을 다룬다. 이 패키지도 검색 적중률과
  Q&A required-fact, forbidden-fact, citation 지표를 별도로 계산한다.
- [ARES](https://arxiv.org/abs/2311.09476)는 context relevance, answer faithfulness,
  answer relevance를 평가하고 소량의 사람 라벨을 함께 사용하는 방법을 제안한다.
  현재 scorer는 재현 가능한 문자열·citation 채점을 기본으로 두고, 최종 사업성 검증
  단계에서는 사람 검수 표본 또는 별도 judge를 추가할 수 있게 prediction 생성을
  scoring과 분리했다.

사업성 검증용 production benchmark로 확장할 때는 다음 조건을 추가로 만족한다.

1. 익명화한 실제 서비스 사례에 사람이 gold rule, 관련 문서, 답변 사실과 citation을
   라벨링한다.
2. 모든 profile에 동일한 DB snapshot, 모델 snapshot, prompt, 질문 순서와 검색 k를
   사용한다.
3. manifest의 Git commit, dataset SHA-256, model ID, prompt/policy version을 함께
   보관한다.
4. 비결정적인 생성 지표는 최소 3회 반복하고 평균과 95% 신뢰구간을 보고한다.
5. raw result는 Git이 아닌 제한된 artifact storage에 보관하되 README나 이슈에는
   dataset hash, 실행 명령, 요약 지표와 artifact 위치를 남긴다.

공개 BEIR/RAGBench 점수를 SAi 점수로 그대로 가져오지는 않는다. 이 서비스의 한국어
업무 규칙, Scope, Owner escalation, 회사별 권한 경계는 공개 benchmark와 분포가 다르기
때문이다. 공개 연구는 **지표와 평가 절차의 근거**로 사용하고, 최종 수치는 SAi의 실제
익명화 평가셋에서 측정한다.

### Task별 입력 ID

실행기는 자유 텍스트만 흉내 내지 않고 실제 Scope, 부모 thread, Identity, 기존 규칙과
벡터 인덱스를 사용하기 위해 평가 DB의 행을 참조한다.

```text
classification: input.documentId
drafting:       input.companyId, input.scopeId, input.documentIds
promotion:      input.entryId, method 및 Owner 판정 플래그
retrieval:      input.companyId, input.query, 선택 input.scopeId
qna:            input.companyId, input.question, 선택 input.scopeId/lang
cards:          input.documentId
```

검색 정답 ID와 출력 ID는 `handbook:123`, `chunk:456`, `document:789` 형식을 사용한다.
두 평가 DB는 같은 fixture에서 복제해 PK가 동일해야 한다.

### 정답 라벨 예시

Drafting은 모델의 표현 차이를 허용하기 위해 문장 전체 exact match 대신 각 규칙의
`requiredFacts`가 생성된 제목/본문에 모두 포함되는지 측정한다.

```json
{
  "caseId": "drafting-001",
  "task": "drafting",
  "input": {"companyId": 1, "scopeId": 2, "documentIds": [10, 11, 12]},
  "gold": {
    "rules": [{
      "requiredFacts": ["금요일 오후 배포 금지"],
      "scopeId": 2,
      "evidenceIds": ["document:10", "document:11", "document:12"]
    }],
    "forbiddenFacts": ["금요일 배포 허용"]
  }
}
```

Card는 지시 여부와 구조화된 필드별 사실을 평가한다.

```json
{
  "caseId": "cards-001",
  "task": "cards",
  "input": {"documentId": 20},
  "gold": {
    "isInstruction": true,
    "purposeFacts": ["배포 로그 확인"],
    "deliverableFacts": ["확인 결과"],
    "deadlineFacts": ["오늘"],
    "stepFacts": ["로그 확인"],
    "blankFacts": [],
    "forbiddenFacts": [],
    "urgency": "SOON",
    "expectedDeadlineInferred": false
  }
}
```

## 다른 커밋의 전체 파이프라인 비교

자동 승격 외 분류·검색·Q&A는 각 커밋을 별도 worktree에서 실행하고 공통 출력 계약으로
결과를 저장한다. 운영 DB를 공유하지 말고 동일 스냅샷에서 복제한 평가 DB를 사용한다.

```powershell
git worktree add ../backend-baseline 76b6cd2
git worktree add ../backend-candidate 180acac
```

평가 패키지가 도입되기 전 커밋에는 `benchmarks` 디렉터리가 없으므로 평가 전용
커밋에서 해당 디렉터리만 baseline worktree로 가져온다. 이는 baseline의 운영 코드를
바꾸지 않고 실행기만 얹는 방식이다.

```powershell
cd ../backend-baseline
git checkout <benchmark-package-commit> -- sai/benchmarks
```

과거 private 함수의 시그니처가 현재와 다른 커밋은 작은 commit 전용 adapter가 필요할
수 있다. 공통 JSONL 데이터/출력 계약과 scorer는 그대로 사용한다.

각 worktree의 실제 파이프라인 출력은 동일한 `caseId`를 가진 prediction JSONL로
내보낸 뒤, 현재 패키지의 `compare` 명령으로 비교한다. 모델 효과와 코드 효과를
분리하려면 다음 두 실험을 따로 수행한다.

1. 같은 모델 설정으로 두 커밋 비교
2. 각 커밋의 실제 운영 모델 설정으로 제품 버전 비교

## 측정 지표

- Classification: accuracy, macro F1, label별 precision/recall/F1
- Drafting: rule precision/recall/F1, required-fact recall, Scope accuracy,
  evidence recall, forbidden-fact violation
- Promotion: accuracy, auto coverage/precision, safe-auto recall, manual-review rate,
  critical escape rate
- Retrieval: Hit Rate, Recall, MRR, nDCG
- Q&A: escalation accuracy, required-fact recall, forbidden-fact violation,
  citation precision/recall/F1
- Cards: instruction accuracy, card generation rate, 필드별 fact recall,
  urgency/deadline-inference accuracy, forbidden-fact violation
- Operations: failure rate, P50/P95 latency, token 합계, 비용 합계

Q&A의 사실 평가는 의존성 없는 재현성을 위해 정규화된 문자열 포함 여부를 사용한다.
의미 기반 faithfulness 평가는 RAGAs/ARES 같은 별도 LLM judge 단계와 사람 표본 검수를
추가해야 한다.

## 테스트

```powershell
poetry run python -m unittest benchmarks.tests -v
```

이 테스트는 외부 AI API나 Django DB를 사용하지 않는다. 라이브 AI 벤치마크는 비용과
비결정성이 있으므로 일반 CI 단위 테스트에 포함하지 않는다.
