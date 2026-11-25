#!/usr/bin/env python3
"""Standalone OpenAI-compatible Antigravity Bridge for WoWannWas.

Uses the host's native `agy` CLI directly so the selected model reads attached
images via `read_file` with WoWannWas's full prompt (no model downgrade and no
dependency on external bridges).
"""
from __future__ import annotations

import base64
import hmac
import http.server
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("wowannwas-agy-bridge")

DISPLAY_TO_ID = {
    "gemini 4 hydrogen (low)": "gemini-4-hydrogen-low",
    "gemini 4 hydrogen (medium)": "gemini-4-hydrogen-medium",
    "gemini 4 hydrogen (high)": "gemini-4-hydrogen-high",
    "gemini 4 hydrogen (xhigh)": "gemini-4-hydrogen-xhigh",
    "gemini 4 argon (low)": "gemini-4-argon-low",
    "gemini 4 argon (medium)": "gemini-4-argon-medium",
    "gemini 4 argon (high)": "gemini-4-argon-high",
    "gemini 3.8 flash (low)": "gemini-3.8-flash-low",
    "gemini 3.8 flash (medium)": "gemini-3.8-flash-medium",
    "gemini 3.8 flash (high)": "gemini-3.8-flash-high",
}

SUPPORTED_MODELS = sorted(set(DISPLAY_TO_ID.values()))
DEFAULT_MODEL = "gemini-4-hydrogen-medium"
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def load_dotenv(env_path: Path | None = None) -> None:
    target = env_path or (Path(__file__).resolve().parent / ".env")
    if not target.is_file():
        return
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_model(requested: str | None) -> str:
    raw = (requested or "").strip()
    if not raw:
        return DEFAULT_MODEL
    lowered = raw.lower()
    if lowered in DISPLAY_TO_ID:
        return DISPLAY_TO_ID[lowered]
    if lowered in SUPPORTED_MODELS:
        return lowered
    # Pass through known CLI model slugs cleanly (letters, digits, dot, dash).
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,80}", lowered):
        return lowered
    return DEFAULT_MODEL


def decode_data_url(url: str) -> tuple[str, bytes]:
    if url.startswith("data:"):
        match = re.match(r"^data:image/([a-zA-Z0-9+.-]+);base64,(.+)$", url, re.DOTALL)
        if not match:
            raise ValueError("INVALID_IMAGE_DATA_URL")
        ext = match.group(1).lower()
        if ext == "jpeg":
            ext = "jpg"
        return ext, base64.b64decode(match.group(2))
    raise ValueError("UNSUPPORTED_IMAGE_URI")


def extract_prompt_and_images(messages: list[dict[str, Any]]) -> tuple[str, list[tuple[str, bytes]]]:
    text_sections: list[str] = []
    images: list[tuple[str, bytes]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).strip().lower()
        content = msg.get("content")
        parts_text: list[str] = []
        if isinstance(content, str):
            if content.strip():
                parts_text.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                if ptype == "text" and isinstance(part.get("text"), str) and part["text"].strip():
                    parts_text.append(part["text"].strip())
                elif ptype in ("image_url", "image", "input_image") or "image_url" in part:
                    raw_url = ""
                    if isinstance(part.get("image_url"), dict):
                        raw_url = str(part["image_url"].get("url", ""))
                    elif isinstance(part.get("image_url"), str):
                        raw_url = part["image_url"]
                    if raw_url:
                        images.append(decode_data_url(raw_url))
        if parts_text:
            prefix = f"[{role}] " if role not in ("user", "") else ""
            text_sections.append(prefix + "\n".join(parts_text))
    return "\n\n".join(text_sections).strip(), images


