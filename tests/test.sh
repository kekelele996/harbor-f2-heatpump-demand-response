#!/usr/bin/env bash
# 评分以 root 运行（task.toml 的 [verifier] user = "root"），候选程序由 test_outputs.py 降权到 runner。
# 任何一步失败都停在 0：reward 只在 pytest 完整跑完、junit 能解析、汇总成功时才被改写。
set -u

REWARD=/logs/verifier/reward.txt
V=/logs/verifier
PRIV=/run/harbor-verify
JUNIT="$PRIV/junit.xml"
PY=/usr/local/bin/python3

mkdir -p "$V"
# 解题阶段可能已在这里放了 reward.json（Harbor 优先读它）或把 reward.txt 换成软链接。
rm -rf "$V/reward.json" "$REWARD"
echo 0 > "$REWARD"
chmod 0444 "$REWARD"

finish() {
    rm -rf "$V/reward.json"
    chmod 0444 "$REWARD" 2>/dev/null
}
on_signal() {
    rm -rf "$V/reward.json" "$REWARD"
    echo 0 > "$REWARD"
    chmod 0444 "$REWARD"
    exit 0
}
trap finish EXIT
trap on_signal INT TERM HUP

# 结束候选残留进程，清空评分目录，把随包的 pytest 解到 root 私有目录（不联网、不装进镜像）。
"$PY" -I /tests/score.py prepare --verifier-dir "$V" --private "$PRIV" --vendor /tests/vendor || exit 0

cd "$PRIV" || exit 0
timeout 540s env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONPATH="$PRIV/site" PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m pytest -q -p no:cacheprovider --rootdir=/tests --junitxml="$JUNIT" /tests/test_outputs.py \
    > "$PRIV/pytest.log" 2>&1
cp "$PRIV/pytest.log" "$V/pytest.log" 2>/dev/null

# 候选留下的后台进程在汇总前全部结束。
"$PY" -I /tests/score.py reap

# 完成证明：junit 必须存在、非空、能解析；否则记 INCOMPLETE，reward 保持 0。
if ! test -s "$JUNIT" || ! "$PY" -I -c 'import sys, xml.etree.ElementTree as ET; ET.parse(sys.argv[1])' "$JUNIT"; then
    echo "INCOMPLETE: pytest 没有产出可解析的 junit 报告" > "$V/verification-status.txt"
    exit 0
fi

rm -rf "$V/reward.json"
"$PY" -I /tests/score.py --config /tests/scoring.json --junit "$JUNIT" \
    --report-out "$V/scoring-report.json" --reward-out "$REWARD" \
    || echo "INCOMPLETE: 汇总失败（用例缺失、出错或被跳过）" > "$V/verification-status.txt"
rm -rf "$V/reward.json"
exit 0
