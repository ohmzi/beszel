"""Tests for homelab_maint/inuse.py: a fake /proc tree and tmp dirs, mocked docker/git. Nothing touches the host.

Every probe is exercised both ways: provably unused => unused, any doubt / probe error => used + known=False."""
import os
import subprocess
import time

import conftest  # noqa: F401  (points HOMELAB_MAINT_* at tmp dirs before homelab_maint is imported)
import pytest

from homelab_maint import inuse

ME = os.getpid()


# --------------------------------------------------------------------------- harness
class FakeSh:
    """`sh` stand-in. rows: (prefix, response); response = (rc, stdout, stderr) or callable(cmd_str) -> that."""

    def __init__(self, *rows):
        self.rows, self.calls, self.envs = list(rows), [], []

    def __call__(self, cmd, timeout=60, **kw):
        key = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.calls.append(key)
        self.envs.append(kw.get("env"))
        for prefix, resp in self.rows:
            if key.startswith(prefix):
                rc, out, err = resp(key) if callable(resp) else resp
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 127, "", "unmocked: " + key)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(inuse, "sh", FakeSh())
    clock = [1000.0]
    monkeypatch.setattr(inuse, "_mono", lambda: clock[0])
    yield clock
    inuse.reset_caches()


def mkproc(tmp_path, pid, comm="proc", maps="", cwd=None, exe=None, fds=(), cmdline="", environ="", ppid=1):
    d = tmp_path / "proc" / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    (d / "maps").write_text(maps)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0 0\n")
    (d / "cmdline").write_text(cmdline.replace(" ", "\0"))
    (d / "environ").write_text(environ.replace(" ", "\0"))
    for name, tgt in (("cwd", cwd), ("exe", exe)):
        if tgt:
            os.symlink(tgt, d / name)
    for i, tgt in enumerate(fds):
        os.symlink(tgt, d / "fd" / str(3 + i))
    return d


def mapline(path, dev="08:02", ino=1234, perms="r-xp"):
    return f"7f0000000000-7f0000100000 {perms} 00000000 {dev} {ino}   {path}\n"


def with_me(tmp_path, **kw):
    """our own pid must be visible, or the snapshot is (rightly) not trusted."""
    mkproc(tmp_path, ME, comm="pytest", **kw)


# =========================================================================== Proof
def test_proof_semantics_and_combine():
    u, used, unk = inuse._unused("idle"), inuse._used("busy"), inuse._unknown("no root")
    assert u.unused and not u.used
    assert not used.unused and used.used and used.known
    assert not unk.unused and unk.used and not unk.known and unk.why.startswith("unknown:")
    assert inuse.combine(u, u).unused
    assert inuse.combine(u, used).used and inuse.combine(u, used).known
    c = inuse.combine(u, used, unk)
    assert c.used and not c.known and not c.unused
    assert inuse.combine().used and not inuse.combine().known          # nothing proven => unknown
    assert inuse._a("café" + "x" * 300).isascii() and len(inuse._a("x" * 300)) <= 110


# =========================================================================== /proc snapshot
def test_snapshot_parses_maps_cwd_exe_fd_argv_env(tmp_path):
    with_me(tmp_path)
    mkproc(tmp_path, 100, "java", maps=mapline("/usr/lib/libfoo.so.1") + mapline("/opt/x/y.so (deleted)", ino=7)
           + "7ffd0000-7ffd1000 rw-p 00000000 00:00 0   [stack]\n",
           cwd="/home/u/proj", exe="/usr/bin/java", fds=["/home/u/proj/a.log", "socket:[123]", "/dev/null"],
           cmdline="java -jar /home/u/proj/app.jar --cfg=/etc/app.conf", environ="VIRTUAL_ENV=/home/u/.venv PATH=/home/u/.venv/bin:/usr/bin")
    s = inuse.proc_snapshot()
    assert s.ok and s.nproc == 2 and s.unreadable == 0
    assert s.maps["/usr/lib/libfoo.so.1"] == [100]
    assert "/opt/x/y.so" in s.maps and not any("(deleted)" in k for k in s.maps)      # kernel suffix stripped
    assert ("08:02", 1234) in s.inodes
    assert (100, "cwd") in s.held["/home/u/proj"] and (100, "exe") in s.held["/usr/bin/java"]
    assert (100, "fd") in s.held["/home/u/proj/a.log"] and "/dev/null" in s.held
    assert not any(k.startswith("socket") for k in s.held)
    assert (100, "argv") in s.held["/home/u/proj/app.jar"] and (100, "argv") in s.held["/etc/app.conf"]
    assert (100, "env") in s.held["/home/u/.venv"] and (100, "env") in s.held["/home/u/.venv/bin"]


