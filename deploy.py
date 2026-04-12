import subprocess
import os
import sys
import tempfile
import datetime
import io

EC2_HOST        = os.environ.get('EC2_HOST', '13.52.104.120')
EC2_USER        = os.environ.get('EC2_USER', 'ubuntu')
EC2_SSH_KEY     = os.environ.get('EC2_SSH_KEY', '')
EC2_DEPLOY_PATH = os.environ.get('EC2_DEPLOY_PATH', '/home/ubuntu/st-checker-bot')
EC2_SERVICE     = os.environ.get('EC2_SERVICE', 'st-checker-bot')
GITHUB_BRANCH   = os.environ.get('GITHUB_BRANCH', 'main')

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def log(msg, color=RESET):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}")

def run_cmd(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}")
    if result.stderr.strip():
        print(f"  {YELLOW}{result.stderr.strip()}{RESET}")
    return result.returncode

def push_to_github(commit_msg=""):
    print(f"\n{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}{CYAN}   📦  GITHUB PUSH{RESET}")
    print(f"{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    root = os.path.dirname(os.path.abspath(__file__))

    github_remote = os.environ.get("GITHUB_REMOTE", "").strip()
    if not github_remote:
        res = subprocess.run("git remote get-url origin", shell=True, cwd=root,
                             capture_output=True, text=True)
        existing = res.stdout.strip()
        if existing and "gitsafe" not in existing:
            github_remote = existing

    if not github_remote:
        log("GITHUB_REMOTE set nahi hai — GitHub push skip.", YELLOW)
        log("Set karo: export GITHUB_REMOTE=https://github.com/user/repo.git", YELLOW)
        return False

    if not commit_msg:
        commit_msg = f"Update - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    log(f"Remote: {github_remote}", CYAN)
    log(f"Commit: {commit_msg}", CYAN)

    log("Git reinit kar raha hoon...", CYAN)
    # FIX: cwd=root har run_cmd mein pass karna zaroori hai
    run_cmd(f"rm -rf {os.path.join(root, '.git')}", cwd=root)
    run_cmd("git init", cwd=root)
    run_cmd("git add .", cwd=root)
    run_cmd(f'git commit -m "{commit_msg}"', cwd=root)
    run_cmd("git branch -M main", cwd=root)
    run_cmd(f"git remote add origin {github_remote}", cwd=root)

    log(f"GitHub pe force push kar raha hoon...", CYAN)
    code = run_cmd("git push -u origin main --force", cwd=root)
    if code == 0:
        log("GitHub push successful! ✅", GREEN)
        return True
    else:
        log("GitHub push fail hua! ❌", RED)
        return False

def _load_pkey(key_str):
    import paramiko
    import textwrap
    # FIX: tempfile aur io top-level pe already imported hain, dobara import nahi karna

    key_str = key_str.replace('\\n', '\n').strip()

    # PuTTY PPK format detect karein — convert to PEM in-memory
    if key_str.startswith('PuTTY-User-Key-File'):
        try:
            import base64, struct
            from cryptography.hazmat.primitives.asymmetric.rsa import (
                RSAPrivateNumbers, RSAPublicNumbers, rsa_crt_dmp1, rsa_crt_dmq1
            )
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PrivateFormat, NoEncryption
            )

            def _read_blob(data, offset):
                length = struct.unpack('>I', data[offset:offset+4])[0]
                return data[offset+4:offset+4+length], offset+4+length

            def _read_mpint(data, offset):
                val, new_offset = _read_blob(data, offset)
                return int.from_bytes(val, 'big'), new_offset

            ppk_lines = key_str.strip().splitlines()

            pub_idx = next(i for i, l in enumerate(ppk_lines) if l.startswith('Public-Lines:'))
            pub_count = int(ppk_lines[pub_idx].split(': ')[1])
            pub_data = base64.b64decode(''.join(ppk_lines[pub_idx+1:pub_idx+1+pub_count]))

            priv_idx = next(i for i, l in enumerate(ppk_lines) if l.startswith('Private-Lines:'))
            priv_count = int(ppk_lines[priv_idx].split(': ')[1])
            priv_data = base64.b64decode(''.join(ppk_lines[priv_idx+1:priv_idx+1+priv_count]))

            _, offset = _read_blob(pub_data, 0)
            e, offset = _read_mpint(pub_data, offset)
            n, offset = _read_mpint(pub_data, offset)

            d, offset = _read_mpint(priv_data, 0)
            p, offset = _read_mpint(priv_data, offset)
            q, offset = _read_mpint(priv_data, offset)
            iqmp, _ = _read_mpint(priv_data, offset)

            dp = rsa_crt_dmp1(d, p)
            dq = rsa_crt_dmq1(d, q)
            pub_nums = RSAPublicNumbers(e, n)
            priv_nums = RSAPrivateNumbers(p, q, d, dp, dq, iqmp, pub_nums)
            priv_key = priv_nums.private_key(default_backend())
            pem = priv_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
            return paramiko.RSAKey.from_private_key(io.StringIO(pem.decode()))
        except Exception as ppk_err:
            raise ValueError(f"PPK key load nahi ho saka: {ppk_err}")

    # Agar PEM header nahi hai toh raw base64 hai — wrap kar do
    if 'BEGIN' not in key_str:
        b64 = key_str.replace('\n', '').replace('\r', '').replace(' ', '')
        wrapped = '\n'.join(textwrap.wrap(b64, 64))
        candidates = [
            f"-----BEGIN RSA PRIVATE KEY-----\n{wrapped}\n-----END RSA PRIVATE KEY-----\n",
            f"-----BEGIN OPENSSH PRIVATE KEY-----\n{wrapped}\n-----END OPENSSH PRIVATE KEY-----\n",
        ]
    else:
        candidates = [key_str]

    for pem in candidates:
        key_io = io.StringIO(pem)
        for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                key_io.seek(0)
                return cls.from_private_key(key_io)
            except Exception:
                continue

    raise ValueError("SSH key load nahi ho saka — format check karein (RSA/Ed25519/ECDSA).")

