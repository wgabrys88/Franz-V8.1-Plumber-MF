import base64
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True, slots=True)
class PanelConfig:
    host: str = "127.0.0.1"
    port: int = 1236
    vlm_url: str = "http://127.0.0.1:1235/v1/chat/completions"
    vlm_timeout: float = 360.0
    annotate_timeout: float = 19.0
    lines_per_batch: int = 50
    runs_dir: str = "runs"
    sse_keepalive: float = 70.0
    norm: int = 1000


CFG = PanelConfig()
HERE = Path(__file__).resolve().parent
WIN32_PATH = HERE / "win32.py"
HTML_PATH = HERE / "panel.html"

_run_dir: Path = HERE
_log_path: Path = HERE / "log.jsonl"
_images_dir: Path = HERE / "images"
_log_lock = threading.Lock()
_log_line_count: int = 0
_log_batch_index: int = 0

_sse_lock = threading.Lock()
_sse_events: list[threading.Event] = []

_pending: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()

_brain_procs: dict[str, subprocess.Popen[bytes]] = {}
_brain_lock = threading.Lock()

_browser_ready = threading.Event()
_browser_connected: bool = False
_browser_lock = threading.Lock()

_startup_region: str = "NONE"
_startup_scale: float = 1.0


def _init_run_dir() -> None:
    global _run_dir, _log_path, _images_dir, _log_line_count, _log_batch_index
    ts = time.strftime("%Y%m%d_%H%M%S")
    _run_dir = HERE / CFG.runs_dir / ts
    _run_dir.mkdir(parents=True, exist_ok=True)
    _images_dir = _run_dir / "images"
    _images_dir.mkdir(exist_ok=True)
    _log_batch_index = 0
    _log_line_count = 0
    _log_path = _run_dir / f"log_{_log_batch_index:04d}.jsonl"


def _rotate_log_if_needed() -> None:
    global _log_path, _log_line_count, _log_batch_index
    if _log_line_count >= CFG.lines_per_batch:
        _log_batch_index += 1
        _log_line_count = 0
        _log_path = _run_dir / f"log_{_log_batch_index:04d}.jsonl"


def _save_png(name: str, png_bytes: bytes) -> str:
    path = _images_dir / f"{name}.png"
    path.write_bytes(png_bytes)
    return path.name


def _save_b64_as_png(name: str, b64_data: str) -> str:
    if not b64_data:
        return ""
    png_bytes = base64.b64decode(b64_data)
    return _save_png(name, png_bytes)


def _load_png_as_b64(filename: str) -> str:
    if not filename:
        return ""
    path = _images_dir / filename
    if not path.exists():
        return ""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _log(
    event: str, *,
    from_comp: str = "", to_comp: str = "", agent: str = "",
    request_id: str = "", label: str = "",
    error: bool = False, finish_reason: str = "",
    duration: float = 0.0, tokens: int = 0,
    image: str = "",
    **extra: Any,
) -> dict[str, Any]:
    _rotate_log_if_needed()
    global _log_line_count

    entry: dict[str, Any] = {"ts": time.time(), "event": event}
    if from_comp:
        entry["from"] = from_comp
    if to_comp:
        entry["to"] = to_comp
    if agent:
        entry["agent"] = agent
    if request_id:
        entry["request_id"] = request_id
    if label:
        entry["label"] = label
    if error:
        entry["error"] = True
    if finish_reason:
        entry["finish_reason"] = finish_reason
    if duration > 0:
        entry["duration"] = round(duration, 2)
    if tokens > 0:
        entry["tokens"] = tokens
    if image:
        entry["image"] = image
    if extra:
        entry["fields"] = extra

    line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"

    with _log_lock:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(line)
        _log_line_count += 1

    _notify_html()
    return entry


def _notify_html() -> None:
    with _sse_lock:
        for ev in _sse_events:
            ev.set()


def _win32(args: list[str], request_id: str, agent: str) -> subprocess.CompletedProcess[bytes]:
    cmd = [sys.executable, str(WIN32_PATH)] + args
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        _log("win32_error", from_comp="win32", to_comp="panel",
             agent=agent, request_id=request_id,
             label=f"win32 FAIL {args[0] if args else '?'}",
             error=True,
             returncode=proc.returncode,
             stderr=proc.stderr.decode(errors="replace"))
    return proc


def _select_region() -> str:
    proc = subprocess.run(
        [sys.executable, str(WIN32_PATH), "select_region"], capture_output=True,
    )
    if proc.returncode != 0:
        return "NONE"
    return proc.stdout.decode().strip()