def test_snapshot_not_ok_when_any_process_unreadable(tmp_path, monkeypatch):
    with_me(tmp_path)
    mkproc(tmp_path, 200, "rootd", maps=mapline("/usr/lib/libx.so"))
    real = inuse._read_text

    def deny(p, limit=-1):
        if "/200/maps" in p:
            raise PermissionError(13, "denied")
        return real(p, limit)

    monkeypatch.setattr(inuse, "_read_text", deny)
    s = inuse.proc_snapshot()
    assert not s.ok and s.unreadable == 1 and "unreadable" in s.err
    # every proof built on it is "unknown" = used + not known, never "unused"
    pr = inuse.lib_mapped_by_processes(["/usr/lib/libx.so"])["/usr/lib/libx.so"]
    assert pr.used and not pr.known and not pr.unused
    assert inuse.process_cwd_or_open_under("/anything").used
    assert not inuse.process_cwd_or_open_under("/anything").known
    assert not inuse.files_in_use(["/a"])["/a"].known


def test_snapshot_ignores_vanished_process_but_needs_ourselves(tmp_path, monkeypatch):
    with_me(tmp_path)
    (tmp_path / "proc" / "300").mkdir()                    # no maps file: it exited while we scanned
    s = inuse.proc_snapshot()
    assert s.ok and s.nproc == 1
    # hidepid-like: our own pid is invisible => we cannot trust the scan
    inuse.reset_caches()
    import shutil
    shutil.rmtree(tmp_path / "proc" / str(ME))
    mkproc(tmp_path, 301, "other")
    s2 = inuse.proc_snapshot()
    assert not s2.ok and "own process" in s2.err
    # empty /proc
    inuse.reset_caches()
    shutil.rmtree(tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    assert not inuse.proc_snapshot().ok
    # missing /proc entirely
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "PROC", tmp_path / "nope")
    s3 = inuse.proc_snapshot()
    assert not s3.ok and "unreadable" in s3.err


def test_snapshot_is_cached_and_refreshable(tmp_path, sandbox):
    with_me(tmp_path)
    s1 = inuse.proc_snapshot()
    mkproc(tmp_path, 400, "late", maps=mapline("/usr/lib/liblate.so"))
    assert inuse.proc_snapshot() is s1                      # within TTL: same object, no second /proc pass
    assert "/usr/lib/liblate.so" not in s1.maps
    s2 = inuse.proc_snapshot(refresh=True)
    assert s2 is not s1 and "/usr/lib/liblate.so" in s2.maps
    sandbox[0] += 31
    mkproc(tmp_path, 401, "later", maps=mapline("/usr/lib/liblater.so"))
    assert "/usr/lib/liblater.so" in inuse.proc_snapshot().maps       # TTL expired => rescanned


def test_own_and_ancestor_argv_env_are_not_use(tmp_path):
    # the runner (and the shell that started it) name paths on their command lines without using them
    mkproc(tmp_path, ME, "pytest", cmdline="pytest /home/u/proj/x.py", ppid=500)
    mkproc(tmp_path, 500, "bash", cmdline="bash -c /home/u/proj/run.sh", ppid=1, cwd="/home/u/other")
    mkproc(tmp_path, 600, "stranger", cmdline="vim /home/u/proj/y.py")
    s = inuse.proc_snapshot()
    assert "/home/u/proj/x.py" not in s.held and "/home/u/proj/run.sh" not in s.held
    assert (600, "argv") in s.held["/home/u/proj/y.py"]
    assert (500, "cwd") in s.held["/home/u/other"]           # but an ancestor's cwd is still real use


# =========================================================================== whole-host view (partial /proc must not read as "unused")
def host_view(tmp_path, total, *, init=True, same_ns=True, loadavg=True, threads_each=1):
    """Make the fake /proc pass or fail the whole-host checks the real /proc gets: pid 1, a pid-namespace link per process,
    task dirs (thread counts) and /proc/loadavg whose 4th field says `total` threads exist on the machine."""
    proc = tmp_path / "proc"
    if init:
        mkproc(tmp_path, 1, "systemd", maps=mapline("/usr/lib/systemd/systemd"))
    for d in proc.iterdir():
        if d.name.isdigit():
            (d / "ns").mkdir(exist_ok=True)
            tgt = "pid:[4026531836]" if (same_ns or d.name != str(ME)) else "pid:[4026539999]"
            if not (d / "ns" / "pid").is_symlink():
                os.symlink(tgt, d / "ns" / "pid")
            for t in range(threads_each):
                (d / "task" / str(int(d.name) + t)).mkdir(parents=True, exist_ok=True)
    if loadavg:
        (proc / "loadavg").write_text(f"0.10 0.20 0.30 1/{total} 12345\n")


@pytest.fixture
def fullview(monkeypatch):
    monkeypatch.setattr(inuse, "FULL_VIEW_CHECK", True)       # the real /proc always gets these checks; a fake tree opts in


def test_whole_host_view_is_accepted(tmp_path, fullview):
    with_me(tmp_path)
    mkproc(tmp_path, 10, "app", maps=mapline("/usr/lib/libx.so"))
    host_view(tmp_path, total=3)
    s = inuse.proc_snapshot()
    assert s.ok and s.nproc == 3 and s.threads == 3
    assert inuse.files_in_use(["/usr/lib/libx.so"])["/usr/lib/libx.so"].used


