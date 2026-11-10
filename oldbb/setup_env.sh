#!/bin/bash
# Python environment of an oldbb tree (the original BranchBench, dengn/matrixone-branchbench @95c18b7, + the 4.2.5
# branchbench.patch + our commits on JustinJat/matrixone-branchbench, branch justin/oldbb), 11-10: its own .venv (uv,
# Python 3.13, the 4.2.5 pins + the Neon backend's packages; the resolved versions in .venv-freeze.txt), the protobuf
# modules (as 4.2.5 prepare_branchbench.sh), an import check of the runner and the three backends, a dry run of the
# driver; then the tree's path goes into newbb/OLDBB_DIR. No git on the server: the tree is GitHub's tarball.
# Usage: bash <tree>/oldbb/setup_env.sh <tree>
set -o pipefail
D=$(cd "${1:-$(dirname "$0")/..}" && pwd) || exit 1
T=/ssd_root/wang7693/branchbench/newbb
export UV_CACHE_DIR=$T/uv-cache UV_PYTHON_INSTALL_DIR=$T/uv-python UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy
export PATH=$T/tools/uvenv/bin:$PATH
log() { echo "== [oldbb-setup] $(date -u '+%m-%d %T') $*"; }
die() { log "FAILED: $*"; exit 1; }
command -v uv >/dev/null || die "uv not found in $T/tools/uvenv/bin"
[ -f $D/macrobench/runner.py ] && [ -f $D/macrobench/validation.py ] || die "$D: not the original BranchBench with the 4.2.5 patch"
grep -q DOLT_MYSQL $D/macrobench/task.proto && [ -f $D/dblib/dolt_mysql.py ] || die "$D: dolt_mysql commit missing"
grep -q NEON_COMPUTE_CU $D/dblib/neon.py || die "$D: Neon commit missing"
log "tree $D ($(cat $D/FORK_SOURCE 2>/dev/null))"
[ -x $D/.venv/bin/python ] || uv venv -q --python 3.13 $D/.venv || die "uv venv"
uv pip install -q --python $D/.venv/bin/python -r $D/oldbb/requirements.txt || die "package install"
uv pip freeze --python $D/.venv/bin/python > $D/.venv-freeze.txt
$D/.venv/bin/python -m grpc_tools.protoc -I $D --python_out=$D \
  $D/dblib/result.proto $D/microbench/task.proto $D/microbench/task2.proto $D/macrobench/task.proto || die "protoc"
( cd $D && PYTHONPATH=$D .venv/bin/python -c '
from macrobench import runner, task_pb2 as tp
from dblib.neon import NeonToolSuite
from dblib.dolt_mysql import DoltMySQLToolSuite
from dblib.matrixone import MoToolSuite
print("import ok:", runner.__file__, "| backends", list(tp.Backend.keys()))' ) || die "import check"
C=$(mktemp -d) && rm -rf $C
( cd $D && .venv/bin/python oldbb/run_oldbb.py --backend NEON --scale 1 --output $C --check ) || die "driver dry run"
rm -rf $C
echo $D > $T/OLDBB_DIR
log "done: $D (path in newbb/OLDBB_DIR)"
