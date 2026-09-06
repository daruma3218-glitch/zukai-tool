"""Subscription-only Claude/Codex execution. Canonical copy: subsk-worker.

No metered LLM API client is imported or called. Vendored copies are hash-checked.
Prompts/checkpoints stay in ignored local state; notices contain metadata only.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

POLICY_VERSION = "2026-09-06-cli-only-v1"
ROUTING_VERSION = "2026-09-07-workloads-v1"
# Explicit stages only. A workload never overrides a caller's explicit model.
WORKLOAD_PROFILES = {
    "classification": {"model": "haiku", "effort": "low"},
    "standard": {"model": "sonnet", "effort": "medium"},
    **{name: {"model": "gpt-6-astra", "effort": "high"} for name in (
        "research_selection", "manuscript_structure", "manuscript_rewrite",
        "assets_plan", "assets_review", "system_design", "academy_design",
        "publishing_design", "briefing_judgment", "channel_structure",
        "material_synthesis", "sentence_planning")},
}
ASSISTANT_ROOM_ID = 433846285  # Verified by Chatwork GET /rooms on 2026-09-06.
ATTEMPTS_PER_PROVIDER = 3
RETRY_DELAYS = (5, 15)
BUCKET = "subsk-gateway"
JOB_ID = contextvars.ContextVar("subscription_job_id", default="")
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_PUMP_THREAD = None


class SubscriptionUnavailable(RuntimeError):
    def __init__(self, reason, *, attempts=None, request_id="", checkpoint="", notified=False):
        self.reason = reason
        self.attempts = attempts or []
        self.request_id = request_id
        self.checkpoint = checkpoint
        self.notified = notified
        super().__init__(f"サブスクCLIで処理を継続できません ({reason}, id={request_id})。API自動切替なし。")


class CliFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def state_dir():
    configured = os.environ.get("SUBSK_STATE_DIR")
    if configured:
        path = Path(configured)
    elif os.name == "nt" and Path(r"I:\AI_Workspace\subsk-worker").is_dir():
        path = Path(r"I:\AI_Workspace\subsk-worker\state")
    else:
        path = Path(__file__).resolve().parent / ".subscription-state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_env(source=None):
    env = dict(os.environ if source is None else source)
    exact = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
             "CODEX_API_KEY", "CODEX_BASE_URL", "CODEX_ACCESS_TOKEN", "AZURE_OPENAI_API_KEY",
             "AZURE_OPENAI_ENDPOINT", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
    for key in list(env):
        if key in exact or key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_USE_"):
            env.pop(key, None)
    return env


def _run(command, *, env, cwd=None, stdin="", timeout=60):
    """Kill the complete child tree on Windows so timed-out CLI calls cannot linger."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, cwd=cwd,
                               creationflags=_CREATE_NO_WINDOW)
    try:
        out, err = process.communicate(stdin.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=_CREATE_NO_WINDOW, timeout=15)
        else:
            process.kill()
        process.communicate()
        raise CliFailure("timeout") from None
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def cli_path(provider):
    if provider == "codex" and os.name == "nt":
        # The desktop app updates its CLI independently of a stale global npm install.
        bundled = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = list(bundled.glob("*/codex.exe")) if bundled.is_dir() else []
        if candidates:
            return str(max(candidates, key=lambda p:p.stat().st_mtime))
    return shutil.which(provider)


def check_auth(provider, env=None):
    env = clean_env(env)
    cli = cli_path(provider)
    if not cli:
        raise CliFailure("cli_missing")
    args = [cli, "auth", "status", "--json"] if provider == "claude" else [cli, "login", "status"]
    code, out, err = _run(args, env=env, timeout=30)
    if code:
        raise CliFailure("auth_unavailable")
    if provider == "claude":
        try:
            status = json.loads(out)
        except ValueError:
            raise CliFailure("auth_invalid") from None
        if not (isinstance(status, dict) and status.get("loggedIn") is True
                and status.get("authMethod") == "claude.ai"
                and status.get("apiProvider") == "firstParty"
                and status.get("subscriptionType") in {"max", "pro", "team", "enterprise"}):
            raise CliFailure("subscription_auth_required")
    elif "Logged in using ChatGPT" not in out + err:
        raise CliFailure("subscription_auth_required")
    return {"provider":provider, "authentication":"subscription"}


def readiness():
    status = {}
    for provider in ("claude", "codex"):
        try:
            status[provider] = {"ready":True, **check_auth(provider)}
        except (CliFailure, OSError) as exc:
            status[provider] = {"ready":False, "reason":getattr(exc,"code","launch_failed")}
    return {"policy_version":POLICY_VERSION, "routing_version":ROUTING_VERSION, "api_fallback":False,
            "ready":any(s["ready"] for s in status.values()), "providers":status}


def _safe_label(value):
    return re.sub(r"[\x00-\x1f\[\]]", " ", str(value or ""))[:160]


