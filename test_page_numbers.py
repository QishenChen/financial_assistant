#!/usr/bin/env python3
"""Quick test to verify page numbers appear in ref links."""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.executor import execute

print("Running quick test: 招商银行不良贷款余额是多少")
print("-" * 50)

r = execute('招商银行不良贷款余额是多少')
ans = r.get('answer', '')

# Count refs with and without page numbers
all_refs = re.findall(r'\(ref:([^)]+)\)', ans)
with_pages = [r for r in all_refs if re.search(r':\d+$', r)]
without_pages = [r for r in all_refs if not re.search(r':\d+$', r)]

print(f"Total ref links: {len(all_refs)}")
print(f"With page numbers: {len(with_pages)}")
print(f"Without page numbers: {len(without_pages)}")

if with_pages:
    print("\n✓ SUCCESS - Page numbers found in refs:")
    for r in with_pages[:5]:
        print(f"  ref:{r}")
else:
    print("\n✗ FAILED - No page numbers in refs")
    print("Sample refs:")
    for r in all_refs[:5]:
        print(f"  ref:{r}")

# Show a snippet of the answer
print(f"\nAnswer preview ({len(ans)} chars):")
print(ans[:500])

# Steps log
print("\nExecution steps:")
for s in r.get('steps_log', []):
    print(f"  [{s.get('verdict','?')}] Step {s.get('step')}: {s.get('task_type')}")