"""팩트체크 도구 로컬 서버.

같은 폴더의 .env에서 TAVILY_API_KEY, CLAUDE_API_KEY를 읽어
브라우저 대신 Tavily(검색)와 Claude(판정)를 불러 준다. 키는 브라우저로 가지 않는다.

실행:  「팩트체크 열기.bat」 더블클릭 (또는 python server.py)
       →  http://127.0.0.1:8765 이 자동으로 열린다. 이미 켜져 있으면 화면만 연다.
필요한 패키지: anthropic (pip install anthropic)

서버 배포(Render 등): 키와 설정은 .env 대신 환경변수로 준다. 환경변수가 .env보다 우선한다.
  FACTCHECK_HOST=0.0.0.0     외부 접속을 받는다(이때는 비밀번호가 반드시 있어야 켜진다)
  PORT                       호스팅 업체가 정해 주는 포트
  FACTCHECK_PASSWORD         동료들과 나눠 쓰는 공용 비밀번호
  FACTCHECK_DAILY_LIMIT      하루(UTC)에 받는 Claude 호출 수 상한. 0이면 무제한
  FACTCHECK_ORIGIN           접속 주소(https://...). Render는 RENDER_EXTERNAL_URL로 자동 인식
"""

import datetime
import hmac
import http.server
import json
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import anthropic

HOST = os.environ.get("FACTCHECK_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT") or 8765)
LOCAL_ONLY = HOST in ("127.0.0.1", "localhost", "::1")
HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
INDEX_PATH = HERE / "index.html"

TAVILY_URL = "https://api.tavily.com/search"
# 화면에서 고를 수 있는 판정 모델. 순서대로 선택칸에 나온다. 가격은 100만 토큰당 달러(2026-10 기준).
# fallbacks: 안전 분류기가 거절하면 서버가 다른 모델로 다시 판정하는 기능. Haiku 5.5는 지원하지 않는다.
CLAUDE_MODELS = {
    "claude-sonnet-5-5": {"label": "Sonnet 5.5 — 기본, 균형", "effort": "medium", "fallbacks": True,
                          "price_in": 2.00, "price_out": 10.00},
    "claude-haiku-5-5": {"label": "Haiku 5.5 — 가장 쌈, 단순한 숫자·사실 확인용", "effort": "medium", "fallbacks": False,
                         "price_in": 0.10, "price_out": 0.50},
    "claude-opus-5-5": {"label": "Opus 5.5 — 가장 꼼꼼, 비쌈", "effort": "medium", "fallbacks": True,
                        "price_in": 4.00, "price_out": 20.00},
}
DEFAULT_MODEL = "claude-sonnet-5-5"
VERDICTS = ["사실", "대체로 사실", "절반의 사실", "대체로 사실 아님", "사실 아님", "판단 유보"]
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "used_evidence": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["verdict", "summary", "reason", "used_evidence"],
    "additionalProperties": False,
}
# 주장이 말하는 시점. start·end는 YYYY-MM-DD, 둘 다 빈 문자열이면 「현재」. label은 사람이 읽는 표현.
TIME_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "start": {"type": "string"},
        "end": {"type": "string"},
    },
    "required": ["label", "start", "end"],
    "additionalProperties": False,
}
# 판정 전에 주장이 모호한지 보고, 모호하면 검증 가능한 문장 후보 3개를 받는다.
# 원래 주장과 후보마다 주장 시점도 받는다. 후보 개수(3개)는 스키마로 강제하지 않고 화면에서 검사한다.
CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "ambiguous": {"type": "boolean"},
        "reason": {"type": "string"},
        "time": TIME_SCHEMA,
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "time": TIME_SCHEMA},
                "required": ["text", "time"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ambiguous", "reason", "time", "candidates"],
    "additionalProperties": False,
}
# ───── 취재 도우미(챗봇) ─────
# 사용자의 말을 갈래로 나눈다: chat(인사·잡담) / check(주장 확인, 앞 주장 이어받기 포함) /
# ask_last(방금 판정에 대한 질문) / extract(기사·보도자료 문단에서 확인할 문장 뽑기) / howto(도구 설명).
# 실제 검색·판정은 화면이 판정 화면과 같은 함수(/api/tavily, /api/claude)로 한다.
CHAT_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "sources": {"type": "string", "enum": ["all", "public", "stats", "press", "regional"]},
        "days": {"type": "integer"},
        "region": {"type": "string"},
        "label": {"type": "string"},
    },
    "required": ["sources", "days", "region", "label"],
    "additionalProperties": False,
}
CHAT_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["chat", "check", "ask_last", "extract", "howto"]},
        "reply": {"type": "string"},
        "claim": {"type": "string"},
        "time": TIME_SCHEMA,
        "inherited": {"type": "boolean"},
        "changed": {"type": "string"},
        "scope": CHAT_SCOPE_SCHEMA,
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "time": TIME_SCHEMA},
                "required": ["text", "time"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["intent", "reply", "claim", "time", "inherited", "changed", "scope", "sentences"],
    "additionalProperties": False,
}
# 이미 찾은 근거 안에서만 답한다. next_checks는 판단 유보일 때 다음에 확인할 곳(기관·부서, 자료 이름).
CHAT_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "used_evidence": {"type": "array", "items": {"type": "integer"}},
        "next_checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"place": {"type": "string"}, "material": {"type": "string"}},
                "required": ["place", "material"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "used_evidence", "next_checks"],
    "additionalProperties": False,
}
MAX_BODY = 512 * 1024
TIMEOUT = 90
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
for _o in (os.environ.get("FACTCHECK_ORIGIN"), os.environ.get("RENDER_EXTERNAL_URL")):
    if _o:
        ALLOWED_ORIGINS.add(_o.strip().rstrip("/"))


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k] = v
    return env


