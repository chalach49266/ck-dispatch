"""Throwaway: print the context around a string in the .js files of a folder (or one file)."""
import os
import sys

path, needle = sys.argv[1], sys.argv[2]
before, after = int(sys.argv[3]) if len(sys.argv) > 3 else 200, int(sys.argv[4]) if len(sys.argv) > 4 else 400
files = [os.path.join(path, n) for n in sorted(os.listdir(path)) if n.endswith(".js")] if os.path.isdir(path) else [path]
total = 0
for p in files:
    data = open(p, encoding="utf-8", errors="replace").read()
    i, n = 0, 0
    while n < 6 and total < 30:
        i = data.find(needle, i)
        if i < 0:
            break
        print(f"--- {os.path.basename(p)} @{i}\n{data[max(0, i - before):i + after]}\n")
        i += len(needle)
        n += 1
        total += 1
