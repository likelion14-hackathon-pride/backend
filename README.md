# SAI Backend 


| 항목 | 버전 |
| --- | --- |
| Python | 3.12 이상 |
| Poetry | 2.0 이상 |
| Docker Desktop | DB 컨테이너 실행용 |
| DataBase | PostgreSQL 16 + pgvector | 

---

## 로컬 실행

### 1. DB 컨테이너 띄우기

프로젝트 루트에서 실행합니다.

```bash
docker compose up -d
```

`STATUS`가 `healthy`인지 확인

```bash
docker compose ps
```

### 2. 의존성 설치

`sai/` 디렉터리에서 실행합니다.


```bash
poetry install
```

### 3. 마이그레이션

```bash
poetry run python manage.py migrate
```

pgvector 확장(`CREATE EXTENSION vector`)은 handbook 마이그레이션의 `VectorExtension()`이 자동으로 실행. 


### 4. 서버 실행

```bash
poetry run python manage.py runserver
```

| 주소 | 설명 |
| --- | --- |
| http://127.0.0.1:8000/swagger/ | API 문서 |
| http://127.0.0.1:8000/admin/ | Django 관리자 |

관리자 계정이 필요하면:

```bash
poetry run python manage.py createsuperuser
```

---

## DB 초기화

```bash
docker compose down -v
```

```bash
docker compose up -d
```

```bash
poetry run python manage.py migrate
```

## DB 상태 확인

```bash
docker exec -it sai-db psql -U sai -d sai
```

```sql
SELECT extname FROM pg_extension WHERE extname = 'vector';
\d handbook_entry
```

`embedding_ko | vector(1024)`와 `hb_emb_ko_idx ... hnsw`가 보이면 정상.

