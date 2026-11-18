# 从零部署 LLMBench 并测试 BFCL V4

本指南用 **Linux AMD64 单机 + Docker Compose** 搭一套独立环境。先用内置 mock 验证平台能提交、评分和下载产物，再用真实模型验证原生工具调用。最后跑每类两题的配置，共 **10 道正式评分题**。Kubernetes、权限负测、Autotune 和缓存对照另见 [生产验收执行指南](bfcl-v4-production-acceptance.zh.md)。

## 1. 准备机器和代码

准备能够运行 Docker 的 Linux AMD64 主机。LLMBench worker 和 mock 都跑在 CPU 上，只有真实模型的推理服务需要自己的运行资源。镜像构建要访问基础镜像、Python 和 Node 依赖源、固定的 BFCL 上游源码及 tokenizer 下载源。正式运行时，worker 需要能访问待测模型端点。

以下命令在同一个 Bash 会话中，从 **包含 BFCL 最新改动的 LLMBench checkout 根目录**执行。若代码只在开发机的本地分支或工作区，先通过团队既有交付方式把它带到部署机。下面的检查能避免在缺少最新改动的 checkout 上构建。

```bash
set -euo pipefail
set +x
uname -m                          # 预期 x86_64
docker info --format '{{.OSType}}/{{.Architecture}}'
docker compose version
command -v git openssl curl jq python3
git status --short
git rev-parse --short HEAD
test -f benchmarks/bfcl-v4-tool-use-v1.yaml
grep -En 'cases_per_category|probe_endpoint' bench/modules/bfcl_v4.py
```

镜像内的锁定 BFCL 环境只在 Linux AMD64 构建。若操作者电脑是 Apple Silicon 等 ARM64 设备，建议用一台 Linux AMD64 测试机远程执行这一流程。工作区有未提交改动时，构建会包含这些改动。记录实际使用的源码版本，便于复跑。

## 2. 生成全新的凭据，构建当前 checkout

用独立 Compose project 名隔离测试容器、网络和卷。脚本生成的管理员邮箱和密码用于首次登录。示例把 Web 端口绑定在主机回环地址，远程操作可通过 SSH 转发访问。

```bash
scripts/gen-prod-secrets.sh --env --admin-email admin@your-company.com
chmod 600 .env

export LLMBENCH_VERSION="bfcl-test-$(git rev-parse --short HEAD)"
export LLMBENCH_PORT='127.0.0.1:8080'
PROJECT='llmbench-bfcl-test'
dc() { docker compose -p "$PROJECT" "$@"; }

dc pull postgres redis
dc build backend frontend
dc --profile mock up -d --no-build --pull never
```

把 `admin@your-company.com` 替换为实际管理员邮箱，并把脚本打印的密码保存到密码管理器。已有 `.env` 时，优先换干净的 checkout。已有数据库卷时继续使用原凭据，初始化任务不会替已存在的管理员重置密码。保留 `.env`，它同时保存数据库密码和加密已有模型凭据所用的密钥。

`backend`、`worker`、`migrate` 和 `mock-llm` 使用同一 tag 的 backend 镜像，只需构建一次。用自定义 tag 和 `--pull never` 启动，可确保启动的就是当前 checkout 构建出的版本。Compose 的 `migrate` 会执行数据库迁移并创建第一个管理员、默认 benchmark。BFCL benchmark 稍后单独导入。

如果测试机是远程服务器，在操作电脑另开终端建立转发；命令中的地址换成实际测试机。

```bash
ssh -N -L 8080:127.0.0.1:8080 user@llmbench-test-host
```

然后在浏览器打开 `http://localhost:8080`。本机端口冲突时可选用空闲端口，并把访问 URL 及 `ALLOWED_ORIGINS` 一同改到对应端口。

## 3. 确认平台和 BFCL runner 就绪

在运行 Compose 的测试机上执行：

```bash
dc --profile mock ps -a
dc logs --tail=60 migrate backend worker

WEB_URL='http://localhost:8080'
API_BASE="$WEB_URL/api"

ready=0
for _ in $(seq 1 30); do
  if curl -fsS "$API_BASE/health" | jq -e '.status == "ok"' >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1
curl -fsS "$API_BASE/modules/bfcl_v4" |
  jq -e '.name == "bfcl_v4" and (.params_schema.properties | has("cases_per_category") and has("probe_endpoint"))'

dc exec -T worker sh -ec '
  uname -m
  cat /opt/bfcl/llmbench-bfcl-revision
  "$BFCL_PYTHON" -m bfcl_eval generate --help >/dev/null
  "$BFCL_PYTHON" -m bfcl_eval evaluate --help >/dev/null
  test -w "$BFCL_ARTIFACT_DIR"
'
dc exec -T backend sh -ec 'test -r "$BFCL_ARTIFACT_DIR"'
```

