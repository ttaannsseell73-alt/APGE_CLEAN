import re
import subprocess
import sys
from pathlib import Path


ALLOWED_PLACEHOLDER_MARKERS = ("your_", "test_", "example", "placeholder", "<", "${")
ASSIGNMENT = re.compile(
    r"(?im)^\s*BINANCE_(?:TESTNET_)?API_(?:KEY|SECRET)\s*=\s*([^\s#]+)\s*$"
)
HIGH_SIGNAL = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9_-]{24,}"),
)


def _tracked_files():
    completed = subprocess.run(
        ["git", "ls-files", "-z"], check=True, stdout=subprocess.PIPE
    )
    return [Path(item.decode("utf-8")) for item in completed.stdout.split(b"\0") if item]


def main() -> int:
    problems = []
    for path in _tracked_files():
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            problems.append(f"tracked secret environment file: {path}")
            continue
        try:
            data = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for match in ASSIGNMENT.finditer(data):
            value = match.group(1).strip().strip('"\'')
            lower = value.lower()
            if value and not any(marker in lower for marker in ALLOWED_PLACEHOLDER_MARKERS):
                problems.append(f"credential-like Binance assignment: {path}")

        for pattern in HIGH_SIGNAL:
            if pattern.search(data):
                problems.append(f"high-signal credential pattern: {path}")

    if problems:
        for problem in sorted(set(problems)):
            print(problem)
        return 1
    print("repository secret scan: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
