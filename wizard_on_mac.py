import json
import os
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox, scrolledtext
from pathlib import Path
import requests


def get_access_code(url="https://foreigner-eshi.onrender.com/"):
    try:
        res = requests.get(url, timeout=30)
        match = re.search(r"__ACCESS_CODE__\s*=\s*(\d+)", res.text)
        if match:
            return int(match.group(1))
        return None
    except requests.RequestException:
        return None


access_code = get_access_code()

import playitSide_on_mac

SECRET_KEY = "5b9da02a2a0040085a4d06f0b6c84591b6d06578dc4efd52ba0b276d582313cd"
RELEASE_API = "https://api.github.com/repos/playit-cloud/playit-agent/releases/latest"

APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "playit_wizard"
INSTALL_DIR = APP_SUPPORT_ROOT / "runtime"
BIN_DIR = INSTALL_DIR / "bin"
CFG_DIR = INSTALL_DIR / "config"
STORE_DIR = CFG_DIR / "playit_gg"
META_FILE = INSTALL_DIR / "meta.json"

BACKUP_CFG_DIR = Path(__file__).resolve().parent / "playit_gg"

OS_KEYS = {"windows": ["windows"], "linux": ["linux"], "darwin": ["darwin", "macos", "mac"]}
ARCH_KEYS = {
    "x86_64": ["x86_64", "amd64", "x64"],
    "amd64": ["x86_64", "amd64", "x64"],
    "arm64": ["arm64", "aarch64"],
    "aarch64": ["arm64", "aarch64"],
}
SKIP_SUFFIXES = (".sha256", ".sha", ".sig", ".zip", ".tar.gz", ".msi", ".deb", ".rpm", ".txt")
URL_RE = re.compile(r"https?://\S+")

GREEN = "#1a9c3c"
RED = "#d12f2f"
ORANGE = "#d98a00"
GREY = "#666666"

IS_MACOS = platform.system().lower() == "darwin"


def system_cfg_dir():
    system = platform.system().lower()
    if system == "darwin":
        return APP_SUPPORT_ROOT / "config" / "playit_gg"
    if system == "windows":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "playit_gg"
    return Path.home() / ".config" / "playit_gg"


SYSTEM_CFG = system_cfg_dir()


def has_cfg(path):
    try:
        if not path.exists():
            return False
        p_file = path / "playit"
        t_file = path / "playit.toml"
        valid_p = p_file.exists() and p_file.stat().st_size > 0
        valid_t = t_file.exists() and t_file.stat().st_size > 0
        return valid_p or valid_t
    except Exception:
        return False


def normalize_cfg(target_path):
    try:
        toml_src = target_path / "playit.toml"
        playit_target = target_path / "playit"
        if toml_src.exists() and not playit_target.exists():
            shutil.copy2(toml_src, playit_target)
        elif playit_target.exists() and not toml_src.exists():
            shutil.copy2(playit_target, toml_src)
    except Exception:
        pass


def provision_secret_if_needed():
    if not SECRET_KEY or len(SECRET_KEY.strip()) < 10:
        return
    content = f'secret_key = "{SECRET_KEY.strip()}"\n'
    for folder in (SYSTEM_CFG, STORE_DIR, BACKUP_CFG_DIR):
        try:
            if not has_cfg(folder):
                folder.mkdir(parents=True, exist_ok=True)
                toml_file = folder / "playit.toml"
                toml_file.write_text(content, encoding="utf-8")
                normalize_cfg(folder)
        except Exception:
            pass


def pick_asset(assets, system, machine):
    os_keys = OS_KEYS.get(system)
    arch_keys = ARCH_KEYS.get(machine)
    if not os_keys or not arch_keys:
        raise OSError(f"Unsupported platform: {system}/{machine}")
    matches = []
    for asset in assets:
        name = asset["name"].lower()
        if name.endswith(SKIP_SUFFIXES):
            continue
        if system == "windows" and not name.endswith(".exe"):
            continue
        if any(k in name for k in os_keys) and any(k in name for k in arch_keys):
            matches.append(asset)
    if not matches:
        raise RuntimeError("Latest release matching binary not found")
    matches.sort(key=lambda a: ("signed" not in a["name"].lower(), len(a["name"])))
    return matches[0]


def read_meta():
    try:
        return json.loads(META_FILE.read_text())
    except Exception:
        return None


def get_binary():
    meta = read_meta()
    if not meta:
        return None
    path = BIN_DIR / meta["binary"]
    if path.exists() and path.stat().st_size > 0:
        return path
    return None


