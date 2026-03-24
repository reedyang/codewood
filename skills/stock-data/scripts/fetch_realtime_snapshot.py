#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch realtime A-share snapshots from Sina Finance quote API and
print analyzer-compatible JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Dict, List
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "")
        if s == "" or s.lower() == "nan":
            return default
        return float(s)
    except Exception:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "")
        if s == "" or s.lower() == "nan":
            return default
        return int(float(s))
    except Exception:
        return default


def _normalize_a_code(code: str) -> str:
    c = (code or "").strip()
    c = c.lower().replace("sh", "").replace("sz", "")
    if not c:
        return c
    if re.fullmatch(r"\d{1,6}", c):
        return c.zfill(6)
    return c


def _parse_codes(raw_codes: List[str]) -> List[str]:
    out: List[str] = []
    for item in raw_codes:
        for part in item.split(","):
            code = _normalize_a_code(part)
            if code:
                out.append(code)
    uniq: List[str] = []
    seen = set()
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _to_sina_symbol(code: str) -> str:
    c = _normalize_a_code(code)
    if not c:
        return c
    if c[0] in ("5", "6", "9"):
        return f"sh{c}"
    return f"sz{c}"


def _fetch_sina_quotes(codes: List[str], timeout: float = 10.0) -> Dict[str, Dict[str, Any]]:
    if not codes:
        return {}

    symbol_to_code: Dict[str, str] = {_to_sina_symbol(c): c for c in codes}
    symbols = [s for s in symbol_to_code.keys() if s]
    if not symbols:
        return {}

    url = f"https://hq.sinajs.cn/list={','.join(symbols)}"
    req = Request(
        url=url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Sina quote request failed: {exc}") from exc

    text = raw.decode("gbk", errors="replace")
    out: Dict[str, Dict[str, Any]] = {}

    for line in text.splitlines():
        m = re.match(r'^var\s+hq_str_([a-z]{2}\d{6})="(.*)";\s*$', line.strip())
        if not m:
            continue
        symbol, payload = m.group(1), m.group(2)
        code = symbol_to_code.get(symbol)
        if not code or not payload:
            continue
        parts = payload.split(",")
        if len(parts) < 10:
            continue

        name = parts[0].strip() or code
        open_price = _to_float(parts[1])
        pre_close = _to_float(parts[2])
        price = _to_float(parts[3])
        high = _to_float(parts[4], default=price)
        low = _to_float(parts[5], default=price)
        volume = _to_int(parts[8])
        amount = _to_float(parts[9])

        change_amount = price - pre_close if price > 0 and pre_close > 0 else 0.0
        change_pct = (change_amount / pre_close * 100.0) if pre_close > 0 else 0.0

        if price <= 0:
            continue

        out[code] = {
            "name": name,
            "price": round(price, 4),
            "change_pct": round(change_pct, 4),
            "change_amount": round(change_amount, 4),
            "open_price": round(open_price, 4),
            "high": round(high, 4),
            "low": round(low, 4),
            "volume": volume,
            "amount": amount,
            "pre_close": round(pre_close, 4),
        }

    return out


def fetch_snapshots(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    return _fetch_sina_quotes(codes)


def fetch_snapshots_with_retry(codes: List[str], retries: int, retry_delay: float) -> Dict[str, Dict[str, Any]]:
    last_err: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            return fetch_snapshots(codes)
        except Exception as err:
            last_err = err
            if attempt >= retries:
                break
            time.sleep(max(0.1, retry_delay) * attempt)
    if last_err is not None:
        raise last_err
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch realtime A-share snapshots from Sina Finance quote API"
    )
    parser.add_argument(
        "codes",
        nargs="*",
        help="A-share codes, supports space-separated or comma-separated values",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON (single line)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retry count for transient network errors (default: 3)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=1.0,
        help="Base retry delay seconds for backoff (default: 1.0)",
    )
    args = parser.parse_args()

    codes = _parse_codes(args.codes) if args.codes else []
    if not codes:
        print("{}")
        return 0

    try:
        snapshots = fetch_snapshots_with_retry(
            codes=codes,
            retries=max(1, int(args.retries)),
            retry_delay=max(0.1, float(args.retry_delay)),
        )
    except RuntimeError as err:
        print(f"[stock-data] {err}", file=sys.stderr)
        return 1
    except Exception as err:
        msg = str(err)
        if "RemoteDisconnected" in msg or "Connection aborted" in msg:
            print(
                "[stock-data] network error after retries: 远端连接中断。"
                "请稍后重试，或在命令中增加 --retries/--retry-delay。",
                file=sys.stderr,
            )
            return 2
        print(f"[stock-data] unexpected error: {err}", file=sys.stderr)
        return 1

    if args.compact:
        print(json.dumps(snapshots, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