def test_partial_proc_view_is_not_ok_the_unshare_pid_shape(tmp_path, fullview):
    """`unshare --pid --fork --mount-proc` (or hidepid=2, ProtectProc=invisible, a container): only a few processes are visible
    while the kernel counts thousands of threads. Every file used to look "unused" (ok=True, known=True)."""
    with_me(tmp_path)                                      # the namespace's pid 1 is "us": pid 1 present, same namespace
    host_view(tmp_path, total=7776)
    s = inuse.proc_snapshot()
    assert not s.ok and s.err.startswith("partial /proc view: saw 2 of 7776 threads")
    pr = inuse.files_in_use(["/usr/lib/x86_64-linux-gnu/libgtk-3.so.0"])["/usr/lib/x86_64-linux-gnu/libgtk-3.so.0"]
    assert pr.used and not pr.known and not pr.unused and "partial /proc view" in pr.why
    for call in (lambda: inuse.lib_mapped_by_processes(["/x.so"])["/x.so"], lambda: inuse.process_cwd_or_open_under("/x")):
        assert not call().unused


@pytest.mark.parametrize("seen,total,ok", [(9, 10, True), (10, 10, True), (8, 10, False), (3, 100, False)])
def test_partial_view_threshold_is_ninety_percent_of_the_kernels_thread_count(tmp_path, fullview, seen, total, ok):
    with_me(tmp_path)
    host_view(tmp_path, total=total, threads_each=0)       # pid 1 counts as one thread; give ourselves the rest
    for i in range(seen - 1):
        (tmp_path / "proc" / str(ME) / "task" / str(i)).mkdir(parents=True, exist_ok=True)
    s = inuse.proc_snapshot()
    assert s.threads == seen and s.ok is ok


@pytest.mark.parametrize("kw,why", [({"init": False}, "pid 1 not visible"),
                                    ({"same_ns": False}, "different pid namespace than pid 1"),
                                    ({"loadavg": False}, "/proc/loadavg unreadable")])
def test_partial_view_other_signals_each_fail_closed(tmp_path, fullview, kw, why):
    with_me(tmp_path)
    host_view(tmp_path, total=2, **kw)
    s = inuse.proc_snapshot()
    assert not s.ok and why in s.err


def test_whole_host_checks_apply_to_the_real_proc_only_unless_forced(tmp_path, monkeypatch):
    with_me(tmp_path)                                      # no pid 1, no loadavg, no ns links: a bare fake tree
    assert inuse._whole_host_check() is False and inuse.proc_snapshot().ok     # auto mode on a patched PROC: not enforced
    monkeypatch.setattr(inuse, "PROC", inuse.Path("/proc"))
    assert inuse._whole_host_check() is True                                   # the real /proc: always enforced
    monkeypatch.setattr(inuse, "FULL_VIEW_CHECK", False)
    assert inuse._whole_host_check() is False
    monkeypatch.setattr(inuse, "FULL_VIEW_CHECK", True)
    monkeypatch.setattr(inuse, "PROC", tmp_path / "proc")
    inuse.reset_caches()
    assert not inuse.proc_snapshot().ok


def test_files_in_use_with_nothing_to_check_still_fails_closed_on_an_unusable_proc(tmp_path, monkeypatch):
    with_me(tmp_path)
    assert inuse.files_in_use([]) == {}                    # usable snapshot: an empty request is just empty
    inuse.reset_caches()
    mkproc(tmp_path, 200, "rootd")
    real = inuse._read_text
    monkeypatch.setattr(inuse, "_read_text", lambda p, limit=-1: (_ for _ in ()).throw(PermissionError(13, "x"))
                        if "/200/maps" in p else real(p, limit))
    r = inuse.files_in_use([])
    assert list(r) == [""] and r[""].used and not r[""].known and not r[""].unused
    assert any(x.used for x in r.values())                 # what a caller iterating .values() sees: in use / unknown


def test_all_kinds_sees_what_the_default_kinds_miss(tmp_path):
    with_me(tmp_path)
    mkproc(tmp_path, 21, "python3", cmdline="python3 /usr/bin/solaar --window=hide", environ="LD_PRELOAD=/usr/lib/libshim.so")
    paths = ["/usr/bin/solaar", "/usr/lib/libshim.so"]
    assert all(r.unused for r in inuse.files_in_use(paths).values())             # map/exe/fd/cwd: interpreter scripts are invisible
    r = inuse.files_in_use(paths, kinds=inuse.ALL_KINDS)
    assert r["/usr/bin/solaar"].used and "argv" in r["/usr/bin/solaar"].why and r["/usr/lib/libshim.so"].used


