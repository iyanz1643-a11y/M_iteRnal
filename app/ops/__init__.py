"""운영 기반 (파트 K). 화면도 업무 계산도 아닌 것만 둔다.

| 파일 | 하는 일 |
| --- | --- |
| schema.py | schema.sql 의 묶음 하나를 오래된 DB 에 다시 돌리기, 스키마 버전 |
| sessions.py | 계정마다 세션 버전. 로그아웃, 비밀번호 바꾸기, 계정 중지 때 올려 복사한 쿠키를 막는다 |
| ratelimit.py | 로그인과 공개 화면의 요청 횟수 제한. 메모리 저장, 나중에 공용 저장소로 바꾼다 |
| observe.py | 요청 번호, 오류 기록, 느린 요청 기록. 개인정보는 남기지 않는다 |
| health.py | /healthz 의 DB 연결과 스키마 버전 확인. 자료는 내지 않는다 |
| jobs.py | 작업 표와 처리기. 워커는 app/worker.py |
| storage.py | 원본, 첨부, 업로드 파일을 한 인터페이스로. 지금은 로컬 폴더 구현 |
| sqlcompat.py | SQLite 와 PostgreSQL 의 SQL 차이를 메우는 최소한의 층과 점검 |
| backup.py | DB 백업과 암호화, 되살리기 확인 |
"""
