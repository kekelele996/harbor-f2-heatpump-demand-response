#!/usr/bin/env python3
"""评分辅助：清场（prepare / reap）和按 junit 结果加权汇总。

只用标准库。所有路径都由 test.sh 用参数传入，这里不写任何容器内的绝对路径，
审核器会在宿主机上直接调用汇总模式做权重探针。

    score.py prepare --verifier-dir DIR --private DIR --vendor DIR
        结束候选残留进程，清空评分目录，校验并解压随包的 pytest 轮子到 root 私有目录。
    score.py reap
        pytest 结束后再结束一次候选残留进程（后台改写结果的进程）。
    score.py --config scoring.json --junit junit.xml --report-out R.json --reward-out reward.txt
        按 scoring.json 汇总：gate 测试有一个没过记 0；否则累加通过的计分测试权重。
        junit 里出现 error/skipped、用例集合与配置不一致时退出码 2，不写 reward。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

CANDIDATE_USER = "runner"


# ---------- 清场 ----------

def _candidate_uid():
    import pwd

    try:
        return pwd.getpwnam(CANDIDATE_USER).pw_uid
    except KeyError:
        return None


def _stat_fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def _protected_pids():
    """PID 1 和容器启动时随它拉起的保活进程（Harbor 的 sleep infinity）不能杀。"""
    protected = {1}
    try:
        init_start = int(_stat_fields(1)[19])
    except (OSError, ValueError, IndexError):
        return protected
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = _stat_fields(entry.name)
            if int(fields[1]) == 1 and int(fields[19]) - init_start <= 50:  # 0.5 s 内
                protected.add(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return protected


def kill_candidate_processes():
    uid = _candidate_uid()
    if uid is None or uid == os.getuid() or not Path("/proc").is_dir():
        return 0
    protected = _protected_pids()
    killed = 0
    for _ in range(3):  # 进程可能在扫描时 fork，扫三遍
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) in protected:
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                os.kill(int(entry.name), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, FileNotFoundError):
                continue
    return killed


def _remove(path: Path):
    """文件、软链接、目录都删；候选可能把 reward.json 建成目录让 unlink 失败。"""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(verifier_dir: Path, private: Path, vendor: Path) -> int:
    killed = kill_candidate_processes()
    verifier_dir.mkdir(parents=True, exist_ok=True)
    for item in verifier_dir.iterdir():
        if item.name in {"test-stdout.txt", "reward.txt"}:  # Harbor 正在写的输出；reward.txt 已由 test.sh 写 0
            continue
        _remove(item)
    try:
        verifier_dir.chmod(0o755)
    except OSError:
        pass
    shutil.rmtree(private, ignore_errors=True)
    private.mkdir(mode=0o700, parents=True)
    site = private / "site"
    site.mkdir(mode=0o700)
    sums = {}
    for line in (vendor / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, name = line.split()
            sums[name] = digest
    for name, digest in sorted(sums.items()):
        wheel = vendor / name
        if _sha256(wheel) != digest:
            print(f"prepare: {name} 哈希不符", file=sys.stderr)
            return 1
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
    print(f"prepare: killed={killed} wheels={len(sums)}", file=sys.stderr)
    return 0


# ---------- 加权汇总 ----------

def case_id(node: ET.Element) -> str:
    """junit 的 classname/name 换回 pytest 节点 id：test_outputs.py::[Class::]test_name。"""
    parts = node.get("classname", "").split(".")
    split = next((i for i, part in enumerate(parts) if part[:1].isupper()), len(parts))
    module, classes = parts[:split], parts[split:]
    return "::".join([module[-1] + ".py", *classes, node.get("name", "")])


def summarize(config_path: Path, junit: Path, report_out: Path, reward_out: Path) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    gates = list(config["gate_tests"])
    weights = {key: float(value["weight"]) for key, value in config["tests"].items()}
    outcomes = {}
    for node in ET.parse(junit).getroot().iter("testcase"):
        if node.find("error") is not None or node.find("skipped") is not None:
            print(f"incomplete: {case_id(node)} error/skipped", file=sys.stderr)
            return 2
        key = case_id(node)
        if key in outcomes:
            print(f"incomplete: 重复用例 {key}", file=sys.stderr)
            return 2
        outcomes[key] = node.find("failure") is None
    expected = set(gates) | set(weights)
    if set(outcomes) != expected:
        print(f"incomplete: 缺 {sorted(expected - set(outcomes))} 多 {sorted(set(outcomes) - expected)}",
              file=sys.stderr)
        return 2
    gate_passed = all(outcomes[key] for key in gates)
    reward = math.fsum(weight for key, weight in weights.items() if outcomes[key]) if gate_passed else 0.0
    reward = round(min(max(reward, 0.0), 1.0), 3)
    report = {
        "status": "COMPLETED",
        "reward": reward,
        "gate_passed": gate_passed,
        "passed": sorted(key for key, ok in outcomes.items() if ok),
        "failed": sorted(key for key, ok in outcomes.items() if not ok),
    }
    report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp = reward_out.with_name(reward_out.name + ".tmp")
    tmp.write_text(f"{reward:.3f}\n", encoding="utf-8")
    os.replace(tmp, reward_out)
    return 0


def main(argv):
    if argv[:1] == ["prepare"]:
        parser = argparse.ArgumentParser(prog="score.py prepare")
        parser.add_argument("--verifier-dir", required=True, type=Path)
        parser.add_argument("--private", required=True, type=Path)
        parser.add_argument("--vendor", required=True, type=Path)
        args = parser.parse_args(argv[1:])
        return prepare(args.verifier_dir, args.private, args.vendor)
    if argv[:1] == ["reap"]:
        print(f"reap: killed={kill_candidate_processes()}", file=sys.stderr)
        return 0
    parser = argparse.ArgumentParser(prog="score.py")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--report-out", required=True, type=Path)
    parser.add_argument("--reward-out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return summarize(args.config, args.junit, args.report_out, args.reward_out)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(f"incomplete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