def _env_secret(name):
    value = os.environ.get(name, "").strip()
    if not value and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, name)[0]).strip()
        except OSError:
            pass
    return value


@contextlib.contextmanager
def _notice_db():
    db = sqlite3.connect(state_dir() / "notifications.sqlite3", timeout=15)
    db.execute("CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, body TEXT NOT NULL, "
               "created REAL NOT NULL, sent TEXT, tries INTEGER NOT NULL DEFAULT 0, next REAL NOT NULL DEFAULT 0)")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def enqueue_notice(request, attempts, reason, *, test=False):
    job = request.get("job_id") or request["id"]
    # A job's parallel failures produce one notice. Without a job id, use the call id.
    notice_id = hashlib.sha256((request.get("tool", "") + ":" + job).encode()).hexdigest()[:20]
    routes = ", ".join(f"{a['provider']}#{a['attempt']}:{a['reason']}" for a in attempts)
    title = "CLI障害通知の接続テスト" if test else "サブスクCLI処理停止・要確認"
    body = (f"[info][title]{title}[/title]\n"
            f"ツール: {_safe_label(request.get('tool'))}\n"
            f"工程: {_safe_label(request.get('label'))}\n"
            f"ジョブ: {_safe_label(job)}\n"
            f"状態: {_safe_label(reason)}\n"
            f"試行: {_safe_label(routes)}\n"
            "従量課金LLM APIへの切替は行っていません。再開用の入力を保存しました。\n"
            f"通知ID: {notice_id}\n[/info]")
    with _notice_db() as db:
        db.execute("INSERT OR IGNORE INTO notices(id,body,created) VALUES(?,?,?)", (notice_id,body,time.time()))
    return notice_id


def _chatwork_request(token, method, path, body=None):
    data = None if body is None else urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request("https://api.chatwork.com/v2" + path, data=data,
                                 headers={"X-ChatWorkToken":token}, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else []


def flush_notifications(limit=3):
    """Retry delivery independently of LLM generation. Reconcile ambiguous POSTs by notice id."""
    token = _env_secret("CHATWORK_BOT_API_TOKEN") or _env_secret("CHATWORK_API_TOKEN")
    relay = not token and bool(_gateway_conf())
    if not token and not relay:
        return {"sent":0, "pending":True, "reason":"chatwork_token_missing"}
    delivered = 0
    for _ in range(limit):
        with _notice_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,body,tries FROM notices WHERE sent IS NULL AND next <= ? ORDER BY created LIMIT 1",
                             (time.time(),)).fetchone()
            if row is None:
                break
            ident, body, tries = row
            db.execute("UPDATE notices SET next=?,tries=tries+1 WHERE id=?", (time.time()+120,ident))
        try:
            existing = []
            if relay:
                result = _relay_notice(ident,body)
                mid = str(result.get("message_id") or "")
                if not mid:
                    raise OSError("notification relay delivery pending")
            elif tries:
                existing = _chatwork_request(token, "GET", f"/rooms/{ASSISTANT_ROOM_ID}/messages?force=1")
            if not relay:
                mid = next((str(m["message_id"]) for m in existing if f"通知ID: {ident}" in m.get("body", "")), "")
                if not mid:
                    result = _chatwork_request(token, "POST", f"/rooms/{ASSISTANT_ROOM_ID}/messages",
                                              {"body":body,"self_unread":"1"})
                    mid = str(result["message_id"])
            with _notice_db() as db:
                db.execute("UPDATE notices SET sent=? WHERE id=?", (mid,ident))
            # Existing Chatwork assistant excludes messages listed here; avoid a bot reply loop.
            _record_sent_message(mid)
            delivered += 1
        except Exception:
            # Keep the outbox entry and never regenerate the LLM work on notification failure.
            with _notice_db() as db:
                db.execute("UPDATE notices SET next=? WHERE id=?", (time.time()+min(3600,60*2**min(tries,5)),ident))
    return {"sent":delivered}


def relay_auth_key():
    conf=_gateway_conf()
    return hashlib.sha256(("subscription-cli-notice-v1:"+conf[1]).encode()).hexdigest() if conf else ""


def _relay_notice(ident,body):
    url=os.environ.get("SUBSK_NOTIFY_URL","https://otona-manabi-tv-v2.onrender.com/api/subscription-notice")
    if urllib.parse.urlparse(url).scheme!="https":raise ValueError("HTTPS notification relay required")
    payload=json.dumps({"id":ident,"body":body},ensure_ascii=False).encode()
    req=urllib.request.Request(url,data=payload,method="POST",headers={
        "Content-Type":"application/json", "X-Subsk-Notify":relay_auth_key()})
    with urllib.request.urlopen(req,timeout=60) as response:return json.loads(response.read())


