"""
push.py — Sirf GitHub pe push karo.
Usage:
    python push.py "commit message"
    python push.py              # default: "Update - YYYY-MM-DD HH:MM"

Env vars:
    GITHUB_REMOTE — GitHub repo URL (e.g. https://github.com/user/repo.git)
"""
import sys
import os
import subprocess
import datetime

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def log(msg, color=RESET):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}")

def run(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}")
    if result.stderr.strip():
        print(f"  {YELLOW}{result.stderr.strip()}{RESET}")
    return result.returncode

def main():
    commit_msg = sys.argv[1] if len(sys.argv) > 1 else f"Update - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
    root = os.path.dirname(os.path.abspath(__file__))

    print(f"\n{BOLD}{GREEN}╔══════════════════════════════════╗")
    print(f"║    📦  GITHUB PUSH                ║")
    print(f"╚══════════════════════════════════╝{RESET}\n")
    print(f"{CYAN}Commit message: \"{commit_msg}\"{RESET}")

    github_remote = os.environ.get("GITHUB_REMOTE", "").strip()
    if not github_remote:
        res = subprocess.run("git remote get-url origin", shell=True, cwd=root,
                             capture_output=True, text=True)
        existing = res.stdout.strip()
        if existing and "gitsafe" not in existing:
            github_remote = existing

    if not github_remote:
        log("GITHUB_REMOTE set nahi hai!", RED)
        log("Set karo: export GITHUB_REMOTE=https://github.com/user/repo.git", YELLOW)
        sys.exit(1)

    print(f"\n{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    log(f"Remote: {github_remote}", CYAN)

    log("Git reinit kar raha hoon...", CYAN)
    run(f"rm -rf {os.path.join(root, '.git')}", cwd=root)
    run("git init", cwd=root)
    run("git add .", cwd=root)
    run(f'git commit -m "{commit_msg}"', cwd=root)
    run("git branch -M main", cwd=root)
    run(f"git remote add origin {github_remote}", cwd=root)

    log("Force push kar raha hoon...", CYAN)
    code = run("git push -u origin main --force", cwd=root)

    print(f"\n{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    if code == 0:
        print(f"  {GREEN}✅ GitHub push successful!{RESET}")
    else:
        print(f"  {RED}❌ GitHub push fail hua!{RESET}")
        sys.exit(1)
    print()

if __name__ == "__main__":
    main()
