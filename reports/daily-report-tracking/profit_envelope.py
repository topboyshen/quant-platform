"""Fresh fixed runtime; preserves the scheduler parent/dispatch chain."""
import os

if __name__ == "__main__":
    os.umask(0o077)
    os.execv("/bin/bash", [
        "/bin/bash",
        "/home/ubuntu/releases/daily-report-tracking-release-20261109.2/candidate/run_original.sh",
        "profit",
    ])