def receive_relay_notice(value):
    """Called only after the web application's constant-time authentication check."""
    if not (_env_secret("CHATWORK_BOT_API_TOKEN") or _env_secret("CHATWORK_API_TOKEN")):
        raise RuntimeError("Notification sender is not configured")
    ident,body=value.get("id",""),value.get("body","")
    if not re.fullmatch(r"[a-f0-9]{20}",str(ident)) or not isinstance(body,str) or len(body)>3000:
        raise ValueError("Invalid notice")
    with _notice_db() as db:
        db.execute("INSERT OR IGNORE INTO notices(id,body,created) VALUES(?,?,?)",(ident,body,time.time()))
    start_notification_pump()
    flush_notifications()
    with _notice_db() as db:
        row=db.execute("SELECT sent FROM notices WHERE id=?",(ident,)).fetchone()
    return {"queued":True,"message_id":row[0] if row else None}


def start_notification_pump():
    global _PUMP_THREAD
    if _PUMP_THREAD is not None and _PUMP_THREAD.is_alive():return
    def pump():
        while True:
            time.sleep(30)
            with contextlib.suppress(Exception):flush_notifications()
    _PUMP_THREAD=threading.Thread(target=pump,name="subscription-notice-delivery",daemon=True)
    _PUMP_THREAD.start()


async def guarded_agent_query(prompt, options, query):
    """Keep an existing SDK tool session on subscription auth, without replaying writes.

    Stateless Messages requests use the full dual-CLI router. An interrupted session
    that may have posted or edited needs operator recovery, not a second execution.
    """
    request={"id":uuid.uuid4().hex,"job_id":JOB_ID.get(),"tool":"chatwork-agent",
             "label":"対話型ツール実行", "system":getattr(options,"system_prompt", ""),"query":prompt}
    attempts=[]
    for index in range(3):
        if index:await asyncio.sleep(RETRY_DELAYS[index-1])
        tools_started=False
        completed=False
        try:
            await asyncio.to_thread(check_auth,"claude")
            async for msg in query(prompt=prompt,options=options):
                for block in getattr(msg,"content",[]) or []:
                    if getattr(block,"type","")=="tool_use" or type(block).__name__=="ToolUseBlock":tools_started=True
                if type(msg).__name__=="ResultMessage":
                    if getattr(msg,"is_error",False):raise CliFailure("agent_result_error")
                    completed=True
                yield msg
            if not completed:raise CliFailure("agent_incomplete")
            return
        except Exception as exc:
            attempts.append({"provider":"claude","attempt":index+1,"reason":getattr(exc,"code","agent_execution_error")})
            if tools_started:break
    # Codex cannot replay Claude SDK session/tool IDs or prove that a previous write did not occur.
    try:
        await asyncio.to_thread(check_auth,"codex")
        reason="stateful_tool_session_not_replayable"
    except Exception:
        reason="auth_unavailable"
    attempts.append({"provider":"codex","attempt":1,"reason":reason})
    _terminal(request,attempts,"agent_requires_recovery")


def _record_sent_message(mid):
    bot_log = Path(r"I:\AI_Workspace\chatwork-agent\sent_message_ids.jsonl")
    if os.name == "nt" and bot_log.parent.is_dir():
        with contextlib.suppress(OSError):
            with bot_log.open("a",encoding="utf-8") as handle:
                handle.write(json.dumps({"message_id":mid,"room_id":ASSISTANT_ROOM_ID,"ts":time.time()})+"\n")


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    with contextlib.suppress(OSError):
        temp.chmod(0o600)
    os.replace(temp,path)


def _record(request, **fields):
    row = {"ts":time.time(), "policy_version":POLICY_VERSION, "request_id":request["id"],
           "job_id":request.get("job_id", ""),"tool":request.get("tool", ""),
           "label":request.get("label", ""),"channel":request.get("channel", ""), **fields}
    try:
        with (state_dir()/"routing.jsonl").open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(row,ensure_ascii=False)+"\n")
    except OSError:
        pass


def _terminal(request, attempts, reason, *, already_notified=False):
    checkpoint = ""
    try:
        checkpoint = state_dir()/"failed"/(request["id"]+".json")
        _write_json(checkpoint,{"request":request,"attempts":attempts,"reason":reason,"created":time.time()})
    except OSError:
        checkpoint = ""
        reason += ":checkpoint_write_failed"
    queued = already_notified
    if not queued:
        try:
            enqueue_notice(request,attempts,reason,test=bool(request.get("notification_test")))
            queued = True
            flush_notifications()
        except (OSError,sqlite3.Error):
            pass
    _record(request,outcome="stopped",reason=reason,attempts=attempts,notification_queued=queued)
    raise SubscriptionUnavailable(reason,attempts=attempts,request_id=request["id"],
                                  checkpoint=str(checkpoint),notified=queued)


