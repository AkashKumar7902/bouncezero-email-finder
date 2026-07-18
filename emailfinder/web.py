"""Minimal localhost web UI over the SAME core, using ONLY stdlib http.server.

Zero web-framework dependency, fully offline, no CDN / external URLs (CSP-safe),
bound to ``127.0.0.1`` by default. A single inline single-page app (HTML + CSS +
vanilla JS, no build step) plus a thin set of JSON endpoints that wrap the shared
:class:`~emailfinder.engine.Engine`.

The page surfaces, honestly:
  * a single-lookup card -> provider badge + 0-100 confidence bar + status chip
    that shows the M365 / catch-all cap note whenever it applies;
  * a 'why this guess' popover fed by :class:`ScoredCandidate.reasons` + the
    template/separator/provider/verification_mode provenance;
  * a batch CSV upload -> results table -> enriched CSV download;
  * a bounce re-score panel -> fix-list table -> fix-list download;
  * a public no-login opt-out form feeding the global suppression list.

Safety invariants honored by every response (dossier 5 / 8.1):
  * Microsoft 365 and catch-all domains are NEVER labelled DELIVERABLE — the
    scorer caps them and the UI shows the cap note explicitly;
  * a timeout / port-25 block surfaces as ``verification_unavailable`` (never
    "invalid");
  * LinkedIn URLs are only ever slug-parsed locally by the engine — this module
    performs ZERO network I/O against linkedin.com.

``batch`` and ``rescore`` are imported lazily inside the handlers that need them
so importing this module (and the core lookup / opt-out endpoints) never hard-
depends on those surfaces being present.
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models import FindResult, Provider, ScoredCandidate

# --------------------------------------------------------------------------- #
# Human-facing labels
# --------------------------------------------------------------------------- #
_PROVIDER_LABELS: dict[str, str] = {
    Provider.MICROSOFT365.value: "Microsoft 365",
    Provider.GOOGLE_WORKSPACE.value: "Google Workspace",
    Provider.CONSUMER_GMAIL.value: "Gmail",
    Provider.PROOFPOINT.value: "Proofpoint",
    Provider.MIMECAST.value: "Mimecast",
    Provider.CISCO_IRONPORT.value: "Cisco IronPort",
    Provider.BARRACUDA.value: "Barracuda",
    Provider.ZOHO.value: "Zoho",
    Provider.AMAZON_SES.value: "Amazon SES",
    Provider.YAHOO_AOL.value: "Yahoo / AOL",
    Provider.OTHER.value: "Other",
    Provider.NONE_UNKNOWN.value: "Unknown",
}


def _provider_label(provider: Provider) -> str:
    """Human-readable badge label for a Provider enum."""
    return _PROVIDER_LABELS.get(provider.value, provider.value)


def _cap_note(provider: Provider, is_catch_all: bool) -> str | None:
    """Return the honest cap note for M365 / catch-all, else None.

    These are the two cases the scorer hard-caps and the UI MUST surface so a
    user never mistakes a pattern-only guess for a verified address.
    """
    if provider == Provider.MICROSOFT365:
        return "capped: Microsoft 365 not RCPT-verifiable"
    if is_catch_all:
        return "catch-all: pattern-only"
    return None


# --------------------------------------------------------------------------- #
# Serialization (the UI never re-derives — reasons[] travel with every result)
# --------------------------------------------------------------------------- #
def _scored_to_dict(
    sc: ScoredCandidate | None, domain: str | None, provider: Provider
) -> dict | None:
    """Serialize one ScoredCandidate including its reasons[] + provenance."""
    if sc is None:
        return None
    cand = sc.candidate
    local = cand.local_part
    email = f"{local}@{domain}" if domain else local
    return {
        "email": email,
        "local_part": local,
        "template": cand.template,
        "separator": cand.separator,
        "shape": cand.shape,
        "prior": cand.prior,
        "source": cand.source,
        "name_origin": cand.name_origin,
        "score": sc.score,
        "status": sc.status.value,
        "is_catch_all": sc.is_catch_all,
        "is_role": sc.is_role,
        "is_disposable": sc.is_disposable,
        "webmail": sc.webmail,
        "reasons": list(sc.reasons),
        "cap_note": _cap_note(provider, sc.is_catch_all),
    }


def render_result_json(result: FindResult) -> dict:
    """Serialize a FindResult (incl. per-candidate reasons) for the UI.

    Every candidate carries its reasons[] trail and, where applicable, the
    honest M365 / catch-all cap note, so the front-end never has to re-derive
    scoring or deliverability logic.
    """
    provider = result.provider
    mx = None
    if result.mx is not None:
        mx = {
            "domain": result.mx.domain,
            "hosts": list(result.mx.hosts),
            "is_implicit": result.mx.is_implicit,
            "error": result.mx.error,
        }
    return {
        "suppressed": result.suppressed,
        "domain": result.domain,
        "provider": provider.value,
        "provider_label": _provider_label(provider),
        "strategy": result.strategy.value,
        "verification_mode": result.verification_mode,
        "provenance_id": result.provenance_id,
        "mx": mx,
        "notes": list(result.notes),
        "query": result.query,
        "best": _scored_to_dict(result.best, result.domain, provider),
        "alternates": [
            _scored_to_dict(a, result.domain, provider) for a in result.alternates
        ],
    }


# --------------------------------------------------------------------------- #
# multipart / body helpers (stdlib only — cgi was removed in 3.13)
# --------------------------------------------------------------------------- #
def _parse_content_type(header: str | None) -> tuple[str, dict[str, str]]:
    """Split a Content-Type header into ``(mime, params)``."""
    if not header:
        return "", {}
    parts = header.split(";")
    mime = parts[0].strip().lower()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')
    return mime, params


def _disp_attr(header: str, key: str) -> str | None:
    """Extract ``key="value"`` from a Content-Disposition header line."""
    m = re.search(key + r'="([^"]*)"', header)
    return m.group(1) if m else None


def _parse_multipart(body: bytes, content_type: str | None) -> dict[str, dict]:
    """Parse a multipart/form-data body into ``{field_name: {...}}``.

    File fields yield ``{"filename": str, "data": bytes}``; plain fields yield
    ``{"value": str}``. A pure-stdlib parser (the ``cgi`` module was removed in
    Python 3.13); tolerant of malformed parts, which are skipped.
    """
    _mime, params = _parse_content_type(content_type)
    boundary = params.get("boundary")
    if not boundary:
        return {}
    delim = b"--" + boundary.encode("latin-1")
    fields: dict[str, dict] = {}
    for chunk in body.split(delim):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        head, data = chunk.split(b"\r\n\r\n", 1)
        headers = head.decode("utf-8", "replace")
        disp = ""
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition"):
                disp = line
                break
        name = _disp_attr(disp, "name")
        if name is None:
            continue
        filename = _disp_attr(disp, "filename")
        if filename is not None:
            fields[name] = {"filename": filename, "data": data}
        else:
            fields[name] = {"value": data.decode("utf-8", "replace").strip()}
    return fields


def _as_bool(value) -> bool:
    """Coerce a form/JSON scalar to bool (accepts on/true/1/yes)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# request handler factory