预期 `postgres`、`redis`、`backend`、`worker`、`frontend` 和 `mock-llm` 运行。一次性的 `migrate` 为 `Exited (0)` 是正常状态。`/api/health` 检查 backend 到数据库和 Redis 的连通性，浏览器首页能打开只说明 frontend 可达。worker 显示的 BFCL revision 应与 [冒烟清单](../bench/bfcl/smoke-manifest.json)一致。

可选：用刚构建的完整镜像运行官方 runner 契约测试。测试会在独立容器内启动专用模拟端点，验证流式工具调用、工具结果回填、官方评分和每类两题的覆盖。

```bash
BACKEND_IMAGE="$(dc images -q backend)"
test -n "$BACKEND_IMAGE"
docker run --rm --network none --entrypoint /opt/bfcl/bin/python \
  "$BACKEND_IMAGE" -m bench.bfcl.contract_test
```

## 4. 导入每类两题的 benchmark

用以下命令从仓库模板生成一份验收配置，保留模板的评分规则，并使用独立 slug。正式测试配置保持默认关闭探测。

```bash
umask 077
EVIDENCE="$(mktemp -d)"
FORMAL_SLUG='bfcl-v4-local-2-per-category'
FORMAL_YAML="$EVIDENCE/$FORMAL_SLUG.yaml"

sed \
  -e "s/^slug: bfcl-v4-tool-use-v1$/slug: $FORMAL_SLUG/" \
  -e 's/^name:.*$/name: BFCL V4 local acceptance - 2 per category/' \
  -e 's/^      cases_per_category: 1$/      cases_per_category: 2/' \
  benchmarks/bfcl-v4-tool-use-v1.yaml > "$FORMAL_YAML"
printf '准备导入 %s\n' "$FORMAL_YAML"
```

**用网页导入**：在 `/login` 用第 2 步创建的管理员登录。打开 `/admin/benchmarks`，点击「导入 YAML」，选择刚生成的文件。导入后打开该 benchmark，确认它是 active，只有一个 `bfcl_v4` 模块，`task_set=bfcl-v4-tool-use-v1`，`cases_per_category=2`，并发为 2。若在远程机上生成了文件，需要先把 YAML 拿到浏览器所在电脑，或者直接用下面的 API 导入。

**用 API 导入**：同一个 slug 只需导入一次。下列登录方法使用管理员邮箱和密码。密码通过标准输入进入请求体，Bearer token 保留在当前 shell 中。

```bash
read -r -p 'LLMBench 管理员邮箱: ' ADMIN_LOGIN_EMAIL
read -r -s -p 'LLMBench 管理员密码: ' ADMIN_LOGIN_PASSWORD
printf '\n'
LLMBENCH_TOKEN="$(
  printf '%s' "$ADMIN_LOGIN_PASSWORD" |
    jq -Rs --arg email "$ADMIN_LOGIN_EMAIL" '{email:$email,password:.}' |
    curl -fsS -H 'Content-Type: application/json' --data-binary @- \
      "$API_BASE/auth/login" |
    jq -er '.access_token'
)"
unset ADMIN_LOGIN_PASSWORD

api() { curl -fsS -H "Authorization: Bearer $LLMBENCH_TOKEN" "$@"; }

# 网页已导入时跳过下面这条 POST。
api -F "file=@$FORMAL_YAML" "$API_BASE/benchmarks/admin/benchmarks/import" |
  jq '{id,slug,status}'
api "$API_BASE/benchmarks/$FORMAL_SLUG" |
  jq -e '.status == "active" and any(.modules[];
    .module_name == "bfcl_v4" and .params_json.cases_per_category == 2)'
```

导入返回 409 时，先用 GET 检查已有同名配置。若配置不同，使用另一个 slug 创建副本。

## 5. 先跑一遍内置 mock

**用网页提交**：直接打开 `http://localhost:8080/benchmarks/bfcl-v4-local-2-per-category/submit`，填端点 `http://mock-llm:8000/v1`、模型名 `mock`，API key 留空。提交后在详情页记录 submission ID。

