#!/usr/bin/env python3
"""Small JSON bridge between the browser worker and fenced LinkedIn state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from linkedin_easy_apply_adapter import DurableIntentStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    reserve = sub.add_parser("reserve")
    reserve.add_argument("--input", required=True)

    begin = sub.add_parser("begin")
    begin.add_argument("--intent-id", required=True, type=int)
    begin.add_argument("--worker-id", required=True)

    ambiguous = sub.add_parser("ambiguous")
    ambiguous.add_argument("--intent-id", required=True, type=int)
    ambiguous.add_argument("--token", required=True)
    ambiguous.add_argument("--reason", required=True)

    receipt = sub.add_parser("receipt")
    receipt.add_argument("--intent-id", required=True, type=int)
    receipt.add_argument("--token", required=True)
    receipt.add_argument("--readback", required=True)
    receipt.add_argument("--evidence", required=True)

    args = parser.parse_args()
    store = DurableIntentStore(args.db)
    if args.command == "reserve":
        data = json.loads(Path(args.input).read_text())
        intent = store.reserve(**data)
        result = {"intent_id": intent.intent_id, "created": intent.created}
    elif args.command == "begin":
        result = {"execution_token": store.begin_submit(args.intent_id, worker_id=args.worker_id)}
    elif args.command == "ambiguous":
        store.mark_submit_ambiguous(args.intent_id, execution_token=args.token, reason=args.reason)
        result = {"state": "ambiguous"}
    else:
        readback = json.loads(Path(args.readback).read_text())
        receipt_id = store.record_submit_readback(
            args.intent_id,
            execution_token=args.token,
            readback=readback,
            evidence_bytes=Path(args.evidence).read_bytes(),
        )
        result = {"state": "verified", "receipt_id": receipt_id}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