# --------------------------------------------------------------------------- #
def create_handler(engine) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass bound to one shared Engine.

    Serves the inline single-page app (no external URLs) plus the JSON
    endpoints. All engine access is serialized behind a lock so concurrent
    requests share the single Engine / SQLite cache safely.
    """
    lock = threading.Lock()
    # Last enriched CSV produced by a batch run, for GET /api/export.
    export_state: dict[str, bytes | str | None] = {"csv": None, "name": None}

    class Handler(BaseHTTPRequestHandler):
        server_version = "BounceZeroWeb/0.1"
        protocol_version = "HTTP/1.1"

        # -- low-level responders --------------------------------------- #
        def _send(self, code: int, body: bytes, ctype: str,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # Lock the page down: same-origin only, no external anything.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline' 'self'; "
                "style-src 'unsafe-inline' 'self'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'self'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _read_body(self) -> bytes:
            # do_POST drains the whole body up-front into _body_cache, so every
            # handler (and every early 404/error return) reads the same bytes and
            # the request is always fully consumed — otherwise an unread body
            # desyncs the next request on a keep-alive HTTP/1.1 connection.
            return getattr(self, "_body_cache", b"")

        def _drain_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _read_json(self) -> dict:
            try:
                raw = self._read_body()
                return json.loads(raw or b"{}")
            except (ValueError, TypeError):
                return {}

        def log_message(self, fmt, *args):  # noqa: D401 - quiet by default
            """Suppress the default stderr access log (kept tidy for a CLI)."""
            return

        # -- routing ---------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/export":
                self._handle_export()
                return
            if path.startswith("/api/kb/"):
                self._handle_kb(unquote(path[len("/api/kb/"):]))
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            # Always consume the full request body FIRST (fresh per request) so
            # no handler or error path can leave bytes unread and desync a
            # keep-alive connection.
            self._body_cache = self._drain_body()
            try:
                if path == "/api/find":
                    self._handle_find()
                elif path == "/api/batch":
                    self._handle_batch()
                elif path == "/api/rescore":
                    self._handle_rescore()
                elif path == "/api/feedback":
                    self._handle_feedback()
                elif path == "/api/optout":
                    self._handle_optout()
                else:
                    self._json(404, {"error": "not_found"})
            except _NotAvailable as exc:
                self._json(503, {"error": "unavailable", "detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 - never leak a stack to the UI
                self._json(400, {"error": "bad_request", "detail": str(exc)})

        # -- endpoint handlers ------------------------------------------ #
        def _handle_find(self) -> None:
            data = self._read_json()
            name = (data.get("name") or "").strip() or None
            domain = (data.get("domain") or "").strip() or None
            company = (data.get("company") or "").strip() or None
            linkedin_url = (data.get("linkedin_url") or "").strip() or None
            verify = _as_bool(data.get("verify", False))
            use_providers = _as_bool(data.get("use_providers", False))
            with lock:
                result = engine.find(
                    name,
                    domain,
                    company=company,
                    linkedin_url=linkedin_url,
                    verify=verify,
                    use_providers=use_providers,
                )
            self._json(200, render_result_json(result))

        def _handle_feedback(self) -> None:
            data = self._read_json()
            email = (data.get("email") or "").strip()
            domain = (data.get("domain") or "").strip()
            deliverable = _as_bool(data.get("deliverable", False))
            if not email or not domain:
                self._json(400, {"error": "email and domain required"})
                return
            with lock:
                engine.confirm(email, domain, deliverable=deliverable)
            self._json(200, {"ok": True, "email": email, "deliverable": deliverable})

        def _handle_optout(self) -> None:
            data = self._read_json()
            email = (data.get("email") or "").strip() or None
            name = (data.get("name") or "").strip() or None
            domain = (data.get("domain") or "").strip() or None
            if not email and not (name and domain):
                self._json(400, {"error": "provide an email or a name + domain"})
                return
            with lock:
                engine.compliance.add_suppression(email, name, domain, "web-optout")
            # Public opt-out is a 204 (no content), per the contract.
            self._send(204, b"", "text/plain; charset=utf-8")

        def _handle_kb(self, domain: str) -> None:
            domain = (domain or "").strip().lower()
            if not domain:
                self._json(400, {"error": "domain required"})
                return
            with lock:
                entry = _kb_lookup(engine, domain)
            if entry is None:
                self._json(404, {"error": "no_kb_entry", "domain": domain})
                return
            self._json(200, {"domain": domain, "entry": _jsonable(entry)})

        def _handle_batch(self) -> None:
            batch = _import_batch()
            in_path, _fname = self._save_upload(default_ext=".csv")
            out_path = Path(tempfile.mkstemp(suffix=".enriched.csv")[1])
            fields = self._last_fields or {}
            verify = _as_bool(_field_value(fields, "verify"))
            use_providers = _as_bool(_field_value(fields, "use_providers"))
            try:
                with lock:
                    stats = batch.run_batch(
                        engine,
                        in_path,
                        out_path,
                        verify=verify,
                        use_providers=use_providers,
                    )
                csv_text = out_path.read_text(encoding="utf-8")
            finally:
                _unlink(in_path)
                _unlink(out_path)
            export_state["csv"] = csv_text
            export_state["name"] = "bouncezero_enriched.csv"
            columns, rows = _csv_to_rows(csv_text)
            self._json(
                200,
                {
                    "columns": columns,
                    "rows": rows,
                    "csv": csv_text,
                    "stats": _stats_to_dict(stats),
                },
            )

        def _handle_rescore(self) -> None:
            rescore = _import_rescore()
            in_path, fname = self._save_upload(default_ext=".csv")
            fields = self._last_fields or {}
            apply_kb = _as_bool(_field_value(fields, "apply_kb"))
            kb_path = getattr(engine, "_kb_path", None)
            try:
                with lock:
                    if fname.lower().endswith((".mbox", ".mbx")):
                        items = rescore.rescore_mailbox(
                            in_path, engine, kb_path, apply_kb=apply_kb
                        )
                    else:
                        items = rescore.rescore_csv(
                            in_path, engine, kb_path, apply_kb=apply_kb
                        )
                out_path = Path(tempfile.mkstemp(suffix=".fixlist.csv")[1])
                try:
                    rescore.write_fixlist(items, out_path)
                    csv_text = out_path.read_text(encoding="utf-8")
                finally:
                    _unlink(out_path)
            finally:
                _unlink(in_path)
            columns, rows = _csv_to_rows(csv_text)
            self._json(
                200,
                {
                    "columns": columns,
                    "rows": rows,
                    "csv": csv_text,
                    "count": len(items),
                },
            )

        def _handle_export(self) -> None:
            csv_text = export_state.get("csv")
            if not csv_text:
                self._json(404, {"error": "no_export", "detail": "run a batch first"})
                return
            name = export_state.get("name") or "export.csv"
            self._send(
                200,
                str(csv_text).encode("utf-8"),
                "text/csv; charset=utf-8",
                extra={"Content-Disposition": f'attachment; filename="{name}"'},
            )

        # -- upload helper ---------------------------------------------- #
        def _save_upload(self, default_ext: str = ".csv") -> tuple[Path, str]:
            """Persist an uploaded file to a temp path; return ``(path, name)``.

            Accepts a multipart ``file`` field or, as a fallback, a raw
            ``text/csv`` request body. The multipart form fields are stashed on
            ``self._last_fields`` for the caller to read toggles from.
            """
            body = self._read_body()
            ctype = self.headers.get("Content-Type", "")
            self._last_fields: dict[str, dict] = {}
            data: bytes | None = None
            filename = "upload" + default_ext
            mime, _params = _parse_content_type(ctype)
            if mime == "multipart/form-data":
                fields = _parse_multipart(body, ctype)
                self._last_fields = fields
                file_field = fields.get("file") or _first_file(fields)
                if file_field is not None:
                    data = file_field.get("data")
                    filename = file_field.get("filename") or filename
            if data is None:
                # Raw-body fallback (e.g. text/csv posted directly).
                data = body
            fd, tmp = tempfile.mkstemp(suffix=default_ext)
            path = Path(tmp)
            import os

            with os.fdopen(fd, "wb") as fh:
                fh.write(data or b"")
            return path, filename

    return Handler


# --------------------------------------------------------------------------- #
# module-private helpers
# --------------------------------------------------------------------------- #
class _NotAvailable(RuntimeError):
    """A surface module (batch / rescore) is not installed."""


def _import_batch():
    try:
        from . import batch
    except ImportError as exc:  # pragma: no cover - only when batch absent
        raise _NotAvailable("batch surface not available") from exc
    return batch


def _import_rescore():
    try:
        from . import rescore
    except ImportError as exc:  # pragma: no cover - only when rescore absent
        raise _NotAvailable("rescore surface not available") from exc
    return rescore


def _jsonable(obj):
    """Recursively convert sets (which kb_store keeps in memory) to sorted lists.

    The KB overlay stores ``known_bad_locals`` / ``no_bounce_locals`` as sets in
    memory (they round-trip to sorted lists on save); JSON can't serialize a set,
    so the read-only KB endpoint normalizes them here without mutating the KB.
    """
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _kb_lookup(engine, domain: str) -> dict | None:
    """Case-insensitive lookup into the engine's loaded KB overlay."""
    kb = getattr(engine, "kb", None)
    if not isinstance(kb, dict):
        return None
    if domain in kb:
        return kb[domain]
    for key, value in kb.items():
        if isinstance(key, str) and key.lower() == domain:
            return value
    return None