def _tandem_select() -> tuple[str, float]:
    print("Select capture region...")
    _log("select_region", from_comp="panel", to_comp="panel", label="select region prompt")
    region = _select_region()
    if region == "NONE":
        _log("select_region", from_comp="panel", to_comp="panel", label="no region selected")
        return "NONE", 1.0
    print(f"Region: {region}")
    _log("select_region", from_comp="panel", to_comp="panel",
         label=f"region {region}", region=region)

    print("Select horizontal scale reference...")
    _log("select_scale", from_comp="panel", to_comp="panel", label="select scale prompt")
    scale_region = _select_region()
    if scale_region == "NONE":
        _log("select_scale", from_comp="panel", to_comp="panel", label="no scale selected")
        return region, 1.0
    parts = scale_region.split(",")
    if len(parts) != 4:
        _log("select_scale", from_comp="panel", to_comp="panel",
             label="invalid scale region", error=True, raw=scale_region)
        return region, 1.0
    scale = abs(int(parts[2]) - int(parts[0])) / float(CFG.norm)
    print(f"Scale: {scale:.4f}")
    _log("select_scale", from_comp="panel", to_comp="panel",
         label=f"scale {scale:.4f}", scale=scale)
    return region, scale


def _terminate_brains() -> None:
    with _brain_lock:
        for proc in _brain_procs.values():
            try:
                proc.terminate()
            except Exception:
                pass
        _brain_procs.clear()


def _extract_vlm_fields(vlm_request: dict[str, Any]) -> tuple[str, str, str]:
    messages = vlm_request.get("messages", [])
    system_prompt = user_message = vlm_image_b64 = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        match role:
            case "system":
                if isinstance(content, str):
                    system_prompt = content
            case "user":
                if isinstance(content, str):
                    user_message = content
                elif isinstance(content, list):
                    texts: list[str] = []
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        match part.get("type", ""):
                            case "text":
                                texts.append(part.get("text", ""))
                            case "image_url":
                                url = part.get("image_url", {}).get("url", "")
                                marker = ";base64,"
                                idx = url.find(marker)
                                if idx != -1:
                                    vlm_image_b64 = url[idx + len(marker):]
                    user_message = "\n".join(texts) if texts else ""
    return system_prompt, user_message, vlm_image_b64


def _handle_capture(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    region = body.get("region", "NONE")
    scale = body.get("capture_scale", 0.0)
    capture_size = body.get("capture_size", [0, 0])
    cmd = [sys.executable, str(WIN32_PATH), "capture", "--region", region]
    if scale > 0:
        cmd += ["--scale", str(scale)]
    elif capture_size[0] > 0 and capture_size[1] > 0:
        cmd += ["--width", str(capture_size[0]), "--height", str(capture_size[1])]
    else:
        return {"error": "capture requires capture_scale or capture_size"}

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True)
    duration = time.time() - t0

    if proc.returncode != 0:
        _log("capture_failed", from_comp="win32", to_comp="panel",
             agent=agent, request_id=rid,
             label="capture FAIL", error=True, duration=duration,
             returncode=proc.returncode,
             stderr=proc.stderr.decode(errors="replace"))
        return {"error": f"capture failed rc={proc.returncode}"}
    if not proc.stdout:
        _log("capture_empty", from_comp="win32", to_comp="panel",
             agent=agent, request_id=rid, label="capture empty", error=True)
        return {"error": "capture returned empty"}

    png_filename = _save_png(f"{rid}_capture", proc.stdout)
    image_b64 = base64.b64encode(proc.stdout).decode("ascii")

    _log("capture_done", from_comp="win32", to_comp="panel",
         agent=agent, request_id=rid,
         label=f"capture {agent}",
         image=png_filename,
         duration=duration)

    return {"image_b64": image_b64}