# =========================================================================== lib_mapped_by_processes / files_in_use
def test_lib_mapped_by_path_realpath_inode_and_deleted(tmp_path):
    real = tmp_path / "usr" / "lib"
    real.mkdir(parents=True)
    lib = real / "libreal.so.1"
    lib.write_text("x")
    (tmp_path / "lib").symlink_to(real)                      # merged-usr: /lib -> /usr/lib
    st = os.stat(lib)
    other = tmp_path / "elsewhere.so"
    other.write_text("y")                                    # same inode as `alias` below, mapped under another name
    alias = tmp_path / "alias.so"
    os.link(other, alias)
    ost = os.stat(other)
    with_me(tmp_path)
    mkproc(tmp_path, 10, "app", maps=mapline(str(lib), ino=st.st_ino)
           + mapline("/some/container/path.so", dev=f"{os.major(ost.st_dev):02x}:{os.minor(ost.st_dev):02x}", ino=ost.st_ino)
           + mapline(str(tmp_path / "gone.so") + " (deleted)", ino=999))
    r = inuse.lib_mapped_by_processes([str(tmp_path / "lib" / "libreal.so.1"), str(alias), str(tmp_path / "gone.so"),
                                       str(tmp_path / "free.so"), "/nonexistent/lib.so"])
    assert r[str(tmp_path / "lib" / "libreal.so.1")].used                       # via realpath
    assert r[str(alias)].used and "pid 10 (app)" in r[str(alias)].why           # via dev:inode, other path name
    assert r[str(tmp_path / "gone.so")].used                                    # "(deleted)" mapping still counts
    free = r[str(tmp_path / "free.so")]
    assert free.unused and "not mapped by any of 2 processes" in free.why
    assert r["/nonexistent/lib.so"].unused


def test_files_in_use_kinds(tmp_path):
    with_me(tmp_path)
    mkproc(tmp_path, 20, "d", exe="/usr/bin/daemon", cwd="/srv/data", fds=["/var/lib/x.db"])
    r = inuse.files_in_use(["/usr/bin/daemon", "/var/lib/x.db", "/srv/data", "/usr/bin/idle"])
    assert r["/usr/bin/daemon"].used and r["/var/lib/x.db"].used and r["/srv/data"].used
    assert r["/usr/bin/idle"].unused
    only_map = inuse.files_in_use(["/usr/bin/daemon"], kinds=("map",))
    assert only_map["/usr/bin/daemon"].unused               # exe alone does not count when only mappings are asked for


# =========================================================================== process_cwd_or_open_under
@pytest.mark.parametrize("kw,kind", [({"cwd": "/p/proj/sub"}, "cwd"), ({"fds": ["/p/proj/f.txt"]}, "fd"),
                                     ({"exe": "/p/proj/bin/tool"}, "exe"),
                                     ({"maps": mapline("/p/proj/lib/x.so")}, "map"),
                                     ({"cmdline": "node /p/proj/server.js"}, "argv"),
                                     ({"environ": "VIRTUAL_ENV=/p/proj"}, "env")])
def test_process_under_each_kind(tmp_path, kw, kind):
    with_me(tmp_path)
    mkproc(tmp_path, 30, "tool", **kw)
    pr = inuse.process_cwd_or_open_under("/p/proj")
    assert pr.used and pr.known and "pid 30 (tool)" in pr.why and kind in pr.why
    assert inuse.process_cwd_or_open_under("/p/proj", kinds=[k for k in ("cwd", "exe", "fd", "map", "argv", "env")
                                                              if k != kind]).unused


def test_process_under_boundaries_and_unused(tmp_path):
    with_me(tmp_path)
    mkproc(tmp_path, 40, "sh", cwd="/p", fds=["/p/proj2/a", "/p/projx"], cmdline="ls /p/pro")
    # a cwd ABOVE the path, siblings sharing a name prefix, and shorter prefixes are not use of /p/proj
    pr = inuse.process_cwd_or_open_under("/p/proj")
    assert pr.unused and "2 processes" in pr.why
    # but the exact path itself and anything below it is
    mkproc(tmp_path, 41, "ed", fds=["/p/proj"])
    assert inuse.process_cwd_or_open_under("/p/proj", ).unused          # cached snapshot predates pid 41 ...
    inuse.proc_snapshot(refresh=True)
    assert inuse.process_cwd_or_open_under("/p/proj").used              # ... a refresh sees it


