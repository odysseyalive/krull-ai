import asyncio, json
from functions.context_manager import Filter

async def run_test():
    f = Filter()
    f.valves.max_context_tokens = 2000
    f.valves.compact_threshold = 0.6
    f.valves.preserve_recent = 3
    msgs = [{"role":"system","content":"system"}]
    for i in range(40):
        msgs.append({"role":"user","content":("user text %d " % i) + "x"*300})
        msgs.append({"role":"assistant","content":"ack %d" % i})
    body = {"messages": msgs}
    out = await f.inlet(body)
    print("After inlet, message count:", len(out["messages"]))
    print(json.dumps(out["messages"][:4], indent=2)[:2000])

asyncio.run(run_test())
