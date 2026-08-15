#!/usr/bin/env python3
"""Detect environment + Ollama readiness for token-aware routing."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Optional
from urllib.request import urlopen


def run_cmd(cmd: str, timeout: int = 5) -> Optional[str]:
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout)
        return out.decode("utf-8", "ignore")
    except Exception:
        return None


def safe_float(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return float(v)
    except Exception:
        return None


def parse_memory_pressure(raw: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if not raw:
        return data
    m = re.search(r"System-wide memory free percentage:\s*(\d+)%", raw)
    if m:
        data["free_percent"] = int(m.group(1))
    for key in [
        "Pages free",
        "Pages active",
        "Pages inactive",
        "Pages wired down",
        "Pages occupied by compressor",
        "Pages stored in compressor",
        "Swapins",
        "Swapouts",
    ]:
        mm = re.search(rf"^{re.escape(key)}:\s+([0-9.]+)", raw, re.M)
        if mm:
            data[key.replace(" ", "_").lower()] = int(mm.group(1).split(".")[0]) if mm.group(1).isdigit() else mm.group(1)
    return data


def detect_hardware() -> Dict[str, Any]:
    return {
        "os": run_cmd("uname -s") and run_cmd("uname -s").strip(),
        "os_version": run_cmd("uname -r") and run_cmd("uname -r").strip(),
        "arch": run_cmd("uname -m") and run_cmd("uname -m").strip(),
        "cpu": run_cmd("sysctl -n hw.logicalcpu") and run_cmd("sysctl -n hw.logicalcpu").strip(),
        "memory_bytes": run_cmd("sysctl -n hw.memsize") and run_cmd("sysctl -n hw.memsize").strip(),
        "gpu_hint": run_cmd("system_profiler SPDisplaysDataType 2>/dev/null | rg -i 'Chip|Model|VRAM|Metal' | head -n 20") or "",
    }


def detect_memory() -> Dict[str, Any]:
    pressure_raw = run_cmd("memory_pressure") or ""
    vm_raw = run_cmd("vm_stat") or ""
    return {
        "memory_pressure_raw": bool(pressure_raw),
        "memory_pressure": parse_memory_pressure(pressure_raw),
        "vm_free": (lambda: parse_memory_pressure(vm_raw).get("pages_free"))(),
        "vm_inactive": (lambda: parse_memory_pressure(vm_raw).get("pages_inactive"))(),
        "vm_active": (lambda: parse_memory_pressure(vm_raw).get("pages_active"))(),
    }


def detect_ollama(host: str, model: str) -> Dict[str, Any]:
    # binary
    binary = os.environ.get("OLLAMA_BIN") or shutil.which("ollama")

    # api
    version = None
    models_list = []
    loaded = []
    reachable = False
    api_error = None
    try:
        with urlopen(f"{host}/api/version", timeout=3) as r:
            if r.status == 200:
                version = json.loads(r.read().decode("utf-8", "ignore")).get("version")
                reachable = True
    except Exception as exc:
        api_error = str(exc)

    if reachable:
        try:
            with urlopen(f"{host}/api/tags", timeout=3) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                    models_list = [m.get("name") for m in data.get("models", []) if m.get("name")]
        except Exception:
            pass
        try:
            with urlopen(f"{host}/api/ps", timeout=3) as r:
                if r.status == 200:
                    loaded = json.loads(r.read().decode("utf-8", "ignore")).get("models", [])
        except Exception:
            pass

    return {
        "binary": binary if os.path.exists(binary) else None,
        "host": host,
        "version": version,
        "reachable": reachable,
        "reachable_error": api_error,
        "models_available": models_list,
        "loaded_models": loaded,
        "model_available": model in models_list if model else (len(models_list) > 0),
    }


def evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    hw = detect_hardware()
    memory = detect_memory()
    ollama = detect_ollama(args.host, args.model)

    return {
        "status": "ok",
        "hardware": hw,
        "memory": memory,
        "ollama": ollama,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="qwen2.5-coder:7b")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    report = evaluate(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        m = report["memory"].get("memory_pressure", {})
        print(f"OS: {report['hardware'].get('os')} {report['hardware'].get('os_version')}")
        print(f"Arch: {report['hardware'].get('arch')} | CPU: {report['hardware'].get('cpu')}")
        print(f"Ollama reachable: {report['ollama']['reachable']} version={report['ollama']['version']}")
        print(f"Model {args.model} available: {report['ollama']['model_available']}")
        print(f"Memory free percent: {m.get('free_percent')} pages_free={m.get('pages_free')}")


if __name__ == "__main__":
    main()