def run_agy(model: str, prompt_text: str, images: list[tuple[str, bytes]], timeout_sec: int = 120) -> tuple[str, dict[str, Any]]:
    agy_bin = os.environ.get("AGY_BIN") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    if not agy_bin or not Path(agy_bin).exists():
        raise RuntimeError("AGY_CLI_NOT_FOUND")

    temp_dir = Path(tempfile.mkdtemp(prefix="wowannwas-agy-"))
    try:
        saved_paths: list[Path] = []
        for idx, (ext, raw_bytes) in enumerate(images):
            safe_ext = re.sub(r"[^a-z0-9]", "", ext.lower()) or "jpg"
            img_path = temp_dir / f"image_{idx}.{safe_ext}"
            img_path.write_bytes(raw_bytes)
            saved_paths.append(img_path)

        if saved_paths:
            files_list = "\n".join(f"- Bild {i + 1}: {p}" for i, p in enumerate(saved_paths))
            full_prompt = (
                f"Lies zuerst alle folgenden Bilddateien direkt mit read_file ein und führe keinerlei Shell-Befehle oder Workspace-Suchen aus:\n"
                f"{files_list}\n\n"
                f"{prompt_text}"
            )
        else:
            full_prompt = prompt_text

        cmd = [
            agy_bin,
            "--model",
            model,
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "-p",
            full_prompt,
        ]
        env = os.environ.copy()
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{local_bin}:/opt/homebrew/bin:/usr/local/bin:{env.get('PATH', '')}"

        proc = subprocess.run(
            cmd,
            cwd=str(temp_dir),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise RuntimeError(f"AGY_EXIT_{proc.returncode}: {detail}")

        stdout = proc.stdout.strip()
        try:
            parsed = json.loads(stdout)
        except ValueError:
            return stdout, {"prompt_tokens": 0, "completion_tokens": len(stdout.split()), "total_tokens": len(stdout.split())}

        if isinstance(parsed, dict) and parsed.get("status") not in (None, "SUCCESS"):
            raise RuntimeError(f"AGY_STATUS_{parsed.get('status')}: {parsed.get('error') or parsed.get('response') or ''}")

        content = parsed.get("response", stdout) if isinstance(parsed, dict) else stdout
        usage = parsed.get("usage", {}) if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict) else {}
        return str(content), usage
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        expected = os.environ.get("AGY_BRIDGE_AUTH_KEY", "").strip()
        if not expected:
            return False
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
        return hmac.compare_digest(token, expected)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "wowannwas-agy-bridge"})
            return
        if self.path == "/v1/models":
            if not self._authorized():
                self._send_json(401, {"error": {"message": "Unauthorized"}})
                return
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": mid, "object": "model", "owned_by": "wowannwas-agy-bridge"} for mid in SUPPORTED_MODELS],
                },
            )
            return
        self._send_json(404, {"error": {"message": "Not found"}})

    def do_POST(self) -> None:
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": {"message": "Not found"}})
            return
        if not self._authorized():
            self._send_json(401, {"error": {"message": "Unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": {"message": "Invalid Content-Length"}})
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(413 if length > MAX_REQUEST_BYTES else 400, {"error": {"message": "Invalid request size"}})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"error": {"message": "Invalid JSON"}})
            return

        messages = body.get("messages")
        if not isinstance(messages, list):
            self._send_json(400, {"error": {"message": "messages array required"}})
            return

        model = resolve_model(body.get("model"))
        try:
            prompt_text, images = extract_prompt_and_images(messages)
            if not prompt_text and not images:
                self._send_json(400, {"error": {"message": "Empty prompt"}})
                return
            response_text, usage = run_agy(model, prompt_text, images)
        except ValueError as exc:
            self._send_json(400, {"error": {"message": str(exc)}})
            return
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": {"message": "AGY_TIMEOUT"}})
            return
        except Exception as exc:
            logger.exception("Bridge execution failed")
            self._send_json(502, {"error": {"message": str(exc)[:300]}})
            return

        self._send_json(
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": response_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )


def main() -> None:
    load_dotenv()
    host = os.environ.get("AGY_OWN_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("AGY_OWN_BRIDGE_PORT", "18096"))
    if not os.environ.get("AGY_BRIDGE_AUTH_KEY", "").strip():
        raise SystemExit("AGY_BRIDGE_AUTH_KEY must be set in environment or backend/.env")
    server = http.server.ThreadingHTTPServer((host, port), BridgeHandler)
    logger.info("WoWannWas Antigravity Bridge listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
