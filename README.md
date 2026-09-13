# SAi Backend

SAi는 Slack, GitHub, 업로드 문서에 흩어진 업무 지식을 수집해 근거가 추적되는 핸드북으로 만들고, 확정된 규칙을 기반으로 답변과 실행 카드를 제공하는 Django REST API 서버입니다. 
명확한 결정은 자동화하고 위험·충돌·근거 부족 같은 예외만 Owner가 감독하는 Human-on-the-Loop 구조를 지향합니다.

## 핵심 기능

- JWT 인증과 회사별 `OWNER`/`MEMBER` 권한 분리
- Slack, GitHub, 로컬 파일 수집과 비밀정보 제거
- 원문 분류, 구조 보존 청킹, 번역 및 임베딩
- 원문 근거와 Scope를 유지하는 핸드북 초안 생성
- Owner 결정 및 반복 근거 기반 자동 승격
- 위험·충돌·근거 부족 규칙의 개별 검토 분류
- Dense+lexical 하이브리드 검색 기반 Ask SAi
- 답변 citation 검증과 Slack Owner 에스컬레이션
- 업무 지시의 목적·결과물·기한·단계 카드화
- 독립 실행 가능한 AI 품질·비용·지연시간 벤치마크

## 기술 스택

| 영역 | 기술 |
| --- | --- |
| Backend | Python 3.12+, Django 6.1, Django REST Framework |
| Auth | SimpleJWT |
| Database | PostgreSQL 16, pgvector |
| Search | HNSW cosine search, `pg_trgm`, hybrid reranking |
| AI | 역할별로 설정 가능한 OpenAI 모델 |
| Embedding | `text-embedding-3-small`, 1,536 dimensions |
| Integration | Slack, GitHub, Local File, AWS S3 |
| Runtime | Poetry, Gunicorn, background worker |
| Deployment | GitHub Actions, AWS EC2 |

## 시스템 구조

![alt text](/sai/images/image.png)


## 지식 생성 파이프라인

### 1. 수집과 전처리

```text
Connection
  → SourceItem
  → RawDocument
  → Secret Redaction
  → AI Classification
  → Boundary-aware Chunking
  → Translation / Embedding
  → Handbook Draft / Instruction Card
```

- Slack 메시지는 작성자와 thread 문맥 보존을 위해 한 메시지를 임의 분할하지 않습니다.
- 긴 GitHub·업로드 문서만 문단, 줄, 문장, 공백 순서로 경계를 찾아 분할합니다.
- 청크 본문이 변하지 않으면 기존 번역과 임베딩을 유지합니다.
- 같은 회사에 동일한 원문이 이미 번역돼 있으면 저장된 결과를 재사용합니다.
- AI 응답에서 누락된 분류 index만 fallback 모델로 한 번 재판정합니다.

### 2. 핸드북 Scope와 Evidence

규칙은 회사 공통 또는 프로젝트 Scope에 속하며 생성 근거인 원문과 연결됩니다.

```text
CompanyScope
├── COMPANY: Company / People / Product Engineering / Security
└── PROJECT: 개별 프로젝트
```

프로젝트 질문은 회사 공통 Scope와 선택한 프로젝트 Scope만 함께 검색합니다. 다른
프로젝트 규칙은 검색하지 않습니다. 규칙과 답변의 evidence에는 원문, 발화자, 위치,
발생 시각과 permalink를 유지합니다.

## 핸드북 자동 승격

`status`는 규칙의 실제 승인·활성 상태이고 `promotionType`은 자동화 정책이 판단한
처리 경로입니다. 두 상태는 의도적으로 분리되어 있습니다.

| `promotionType` | 의미 | 처리 방식 |
| --- | --- | --- |
| `AUTO_PROMOTED` | 정책 조건을 모두 만족 | 기존 확정 함수로 자동 승인·번역·임베딩 |
| `PENDING_REVIEW` | 일반 저위험 후보지만 자동 승격 기준 미달 | Owner 일괄 승인·거절 가능 |
| `MANUAL_REQUIRED` | 위험·충돌·근거 또는 Scope 문제 | 개별 검토만 허용 |

### Owner 답변 자동 승격

일반 Slack 대화가 아니라 SAi가 만든 에스컬레이션에 대한 실제 Owner 답변만
대상입니다. 답변과 적용 Scope가 확정되고, 내용이 비어 있지 않으며, 위험·무효 근거나
기존 확정 규칙과의 충돌이 없을 때 자동 승격합니다.

### 반복 근거 자동 승격

다음 조건을 모두 만족하는 저위험 프로젝트 규칙만 자동 승격합니다.

