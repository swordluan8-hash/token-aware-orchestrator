#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional
from urllib import error, request


@dataclass
class ProbeResult:
    provider: str
    model: str
    available: bool
    status: str
    latency_ms: float
    base_url: str
    num_ctx: Optional[int] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def _api_request(
    base_url: str,
    endpoint: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 5,
) -> Dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}


def _classify_error(exc: Exception) -> str:
    msg = str(exc)
    if isinstance(exc, error.URLError):
        return "api_unreachable"
    low = msg.lower()
    if "out of memory" in low or "allocate" in low or "kv cache" in low:
        return "out_of_memory"
    if "failed to load model" in low:
        return "model_unavailable"
    return "not_ready"


def run_probe(
    *,
    provider: str,
    model: str,
    base_url: str = "http://127.0.0.1:11434",
    num_ctx: int = 1024,
    timeout_seconds: int = 8,
    mock: bool = False,
    mock_available: bool = False,
    mock_error: str = "",
) -> ProbeResult:
    start = time.perf_counter()

    if provider == "mock":
        return ProbeResult(
            provider=provider,
            model=model,
            available=bool(mock_available),
            status="mock_available" if mock_available else "mock_unavailable",
            latency_ms=0.0,
            base_url=base_url,
            num_ctx=num_ctx,
            error=mock_error or None,
            details={"mock": True},
        )

    if mock:
        return ProbeResult(
            provider=provider,
            model=model,
            available=False,
            status="mock_enabled_but_provider_not_mock",
            latency_ms=0.0,
            base_url=base_url,
            num_ctx=num_ctx,
            error="mock mode enabled but provider is not mock",
            details={"mock": True},
        )

    try:
        ps = _api_request(base_url, "/api/ps", timeout=timeout_seconds)
        tags = ps.get("models", []) if isinstance(ps, dict) else []
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            provider=provider,
            model=model,
            available=False,
            status=_classify_error(exc),
            latency_ms=latency_ms,
            base_url=base_url,
            num_ctx=num_ctx,
            error=str(exc),
            details={"phase": "check_ps"},
        )

    model_found = False
    for item in tags:
        if isinstance(item, dict) and item.get("name") == model:
            model_found = True
            break

    if not model_found:
        try:
            catalog = _api_request(base_url, "/api/tags", timeout=timeout_seconds)
            if isinstance(catalog, dict):
                names = [entry.get("name") for entry in catalog.get("models", []) if isinstance(entry, dict)]
                if model in names:
                    model_found = True
        except Exception:
            names = []

    if not model_found:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            provider=provider,
            model=model,
            available=False,
            status="model_not_found",
            latency_ms=latency_ms,
            base_url=base_url,
            num_ctx=num_ctx,
            error=f"model '{model}' not present",
            details={"check": "api_tags"},
        )

    try:
        _api_request(
            base_url,
            "/api/generate",
            method="POST",
            payload={
                "model": model,
                "prompt": "Say ok",
                "stream": False,
                "options": {"num_ctx": num_ctx},
            },
            timeout=timeout_seconds,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            provider=provider,
            model=model,
            available=True,
            status="ready",
            latency_ms=latency_ms,
            base_url=base_url,
            num_ctx=num_ctx,
            details={"phase": "generate_ping"},
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ProbeResult(
            provider=provider,
            model=model,
            available=False,
            status=_classify_error(exc),
            latency_ms=latency_ms,
            base_url=base_url,
            num_ctx=num_ctx,
            error=str(exc),
            details={"phase": "generate_ping"},
        )


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Probe local worker")
    parser.add_argument("--provider", default="ollama_chat")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--num-ctx", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--mock-available", action="store_true")
    parser.add_argument("--mock-error", default="")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    result = run_probe(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        num_ctx=args.num_ctx,
        timeout_seconds=args.timeout,
        mock=args.mock,
        mock_available=args.mock_available,
        mock_error=args.mock_error,
    )
    print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    return 0 if result.available else 1


if __name__ == "__main__":
    raise SystemExit(main())
