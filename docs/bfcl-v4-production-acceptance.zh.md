# BFCL V4 生产验收执行指南

按顺序完成镜像验证、真实端点探测、正式提交、故障与权限验收，再接入 BaselineHub 和 Autotune。缓存对照使用单独的实验记录。当前正式配置是 BFCL V4 的五题子集，每类一题，准确率以这五道题为分母。

## 1. 准备验收环境和记录

- 在 Linux AMD64 worker 上执行。准备可从 worker 访问的 OpenAI 兼容端点、实际 served model name，以及由密钥管理系统注入的端点凭据。端点地址可用 base URL 或完整 Chat Completions 路径。凭据放在提交的 `api_key` 字段，URL 不携带用户信息、查询参数或片段。
- 准备 LLMBench 管理员或 service account、另一个用于权限负测的普通账户，以及 backend 和 worker 都能访问的 RWX PVC。用验收环境或专用测试端点做取消、断流和超时测试。
- 记录 LLMBench Git SHA、镜像 digest、BFCL revision、端点部署 ID、模型及模板版本、测试时间、操作者和每次提交的目的。归档中可含模型输出和模拟工具内容，存放在验收团队可访问的位置。
- 以下示例从仓库根目录用 Bash 执行，需要 Docker、Helm、kubectl、curl、jq、Python 3。将示例非敏感变量换成实际值。通过现有密钥管理流程设置 `LLMBENCH_TOKEN` 和 `BFCL_ENDPOINT_API_KEY`，关闭 shell xtrace。无鉴权模型端点可将后者留空。

```bash
set -euo pipefail
set +x
export API_BASE='https://llmbench.example.com/api'
export BFCL_ENDPOINT='https://inference.example.com/v1'
export BFCL_MODEL='served-model-name'
: "${LLMBENCH_TOKEN:?请先注入 LLMBench 管理员或 service account token}"
EVIDENCE="$(mktemp -d)"
git rev-parse HEAD > "$EVIDENCE/git-sha.txt"

api() {
  curl -fsS -H "Authorization: Bearer $LLMBENCH_TOKEN" "$@"
}
```

`API_BASE` 应包含实际代理前缀。若直连集群内 backend Service，通常使用 `http://<service>:8000`。以该部署的 OpenAPI 和 [Autotune API 契约](api/for-autotune.md)为准，区分 LLMBench token 与模型端点 API key。

## 2. 构建镜像并验证 worker

先在 Linux AMD64 构建机或 CI 验证 BFCL 构建阶段。构建时下载锁定依赖，安装和 CLI 自检在无网阶段执行。再在无外网的容器中运行官方契约测试，容器内的模拟端点使用回环地址。

```bash
docker build --platform linux/amd64 --target bfcl-builder \
  -f platform/backend/Dockerfile -t llmbench-bfcl-acceptance .
docker run --rm --platform linux/amd64 --network none \
  -v "$PWD:/app:ro" -w /app llmbench-bfcl-acceptance \
  /opt/bfcl/bin/python -m bench.bfcl.contract_test \
  2>&1 | tee "$EVIDENCE/image-contract.log"
```

**通过条件**：命令退出码为 0。测试确认两题冒烟的流式和非流式官方评分一致，观察到工具结果回填、题间重叠与题内顺序，五类题均有官方评分，探测阶段的请求未计入正式指标。