def test_process_under_uses_realpath(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with_me(tmp_path)
    mkproc(tmp_path, 50, "w", cwd=str(real))
    assert inuse.process_cwd_or_open_under(str(link)).used      # asked via the symlink, held via the target


def test_process_under_many_hits_summarised(tmp_path):
    with_me(tmp_path)
    for i in range(6):
        mkproc(tmp_path, 60 + i, f"w{i}", cwd="/p/proj")
    pr = inuse.process_cwd_or_open_under("/p/proj")
    assert pr.used and "(+3 more)" in pr.why


# =========================================================================== docker bind mounts
def docker_sh(containers, fail_inspect=False, short=False):
    """containers: {name: [sources]} -> FakeSh that answers `docker ps` / `docker inspect`."""
    import json
    names = list(containers)
    ids = {n: f"{i:064x}" for i, n in enumerate(names, 1)}

    def inspect(key):
        if fail_inspect:
            return (1, "", "boom")
        lines = [f"/{n}\t" + json.dumps([{"Type": "bind", "Source": s, "Destination": "/d"} for s in containers[n]])
                 for n in names if ids[n] in key]
        return (0, "\n".join(lines[:-1] if short else lines) + "\n", "")

    return FakeSh(("docker ps", (0, "\n".join(ids[n] for n in names) + "\n", "")), ("docker inspect", inspect))


def test_container_bind_mounts_and_mounted_by(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setattr(inuse, "sh", docker_sh({"seerr": [str(tmp_path / "link")], "db": ["/var/lib/db"], "root": ["/"],
                                                "child": [str(real / "sub")], "none": []}))
    m = inuse.container_bind_mounts()
    assert set(m) == {"seerr", "db", "root", "child", "none"}
    assert str(real) in m["seerr"] and str(tmp_path / "link") in m["seerr"]      # raw and resolved
    assert inuse.mounted_by_container(str(real), m).why == "mounted by container child,seerr"   # a child mount and a link both count
    assert inuse.mounted_by_container("/var/lib/db/x", m).used                    # inside a mounted dir
    free = inuse.mounted_by_container("/home/u/free", m)
    assert free.unused and "5 checked" in free.why                                # "/" mounts are ignored, not everything-is-used
    inuse.reset_caches()


def test_container_bind_mounts_fail_closed(monkeypatch):
    monkeypatch.setattr(inuse, "sh", FakeSh(("docker ps", (1, "", "daemon down"))))
    assert inuse.container_bind_mounts() is None
    pr = inuse.mounted_by_container("/x")
    assert pr.used and not pr.known and pr.why.startswith("unknown")
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "sh", docker_sh({"a": ["/x"]}, fail_inspect=True))
    assert inuse.container_bind_mounts() is None
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "sh", docker_sh({"a": ["/x"], "b": ["/y"]}, short=True))   # fewer lines than containers
    assert inuse.container_bind_mounts() is None


def test_container_bind_mounts_batches_and_running_only(monkeypatch):
    f = docker_sh({f"c{i}": [f"/m/{i}"] for i in range(230)})
    monkeypatch.setattr(inuse, "sh", f)
    m = inuse.container_bind_mounts(running_only=True)
    assert len(m) == 230 and sum(1 for c in f.calls if c.startswith("docker inspect")) == 3      # 100 + 100 + 30
    assert f.calls[0] == "docker ps -q --no-trunc"                                              # no -a when running only
    inuse.container_bind_mounts(running_only=True)                                               # cached
    assert sum(1 for c in f.calls if c.startswith("docker ps")) == 1
    inuse.container_bind_mounts()
    assert f.calls[-1].startswith("docker inspect") and "docker ps -a -q --no-trunc" in f.calls


def test_mounted_by_container_json_null(monkeypatch):
    f = FakeSh(("docker ps", (0, "a" * 64 + "\n", "")), ("docker inspect", (0, "/solo\tnull\n", "")))
    monkeypatch.setattr(inuse, "sh", f)
    assert inuse.container_bind_mounts() == {"solo": []}


# =========================================================================== referenced_by
def test_referenced_by_unit_cron_script_and_variants(tmp_path):
    roots = tmp_path / "etc"
    (roots / "systemd").mkdir(parents=True)
    (roots / "systemd" / "svc.service").write_text("[Service]\nExecStart=/home/ohmz/.venv/bin/python /x.py\n")
    (roots / "cron").write_text("0 3 * * * ohmz ~/tools/run.sh\n")
    (roots / "script.sh").write_text("cd $HOME/apps/one && ./go; cd ${HOME}/apps/two; cd ~ohmz/apps/three\n")
    (roots / "near.sh").write_text("/home/ohmz/.venv2/bin/python /home/ohmz/.venv-old /home/ohmz/.venvx\n")
    (roots / "blob.bin").write_bytes(b"\0\0/home/ohmz/binary/ref\0")
    r = inuse.referenced_by_many(["/home/ohmz/.venv", "/home/ohmz/tools/run.sh", "/home/ohmz/apps/one",
                                  "/home/ohmz/apps/two", "/home/ohmz/apps/three", "/home/ohmz/binary/ref",
                                  "/home/ohmz/unref", "relative/path"], [str(roots)])
    assert r["/home/ohmz/.venv"].used and r["/home/ohmz/.venv"].why.endswith("svc.service")
    assert r["/home/ohmz/tools/run.sh"].used and r["/home/ohmz/apps/one"].used      # ~/ and $HOME/ forms
    assert r["/home/ohmz/apps/two"].used and r["/home/ohmz/apps/three"].used        # ${HOME} and ~user forms
    assert r["/home/ohmz/binary/ref"].unused                                        # binary files are not searched
    assert r["/home/ohmz/unref"].unused and "no reference in 5 " in r["/home/ohmz/unref"].why
    assert r["relative/path"].used and not r["relative/path"].known
    # name-boundary: .venv2 / .venv-old / .venvx must not count as a reference to .venv
    only_near = tmp_path / "only"
    only_near.mkdir()
    (only_near / "n.sh").write_text((roots / "near.sh").read_text())
    assert inuse.referenced_by("/home/ohmz/.venv", [str(only_near)]).unused