- 서로 다른 원문 근거 3개 이상
- 최근 30일 이내 근거 1개 이상
- 모든 원문의 Scope가 같은 프로젝트로 확정
- AI 추론 가능성이 낮은 `HIGH` confidence 규칙
- 위험 키워드, 무효 출처, 기존 규칙 중복·충돌 없음
- 회사 공통 규칙이 아님

`3개`, `30일`, 유사도 기준과 정책 버전은 Django 설정으로 관리하며 각 판단 결과에
사용한 정책 버전을 저장합니다.

### 반드시 개별 검토하는 경우

- 회사 설정 위험 키워드 또는 보안·개인정보·결제·배포·인사 영역
- Scope 미확정
- 원문 근거 없음, 삭제되거나 연결이 끊긴 출처
- 회사 전체에 적용되는 반복 근거 규칙
- 기존 확정 규칙과의 중복 또는 잠재적 충돌
- 유사도 검색이나 외부 API 실패
- 판단할 수 없는 보수적 fallback 상황

자동 승격은 수동 승인과 같은 `mark_confirmed()` 및 `finalize_entries()` 경로를
사용합니다. Worker 재시도 시 이미 자동 승격된 항목을 다시 확정하지 않습니다. 번역이나
임베딩이 실패하면 해당 완료 시각이 비어 있어 이후 동일 함수 호출로 이어서 처리할 수
있습니다.

### 승인 상태와 승격 유형

프론트엔드는 현재 검토 상태와 과거 자동화 경로를 혼동하면 안 됩니다.

| 필드 | 값 |
| --- | --- |
| `status` | `DRAFT`, `CONFIRMED`, `BLANK`, `ARCHIVED` |
| `reviewStatus` | `PENDING`, `APPROVED`, `REJECTED`, `HELD` |
| `promotionType` | `AUTO_PROMOTED`, `PENDING_REVIEW`, `MANUAL_REQUIRED` |

예를 들어 `reviewStatus=APPROVED`와 `promotionType=PENDING_REVIEW`가 함께 있으면
현재 승인된 규칙이 수동 검토 경로를 거쳤다는 뜻입니다. 검토 대기 목록은 반드시
`reviewStatus=PENDING`으로 조회해야 하며 `promotionType=PENDING_REVIEW`만으로 현재
대기 상태를 판단하면 안 됩니다.

## Ask SAi와 에스컬레이션

```text
Question
  → Question Embedding
  → Company / Project Scope Filter
  → Dense + Lexical Candidate Search
  → Hybrid Reranking
  → Grounded Answer
  → Citation Validation
```

| Verdict | 의미 |
| --- | --- |
| `GROUNDED` | 확정 핸드북 규칙으로 답변 가능 |
| `GROUNDED_BY_CASES` | 과거 사내 사례로 제한적 답변 가능 |
| `NO_SOURCE` | 답변할 사내 근거 없음 |
| `NEEDS_DECISION` | 새로운 Owner 결정 필요 |
| `OUT_OF_SCOPE` | 회사 업무 범위 밖 질문 |

`NO_SOURCE`와 `NEEDS_DECISION`은 내용을 만들어내지 않고 Owner에게 보낼 질문 초안을
만듭니다. Slack webhook은 빠르게 수신만 하고 전송, 답변 확인, 핸드북 승격은 worker가
처리합니다.

## Instruction Card

업무 대화에서 실행 지시를 판별해 다음 구조로 저장합니다.

- 목적과 결과물
- 기한과 긴급도
- 관련 핸드북 규칙
- 실행 단계
- 확인이 필요한 `Blank`

원문이나 확정 규칙에서 확인할 수 없는 내용은 추측하지 않고 `Blank`로 남깁니다.

## 주요 Handbook API

전체 명세와 request/response schema는 `/swagger/`에서 확인합니다.

| Method | Endpoint | 용도 |
| --- | --- | --- |
| `GET` | `/api/companies/{companyId}/handbook/entries` | 목록 및 상태·Scope 필터 |
| `POST` | `/api/companies/{companyId}/handbook/entries` | Owner 직접 규칙 등록 |
| `GET/PATCH/DELETE` | `/api/companies/{companyId}/handbook/entries/{entryId}` | 상세·수정·비활성화 |
| `GET` | `/api/companies/{companyId}/handbook/entries/{entryId}/evidence` | 원문 근거 조회 |
| `POST` | `/api/companies/{companyId}/handbook/entries/{entryId}/review` | 개별 승인·거절·보류 |
| `POST` | `/api/companies/{companyId}/handbook/entries/review-all` | 일괄 승인·거절 |
| `GET` | `/api/companies/{companyId}/handbook/scopes` | 회사·프로젝트 Scope 조회 |
| `POST` | `/api/companies/{companyId}/ask` | 근거 기반 질문 |
| `GET/POST` | `/api/companies/{companyId}/questions` | Owner 에스컬레이션 |

