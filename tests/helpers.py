"""Test sandbox: fake HOME, fake OneDrive workspaces, fake ~/PhD_local, stub CLIs, test config.

Nothing here touches the real HOME, the real workspaces or any real agent CLI.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CK_RUN = os.path.join(REPO, "bin", "ck-run")
FAKE_CLI = os.path.join(REPO, "tests", "fake_cli.py")
FAKE_OSASCRIPT = os.path.join(REPO, "tests", "fake_osascript.py")
REAL_CONFIG = os.path.join(REPO, "config", "workspaces.json")

DEVFAB_FILES = [
    "Design Library/index.md", "Design Library/maps/ai-retrieval-guide.md",
    "Design Library/questions/open-questions.md", "Design Library/CLAUDE.md", "Design Library/AGENTS.md",
    "Design Library/pages/page.md", ".agents/AGENTS.md", "CLAUDE.md", "AGENTS.md", "Fabrication/run1.md",
    "Projects/CK Design/chip.py", "Projects/Notes/note.md", "Archive/old.md", "Design Yard/x.md",
    "Microwave_Ecosystem/tools/a.py", ".cursor/rules/r.mdc", ".claude/settings.json",
]
NV_FILES = [
    "NV Project/index.md", "NV Project/wiki/maps/ai_retrieval_guide.md",
    "NV Project/wiki/questions/open_questions.md", "NV Project/CLAUDE.md", "NV Project/AGENTS.md",
    "NV Project/wiki/page.md", "CLAUDE.md", "AGENTS.md", "nv_magnetometer_project/data/run.csv",
    "AQS/brief.md", "output/slides/deck.md",
]
# WashU HW (a folder with a space, outside OneDrive): theory II has solutions/ and prep/, quantum II only
# solutions/, classical mechanics neither; the other courses in the config have no folder here at all.
WASHU_FILES = [
    "CLAUDE.md", "HW Brain/CLAUDE.md", "HW Brain/index.md", "HW Brain/maps/ai-retrieval-guide.md",
    "HW Brain/questions/open-questions.md", "HW Brain/log.md",
    "theory II WashU/PS9_5020/PS9_5020.tex", "theory II WashU/theory project plan.md",
    "theory II WashU/solutions/PS9 notes.tex", "theory II WashU/prep/exam plan.md",
    "quantum II WashU/PS4_5240/PS4_5240.tex", "quantum II WashU/solutions/PS4 check.py",
    "classical mechanics WashU/HW1/HW1.pdf", ".claude/settings.local.json", "v242/Launcher/run.sh",
]


def _touch(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Sandbox:
    def __init__(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ckrun-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.onedrive = os.path.join(self.home, "Library", "CloudStorage", "OneDrive-Test")
        self.phd = os.path.join(self.onedrive, "Documents", "PhD")
        self.devfab = os.path.join(self.phd, "Device Fabrication")
        self.nv = os.path.join(self.phd, "NV Compact Magnetometer")
        self.washu = os.path.join(self.home, "Downloads", "WashU HW")
        self.local = os.path.join(self.home, "PhD_local")
        self.runs = os.path.join(self.local, "agent_runs")
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(os.path.join(self.home, ".git"))  # like the real Mac: the home folder is a git repo
        _touch(os.path.join(self.home, ".ssh", "id_rsa"), "FAKE KEY\n")
        for f in DEVFAB_FILES:
            _touch(os.path.join(self.devfab, f))
        for f in NV_FILES:
            _touch(os.path.join(self.nv, f))
        for f in WASHU_FILES:
            _touch(os.path.join(self.washu, f))
        _touch(os.path.join(self.phd, "Other", "notes.md"))
        # a raw folder that is a symlink into ~/PhD_local (like Photonics_Ecosystem/repos)
        repos_real = os.path.join(self.local, "Device Fabrication", "Photonics_Ecosystem", "repos")
        _touch(os.path.join(repos_real, "gdsfactory", "README.md"))
        os.makedirs(os.path.join(self.devfab, "Photonics_Ecosystem"), exist_ok=True)
        os.symlink(repos_real, os.path.join(self.devfab, "Photonics_Ecosystem", "repos"))
        os.makedirs(os.path.join(self.local, "myrepo", ".git"))
        _touch(os.path.join(self.local, "myrepo", "src", "a.py"))
        _touch(os.path.join(self.local, "norepo", "b.py"))
        os.makedirs(os.path.join(self.local, "ck-dispatch"))
        # config: the real one, with paths moved into the sandbox (raw lists stay the real ones)
        with open(REAL_CONFIG) as f:
            cfg = json.load(f)
        cfg["onedrive_root"] = self.onedrive
        cfg["local_root"] = self.local
        cfg["runs_dir"] = self.runs
        cfg["bin_dirs"] = [self.stubs, "/usr/bin", "/bin"]
        cfg["workspaces"]["devfab"]["root"] = self.devfab
        cfg["workspaces"]["nv"]["root"] = self.nv
        cfg["workspaces"]["washu"]["root"] = self.washu
        for key, w in cfg["workspaces"].items():  # a new workspace must get a sandbox root here first
            if not w["root"].startswith(self.tmp + os.sep):
                raise RuntimeError(f"test config would use the real folder of workspace '{key}': {w['root']}")
        # --live stays on by default; the mock osascript fails unless a test says otherwise (see window())
        cfg["live"] = {"default": True, "osascript": os.path.join(self.stubs, "osascript"),
                       "start_timeout_s": 3, "early_exit_s": 20, "poll_s": 0.2}
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as f:
            json.dump(cfg, f, indent=1)
        os.makedirs(self.stubs)
        with open(FAKE_CLI) as f:
            src = f.read()
        for name in ("claude", "codex", "agent", "agy"):
            p = os.path.join(self.stubs, name)
            with open(p, "w") as f:
                f.write(f"#!{sys.executable}\n" + src)
            os.chmod(p, 0o755)
        with open(FAKE_OSASCRIPT) as f:
            src = f.read()
        p = os.path.join(self.stubs, "osascript")
        with open(p, "w") as f:
            f.write(f"#!{sys.executable}\n" + src)
        os.chmod(p, 0o755)
        self.control()

    def window(self, mode):
        """Mock Terminal: 'run' (window runs live.sh), 'silent' (never starts), 'fail' (osascript error)."""
        with open(os.path.join(self.stubs, "osascript.json"), "w") as f:
            json.dump({"mode": mode}, f)

    def set_live(self, **kw):
        """Change the test config's live section, e.g. set_live(terminal="iTerm2")."""
        with open(self.config) as f:
            cfg = json.load(f)
        cfg["live"].update(kw)
        with open(self.config, "w") as f:
            json.dump(cfg, f, indent=1)

    def osascript_calls(self):
        d = os.path.join(self.stubs, "osascript-calls")
        if not os.path.isdir(d):
            return []
        out = []
        for n in sorted(os.listdir(d)):
            with open(os.path.join(d, n)) as f:
                out.append(json.load(f))
        return out

    def env(self, **extra):
        e = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "en_US.UTF-8",
             "TMPDIR": self.tmp + "/", "USER": "tester", "CK_DISPATCH_CONFIG": self.config,
             "SECRET_TOKEN": "s3cr3t-value", "OPENAI_API_KEY": "sk-test-value", "ANTHROPIC_API_KEY": "ak-test-value",
             "GITHUB_TOKEN": "gh-test-value"}
        e.update(extra)
        return e

    def control(self, **per_agent):
        with open(os.path.join(self.stubs, "control.json"), "w") as f:
            json.dump(per_agent, f)
        shutil.rmtree(os.path.join(self.stubs, "calls"), ignore_errors=True)

    def calls(self):
        d = os.path.join(self.stubs, "calls")
        if not os.path.isdir(d):
            return []
        out = []
        for n in sorted(os.listdir(d)):
            with open(os.path.join(d, n)) as f:
                out.append(json.load(f))
        return out

    def task(self, text="Find the current MMI width and cite the page.", name="task.md"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def ck(self, *args, timeout=90):
        return subprocess.run([sys.executable, "-I", CK_RUN, *args], env=self.env(), capture_output=True,
                              text=True, timeout=timeout)

    def run(self, agent="claude", ws="devfab", *extra, title="Test task", task=None):
        # ck-run's default is --mode auto (2026-11-02); older tests were written for read, so pin it unless given
        mode = [] if any(str(x).startswith("--mode") for x in extra) else ["--mode", "read"]
        return self.ck("run", "--agent", agent, "--workspace", ws, "--title", title,
                       "--prompt-file", task or self.task(), *mode, *extra)

    def ledger(self):
        p = os.path.join(self.runs, "ledger.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f if x.strip()]

    def run_id_from(self, proc):
        first = proc.stdout.splitlines()[0]
        return os.path.basename(os.path.dirname(first))

    def result(self, run_id):
        with open(os.path.join(self.runs, run_id, "result.json")) as f:
            return json.load(f)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