**用 API 提交**：以下三个函数也供下一节测试真实端点。给 `BFCL_MODEL_KEY` 赋值时要 `export`，因为 jq 从环境读它。

```bash
submit_bfcl() {
  local slug="$1" endpoint="$2" model="$3" label="$4"
  jq -n --arg endpoint "$endpoint" --arg model "$model" --arg label "$label" \
    '{endpoint_url:$endpoint, model:$model, api_key:(env.BFCL_MODEL_KEY // ""),
      description_summary:$label}' |
    api -H 'Content-Type: application/json' --data-binary @- \
      "$API_BASE/submissions/benchmarks/$slug/submit" |
    tee "$EVIDENCE/$label.submit.json" | jq -er '.id'
}

poll_bfcl() {
  local id="$1" state attempt
  for attempt in $(seq 1 360); do
    api "$API_BASE/submissions/$id" > "$EVIDENCE/$id.detail.json"
    state="$(jq -er '.status' "$EVIDENCE/$id.detail.json")"
    printf '%s %s\n' "$id" "$state" >&2
    case "$state" in
      done|failed|canceled)
        jq '{id,status,passed,error,runs:[.runs[] | {id,module_name,status,passed,error,metrics_json}]}' \
          "$EVIDENCE/$id.detail.json"
        return 0
        ;;
    esac
    sleep 5
  done
  printf '本地轮询超时，提交可能仍在运行：%s\n' "$id" >&2
  return 1
}

download_bfcl() {
  local id="$1" run_id dir
  run_id="$(jq -er '.runs[] | select(.module_name == "bfcl_v4") | .id' \
    "$EVIDENCE/$id.detail.json")"
  dir="$EVIDENCE/$id"
  mkdir -p "$dir"
  api -o "$dir/artifacts.tar.gz" "$API_BASE/submissions/$id/runs/$run_id/artifacts"
  tar -tzf "$dir/artifacts.tar.gz"
  tar -xzf "$dir/artifacts.tar.gz" -C "$dir"
}

MOCK_ID="$(BFCL_MODEL_KEY='' submit_bfcl "$FORMAL_SLUG" 'http://mock-llm:8000/v1' mock bfcl-mock)"
poll_bfcl "$MOCK_ID"
download_bfcl "$MOCK_ID"
jq -e '.metrics.case_count == 10' "$EVIDENCE/$MOCK_ID/summary.json"
```

已经用网页提交时，直接把对应 submission ID 赋给 `MOCK_ID`，再执行轮询和下载。提交成功后，worker 会从队列领取任务，运行生成与评分。轮询函数返回 0 表示已看到终态，仍需检查 `runs[].status`。module run 正常完成应为 `done`，正式评分覆盖 10 道题。通用 `mock-llm` 的流式路径主要生成文本，准确率低或没有原生工具调用是预期情况。这一步验证平台链路；工具调用的验证用第 3 步的专用 fixture 或下一步的真实端点。

下载后的目录包含 `manifest.json`、官方 `result/` 和 `score/`、`request_events.jsonl`、`case_results.jsonl`、`summary.json`。可用下列命令核对五类各两题：

```bash
jq -r '.cases | to_entries[] | "\(.key)\t\(.value | length)\t\(.value | join(","))"' \
  "$EVIDENCE/$MOCK_ID/manifest.json"
jq -s 'length' "$EVIDENCE/$MOCK_ID/case_results.jsonl"
```

## 6. 换真实端点，先探测再做正式评测

选择 **从 worker 容器实际可访问** 的端点。远程模型服务用其可达 URL 和实际 served model name。若模型跑在 Compose 同一网络的另一个容器里，使用服务名与容器端口。表单里写 `127.0.0.1` 会指向 worker 容器自身。若模型跑在 Linux 宿主机，需显式配置宿主机网关或使用 worker 可达的宿主机 IP，且模型服务需监听可访问的地址。

先生成并导入一个两题冒烟配置，正式两题前会额外跑一次固定多轮探测。这个配置的 slug 与十题正式评测分开。

```bash
PROBE_SLUG='bfcl-v4-local-probe'
PROBE_YAML="$EVIDENCE/$PROBE_SLUG.yaml"
sed \
  -e "s/^slug: bfcl-v4-tool-use-v1$/slug: $PROBE_SLUG/" \
  -e 's/^name:.*$/name: BFCL V4 local endpoint probe/' \
  -e 's/^      task_set: bfcl-v4-tool-use-v1$/      task_set: bfcl-v4-smoke-v1\
      probe_endpoint: true/' \
  benchmarks/bfcl-v4-tool-use-v1.yaml > "$PROBE_YAML"
api -F "file=@$PROBE_YAML" "$API_BASE/benchmarks/admin/benchmarks/import" |
  jq '{id,slug,status}'
```