일괄 검토는 같은 회사의 `PENDING_REVIEW` 항목만 처리합니다. `MANUAL_REQUIRED`는
건너뛰며, 다른 회사 ID가 하나라도 포함되면 전체 요청을 `403`으로 차단합니다. 이미
처리된 ID와 중복 ID는 `results`와 `skipped`에 이유를 담아 반환합니다.

핸드북 목록 응답에는 기존 필드와 함께 다음 자동화 정보가 포함됩니다.

```text
promotionType, isAutoPromoted, autoPromotionMethod, promotionReason,
evidenceCount, riskKeywords, hasConflict, hasSimilarRule,
similarEntryId, similarityScore, autoPromotedAt, promotionPolicyVersion
```

## AI 벤치마크

`sai/benchmarks`는 운영 Django 앱과 분리된 오프라인 평가 패키지입니다. 실제 서비스
함수를 호출하지만 평가 fixture는 transaction rollback하며 일반 CI 테스트는 외부 API를
호출하지 않습니다.

### 실측 RAG ablation

- 규칙 45개, 업무 범주 9개, 프로젝트 5개
- 근거 질문 135개와 미지원·Scope 불일치 질문 15개
- 총 150개 고유 질문을 3회 반복
- profile당 retrieval 405건 + Q&A 450건
- 전체 API 호출 4,368회, 총 2,002,648 token
- 계산 비용 `$5.13466448`, 실행 실패 0건

| 지표 | LLM 단독 | Dense | Hybrid | 현재 SAi |
| --- | ---: | ---: | ---: | ---: |
| Retrieval Hit Rate@5 | 0.000000 | 1.000000 | 1.000000 | 1.000000 |
| Retrieval MRR@5 | 0.000000 | 0.996296 | 1.000000 | 1.000000 |
| Q&A verdict 정확도 | 0.100000 | 1.000000 | 1.000000 | 1.000000 |
| Escalation 정확도 | 0.100000 | 1.000000 | 1.000000 | 1.000000 |
| Required-fact recall | 0.290617 | 0.969877 | 0.972181 | 0.972346 |
| Citation recall | 0.000000 | 1.000000 | 1.000000 | 1.000000 |
| Citation F1 | N/A | 1.000000 | 1.000000 | 1.000000 |

현재 파이프라인의 3회 required-fact recall은 `0.972840 / 0.971358 / 0.972840`으로
관찰됐습니다. 현재 profile의 계산 비용은 `$1.29477126`으로 LLM 단독보다
`$0.04425726`(`3.539126%`) 높았습니다. 검색 embedding 비용이 추가되는 대신 근거
검색, citation 및 미지원 질문 escalation 품질을 확보했습니다.

모델 라우팅 파일럿과 RAG ablation을 합친 실제 API 호출은 4,400회, 계산 비용은
`$5.35732368`입니다. 비용은 API usage와 실행 당시 Standard 단가로 계산한 값이며 세금,
prompt caching과 계정별 청구 조건에 따라 실제 청구서와 소수점 차이가 날 수 있습니다.

이 결과는 영문 합성 데이터에서 측정한 내부 비교입니다. 실제 고객 전체의 일반 정확도나
원문 청킹의 단독 개선량으로 확대 해석하지 않습니다. 평가 설계, 모델 선정 파일럿,
재현 명령과 연구 근거는 [벤치마크 최종 리포트](sai/benchmarks/README.md)를 참고하세요.

## 프로젝트 구조

```text
backend/
├── .github/workflows/  # EC2 배포
├── docker-compose.yml  # 로컬 PostgreSQL + pgvector
├── README.md
└── sai/
    ├── config/         # Django·AI·정책 설정
    ├── accounts/       # 인증, 사용자, Membership
    ├── companies/      # 회사, 대시보드, 설정
    ├── onboarding/     # 초기 조직 규칙
    ├── sources/        # 수집, 분류, 청킹, 번역, 임베딩, worker
    ├── handbook/       # 규칙, Scope, Evidence, 승격, 검색
    ├── qna/            # Ask SAi, Citation, Escalation
    ├── cards/          # Instruction Card와 Task
    ├── policy/         # 회사 위험 키워드
    └── benchmarks/     # 오프라인 평가 패키지와 최종 리포트
```

## 로컬 실행

### 1. PostgreSQL 실행

`backend` 디렉터리에서 실행합니다.

```powershell
docker compose up -d
```

### 2. 의존성 설치와 마이그레이션

```powershell
cd sai
poetry install
poetry run python manage.py migrate
```

설정은 환경변수를 우선 사용하고 없으면 Git에서 제외된 `sai/secrets.json`을 읽습니다.
운영 키나 자격증명을 저장소에 커밋하지 마세요.

### 3. API 서버와 worker 실행