def test_referenced_by_parent_dir_reference_is_not_use_of_child(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "a.service").write_text("WorkingDirectory=/home/ohmz/StudioProjects/app\n")
    assert inuse.referenced_by("/home/ohmz/StudioProjects/app/dist", [str(d)]).unused
    assert inuse.referenced_by("/home/ohmz/StudioProjects/app", [str(d)]).used


def test_referenced_by_skips_heavy_dirs_and_missing_roots(tmp_path):
    d = tmp_path / "tree"
    (d / "node_modules").mkdir(parents=True)
    (d / "node_modules" / "x.js").write_text("/home/u/hidden\n")
    (d / "big.txt").write_text("/home/u/huge " + "a" * 5000)
    pr = inuse.referenced_by("/home/u/hidden", [str(d), str(tmp_path / "does-not-exist")])
    assert pr.unused                                         # node_modules pruned; a missing root is not an error
    assert inuse.referenced_by("/home/u/huge", [str(d)], max_bytes=100).unused    # over-size files are skipped
    assert inuse.referenced_by("/home/u/huge", [str(d)]).used


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_referenced_by_unreadable_root_is_unknown_not_unused(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    (d / "f").write_text("x")
    d.chmod(0)
    try:
        pr = inuse.referenced_by("/home/u/p", [str(d)])
        assert pr.used and not pr.known and "incomplete" in pr.why
        # ... but a reference found elsewhere is still a definite "used"
        ok = tmp_path / "ok"
        ok.mkdir()
        (ok / "a").write_text("/home/u/p\n")
        assert inuse.referenced_by("/home/u/p", [str(d), str(ok)]).known
    finally:
        d.chmod(0o755)


def test_referenced_by_budget_exhaustion_is_unknown(tmp_path):
    d = tmp_path / "many"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}").write_text("nothing here")
    pr = inuse.referenced_by("/home/u/p", [str(d)], max_files=2)
    assert pr.used and not pr.known and "budget" in pr.why
    ticks = iter(range(0, 1000, 100))
    inuse._mono = lambda: next(ticks)                       # a clock that jumps past the time budget
    try:
        pr = inuse.referenced_by("/home/u/p", [str(d)], budget_s=50)
    finally:
        inuse._mono = time.monotonic
    assert not pr.known


def test_default_ref_roots_never_include_the_tools_own_config():
    roots = inuse.default_ref_roots(["/extra/dir"])
    assert "/etc/systemd/system" in roots and "/usr/local/sbin" in roots and "/extra/dir" in roots
    assert not any("homelab-maint" in r for r in roots)


# =========================================================================== git
def git_sh(*, toplevel, gitdir, ignored=True, tracked="", commit="1700000000\n", status="", rc_rev=0, rc_ign=None,
           rc_ls=0, rc_log=0, log_err="", rc_status=0, rev_err=""):
    rows = [
        ("git -C", lambda key: (
            (rc_rev, f"{toplevel}\n{gitdir}\n" if not rc_rev else "", rev_err) if " rev-parse " in key else
            ((rc_ign if rc_ign is not None else (0 if ignored else 1)), "", "") if " check-ignore " in key else
            (rc_ls, tracked, "") if " ls-files " in key else
            (rc_log, commit if not rc_log else "", log_err) if " log " in key else
            (rc_status, status, "") if " status " in key else (1, "", "unexpected: " + key))),
    ]
    return FakeSh(*rows)


def test_git_state_ignored_untracked_old_repo(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    (repo / ".git" / "logs").mkdir(parents=True)
    (repo / "target").mkdir()
    os.utime(repo / ".git" / "logs", None)
    log = repo / ".git" / "logs" / "HEAD"
    log.write_text("x")
    old = time.time() - 200 * 86400
    os.utime(log, (old, old))
    f = git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), commit=f"{int(old)}\n")
    monkeypatch.setattr(inuse, "sh", f)
    g = inuse.git_state(str(repo / "target"))
    assert g.known and g.in_repo and g.ignored and not g.tracked and g.last_change == 0.0
    assert 199 < g.idle_days() < 201 and "ignored, untracked" in g.why and "repo idle 200 d" in g.why
    # git ran read-only: no optional locks, no prompts, no fsmonitor daemon
    assert all(e == {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"} for e in f.envs)
    assert all("core.fsmonitor=false" in c for c in f.calls)


def test_git_state_tracked_and_not_ignored(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    (repo / "server-rs" / "target").mkdir(parents=True)
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), ignored=False,
                                            tracked="server-rs/target/debug/x\0"))
    g = inuse.git_state(str(repo / "server-rs" / "target"))
    assert g.known and g.tracked and not g.ignored and "NOT ignored, tracked/staged" in g.why


