import json
from collections import Counter
from datetime import datetime

def analyze_transcript(path):
    tokens = {'input': 0, 'output': 0, 'total': 0, 'cache_read': 0}
    tool_counts = Counter()
    steps = 0
    timestamps = []
    errors = 0
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): 
                continue
            ev = json.loads(line)
            ts = ev.get('created_at')
            if ts:
                timestamps.append(datetime.fromisoformat(ts.replace('Z', '+00:00')))
            
            # token counting
            inp = ev.get('input_tokens', 0)
            out = ev.get('output_tokens', 0)
            cache = ev.get('cache_read_tokens', 0)
            tokens['input'] += inp
            tokens['output'] += out
            tokens['cache_read'] += cache
            tokens['total'] += inp + out + cache
            
            # steps & tools
            if ev.get('type') == 'PLANNER_RESPONSE':
                steps += 1
                for tc in ev.get('tool_calls', []):
                    tool_counts[tc.get('name')] += 1
            if ev.get('status') == 'ERROR':
                errors += 1
                
    dur = 0
    if len(timestamps) >= 2:
        dur = (timestamps[-1] - timestamps[0]).total_seconds()
    return steps, dur, tokens, tool_counts, errors

agents = [
    ("Business Task: Native Agent (No MCP)", "C:/Users/pc/.gemini/antigravity/brain/799acdfd-3222-49b2-9b9b-eaa5fcce0eb8/.system_generated/logs/transcript.jsonl"),
    ("Business Task: RootScout MCP Agent", "C:/Users/pc/.gemini/antigravity/brain/5a88582d-6661-40a5-b902-efd071640fe5/.system_generated/logs/transcript.jsonl")
]

for name, p in agents:
    steps, dur, tokens, tools, errors = analyze_transcript(p)
    print(f"=== {name} ===")
    print(f"Duration: {dur:.1f}s ({dur/60:.2f} min)")
    print(f"Planner Steps: {steps}")
    print(f"Tool Errors / Retries: {errors}")
    print(f"Tokens: Input={tokens['input']:,}, Output={tokens['output']:,}, CacheRead={tokens['cache_read']:,}, Total={tokens['total']:,}")
    print(f"Total Tool Calls ({sum(tools.values())}): {dict(tools)}")
    print()
