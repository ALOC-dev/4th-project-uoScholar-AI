# 4rd-project-uoScholar-AI (Python)

## 프로젝트 소개
서울시립대 재학생들은 복수전공, 선후수 체계, 수강 제한, 학점 이수 기준 등 학사 관련 정보를 주로 공지사항에서 확인해야 하지만, 공지의 **가독성과 접근성이 떨어져** 원하는 정보를 빠르게 찾기 어렵습니다.

이를 해결하기 위해 저희 팀은 필요한 공지를 쉽고 정확하게 제공하는 **RAG 기반 챗봇 "UoScholar"**를 개발하였습니다.

UoScholar는 서울시립대학교 공지사항 데이터를 크롤링하고, 벡터 임베딩을 통해 Pinecone DB에 저장합니다. 이후 **Retrieval-Augmented Generation (RAG)** 기법과 **Cohere Reranker**를 활용하여 학생들의 질문에 가장 적합한 공지사항을 찾아내고, LLM 기반으로 자연스러운 답변을 제공합니다.

---

## 시스템 아키텍처

```
공지 크롤링 → MySQL → 벡터 인덱싱 → Pinecone → RAG 챗봇 → 사용자
(Playwright)         (한국어 임베딩)        (Cohere Reranker)
```

---

## 주요 기능

### 1. 공지사항 크롤링 (`notice_crawler.py`)
- BeautifulSoup으로 목록 페이지에서 최신 공지 URL 수집
- Playwright로 공지 페이지 전체 스크린샷 캡처
- GPT-4o Vision API로 **HTML 텍스트 + 이미지** 멀티모달 요약
- MySQL에 공지 메타데이터 및 요약 저장

### 2. 벡터 인덱싱 (`index.py`)
- MySQL에서 공지 데이터 읽기
- LangChain으로 900자 단위 청킹 (오버랩 150자)
- 한국어 임베딩 모델(`jhgan/ko-sroberta-multitask`)로 768차원 벡터 생성
- Pinecone에 title/summary 타입 구분하여 저장

### 3. RAG 챗봇 (`chatbot.py`)
- **Cohere Reranker**로 검색 결과 재정렬
- **세션 기반 대화 관리** (대화 히스토리 추적)
- **요구사항 자동 추출** (LLM이 키워드, 카테고리 등 분석)
- **명확화 질문 생성** (검색 실패 시 추가 정보 요청)

#### 📌 대화형 챗봇 플로우

```
사용자 질문
    │
    ▼
┌─────────────────────────┐
│ 1. 요구사항 추출 (LLM)  │  ← JSON 형식: category, keywords, target_audience 등
└───────────┬─────────────┘
            │
            ▼
      ┌────────────┐
      │ 공지 관련? │
      └─────┬──────┘
            │
    ┌───────┴────────┐
    │                │
   YES              NO
    │                │
    ▼                ▼
┌───────────┐   ┌────────────┐
│ 벡터 검색 │   │ 일반 대화  │
│ + Rerank  │   │ 응답 생성  │
└─────┬─────┘   └────────────┘
      │
 ┌────┴─────┐
 │          │
발견       미발견
 │          │
 ▼          ▼
최종추천   명확화질문
```

**특징**:
- **세션 기반 대화 관리**: `session_id`로 대화 히스토리 추적
- **공지 관련 여부 판단**: LLM이 질문을 분석하여 공지 검색이 필요한지 자동 판단
- **명확화 질문**: 검색 결과가 부족하면 추가 정보를 자연스럽게 요청
- **날짜 인식**: "최근", "이번 달" 등 상대적 날짜 표현 처리 (현재 날짜 기준)

**API 엔드포인트**:
```bash
POST /chat
{
  "query": "장학금 신청 일정 알려줘",
  "session_id": "user123"
}
```

**응답 예시**:
```json
{
  "response": "제가 찾아낸 공지는 '2024학년도 1학기 교내장학금 신청 안내'입니다...",
  "turn": 1,
  "completed": false,
  "recommended_notice": {
    "title": "2024학년도 1학기 교내장학금 신청 안내",
    "link": "https://www.uos.ac.kr/...",
    "posted_date": "2024-03-01",
    "department": "학생처",
    "score": 0.87
  }
}
```

---

## 기술 스택

### Backend & AI
- **Python 3.10+**: 전체 시스템 구현 언어
- **FastAPI**: REST API 서버 (비동기 처리)
- **LangChain**: RAG 파이프라인 구축 (청킹, 벡터 검색)
- **OpenAI GPT-4o / GPT-4o-mini**: 멀티모달 요약, 대화 생성, 요구사항 분석
- **Cohere Rerank**: 검색 결과 재정렬
- **SentenceTransformers**: 한국어 임베딩 (`jhgan/ko-sroberta-multitask`)

### Crawling & Processing
- **Requests + BeautifulSoup4**: HTML 파싱
- **Playwright**: 동적 페이지 렌더링 + 이미지 캡처
- **Pillow**: 이미지 처리

### Database & Vector Store
- **MySQL**: 공지 원본 데이터 저장
- **Pinecone**: 벡터 인덱스 (코사인 유사도 검색)

### DevOps
- **python-dotenv**: 환경 변수 관리
- **logging**: 로그 추적

---

## 설치 및 실행

### 1. 환경 설정

```bash
# Python 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt

# Playwright 브라우저 설치
playwright install chromium
```

### 2. 환경 변수 설정 (`.env`)

```env
# OpenAI
OPENAI_API_KEY=sk-...

# MySQL
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=yourpassword
DB_NAME=uos_notices

# Pinecone
PINECONE_API_KEY=...
PINECONE_INDEX=uos-notices
PINECONE_NAMESPACE=  # 비워두면 기본 네임스페이스 사용
PINECONE_CLOUD=aws
PINECONE_REGION=us-east-1

# Embedding
EMBED_TYPE=korean
EMBED_MODEL=jhgan/ko-sroberta-multitask

# Cohere (Reranker)
COHERE_API_KEY=...

# Chat Model
CHAT_MODEL=gpt-4o-mini
```


### 3. 실행 순서

```bash
# 1단계: 공지 크롤링 (MySQL에 저장)
python scripts/run_crawler.py

# 2단계: 벡터 인덱싱 (Pinecone에 업로드)
python scripts/run_indexer.py

# 3단계: 챗봇 서버 실행
python -m uvicorn src.uosai.chat.chatbot:app --host 0.0.0.0 --port 8000
```

---

## 참고
- [LangChain 공식 문서](https://python.langchain.com/)
- [Pinecone 공식 문서](https://docs.pinecone.io/)
- [Cohere Rerank](https://docs.cohere.com/docs/reranking)

