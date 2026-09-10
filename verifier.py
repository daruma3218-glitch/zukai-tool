"""承認済みの画像・対応する抜粋・章名だけをサブスクCLIで検査する。

Codex(ASTRA)を優先し、Codexが利用上限・障害で使えない時はClaude CLI(サブスク認証)で代替する(2026-09-10 社長指示)。
従量課金のLLM APIへは切り替えない。実際に検査したCLI・モデルは report["routes"] に残す。
"""
import base64
import json
import os
from pathlib import Path

import subscription_runtime as runtime
from utils import parse_json_array

VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "").strip() or "gpt-6-astra"
# Codex不能時にClaude CLIで使うモデル。未指定なら共通CLIの既定(ASTRA相当→Opus / high)。
VERIFY_FALLBACK_MODEL = os.environ.get("VERIFY_FALLBACK_MODEL", "").strip() or None
BATCH_SIZE = 4


def verify_images(items, images_dir, *, job_id="", on_review=None):
    """画像を再生成せず、合格・要修正・未確認を記録する。原稿全文は受け取らない。"""
    root = Path(images_dir).resolve()
    report = {"requested_model": VERIFY_MODEL, "routes": [], "items": [], "api_fallback": False}
    candidates = [item for item in items if item.get("success")]
    unavailable = False
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        attachments, inputs, verdicts = [], [], {}
        for item in batch:
            idx = item["index"]
            verdicts[idx] = {"index": idx, "status": "unverified", "reason": "内容検査を完了できませんでした"}
            if unavailable:
                continue
            path = (root / (item.get("filename") or "")).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                verdicts[idx]["reason"] = "検査する画像を読み込めませんでした"
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if not data:
                continue
            attachments.append({"media_type": "image/png", "data": base64.b64encode(data).decode("ascii")})
            inputs.append({"index": idx, "attachment": len(attachments),
                           "excerpt": item.get("excerpt", ""), "section": item.get("section", "")})
        if inputs:
            try:
                text, meta = runtime.generate(
                    "あなたは図解の内容検査担当です。添付画像を必ず読み、対応する原稿の抜粋と"
                    "意味・数値・主体・因果の矢印・年代・地理・通貨・ラベルの可読性を照合してください。"
                    "抜粋だけでは確かめられない事実を正しいと断定しないでください。"
                    "返答はJSON配列のみ。各項目はindex、status(pass/needs_fix/unverified)、reason(日本語)。",
                    "添付と原稿抜粋の対応:\n" + json.dumps(inputs, ensure_ascii=False),
                    model=VERIFY_MODEL, primary="codex", allow_fallback=True, fallback_model=VERIFY_FALLBACK_MODEL,
                    workload="assets_review", effort="high", timeout=300, max_tokens=4000,
                    attachments=attachments, tool="zukai", label="content_review", job_id=job_id,
                )
                report["routes"].append({k: meta.get(k) for k in ("_model", "_provider", "_authentication", "_effort", "_routing_version")})
                allowed = {item["index"] for item in inputs}
                parsed = parse_json_array(text)
                for result in parsed:
                    if not isinstance(result, dict):
                        continue
                    idx = result.get("index")
                    if type(idx) is not int or idx not in allowed:
                        continue
                    duplicates = sum(isinstance(row, dict) and row.get("index") == idx for row in parsed)
                    reason = result.get("reason")
                    if (duplicates == 1 and result.get("status") in {"pass", "needs_fix", "unverified"}
                            and isinstance(reason, str) and reason.strip()):
                        verdicts[idx] = {"index": idx, "status": result["status"], "reason": reason[:300]}
            except runtime.SubscriptionUnavailable:
                unavailable = True
            except (ValueError, TypeError, OSError):
                pass  # 未確認を維持し、合格に見せない。
        for item in batch:
            result = verdicts[item["index"]]
            report["items"].append(result)
            if on_review:
                on_review(item, result)
    report["counts"] = {status: sum(item["status"] == status for item in report["items"])
                        for status in ("pass", "needs_fix", "unverified")}
    return report
