"""Check for functions/context_manager.py's synchronous compaction (the live
browser-UI path; only the async summary WORKER was retired, per ledger
DEC-2026-08-15-retire-async-summary). Run from the repo root:

    python3 tools/test_compact.py

Exits non-zero on the first failed assertion.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# context_manager.py calls logging.basicConfig(filename="/app/logs/...") at
# import; that path exists only inside the Open WebUI container. basicConfig is
# a no-op once the root logger has a handler, so configure one first. The
# filter's own behaviour is untouched.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

from functions.context_manager import Filter  # noqa: E402


def _conversation(pairs):
    msgs = [{"role": "system", "content": "system"}]
    for i in range(pairs):
        msgs.append({"role": "user", "content": ("user text %d " % i) + "x" * 300})
        msgs.append({"role": "assistant", "content": "ack %d" % i})
    return msgs


def _filter(**valves):
    f = Filter()
    for k, v in valves.items():
        setattr(f.valves, k, v)
    return f


async def main():
    # Over threshold: oldest turns dropped, system kept, one note, recent kept.
    f = _filter(max_context_tokens=2000, compact_threshold=0.6, preserve_recent=3)
    msgs = _conversation(40)
    out = await f.inlet({"messages": list(msgs)})
    got = out["messages"]
    assert len(got) == 1 + 1 + 6, len(got)
    assert got[0] == msgs[0], "system message must be kept first"
    assert got[1]["role"] == "system" and "74 earlier message(s) were trimmed" in got[1]["content"], got[1]
    assert got[2:] == msgs[-6:], "the 3 most recent pairs must be kept verbatim"
    assert f._estimate_messages_tokens(got) <= 2000 * 0.6, "compacted context must fit the threshold"

    # Under threshold: body returned unchanged.
    small = _conversation(2)
    out = await _filter(max_context_tokens=131072).inlet({"messages": list(small)})
    assert out["messages"] == small

    # Disabled: never compacts, however long.
    out = await _filter(max_context_tokens=2000, compact_threshold=0.6, enabled=False).inlet(
        {"messages": list(msgs)}
    )
    assert out["messages"] == msgs

    # Nothing older than the preserved window: no compaction even over threshold.
    few = _conversation(3)
    out = await _filter(max_context_tokens=100, compact_threshold=0.1, preserve_recent=3).inlet(
        {"messages": list(few)}
    )
    assert out["messages"] == few


asyncio.run(main())
print("test_compact: 4/4 cases passed")