def test_git_state_working_tree_changes_set_activity(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    now = time.time()
    f1 = repo / "a.txt"
    f1.write_text("a")
    os.utime(f1, (now - 3 * 86400, now - 3 * 86400))
    newdir = repo / "newdir"
    newdir.mkdir()
    f2 = newdir / "b.txt"
    f2.write_text("b")
    os.utime(f2, (now - 5 * 86400, now - 5 * 86400))
    os.utime(newdir, (now - 5 * 86400, now - 5 * 86400))
    old = now - 300 * 86400
    status = " M a.txt\0?? newdir/\0R  renamed.txt\0old-name.txt\0"
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), status=status,
                                            commit=f"{int(old)}\n"))
    g = inuse.git_state(str(repo))
    assert g.known and abs(g.last_change - (now - 0)) < 5          # renamed.txt does not exist on disk => "now"
    # without the rename the newest change is the 3-day-old file
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), status=" M a.txt\0?? newdir/\0",
                                            commit=f"{int(old)}\n"))
    g = inuse.git_state(str(repo))
    assert abs(g.last_change - (now - 3 * 86400)) < 5 and 2.9 < g.idle_days() < 3.1


def test_git_state_rename_origin_record_is_not_a_path(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    (repo / "new.txt").write_text("n")
    old = time.time() - 400 * 86400
    os.utime(repo / "new.txt", (old, old))
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"),
                                            status="R  new.txt\0some-origin-that-would-parse-as-a-record\0",
                                            commit=f"{int(old)}\n"))
    g = inuse.git_state(str(repo))
    assert g.known and g.idle_days() > 399               # the origin record was skipped (it would have counted as "deleted => now")


def test_git_state_too_many_changes_counts_as_active(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    status = "".join(f"?? f{i}.txt\0" for i in range(5100))
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), status=status))
    g = inuse.git_state(str(repo))
    assert g.known and g.idle_days() < 1


def test_git_state_reflog_counts_as_activity(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git" / "logs").mkdir(parents=True)
    (repo / ".git" / "logs" / "HEAD").write_text("checkout")      # fresh mtime: someone checked a branch out today
    old = time.time() - 400 * 86400
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), commit=f"{int(old)}\n"))
    assert inuse.git_state(str(repo)).idle_days() < 1


def test_git_state_fresh_repo_without_commits_is_not_idle_forever(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")          # `git init` just ran: HEAD is fresh
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), rc_log=128,
                                            log_err="fatal: your current branch 'main' does not have any commits yet"))
    g = inuse.git_state(str(repo))
    assert g.known and g.last_commit > 0 and g.idle_days() < 1


def test_git_state_no_commits_and_not_a_repo(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), rc_log=128,
                                            log_err="fatal: your current branch 'master' does not have any commits yet"))
    g = inuse.git_state(str(repo))
    assert g.known and g.last_commit == 0.0 and g.idle_days() == float("inf")
    inuse.reset_caches()
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel="", gitdir="", rc_rev=128,
                                            rev_err="fatal: not a git repository (or any of the parent directories): .git"))
    g = inuse.git_state(str(tmp_path))
    assert g.known and not g.in_repo and not g.ignored and g.why == "not in a git repository"


@pytest.mark.parametrize("kw", [dict(rc_rev=128, rev_err="fatal: detected dubious ownership in repository"),
                                dict(rc_ign=128), dict(rc_ls=128), dict(rc_log=128, log_err="fatal: bad object HEAD"),
                                dict(rc_status=1)])
def test_git_state_any_git_error_is_unknown(tmp_path, monkeypatch, kw):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git"), **kw))
    g = inuse.git_state(str(repo))
    assert not g.known and g.why.startswith("unknown")