把下列值替换为真实端点。如果需要鉴权，输入模型 API key；无鉴权时直接回车。它和 LLMBench 登录 token 是两套独立凭据。

```bash
MODEL_URL='https://inference.example.com/v1'
MODEL_NAME='actual-served-model-name'
read -r -s -p '模型 API key（无鉴权直接回车）: ' BFCL_MODEL_KEY
printf '\n'
export BFCL_MODEL_KEY

PROBE_ID="$(submit_bfcl "$PROBE_SLUG" "$MODEL_URL" "$MODEL_NAME" bfcl-real-probe)"
poll_bfcl "$PROBE_ID"
download_bfcl "$PROBE_ID"
jq . "$EVIDENCE/$PROBE_ID/probe/probe.json"
```

确认报告里的 `case_id` 为 `multi_turn_base_0`。重点看 `tool_call_observed`、`tool_result_roundtrip_observed`、`stream_usage_observed` 和完整 usage 样本数。若没有观察到工具调用，结合模型配置和服务端请求记录判断原因；若 HTTP 或官方 runner 失败，优先查 `probe/request_events.jsonl` 和 `probe/runner_error.json`。探测流量已经访问过端点。若下一轮要测冷缓存，先按部署侧约定恢复缓存初始状态。

用探测关闭的十题正式配置提交同一真实端点，取回本次独立产物。

```bash
REAL_ID="$(submit_bfcl "$FORMAL_SLUG" "$MODEL_URL" "$MODEL_NAME" bfcl-real-formal)"
poll_bfcl "$REAL_ID"
download_bfcl "$REAL_ID"
jq '.metrics | {accuracy, case_count, case_success_count,
     ttft_p90_ms, ttft_coverage, active_cases_peak, active_http_peak,
     input_usage_coverage, output_usage_coverage, cache_hit_rate, cache_usage_coverage}' \
  "$EVIDENCE/$REAL_ID/summary.json"
unset BFCL_MODEL_KEY
```

正常结果应覆盖五类各两题，整体准确率等于通过题数除以 10。TTFT 使用正式生成请求，工具调用或文本的首个有效分片都可以提供样本。缓存命中率缺报时可能为 null。需要写验收结论时，保存 submission ID 和归档，并记录 worker 镜像 ID、模型部署版本和 manifest 的 `protocol_hash`。

## 7. 排查和停止

| 现象 | 先检查 |
| --- | --- |
| 容器没起来或 API 返回 502 | `dc --profile mock ps -a`、`dc logs migrate postgres redis backend frontend`。先解决初始化和依赖健康问题。 |
| `bfcl_v4` 不存在或 schema 没有新参数 | backend 是否用了当前 checkout 构建的镜像。确认启动日志已完成模块注册，再检查 `/api/modules/bfcl_v4`。 |
| 平台收到了提交，任务一直 queued | 检查 `dc logs -f worker`、Redis 和 worker 启动状态，再确认没有其他任务占满 worker。 |
| worker 报 BFCL 环境不可用 | 检查镜像架构、`BFCL_PYTHON` 及 `/opt/bfcl` 内容。构建和运行都用 Linux AMD64。 |
| 端点连接失败、401、403 或 404 | 从 worker 视角核对网络、路径、served model name 和模型凭据。用归档的请求事件区分传输错误和模型答题结果。 |
| submission 显示 `done`，没有预期指标 | 检查 `runs[].status` 与 `error`，BFCL module run 可能为 `failed`。看归档或 `GET /submissions/{id}/logs/download`。 |
| 归档接口报 403 / 404 | 确认使用提交者或管理员 token，module run ID 属于该 submission，且两个容器都挂载了 `bfcl_artifacts`。等 worker 完成归档后再试。 |

需要取消排队或正在运行的提交时，从详情页取消，或调用 `POST $API_BASE/submissions/<id>/cancel`。确认 worker 停止处理后再关环境。

临时停止时保留数据库和产物；下次使用同一个 project、`.env` 和镜像 tag 启动。

```bash
dc --profile mock down
unset LLMBENCH_TOKEN
```

普通 `down` 保留命名卷。只有明确要清空此测试环境，并已保存需要的归档时，才考虑用 `down --volumes` 删除测试数据库和归档卷。
