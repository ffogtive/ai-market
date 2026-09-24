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
npx wrangler login
npx wrangler kv namespace create RETRO    # 나온 id를 wrangler.toml에 반영
npm run deploy
```
연결 URL: `https://retro-mcp.<서브도메인>.workers.dev/mcp/<key>`
Claude·ChatGPT·Gemini의 "커스텀 커넥터 추가"에 이 URL을 넣습니다.

## 토큰
연결된 대화마다 도구 정의(~260 토큰, chars/4 추정)가 맥락에 들어가고, 호출 1회는 인자+결과로 대화당 100~150 토큰 수준. 시각은 서버가 찍고 결과는 한 줄이라 모델 출력 토큰을 최소화. 통계·페이지는 서버가 LLM 없이 계산.