def key(name: str) -> str:
    # 환경변수(서버 배포)가 먼저다. 없으면 .env를 요청마다 다시 읽어서, .env를 고치면 서버를 다시 켜지 않아도 반영된다.
    v = os.environ.get(name, "").strip()
    return v or load_env(ENV_PATH).get(name, "").strip()


def daily_limit() -> int:
    try:
        return max(0, int(key("FACTCHECK_DAILY_LIMIT") or 0))
    except ValueError:
        return 0


# 하루 Claude 호출 수. 서버를 다시 켜면 0부터 센다(무료 호스팅은 자주 재시작되므로 넉넉한 안전장치일 뿐이다).
_usage = {"day": "", "count": 0}
_usage_lock = threading.Lock()


def take_quota() -> bool:
    limit = daily_limit()
    if not limit:
        return True
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    with _usage_lock:
        if _usage["day"] != today:
            _usage["day"], _usage["count"] = today, 0
        if _usage["count"] >= limit:
            return False
        _usage["count"] += 1
        return True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FactCheck/1.0"

    # ── 응답 도우미 ──
    def send_json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_error(self, status: int, message: str) -> None:
        # source=local 표시로 화면이 「서버 문제」와 「Tavily·Gemini가 돌려준 오류」를 구별한다.
        self.send_json(status, {"error": {"message": message}, "source": "local"})

    def log_message(self, fmt, *args):  # 요청 로그에 키가 찍힐 일은 없지만 간단히 유지
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ── 화면 ──
    def do_GET(self):
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            data = INDEX_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.split("?", 1)[0] == "/api/models":
            self.send_json(200, {
                "default": DEFAULT_MODEL,
                "models": [{"id": m, "label": c["label"]} for m, c in CLAUDE_MODELS.items()],
            })
            return
        self.local_error(404, "없는 주소입니다.")

    # ── 중계 ──
    def do_POST(self):
        # 다른 웹사이트가 이 서버를 몰래 불러 키를 쓰지 못하게 막는다.
        origin = self.headers.get("Origin")
        if origin not in ALLOWED_ORIGINS:
            self.local_error(403, "이 화면에서 온 요청만 받습니다.")
            return
        password = key("FACTCHECK_PASSWORD")
        if password:
            given = self.headers.get("X-App-Password") or ""
            if not hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8")):
                self.send_json(401, {"error": {"message": "비밀번호가 틀렸습니다." if given else "비밀번호가 필요합니다."},
                                     "source": "local", "need_password": True})
                return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.local_error(415, "JSON 요청만 받습니다.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self.local_error(400, "요청 크기가 잘못됐습니다.")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self.local_error(400, "요청을 JSON으로 읽지 못했습니다.")
            return

        path = self.path.split("?", 1)[0]
        if path == "/api/tavily":
            k = key("TAVILY_API_KEY")
            if not k:
                self.local_error(500, ".env에 TAVILY_API_KEY가 없습니다.")
                return
            payload.pop("api_key", None)
            self.forward(TAVILY_URL, payload, {"Authorization": "Bearer " + k})
            return

        if path == "/api/claude":
            self.ask_claude(payload, VERDICT_SCHEMA, "판정")
            return
        if path == "/api/clarify":
            self.ask_claude(payload, CLARIFY_SCHEMA, "문장 정리")
            return
        if path == "/api/chat-route":
            self.ask_claude(payload, CHAT_ROUTE_SCHEMA, "질문 살펴보기")
            return
        if path == "/api/chat-answer":
            self.ask_claude(payload, CHAT_ANSWER_SCHEMA, "근거 답변")
            return

        self.local_error(404, "없는 주소입니다.")

    def ask_claude(self, payload: dict, schema: dict, what: str) -> None:
        """Claude에 프롬프트를 보내고 schema 모양의 JSON을 돌려준다. what은 오류 문구에 쓰는 작업 이름."""
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self.local_error(400, f"{what}에 보낼 프롬프트가 비어 있습니다.")
            return
        model = payload.get("model") or DEFAULT_MODEL
        conf = CLAUDE_MODELS.get(model)
        if conf is None:
            self.local_error(400, f"고를 수 없는 모델입니다: {model}")
            return
        k = key("CLAUDE_API_KEY") or key("ANTHROPIC_API_KEY")
        if not k:
            self.local_error(500, ".env에 CLAUDE_API_KEY가 없습니다.")
            return
        if not take_quota():
            self.local_error(429, "오늘 쓸 수 있는 판정 횟수를 다 썼습니다. 내일(한국 시간 오전 9시 이후) 다시 시도하세요.")
            return

        # SDK가 429·5xx·연결 오류를 알아서 2번까지 다시 시도한다.
        client = anthropic.Anthropic(api_key=k, timeout=TIMEOUT, max_retries=2)
        request = {
            "model": model,
            "max_tokens": 16000,
            "output_config": {
                "effort": conf["effort"],
                "format": {"type": "json_schema", "schema": schema},
            },
            "messages": [{"role": "user", "content": prompt}],
        }
        if conf["fallbacks"]:
            # 안전 분류기가 거절하면 서버 쪽에서 다른 모델로 넘겨 다시 판정한다.
            request["betas"] = ["server-side-fallback-2026-07-01"]
            request["fallbacks"] = "default"
        try:
            res = client.beta.messages.create(**request)
        except anthropic.AuthenticationError:
            self.local_error(401, ".env의 CLAUDE_API_KEY가 틀렸거나 만료됐습니다.")
            return
        except anthropic.PermissionDeniedError as e:
            self.local_error(403, f"Claude API 권한이 없습니다: {e.message}")
            return
        except anthropic.RateLimitError:
            self.local_error(429, "Claude API 사용 한도를 넘었습니다. 잠시 뒤 다시 시도하세요.")
            return
        except anthropic.BadRequestError as e:
            self.local_error(400, f"Claude API가 요청을 거절했습니다: {e.message}")
            return
        except anthropic.APIStatusError as e:
            self.local_error(502, f"Claude API 오류 (HTTP {e.status_code}): {e.message}")
            return
        except anthropic.APIConnectionError:
            self.local_error(502, "Claude API에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
            return

        if res.stop_reason == "refusal":
            self.local_error(422, f"Claude가 이 주장의 {what}을(를) 거절했습니다.")
            return
        if res.stop_reason == "max_tokens":
            self.local_error(502, "Claude 답이 길이 제한에 걸려 잘렸습니다.")
            return
        text = next((b.text for b in res.content if b.type == "text"), "")
        try:
            result = json.loads(text)
        except ValueError:
            self.local_error(502, "Claude 답을 JSON으로 읽지 못했습니다.")
            return
        result["model"] = res.model  # fallback이 돌았으면 실제로 답한 모델이 찍힌다
        u = res.usage
        result["usage"] = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            # 요청한 모델 가격 기준 추정치. fallback이 돈 경우엔 실제와 다를 수 있다.
            "cost_usd": round(u.input_tokens / 1e6 * conf["price_in"] + u.output_tokens / 1e6 * conf["price_out"], 5),
        }
        self.send_json(200, result)

    def forward(self, url: str, payload: dict, headers: dict) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                status, body = res.status, res.read()
        except urllib.error.HTTPError as e:
            # 429·404·503 같은 상태 코드를 그대로 돌려줘야 화면의 모델 전환·재시도가 동작한다.
            status, body = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            self.local_error(502, f"외부 서비스에 연결하지 못했습니다: {reason}")
            return

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class ExclusiveServer(http.server.ThreadingHTTPServer):
    # 윈도에서는 기본 설정(SO_REUSEADDR)이면 같은 포트에 서버가 두 개 뜬다(2026-11-04 실측).
    # 포트를 독점해야 「이미 켜져 있음」을 알아챌 수 있다.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as res:
            return res.headers.get("Server", "").startswith(Handler.server_version)
    except (urllib.error.URLError, OSError):
        return False


def main() -> None:
    missing = [n for n in ("TAVILY_API_KEY",) if not key(n)]
    if not (key("CLAUDE_API_KEY") or key("ANTHROPIC_API_KEY")):
        missing.append("CLAUDE_API_KEY")
    if missing:
        print(f"경고: 환경변수나 {ENV_PATH} 에 {', '.join(missing)} 가 없습니다. 검증하기를 누르면 오류가 납니다.")

    if not LOCAL_ONLY:
        # 외부 접속을 받으면서 비밀번호가 없으면 주소를 아는 누구나 키를 쓴다. 아예 켜지 않는다.
        if not key("FACTCHECK_PASSWORD"):
            print("FACTCHECK_PASSWORD가 없어서 외부 접속 모드로 켤 수 없습니다.")
            sys.exit(1)
        print(f"외부 접속 모드: 허용 주소 {sorted(ALLOWED_ORIGINS)}, 하루 상한 {daily_limit() or '없음'}", flush=True)
        httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
        return

    url = f"http://{HOST}:{PORT}/"
    try:
        httpd = ExclusiveServer((HOST, PORT), Handler)
    except OSError as e:
        # 이미 켜져 있는 팩트체크 서버라면 화면만 열고 끝낸다.
        if already_running(url):
            print("팩트체크 도구가 이미 켜져 있어서 화면만 엽니다.")
            if "--no-browser" not in sys.argv:
                webbrowser.open(url)
            return
        print(f"{PORT}번 포트를 다른 프로그램이 쓰고 있어서 열지 못했습니다. ({e})")
        sys.exit(1)

    print(f"팩트체크 도구: {url}")
    print("이 창을 닫으면 팩트체크 도구도 꺼집니다. 쓰는 동안에는 열어 두세요.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
