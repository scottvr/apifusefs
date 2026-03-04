from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
from typing import Any

import mfusepy as fuse

from .fuse_ops import ProviderFuse
from .providers import APIFuse, APISpecError, JSONFuse, OpenAPIProviderAdapter


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mount a read-only FUSE filesystem backed by an OpenAPI spec or static JSON."
    )
    parser.add_argument("mountpoint", help="directory where the filesystem will be mounted")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--api-spec",
        help="path or URL for the OpenAPI JSON/YAML spec, or a base API URL",
    )
    source_group.add_argument(
        "--json-input",
        help="path to a local JSON file to mount directly",
    )
    parser.add_argument(
        "--server-url",
        help="override the API server base URL used for endpoint requests",
    )
    parser.add_argument(
        "--auth-token",
        help="auth token value to send in the configured auth header",
    )
    parser.add_argument(
        "--auth-token-file",
        help="read the auth token from a local file",
    )
    parser.add_argument(
        "--auth-token-env",
        default="APIFUSE_auth_token",
        help="environment variable name to read the auth token from (default: APIFUSE_auth_token)",
    )
    parser.add_argument(
        "--auth-header",
        default="Authorization",
        help="HTTP header name for the auth token (default: Authorization)",
    )
    parser.add_argument(
        "--auth-scheme",
        default="Bearer",
        help="auth scheme/prefix used in the auth header value (default: Bearer)",
    )
    parser.add_argument(
        "--refresh-url",
        help="token refresh endpoint URL (enables refresh-on-401 when refresh token is provided)",
    )
    parser.add_argument(
        "--refresh-token",
        help="refresh token value",
    )
    parser.add_argument(
        "--refresh-token-file",
        help="read refresh token from a local file",
    )
    parser.add_argument(
        "--refresh-token-env",
        default="APIFUSE_refresh_token",
        help="environment variable name to read refresh token from (default: APIFUSE_refresh_token)",
    )
    parser.add_argument(
        "--refresh-body-token-key",
        default="refresh_token",
        help="JSON key used for refresh token in refresh request body (default: refresh_token)",
    )
    parser.add_argument(
        "--refresh-response-token-key",
        default="access_token",
        help="JSON key to read new access token from refresh response (default: access_token)",
    )
    parser.add_argument(
        "--discover-refresh-from-response",
        action="store_true",
        help="opt-in: discover refresh URL/token from JSON responses on allowlisted paths",
    )
    parser.add_argument(
        "--refresh-discovery-path",
        action="append",
        default=[],
        help="path prefix allowlist for refresh discovery (repeatable), e.g. /auth or /session/login",
    )
    parser.add_argument(
        "--refresh-discovery-url-key",
        action="append",
        default=[],
        help="JSON key candidate for refresh URL discovery (repeatable)",
    )
    parser.add_argument(
        "--refresh-discovery-token-key",
        action="append",
        default=[],
        help="JSON key candidate for refresh token discovery (repeatable)",
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=10,
        help="when collection GET fails, probe ids 0..N-1 via the item endpoint",
    )
    parser.add_argument(
        "--cache-ttl",
        type=float,
        default=2.0,
        help="seconds to cache successful JSON responses (default: 2.0)",
    )
    parser.add_argument(
        "--error-cache-ttl",
        type=float,
        default=1.0,
        help="seconds to cache failed JSON responses (default: 1.0)",
    )
    parser.add_argument(
        "--cache-max-entries",
        type=int,
        default=512,
        help="maximum number of cached JSON responses (default: 512)",
    )
    parser.add_argument(
        "--symlink-names",
        action="store_true",
        help="add collection-level symlink aliases using common name fields like name, username, slug, or title",
    )
    parser.add_argument(
        "--symlink-map",
        action="append",
        default=[],
        help="add custom collection symlink aliases, e.g. products=title or products=categories/name",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds for spec and endpoint requests",
    )
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help="ask libfuse to daemonize internally (not recommended on macOS; prefer external process management)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="force mount even if bootstrap validation fails",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--log-file",
        help="write logs to this file instead of stderr (recommended for daemon mode)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = "json" if args.json_input else "openapi"

    args.mountpoint = os.path.abspath(args.mountpoint)
    if args.api_spec:
        parsed_api_spec = urllib.parse.urlparse(args.api_spec)
        if parsed_api_spec.scheme not in {"http", "https"} or not parsed_api_spec.netloc:
            args.api_spec = os.path.abspath(args.api_spec)
    if args.json_input:
        args.json_input = os.path.abspath(args.json_input)
    if args.auth_token_file:
        args.auth_token_file = os.path.abspath(args.auth_token_file)
    if args.refresh_token_file:
        args.refresh_token_file = os.path.abspath(args.refresh_token_file)
    if args.log_file:
        args.log_file = os.path.abspath(args.log_file)
    args.foreground = not args.daemonize

    logging_kwargs: dict[str, Any] = {
        "level": logging.DEBUG if args.debug else logging.INFO,
        "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
    }
    if args.log_file:
        logging_kwargs["filename"] = args.log_file
        logging_kwargs["filemode"] = "a"
    logging.basicConfig(**logging_kwargs)

    LOGGER.info(
        "starting apifuse mode=%s mountpoint=%s api_spec=%s json_input=%s server_url=%s foreground=%s",
        mode,
        args.mountpoint,
        args.api_spec,
        args.json_input,
        args.server_url,
        args.foreground,
    )
    if mode == "openapi":
        has_refresh_token_source = bool(
            args.refresh_token
            or args.refresh_token_file
            or (args.refresh_token_env and os.environ.get(args.refresh_token_env))
        )
        discovery_scope = args.refresh_discovery_path or ["<auth-like default>"]
        LOGGER.debug(
            "refresh config: discover=%s scope=%s url_keys=%s token_keys=%s explicit_refresh_url=%s explicit_refresh_token=%s",
            args.discover_refresh_from_response,
            discovery_scope,
            args.refresh_discovery_url_key or ["refresh_url", "refresh_endpoint", "token_refresh_url"],
            args.refresh_discovery_token_key or ["refresh_token"],
            bool(args.refresh_url),
            has_refresh_token_source,
        )
    if args.log_file:
        LOGGER.info("logging to %s", args.log_file)
    if args.daemonize and sys.platform == "darwin":
        LOGGER.warning(
            "libfuse daemon mode is unreliable on macOS; prefer the default foreground mode and background the process externally"
        )

    if mode == "openapi":
        try:
            provider = APIFuse(
                args.api_spec,
                server_url=args.server_url,
                timeout=args.timeout,
                auth_token=args.auth_token,
                auth_token_file=args.auth_token_file,
                auth_token_env=args.auth_token_env,
                auth_header=args.auth_header,
                auth_scheme=args.auth_scheme,
                refresh_url=args.refresh_url,
                refresh_token=args.refresh_token,
                refresh_token_file=args.refresh_token_file,
                refresh_token_env=args.refresh_token_env,
                refresh_body_token_key=args.refresh_body_token_key,
                refresh_response_token_key=args.refresh_response_token_key,
                discover_refresh_from_response=args.discover_refresh_from_response,
                refresh_discovery_paths=args.refresh_discovery_path,
                refresh_discovery_url_keys=args.refresh_discovery_url_key,
                refresh_discovery_token_keys=args.refresh_discovery_token_key,
                probe_limit=args.probe_limit,
                cache_ttl=args.cache_ttl,
                error_cache_ttl=args.error_cache_ttl,
                cache_max_entries=args.cache_max_entries,
                symlink_names=args.symlink_names,
                symlink_map=args.symlink_map,
            )
            provider.bootstrap_validate(force=args.force)
            operations = ProviderFuse(OpenAPIProviderAdapter(provider))
        except APISpecError as exc:
            parser.error(str(exc))
    else:
        try:
            with open(args.json_input, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"unable to load JSON from {args.json_input}: {exc}")
        operations = JSONFuse(
            payload,
            symlink_names=args.symlink_names,
            symlink_map=args.symlink_map,
        )

    fuse.FUSE(
        operations,
        args.mountpoint,
        foreground=args.foreground,
        ro=True,
        nothreads=True,
    )
    return 0