def _handle_annotate(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    with _browser_lock:
        if not _browser_connected:
            _log("annotate_skipped", from_comp="panel", to_comp="browser",
                 agent=agent, request_id=rid,
                 label="annotate SKIPPED (no browser)", error=True)
            return {"error": "browser not connected"}

    image_b64 = body.get("image_b64", "")
    overlays = body.get("overlays", [])

    input_png = _save_b64_as_png(f"{rid}_annotate_input", image_b64)

    annotation_request = {
        "request_id": rid,
        "agent": agent,
        "image": input_png,
        "overlays": overlays,
    }
    req_path = _run_dir / f"{rid}_annotate_request.json"
    req_path.write_text(json.dumps(annotation_request, separators=(",", ":")), encoding="utf-8")

    _log("annotate_request", from_comp="panel", to_comp="browser",
         agent=agent, request_id=rid,
         label=f"annotate {agent}",
         image=input_png,
         overlay_count=len(overlays))

    slot: dict[str, Any] = {"event": threading.Event(), "result": ""}
    with _pending_lock:
        _pending[rid] = slot

    if not slot["event"].wait(timeout=CFG.annotate_timeout):
        _log("annotate_timeout", from_comp="browser", to_comp="panel",
             agent=agent, request_id=rid, label="annotate TIMEOUT", error=True)
        with _pending_lock:
            _pending.pop(rid, None)
        return {"error": "annotate timeout"}

    result_b64 = slot["result"]
    output_png = _save_b64_as_png(f"{rid}_annotate_output", result_b64)

    _log("annotate_done", from_comp="browser", to_comp="panel",
         agent=agent, request_id=rid,
         label=f"annotated {agent}",
         image=output_png)

    return {"image_b64": result_b64}


def _handle_vlm(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    vlm_request = body.get("vlm_request", {})
    system_prompt, user_message, vlm_image_b64 = _extract_vlm_fields(vlm_request)

    image_file = ""
    if vlm_image_b64:
        image_file = _save_b64_as_png(f"{rid}_vlm_image", vlm_image_b64)

    _log("vlm_forward", from_comp="panel", to_comp="vlm",
         agent=agent, request_id=rid,
         label=f"vlm {agent}",
         image=image_file,
         system_prompt=system_prompt,
         user_message=user_message,
         max_tokens=vlm_request.get("max_tokens", 0),
         temperature=vlm_request.get("temperature", 0.0))

    t0 = time.time()
    fwd_body = json.dumps(vlm_request, separators=(",", ":")).encode()
    fwd_req = urllib.request.Request(
        CFG.vlm_url, data=fwd_body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(fwd_req, timeout=CFG.vlm_timeout) as resp:
            resp_bytes = resp.read()
        duration = time.time() - t0
        resp_obj = json.loads(resp_bytes)

        choices = resp_obj.get("choices", [])
        finish_reason = choices[0].get("finish_reason", "unknown") if choices else "none"
        vlm_reply = choices[0].get("message", {}).get("content", "") if choices else ""
        tokens = resp_obj.get("usage", {}).get("completion_tokens", 0)

        _log("vlm_response", from_comp="vlm", to_comp="panel",
             agent=agent, request_id=rid,
             label=f"reply {agent} ({finish_reason})",
             finish_reason=finish_reason, duration=duration, tokens=tokens,
             vlm_reply=vlm_reply)

        return resp_obj

    except urllib.error.HTTPError as exc:
        duration = time.time() - t0
        error_body = ""
        try:
            error_body = exc.read().decode(errors="replace")
        except Exception:
            pass
        _log("vlm_error", from_comp="vlm", to_comp="panel",
             agent=agent, request_id=rid,
             label=f"VLM ERROR HTTP {exc.code}",
             error=True, duration=duration,
             status=exc.code, error_body=error_body)
        return {"error": f"HTTP {exc.code}: {error_body}"}

    except Exception as exc:
        duration = time.time() - t0
        _log("vlm_error", from_comp="vlm", to_comp="panel",
             agent=agent, request_id=rid,
             label=f"VLM ERROR {exc}",
             error=True, duration=duration, error_text=str(exc))
        return {"error": str(exc)}


def _handle_device(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    actions = body.get("actions", [])
    region = body.get("region", "NONE")
    results: list[dict[str, Any]] = []
    t0 = time.time()

    action_arg_map: dict[str, list[str]] = {
        "drag": ["drag", "--from_pos", "{x1},{y1}", "--to_pos", "{x2},{y2}", "--region", "{region}"],
        "click": ["click", "--pos", "{x},{y}", "--region", "{region}"],
        "double_click": ["double_click", "--pos", "{x},{y}", "--region", "{region}"],
        "right_click": ["right_click", "--pos", "{x},{y}", "--region", "{region}"],
        "type_text": ["type_text", "--text", "{text}"],
        "press_key": ["press_key", "--key", "{key}"],
        "hotkey": ["hotkey", "--keys", "{keys}"],
        "scroll_up": ["scroll_up", "--pos", "{x},{y}", "--region", "{region}", "--clicks", "{clicks}"],
        "scroll_down": ["scroll_down", "--pos", "{x},{y}", "--region", "{region}", "--clicks", "{clicks}"],
    }

    for act in actions:
        action_type = act.get("type", "")
        _log("action_dispatch", from_comp="panel", to_comp="win32",
             agent=agent, request_id=rid,
             label=f"{action_type} {agent}", action_type=action_type)

        if action_type == "cursor_pos":
            p = _win32(["cursor_pos", "--region", region], rid, agent)
            stdout_text = p.stdout.decode(errors="replace").strip() if p.stdout else ""
            results.append({"type": action_type, "ok": p.returncode == 0, "pos": stdout_text})
            continue

        template = action_arg_map.get(action_type)
        if not template:
            results.append({"type": action_type, "ok": False, "error": f"unknown: {action_type}"})
            continue

        cmd_args: list[str] = []
        merged = dict(act)
        merged["region"] = region
        for part in template:
            if "{" in part:
                try:
                    cmd_args.append(part.format(**merged))
                except KeyError as ke:
                    results.append({"type": action_type, "ok": False, "error": f"missing field: {ke}"})
                    cmd_args = []
                    break
            else:
                cmd_args.append(part)

        if not cmd_args:
            continue

        p = _win32(cmd_args, rid, agent)
        results.append({"type": action_type, "ok": p.returncode == 0})

    duration = time.time() - t0
    ok_all = all(r.get("ok", False) for r in results)
    _log("device_done", from_comp="win32", to_comp="panel",
         agent=agent, request_id=rid,
         label=f"device {agent} {'ok' if ok_all else 'FAIL'}",
         error=not ok_all, duration=duration, action_count=len(results))
    return {"ok": ok_all, "results": results}


def _handle_log(body: dict[str, Any], rid: str, agent: str) -> dict[str, Any]:
    _log(body.get("log_event", "brain_log"),
         from_comp=f"brain.{agent}", to_comp="panel",
         agent=agent, request_id=rid,
         label=body.get("log_label", ""),
         error=body.get("log_error", False),
         **body.get("log_fields", {}))
    return {"ok": True}


class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json(self, code: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "bad json"})
            return None

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        match path:
            case "/":
                raw = HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)

            case "/ready":
                self._json(200, {
                    "ok": True,
                    "region": _startup_region,
                    "scale": _startup_scale,
                    "run_dir": str(_run_dir.relative_to(HERE)),
                })

            case "/events":
                ev = threading.Event()
                with _sse_lock:
                    _sse_events.append(ev)

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._cors()
                self.end_headers()
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()

                _log("sse_connect", from_comp="browser", to_comp="panel", label="SSE connect")

                with _browser_lock:
                    global _browser_connected
                    _browser_connected = True
                _browser_ready.set()

                try:
                    while True:
                        if ev.wait(timeout=CFG.sse_keepalive):
                            ev.clear()
                            self.wfile.write(b"event: update\ndata: {}\n\n")
                            self.wfile.flush()
                        else:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    with _sse_lock:
                        try:
                            _sse_events.remove(ev)
                        except ValueError:
                            pass
                        remaining = len(_sse_events)
                    with _browser_lock:
                        _browser_connected = remaining > 0
                    _log("sse_disconnect", from_comp="browser", to_comp="panel",
                         label="SSE disconnect")

            case "/logs":
                params = parse_qs(parsed.query)
                batch_str = params.get("batch", [""])[0]
                after_str = params.get("after", ["0"])[0]
                after_line = int(after_str)

                if batch_str == "":
                    batches = sorted(_run_dir.glob("log_*.jsonl"))
                    self._json(200, {
                        "batches": [b.name for b in batches],
                        "run_dir": str(_run_dir.relative_to(HERE)),
                    })
                    return

                log_file = _run_dir / batch_str
                if not log_file.exists() or not log_file.name.startswith("log_"):
                    self._json(404, {"error": "batch not found"})
                    return

                lines: list[dict[str, Any]] = []
                with log_file.open("r", encoding="utf-8") as f:
                    for i, raw_line in enumerate(f):
                        if i < after_line:
                            continue
                        raw_line = raw_line.strip()
                        if raw_line:
                            try:
                                lines.append(json.loads(raw_line))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                pass

                self._json(200, {"batch": batch_str, "after": after_line, "lines": lines})

            case _ if path.startswith("/images/"):
                filename = path[len("/images/"):]
                img_path = _images_dir / filename
                if not img_path.exists() or not img_path.name.endswith(".png"):
                    self._json(404, {"error": "image not found"})
                    return
                raw = img_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)

            case "/annotate-request":
                params = parse_qs(parsed.query)
                rid = params.get("rid", [""])[0]
                if not rid:
                    self._json(400, {"error": "rid required"})
                    return
                req_path = _run_dir / f"{rid}_annotate_request.json"
                if not req_path.exists():
                    self._json(404, {"error": "no pending annotation"})
                    return
                data = json.loads(req_path.read_text(encoding="utf-8"))
                self._json(200, data)

            case _:
                self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?")[0]

        match path:
            case "/route":
                req = self._read_body()
                if req is None:
                    return
                agent = req.get("agent")
                recipients = req.get("recipients")
                if not agent or not isinstance(recipients, list):
                    self._json(400, {"error": "agent and recipients[] required"})
                    return
                rid = str(uuid.uuid4())
                _log("route", from_comp=f"brain.{agent}", to_comp="panel",
                     agent=agent, request_id=rid,
                     label=f"{agent}:{recipients}", recipients=recipients)

                result: dict[str, Any]
                target = recipients[0] if recipients else ""

                match target:
                    case "win32_capture":
                        result = _handle_capture(req, rid, agent)
                    case "annotate":
                        result = _handle_annotate(req, rid, agent)
                    case "vlm":
                        result = _handle_vlm(req, rid, agent)
                    case "win32_device":
                        result = _handle_device(req, rid, agent)
                    case "log":
                        result = _handle_log(req, rid, agent)
                    case _:
                        result = {"error": f"unknown target: {target}"}

                result["request_id"] = rid
                self._json(200 if "error" not in result else 502, result)

            case "/result":
                data = self._read_body()
                if data is None:
                    return
                rid_val = data.get("request_id", "")
                annotated = data.get("image_b64", "")
                with _pending_lock:
                    slot = _pending.pop(rid_val, None)
                if slot:
                    slot["result"] = annotated
                    slot["event"].set()
                    _log("result_received", from_comp="browser", to_comp="panel",
                         request_id=rid_val, label="annotation result")
                self._json(200, {"ok": True})

            case _:
                self._json(404, {"error": "not found"})


def start_server(host: str = CFG.host, port: int = CFG.port) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer((host, port), PanelHandler)
    _log("server_start", from_comp="panel", to_comp="panel", label=f"{host}:{port}")
    return server


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: panel.py <brain_file.py>")
        print("       panel.py --replay <run_dir>")
        raise SystemExit(1)

    if sys.argv[1] == "--replay":
        if len(sys.argv) < 3:
            print("Usage: panel.py --replay <run_dir>")
            raise SystemExit(1)
        replay_dir = Path(sys.argv[2])
        if not replay_dir.exists():
            replay_dir = HERE / CFG.runs_dir / sys.argv[2]
        if not replay_dir.exists():
            print(f"ERROR: {replay_dir} not found")
            raise SystemExit(1)
        _run_dir = replay_dir.resolve()
        _images_dir = _run_dir / "images"
        _log_path = sorted(_run_dir.glob("log_*.jsonl"))[-1] if list(_run_dir.glob("log_*.jsonl")) else _run_dir / "log_0000.jsonl"
        print(f"Replay from {_run_dir}")
        srv = start_server()
        print(f"Panel replay on http://{CFG.host}:{CFG.port}")
        webbrowser.open(f"http://{CFG.host}:{CFG.port}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        brain_arg = sys.argv[1]
        brain_path = HERE / brain_arg
        if not brain_path.exists():
            print(f"ERROR: {brain_arg} not found")
            raise SystemExit(1)

        _init_run_dir()

        _startup_region, _startup_scale = _tandem_select()
        if _startup_region == "NONE":
            print("No region selected, exiting.")
            raise SystemExit(1)

        _log("startup", from_comp="panel", to_comp="panel", label="startup",
             region=_startup_region, scale=_startup_scale)
        print(f"Region: {_startup_region}  Scale: {_startup_scale:.4f}")
        print(f"Run dir: {_run_dir}")

        srv = start_server()
        print(f"Panel on http://{CFG.host}:{CFG.port}")
        webbrowser.open(f"http://{CFG.host}:{CFG.port}")

        def _launch_brain_when_ready():
            print("Waiting for browser to connect...")
            if not _browser_ready.wait(timeout=30):
                print("ERROR: Browser did not connect within 30 seconds.")
                os._exit(1)
            print("Browser connected.")

            proc = subprocess.Popen(
                [sys.executable, str(brain_path),
                 "--region", _startup_region, "--scale", str(_startup_scale)],
            )
            with _brain_lock:
                _brain_procs[brain_path.stem] = proc
            _log("brain_launched", from_comp="panel", to_comp="brain",
                 label=f"launch {brain_arg}", pid=proc.pid)
            print(f"Brain {brain_arg} pid={proc.pid}")

        threading.Thread(target=_launch_brain_when_ready, daemon=True).start()

        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _terminate_brains()
