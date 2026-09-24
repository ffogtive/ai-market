# retro MCP server (PoC)

각 AI 앱(Claude·ChatGPT·Gemini)에 커넥터로 붙여, 대화가 끝날 때 AI가 `log_activity`로 하루 기록을 남기고 `get_today`로 회고 페이지를 보는 최소 서버. Cloudflare Worker + KV.

- `POST /mcp/<key>` — MCP (Streamable HTTP, 상태 없음, JSON 응답)
- `GET /p/<key>/<date>` — 그날의 회고 페이지 (LLM 없이 서버에서 계산)

`<key>`는 사용자별 비밀 문자열(PoC 인증). 디렉터리 등재 시 OAuth로 교체.

## 로컬 실행 / 테스트
```bash
npm install
npm run dev     # http://127.0.0.1:8787 (wrangler dev --local, KV 불필요)
npm test        # 공식 MCP SDK 클라이언트로 e2e
```

## 배포 (Cloudflare)
```bash
./deploy.sh
```
로그인(브라우저, 또는 `CLOUDFLARE_API_TOKEN`) → KV 생성·`wrangler.toml` 반영 → 배포 → 개인 키 생성(`.retro-key`, gitignore) → 라이브 스모크 테스트 → 연결 URL 출력까지 한 번에. 다시 실행해도 KV·키는 재사용.

출력된 `https://retro-mcp.<서브도메인>.workers.dev/mcp/<key>`를 Claude "설정 → 커넥터 → 커스텀 커넥터 추가"에 넣습니다(인증 없음 — OAuth 탐색 경로는 404라 Claude가 authless로 연결). URL 자체가 비밀번호이므로 공유 금지.

## 토큰
연결된 대화마다 도구 정의(~260 토큰, chars/4 추정)가 맥락에 들어가고, 호출 1회는 인자+결과로 대화당 100~150 토큰 수준. 시각은 서버가 찍고 결과는 한 줄이라 모델 출력 토큰을 최소화. 통계·페이지는 서버가 LLM 없이 계산.
