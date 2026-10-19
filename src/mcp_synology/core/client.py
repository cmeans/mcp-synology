"""DSM API client (async httpx).

Thin wrapper that knows DSM request/response conventions but nothing
about specific APIs (File Station, Download Station, etc.).
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import secrets
import shutil
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, BinaryIO, Self

if TYPE_CHECKING:
    from pathlib import Path

import httpx

from mcp_synology.core.errors import (
    DsmHttpError,
    DsmReplyError,
    DsmTimeoutError,
    DsmUnavailableError,
    RequestTooLongError,
    SynologyError,
    error_detail,
    error_from_code,
)
from mcp_synology.core.redaction import redact_urls
from mcp_synology.core.state import ApiInfoEntry

logger = logging.getLogger(__name__)

# Session error codes that trigger transparent re-auth.
_SESSION_ERROR_CODES = frozenset({106, 107, 119})

# Request params masked as *** in debug logs.
# passphrase: a Photos share link's secret (its URL is /mo/sharing/<passphrase>).
_SENSITIVE_PARAMS = frozenset({"passwd", "password", "_sid", "device_id", "otp_code", "passphrase"})


def _log_safe(
    params: dict[str, Any], sensitive: frozenset[str] = _SENSITIVE_PARAMS
) -> dict[str, Any]:
    """Params as they may be logged: sensitive names masked, URL secrets redacted (#143)."""
    return {
        k: "***" if k in sensitive else redact_urls(v) if isinstance(v, str) else v
        for k, v in params.items()
    }


# Type alias for transfer progress callbacks.
# Called with (bytes_transferred, total_bytes_or_None).
ProgressCallback = Callable[[int, int | None], Coroutine[Any, Any, None]]


_MAY_HAVE_APPLIED = "DSM may still have applied the request; check before retrying."

_NOT_A_REPLY = (
    "A reverse proxy or captive portal in front of the NAS may be answering instead of "
    "DSM (a login page, say). Try again shortly; if it keeps happening, check what's in "
    "front of the NAS, and run 'mcp-synology check'."
)


def _read_json(resp: httpx.Response, label: str, *, may_have_applied: bool = False) -> Any:
    """The DSM reply envelope, or ``DsmReplyError`` if the answer isn't a JSON object (#282).

    ``resp.json()`` raised a bare ``JSONDecodeError`` for an HTML page, which reached the
    model as "Expecting value: line 1 column 1 (char 0)". The error names ``label`` and
    the Content-Type, never the body.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    ctype = resp.headers.get("content-type", "").split(";")[0].strip() or "none"
    raise DsmReplyError(
        f"{label}: the answer wasn't a DSM reply (Content-Type {ctype}).",
        retryable=not may_have_applied,
        suggestion=f"{_MAY_HAVE_APPLIED} {_NOT_A_REPLY}" if may_have_applied else _NOT_A_REPLY,
    )


def _error_envelope(body: bytes) -> dict[str, Any] | None:
    """The ``error`` object of a DSM error envelope in ``body``, or None if it has none."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("error"), dict):
        return None
    return parsed["error"]  # type: ignore[no-any-return]


async def _guarded_post(
    label: str, send: Callable[[], Awaitable[httpx.Response]]
) -> httpx.Response:
    """Send a non-idempotent POST, mapping transport and HTTP failures to typed errors.

    Shared by URI create (form POST) and torrent/NZB create (multipart, #154).
    DSM may already have created the task, so every failure is non-retryable,
    and the message is never empty (httpx's ``ConnectTimeout('')`` is).
    """
    try:
        resp = await send()
    except httpx.TimeoutException as e:
        raise DsmTimeoutError(
            f"{label}: request to DSM timed out ({type(e).__name__}).",
            suggestion=_MAY_HAVE_APPLIED,
        ) from e
    except httpx.TransportError as e:
        detail = f": {e}" if str(e) else ""
        raise DsmUnavailableError(
            f"{label}: couldn't reach DSM ({type(e).__name__}{detail}).",
            suggestion=_MAY_HAVE_APPLIED,
        ) from e
    if resp.status_code == 414:
        raise RequestTooLongError(
            f"{label}: DSM rejected the request with HTTP 414 (Request-URI Too Large).",
            suggestion="Reduce the size of the request (e.g. fewer URIs per call).",
        )
    if resp.status_code >= 400:
        raise DsmUnavailableError(
            f"{label}: DSM answered HTTP {resp.status_code}.",
            suggestion=_MAY_HAVE_APPLIED,
        )
    return resp


_DSM_CALL_ATTR = "mcp_synology_dsm_call"


def dsm_call_of(exc: BaseException) -> str | None:
    """The ``API/method`` a network error happened on, if ``DsmClient`` recorded it (#237)."""
    call = getattr(exc, _DSM_CALL_ATTR, None)
    return call if isinstance(call, str) else None


def _check_status(resp: httpx.Response, label: str) -> None:
    """Raise ``DsmHttpError`` for a non-2xx answer, in place of ``raise_for_status()``.

    httpx's ``HTTPStatusError`` names the full request URL, which carries ``passwd``
    or ``_sid``, so it reached the model and the server log (#232). Building the
    error from the status means no exception ever holds the URL.
    """
    if resp.is_success:
        return
    status = resp.status_code
    if status >= 500:
        suggestion = (
            "DSM, or a proxy in front of it, had a server error. Try again shortly; "
            "if it keeps happening, run 'mcp-synology check'."
        )
    else:
        suggestion = "Run 'mcp-synology check' to verify the host, port and https settings."
    raise DsmHttpError(f"{label}: DSM answered HTTP {status}.", status, suggestion=suggestion)


def _publish_download(tmp_file: Path, dest_file: Path, *, overwrite: bool) -> None:
    """Move a finished download from ``tmp_file`` onto ``dest_file``.

    With ``overwrite`` the destination is replaced, keeping its permission bits.
    Without it, a ``dest_file`` that appeared while the download streamed is never
    replaced: a hard link fails if the name exists (POSIX and NTFS alike), and where
    links aren't supported (FAT, some network mounts) a final existence check
    stands in. Either way an existing file raises ``FileExistsError``.
    """
    if overwrite:
        with contextlib.suppress(OSError):
            shutil.copymode(dest_file, tmp_file)  # best effort; dest may not exist
        os.replace(tmp_file, dest_file)
        return
    try:
        os.link(tmp_file, dest_file)
    except FileExistsError:
        raise
    except OSError:
        if dest_file.exists():
            raise FileExistsError(errno.EEXIST, "File exists", str(dest_file)) from None
        os.replace(tmp_file, dest_file)
        return
    tmp_file.unlink()


class DsmClient:
    """Async DSM API client.

    Usage as an async context manager:
        async with DsmClient(base_url="http://nas:5000", ...) as client:
            await client.query_api_info()
            data = await client.request("SYNO.FileStation.List", "list_share", ...)
    """

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._api_cache: dict[str, ApiInfoEntry] = {}
        self._sid: str | None = None
        self._re_auth_callback: ReAuthCallback | None = None
        logger.debug(
            "DsmClient created: base_url=%s, verify_ssl=%s, timeout=%d",
            self._base_url,
            verify_ssl,
            timeout,
        )

    @property
    def api_cache(self) -> dict[str, ApiInfoEntry]:
        """The cached API info map."""
        return self._api_cache

    @property
    def sid(self) -> str | None:
        """Current session ID."""
        return self._sid

    @sid.setter
    def sid(self, value: str | None) -> None:
        self._sid = value

    def _session_headers(self) -> dict[str, str]:
        """The session as DSM's ``id`` cookie, never as ``_sid`` (#176).

        JSON-format APIs parse every parameter value as JSON first (#121), and that
        includes ``_sid``: a session ID that begins like a number (``-4Ms…``,
        ``4e70…``, ``8-aZ…``; on vdsm about 1 in 75 on DSM 7.2.2 and 1 in 170 on
        7.1) then matches no session, and every call with it gets 119, re-auth
        included, until a new login happens to draw another shape. Quoting ``_sid``
        fixes those APIs but breaks Download Station's v1 CGIs (105) and ``logout``
        (answers success, session stays). The cookie works for every API on DSM 7.1
        and 7.2.2, and keeps the session out of URLs and logs. Sent as a header, so
        the httpx cookie jar never holds or resends it. An explicit ``Cookie`` header
        replaces the jar's, so any cookie the jar holds (a reverse proxy's own login,
        say) is sent along with it; an ``id`` in the jar never goes beside the session.
        """
        if not self._sid:
            return {}
        jar = list(self._http.cookies.jar) if self._http else []
        cookies = [f"{c.name}={c.value}" for c in jar if c.name != "id"]
        return {"Cookie": "; ".join([f"id={self._sid}", *cookies])}

    def set_re_auth_callback(self, callback: ReAuthCallback) -> None:
        """Set callback for transparent re-authentication on session errors."""
        self._re_auth_callback = callback

    async def __aenter__(self) -> Self:
        logger.debug("Opening HTTP client connection to %s", self._base_url)
        # Silence httpx's built-in request logger — it logs full URLs at INFO level,
        # which leaks sensitive query params (passwd, _sid, device_id, otp_code).
        # We do our own request logging with proper masking in request().
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._http = httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._http:
            logger.debug("Closing HTTP client connection")
            await self._http.aclose()
            self._http = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            msg = "DsmClient must be used as an async context manager."
            raise RuntimeError(msg)
        return self._http

    async def query_api_info(self) -> dict[str, ApiInfoEntry]:
        """Call SYNO.API.Info with query=ALL and populate the API cache.

        Returns the cached mapping of API name -> ApiInfoEntry.
        """
        http = self._get_http()
        url = f"{self._base_url}/webapi/query.cgi"
        params = {
            "api": "SYNO.API.Info",
            "version": "1",
            "method": "query",
            "query": "ALL",
        }

        logger.debug("Querying API info: GET %s", url)
        resp = await http.get(url, params=params)
        _check_status(resp, "SYNO.API.Info/query")
        body = _read_json(resp, "SYNO.API.Info/query")

        if not body.get("success"):
            code = body.get("error", {}).get("code", 0)
            logger.debug("API info query failed with code %d", code)
            raise error_from_code(code, "SYNO.API.Info")

        data: dict[str, Any] = body["data"]
        self._api_cache = {}
        for api_name, info in data.items():
            self._api_cache[api_name] = ApiInfoEntry(
                path=info["path"],
                min_version=info.get("minVersion", 1),
                max_version=info.get("maxVersion", 1),
                request_format=info.get("requestFormat"),
            )

        logger.debug("API info cache populated: %d APIs available", len(self._api_cache))
        # Log only APIs we actually use — not all 800+
        _relevant_apis = frozenset(
            {
                "SYNO.API.Auth",
                "SYNO.DSM.Info",
                "SYNO.FileStation.Info",
                "SYNO.FileStation.List",
                "SYNO.FileStation.Search",
                "SYNO.FileStation.DirSize",
                "SYNO.FileStation.CreateFolder",
                "SYNO.FileStation.Rename",
                "SYNO.FileStation.CopyMove",
                "SYNO.FileStation.Delete",
                "SYNO.Core.System",
                "SYNO.Core.System.Utilization",
                "SYNO.FileStation.Upload",
                "SYNO.FileStation.Download",
            }
        )
        for name in sorted(_relevant_apis):
            entry = self._api_cache.get(name)
            if entry:
                fmt_tag = f", format={entry.request_format}" if entry.request_format else ""
                logger.debug(
                    "  %s: path=%s, v%d-v%d%s",
                    name,
                    entry.path,
                    entry.min_version,
                    entry.max_version,
                    fmt_tag,
                )
            else:
                logger.debug("  %s: NOT AVAILABLE", name)

        return self._api_cache

    def negotiate_version(
        self,
        api_name: str,
        min_version: int = 1,
        max_version: int | None = None,
    ) -> int:
        """Pick the highest compatible API version.

        Compares the requested version range with the NAS's supported range.
        Returns the highest version supported by both sides.

        Raises ApiNotFoundError if the API is not in the cache.
        """
        if api_name not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api_name}' not found on this NAS.",
                code=102,
                suggestion="Check that the required Synology package is installed.",
            )

        info = self._api_cache[api_name]
        nas_max = info.max_version
        nas_min = info.min_version

        # Our desired range
        our_max = max_version if max_version is not None else nas_max

        # Negotiated version: highest that both sides support
        negotiated = min(our_max, nas_max)

        if negotiated < max(min_version, nas_min):
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api_name}': no compatible version. "
                f"NAS supports v{nas_min}-v{nas_max}, we need v{min_version}+.",
                code=104,
                suggestion="Update DSM or use an older version of mcp-synology.",
            )

        logger.debug(
            "Negotiated %s: v%d (NAS v%d-v%d, requested v%d-v%s)",
            api_name,
            negotiated,
            nas_min,
            nas_max,
            min_version,
            max_version or "max",
        )
        return negotiated

    async def request(
        self,
        api: str,
        method: str,
        version: int | None = None,
        params: dict[str, Any] | None = None,
        *,
        post_form: bool = False,
        read_timeout: float | None = None,
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Make a DSM API request.

        Builds the URL from the API cache, injects session ID, parses the
        response envelope, and handles error codes.

        On session errors (106/107/119), triggers re-auth and retries once.

        The session goes as the ``id`` cookie (``_session_headers()``, #176).
        ``post_form`` sends the parameters as a form-encoded POST body instead of
        the query string. The
        transport is chosen per call (each module spec records its choice):
        ``SYNO.API.Auth`` ``login`` uses it so ``passwd``, ``otp_code`` and
        ``device_id`` never reach a URL, where DSM's and any reverse proxy's access
        logs record it (#235), and the Photos module sends its JSON calls this way
        (``docs/specs/photos-module-spec.md``, Transport). ``api``/``method``/
        ``version`` go in the body too: a POST with them only in the query string and
        an empty body fails with 101. File Station and Download Station stay on GET.

        ``read_timeout`` (seconds) replaces the client's read timeout for this request
        only; connect, write and pool keep the client's default, so an unreachable NAS
        still fails fast. File Station task stops use it (#245): DSM 7.1 sometimes
        answers a Delete ``stop`` only after ~60 s, although the stop takes effect at once.
        """
        http = self._get_http()

        # Resolve API path and version
        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        info = self._api_cache[api]
        resolved_version = version if version is not None else info.max_version
        url = f"{self._base_url}/webapi/{info.path}"

        # Build request params
        req_params: dict[str, Any] = {
            "api": api,
            "version": str(resolved_version),
            "method": method,
        }
        if params:
            req_params.update(params)

        # Log request (mask password)
        log_params = _log_safe(req_params)
        retry_tag = " (retry)" if _is_retry else ""

        # GET with query params unless the caller chose post_form (see the docstring).
        # The requestFormat field in the API info is metadata about how DSM parses
        # values (as JSON, #121), not a mandate to POST.
        logger.debug(
            "DSM %s%s: %s/%s v%d — %s",
            "POST" if post_form else "GET",
            retry_tag,
            api,
            method,
            resolved_version,
            log_params,
        )

        req_timeout = (
            httpx.USE_CLIENT_DEFAULT
            if read_timeout is None
            else httpx.Timeout(self._timeout, read=read_timeout)
        )
        try:
            headers = self._session_headers()
            if post_form:
                resp = await http.post(url, data=req_params, headers=headers, timeout=req_timeout)
            else:
                resp = await http.get(url, params=req_params, headers=headers, timeout=req_timeout)
        except httpx.TransportError as e:
            # Name the call without changing the exception's type: get_client() and
            # `check` rely on httpx.TransportError to report an unreachable NAS (#237).
            setattr(e, _DSM_CALL_ATTR, f"{api}/{method}")
            raise
        if resp.status_code == 414:
            raise RequestTooLongError(
                f"{api}/{method}: DSM rejected the request with HTTP 414 (Request-URI Too Large).",
                suggestion="Reduce the size of the request (e.g. fewer URIs per call).",
            )
        _check_status(resp, f"{api}/{method}")
        body = _read_json(resp, f"{api}/{method}")

        if body.get("success"):
            data: dict[str, Any] = body.get("data", {})
            if isinstance(data, dict):
                logger.debug(
                    "DSM response: %s/%s — success (keys: %s)", api, method, list(data.keys())
                )
            else:
                logger.debug(
                    "DSM response: %s/%s — success (list, %d items)", api, method, len(data)
                )
            return data

        error = body.get("error", {})
        code = error.get("code", 0)
        logger.debug(
            "DSM response: %s/%s — error code %d%s",
            api,
            method,
            code,
            error_detail(error.get("errors")),
        )

        # Transparent re-auth on session errors (one retry)
        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/%s, attempting re-auth.", code, api, method)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.request(
                api,
                method,
                version,
                params,
                post_form=post_form,
                read_timeout=read_timeout,
                _is_retry=True,
            )

        raise error_from_code(code, api, error.get("errors"))

    async def request_form_post(
        self,
        api: str,
        method: str,
        version: int,
        params: dict[str, str] | None = None,
        *,
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Make a DSM API request as a form-encoded POST.

        A scoped exception to the GET-only rule (see ``request()``), used ONLY
        for Download Station URI ``Task.create``: a magnet with many trackers,
        or a list of URIs, pushes a GET URL past DSM's ~8 KB limit (HTTP 414),
        and GET would also put source-URL credentials in access logs (#123).
        Everything else stays on GET — POST caused silent failures on DSM 7.1
        FileStation APIs.

        Same envelope handling, typed errors, and one-shot re-auth on session
        errors as ``request()``. The session goes as the ``id`` cookie (#176);
        everything else goes in the body. Non-2xx HTTP statuses
        and timeouts raise typed ``SynologyError``s rather than httpx errors.
        """
        http = self._get_http()

        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        url = f"{self._base_url}/webapi/{self._api_cache[api].path}"
        form_data: dict[str, str] = {"api": api, "version": str(version), "method": method}
        if params:
            form_data.update(params)

        log_data = _log_safe(form_data)
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug("DSM POST%s: %s/%s v%d — %s", retry_tag, api, method, version, log_data)

        resp = await _guarded_post(
            f"{api}/{method}",
            lambda: http.post(url, data=form_data, headers=self._session_headers()),
        )
        body = _read_json(resp, f"{api}/{method}", may_have_applied=True)

        if body.get("success"):
            data: dict[str, Any] = body.get("data", {})
            logger.debug("DSM response: %s/%s — success", api, method)
            return data

        code = body.get("error", {}).get("code", 0)
        logger.debug("DSM response: %s/%s — error code %d", api, method, code)

        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/%s, attempting re-auth.", code, api, method)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.request_form_post(api, method, version, params, _is_retry=True)

        raise error_from_code(code, api)

    async def upload(
        self,
        dest_folder: str,
        file_path: Path,
        filename: str,
        *,
        overwrite: bool = False,
        create_parents: bool = True,
        version: int | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Upload a file to the NAS via SYNO.FileStation.Upload (POST multipart).

        Returns the parsed data dict from the JSON response envelope.
        """
        api = "SYNO.FileStation.Upload"
        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )
        # Pin to v2 — v3 uses JSON request format that is incompatible with
        # our multipart POST. Same issue as CopyMove/Delete.
        resolved_version = (
            version if version is not None else min(self._api_cache[api].max_version, 2)
        )
        fields: dict[str, str] = {
            "path": dest_folder,
            "create_parents": str(create_parents).lower(),
        }
        # Only send overwrite=true. DSM reads overwrite=false as "skip": it answers success
        # with data.blSkip=true and leaves the existing file alone. With the parameter
        # omitted it fails with 414 (file exists) instead (#195, DSM 7.1 and 7.2.2).
        if overwrite:
            fields["overwrite"] = "true"
        return await self.upload_multipart(
            api,
            "upload",
            resolved_version,
            fields,
            file_path=file_path,
            filename=filename,
            timeout=timeout,
        )

    async def upload_multipart(
        self,
        api: str,
        method: str,
        version: int,
        fields: dict[str, str],
        *,
        file_path: Path,
        filename: str,
        file_field: str = "file",
        content_type: str = "application/octet-stream",
        timeout: float = 300.0,
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Upload a file as a multipart POST: File Station's Upload, Photos' Upload.Item.

        ``fields`` are sent as form fields, already encoded as the API expects them
        (JSON-format APIs need JSON-encoded strings); ``api``/``method``/``version`` go
        with them. The file part comes last, named ``file_field``.

        The file is opened (or re-opened on retry) within this method so the
        stream is always fresh.

        Returns the parsed data dict from the JSON response envelope.
        """
        http = self._get_http()

        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        url = f"{self._base_url}/webapi/{self._api_cache[api].path}"
        form_data: dict[str, str] = {"api": api, "version": str(version), "method": method}
        form_data.update(fields)
        # The session goes as the id cookie (#176); the Upload API doesn't read
        # _sid from multipart form data.

        log_data = _log_safe(form_data)
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug(
            "DSM POST%s: %s/%s v%d — %s, file=%s",
            retry_tag,
            api,
            method,
            version,
            log_data,
            filename,
        )

        def _open_file() -> BinaryIO:
            return open(file_path, "rb")  # noqa: SIM115

        fh = _open_file()
        try:
            resp = await http.post(
                url,
                headers=self._session_headers(),
                data=form_data,
                files={file_field: (filename, fh, content_type)},
                timeout=httpx.Timeout(timeout),
            )
        except httpx.TransportError as e:
            setattr(e, _DSM_CALL_ATTR, f"{api}/{method}")
            raise
        finally:
            fh.close()

        _check_status(resp, f"{api}/{method}")
        body = _read_json(resp, f"{api}/{method}")

        if body.get("success"):
            logger.debug("DSM response: %s/%s — success", api, method)
            data: dict[str, Any] = body.get("data", {})
            return data

        error = body.get("error", {})
        code = error.get("code", 0)
        logger.debug(
            "DSM response: %s/%s — error code %d%s",
            api,
            method,
            code,
            error_detail(error.get("errors")),
        )

        # Re-auth on session errors (file must be re-opened on retry)
        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/%s, attempting re-auth.", code, api, method)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.upload_multipart(
                api,
                method,
                version,
                fields,
                file_path=file_path,
                filename=filename,
                file_field=file_field,
                content_type=content_type,
                timeout=timeout,
                _is_retry=True,
            )

        raise error_from_code(code, api, error.get("errors"))

    async def create_download_task_with_file(
        self,
        file_path: Path,
        filename: str,
        *,
        destination: str | None = None,
        timeout: float = 300.0,
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Create a Download Station task by uploading a .torrent or .nzb file.

        Multipart POST against ``SYNO.DownloadStation2.Task.create`` v2. The
        v1 ``SYNO.DownloadStation.Task`` multipart path returns DSM 101 on DSM
        7.2.2 in every layout and version tried (#123), so there is no v1
        fallback. DS2 is a JSON-format API: string fields are JSON-encoded,
        the file part is named ``torrent`` (referenced by ``file=["torrent"]``)
        and goes last. Same re-auth-on-session-error retry and session
        cookie as ``upload()``. Returns the ``data`` dict
        (``{"task_id": [...], "list_id": [...]}``).
        """
        api = "SYNO.DownloadStation2.Task"
        http = self._get_http()

        # Raises ApiNotFoundError (102 absent / 104 no v2) — no fallback exists.
        resolved_version = self.negotiate_version(api, min_version=2, max_version=2)
        url = f"{self._base_url}/webapi/{self._api_cache[api].path}"

        form_data: dict[str, str] = {
            "api": api,
            "version": str(resolved_version),
            "method": "create",
            "type": json.dumps("file"),
            "file": json.dumps(["torrent"]),
            "create_list": "false",
            "size": str(file_path.stat().st_size),
        }
        if destination is not None:
            form_data["destination"] = json.dumps(destination)

        # The session goes as the id cookie (#176), as for the File Station Upload API.

        log_data = _log_safe(form_data)
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug(
            "DSM POST%s: %s/create v%d — %s, file=%s",
            retry_tag,
            api,
            resolved_version,
            log_data,
            filename,
        )

        def _open_file() -> BinaryIO:
            return open(file_path, "rb")  # noqa: SIM115

        fh = _open_file()
        try:
            resp = await _guarded_post(
                f"{api}/create",
                lambda: http.post(
                    url,
                    headers=self._session_headers(),
                    data=form_data,
                    files={"torrent": (filename, fh, "application/x-bittorrent")},
                    timeout=httpx.Timeout(timeout),
                ),
            )
        finally:
            fh.close()

        body = _read_json(resp, f"{api}/create", may_have_applied=True)

        if body.get("success"):
            logger.debug("DSM response: %s/create — success", api)
            data: dict[str, Any] = body.get("data", {})
            return data

        code = body.get("error", {}).get("code", 0)
        logger.debug("DSM response: %s/create — error code %d", api, code)

        # Re-auth on session errors (file must be re-opened on retry)
        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/create, attempting re-auth.", code, api)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.create_download_task_with_file(
                file_path,
                filename,
                destination=destination,
                timeout=timeout,
                _is_retry=True,
            )

        raise error_from_code(code, api)

    async def download(
        self,
        path: str,
        dest_file: Path,
        *,
        version: int | None = None,
        timeout: float = 300.0,
        chunk_size: int = 65536,
        progress_callback: ProgressCallback | None = None,
        overwrite: bool = True,
    ) -> int:
        """Download a file from the NAS via SYNO.FileStation.Download (GET, binary).

        See ``download_binary()``. Returns total bytes written.
        """
        api = "SYNO.FileStation.Download"
        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )
        resolved_version = version if version is not None else self._api_cache[api].max_version
        return await self.download_binary(
            api,
            "download",
            resolved_version,
            {"path": path, "mode": "download"},
            dest_file,
            timeout=timeout,
            chunk_size=chunk_size,
            progress_callback=progress_callback,
            overwrite=overwrite,
        )

    async def download_binary(
        self,
        api: str,
        method: str,
        version: int,
        params: dict[str, str],
        dest_file: Path,
        *,
        timeout: float = 300.0,
        chunk_size: int = 65536,
        progress_callback: ProgressCallback | None = None,
        overwrite: bool = True,
        _is_retry: bool = False,
    ) -> int:
        """Stream a binary DSM reply (GET) to ``dest_file``: File Station's Download,
        Photos' Download and Thumbnail.

        Streams the response to a temp file (``.mcp-synology-*.part``) next to ``dest_file`` and
        renames it onto ``dest_file`` only after the whole body is written, so
        a failed or interrupted download never truncates or removes an
        existing file at ``dest_file``. The temp file is removed on any
        failure. With ``overwrite=False`` the finished file is published so that it
        never replaces a ``dest_file`` that appeared during the download: that
        raises ``FileExistsError``. Returns total bytes written.

        Checks Content-Length against local disk free space before writing.
        If the NAS returns a JSON error envelope (Content-Type: application/json)
        instead of binary data, parses and raises the appropriate exception. A text/html
        200, or JSON that isn't an envelope, raises ``DsmReplyError`` before anything is
        written: DSM never sends a file as text/html (#292). An HTTP
        error status without one raises ``FileStationError`` for File Station (as
        before) and ``DsmHttpError`` for any other API (Photos answers a thumbnail
        that isn't generated yet with a 404 HTML page).
        """
        http = self._get_http()

        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        url = f"{self._base_url}/webapi/{self._api_cache[api].path}"
        req_params: dict[str, str] = {"api": api, "version": str(version), "method": method}
        req_params.update(params)

        log_params = _log_safe(req_params)
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug(
            "DSM GET%s: %s/%s v%d — %s",
            retry_tag,
            api,
            method,
            version,
            log_params,
        )

        async with http.stream(
            "GET",
            url,
            params=req_params,
            headers=self._session_headers(),
            timeout=httpx.Timeout(timeout),
        ) as resp:
            # Check for HTTP-level errors (502, 404, etc.) before reading body.
            # Some DSM errors come back as HTTP errors rather than JSON envelopes.
            if resp.status_code >= 400:
                # Try to read the body for a DSM error envelope
                error = _error_envelope(await resp.aread())
                if error is not None:
                    raise error_from_code(error.get("code", 0), api, error.get("errors"))
                if "FileStation" not in api:
                    _check_status(resp, f"{api}/{method}")
                from mcp_synology.core.errors import FileStationError

                raise FileStationError(
                    f"HTTP {resp.status_code} from download API",
                    code=resp.status_code,
                    suggestion="Check that the file path exists on the NAS.",
                )

            content_type = resp.headers.get("content-type", "")
            media_type = content_type.split(";")[0].strip().lower()

            # JSON means DSM's error envelope. An HTML page on a 200 is never the file
            # (DSM sends application/octet-stream for every File Station download, even
            # a real .html file, and application/force-download or image/* for Photos,
            # on 7.1 and 7.2.2): it's a reverse proxy's or captive portal's page, so
            # raise before anything is written (#292).
            if media_type in ("application/json", "text/html", "application/xhtml+xml"):
                body_bytes = await resp.aread()
                error = _error_envelope(body_bytes)
                if error is None:  # a page, or JSON that isn't DSM's error envelope
                    raise DsmReplyError(
                        f"{api}/{method}: the answer wasn't a DSM reply (Content-Type "
                        f"{media_type}).",
                        suggestion=_NOT_A_REPLY,
                    )
                code = error.get("code", 0)
                logger.debug("DSM response: %s/%s — error code %s", api, method, code)

                if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
                    logger.info("Session error %d on %s/%s, attempting re-auth.", code, api, method)
                    try:
                        await self._re_auth_callback()
                    except SynologyError:
                        raise error_from_code(code, api) from None
                    return await self.download_binary(
                        api,
                        method,
                        version,
                        params,
                        dest_file,
                        timeout=timeout,
                        chunk_size=chunk_size,
                        progress_callback=progress_callback,
                        overwrite=overwrite,
                        _is_retry=True,
                    )

                raise error_from_code(code, api, error.get("errors"))

            # Check disk space before writing
            content_length = resp.headers.get("content-length")
            total_size: int | None = int(content_length) if content_length else None
            if total_size:
                free_space = shutil.disk_usage(dest_file.parent).free
                if total_size > free_space:
                    from mcp_synology.core.formatting import format_size

                    msg = (
                        f"Insufficient local disk space: file is {format_size(total_size)} "
                        f"but only {format_size(free_space)} free."
                    )
                    raise OSError(msg)

            # Binary response — stream to a temp file, then rename onto dest.
            # The temp name doesn't embed the target name, so a long target
            # name can't push it past the filesystem's name-length limit.
            tmp_file = dest_file.with_name(f".mcp-synology-{secrets.token_hex(8)}.part")
            bytes_written = 0
            # Opened before the try: if the name somehow exists already, that isn't ours
            # to delete.
            fh = open(tmp_file, "xb")  # noqa: SIM115 - closed below, before publishing
            try:
                with fh:
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        fh.write(chunk)
                        bytes_written += len(chunk)
                        if progress_callback:
                            await progress_callback(bytes_written, total_size)
                    # On disk before the rename, so a power loss can't leave an empty file.
                    fh.flush()
                    os.fsync(fh.fileno())
                _publish_download(tmp_file, dest_file, overwrite=overwrite)
            except BaseException:
                try:
                    tmp_file.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_err:
                    logger.warning(
                        "Failed to clean up partial download %s: %s", tmp_file, cleanup_err
                    )
                raise

            logger.debug(
                "DSM response: %s/%s — success (%d bytes written)", api, method, bytes_written
            )
            return bytes_written

    async def fetch_dsm_info(self) -> dict[str, Any]:
        """Query SYNO.DSM.Info getinfo and return the data dict.

        Returns an empty dict if the API is unavailable.
        """
        if "SYNO.DSM.Info" not in self._api_cache:
            logger.debug("SYNO.DSM.Info not in API cache, skipping hostname fetch")
            return {}
        try:
            data = await self.request("SYNO.DSM.Info", "getinfo")
            logger.debug("DSM info: %s", {k: v for k, v in data.items() if k != "serial"})
            return data
        except SynologyError as e:
            logger.debug("Failed to fetch DSM info: %s", e)
            return {}

    @staticmethod
    def escape_path_param(paths: list[str]) -> str:
        """Escape and comma-join paths for DSM multi-path parameters.

        Backslashes are escaped to \\\\, commas are escaped to \\,.
        """
        escaped = []
        for p in paths:
            p = p.replace("\\", "\\\\")
            p = p.replace(",", "\\,")
            escaped.append(p)
        return ",".join(escaped)


# Type alias for re-auth callback
ReAuthCallback = Callable[[], Coroutine[Any, Any, None]]
