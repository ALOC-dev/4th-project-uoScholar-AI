# 3rd-project-uoScholar-AI (Python)

## 프로젝트 소개
서울시립대 재학생들은 복수전공, 선후수 체계, 수강 제한, 학점 이수 기준 등 학사 관련 정보를 주로 공지사항에서 확인해야 하지만, 공지의 **가독성과 접근성이 떨어져** 원하는 정보를 빠르게 찾기 어렵습니다.  
이를 해결하기 위해 저희 팀은 필요한 공지를 쉽고 정확하게 제공하는 챗봇 어플리케이션 "**UoScholar**"를 개발하였습니다.

UoScholar는 서울시립대학교 공지사항 데이터를 크롤링하고, 임베딩을 통해 벡터로 DB에 저장합니다. 이후 LLM 기반 프롬프트 엔지니어링을 통해 학생들의 질문에 맞는 공지사항을 찾아내고, 이를 포함한 자연어 답변을 제공합니다.

---

## 파이프라인

### 1. 공지사항 크롤링 및 PDF 추출
1. **공지 HTML 수집 (Requests + BeautifulSoup)**  
   - 공지 URL(`list_id`, `seq`)을 요청하고, 제목·부서·작성일·게시물 번호 등을 파싱  

2. **PDF 변환 (Playwright)**  
   - Chromium 기반으로 HTML 페이지를 PDF로 저장  
   - 로딩 안정화(`domcontentloaded → networkidle → 추가 대기`) 후 추출  
   - 실패 시 해당 게시물은 스킵  

3. **공지 요약 (OpenAI GPT-4o-mini)**  
   - PDF를 OpenAI API에 업로드  
   - **제목, 부서, 기간, 장소, 대상, 문의처** 중심으로 한 단락 요약  
   - 날짜는 YYYY-MM-DD 형식으로 정규화  

4. **임베딩 생성 (text-embedding-3-small)**  
   - 요약 텍스트를 임베딩 벡터로 변환  
   - JSON 형태로 DB 저장  

5. **DB 업서트 (MySQL)**  
   - 새로운 공지를 `INSERT ... ON DUPLICATE KEY UPDATE`로 저장  
   - 이미 존재하는 `post_number`는 최신 요약·임베딩으로 갱신  

6. **크롤링 루프 제어**  
   - `crawl_category` 함수는 시작 `seq`부터 순차 탐색  
   - 게시물 없음(`not_found`)이 연속 일정 횟수(`missing_break`) 발생 시 크롤링 종료  

---

### 2. 사용자 질문 분석 및 답변 생성
1. **사용자 질문 입력 (FastAPI)**  
   - 클라이언트가 `/analyze_question` 엔드포인트에 질문 전달  

2. **키워드 추출 (LLM 기반)**  
   - GPT를 활용해 질문에서 JSON 형식으로 키워드 추출  
     - `title_keywords`: 제목 핵심 단어  
     - `writer_keywords`: 작성 부서  
     - `year`: 특정 연도 (없으면 null)  

3. **DB 검색 (MySQL)**  
   - `title_keywords`는 OR 조건  
   - `writer_keywords`는 LIKE 조건  
   - `year`는 필터링하여 최대 10개 공지를 반환  

4. **GPT 응답 생성**  
   - 검색된 공지들을 요약 목록으로 GPT에 전달  
   - GPT가 질문 의도에 맞는 자연스러운 답변을 생성  

5. **최종 응답 반환**  
   - API는 `추출된 키워드`, `검색 결과`, `GPT 답변`을 JSON으로 반환  

---

## 기술 스택
- **Python**: 데이터 크롤링과 AI 파이프라인 개발에 적합한 범용 언어  
- **FastAPI**: 경량·비동기 REST API 서버 구축을 위한 프레임워크  
- **MySQL**: 공지사항 데이터 저장 및 검색 최적화  
- **OpenAI GPT (LangChain 포함)**: 자연어 처리, 키워드 추출, 응답 생성을 담당  
- **Requests & BeautifulSoup**: HTML 요청 및 DOM 파싱  
- **Playwright**: 동적 페이지도 안정적으로 PDF 변환  

---


## 응답 예시

### 사용자 질문
```bash
"2024학년도 장학금 신청 일정 알려줘"
````

### API 응답

```json
{
  "extracted_keywords": {
    "title_keywords": ["장학금"],
    "writer_keywords": ["학생처"],
    "year": 2024
  },
  "search_results": [
    {
      "posted_date": "2024-03-01",
      "department": "학생처",
      "title": "2024학년도 1학기 장학금 신청 안내",
      "link": "https://www.uos.ac.kr/..."
    }
  ],
  "gpt_answer": "2024학년도 1학기 장학금 신청은 학생처에서 진행되며 3월 1일부터 접수 시작됩니다."
}
```