随后用团队现有镜像发布流程构建并发布同一 SHA 的完整 backend 镜像，记录不可变 tag 和 digest。按 [生产升级流程](deploying.md#upgrades)完成发布。Helm 中为 backend 和 worker 使用同一个镜像版本，并在部署 values 里启用已有的 RWX PVC。保留生产已有 values 和 Secret 配置。

```yaml
image:
  backend:
    repository: registry.example.com/team/llm-bench-backend
    tag: bfcl-acceptance-<git-sha>
bfclArtifacts:
  enabled: true
  existingClaim: llmbench-bfcl-artifacts
```

发布后确认 backend 和 worker 都已滚动完成，并核对 Pod 的实际镜像 ID。下例中的命名空间和 Deployment 名按实际 release 替换。

```bash
NS=llm-bench
RELEASE=llm-bench
WORKER=llm-bench-worker
BACKEND=llm-bench-backend

kubectl -n "$NS" rollout status "deployment/$BACKEND"
kubectl -n "$NS" rollout status "deployment/$WORKER"
kubectl -n "$NS" get pods -l "app.kubernetes.io/instance=$RELEASE" -o json |
  jq '[.items[] | {pod:.metadata.name,
      containers:[.status.containerStatuses[]? | select(.name == "backend" or .name == "worker")
        | {name,image,imageID}]} | select(.containers | length > 0)]' \
  > "$EVIDENCE/pod-image-ids.json"
kubectl -n "$NS" exec "deployment/$WORKER" -c worker -- sh -ec '
  uname -m
  cat /opt/bfcl/llmbench-bfcl-revision
  /opt/bfcl/bin/python -m bfcl_eval generate --help >/dev/null
  /opt/bfcl/bin/python -m bfcl_eval evaluate --help >/dev/null
  test -w "$BFCL_ARTIFACT_DIR"
'
kubectl -n "$NS" exec "deployment/$BACKEND" -c backend -- sh -ec '
  test -r "$BFCL_ARTIFACT_DIR"
'
```

期望 worker 架构为 `x86_64`，revision 与 [冻结 manifest](../bench/bfcl/smoke-manifest.json)一致。API 和 worker 应能通过同一共享路径读取归档。

## 3. 导入配置，探测真实端点

准备两个 active benchmark。探测配置使用单独的 slug，开启探测并将正式部分缩为两题冒烟。生产配置使用仓库五题模板，保持探测关闭。这样第二次提交可以独立选择缓存初始状态。导入使用 multipart 的 `file` 字段。若 slug 已存在，先核对其模块参数与归属。并行验收时为探测配置换一个唯一 slug。

```bash
sed \
  -e 's/^slug: bfcl-v4-tool-use-v1$/slug: bfcl-v4-probe-acceptance/' \
  -e 's/^      task_set: bfcl-v4-tool-use-v1$/      task_set: bfcl-v4-smoke-v1\
      probe_endpoint: true/' \
  benchmarks/bfcl-v4-tool-use-v1.yaml > "$EVIDENCE/probe.yaml"

api -F "file=@$EVIDENCE/probe.yaml" \
  "$API_BASE/benchmarks/admin/benchmarks/import" > "$EVIDENCE/import-probe.json"
api -F "file=@benchmarks/bfcl-v4-tool-use-v1.yaml" \
  "$API_BASE/benchmarks/admin/benchmarks/import" > "$EVIDENCE/import-formal.json"
```

使用下列函数提交、读取状态和取回归档。凭据请求体经标准输入传给 curl，证据目录保存返回 JSON。异步提交成功返回 HTTP 202。实际请求参数应以提交明细和归档中的 manifest 为准。

```bash
submit() {
  local slug="$1" label="$2"
  jq -n --arg endpoint "$BFCL_ENDPOINT" --arg model "$BFCL_MODEL" \
    --arg label "$label" \
    '{endpoint_url:$endpoint, model:$model,
      api_key:(env.BFCL_ENDPOINT_API_KEY // ""), description_summary:$label}' |
  api -H 'Content-Type: application/json' --data-binary @- \
    "$API_BASE/submissions/benchmarks/$slug/submit" |
  tee "$EVIDENCE/$label.submit.json" | jq -er '.id'
}

poll() {
  local id="$1" state
  while true; do
    api "$API_BASE/submissions/$id" > "$EVIDENCE/$id.detail.json"
    state="$(jq -er '.status' "$EVIDENCE/$id.detail.json")"
    printf 'submission %s %s\n' "$id" "$state" >&2
    case "$state" in
      done|failed|canceled) break ;;
    esac
    sleep 3
  done
  jq '{id,status,passed,error,runs:[.runs[] | {id,module_name,status,passed,error,metrics_json}]}' \
    "$EVIDENCE/$id.detail.json"
}

download() {
  local id="$1" run dir
  run="$(jq -er '.runs[] | select(.module_name == "bfcl_v4") | .id' "$EVIDENCE/$id.detail.json")"
  dir="$EVIDENCE/$id"
  mkdir -p "$dir"
  api -o "$dir/artifacts.tar.gz" "$API_BASE/submissions/$id/runs/$run/artifacts"
  tar -tzf "$dir/artifacts.tar.gz" > "$dir/files.txt"
  tar -xzf "$dir/artifacts.tar.gz" -C "$dir"
}

PROBE_ID="$(submit bfcl-v4-probe-acceptance bfcl-probe)"
poll "$PROBE_ID"
download "$PROBE_ID"
jq . "$EVIDENCE/$PROBE_ID/probe/probe.json"
```

**通过条件**：在真实端点的验收记录中，确认路径与 served model name 正确；受保护端点接受有效凭据。在 `probe/probe.json` 中核对 `case_id=multi_turn_base_0`、`tool_call_observed=true`、`tool_result_roundtrip_observed=true`。如果后续要以 usage 排序，核对 `stream_usage_observed`，并比较 `usage_sample_count` 和 `successful_requests`。字段为 false 表示本次未观察到。若模型仅输出文本，用预期会调用工具的候选模型重新验证；结合端点日志区分模型选择和协议拒绝。

确认探测请求的事件 phase 为 `probe`，探测目录没有评分文件或 trace，正式 `request_events.jsonl` 只有 `generate` 事件。探测不参与正式得分和性能统计，但会访问端点。进入冷缓存验收前按预定协议恢复初始状态。

## 4. 运行五题正式配置并复算

```bash
FORMAL_ID="$(submit bfcl-v4-tool-use-v1 bfcl-formal)"
poll "$FORMAL_ID"
download "$FORMAL_ID"
RUN_DIR="$EVIDENCE/$FORMAL_ID"
jq '{name,revision,protocol_hash,concurrency,cases}' "$RUN_DIR/manifest.json"
jq '.metrics | {accuracy,case_count,case_success_count,ttft_p90_ms,ttft_coverage,
     http_attempt_count,request_error_count,infra_error_count,active_cases_peak,
     active_http_peak,generation_wall_seconds,passed_cases_per_hour,
     cache_hit_rate,cache_usage_coverage}' "$RUN_DIR/summary.json"
```

- **覆盖和身份**：`case_results.jsonl` 恰好包含 manifest 中的 5 个唯一 ID，官方 `score/` 中有五类结果。核对 `identity.json` 和事件中的 submission、module run、attempt ID。再提交一次确认 attempt ID 不同，归档互相独立。
- **区分完成与达标**：正常完成应看到 submission 和 module run 都进入 `done`。低准确率仍可属于一次有效测量，是否通过以配置的红线为准。模板使用准确率作为分数，首轮验收不要用一个凭空设定的准确率或延迟阈值判断部署好坏。
- **复算**：`accuracy = case_success_count / 5`，分项按该类覆盖题数计算；`passed_cases_per_hour = case_success_count × 3600 / generation_wall_seconds`；请求 RPS 以 HTTP 尝试数除以生成墙钟。缓存命中率只在同时报告有效输入与缓存 token 的成功请求上算 `Σ cached / Σ input`。缓存缺报保留 null，实测零保留 0。核对样本数和覆盖率，再解读 TTFT 分位数。
- **时序**：按同一题的 `request_index` 排序，检查上一个模型调用的末次尝试结束后才发起下一个调用。用事件的 `started_monotonic` 和 `finished_monotonic` 判断两道题是否有请求重叠。快速真实端点未观察到重叠时，可以在验收端点加可控延迟复测，避免仅凭配置并发数下结论。

## 5. 验收失败、取消和归档权限

在验收环境分别执行下列用例，每项保存时间、提交 ID、module run ID、终态、错误字段和能取到的诊断。提交级 `done` 可能包含一个 BFCL module run 的 `failed`，所以两层状态都要检查。

| 用例 | 操作 | 通过条件 |
| --- | --- | --- |
| 端点鉴权失败 | 给受保护的验收端点提交一个无效的测试 key | module run 为 `failed`，`passed=false`。请求事件显示 401 或 403。归档和错误信息中没有该测试 key。 |
| 断流或缺少生成结果 | 用受控 fixture 在输出分片后断流或省略完成信号。缺题或重复题使用验收专用 runner fixture 制作缺损产物 | module run 为 `failed`，官方部分评分不能把不完整 run 当作有效结果。能读取已落盘的事件和诊断。 |
| 必需用量缺报 | 复制验收 benchmark，设置 `min_usage_coverage: 1`，用能正常回答但省略 usage 的 fixture 跑一轮 | 因覆盖不足失败。再用返回完整 usage 且缓存计数为 0 的 fixture 验证 0 能计入覆盖。 |
| 模块总时限 | 复制验收 benchmark，设置较短 `max_seconds`，用可控制的慢端点让生成越过预算 | 模块在总时限和终止清理窗口后失败，当前子进程停止，并保留部分产物。此用例验收模块总时限。 |
| 生成期间取消 | 在测试端点延迟返回流。看到 `BFCL generate` 后取消提交 | submission 为 `canceled`，当前生成进程和请求停止，后续没有评分产物。 |
| 评分期间取消 | 两题或五题评分通常很快。在隔离的验收 worker 中用指向真实解释器的测试 wrapper 暂停 `--phase evaluate`，看到 `BFCL evaluate` 后取消 | submission 为 `canceled`，评分子进程组退出，已完成的生成产物可读。移除测试 wrapper 后再跑正常提交。 |
| 归档访问与保留 | 分别用提交者、管理员、无关普通用户下载同一个成功归档。用提交者再下载失败归档，并在 worker Pod 重启后复查 | 提交者与管理员得 200，无关用户得 403。归档仍能解压，保留期符合部署策略。已过保留期或归档不存在时为 404。 |

取消使用提交者或管理员身份。取消 API 会先把提交标记为 `canceled`。随后用明细接口确认 module run 结束且有取消记录，并从 worker 和端点日志确认子进程退出、没有继续发请求。已经结束的提交调用取消会返回 409。

```bash
api -X POST "$API_BASE/submissions/$CANCEL_ID/cancel"
poll "$CANCEL_ID"
```

成功归档应包含 `artifacts.json`、`manifest.json`、`identity.json`、`request_events.jsonl`、`case_results.jsonl`、`summary.json` 及官方 `result/`、`score/`。失败或取消的归档按实际进度可能只有其中一部分。检查压缩包没有临时 `config.json`，保留策略验证使用短期限的专用测试位置。

## 6. 联调 BaselineHub 与 Autotune

1. 用 Autotune 的 service account 访问 LLMBench，确认 `GET /auth/me` 返回预期身份，`GET /benchmarks/<slug>` 能找到 active 配置。如果 Autotune 要管理或锁定 benchmark，用它的 service account 导入自有配置。锁定接口是切换操作，先检查 `is_locked`。把预期 slug 绑定到实际部署的 BaselineHub track，记录 track ID、benchmark ID 和配置版本。
2. 从 Autotune 发起一个候选运行。用 `source_url`、提交 ID、module run ID 和候选部署 ID 对齐两边记录。在 LLMBench 明细里找到 `runs[].module_name=bfcl_v4`，逐项对照 `metrics_json` 和 Autotune 实际存储的指标。若 Autotune 使用计划中的命名，至少核对 `bfcl_v4.accuracy`、`bfcl_v4.ttft_p90_ms`、`bfcl_v4.passed_cases_per_hour` 的值和单位。命名以实际读取结果为验收证据。
3. 用步骤 5 的缺报或鉴权失败用例从 Autotune 再发一次。确认失败、取消或必需指标缺失的候选被排除出排序，空值没有被转换为实测零。
4. 分别跑一次正常结束和主动取消。在 Autotune 与部署侧核对候选服务被回收，记录回收时间与部署日志。Autotune 的 track 和回收操作在对应部署执行，LLMBench 只提供提交、状态和取消 API。

## 7. 执行 cache off/on 对照

**闭环对照。** 在部署侧确认缓存开关确实生效。两臂固定模型权重、tokenizer、chat template、tool parser、采样设置、并发、GPU 配置和题集。关闭 `probe_endpoint`，提前写明缓存初始状态与验证方法。使用独立服务或排程避免争用资源。建议先做至少三组成对重复并交替两臂顺序，保存每次的部署 ID、顺序、提交 ID、`protocol_hash`、准确率、请求数、输入输出用量、TTFT、RPS 和缓存覆盖。两臂的 `protocol_hash` 必须一致。闭环下模型可能走不同工具轨迹，解读性能差异时同时对照请求数和 token 工作量。

**固定 Replay 对照。** 单独导入一个 `export_replay_trace: true` 的 BFCL 配置，取一次有效闭环运行的 `trace.jsonl`、`trace-index.jsonl` 和 `summary.json`。使用以下命令校验文件，再把同一份 JSONL 放到两个 Replay worker 都可读的固定数据路径。

```bash
TRACE_DIR="$EVIDENCE/$TRACE_SUBMISSION_ID"
python3 - "$TRACE_DIR" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
trace = (root / "trace.jsonl").read_bytes()
summary = json.loads((root / "summary.json").read_text())
expected = summary["metrics"]["trace_sha256"]
assert hashlib.sha256(trace).hexdigest() == expected, "trace file hash mismatch"
rows = trace.splitlines()
index = (root / "trace-index.jsonl").read_bytes().splitlines()
assert len(rows) == len(index), "trace row count mismatch"
for n, (row, meta) in enumerate(zip(rows, index), start=1):
    request = json.loads(row)
    entry = json.loads(meta)
    assert request["line_no"] == entry["line"] == n
    assert hashlib.sha256(request["request_body"].encode()).hexdigest() == entry["body_sha256"]
print(expected)
PY
```

创建包含 `replay` 模块的独立 benchmark。示例中的路径是 **worker 内**只读数据路径，把冻结文件发布到该位置后，用步骤 3 的文件导入接口导入配置。两个部署提交到同一个 benchmark。 `max_samples: 0` 表示读取完整文件，提交前确认耗时预算足够。

```yaml
slug: bfcl-v4-fixed-replay-acceptance
name: BFCL V4 fixed Replay acceptance
version: "1"
status: active
modules:
  - order_index: 1
    module_name: replay
    weight: 1.0
    params:
      dataset_path: /app/dataset/bfcl/trace.jsonl
      concurrency: 2
      max_samples: 0
      request_timeout: 120
      max_retries: 0
    metric_configs:
      - key: uptime
        role: score
        formula: passthrough
      - key: ttft_p99_ms
        role: display
      - key: cache_hit_rate
        role: display
      - key: cache_usage_coverage
        role: display
```

分别向两臂提交，核对 worker 实际加载的数据身份，再对比 TTFT、吞吐、输出长度和 `cache_usage_coverage`。缓存覆盖不足时将缓存命中结论记为不可比较。当前 Replay 独立调度每条请求，因此此阶段可比较固定输入流量，不验收多轮请求依次抵达时的前缀复用。

## 8. 收尾与判定

将每项记为「通过」「失败」或「待验」，并附上提交 ID、归档、镜像 digest 和日志位置。建议至少分别记录：

| 验收结论 | 所需证据 |
| --- | --- |
| BFCL worker 可用 | AMD64 镜像契约测试、容器内 runner pin、真实端点探测报告 |
| BFCL 可提交并归档 | 五类官方评分、指标复算、取消和失败记录、跨 Pod 下载权限验证 |
| 可进入 Autotune 排序 | track 绑定、指标取值截图或导出、失败候选排除、模型回收记录，以及用同一冻结协议测出的基线与阈值 |
| 可得出缓存对照结论 | 成对运行记录、协议和数据哈希、部署配置差异、usage 覆盖与本次对照的适用范围 |

若任一环节仍待验，保留已经完成的验收结果，并注明缺失证据及责任方。按会话 Replay、严格单请求墙钟中止和 ARM64 BFCL 环境分别按后续范围记录。