def flatten(value):
    """Keep role delimiters and reject unsupported content instead of silently dropping it."""
    if isinstance(value,str):
        return value
    if isinstance(value,list):
        pieces = []
        for block in value:
            if not isinstance(block,dict):
                raise ValueError("Unsupported message block")
            if "role" in block:
                pieces.append(f"<{block['role']}>\n{flatten(block.get('content',''))}\n</{block['role']}>")
            elif block.get("type") in ("text", "input_text") or "text" in block:
                pieces.append(str(block.get("text", "")))
            elif block.get("type") in ("image", "document"):
                continue  # attachments are handled separately by messages_create
            elif block.get("type") in ("tool_use", "tool_result"):
                pieces.append(json.dumps(block,ensure_ascii=False))
            else:
                raise ValueError("Unsupported message block")
        return "\n".join(pieces)
    if value is None:
        return ""
    raise ValueError("Unsupported message format")


def _failure_code(code, out, err):
    combined = (out+"\n"+err).lower()
    if any(s in combined for s in ("usage limit","rate limit","quota","hit your limit","429")):
        return "usage_limit"
    if any(s in combined for s in ("unauthorized","authentication","not logged in","401","login required")):
        return "auth_unavailable"
    if any(s in combined for s in ("billing","credit balance","api key")):
        return "billing_route_rejected"
    return "cli_exit_"+str(code) if code else "invalid_output"


def _parse_claude(out):
    try:
        data = json.loads(out)
    except ValueError:
        events=[]
        for line in out.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        data = next((e for e in reversed(events) if isinstance(e,dict) and e.get("type")=="result"),None)
        if data is None:
            raise CliFailure("incomplete_output")
        # A search loop may emit a long document over multiple assistant turns.
        # The terminal result contains only the final turn. Preserve all distinct turns.
        turns={}
        for event in events:
            if event.get("type")!="assistant" or event.get("parent_tool_use_id"):continue
            message=event.get("message") or {}
            ident=message.get("id") or event.get("uuid") or str(len(turns))
            part="".join(b.get("text","") for b in message.get("content",[]) if b.get("type")=="text")
            if part:turns[ident]=part
        if turns:
            data["result"]="\n".join(turns.values())
    if not isinstance(data,dict) or data.get("is_error") or not str(data.get("result","")).strip():
        raise CliFailure(_failure_code(0,out,""))
    providers = {x.get("provider") for x in (data.get("modelUsage") or {}).values()
                 if isinstance(x,dict) and x.get("provider")}
    if providers and providers != {"firstParty"}:
        raise CliFailure("billing_route_rejected")
    return str(data["result"]).strip(),data


def _parse_codex(out):
    events=[]
    try:
        events=[json.loads(line) for line in out.splitlines() if line.strip()]
    except ValueError:
        raise CliFailure("invalid_output") from None
    completed = next((e for e in reversed(events) if e.get("type")=="turn.completed"),None)
    if any(e.get("type") in {"turn.failed","error"} for e in events) or completed is None:
        raise CliFailure(_failure_code(0,out,""))
    texts=[e.get("item",{}).get("text","") for e in events if e.get("type")=="item.completed"
           and e.get("item",{}).get("type")=="agent_message"]
    if not texts or not texts[-1].strip():
        raise CliFailure("empty_output")
    return texts[-1].strip(), {"usage":completed.get("usage",{}),"result":texts[-1],"is_error":False}


def _model_for_codex(request):
    requested = request.get("model","")
    if requested.startswith(("gpt-","o3","o4")):
        return requested
    configured = os.environ.get("SUBSK_CODEX_MODEL", "")
    if not configured:
        tier = _model_tier(requested)
        configured = {"low": "gpt-5.6-luna", "medium": "gpt-5.6-sol", "high": "gpt-6-astra"}.get(tier, "")
    if not configured:
        try:
            import tomllib
            home=Path(os.environ.get("CODEX_HOME",str(Path.home()/".codex")))
            configured=tomllib.loads((home/"config.toml").read_text(encoding="utf-8")).get("model","")
        except (OSError,ValueError,ImportError):
            pass
    return configured if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}",configured or "") else ""


def _model_tier(model):
    model = str(model).lower()
    if any(word in model for word in ("haiku", "luna", "mini", "spark")):
        return "low"
    if any(word in model for word in ("opus", "astra", "fable")) or model.startswith(("o3", "o4")):
        return "high"
    if any(word in model for word in ("sonnet", "sol", "terra")):
        return "medium"
    return None


def resolve_route(request, provider):
    """Return the selected CLI model/effort, including the fallback route."""
    workload = request.get("workload") or ""
    if workload and workload not in WORKLOAD_PROFILES:
        raise ValueError("Unknown subscription workload: " + workload)
    profile = WORKLOAD_PROFILES.get(workload, {})
    requested = str(request.get("model") or profile.get("model", "sonnet"))
    fallback = request.get("fallback_model") if request.get("primary") != provider else None
    if provider == "codex":
        model = str(fallback or _model_for_codex({**request, "model": requested}))
        effort = request.get("codex_effort") or request.get("effort") or profile.get("effort") or _model_tier(requested) or "high"
        allowed = {"low", "medium", "high", "xhigh", "max"}
    elif provider == "claude":
        model = str(fallback or requested)
        if model.startswith(("gpt-", "o3", "o4")):
            model = os.environ.get("SUBSK_CLAUDE_MODEL") or {"low": "haiku", "high": "opus"}.get(_model_tier(model), "sonnet")
        if not model.startswith("claude-"):
            model = "opus" if "opus" in model else "haiku" if "haiku" in model else "sonnet"
        # Keep existing Claude effort unless the caller explicitly selects a profile/effort.
        effort = request.get("claude_effort") or request.get("effort") or profile.get("effort") or "high"
        effort = "high" if effort == "xhigh" else effort
        allowed = {"low", "medium", "high", "max"}
    else:
        raise ValueError("provider must be claude or codex")
    if effort not in allowed:
        raise ValueError("Unsupported reasoning effort: " + str(effort))
    if model and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", model):
        raise ValueError("Invalid model name")
    return {"model": model, "effort": effort, "workload": workload, "routing_version": ROUTING_VERSION}