def deploy_to_ec2():
    print(f"\n{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}{CYAN}   🚀  EC2 DEPLOY{RESET}")
    print(f"{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    if not EC2_HOST:
        log("EC2_HOST secret set nahi hai! Pehle set karein.", RED)
        return False

    try:
        import paramiko
    except ImportError:
        log("paramiko library nahi mili. 'pip install paramiko' chalayein.", RED)
        return False

    # FIX: abspath use karo taake relative path se bhi theek kaam kare
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    # Key source: env var > local ec2_key.pem > local ec2_key.ppk > ~/.ssh/
    _KEY_FILE_PATHS = [
        os.path.join(_SCRIPT_DIR, "ec2_key.pem"),
        os.path.join(_SCRIPT_DIR, "ec2_key.ppk"),
        os.path.expanduser("~/.ssh/ec2_key.pem"),
        os.path.expanduser("~/.ssh/ec2_key.ppk"),
    ]

    ssh_key_str = EC2_SSH_KEY
    if not ssh_key_str:
        for _kp in _KEY_FILE_PATHS:
            if os.path.exists(_kp):
                with open(_kp, "r") as _kf:
                    ssh_key_str = _kf.read()
                log(f"SSH key file se load kiya: {_kp}", CYAN)
                break

    if not ssh_key_str:
        log("SSH key nahi mili! EC2_SSH_KEY secret set karein ya ~/.ssh/ec2_key.ppk rakhein.", RED)
        return False

    try:
        log("SSH key load kar raha hoon...", CYAN)
        pkey = _load_pkey(ssh_key_str)
    except Exception as e:
        log(f"SSH key error: {e}", RED)
        return False

    # Files jo upload karne hain EC2 pe (local_path → remote_subpath)
    UPLOAD_FILES = [
        "file1.py",
        "main.py",
        "scraper.py",
        "shopify_checker.py",
        "database.py",
        "gatet.py",
        "keep_alive.py",
        "deploy.py",
        "stripe_core.py",
        "dlx_hitter.py",
        "ui_formatter.py",
        "requirements.txt",
        # .scr TG Channel Scraper modules
        "services/__init__.py",
        "services/tg_scraper_service.py",
        "services/cleaner.py",
        "services/ig_reporter.py",
        "services/stripe_link_converter.py",
        "utils/__init__.py",
        "utils/parser.py",
        "utils/tg_scr_validator.py",
        "utils/tg_scr_formatter.py",
    ]

    # Sinket hitter patch files (uploaded to /tmp/ on EC2)
    SINKET_FILES = [
        ("sinket_server_patched.js",  "/tmp/sinket_server_patched.js"),
        ("sinket_hitter_patched.js",  "/tmp/sinket_hitter_patched.js"),
        ("setup_sinket_ec2.sh",       "/tmp/setup_sinket_ec2.sh"),
    ]

    log(f"EC2 se connect kar raha hoon: {EC2_USER}@{EC2_HOST}", CYAN)
    log(f"Deploy path: {EC2_DEPLOY_PATH}", CYAN)

    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=EC2_HOST,
            username=EC2_USER,
            pkey=pkey,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
        log("SSH connection kamyab! ✅", GREEN)

        # SFTP se files seedha upload karein
        sftp = client.open_sftp()

        # Subdirectories EC2 pe create karein agar zaroorat ho
        _created_dirs = set()
        for fname in UPLOAD_FILES:
            subdir = os.path.dirname(fname)
            if subdir and subdir not in _created_dirs:
                remote_dir = f"{EC2_DEPLOY_PATH}/{subdir}"
                try:
                    sftp.stat(remote_dir)
                except FileNotFoundError:
                    try:
                        sftp.mkdir(remote_dir)
                        log(f"  📁 Dir created on EC2: {remote_dir}", CYAN)
                    except Exception as de:
                        log(f"  ⚠️  Dir create fail: {remote_dir} — {de}", YELLOW)
                _created_dirs.add(subdir)

        uploaded = 0
        for fname in UPLOAD_FILES:
            local_path = os.path.join(_SCRIPT_DIR, fname)
            if not os.path.exists(local_path):
                log(f"  Skip (nahi mila): {fname}", YELLOW)
                continue
            remote_path = f"{EC2_DEPLOY_PATH}/{fname}"
            try:
                sftp.put(local_path, remote_path)
                size_kb = os.path.getsize(local_path) / 1024
                log(f"  ✅ Uploaded: {fname}  ({size_kb:.1f} KB)", GREEN)
                uploaded += 1
            except Exception as fe:
                log(f"  ❌ Upload fail: {fname} — {fe}", RED)
        sftp.close()
        log(f"{uploaded}/{len(UPLOAD_FILES)} files uploaded.", CYAN)

        # ── Sinket hitter patch files upload ──────────────────────────────────
        _sinket_ok = True
        sftp3 = client.open_sftp()
        for _local, _remote in SINKET_FILES:
            # FIX: abspath-based _SCRIPT_DIR use karo
            _lpath = os.path.join(_SCRIPT_DIR, _local)
            if not os.path.exists(_lpath):
                log(f"  Sinket file missing (skip): {_local}", YELLOW)
                _sinket_ok = False
                continue
            try:
                sftp3.put(_lpath, _remote)
                log(f"  ✅ Sinket: {_local} → {_remote}", GREEN)
            except Exception as _se:
                log(f"  ❌ Sinket upload fail: {_local} — {_se}", RED)
                _sinket_ok = False
        sftp3.close()

        # ── Sync env vars (BOT_TOKEN / ADMIN_ID) from Replit secrets → EC2 service file ──
        _bot_token  = os.environ.get("BOT_TOKEN",  "").strip()
        _admin_id   = os.environ.get("ADMIN_ID",   "").strip()
        if _bot_token and _admin_id:
            _svc_path   = f"/etc/systemd/system/{EC2_SERVICE}.service"
            _tmp_path   = f"/home/{EC2_USER}/.svc_tmp_{EC2_SERVICE}.service"
            _svc_content = (
                "[Unit]\n"
                f"Description=ST-CHECKER Telegram Bot\n"
                "After=network.target\n\n"
                "[Service]\n"
                "Type=simple\n"
                f"User={EC2_USER}\n"
                f"WorkingDirectory={EC2_DEPLOY_PATH}\n"
                f'Environment="BOT_TOKEN={_bot_token}"\n'
                f'Environment="ADMIN_ID={_admin_id}"\n'
                f"ExecStart={EC2_DEPLOY_PATH}/venv/bin/python3 {EC2_DEPLOY_PATH}/main.py\n"
                "Restart=always\n"
                "RestartSec=10\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            # FIX: top-level io already imported hai, _io alias zaroorat nahi
            sftp2 = client.open_sftp()
            with sftp2.file(_tmp_path, "w") as _f:
                _f.write(_svc_content)
            sftp2.close()
            _mv_cmd = f"sudo mv {_tmp_path} {_svc_path} && sudo systemctl daemon-reload"
            stdin, stdout, stderr = client.exec_command(_mv_cmd, get_pty=False)
            stdout.channel.recv_exit_status()
            log("Service file updated with fresh BOT_TOKEN & ADMIN_ID ✅", GREEN)
        else:
            log("BOT_TOKEN/ADMIN_ID not found in env — skipping service file sync", YELLOW)

        # Install/update Python dependencies including playwright
        log("pip install -r requirements.txt chal raha hai...", CYAN)
        pip_cmd = (
            f"{EC2_DEPLOY_PATH}/venv/bin/pip install -r {EC2_DEPLOY_PATH}/requirements.txt -q && "
            f"{EC2_DEPLOY_PATH}/venv/bin/playwright install chromium --with-deps 2>/dev/null || true"
        )
        _pi, _po, _pe = client.exec_command(pip_cmd, get_pty=False)
        _pi_code = _po.channel.recv_exit_status()
        _pi_err = _pe.read().decode(errors='replace').strip()
        if _pi_code == 0:
            log("Dependencies updated ✅", GREEN)
        else:
            log(f"pip warning (non-fatal): {_pi_err[:120]}", YELLOW)

        # Bot service restart karein
        log(f"Bot restart kar raha hoon: {EC2_SERVICE}", CYAN)
        restart_cmd = f"sudo systemctl restart {EC2_SERVICE}"
        stdin, stdout, stderr = client.exec_command(restart_cmd, get_pty=False)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors='replace').strip()
        err = stderr.read().decode(errors='replace').strip()
        if out: print(f"  {out}")
        if err: print(f"  {YELLOW}{err}{RESET}")

        # ── Sinket Hitter setup (non-blocking, runs in background) ───────────
        if _sinket_ok:
            log("Sinket Hitter setup chal raha hai...", CYAN)
            _sinket_setup_cmd = (
                "chmod +x /tmp/setup_sinket_ec2.sh && "
                "nohup bash /tmp/setup_sinket_ec2.sh > /tmp/sinket_setup.log 2>&1 &"
            )
            s_in, s_out, s_err = client.exec_command(_sinket_setup_cmd, get_pty=False)
            s_out.channel.recv_exit_status()
            # Check if sinket-hitter is already active (skip wait if already running)
            _, _stat_out, _ = client.exec_command("sudo systemctl is-active sinket-hitter")
            _sk_status = _stat_out.read().decode().strip()
            if _sk_status == "active":
                log("Sinket Hitter already active ✅ (port 3001)", GREEN)
            else:
                log("Sinket Hitter setup background mein chal raha hai ⏳", YELLOW)
                log("  (60-120s lagta hai pehli baar — /tmp/sinket_setup.log check karo)", YELLOW)
        else:
            log("Sinket Hitter skip (patch files missing)", YELLOW)

        client.close()

        if exit_code == 0:
            log("EC2 deploy successful! Bot restart ho gaya ✅", GREEN)
            return True
        else:
            log(f"Restart fail hua (exit code: {exit_code}) ❌", RED)
            return False

    except Exception as e:
        log(f"EC2 connection error: {e}", RED)
        return False

def main():
    args = sys.argv[1:]
    skip_github = "--skip-github" in args or "--ec2-only" in args
    remaining = [a for a in args if not a.startswith("--")]
    commit_msg = remaining[0] if remaining else f"Update - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    print(f"\n{BOLD}{GREEN}╔══════════════════════════════════╗")
    print(f"║    🛠️  DEPLOY SCRIPT              ║")
    print(f"║  GitHub + EC2: {EC2_HOST}  ║")
    print(f"╚══════════════════════════════════╝{RESET}\n")
    print(f"{CYAN}Commit message: \"{commit_msg}\"{RESET}")
    if skip_github:
        print(f"{YELLOW}GitHub push: SKIPPED (--skip-github){RESET}")

    # Step 1: GitHub push (only if not skipped)
    github_ok = False
    if not skip_github:
        github_ok = push_to_github(commit_msg)
    else:
        github_ok = None  # N/A

    # Step 2: EC2 deploy (always)
    ec2_ok = deploy_to_ec2()

    print(f"\n{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}   📊  DEPLOY SUMMARY{RESET}")
    print(f"{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    if github_ok is None:
        print(f"  GitHub : {YELLOW}⏭️  Skipped{RESET}")
    else:
        print(f"  GitHub : {GREEN+'✅ Success' if github_ok else YELLOW+'⚠️  Skipped/Failed'}{RESET}")
    print(f"  EC2    : {GREEN+'✅ Success' if ec2_ok else RED+'❌ Failed'}{RESET}")
    print()

    if not ec2_ok:
        sys.exit(1)

if __name__ == "__main__":
    main()
