"""DSM API client (async httpx).

Thin wrapper that knows DSM request/response conventions but nothing
about specific APIs (File Station, Download Station, etc.).
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, BinaryIO, Self

if TYPE_CHECKING:
    from pathlib import Path

import httpx

from mcp_synology.core.errors import (
    SynologyError,
    error_from_code,
)
from mcp_synology.core.state import ApiInfoEntry

logger = logging.getLogger(__name__)

# Session error codes that trigger transparent re-auth.
_SESSION_ERROR_CODES = frozenset({106, 107, 119})

# Type alias for transfer progress callbacks.
# Called with (bytes_transferred, total_bytes_or_None).
ProgressCallback = Callable[[int, int | None], Coroutine[Any, Any, None]]


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
        resp.raise_for_status()
        body = resp.json()

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
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Make a DSM API request.

        Builds the URL from the API cache, injects session ID, parses the
        response envelope, and handles error codes.

        On session errors (106/107/119), triggers re-auth and retries once.
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

        # Inject session ID
        if self._sid:
            req_params["_sid"] = self._sid

        # Log request (mask password)
        _sensitive = frozenset({"passwd", "_sid", "device_id", "otp_code"})
        log_params = {k: ("***" if k in _sensitive else v) for k, v in req_params.items()}
        retry_tag = " (retry)" if _is_retry else ""

        # Always use GET with query params. DSM v2 APIs work with GET, and we
        # pin all APIs to v2 to avoid v3 JSON request format issues. The
        # requestFormat field in the API info is metadata about what the API
        # supports, not a mandate to POST — on DSM 7.1, every FileStation API
        # reports requestFormat=JSON even at v2.
        logger.debug(
            "DSM GET%s: %s/%s v%d — %s",
            retry_tag,
            api,
            method,
            resolved_version,
            log_params,
        )

        resp = await http.get(url, params=req_params)
        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            data: dict[str, Any] = body.get("data", {})
            logger.debug("DSM response: %s/%s — success (keys: %s)", api, method, list(data.keys()))
            return data

        code = body.get("error", {}).get("code", 0)
        logger.debug("DSM response: %s/%s — error code %d", api, method, code)

        # Transparent re-auth on session errors (one retry)
        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/%s, attempting re-auth.", code, api, method)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.request(api, method, version, params, _is_retry=True)

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
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Upload a file to the NAS via SYNO.FileStation.Upload (POST multipart).

        This is the ONE case where POST is mandatory — the Upload API requires
        multipart form data. All other DSM APIs use GET.

        The file is opened (or re-opened on retry) within this method so the
        stream is always fresh.

        Returns the parsed data dict from the JSON response envelope.
        """
        api = "SYNO.FileStation.Upload"
        http = self._get_http()

        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        info = self._api_cache[api]
        # Pin to v2 — v3 uses JSON request format that is incompatible with
        # our multipart POST. Same issue as CopyMove/Delete.
        resolved_version = version if version is not None else min(info.max_version, 2)
        url = f"{self._base_url}/webapi/{info.path}"

        form_data: dict[str, str] = {
            "api": api,
            "version": str(resolved_version),
            "method": "upload",
            "path": dest_folder,
            "overwrite": str(overwrite).lower(),
            "create_parents": str(create_parents).lower(),
        }
        # SID must be a query parameter, not a form field — the Upload API
        # does not read _sid from multipart form data.
        query_params: dict[str, str] = {}
        if self._sid:
            query_params["_sid"] = self._sid

        _sensitive = frozenset({"_sid"})
        log_data = {k: ("***" if k in _sensitive else v) for k, v in form_data.items()}
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug(
            "DSM POST%s: %s/upload v%d — %s, file=%s",
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
            resp = await http.post(
                url,
                params=query_params,
                data=form_data,
                files={"file": (filename, fh, "application/octet-stream")},
                timeout=httpx.Timeout(timeout),
            )
        finally:
            fh.close()

        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            logger.debug("DSM response: %s/upload — success", api)
            data: dict[str, Any] = body.get("data", {})
            return data

        code = body.get("error", {}).get("code", 0)
        logger.debug("DSM response: %s/upload — error code %d", api, code)

        # Re-auth on session errors (file must be re-opened on retry)
        if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
            logger.info("Session error %d on %s/upload, attempting re-auth.", code, api)
            try:
                await self._re_auth_callback()
            except SynologyError:
                raise error_from_code(code, api) from None
            return await self.upload(
                dest_folder,
                file_path,
                filename,
                overwrite=overwrite,
                create_parents=create_parents,
                version=version,
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
        _is_retry: bool = False,
    ) -> int:
        """Download a file from the NAS via SYNO.FileStation.Download (GET, binary).

        Streams the response to disk. Returns total bytes written.

        Checks Content-Length against local disk free space before writing.
        If the NAS returns a JSON error envelope (Content-Type: application/json)
        instead of binary data, parses and raises the appropriate exception.
        """
        api = "SYNO.FileStation.Download"
        http = self._get_http()

        if api not in self._api_cache:
            from mcp_synology.core.errors import ApiNotFoundError

            raise ApiNotFoundError(
                f"API '{api}' not found. Call query_api_info() first.",
                code=102,
            )

        info = self._api_cache[api]
        resolved_version = version if version is not None else info.max_version
        url = f"{self._base_url}/webapi/{info.path}"

        params: dict[str, str] = {
            "api": api,
            "version": str(resolved_version),
            "method": "download",
            "path": path,
            "mode": "download",
        }
        if self._sid:
            params["_sid"] = self._sid

        _sensitive = frozenset({"_sid"})
        log_params = {k: ("***" if k in _sensitive else v) for k, v in params.items()}
        retry_tag = " (retry)" if _is_retry else ""
        logger.debug(
            "DSM GET%s: %s/download v%d — %s",
            retry_tag,
            api,
            resolved_version,
            log_params,
        )

        async with http.stream("GET", url, params=params, timeout=httpx.Timeout(timeout)) as resp:
            # Check for HTTP-level errors (502, 404, etc.) before reading body.
            # Some DSM errors come back as HTTP errors rather than JSON envelopes.
            if resp.status_code >= 400:
                # Try to read the body for a DSM error envelope
                body_bytes = await resp.aread()
                try:
                    body = json.loads(body_bytes)
                    code = body.get("error", {}).get("code", 0)
                    raise error_from_code(code, api)
                except (json.JSONDecodeError, KeyError):
                    from mcp_synology.core.errors import FileStationError

                    raise FileStationError(
                        f"HTTP {resp.status_code} from download API",
                        code=resp.status_code,
                        suggestion="Check that the file path exists on the NAS.",
                    ) from None

            content_type = resp.headers.get("content-type", "")

            # JSON response means error envelope
            if "application/json" in content_type:
                body_bytes = await resp.aread()
                body = json.loads(body_bytes)
                code = body.get("error", {}).get("code", 0)
                logger.debug("DSM response: %s/download — error code %d", api, code)

                if code in _SESSION_ERROR_CODES and not _is_retry and self._re_auth_callback:
                    logger.info("Session error %d on %s/download, attempting re-auth.", code, api)
                    try:
                        await self._re_auth_callback()
                    except SynologyError:
                        raise error_from_code(code, api) from None
                    return await self.download(
                        path,
                        dest_file,
                        version=version,
                        timeout=timeout,
                        chunk_size=chunk_size,
                        progress_callback=progress_callback,
                        _is_retry=True,
                    )

                raise error_from_code(code, api)

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

            # Binary response — stream to disk with progress
            bytes_written = 0
            with open(dest_file, "wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                    fh.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        await progress_callback(bytes_written, total_size)

            logger.debug(
                "DSM response: %s/download — success (%d bytes written)", api, bytes_written
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