def _desktop_claude_cli():
    """Claude Desktop 同梱の claude.exe(2.1.251+ で claude-fable-5-1 等の完全IDが使える)。無ければ None"""
    try:
        import glob
        base=Path(os.environ.get("APPDATA",str(Path.home()/"AppData"/"Roaming")))/"Claude"/"claude-code"
        cands=sorted(glob.glob(str(base/"*"/"claude.exe")),key=lambda p:[int(x) for x in re.findall(r"\d+",Path(p).parent.name)])
        return cands[-1] if cands else None
    except Exception:
        return None


def _invoke(provider, request):
    route = resolve_route(request, provider)
    env=clean_env()
    # 2026-09-06: CLI の1ターン出力上限(既定 32,000 トークン)を引き上げる。素材レポート等の長い単一応答が
    # "response exceeded the 32000 output token maximum" で落ちた(assets-8cb0fbbd ショート候補提案・32分浪費)
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", os.environ.get("SUBSK_CLI_MAX_OUTPUT_TOKENS", "64000"))
    auth=check_auth(provider,env)
    cli=cli_path(provider)
    with tempfile.TemporaryDirectory(prefix="subsk-cli-") as raw:
        cwd=Path(raw)
        system=cwd/"system.md"
        system.write_text(request["system"],encoding="utf-8")
        files=[]
        for index, attachment in enumerate(request.get("attachments",[])):
            suffix={"application/pdf":".pdf","image/png":".png","image/jpeg":".jpg","image/webp":".webp"}.get(attachment["media_type"])
            if not suffix:
                raise CliFailure("unsupported_attachment")
            file=cwd/f"input_{index}{suffix}"
            file.write_bytes(base64.b64decode(attachment["data"],validate=True))
            files.append(file)
        prompt=request["query"]
        if request["use_search"]:
            prompt += "\n\n検索を使って根拠を確認し、参照した出典URLを本文に明記してください。"
        if provider=="claude":
            alias=route["model"]
            tools=[]
            if request["use_search"]:
                tools.append("WebSearch")
            if files:
                tools.append("Read")
                prompt="Readツールで次の添付を読み取ってから回答してください:\n"+"\n".join(str(f) for f in files)+"\n\n"+prompt
            args=[cli,"-p","--safe-mode","--no-session-persistence","--setting-sources","",
                  "--output-format","stream-json","--verbose","--permission-mode","dontAsk",
                  "--model",alias,"--effort",route["effort"],"--system-prompt-file",str(system),
                  "--tools",",".join(tools)]
            if tools:
                args += ["--allowedTools",",".join(tools)]
        else:
            args=[cli,"exec","--ignore-user-config","--ephemeral","--skip-git-repo-check","--sandbox","read-only",
                  "--disable","shell_tool","--disable","apps","--disable","plugins","--disable","multi_agent",
                  "-c",'model_provider="openai"',"-c",'forced_login_method="chatgpt"',
                  "-c",'web_search="live"' if request["use_search"] else 'web_search="disabled"',
                  "-c",'model_reasoning_effort="'+route["effort"]+'"',"--json"]
            model=route["model"]
            if model:
                args += ["--model",model]
            for file in files:
                if file.suffix==".pdf":
                    import fitz
                    with fitz.open(file) as document:
                        if len(document)>50:
                            raise CliFailure("attachment_too_long")
                        for page_index,page in enumerate(document):
                            png=cwd/f"{file.stem}_{page_index}.png"
                            page.get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(png)
                            args += ["--image",str(png)]
                else:
                    args += ["--image",str(file)]
            prompt=("以下の指示に従って最終成果の本文だけを返してください。ファイル編集・投稿はしないでください。\n"
                    "<system_instructions>\n"+request["system"]+"\n</system_instructions>\n\n"+prompt)
            args += ["-"]
        code,out,err=_run(args,env=env,cwd=str(cwd),stdin=prompt,timeout=request["timeout"])
        if provider=="claude" and code and "does not support this model" in (out+err) and _desktop_claude_cli():
            # 古い npm 版 CLI が完全なモデルIDを知らない → Desktop 同梱の新しい CLI で1回だけ再実行
            args[0]=_desktop_claude_cli()
            code,out,err=_run(args,env=env,cwd=str(cwd),stdin=prompt,timeout=request["timeout"])
        if code:
            _record(request,outcome="cli_error",provider=provider,code=code,stderr_tail=(err or out)[-300:])
        if "takes precedence over your claude.ai login" in err:
            raise CliFailure("billing_route_rejected")
        if code and "output token maximum" in (out+err):
            # 2026-09-06: 出力上限超過は再試行しても同じ(毎回30分以上浪費) → 同じプロバイダでは再試行しない
            raise CliFailure("output_too_long")
        if code:
            raise CliFailure(_failure_code(code,out,err))
        text,payload = _parse_claude(out) if provider=="claude" else _parse_codex(out)
        if request.get("protocol_tools"):
            _parse_protocol(text,request["protocol_tools"])
        payload.update({"_provider":provider,"_authentication":auth["authentication"],"_policy_version":POLICY_VERSION,
                        "_requested_model":request["model"], "_model":route["model"], "_effort":route["effort"],
                        "_workload":route["workload"], "_routing_version":ROUTING_VERSION})
        return text,payload