```powershell
poetry run python manage.py runserver
```

별도 터미널에서:

```powershell
poetry run python manage.py run_jobs
```

| 주소 | 용도 |
| --- | --- |
| `/health/` | 상태 확인 |
| `/swagger/` | Swagger API 문서 |
| `/admin/` | Django Admin |

worker는 수집, 분류, 청킹, 번역, 임베딩, 핸드북 초안, 카드 생성과 Slack 답변
후처리를 담당합니다. 운영 환경에서 worker가 중지되면 ingestion job이 `QUEUED`에
머무를 수 있습니다.

## 주요 AI·정책 설정

| 설정 | 기본값 | 역할 |
| --- | ---: | --- |
| `SOURCE_LONG_DOCUMENT_CHUNK_CHARS` | `2400` | 긴 문서 청크 최대 문자 수 |
| `SOURCE_LONG_DOCUMENT_CHUNK_OVERLAP_CHARS` | `240` | 인접 청크 문맥 중첩 |
| `HANDBOOK_HYBRID_RETRIEVAL_ENABLED` | `true` | Dense+lexical 검색 활성화 |
| `HANDBOOK_RETRIEVAL_CANDIDATE_MULTIPLIER` | `3` | 최종 결과 대비 후보 배수 |
| `HANDBOOK_RETRIEVAL_VECTOR_WEIGHT` | `0.72` | 하이브리드 점수의 벡터 가중치 |
| `HANDBOOK_PROMOTION_POLICY_VERSION` | `handbook-promotion-v1` | 자동 승격 정책 버전 |
| `HANDBOOK_AUTO_PROMOTION_MIN_EVIDENCE` | `3` | 반복 근거 최소 개수 |
| `HANDBOOK_AUTO_PROMOTION_RECENT_DAYS` | `30` | 최근 근거 인정 기간 |
| `OPENAI_TIMEOUT` | `30` | OpenAI 호출 timeout(초) |
| `OPENAI_MAX_RETRIES` | `1` | OpenAI SDK 최대 retry |

분류, 초안, 번역, 답변, fallback과 embedding 모델은 각각 별도 환경 설정으로 교체할
수 있습니다. 실제 키와 운영 모델명은 환경변수 또는 비공개 secret에서 관리합니다.

## 테스트와 검사

```powershell
poetry run python -m unittest benchmarks.tests -v
poetry run python manage.py test
poetry run python manage.py makemigrations --check --dry-run
poetry run python manage.py check
```

최근 검증 결과:

- 벤치마크 단위 테스트: 12개 통과
- 전체 백엔드 테스트: 898개 통과
- 마이그레이션 누락: 없음
- Django system check: 이상 없음

라이브 벤치마크는 실제 API 비용과 비결정성이 있으므로 일반 테스트에 포함하지
않습니다. 재실행 전 예상 호출 규모와 평가 DB를 확인하세요.

## 배포

현재 `.github/workflows/deploy.yml`은 `main` push 또는 수동 실행으로 시작하며, EC2의
`~/backend`에서 `develop` 브랜치를 pull합니다. 배포 전 두 브랜치의 대상 커밋 관계를
반드시 확인해야 합니다.

```text
GitHub Actions
  → EC2 SSH
  → git pull origin develop
  → poetry install --no-root
  → migrate --noinput
  → collectstatic --noinput
  → restart gunicorn / sai-worker
  → health check
```

운영 배포에는 DB 백업, 마이그레이션 확인과 worker 상태 점검이 필요합니다. 기존 승인
항목은 `reviewStatus=APPROVED`가 현재 상태이며, 새로 추가된 `promotionType`의 기본값만
보고 검토 대기로 판단하거나 기존 규칙을 `AUTO_PROMOTED`로 일괄 변경하면 안 됩니다.

## 설계 원칙

- **Grounded First**: 사내 근거가 있을 때만 답변합니다.
- **Conservative Automation**: 판단할 수 없으면 자동 승격하지 않습니다.
- **Human-on-the-Loop**: 반복 작업은 자동화하고 예외와 고위험 결정은 Owner가 감독합니다.
- **Scope Isolation**: 회사와 프로젝트 지식을 분리합니다.
- **Traceable Evidence**: 규칙, 답변과 승격 판단의 근거를 보존합니다.
- **Idempotent Processing**: worker 재시도가 중복 승격과 중복 외부 호출을 만들지 않게 합니다.
- **Backward-Compatible API**: 기존 응답 필드를 유지하며 새 자동화 정보를 추가합니다.
- **Recoverable State**: 물리 삭제보다 보관·비활성화와 revision 이력을 우선합니다.
- **Measured Improvement**: 품질, 비용, 지연시간과 실패율을 같은 데이터셋으로 비교합니다.
