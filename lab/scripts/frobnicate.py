"""Case obscure-tool: an in-house tool with its own error codes."""

import sys

print(f"frobnicate 1.4.2: checking {sys.argv[1] if len(sys.argv) > 1 else '-'}")
print("E9921: the frobnicator rejected the manifest (state=QX)")
sys.exit(1)