def test_git_state_rejects_garbage_and_outside_paths(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    repo.mkdir()
    monkeypatch.setattr(inuse, "sh", FakeSh(("git -C", (0, "garbage\n", ""))))
    assert not inuse.git_state(str(repo)).known
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(inuse, "sh", git_sh(toplevel=str(repo), gitdir=str(repo / ".git")))
    g = inuse.git_state(str(other))                          # git says the repo is somewhere else: refuse to guess
    assert not g.known and "outside" in g.why


def test_git_runs_as_the_repo_owner_when_root(tmp_path, monkeypatch):
    import pwd
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    f = git_sh(toplevel=str(repo), gitdir=str(repo / ".git"))
    monkeypatch.setattr(inuse, "sh", f)
    monkeypatch.setattr(inuse, "_euid", lambda: 0)
    inuse.git_state(str(repo))
    owner = pwd.getpwuid(os.stat(repo).st_uid).pw_name
    if os.stat(repo).st_uid != 0:
        assert all(c.startswith(f"runuser -u {owner} -- git -C") for c in f.calls)
    inuse.reset_caches()
    f2 = git_sh(toplevel=str(repo), gitdir=str(repo / ".git"))
    monkeypatch.setattr(inuse, "sh", f2)
    monkeypatch.setattr(inuse, "_euid", lambda: 1000)        # not root: plain git, no runuser
    inuse.git_state(str(repo))
    assert all(c.startswith("git -C") for c in f2.calls)


def test_git_per_repo_activity_is_cached(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    (repo / ".git").mkdir(parents=True)
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    f = git_sh(toplevel=str(repo), gitdir=str(repo / ".git"))
    monkeypatch.setattr(inuse, "sh", f)
    inuse.git_state(str(repo / "a"))
    inuse.git_state(str(repo / "b"))
    assert sum(1 for c in f.calls if " status " in c) == 1    # many build dirs of one project => one `git status`


def test_tree_newest(tmp_path):
    d = tmp_path / "t"
    (d / "sub").mkdir(parents=True)
    a, b = d / "a", d / "sub" / "b"
    a.write_text("a")
    b.write_text("b")
    os.utime(a, (1_000, 1_000))
    os.utime(b, (5_000, 5_000))
    os.utime(d / "sub", (2_000, 2_000))
    os.utime(d, (3_000, 3_000))
    assert inuse.tree_newest(str(d)) == (5_000.0, True)
    assert inuse.tree_newest(str(d), cap=1)[1] is False                       # cap hit => incomplete
    assert inuse.tree_newest(str(tmp_path / "missing")) == (0.0, True)


# =========================================================================== NVIDIA driver
def nv(tmp_path, monkeypatch, sys_v=None, proc_line=None, modinfo=(0, "580.173.02\n", "")):
    if sys_v is not None:
        (tmp_path / "sysver").write_text(sys_v + "\n")
    if proc_line is not None:
        (tmp_path / "procver").write_text(proc_line + "\nGCC version:  gcc\n")
    monkeypatch.setattr(inuse, "SYS_NVIDIA_VERSION", str(tmp_path / "sysver"))
    monkeypatch.setattr(inuse, "PROC_NVIDIA_VERSION", str(tmp_path / "procver"))
    monkeypatch.setattr(inuse, "sh", FakeSh(("modinfo", modinfo)))


PROC_LINE = "NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.173.02  Tue Jun 23 08:38:17 UTC 2026"


def test_loaded_driver_sources_agree(tmp_path, monkeypatch):
    nv(tmp_path, monkeypatch, "580.173.02", PROC_LINE)
    d = inuse.loaded_nvidia_driver()
    assert (d.version, d.branch, d.ondisk) == ("580.173.02", "580", "580.173.02") and "proc+sys" in d.why
    nv(tmp_path, monkeypatch, None, PROC_LINE.replace("Kernel Module", "Open Kernel Module for x86_64"))
    (tmp_path / "sysver").unlink()
    assert inuse.loaded_nvidia_driver().version == "580.173.02"                   # /proc alone is enough


def test_loaded_driver_unknown_cases_are_empty(tmp_path, monkeypatch):
    nv(tmp_path, monkeypatch, "580.173.02", PROC_LINE.replace("580.173.02", "570.124.06"))
    d = inuse.loaded_nvidia_driver()
    assert d.version == "" and "disagree" in d.why                                  # sources disagree
    nv(tmp_path, monkeypatch, "garbage", None)
    assert inuse.loaded_nvidia_driver().version == ""
    monkeypatch.setattr(inuse, "SYS_NVIDIA_VERSION", str(tmp_path / "nothing"))
    monkeypatch.setattr(inuse, "PROC_NVIDIA_VERSION", str(tmp_path / "nothing2"))
    d = inuse.loaded_nvidia_driver()
    assert d.version == "" and "not loaded" in d.why                                # module not loaded at all


def test_loaded_driver_ondisk_is_informational(tmp_path, monkeypatch):
    nv(tmp_path, monkeypatch, "580.173.02", PROC_LINE, modinfo=(0, "575.64.03\n", ""))
    d = inuse.loaded_nvidia_driver()
    assert d.version == "580.173.02" and d.ondisk == "575.64.03"                     # pending-reboot hint, never the truth
    nv(tmp_path, monkeypatch, "580.173.02", PROC_LINE, modinfo=(1, "", "not found"))
    assert inuse.loaded_nvidia_driver().ondisk == ""


# =========================================================================== guards shared by every probe
def test_relative_paths_are_unknown_everywhere(tmp_path):
    with_me(tmp_path)
    for pr in (inuse.process_cwd_or_open_under("relative/x"), inuse.mounted_by_container("rel", {"a": ["/x"]}),
               inuse.files_in_use(["rel"])["rel"], inuse.lib_mapped_by_processes([""])[""]):
        assert pr.used and not pr.known and "not absolute" in pr.why
    g = inuse.git_state("relative/dir")
    assert not g.known and "not absolute" in g.why


def test_referenced_by_that_searched_nothing_is_unknown(tmp_path):
    pr = inuse.referenced_by("/home/u/p", [])
    assert pr.used and not pr.known and "no files were searched" in pr.why
    assert not inuse.referenced_by("/home/u/p", [str(tmp_path / "missing-1"), str(tmp_path / "missing-2")]).known
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not inuse.referenced_by("/home/u/p", [str(empty)]).known                 # a root with no files: still nothing searched