def build_env():
    return os.environ.copy()


def on_rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except Exception:
        pass


def remove_tree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_rm_error)
    else:
        shutil.rmtree(path, onerror=on_rm_error)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.proc = None
        self.busy = False
        self.claim_url = None
        self.sync_lock = threading.Lock()
        self.side_thread = None
        self.unlocked = False

        root.title("Wizard on macOS")
        root.geometry("640x620")
        root.minsize(560, 520)

        gate = tk.Frame(root, padx=12, pady=10)
        gate.pack(fill="x")
        tk.Label(gate, text="Access code:", font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.code_var = tk.StringVar()
        self.code_entry = tk.Entry(gate, textvariable=self.code_var, width=20, show="*")
        self.code_entry.pack(side="left", padx=6)
        self.code_entry.bind("<Return>", lambda e: self.on_unlock())
        self.btn_unlock = tk.Button(gate, text="Unlock", width=10, command=self.on_unlock)
        self.btn_unlock.pack(side="left", padx=4)
        self.lbl_gate = tk.Label(gate, text="Locked", fg=RED)
        self.lbl_gate.pack(side="left", padx=8)

        top = tk.Frame(root, padx=12, pady=10)
        top.pack(fill="x")

        self.lbl_install = self.make_row(top, "Installation:", 0)
        self.lbl_agent = self.make_row(top, "Agent:", 1)
        self.lbl_cfg = self.make_row(top, "Login config:", 2)
        self.lbl_path = self.make_row(top, "Runtime directory:", 3)
        self.lbl_path.config(text=str(INSTALL_DIR), fg=GREY)

        self.clean_var = tk.BooleanVar(value=True)

        self.toggle_btn = tk.Button(
            root,
            text="Start playitSide",
            command=self.toggle_side_process,
            width=20,
            state="disabled",
        )
        self.toggle_btn.pack(side=tk.RIGHT, padx=15, pady=(6, 0))

        btns = tk.Frame(root, padx=12, pady=10)
        btns.pack(fill="x")
        self.btn_install = tk.Button(btns, text="Install", width=11, command=self.on_install, state="disabled")
        self.btn_start = tk.Button(btns, text="Start", width=11, command=self.on_start, state="disabled")
        self.btn_stop = tk.Button(btns, text="Stop", width=11, command=self.on_stop, state="disabled")
        self.btn_uninstall = tk.Button(btns, text="Uninstall", width=11, command=self.on_uninstall, state="disabled")
        for b in (self.btn_install, self.btn_start, self.btn_stop, self.btn_uninstall):
            b.pack(side="left", padx=4)

        claim = tk.Frame(root, padx=12)
        claim.pack(fill="x")
        self.btn_claim = tk.Button(claim, text="Claim link", command=self.on_claim, state="disabled")
        self.btn_claim.pack(side="left")

        self.log_box = scrolledtext.ScrolledText(root, height=16, state="disabled", font=("Menlo", 11))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=10)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

        provision_secret_if_needed()
        self.refresh()
        self.poll()

    def make_row(self, parent, title, row):
        tk.Label(parent, text=title, font=("TkDefaultFont", 11, "bold"), anchor="w", width=18).grid(
            row=row, column=0, sticky="w", pady=2
        )
        lbl = tk.Label(parent, text="-", anchor="w")
        lbl.grid(row=row, column=1, sticky="w", pady=2)
        return lbl

    def on_unlock(self):
        global access_code
        if self.unlocked:
            return
        if access_code is None:
            self.lbl_gate.config(text="Fetching code...", fg=ORANGE)
            self.root.update_idletasks()
            access_code = get_access_code()
        if access_code is None:
            self.lbl_gate.config(text="Could not fetch code, try again", fg=RED)
            return
        if self.code_var.get().strip() == str(access_code):
            self.unlocked = True
            self.code_entry.config(state="disabled")
            self.btn_unlock.config(state="disabled")
            self.lbl_gate.config(text="Unlocked", fg=GREEN)
            self.refresh()
        else:
            self.lbl_gate.config(text="Wrong code", fg=RED)
            self.code_var.set("")

    def log(self, msg):
        self.q.put(("log", msg))

    def append_log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def is_side_running(self):
        return playitSide_on_mac.running_flag

    def refresh(self):
        binary = get_binary()
        meta = read_meta() or {}
        running = self.is_running()

        if has_cfg(SYSTEM_CFG):
            self.lbl_cfg.config(text="System folder valid config available", fg=GREEN)
        elif has_cfg(BACKUP_CFG_DIR):
            self.lbl_cfg.config(text="Backup directory valid config available", fg=GREEN)
        elif has_cfg(STORE_DIR):
            self.lbl_cfg.config(text="Runtime storage valid config available", fg=ORANGE)
        else:
            self.lbl_cfg.config(text="Config missing (claim it)", fg=GREY)

        if binary:
            self.lbl_install.config(text=f"Installed (version {meta.get('version', '?')})", fg=GREEN)
        else:
            self.lbl_install.config(text="Not installed", fg=RED)

        if running:
            self.lbl_agent.config(text="Running", fg=GREEN)
        else:
            self.lbl_agent.config(text="Stopped", fg=GREY)

        if self.busy:
            self.lbl_agent.config(text="Busy...", fg=ORANGE)

        idle = not self.busy
        free = self.proc is None
        ok = self.unlocked
        self.btn_install.config(state="normal" if (ok and idle and not binary and free) else "disabled")
        self.btn_start.config(state="normal" if (ok and idle and binary and free) else "disabled")
        self.btn_stop.config(state="normal" if (ok and running) else "disabled")
        can_uninstall = idle and free and (INSTALL_DIR.exists() or SYSTEM_CFG.exists())
        self.btn_uninstall.config(state="normal" if (ok and can_uninstall) else "disabled")
        self.btn_claim.config(state="normal" if (ok and self.claim_url) else "disabled")
        self.toggle_btn.config(state="normal" if ok else "disabled")

    def poll(self):
        try:
            try:
                while True:
                    kind, *data = self.q.get_nowait()
                    if kind == "log":
                        self.append_log(data[0])
                        m = URL_RE.search(data[0])
                        if m and "claim" in m.group(0).lower():
                            self.claim_url = m.group(0)
                    elif kind == "busy_done":
                        self.busy = False
                    elif kind == "exited":
                        if data[0] is self.proc:
                            self.proc = None
                            self.append_log("Agent stopped.")
                    self.refresh()
            except queue.Empty:
                pass

            if self.is_side_running():
                self.toggle_btn.config(text="Stop playitSide")
            else:
                self.toggle_btn.config(text="Start playitSide")

        except Exception as e:
            print("poll error:", e)
        finally:
            self.root.after(300, self.poll)

    def on_install(self):
        if not self.unlocked or self.busy:
            return
        self.busy = True
        self.refresh()
        threading.Thread(target=self.do_install, daemon=True).start()

    def do_install(self):
        system = platform.system().lower()
        machine = platform.machine().lower()
        part = None
        try:
            BIN_DIR.mkdir(parents=True, exist_ok=True)
            CFG_DIR.mkdir(parents=True, exist_ok=True)
            self.log("Searching for latest release...")
            req = urllib.request.Request(RELEASE_API, headers={"User-Agent": "tunnel-wizard"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                release = json.load(resp)
            asset = pick_asset(release["assets"], system, machine)
            target = BIN_DIR / asset["name"]
            part = BIN_DIR / (asset["name"] + ".part")
            self.log(f"Downloading release ({release.get('tag_name', '?')})...")
            dl = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "tunnel-wizard"})
            with urllib.request.urlopen(dl, timeout=60) as resp, open(part, "wb") as f:
                expected = resp.headers.get("Content-Length")
                shutil.copyfileobj(resp, f)
            size = part.stat().st_size
            if size == 0 or (expected and size != int(expected)):
                raise RuntimeError("Download corrupted or incomplete")
            os.replace(part, target)
            if system != "windows":
                os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
            META_FILE.write_text(json.dumps({"binary": asset["name"], "version": release.get("tag_name")}))
            self.log("Installation completed.")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                self.log("Installation failed: API rate limit exceeded.")
            else:
                self.log(f"Installation failed: HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            self.log(f"Installation failed: network error ({e.reason})")
        except Exception as e:
            self.log(f"Installation failed: {e}")
        finally:
            if part is not None and part.exists():
                try:
                    part.unlink()
                except Exception:
                    pass
            self.q.put(("busy_done",))

    def on_start(self):
        if not self.unlocked:
            return
        binary = get_binary()
        if not binary or self.proc is not None:
            return
        self.claim_url = None
        self.stage_in()
        try:
            self.proc = subprocess.Popen(
                [str(binary)],
                cwd=str(INSTALL_DIR),
                env=build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as e:
            self.proc = None
            self.sync_out(self.clean_var.get())
            messagebox.showerror("Start failed", str(e))
            return
        self.log("Agent started.")
        threading.Thread(target=self.read_output, args=(self.proc, self.clean_var.get()), daemon=True).start()
        self.refresh()

    def stage_in(self):
        with self.sync_lock:
            try:
                provision_secret_if_needed()
                SYSTEM_CFG.mkdir(parents=True, exist_ok=True)
                STORE_DIR.mkdir(parents=True, exist_ok=True)
                BACKUP_CFG_DIR.mkdir(parents=True, exist_ok=True)

                source_found = None
                if has_cfg(SYSTEM_CFG):
                    source_found = SYSTEM_CFG
                elif has_cfg(BACKUP_CFG_DIR):
                    source_found = BACKUP_CFG_DIR
                elif has_cfg(STORE_DIR):
                    source_found = STORE_DIR

                if source_found:
                    for dest in (SYSTEM_CFG, BACKUP_CFG_DIR, STORE_DIR):
                        if dest != source_found:
                            dest.mkdir(parents=True, exist_ok=True)
                            shutil.copytree(
                                source_found, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs")
                            )
                        normalize_cfg(dest)
            except Exception as e:
                self.append_log(f"Config staging warning: {e}")

    def sync_out(self, clean):
        with self.sync_lock:
            try:
                active_source = None
                if has_cfg(SYSTEM_CFG):
                    active_source = SYSTEM_CFG
                elif has_cfg(STORE_DIR):
                    active_source = STORE_DIR
                elif has_cfg(BACKUP_CFG_DIR):
                    active_source = BACKUP_CFG_DIR

                if active_source:
                    STORE_DIR.mkdir(parents=True, exist_ok=True)
                    BACKUP_CFG_DIR.mkdir(parents=True, exist_ok=True)

                    for target_dir in (SYSTEM_CFG, STORE_DIR, BACKUP_CFG_DIR):
                        if target_dir != active_source:
                            target_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copytree(
                                active_source, target_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("logs")
                            )
                        normalize_cfg(target_dir)

                    if clean and SYSTEM_CFG.exists() and SYSTEM_CFG != active_source:
                        try:
                            remove_tree(SYSTEM_CFG)
                        except Exception:
                            pass
            except Exception as e:
                self.log(f"Config sync-out warning: {e}")

    def read_output(self, proc, clean):
        try:
            for line in iter(lambda: proc.stdout.readline(4096), ""):
                line = line.strip()
                if line:
                    self.log(f"[Agent] {line}")
        except Exception as e:
            self.log(f"Output error: {e}")
            try:
                proc.kill()
            except Exception:
                pass
        proc.wait()
        self.sync_out(clean)
        self.q.put(("exited", proc))

    def on_stop(self):
        if not self.unlocked or not self.is_running():
            return
        self.proc.terminate()

    def on_claim(self):
        if self.unlocked and self.claim_url:
            webbrowser.open(self.claim_url)

    def on_uninstall(self):
        if not self.unlocked or self.proc is not None or self.busy:
            return
        target_paths = [INSTALL_DIR]
        if SYSTEM_CFG.exists():
            target_paths.append(SYSTEM_CFG)
        formatted_paths = "\n".join(str(p) for p in target_paths)
        ok = messagebox.askyesno(
            "Uninstall",
            f"Remove the following directories?\n\n{formatted_paths}",
        )
        if not ok:
            return
        self.claim_url = None
        self.busy = True
        self.refresh()
        threading.Thread(target=self.do_uninstall, daemon=True).start()

    def do_uninstall(self):
        try:
            remove_tree(INSTALL_DIR)
            if SYSTEM_CFG.exists():
                remove_tree(SYSTEM_CFG)
        except Exception as e:
            self.log(f"Uninstall error: {e}")
        self.q.put(("busy_done",))

    def toggle_side_process(self):
        if not self.unlocked:
            return
        if self.is_side_running():
            playitSide_on_mac.stop_viewer()
            self.append_log("Requested playitSide termination.")
            return

        self.side_thread = threading.Thread(target=playitSide_on_mac.run_viewer, daemon=True)
        self.side_thread.start()
        self.append_log("playitSide viewer thread started.")

    def on_close(self):
        if self.is_running():
            if not messagebox.askyesno("Exit", "Agent is currently running. Terminate process and exit?"):
                return
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        if self.is_side_running():
            playitSide_on_mac.stop_viewer()

        self.root.destroy()


if __name__ == "__main__":
    provision_secret_if_needed()
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        if app.is_side_running():
            playitSide_on_mac.stop_viewer()
        if app.is_running():
            app.proc.terminate()
        root.destroy()