def _first_file(fields: dict[str, dict]) -> dict | None:
    for field in fields.values():
        if "data" in field:
            return field
    return None


def _field_value(fields: dict[str, dict], name: str) -> str:
    field = fields.get(name)
    if not field:
        return ""
    return str(field.get("value", ""))


def _csv_to_rows(csv_text: str) -> tuple[list[str], list[dict]]:
    """Parse CSV text into ``(header, list-of-row-dicts)``."""
    import csv
    import io

    reader = csv.DictReader(io.StringIO(csv_text))
    columns = list(reader.fieldnames or [])
    rows = [dict(r) for r in reader]
    return columns, rows


def _stats_to_dict(stats) -> dict:
    """Best-effort serialization of a BatchStats-like object."""
    if stats is None:
        return {}
    if isinstance(stats, dict):
        return stats
    for attr in ("__dict__",):
        d = getattr(stats, attr, None)
        if isinstance(d, dict):
            return {k: v for k, v in d.items() if not k.startswith("_")}
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(stats):
            return asdict(stats)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _enforce_loopback(host: str) -> str:
    """Refuse to bind anywhere but loopback.

    The UI serves derived personal data and has no auth; exposing it on a public
    interface would defeat the clean-room, single-user posture. A non-loopback
    host is hard-normalized back to ``127.0.0.1`` with a loud warning rather than
    honored.
    """
    if (host or "").strip().lower() not in _LOOPBACK_HOSTS:
        print(
            f"WARNING: refusing to bind non-loopback host {host!r}; "
            "using 127.0.0.1 (the local UI is single-user and unauthenticated)."
        )
        return "127.0.0.1"
    return host or "127.0.0.1"