def run_local_request(request):
    primary=request.get("primary","claude")
    if primary not in {"claude","codex"}:
        raise ValueError("primary must be claude or codex")
    # Validate configuration before retries or failure notifications.
    routes = {provider: resolve_route(request, provider) for provider in ("claude", "codex")}
    attempts=[]
    request_started=time.monotonic()
    for provider in (primary,"codex" if primary=="claude" else "claude"):
        for index in range(ATTEMPTS_PER_PROVIDER):
            if index:
                time.sleep(RETRY_DELAYS[index-1])
            started=time.monotonic()
            try:
                text,payload=_invoke(provider,request)
            except Exception as exc:
                reason=getattr(exc,"code","local_execution_error")
                item={"provider":provider,"attempt":index+1,"reason":reason, **routes[provider]}
                attempts.append(item)
                _record(request,outcome="retry_failed",**item)
                if reason in {"cli_missing","unsupported_attachment","attachment_too_long","billing_route_rejected","output_too_long",
                              "usage_limit", "timeout"}:
                    break
            else:
                _record(request,outcome="completed",provider=provider,attempt=index+1,
                        elapsed_s=round(time.monotonic()-started,1),total_elapsed_s=round(time.monotonic()-request_started,1),
                        requested_model=request.get("model"), **routes[provider], usage=payload.get("usage",{}))
                payload["_attempts"]=attempts
                return text,payload
    _terminal(request,attempts,"both_cli_unavailable")