def serve(engine, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local web UI on loopback only.

    Uses a threading server so a long batch never blocks a single lookup, but
    every engine call is serialized behind a lock inside the handler. Binds
    ``127.0.0.1`` by default — never a public interface.
    """
    host = _enforce_loopback(host)
    handler = create_handler(engine)
    httpd = ThreadingHTTPServer((host, port), handler)
    bound_host, bound_port = httpd.server_address[:2]
    print(f"BounceZero web UI: http://{bound_host}:{bound_port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# The single inline page (no external URLs — CSP-safe, light + dark)
# --------------------------------------------------------------------------- #
_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BounceZero — email finder</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d23; --muted:#5c6470;
    --line:#e3e6ea; --accent:#2f6df6; --accent-ink:#ffffff;
    --ok:#188a4a; --ok-bg:#e4f5ea; --warn:#9a6a00; --warn-bg:#fff2d6;
    --bad:#b3261e; --bad-bg:#fbe3e1; --unk:#5c6470; --unk-bg:#eceef1;
    --shadow:0 1px 3px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0f1216; --card:#171b21; --ink:#e8eaed; --muted:#9aa3af;
      --line:#262c34; --accent:#5b8bff; --accent-ink:#0f1216;
      --ok:#5fd18b; --ok-bg:#123222; --warn:#e6bf6a; --warn-bg:#33280f;
      --bad:#f2938c; --bad-bg:#3a1917; --unk:#9aa3af; --unk-bg:#20262e;
      --shadow:0 1px 3px rgba(0,0,0,.5);
    }
  }
  *{box-sizing:border-box}
  html,body{max-width:100%;overflow-x:hidden}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{padding:22px 20px 6px;max-width:960px;margin:0 auto}
  h1{margin:0;font-size:22px;letter-spacing:-.2px}
  .tag{color:var(--muted);font-size:13px;margin-top:2px}
  main{max-width:960px;margin:0 auto;padding:12px 20px 60px;
    display:grid;gap:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:18px;box-shadow:var(--shadow)}
  .card h2{margin:0 0 12px;font-size:15px;text-transform:uppercase;
    letter-spacing:.06em;color:var(--muted)}
  label{display:block;font-size:13px;color:var(--muted);margin:8px 0 4px}
  input[type=text],select,textarea{width:100%;padding:9px 10px;border-radius:8px;
    border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>div{flex:1 1 200px;min-width:0}
  .toggles{display:flex;gap:18px;align-items:center;margin-top:12px;flex-wrap:wrap}
  .toggles label{display:flex;gap:6px;align-items:center;margin:0;color:var(--ink)}
  button{cursor:pointer;font:inherit;border:0;border-radius:8px;padding:9px 16px;
    background:var(--accent);color:var(--accent-ink);font-weight:600}
  button.ghost{background:transparent;color:var(--accent);
    border:1px solid var(--line)}
  button:disabled{opacity:.5;cursor:default}
  .actions{margin-top:14px;display:flex;gap:10px;flex-wrap:wrap}
  .badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;
    font-weight:600;background:var(--unk-bg);color:var(--unk)}
  .chip{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;
    font-weight:700;text-transform:uppercase;letter-spacing:.04em}
  .chip.deliverable{background:var(--ok-bg);color:var(--ok)}
  .chip.risky{background:var(--warn-bg);color:var(--warn)}
  .chip.undeliverable{background:var(--bad-bg);color:var(--bad)}
  .chip.unknown{background:var(--unk-bg);color:var(--unk)}
  .email{font-size:20px;font-weight:700;word-break:break-all;margin:6px 0}
  .bar{height:10px;border-radius:999px;background:var(--unk-bg);overflow:hidden;
    margin:8px 0}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .cap{color:var(--warn);font-size:13px;margin-top:6px;font-weight:600}
  .meta{color:var(--muted);font-size:13px;margin-top:8px}
  .why{margin-top:12px}
  .why summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:13px}
  .why ul{margin:8px 0 0;padding-left:18px}
  .why li{margin:2px 0;font-size:13px}
  .tblwrap{overflow-x:auto;margin-top:10px;-webkit-overflow-scrolling:touch}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;
    white-space:nowrap}
  th{color:var(--muted);cursor:pointer;user-select:none}
  .alt{border-top:1px solid var(--line);margin-top:14px;padding-top:12px}
  .err{color:var(--bad);font-size:13px;margin-top:8px}
  .hint{color:var(--muted);font-size:12px;margin-top:4px}
  .rowbtns button{padding:4px 8px;font-size:12px;margin-right:4px}
  .note{color:var(--muted);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<header>
  <h1>BounceZero</h1>
  <div class="tag">offline-first, provider-aware email finder &amp; verifier — local only</div>
</header>
<main>

  <section class="card" id="lookup">
    <h2>Single lookup</h2>
    <div class="row">
      <div>
        <label for="name">Full name</label>
        <input type="text" id="name" placeholder="Ajith Kumar" autocomplete="off">
      </div>
      <div>
        <label for="ttype">Target</label>
        <select id="ttype">
          <option value="domain">Domain</option>
          <option value="company">Company</option>
          <option value="linkedin">LinkedIn URL</option>
        </select>
      </div>
      <div>
        <label for="target" id="tlabel">Domain</label>
        <input type="text" id="target" placeholder="acme.com" autocomplete="off">
        <div class="hint" id="thint">e.g. acme.com</div>
      </div>
    </div>
    <div class="toggles">
      <label><input type="checkbox" id="verify"> Verify (SMTP)</label>
      <label><input type="checkbox" id="providers"> Use providers</label>
    </div>
    <div class="actions">
      <button id="findbtn">Find email</button>
    </div>
    <div id="findout"></div>
  </section>

  <section class="card" id="batchcard">
    <h2>Batch CSV</h2>
    <div class="note">Columns: any of name | first,last and domain | company | linkedin_url.</div>
    <div class="row" style="margin-top:8px">
      <div><input type="file" id="batchfile" accept=".csv,text/csv"></div>
    </div>
    <div class="toggles">
      <label><input type="checkbox" id="bverify"> Verify</label>
      <label><input type="checkbox" id="bproviders"> Use providers</label>
    </div>
    <div class="actions">
      <button id="batchbtn">Enrich</button>
      <button class="ghost" id="batchdl" disabled>Download enriched CSV</button>
    </div>
    <div id="batchout"></div>
  </section>

  <section class="card" id="rescard">
    <h2>Re-score bounces</h2>
    <div class="note">Upload a bounced/audit CSV or a DSN .mbox. Buckets by RFC 3463 code.</div>
    <div class="row" style="margin-top:8px">
      <div><input type="file" id="resfile" accept=".csv,.mbox,.mbx"></div>
    </div>
    <div class="toggles">
      <label><input type="checkbox" id="applykb"> Apply to KB (learn)</label>
    </div>
    <div class="actions">
      <button id="resbtn">Re-score</button>
      <button class="ghost" id="resdl" disabled>Download fix-list</button>
    </div>
    <div id="resout"></div>
  </section>

  <section class="card" id="optcard">
    <h2>Opt out</h2>
    <div class="note">Ask us never to derive or return your address. No login required.</div>
    <div class="row" style="margin-top:8px">
      <div>
        <label for="oemail">Email</label>
        <input type="text" id="oemail" placeholder="you@company.com" autocomplete="off">
      </div>
      <div>
        <label for="oname">or Name</label>
        <input type="text" id="oname" placeholder="Jane Doe" autocomplete="off">
      </div>
      <div>
        <label for="odomain">+ Domain</label>
        <input type="text" id="odomain" placeholder="company.com" autocomplete="off">
      </div>
    </div>
    <div class="actions">
      <button id="optbtn">Submit opt-out</button>
    </div>
    <div id="optout"></div>
  </section>

</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function postJSON(url, body){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)});
  return r;
}

// --- target field label swaps -------------------------------------------
const HINTS = {domain:"e.g. acme.com", company:"e.g. Acme Corp",
  linkedin:"e.g. linkedin.com/in/your-name"};
$("ttype").addEventListener("change", () => {
  const t = $("ttype").value;
  $("tlabel").textContent = t === "linkedin" ? "LinkedIn URL"
    : (t.charAt(0).toUpperCase() + t.slice(1));
  $("thint").textContent = HINTS[t];
});

function statusChip(sc){
  const s = (sc.status || "unknown");
  return `<span class="chip ${esc(s)}">${esc(s)}</span>`;
}

function renderCandidate(sc, provLabel){
  if(!sc) return "";
  const pct = Math.max(0, Math.min(100, sc.score|0));
  const reasons = (sc.reasons||[]).map(r => `<li>${esc(r)}</li>`).join("");
  const cap = sc.cap_note ? `<div class="cap">⚠ ${esc(sc.cap_note)}</div>` : "";
  const prov = `<span class="badge">${esc(provLabel)}</span>`;
  const flags = [];
  if(sc.is_catch_all) flags.push("catch-all");
  if(sc.is_role) flags.push("role");
  if(sc.is_disposable) flags.push("disposable");
  if(sc.webmail) flags.push("webmail");
  const flagstr = flags.length ? " · " + flags.join(", ") : "";
  return `
    <div class="email">${esc(sc.email)}</div>
    <div>${prov} ${statusChip(sc)} <span class="meta">${pct}/100${esc(flagstr)}</span></div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${cap}
    <div class="meta">template <b>${esc(sc.template)}</b> · separator
      "${esc(sc.separator)}" · source ${esc(sc.source)}</div>
    <details class="why"><summary>Why this guess</summary>
      <ul>${reasons || "<li>no reasons recorded</li>"}</ul>
      <div class="meta">name origin: ${esc(sc.name_origin)}</div>
    </details>
    <div class="rowbtns" style="margin-top:10px">
      <button class="ghost" data-fb="good" data-email="${esc(sc.email)}">Deliverable</button>
      <button class="ghost" data-fb="bad" data-email="${esc(sc.email)}">Bounced</button>
    </div>`;
}

async function doFind(){
  const btn = $("findbtn"); btn.disabled = true;
  const out = $("findout"); out.innerHTML = '<div class="meta">searching…</div>';
  const body = {name: $("name").value, verify: $("verify").checked,
    use_providers: $("providers").checked};
  const t = $("ttype").value; const v = $("target").value.trim();
  if(t === "domain") body.domain = v;
  else if(t === "company") body.company = v;
  else body.linkedin_url = v;
  try{
    const r = await postJSON("/api/find", body);
    const j = await r.json();
    if(!r.ok){ out.innerHTML = `<div class="err">${esc(j.detail||j.error||"error")}</div>`; return; }
    if(j.suppressed){
      out.innerHTML = `<div class="chip undeliverable">suppressed</div>
        <div class="meta">This identity opted out — no address returned.</div>`;
      return;
    }
    if(!j.best){
      const notes = (j.notes||[]).map(n=>esc(n)).join("; ");
      out.innerHTML = `<div class="meta">No candidate. ${notes}</div>`;
      return;
    }
    let html = renderCandidate(j.best, j.provider_label);
    html += `<div class="meta">provider: ${esc(j.provider_label)} · strategy:
      ${esc(j.strategy)} · verification: ${esc(j.verification_mode)}</div>`;
    if(j.alternates && j.alternates.length){
      html += `<div class="alt"><div class="meta">Alternates</div>`;
      for(const a of j.alternates){
        html += `<div style="margin-top:8px">${esc(a.email)} — ${statusChip(a)}
          <span class="meta">${a.score|0}/100</span></div>`;
      }
      html += `</div>`;
    }
    out.innerHTML = html;
    out.querySelectorAll("[data-fb]").forEach(b => b.addEventListener("click", () =>
      sendFeedback(b.dataset.email, j.domain, b.dataset.fb === "good", b)));
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
  finally{ btn.disabled = false; }
}

async function sendFeedback(email, domain, deliverable, btn){
  btn.disabled = true;
  try{
    await postJSON("/api/feedback", {email, domain, deliverable});
    btn.textContent = deliverable ? "Marked deliverable" : "Marked bounced";
  }catch(e){ btn.textContent = "failed"; }
}

function sortableTable(columns, rows){
  let html = '<div class="tblwrap"><table><thead><tr>';
  html += columns.map((c,i)=>`<th data-col="${i}">${esc(c)}</th>`).join("");
  html += "</tr></thead><tbody>";
  for(const row of rows){
    html += "<tr>" + columns.map(c=>`<td>${esc(row[c])}</td>`).join("") + "</tr>";
  }
  html += "</tbody></table></div>";
  return html;
}

function wireSort(container){
  const table = container.querySelector("table");
  if(!table) return;
  table.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    const idx = +th.dataset.col;
    const tb = table.tBodies[0];
    const rows = Array.from(tb.rows);
    const asc = th.dataset.asc !== "1"; th.dataset.asc = asc ? "1":"0";
    rows.sort((a,b)=>{
      const x=a.cells[idx].textContent, y=b.cells[idx].textContent;
      const nx=parseFloat(x), ny=parseFloat(y);
      const cmp = (!isNaN(nx)&&!isNaN(ny)) ? nx-ny : x.localeCompare(y);
      return asc ? cmp : -cmp;
    });
    rows.forEach(r=>tb.appendChild(r));
  }));
}

function download(name, text, type){
  const blob = new Blob([text], {type: type||"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
}

let lastEnriched = null;
async function doBatch(){
  const f = $("batchfile").files[0];
  const out = $("batchout");
  if(!f){ out.innerHTML = '<div class="err">choose a CSV first</div>'; return; }
  const btn = $("batchbtn"); btn.disabled = true;
  out.innerHTML = '<div class="meta">enriching…</div>';
  const fd = new FormData();
  fd.append("file", f);
  fd.append("verify", $("bverify").checked ? "1":"0");
  fd.append("use_providers", $("bproviders").checked ? "1":"0");
  try{
    const r = await fetch("/api/batch", {method:"POST", body:fd});
    const j = await r.json();
    if(!r.ok){ out.innerHTML = `<div class="err">${esc(j.detail||j.error)}</div>`; return; }
    lastEnriched = j.csv;
    out.innerHTML = sortableTable(j.columns, j.rows);
    wireSort(out);
    $("batchdl").disabled = false;
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
  finally{ btn.disabled = false; }
}

let lastFixlist = null;
async function doRescore(){
  const f = $("resfile").files[0];
  const out = $("resout");
  if(!f){ out.innerHTML = '<div class="err">choose a file first</div>'; return; }
  const btn = $("resbtn"); btn.disabled = true;
  out.innerHTML = '<div class="meta">re-scoring…</div>';
  const fd = new FormData();
  fd.append("file", f);
  fd.append("apply_kb", $("applykb").checked ? "1":"0");
  try{
    const r = await fetch("/api/rescore", {method:"POST", body:fd});
    const j = await r.json();
    if(!r.ok){ out.innerHTML = `<div class="err">${esc(j.detail||j.error)}</div>`; return; }
    lastFixlist = j.csv;
    out.innerHTML = `<div class="meta">${j.count} fix items</div>` +
      sortableTable(j.columns, j.rows);
    wireSort(out);
    $("resdl").disabled = false;
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
  finally{ btn.disabled = false; }
}

async function doOptout(){
  const out = $("optout");
  const body = {email: $("oemail").value, name: $("oname").value,
    domain: $("odomain").value};
  try{
    const r = await fetch("/api/optout", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    if(r.status === 204){
      out.innerHTML = '<div class="chip deliverable">recorded</div>' +
        '<div class="meta">You have been added to the suppression list.</div>';
      $("oemail").value = $("oname").value = $("odomain").value = "";
    }else{
      const j = await r.json().catch(()=>({}));
      out.innerHTML = `<div class="err">${esc(j.error||"failed")}</div>`;
    }
  }catch(e){ out.innerHTML = `<div class="err">${esc(e)}</div>`; }
}

$("findbtn").addEventListener("click", doFind);
$("name").addEventListener("keydown", e => { if(e.key==="Enter") doFind(); });
$("target").addEventListener("keydown", e => { if(e.key==="Enter") doFind(); });
$("batchbtn").addEventListener("click", doBatch);
$("batchdl").addEventListener("click", () =>
  lastEnriched && download("bouncezero_enriched.csv", lastEnriched));
$("resbtn").addEventListener("click", doRescore);
$("resdl").addEventListener("click", () =>
  lastFixlist && download("bouncezero_fixlist.csv", lastFixlist));
$("optbtn").addEventListener("click", doOptout);
</script>
</body>
</html>
"""