def _gateway_conf():
    url=os.environ.get("SUPABASE_URL","").strip().rstrip("/")
    key=(os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
         or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return (url,key) if url and key else None


def _storage(method,path,body=None):
    conf=_gateway_conf()
    if not conf:
        raise CliFailure("gateway_not_configured")
    url,key=conf
    data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None
    req=urllib.request.Request(url+"/storage/v1/object/"+BUCKET+"/"+path,data=data,method=method,
                               headers={"Authorization":"Bearer "+key,"apikey":key,"Content-Type":"application/json","x-upsert":"true"})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            raw=response.read()
            return response.status,json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code,{}


def _gateway_generate(request):
    attempts=[]
    for index in range(3):
        if index:
            time.sleep(RETRY_DELAYS[index-1])
        try:
            code,hb=_storage("GET","hb/worker.json")
            if code==200 and time.time()-float(hb.get("ts",0))<60 and hb.get("policy_version")==POLICY_VERSION:
                break
        except (OSError,ValueError,CliFailure):
            pass
        attempts.append({"provider":"worker","attempt":index+1,"reason":"worker_unavailable"})
    else:
        _terminal(request,attempts,"worker_unavailable")
    path="req/"+request["id"]+".json"
    payload={**request,"kind":"research" if request["use_search"] else "query","created":time.time(),
             "policy_version":POLICY_VERSION,"max_tokens":request.get("max_tokens",4096)}
    try:
        code,_=_storage("POST",path,payload)
        if code not in {200,201}:
            raise CliFailure("queue_write_failed")
        # Worker retries both CLIs. Never time out before that retry budget has elapsed.
        deadline=time.monotonic()+6*(request["timeout"]+30)+2*sum(RETRY_DELAYS)+600
        last_health=time.monotonic()
        missed_health=0
        while time.monotonic()<deadline:
            time.sleep(3)
            if time.monotonic()-last_health > 30:
                last_health=time.monotonic()
                try:
                    hcode,hb=_storage("GET","hb/worker.json")
                    healthy=hcode==200 and time.time()-float(hb.get("ts",0))<90
                except (OSError,ValueError,CliFailure):
                    healthy=False
                missed_health=0 if healthy else missed_health+1
                if missed_health>=3:
                    _terminal(request,attempts,"worker_disconnected")
            try:
                code,result=_storage("GET","res/"+request["id"]+".json")
            except OSError:
                continue
            if code!=200:
                continue
            if result.get("ok") and result.get("text"):
                return result["text"],result.get("payload",{"_provider":result.get("provider","worker")})
            _terminal(request,result.get("attempts",[]),result.get("reason","worker_failed"),
                      already_notified=bool(result.get("notification_queued")))
        _terminal(request,attempts,"worker_response_timeout")
    except SubscriptionUnavailable:
        raise
    except (OSError,CliFailure,ValueError):
        _terminal(request,attempts,"gateway_unavailable")


def generate(system,query,*,model=None,primary=None,use_search=False,timeout=900,
             tool="workflow",label="",channel="",job_id="",attachments=None,effort=None,max_tokens=4096,protocol_tools=None,
             workload="",fallback_model=None):
    if primary is not None and primary not in {"claude", "codex"}:
        raise ValueError("primary must be claude or codex")
    if workload and workload not in WORKLOAD_PROFILES:
        raise ValueError("Unknown subscription workload: " + workload)
    model=model or WORKLOAD_PROFILES.get(workload, {}).get("model", "sonnet")
    request={"id":uuid.uuid4().hex,"job_id":job_id or JOB_ID.get(),"system":flatten(system),"query":flatten(query),
             "model":model,"primary":primary or ("codex" if str(model).startswith(("gpt-","o3","o4")) else "claude"),
             "use_search":bool(use_search),"timeout":max(30,int(timeout or 900)),"tool":tool,"label":label,
             "channel":channel,"attachments":attachments or [],"effort":effort,"max_tokens":max_tokens,
             "protocol_tools":protocol_tools or [], "workload":workload, "fallback_model":fallback_model,
             "routing_version":ROUTING_VERSION}
    for provider in ("claude", "codex"):
        request[provider+"_effort"]=resolve_route(request, provider)["effort"]
    # Older workers require a non-null --effort. New workers use the provider-specific fields.
    request["effort"]=request["effort"] or request[request["primary"]+"_effort"]
    start_notification_pump()
    if cli_path("claude") or cli_path("codex"):
        return run_local_request(request)
    if _gateway_conf():
        return _gateway_generate(request)
    return run_local_request(request)  # records both missing binaries and notifies


def file_attachment(path):
    path=Path(path)
    media={".pdf":"application/pdf",".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp"}.get(path.suffix.lower())
    if not media:
        raise ValueError("Unsupported attachment type")
    return {"media_type":media,"data":base64.b64encode(path.read_bytes()).decode("ascii")}


class Response(SimpleNamespace):
    def model_dump(self):
        return {"id":self.id,"type":"message","role":"assistant","model":self.model,"stop_reason":self.stop_reason,
                "content":[vars(b) for b in self.content],"usage":vars(self.usage)}


def _parse_protocol(text,tools):
    try:
        raw=text.strip()
        if raw.startswith("```"):
            raw=re.sub(r"^```(?:json)?\s*|\s*```$","",raw)
        data=json.loads(raw)
        blocks=data["content"]
        if not isinstance(blocks,list) or not blocks:raise ValueError()
        allowed={t["name"]:t for t in tools}
        for block in blocks:
            if block.get("type")=="text" and isinstance(block.get("text"),str):continue
            if (block.get("type")!="tool_use" or block.get("name") not in allowed
                or not isinstance(block.get("input"),dict)):raise ValueError()
            required=allowed[block["name"]].get("input_schema",{}).get("required",[])
            if any(k not in block["input"] for k in required):raise ValueError()
            block["id"]=str(block.get("id") or "cli_tool_"+uuid.uuid4().hex)
        return blocks
    except (ValueError,KeyError,TypeError,AttributeError):
        raise CliFailure("invalid_tool_protocol") from None


def messages_create(*,tool="workflow",label="",channel="",job_id="",**kwargs):
    messages=kwargs.get("messages",[])
    attachments=[]
    for message in messages:
        if not isinstance(message.get("content"),list):
            continue
        for block in message["content"]:
            if block.get("type") in {"image","document"}:
                source=block.get("source",{})
                if source.get("type")!="base64":
                    raise ValueError("Only explicit base64 attachments are supported")
                attachments.append({"media_type":source["media_type"],"data":source["data"]})
    tools=kwargs.get("tools") or []
    custom=[t for t in tools if t.get("input_schema")]
    web=any(str(t.get("type","")).startswith("web_search") for t in tools)
    if any(t not in custom and not str(t.get("type","")).startswith("web_search") for t in tools):
        raise ValueError("Unsupported tool type")
    system=flatten(kwargs.get("system",""))
    if custom:
        system += ('\n\n次のアプリ固有ツールは実行せず、必要な呼び出しをJSONで返してください。'
                   '呼び出しの実行と結果はホストアプリが担当します。'
                   '\nツール定義: '+json.dumps(custom,ensure_ascii=False)+
                   '\n応答形式はJSONオブジェクトのみ: {"content":[{"type":"text","text":"返答"}]}'
                   ' または {"content":[{"type":"tool_use","id":"一意ID","name":"ツール名","input":{}}]}。'
                   '\n実行していない操作を完了したと答えないでください。')
    text,payload=generate(system,messages,model=kwargs.get("model"),primary=kwargs.get("primary"),
                          use_search=web,timeout=kwargs.get("timeout") or 900,
                          workload=kwargs.get("workload", ""),effort=kwargs.get("effort"),fallback_model=kwargs.get("fallback_model"),
                          tool=tool,label=label,channel=channel,job_id=job_id,attachments=attachments,max_tokens=kwargs.get("max_tokens",4096),protocol_tools=custom)
    usage=payload.get("usage",{})
    content=[SimpleNamespace(**b) for b in _parse_protocol(text,custom)] if custom else [SimpleNamespace(type="text",text=text)]
    return Response(id="cli-"+uuid.uuid4().hex,content=content,
                    stop_reason="tool_use" if any(b.type=="tool_use" for b in content) else "end_turn",model=payload.get("_provider","cli")+"/subscription",
                    usage=SimpleNamespace(input_tokens=usage.get("input_tokens",0),output_tokens=usage.get("output_tokens",0),
                                          cache_read_input_tokens=usage.get("cache_read_input_tokens",usage.get("cached_input_tokens",0)),
                                          cache_creation_input_tokens=usage.get("cache_creation_input_tokens",0)),
                    _subscription_payload=payload)


class _Stream:
    def __init__(self, kwargs, async_mode=False):
        self.kwargs=kwargs
        self.async_mode=async_mode
    def __enter__(self):
        self.response=messages_create(**self.kwargs)
        self.text_stream=iter([self.response.content[0].text])
        return self
    def __exit__(self,*args):
        return False
    def __iter__(self):
        return iter(())
    def get_final_message(self):
        return self._async_final() if self.async_mode else self.response
    async def _async_final(self):
        return self.response
    async def __aenter__(self):
        self.response=await asyncio.to_thread(messages_create,**self.kwargs)
        return self
    async def __aexit__(self,*args):
        return False
    def __aiter__(self):
        return self
    async def __anext__(self):
        raise StopAsyncIteration


class SubscriptionClient:
    """Small Messages-compatible adapter; api_key is deliberately ignored."""
    def __init__(self,*args,tool="workflow",channel="",**kwargs):
        self.tool=tool
        self.channel=channel
        self.job_id=kwargs.get("job_id") or JOB_ID.get()
        self.options={key:kwargs[key] for key in ("timeout", "workload", "effort", "fallback_model", "primary") if key in kwargs}
        self.messages=self
    def _request_kwargs(self,kwargs):
        return {"tool":self.tool,"channel":self.channel,"job_id":self.job_id,
                **self.options,**kwargs}
    def with_options(self,**kwargs):
        return type(self)(tool=self.tool,channel=self.channel,job_id=self.job_id,
                          **{**self.options,**kwargs})
    def create(self,**kwargs):
        return messages_create(**self._request_kwargs(kwargs))
    def stream(self,**kwargs):
        return _Stream(self._request_kwargs(kwargs))
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False


class AsyncSubscriptionClient(SubscriptionClient):
    async def create(self,**kwargs):
        return await asyncio.to_thread(messages_create,**self._request_kwargs(kwargs))
    def stream(self,**kwargs):
        return _Stream(self._request_kwargs(kwargs),async_mode=True)
    async def close(self):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False


class _JsonResponse:
    status = status_code = 200
    def __init__(self,value):
        self.value=value
    def read(self):
        return json.dumps(self.value,ensure_ascii=False).encode("utf-8")
    def json(self):
        return self.value
    def raise_for_status(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self,*args):
        return False


def urlopen(request,*args,**kwargs):
    """Compatibility for legacy urllib callers. Anthropic Messages never reaches HTTP."""
    url=request.full_url if isinstance(request,urllib.request.Request) else str(request)
    if urllib.parse.urlparse(url).hostname=="api.anthropic.com" and "/messages" in url:
        data=json.loads(request.data.decode("utf-8"))
        caller=sys._getframe(1)
        tool=Path(caller.f_code.co_filename).parent.name+"/"+caller.f_code.co_name
        return _JsonResponse(messages_create(tool=tool,**data).model_dump())
    return urllib.request.urlopen(request,*args,**kwargs)


def requests_post(url,*args,**kwargs):
    if urllib.parse.urlparse(url).hostname=="api.anthropic.com" and "/messages" in url:
        data=kwargs.get("json")
        if data is None:
            data=json.loads(kwargs.get("data") or args[0])
        caller=sys._getframe(1)
        tool=Path(caller.f_code.co_filename).parent.name+"/"+caller.f_code.co_name
        return _JsonResponse(messages_create(tool=tool,**data).model_dump())
    import requests
    return requests.post(url,*args,**kwargs)
